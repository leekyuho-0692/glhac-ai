"""알림 이메일 채널 — SMTP(네이버웍스) 연결 후 업체 알림이 실제로 메일로도 나가는지.

배경: 메일 발송 코드는 있었지만 어떤 알림도 channels 에 "email" 을 넣지 않아, SMTP 를
설정해도 알림 메일은 한 통도 나가지 않았다(운영 실측: 알림 82건 중 email 채널 0건).
관리자 채널 상태(/admin/notify-channels)에도 email 이 없어 설정 여부가 안 보였다.

여기서 지키는 성질
  · SMS 로 보내는 알림은 이메일로도 보낸다(같은 수신동의 게이트를 탄다).
  · 채널 상태에 email 이 _send_email 과 같은 조건으로 표시된다.
  · 587 STARTTLS → 로그인 → 발송 순서, 표시 이름이 붙은 FROM 도 봉투 주소로 정상 변환된다.

실행: <venv>/bin/python -m pytest tests/test_email_channel.py -q
"""
import ast
import os
import smtplib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_email_channel_test.db")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.main as m  # noqa: E402
import app.models as models  # noqa: E402
import app.notify as nt  # noqa: E402

MAIN = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "main.py")
SMTP_KEYS = ("GLHAC_SMTP_HOST", "GLHAC_SMTP_PORT", "GLHAC_SMTP_USER", "GLHAC_SMTP_PASS",
             "GLHAC_SMTP_FROM", "GLHAC_SMTP_SSL")


def _clear_smtp(monkeypatch):
    for k in SMTP_KEYS:
        monkeypatch.delenv(k, raising=False)


def test_every_sms_notification_also_requests_email():
    """main.py 의 channels=[...] 리터럴 중 "sms" 가 있는 것은 전부 "email" 도 있어야 한다."""
    tree = ast.parse(open(MAIN, encoding="utf-8").read())
    found = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "channels" and isinstance(node.value, ast.List):
            vals = [e.value for e in node.value.elts if isinstance(e, ast.Constant)]
            if "sms" in vals:
                found += 1
                assert "email" in vals, "line %d: %r" % (node.value.lineno, vals)
    assert found >= 10


def test_channel_status_reports_email(monkeypatch):
    _clear_smtp(monkeypatch)
    assert nt.channel_status()["email"]["status"] == "unset"
    monkeypatch.setenv("GLHAC_SMTP_HOST", "smtp.worksmobile.com")
    assert nt.channel_status()["email"]["status"] == "unset"      # 보내는 주소 없음
    monkeypatch.setenv("GLHAC_SMTP_USER", "noreply@glhac.com")
    st = nt.channel_status()["email"]
    assert st["status"] == "connected" and st["configured"] is True
    assert "noreply" not in str(nt.channel_status())              # 값은 노출하지 않는다


class _FakeSMTP:
    calls = []

    def __init__(self, host, port, timeout=None):
        self.calls.append(("connect", host, port))

    def starttls(self, *a, **k):
        self.calls.append(("starttls",))

    def login(self, user, pw):
        self.calls.append(("login", user))

    def sendmail(self, frm, to, msg):
        self.calls.append(("sendmail", frm, tuple(to), msg))

    def quit(self):
        self.calls.append(("quit",))


def test_send_email_starttls_login_send(monkeypatch):
    _clear_smtp(monkeypatch)
    monkeypatch.setenv("GLHAC_SMTP_HOST", "smtp.worksmobile.com")
    monkeypatch.setenv("GLHAC_SMTP_PORT", "587")
    monkeypatch.setenv("GLHAC_SMTP_USER", "noreply@glhac.com")
    monkeypatch.setenv("GLHAC_SMTP_PASS", "x")
    monkeypatch.setenv("GLHAC_SMTP_FROM", "GL-HAC <noreply@glhac.com>")
    _FakeSMTP.calls = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    n = models.Notification(title="현장심사 예정", body="CV. A — 11/02")
    r = nt._send_email({"email": "owner@company.test"}, "현장심사 예정: CV. A", n)
    assert r == {"channel": "email", "ok": True}
    kinds = [c[0] for c in _FakeSMTP.calls]
    assert kinds == ["connect", "starttls", "login", "sendmail", "quit"]
    assert _FakeSMTP.calls[0][1:] == ("smtp.worksmobile.com", 587)
    send = _FakeSMTP.calls[3]
    assert send[2] == ("owner@company.test",)
    assert "From: GL-HAC <noreply@glhac.com>" in send[3]
    # 실제 smtplib 은 표시 이름이 붙은 FROM 을 봉투 주소로 바꿔 MAIL FROM 에 쓴다
    assert smtplib.quoteaddr(send[1]) == "<noreply@glhac.com>"


def _db(tmp_path):
    eng = create_engine("sqlite:///%s" % (tmp_path / "email.db"))
    models.Base.metadata.create_all(bind=eng)
    return sessionmaker(bind=eng)()


def test_applicant_notice_reaches_company_email_with_consent(tmp_path, monkeypatch):
    db = _db(tmp_path)
    c = models.CaseApplication(case_id="c-mail", org_id="org-mail", company_name="CV. MAIL",
                               email="owner@company.test", phone="010-1111", notify_consent=True)
    db.add(c)
    db.commit()
    m._notify(db, c, "audit_scheduled", "현장심사 예정", "CV. MAIL — 11/02",
              channels=["inapp", "sms", "email"], role="applicant")
    db.commit()
    seen = []

    def fake(ch):
        def _f(contacts, text, n=None):
            seen.append((ch, contacts.get("email")))
            return {"channel": ch, "ok": True}
        return _f
    monkeypatch.setitem(nt.PROVIDERS, "sms", fake("sms"))
    monkeypatch.setitem(nt.PROVIDERS, "email", fake("email"))
    m.drain_notifications(db)
    assert ("email", "owner@company.test") in seen

    # 수신동의가 없으면 이메일도 SMS 와 똑같이 빠진다
    seen.clear()
    c.notify_consent = False
    m._notify(db, c, "audit_scheduled", "현장심사 예정", "CV. MAIL — 11/03",
              channels=["inapp", "sms", "email"], role="applicant")
    db.commit()
    m.drain_notifications(db)
    assert not [s for s in seen if s[0] == "email"]
    db.close()
