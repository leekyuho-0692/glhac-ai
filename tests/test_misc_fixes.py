"""MISC FIXES — 사전심사 반려 사유 통지·D-02R 열람 제한·회원 정지/해제."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_misc_test.db")
os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_NOTIFY_WORKER", "0")
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app                    # noqa: E402
from app import models                      # noqa: E402
from app.db import SessionLocal             # noqa: E402


REVIEW = {"sections": {"documents": {"ok": True, "note": ""}, "materials": {"ok": True, "note": ""}, "process": {"ok": True, "note": ""}}, "verdict": "ready", "note": "가능"}


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _mkcase(c, admin):
    r = c.post("/cases", headers=admin, json={"org_id": "org_demo", "company_name": "MiscCo"})
    assert r.status_code == 200, r.text
    return r.json()["case_id"]


def _review(c, auditor, cid, body):
    r = c.post("/cases/%s/preassess/review" % cid, headers=auditor, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _uid(username):
    db = SessionLocal()
    try:
        u = db.query(models.User).filter_by(username=username).first()
        assert u is not None, username
        return u.user_id
    finally:
        db.close()


def test_불가_판정_사유_통지():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        auditor = _tok(c, "auditor1", "pw")
        client = _tok(c, "applicant1", "pw")
        consultant = _tok(c, "consultant1", "pw")
        cid = _mkcase(c, admin)

        bad = {"sections": REVIEW["sections"], "verdict": "reject", "note": ""}
        r = c.post("/cases/%s/preassess/review" % cid, headers=auditor, json=bad)
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "REASON_REQUIRED", r.text

        inv = {"sections": REVIEW["sections"], "verdict": "x", "note": "사유"}
        r = c.post("/cases/%s/preassess/review" % cid, headers=auditor, json=inv)
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert detail["code"] == "INVALID_VERDICT", r.text
        assert len(detail.get("allowed", [])) == 3, r.text

        rej = {"sections": REVIEW["sections"], "verdict": "reject", "note": "서류 미비"}
        out = _review(c, auditor, cid, rej)
        assert out["verdict"] == "reject", out


        db = SessionLocal()
        try:
            evs = db.query(models.Notification).filter(
                models.Notification.event_type == "preassess.reject"
            ).all()
            roles = set()
            for e in evs:
                for attr in ("recipient_role", "role", "target_role"):
                    v = getattr(e, attr, None)
                    if v:
                        roles.add(v)
            assert "client" in roles, roles
            assert "consultant" in roles, roles
        finally:
            db.close()

        assert client is not None and consultant is not None


def test_D02R_기업_열람_샤리아_비공개():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        applicant = _tok(c, "applicant1", "pw")
        fatwa = _tok(c, "fatwa1", "pw")
        cid = _mkcase(c, admin)

        r = c.get("/cases/%s/preassess-report.pdf" % cid, headers=applicant)
        assert r.status_code == 200, r.text
        assert "application/pdf" in r.headers.get("content-type", ""), r.headers

        r = c.get("/cases/%s/preassess-report.pdf" % cid, headers=fatwa)
        assert r.status_code == 403, r.text


def test_회원_정지_해제():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        consultant = _tok(c, "consultant1", "pw")
        uid = _uid("consultant1")

        r = c.post("/admin/users/%s/suspend" % uid, headers=admin, json={"reason": "테스트"})
        assert r.status_code == 200, r.text
        assert r.json().get("status") == "suspended", r.text

        r = c.post("/auth/login", json={"username": "consultant1", "password": "pw"})
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["code"] == "USER_SUSPENDED", r.text

        r = c.get("/cases", headers=consultant)
        assert r.status_code in (401, 403), r.text

        r = c.get("/admin/users", headers=admin)
        assert r.status_code == 200, r.text
        items = r.json()
        items = items.get("items", items) if isinstance(items, dict) else items
        me = [x for x in items if x.get("username") == "consultant1"]
        assert me and me[0].get("suspended") is True, items

        r = c.post("/admin/users/%s/unsuspend" % uid, headers=admin)
        assert r.status_code == 200, r.text
        _tok(c, "consultant1", "pw")


def test_정지_검증():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        admin_uid = _uid("admin")

        r = c.post("/admin/users/%s/suspend" % admin_uid, headers=admin, json={"reason": "테스트"})
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "CANNOT_SUSPEND_SELF", r.text

        r = c.post("/admin/users/nope-uid/suspend", headers=admin, json={"reason": "테스트"})
        assert r.status_code == 404, r.text
