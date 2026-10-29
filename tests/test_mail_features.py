"""Mail recovery and QR sharing: isolated DB, no real messages are sent."""
import os
import re

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


def temp_from(sent):
    return re.search(r"임시 비밀번호: (\S+)", sent[-1][2]).group(1)


def test_temp_password_mail_then_login_once(setup):
    c, sessions, uid, _, old, sent, *_ = setup
    assert c.post("/auth/password-reset/request", json={"email": "CLIENT@example.com"}).status_code == 200
    assert sent[0][0] == "CLIENT@example.com" and "아이디: client@example.com" in sent[0][2]
    pw = temp_from(sent)
    assert re.fullmatch(r"[A-Za-z2-9]{4}-[A-Za-z2-9]{4}-[A-Za-z2-9]{4}", pw)
    with sessions() as db:
        u = db.get(models.User, uid)
        # 요청만으로는 기존 비밀번호·세션이 그대로다(남이 요청해도 주인이 안 쫓겨남)
        assert auth.verify_pw("Before123!", u.password_hash)
        assert not mf.redeem_temp_password(db, u, "Wrong-Pass-1234")
        assert mf.redeem_temp_password(db, u, pw)
        db.commit()
    assert c.get("/me", headers=old).status_code == 401           # 기존 세션 해제
    with sessions() as db:
        u = db.get(models.User, uid)
        assert auth.verify_pw(pw, u.password_hash)                  # 임시 비번이 새 비번
        assert not auth.verify_pw("Before123!", u.password_hash)
        assert not mf.redeem_temp_password(db, u, pw)               # 한 번만 '임시'로 인정


def test_temp_password_expires(setup, monkeypatch):
    c, sessions, uid, _, _, sent, *_ = setup
    c.post("/auth/password-reset/request", json={"email": "client@example.com"})
    pw = temp_from(sent)
    future = mf.time.time() + 2 * mf.TEMP_PW_WINDOW + 1
    monkeypatch.setattr(mf.time, "time", lambda: future)
    with sessions() as db:
        assert not mf.redeem_temp_password(db, db.get(models.User, uid), pw)


def test_contact_email_also_receives_temp_password(setup):
    c, sessions, *_ , sent, _, _ = setup
    with sessions() as db:
        db.add(models.User(username="auditor01", password_hash=auth.hash_pw("Before123!"), role="auditor",
                           org_id="org_demo", token_version=0, contact_email="Staff@Example.com"))
        db.commit()
    assert c.post("/auth/password-reset/request", json={"email": "staff@example.com"}).status_code == 200
    assert len(sent) == 1 and "아이디: auditor01" in sent[0][2]


def test_both_login_routes_accept_temp_password():
    src = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "main.py"), encoding="utf-8").read()
    for route in ('@app.post("/auth/login")', '@app.post("/auth/handoff/login")'):
        body = src[src.index(route):][:1500]
        assert "_mail_features.redeem_temp_password(db, u, body.password)" in body, route


def test_unknown_email_same_response_and_rate_limit(setup):
    c, _, _, _, _, sent, *_ = setup
    known = c.post("/auth/password-reset/request", json={"email": "client@example.com"})
    unknown = c.post("/auth/password-reset/request", json={"email": "unknown@example.com"})
    assert known.json() == unknown.json()
    assert len(sent) == 1
    for _ in range(2):
        assert c.post("/auth/password-reset/request", json={"email": "client@example.com"}).status_code == 200
    assert c.post("/auth/password-reset/request", json={"email": "client@example.com"}).status_code == 429


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
