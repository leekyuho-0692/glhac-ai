"""메일 발송 기록 — 관리자가 보낸 메일 목록·내용·실패 사유를 본다.

지키는 성질
  · 알림 메일(발송 워커)·임시 비밀번호·영업 QR 세 경로 모두 1행씩 남는다(실패도).
  · 임시 비밀번호는 기록에 남지 않는다(가림) — 관리자가 남의 계정에 들어갈 수 있으면 안 된다.
  · 목록·내용은 admin 전용, 내용 열람은 감사로그(mail_log.view)에 남는다.
  · 기록 실패가 발송·알림 상태 갱신을 깨지 않는다.

실행: <venv>/bin/python -m pytest tests/test_mail_log.py -q
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_mail_log_test.db")

import pytest  # noqa: E402
from fastapi import FastAPI, HTTPException, Response  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.main as m  # noqa: E402
import app.models as models  # noqa: E402
import app.notify as nt  # noqa: E402
from app import auth, mail_delivery, mail_features as mf, mail_log  # noqa: E402
from app.db import get_db  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    eng = create_engine("sqlite:///%s" % (tmp_path / "ml.db"), connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(bind=eng)
    S = sessionmaker(bind=eng)
    with S() as db:
        db.add_all([
            models.User(user_id="u-client", username="client@example.com", password_hash=auth.hash_pw("Before123!"),
                        role="applicant", org_id="org_a", token_version=0),
            models.User(user_id="u-cons", username="cons@example.com", password_hash=auth.hash_pw("Before123!"),
                        role="consultant", org_id="org_a", token_version=0),
            models.User(user_id="u-admin", username="boss", password_hash=auth.hash_pw("Before123!"),
                        role="admin", org_id="org_a", token_version=0),
            models.User(user_id="u-op", username="op", password_hash=auth.hash_pw("Before123!"),
                        role="operator", org_id="org_a", token_version=0),
        ])
        db.commit()
        tok = {u.user_id: auth.make_tokens(u)["token"] for u in db.query(models.User)}

    def database():
        with S() as db:
            yield db
    app = FastAPI()
    app.dependency_overrides[get_db] = database
    mf.register_routes(app, lambda db, owner: type("I", (), {"code": "OWN"})(),
                       lambda url, fmt: Response(b"png", media_type="image/png"), "https://glhac.com")
    app.get("/admin/mail-log")(m.admin_mail_log)
    app.get("/admin/mail-log/{log_id}")(m.admin_mail_log_detail)
    monkeypatch.setenv("GLHAC_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("GLHAC_SMTP_FROM", "sender@example.com")
    sent = []
    monkeypatch.setattr(mail_delivery, "send", lambda *a: sent.append(a))
    mf._hits.clear()
    with TestClient(app) as c:
        yield c, S, tok, sent
    eng.dispose()


def _h(t):
    return {"Authorization": "Bearer " + t}


def test_temp_password_logged_but_masked(env):
    c, S, tok, sent = env
    assert c.post("/auth/password-reset/request", json={"email": "client@example.com"}).status_code == 200
    pw = re.search(r"임시 비밀번호: (\S+)", sent[-1][2]).group(1)
    with S() as db:
        row = db.query(models.MailLog).one()
        assert row.kind == "password_reset" and row.status == "sent" and row.user_id == "u-client"
        assert row.recipient == "client@example.com" and row.subject == "[GL-HAC] 임시 비밀번호 안내"
        assert pw not in row.body and "가림" in row.body and "아이디: client@example.com" in row.body
    # 메일 제목에 [GL-HAC] 가 두 번 붙지 않는다(mail_delivery 가 붙인다)
    assert sent[-1][1] == "임시 비밀번호 안내"


def test_failed_send_logged_with_reason(env, monkeypatch):
    c, S, tok, sent = env

    def boom(*a):
        e = HTTPException(503, {"code": "EMAIL_SEND_FAILED"})
        e.mail_error = "SMTPAuthenticationError: (535, 'nope')"
        raise e
    monkeypatch.setattr(mail_delivery, "send", boom)
    assert c.post("/consultant/qr/email", json={"email": "buyer@example.com"},
                  headers=_h(tok["u-cons"])).status_code == 503
    with S() as db:
        row = db.query(models.MailLog).one()
        assert row.kind == "consultant_qr" and row.status == "failed" and row.actor_id == "u-cons"
        assert "535" in row.error


def test_notification_email_logged_from_worker(env, monkeypatch):
    c, S, tok, sent = env
    with S() as db:
        case = models.CaseApplication(case_id="c-ml", org_id="org_a", company_name="CV. ML",
                                      email="owner@company.test", notify_consent=True)
        db.add(case)
        db.commit()
        m._notify(db, case, "audit_scheduled", "현장심사 예정", "CV. ML — 11/02",
                  channels=["inapp", "email"], role="applicant")
        db.commit()
        monkeypatch.setitem(nt.PROVIDERS, "email",
                            lambda contacts, text, n=None: {"channel": "email", "ok": True})
        m.drain_notifications(db)
        row = db.query(models.MailLog).one()
        assert row.kind == "notification" and row.status == "sent" and row.case_id == "c-ml"
        assert row.recipient == "owner@company.test" and "현장심사 예정: CV. ML — 11/02" == row.body
        assert db.query(models.Notification).filter_by(case_id="c-ml").one().status == "sent"


def test_admin_only_list_detail_and_audit(env):
    c, S, tok, sent = env
    c.post("/auth/password-reset/request", json={"email": "client@example.com"})
    with S() as db:
        mail_log.add(db, "notification", "x@other.test", "[GL-HAC] 인증서 발급", "본문", False, "smtp_error:boom",
                     case_id="c-1")
        db.commit()
    assert c.get("/admin/mail-log", headers=_h(tok["u-op"])).status_code == 403
    assert c.get("/admin/mail-log", headers=_h(tok["u-cons"])).status_code == 403
    r = c.get("/admin/mail-log", headers=_h(tok["u-admin"])).json()
    assert r["total"] == 2 and r["counts"] == {"sent": 1, "failed": 1}
    assert all("body" not in x for x in r["items"])                         # 목록엔 본문 없음
    assert c.get("/admin/mail-log?status=failed", headers=_h(tok["u-admin"])).json()["total"] == 1
    assert c.get("/admin/mail-log?kind=password_reset", headers=_h(tok["u-admin"])).json()["total"] == 1
    hit = c.get("/admin/mail-log?q=OTHER.test", headers=_h(tok["u-admin"])).json()   # 암호화 주소도 검색
    assert hit["total"] == 1 and hit["items"][0]["error"] == "smtp_error:boom"
    lid = hit["items"][0]["log_id"]
    assert c.get("/admin/mail-log/" + lid, headers=_h(tok["u-op"])).status_code == 403
    d = c.get("/admin/mail-log/" + lid, headers=_h(tok["u-admin"])).json()
    assert d["body"] == "본문" and d["case_id"] == "c-1"
    with S() as db:
        assert db.query(models.AuditLog).filter_by(action="mail_log.view", resource_id=lid).count() == 1
    assert c.get("/admin/mail-log/none", headers=_h(tok["u-admin"])).status_code == 404


def test_log_failure_does_not_break_caller(env, monkeypatch):
    c, S, tok, sent = env
    monkeypatch.setattr(models, "MailLog", None)          # 기록 생성이 터져도
    assert c.post("/auth/password-reset/request", json={"email": "client@example.com"}).status_code == 200
    assert len(sent) == 1                                  # 메일은 나간다
