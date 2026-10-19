"""Mail recovery and QR sharing: isolated DB, no real messages are sent."""
import os
from urllib.parse import unquote

os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_SECRET", "test-mail-" + "x" * 40)

import pytest
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import auth, mail_delivery, mail_features as mf, models
from app.db import Base, get_db


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "mail.db"), connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        user = models.User(username="client@example.com", password_hash=auth.hash_pw("Before123!"),
                           role="applicant", org_id="org_demo", token_version=0)
        consultant = models.User(username="consultant@example.com", password_hash=auth.hash_pw("Before123!"),
                                 role="consultant", org_id="org_demo", token_version=0)
        db.add_all([user, consultant]); db.commit()
        uid = user.user_id
        headers = {"Authorization": "Bearer " + auth.make_tokens(consultant)["token"]}
        old_headers = {"Authorization": "Bearer " + auth.make_tokens(user)["token"]}
    def database():
        with sessions() as db:
            yield db
    app = FastAPI()
    app.dependency_overrides[get_db] = database
    owners, urls, sent = [], [], []
    def invite(db, owner):
        owners.append(owner)
        return type("Invite", (), {"code": "OWNCODE"})()
    def qr(url, fmt):
        urls.append((url, fmt))
        return Response(b"qr-png", media_type="image/png")
    mf.register_routes(app, invite, qr, "https://glhac.com")
    @app.get("/me")
    def me(user=Depends(auth.get_current_user)):
        return user
    monkeypatch.setenv("GLHAC_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("GLHAC_SMTP_FROM", "sender@example.com")
    monkeypatch.setenv("GLHAC_APP_URL", "https://app.example.com")
    monkeypatch.setattr(mail_delivery, "send", lambda *args: sent.append(args))
    mf._hits.clear()
    with TestClient(app) as client:
        yield client, sessions, uid, headers, old_headers, sent, owners, urls
    engine.dispose()


def token_from(sent):
    return unquote(sent[-1][2].split("#reset=", 1)[1])


def test_reset_once_and_revokes_sessions(setup):
    c, sessions, uid, _, old, sent, *_ = setup
    assert c.post("/auth/password-reset/request", json={"email": "CLIENT@example.com"}).status_code == 200
    token = token_from(sent)
    assert "https://app.example.com/ui/v4/#reset=" in sent[0][2]
    assert c.get("/me", headers=old).status_code == 200
    body = {"token": token, "new_password": "After123!"}
    assert c.post("/auth/password-reset/confirm", json=body).status_code == 200
    assert c.post("/auth/password-reset/confirm", json=body).status_code == 400
    assert c.get("/me", headers=old).status_code == 401
    with sessions() as db:
        assert auth.verify_pw("After123!", db.get(models.User, uid).password_hash)


def test_unknown_email_same_response_and_rate_limit(setup):
    c, _, _, _, _, sent, *_ = setup
    known = c.post("/auth/password-reset/request", json={"email": "client@example.com"})
    unknown = c.post("/auth/password-reset/request", json={"email": "unknown@example.com"})
    assert known.json() == unknown.json()
    assert len(sent) == 1
    for _ in range(2):
        assert c.post("/auth/password-reset/request", json={"email": "client@example.com"}).status_code == 200
    assert c.post("/auth/password-reset/request", json={"email": "client@example.com"}).status_code == 429


def test_expired_tampered_and_weak_reset(setup, monkeypatch):
    c, _, _, _, _, sent, *_ = setup
    c.post("/auth/password-reset/request", json={"email": "client@example.com"})
    token = token_from(sent)
    assert c.post("/auth/password-reset/confirm", json={"token": token, "new_password": "short"}).status_code == 422
    assert c.post("/auth/password-reset/confirm", json={"token": token + "x", "new_password": "After123!"}).status_code == 400
    future = mf.time.time() + 1801
    monkeypatch.setattr(mf.time, "time", lambda: future)
    assert c.post("/auth/password-reset/confirm", json={"token": token, "new_password": "After123!"}).status_code == 400


def test_qr_image_and_link_belong_to_sender(setup):
    c, _, _, headers, old, sent, owners, urls = setup
    assert c.post("/consultant/qr/email", json={"email": "customer@example.com"}).status_code == 401
    assert c.post("/consultant/qr/email", json={"email": "customer@example.com"}, headers=old).status_code == 403
    assert c.post("/consultant/qr/email", json={"email": "customer@example.com", "consultant_id": "someone-else"}, headers=headers).status_code == 200
    assert owners == [auth.verify_token(headers["Authorization"][7:])["uid"]]
    url = "https://glhac.com/?ref=OWNCODE"
    assert urls == [(url, "png")]
    assert url in sent[-1][2] and url in sent[-1][3]
    assert 'cid:sales-qr' in sent[-1][3] and sent[-1][4] == b"qr-png"


@pytest.mark.parametrize("email", ["bad", "a@example.com,b@example.com", "a@example.com\r\nBcc:b@example.com"])
def test_bad_email_rejected(setup, email):
    c, _, _, headers, _, sent, *_ = setup
    assert c.post("/consultant/qr/email", json={"email": email}, headers=headers).status_code == 422
    assert not sent


def test_missing_smtp_is_not_success(setup, monkeypatch):
    c, _, _, headers, *_ = setup
    monkeypatch.delenv("GLHAC_SMTP_HOST")
    assert c.post("/consultant/qr/email", json={"email": "customer@example.com"}, headers=headers).status_code == 503
    assert c.post("/auth/password-reset/request", json={"email": "unknown@example.com"}).status_code == 503


def test_smtp_mime_and_tls(monkeypatch):
    monkeypatch.setenv("GLHAC_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("GLHAC_SMTP_FROM", "sender@example.com")
    monkeypatch.setenv("GLHAC_SMTP_PORT", "587")
    messages, tls = [], []
    class SMTP:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def starttls(self, **kwargs): tls.append(True)
        def login(self, *args): pass
        def send_message(self, message): messages.append(message); return {}
    monkeypatch.setattr(mail_delivery.smtplib, "SMTP", SMTP)
    mail_delivery.send("customer@example.com", "QR", "https://glhac.com/?ref=X",
                       '<img src="cid:sales-qr">https://glhac.com/?ref=X', b"png")
    assert tls == [True]
    parts = list(messages[0].walk())
    assert any(p.get_content_type() == "image/png" and p["Content-ID"] == "<sales-qr>" for p in parts)
    def failure(*args, **kwargs): raise OSError("private details")
    monkeypatch.setattr(mail_delivery.smtplib, "SMTP", failure)
    with pytest.raises(HTTPException) as error:
        mail_delivery.send("customer@example.com", "test", "test", "test")
    assert error.value.status_code == 503
    assert "private details" not in str(error.value.detail)
