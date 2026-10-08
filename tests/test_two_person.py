"""SRS AUD-03~06·MEM-06 — 메인·서브 오디터 2인 확인."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_twoperson_test.db")
os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_NOTIFY_WORKER", "0")
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app                    # noqa: E402
from app import models                      # noqa: E402
from app.db import SessionLocal             # noqa: E402


REVIEW = {"sections": {"documents": {"ok": True, "note": ""}, "materials": {"ok": True, "note": ""}, "process": {"ok": True, "note": ""}}, "verdict": "supplement", "note": "보완"}


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _mkcase(c, admin):
    r = c.post("/cases", headers=admin, json={"org_id": "org_demo", "company_name": "TwoPersonCo"})
    assert r.status_code == 200, r.text
    return r.json()["case_id"]


def _uid(username):
    db = SessionLocal()
    try:
        u = db.query(models.User).filter_by(username=username).first()
        return u.user_id
    finally:
        db.close()


def _add_co(c, admin, cid, username):
    r = c.post("/cases/%s/auditors" % cid, headers=admin, json={"username": username})
    assert r.status_code == 200, r.text
    return r.json()


def _events(cid, action):
    db = SessionLocal()
    try:
        return db.query(models.WorkflowEvent).filter_by(case_id=cid, action=action).all()
    finally:
        db.close()


def test_서브_없으면_즉시_실행():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        a1 = _tok(c, "auditor1", "pw")
        cid = _mkcase(c, admin)
        r = c.post("/cases/%s/preassess/review" % cid, headers=a1, json=REVIEW)
        assert r.status_code == 200, r.text
        j = r.json()
        assert not j.get("pending")
        assert j.get("verdict") == "supplement", r.text


def test_서브_있으면_대기_후_서브_승인시_실행():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        a1 = _tok(c, "auditor1", "pw")
        a2 = _tok(c, "auditor2", "pw")
        cid = _mkcase(c, admin)
        _add_co(c, admin, cid, "auditor2")
        r = c.post("/cases/%s/preassess/review" % cid, headers=a1, json=REVIEW)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j.get("pending") is True, r.text
        aid = j.get("approval_id")
        assert aid
        assert len(_events(cid, "preassess.review.requested")) == 1
        r = c.get("/cases/%s/preassess/review" % cid, headers=a1)
        assert r.status_code == 200, r.text
        assert r.json().get("reviewed") is False, r.text
        r = c.post("/approvals/%s/approve" % aid, headers=a2, json={})
        assert r.status_code == 200, r.text
        assert r.json().get("status") == "approved", r.text
        r = c.get("/cases/%s/preassess/review" % cid, headers=a1)
        assert r.status_code == 200, r.text
        assert r.json().get("reviewed") is True, r.text
        assert len(_events(cid, "two_person.confirmed")) == 1


def test_서브는_판정_호출_불가():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        a2 = _tok(c, "auditor2", "pw")
        cid = _mkcase(c, admin)
        _add_co(c, admin, cid, "auditor2")
        r = c.post("/cases/%s/preassess/review" % cid, headers=a2, json=REVIEW)
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["code"] == "NOT_MAIN_AUDITOR", r.text


def test_본인_승인_금지와_타인_불가():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        a1 = _tok(c, "auditor1", "pw")
        a3 = _tok(c, "auditor3", "pw")
        cid = _mkcase(c, admin)
        _add_co(c, admin, cid, "auditor2")
        r = c.post("/cases/%s/preassess/review" % cid, headers=a1, json=REVIEW)
        assert r.status_code == 200, r.text
        aid = r.json().get("approval_id")
        assert aid
        r = c.post("/approvals/%s/approve" % aid, headers=a1, json={})
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["code"] == "SELF_APPROVAL_FORBIDDEN", r.text
        r = c.post("/approvals/%s/approve" % aid, headers=a3, json={})
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["code"] == "NOT_A_CHECKER", r.text


def test_되돌림은_의견_필수_알림_기록():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        a1 = _tok(c, "auditor1", "pw")
        a2 = _tok(c, "auditor2", "pw")
        cid = _mkcase(c, admin)
        _add_co(c, admin, cid, "auditor2")
        r = c.post("/cases/%s/preassess/review" % cid, headers=a1, json=REVIEW)
        assert r.status_code == 200, r.text
        aid = r.json().get("approval_id")
        assert aid
        r = c.post("/approvals/%s/reject" % aid, headers=a2, json={})
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["code"] == "OPINION_REQUIRED", r.text
        r = c.post("/approvals/%s/reject" % aid, headers=a2, json={"reason": "재료 근거 부족"})
        assert r.status_code == 200, r.text
        assert r.json().get("status") == "rejected", r.text
        assert len(_events(cid, "preassess.review.returned")) == 1
        db = SessionLocal()
        try:
            n = db.query(models.Notification).filter_by(case_id=cid, event_type="two_person.returned").count()
        finally:
            db.close()
        assert n >= 1
        r = c.get("/cases/%s/preassess/review" % cid, headers=a1)
        assert r.status_code == 200, r.text
        assert r.json().get("reviewed") is False, r.text


def test_중복_요청_409와_취소():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        a1 = _tok(c, "auditor1", "pw")
        a2 = _tok(c, "auditor2", "pw")
        cid = _mkcase(c, admin)
        _add_co(c, admin, cid, "auditor2")
        r = c.post("/cases/%s/preassess/review" % cid, headers=a1, json=REVIEW)
        assert r.status_code == 200, r.text
        aid = r.json().get("approval_id")
        assert aid
        r = c.post("/cases/%s/preassess/review" % cid, headers=a1, json=REVIEW)
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "APPROVAL_PENDING", r.text
        r = c.post("/approvals/%s/cancel" % aid, headers=a2, json={})
        assert r.status_code == 403, r.text
        r = c.post("/approvals/%s/cancel" % aid, headers=a1, json={})
        assert r.status_code == 200, r.text
        assert r.json().get("status") == "cancelled", r.text
        r = c.post("/cases/%s/preassess/review" % cid, headers=a1, json=REVIEW)
        assert r.status_code == 200, r.text
        assert r.json().get("pending") is True, r.text


def test_승인함_목록_can_decide():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        a1 = _tok(c, "auditor1", "pw")
        a2 = _tok(c, "auditor2", "pw")
        a3 = _tok(c, "auditor3", "pw")
        cid = _mkcase(c, admin)
        _add_co(c, admin, cid, "auditor2")
        r = c.post("/cases/%s/preassess/review" % cid, headers=a1, json=REVIEW)
        assert r.status_code == 200, r.text
        aid = r.json().get("approval_id")
        assert aid
        r = c.get("/approvals", headers=a2)
        assert r.status_code == 200, r.text
        items = r.json()
        if isinstance(items, dict):
            items = items.get("items", [])
        mine = [x for x in items if x.get("approval_id") == aid]
        assert mine, r.text
        assert mine[0].get("can_decide") is True, r.text
        assert mine[0].get("two_person") is True, r.text
        assert mine[0].get("checker_roles") == ["co_auditor"], r.text
        r = c.get("/approvals", headers=a3)
        assert r.status_code == 200, r.text
        items3 = r.json()
        if isinstance(items3, dict):
            items3 = items3.get("items", [])
        assert not [x for x in items3 if x.get("approval_id") == aid], r.text


def test_strict_모드_서브_없으면_409():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        a1 = _tok(c, "auditor1", "pw")
        cid = _mkcase(c, admin)
        old = os.environ.get("GLHAC_TWO_PERSON")
        os.environ["GLHAC_TWO_PERSON"] = "strict"
        try:
            r = c.post("/cases/%s/preassess/review" % cid, headers=a1, json=REVIEW)
            assert r.status_code == 409, r.text
            assert r.json()["detail"]["code"] == "CO_AUDITOR_REQUIRED", r.text
        finally:
            if old is None:
                os.environ.pop("GLHAC_TWO_PERSON", None)
            else:
                os.environ["GLHAC_TWO_PERSON"] = old


def test_메인_배정시_서브_자동배정():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        op = _tok(c, "operator1", "pw")
        cid = _mkcase(c, admin)
        uid1 = _uid("auditor1")
        r = c.post("/ops/cases/%s/assign-auditor" % cid, headers=op, json={"auditor_id": uid1})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j.get("co_auditor_id") is not None, r.text
        assert j.get("co_auditor_id") != uid1, r.text
        assert len(_events(cid, "auditor.co_auto_assigned")) == 1
        r = c.get("/cases/%s/auditors" % cid, headers=op)
        assert r.status_code == 200, r.text
        co = r.json().get("co", [])
        assert len(co) == 1, r.text


def test_마지막_서브_삭제는_대기요청_있으면_409():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        a1 = _tok(c, "auditor1", "pw")
        cid = _mkcase(c, admin)
        _add_co(c, admin, cid, "auditor2")
        uid2 = _uid("auditor2")
        r = c.post("/cases/%s/preassess/review" % cid, headers=a1, json=REVIEW)
        assert r.status_code == 200, r.text
        aid = r.json().get("approval_id")
        assert aid
        r = c.delete("/cases/%s/auditors/%s" % (cid, uid2), headers=admin)
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "LAST_CO_AUDITOR_PENDING", r.text
        r = c.post("/approvals/%s/cancel" % aid, headers=a1, json={})
        assert r.status_code == 200, r.text
        r = c.delete("/cases/%s/auditors/%s" % (cid, uid2), headers=admin)
        assert r.status_code == 200, r.text
