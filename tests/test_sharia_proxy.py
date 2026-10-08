"""SRS FORMAL FLOW — 샤리아 위원회 오디터 대리 모드(위원회 비어 있을 때)."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_sharia_proxy_test.db")
os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_NOTIFY_WORKER", "0")
os.environ.setdefault("GLHAC_FORMAL_FLOW", "0")
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app                    # noqa: E402
from app import models                      # noqa: E402
from app.db import SessionLocal             # noqa: E402


client = TestClient(app)


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _mkcase(c, admin):
    r = c.post("/cases", headers=admin, json={"org_id": "org_demo", "company_name": "ShariaCo"})
    assert r.status_code == 200, r.text
    return r.json()["case_id"]


def _set_status(cid, status):
    db = SessionLocal()
    try:
        ca = db.query(models.CaseApplication).filter_by(case_id=cid).first()
        ca.status = status
        db.commit()
    finally:
        db.close()


def test_대리모드_의견_서명_확정_D19():
    os.environ["GLHAC_FORMAL_FLOW"] = "1"
    try:
        with TestClient(app) as c:
            admin = _tok(c, "admin", "admin")
            auditor = _tok(c, "auditor1", "pw")
            cid = _mkcase(c, admin)
            _set_status(cid, "fatwa_review")
            r = c.post(f"/cases/{cid}/fatwa/vote", headers=auditor, json={"member": "auditor", "vote": "approve", "note": "ok"})
            assert r.status_code == 200, r.text
            tally = r.json()
            assert tally.get("proxy") is True
            assert tally.get("members") == 1
            assert tally.get("quorum_need") == 1
            assert tally.get("progress") == "1/1"
            assert tally.get("result") == "passed"
            r = c.post(f"/cases/{cid}/fatwa/sign", headers=auditor, json={"member": "auditor", "image": "data:image/png;base64,iVBORw0KGgo=", "name": "auditor1"})
            assert r.status_code == 200, r.text
            r = c.post(f"/cases/{cid}/fatwa/confirm", headers=auditor)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body.get("decision") == "approved"
            gid = body.get("gen_doc_id")
            assert gid
            r = c.get(f"/gen-docs/{gid}", headers=auditor)
            assert r.status_code == 200, r.text
            content = r.json().get("content")
            assert '"proxy": true' in content
    finally:
        os.environ["GLHAC_FORMAL_FLOW"] = "0"


def test_대리모드_키검증_선행조건():
    os.environ["GLHAC_FORMAL_FLOW"] = "1"
    try:
        with TestClient(app) as c:
            admin = _tok(c, "admin", "admin")
            auditor = _tok(c, "auditor1", "pw")
            cid = _mkcase(c, admin)
            _set_status(cid, "fatwa_review")
            r = c.post(f"/cases/{cid}/fatwa/vote", headers=auditor, json={"member": "notacommittee", "vote": "approve", "note": "x"})
            assert r.status_code == 422, r.text
            assert r.json()["detail"]["code"] == "NOT_COMMITTEE_MEMBER"
            r = c.post(f"/cases/{cid}/fatwa/confirm", headers=auditor)
            assert r.status_code == 409, r.text
            assert r.json()["detail"]["code"] == "VOTES_INCOMPLETE"
            r = c.post(f"/cases/{cid}/fatwa/vote", headers=auditor, json={"member": "auditor", "vote": "conditional", "note": "cons"})
            assert r.status_code == 200, r.text
            assert r.json().get("result") == "conditional"
            r = c.post(f"/cases/{cid}/fatwa/confirm", headers=auditor)
            assert r.status_code == 409, r.text
            assert r.json()["detail"]["code"] == "SIGNATURES_INCOMPLETE"
    finally:
        os.environ["GLHAC_FORMAL_FLOW"] = "0"


def test_대리모드_안건목록():
    os.environ["GLHAC_FORMAL_FLOW"] = "1"
    try:
        with TestClient(app) as c:
            admin = _tok(c, "admin", "admin")
            auditor = _tok(c, "auditor1", "pw")
            cid = _mkcase(c, admin)
            _set_status(cid, "fatwa_review")
            op = _tok(c, "operator1", "pw")
            aid = SessionLocal().query(models.User).filter_by(username="auditor1").first().user_id
            assert c.post("/ops/cases/%s/assign-auditor" % cid, headers=op, json={"auditor_id": aid}).status_code == 200
            r = c.get("/sharia/agenda", headers=auditor)
            assert r.status_code == 200, r.text
            items = r.json()
            if isinstance(items, dict):
                items = items.get("items", [])
            assert any(it.get("proxy") is True for it in items)
    finally:
        os.environ["GLHAC_FORMAL_FLOW"] = "0"


def test_위원회_등록되면_오디터_차단():
    os.environ["GLHAC_FORMAL_FLOW"] = "1"
    try:
        with TestClient(app) as c:
            admin = _tok(c, "admin", "admin")
            auditor = _tok(c, "auditor1", "pw")
            fatwa = _tok(c, "fatwa1", "pw")
            r = c.post("/fatwa/committee", headers=fatwa, json={"members": [{"role": "chair", "name": "Ahmad"}, {"role": "member", "name": "Budi"}, {"role": "member", "name": "Citra"}]})
            assert r.status_code == 200, r.text
            cid = _mkcase(c, admin)
            _set_status(cid, "fatwa_review")
            r = c.post(f"/cases/{cid}/fatwa/vote", headers=auditor, json={"member": "auditor", "vote": "approve", "note": "x"})
            assert r.status_code == 403, r.text
            r = c.post(f"/cases/{cid}/fatwa/sign", headers=auditor, json={"member": "auditor", "image": "data:image/png;base64,iVBORw0KGgo=", "name": "auditor1"})
            assert r.status_code == 403, r.text
            r = c.post(f"/cases/{cid}/fatwa/confirm", headers=auditor)
            assert r.status_code == 403, r.text
            r = c.post(f"/cases/{cid}/fatwa/vote", headers=fatwa, json={"member": "chairman", "vote": "approve", "note": "ok"})
            assert r.status_code == 200, r.text
    finally:
        os.environ["GLHAC_FORMAL_FLOW"] = "0"
