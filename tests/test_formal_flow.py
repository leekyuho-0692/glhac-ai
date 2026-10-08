"""SRS FORMAL FLOW — AI 2차·판정·정식신청·접수·owner/step8 흐름."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_formal_test.db")
os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_NOTIFY_WORKER", "0")
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app                    # noqa: E402
from app import models                      # noqa: E402
from app.db import SessionLocal             # noqa: E402


REVIEW = {"sections": {"documents": {"ok": True, "note": ""}, "materials": {"ok": True, "note": ""}, "process": {"ok": True, "note": ""}}, "verdict": "ready", "note": "가능"}
FORMAL = {"process_summary": "원료 입고→혼합→충전→포장", "halal_management": "할랄 감독자 상주·월 1회 내부심사", "non_halal_handling": False, "non_halal_desc": "", "preferred_audit_period": "2026-11", "sales_channels": "국내 온라인·인니 수출", "pledge": True, "signer_name": "홍길동", "signature": "data:image/png;base64,iVBORw0KGgo="}


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _mkcase(c, admin):
    r = c.post("/cases", headers=admin, json={"org_id": "org_demo", "company_name": "FormalCo"})
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


def _review(c, auditor, cid):
    r = c.post("/cases/%s/preassess/review" % cid, headers=auditor, json=REVIEW)
    assert r.status_code == 200, r.text
    return r.json()


def _ai2(c, auditor, cid):
    r = c.post("/cases/%s/ai-second-analysis" % cid, headers=auditor, json={})
    assert r.status_code == 200, r.text
    return r.json()


def _eligible(c, auditor, cid):
    r = c.post("/cases/%s/eligibility/verdict" % cid, headers=auditor, json={"verdict": "eligible", "reason": "", "note": ""})
    assert r.status_code == 200, r.text
    return r.json()


def _notifications(cid, event_type):
    db = SessionLocal()
    try:
        return db.query(models.Notification).filter_by(case_id=cid, event_type=event_type).count()
    finally:
        db.close()


def _item(c, headers, cid):
    r = c.get("/cases", headers=headers)
    assert r.status_code == 200, r.text
    for it in r.json()["items"]:
        if it.get("case_id") == cid:
            return it
    raise AssertionError("case not in items: %s" % r.text)


def test_ai2는_사전심사_ready_전엔_409():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        cid = _mkcase(c, admin)
        r = c.post("/cases/%s/ai-second-analysis" % cid, headers=admin, json={})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "PREASSESS_NOT_READY", r.text


def test_ai2_실행_결과와_열람제한():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        auditor1 = _tok(c, "auditor1", "pw")
        operator1 = _tok(c, "operator1", "pw")
        applicant1 = _tok(c, "applicant1", "pw")
        cid = _mkcase(c, admin)
        _review(c, auditor1, cid)
        ai2 = _ai2(c, auditor1, cid)
        assert 5 <= ai2["probability"] <= 98, ai2
        assert ai2["recommendation"], ai2

        r = c.get("/cases/%s/ai-second-analysis" % cid, headers=operator1)
        assert r.status_code == 200 and r.json()["exists"] is True, r.text
        r = c.get("/cases/%s/ai-second-analysis" % cid, headers=applicant1)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "DOC_RESTRICTED", r.text

        gid = ai2["gen_doc_id"]
        r = c.get("/cases/%s/gen-docs" % cid, headers=applicant1)
        assert r.status_code == 200, r.text
        assert not any(d["doc_type"] == "ai_second_analysis" for d in r.json()), r.text
        r = c.get("/cases/%s/gen-docs" % cid, headers=operator1)
        assert r.status_code == 200, r.text
        assert any(d["doc_type"] == "ai_second_analysis" for d in r.json()), r.text

        r = c.get("/gen-docs/%s" % gid, headers=applicant1)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "DOC_RESTRICTED", r.text


def test_판정은_AI2_필수_불가는_사유필수():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        auditor1 = _tok(c, "auditor1", "pw")
        cid = _mkcase(c, admin)
        _review(c, auditor1, cid)
        r = c.post("/cases/%s/eligibility/verdict" % cid, headers=auditor1, json={"verdict": "eligible", "reason": "", "note": ""})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "AI2_REQUIRED", r.text
        _ai2(c, auditor1, cid)
        r = c.post("/cases/%s/eligibility/verdict" % cid, headers=auditor1, json={"verdict": "not_eligible", "reason": "", "note": ""})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "REASON_REQUIRED", r.text
        r = c.post("/cases/%s/eligibility/verdict" % cid, headers=auditor1, json={"verdict": "x", "reason": "", "note": ""})
        assert r.status_code == 422, r.text


def test_판정_즉시실행_D05_알림():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        auditor1 = _tok(c, "auditor1", "pw")
        applicant1 = _tok(c, "applicant1", "pw")
        cid = _mkcase(c, admin)
        _review(c, auditor1, cid)
        _ai2(c, auditor1, cid)
        v = _eligible(c, auditor1, cid)
        assert v["verdict"] == "eligible" and v.get("gen_doc_id"), v

        r = c.get("/cases/%s/eligibility" % cid, headers=applicant1)
        assert r.status_code == 200, r.text
        assert r.json()["verdict"] == "eligible" and r.json()["reason"] is None, r.text

        r = c.get("/gen-docs/%s" % v["gen_doc_id"], headers=applicant1)
        assert r.status_code == 200, r.text
        assert _notifications(cid, "eligibility.verdict") >= 2


def test_판정_2인확인():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        auditor1 = _tok(c, "auditor1", "pw")
        auditor2 = _tok(c, "auditor2", "pw")
        cid = _mkcase(c, admin)
        _review(c, auditor1, cid)
        r = c.post("/cases/%s/auditors" % cid, headers=admin, json={"username": "auditor2"})
        assert r.status_code == 200, r.text
        _ai2(c, auditor1, cid)
        r = c.post("/cases/%s/eligibility/verdict" % cid, headers=auditor1, json={"verdict": "eligible", "reason": "", "note": ""})
        assert r.status_code == 200 and r.json().get("pending") is True, r.text
        aid = r.json()["approval_id"]
        r = c.post("/approvals/%s/approve" % aid, headers=auditor2, json={})
        assert r.status_code == 200, r.text
        r = c.get("/cases/%s/eligibility" % cid, headers=auditor1)
        assert r.status_code == 200 and r.json()["verdict"] == "eligible", r.text


def test_정식신청_잠금과_검증():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        auditor1 = _tok(c, "auditor1", "pw")
        applicant1 = _tok(c, "applicant1", "pw")
        cid = _mkcase(c, admin)
        _review(c, auditor1, cid)
        _ai2(c, auditor1, cid)
        r = c.post("/cases/%s/formal-application" % cid, headers=applicant1, json=FORMAL)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "ELIGIBILITY_REQUIRED", r.text
        _eligible(c, auditor1, cid)

        bad = dict(FORMAL)
        bad.pop("process_summary")
        r = c.post("/cases/%s/formal-application" % cid, headers=applicant1, json=bad)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "FIELDS_REQUIRED", r.text
        assert "process_summary" in r.json()["detail"].get("fields", []), r.text

        bad = dict(FORMAL)
        bad["pledge"] = False
        r = c.post("/cases/%s/formal-application" % cid, headers=applicant1, json=bad)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "PLEDGE_REQUIRED", r.text

        bad = dict(FORMAL)
        bad["signature"] = "abc"
        r = c.post("/cases/%s/formal-application" % cid, headers=applicant1, json=bad)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "SIGNATURE_REQUIRED", r.text

        r = c.post("/cases/%s/formal-application" % cid, headers=applicant1, json=FORMAL)
        assert r.status_code == 200 and r.json()["status"] == "submitted", r.text
        r = c.post("/cases/%s/formal-application" % cid, headers=applicant1, json=FORMAL)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "FORMAL_ALREADY_SUBMITTED", r.text


def test_접수_반려_재제출_접수():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        auditor1 = _tok(c, "auditor1", "pw")
        applicant1 = _tok(c, "applicant1", "pw")
        operator1 = _tok(c, "operator1", "pw")
        consultant1 = _tok(c, "consultant1", "pw")
        cid = _mkcase(c, admin)
        _review(c, auditor1, cid)
        _ai2(c, auditor1, cid)
        _eligible(c, auditor1, cid)
        r = c.post("/cases/%s/formal-application" % cid, headers=applicant1, json=FORMAL)
        assert r.status_code == 200, r.text

        r = c.get("/ops/formal-applications", headers=operator1)
        assert r.status_code == 200, r.text
        assert any(x["case_id"] == cid and x["formal"] == "submitted" for x in r.json()["items"]), r.text

        r = c.post("/cases/%s/formal-application/return" % cid, headers=operator1, json={})
        assert r.status_code == 422, r.text
        r = c.post("/cases/%s/formal-application/return" % cid, headers=operator1, json={"reason": "서명 누락"})
        assert r.status_code == 200, r.text

        r = c.get("/cases/%s/formal-application" % cid, headers=applicant1)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "returned", r.text
        assert r.json()["return_reason"] == "서명 누락", r.text

        r = c.post("/cases/%s/formal-application" % cid, headers=applicant1, json=FORMAL)
        assert r.status_code == 200, r.text
        r = c.post("/cases/%s/formal-application/accept" % cid, headers=operator1, json={})
        assert r.status_code == 200, r.text
        r = c.post("/cases/%s/formal-application" % cid, headers=applicant1, json=FORMAL)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "FORMAL_ALREADY_ACCEPTED", r.text

        r = c.get("/ops/formal-applications?status=accepted", headers=operator1)
        assert r.status_code == 200, r.text
        assert any(x["case_id"] == cid for x in r.json()["items"]), r.text

        r = c.get("/cases/%s/formal-application" % cid, headers=consultant1)
        assert r.status_code == 200, r.text
        assert "signature" not in r.json().get("doc", {}), r.text
        r = c.get("/cases/%s/formal-application" % cid, headers=operator1)
        assert r.status_code == 200, r.text
        assert "signature" in r.json().get("doc", {}), r.text


def test_플래그_off_면_owner_종전():
    os.environ["GLHAC_FORMAL_FLOW"] = "0"
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        operator1 = _tok(c, "operator1", "pw")
        cid = _mkcase(c, admin)
        _set_status(cid, "consultant_review")
        it = _item(c, operator1, cid)
        assert it["owner"] == "consultant", it
        assert it["eligibility"] is None, it
        assert it["formal"] is None, it


def test_플래그_on_세부단계_owner():
    try:
        os.environ["GLHAC_FORMAL_FLOW"] = "1"
        with TestClient(app) as c:
            admin = _tok(c, "admin", "admin")
            auditor1 = _tok(c, "auditor1", "pw")
            applicant1 = _tok(c, "applicant1", "pw")
            operator1 = _tok(c, "operator1", "pw")
            cid = _mkcase(c, admin)
            _review(c, auditor1, cid)
            _set_status(cid, "consultant_review")
            it = _item(c, operator1, cid)
            assert it["owner"] == "auditor", it

            _ai2(c, auditor1, cid)
            _eligible(c, auditor1, cid)
            it = _item(c, operator1, cid)
            assert it["owner"] == "client", it

            r = c.post("/cases/%s/formal-application" % cid, headers=applicant1, json=FORMAL)
            assert r.status_code == 200, r.text
            it = _item(c, operator1, cid)
            assert it["owner"] == "ops" and it["step8"] == 2, it

            r = c.post("/cases/%s/contract/approve" % cid, headers=operator1, json={})
            assert r.status_code == 409 and r.json()["detail"]["code"] == "FORMAL_NOT_ACCEPTED", r.text

            r = c.post("/cases/%s/formal-application/accept" % cid, headers=operator1, json={})
            assert r.status_code == 200, r.text
            it = _item(c, operator1, cid)
            assert it["owner"] == "consultant", it
    finally:
        os.environ["GLHAC_FORMAL_FLOW"] = "0"
