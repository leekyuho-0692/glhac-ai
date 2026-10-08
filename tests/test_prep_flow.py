"""PREP FLOW — 13종 카탈로그·업로드·상태·완료 D-16·교육서류·SRS 게이트."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_prep_test.db")
os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_NOTIFY_WORKER", "0")
import base64  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app                    # noqa: E402
from app import models                      # noqa: E402
from app.db import SessionLocal             # noqa: E402


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _mkcase(c, applicant):
    r = c.post("/cases", headers=applicant, json={"company_name": "PrepCo", "sector": "warehouse"})
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


B64 = base64.b64encode(b"hello").decode()
ITEMS = ["org_chart", "halal_supervisor", "training", "sjph_manual", "halal_policy", "production_flow", "facility_layout", "material_evidence", "cleaning", "purchase_log", "label", "traceability", "internal_audit"]
def test_카탈로그_13종_물류제외(c=None):
    c = c or TestClient(app)
    admin = _tok(c, "admin", "admin")
    applicant = _tok(c, "applicant1", "pw")
    cid = _mkcase(c, applicant)
    r = c.get("/cases/%s/prep" % cid, headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) == 13 and body["applicable"] == 11, body
    by = {i["item_key"]: i for i in body["items"]}
    for k in ("production_flow", "label"):
        assert by[k]["applicable"] is False, by[k]
        assert by[k]["status"] == "not_applicable", by[k]
    assert body["applicable"] == 11, body


def test_업로드_상태_검토대기(c=None):
    c = c or TestClient(app)
    admin = _tok(c, "admin", "admin")
    applicant = _tok(c, "applicant1", "pw")
    cid = _mkcase(c, applicant)
    for key in ("halal_policy", "org_chart"):
        r = c.post("/cases/%s/sjph-evidence" % cid, headers=applicant, json={"item_key": key, "filename": "x.pdf", "file_b64": B64})
        assert r.status_code == 200, r.text
    r = c.get("/cases/%s/prep" % cid, headers=admin)
    by = {i["item_key"]: i for i in r.json()["items"]}
    assert by["halal_policy"]["status"] == "pending_review", by["halal_policy"]
    assert by["halal_policy"]["uploaded"] is True, by["halal_policy"]
    assert by["org_chart"]["uploaded"] is True, by["org_chart"]
    r = c.post("/cases/%s/sjph-evidence" % cid, headers=applicant, json={"item_key": "bogus", "filename": "x.pdf", "file_b64": B64})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "BAD_ITEM_KEY", r.text


def test_상태변경_권한과_사유(c=None):
    c = c or TestClient(app)
    admin = _tok(c, "admin", "admin")
    applicant = _tok(c, "applicant1", "pw")
    auditor = _tok(c, "auditor1", "pw")
    cid = _mkcase(c, applicant)
    r = c.patch("/cases/%s/prep/halal_policy" % cid, headers=auditor, json={"status": "revision_requested"})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "REASON_REQUIRED", r.text
    r = c.patch("/cases/%s/prep/halal_policy" % cid, headers=auditor, json={"status": "revision_requested", "note": "보완"})
    assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        n = db.query(models.Notification).filter_by(event_type="prep.revision_requested").count()
        assert n >= 1, n
    finally:
        db.close()
    r = c.patch("/cases/%s/prep/halal_policy" % cid, headers=auditor, json={"status": "not_applicable", "note": "x"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "STATUS_NOT_ALLOWED", r.text
    r = c.patch("/cases/%s/prep/halal_policy" % cid, headers=applicant, json={"status": "not_applicable"})
    assert r.status_code == 422, r.text
    r = c.patch("/cases/%s/prep/halal_policy" % cid, headers=applicant, json={"status": "not_applicable", "note": "해당없음"})
    assert r.status_code == 200, r.text
    r = c.patch("/cases/%s/prep/halal_policy" % cid, headers=applicant, json={"status": "confirmed"})
    assert r.status_code == 403, r.text
    r = c.patch("/cases/%s/prep/halal_policy" % cid, headers=auditor, json={"status": "xx"})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "BAD_STATUS", r.text


def test_컨설턴트_의견(c=None):
    c = c or TestClient(app)
    admin = _tok(c, "admin", "admin")
    applicant = _tok(c, "applicant1", "pw")
    consultant = _tok(c, "consultant1", "pw")
    cid = _mkcase(c, applicant)
    r = c.post("/cases/%s/prep/halal_policy/note" % cid, headers=consultant, json={})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "NOTE_REQUIRED", r.text
    r = c.post("/cases/%s/prep/halal_policy/note" % cid, headers=consultant, json={"note": "확인 필요"})
    assert r.status_code == 200, r.text
    r = c.get("/cases/%s/prep" % cid, headers=admin)
    by = {i["item_key"]: i for i in r.json()["items"]}
    assert len(by["halal_policy"]["notes"]) == 1, by["halal_policy"]


def test_완료_D16_통지_서브확인(c=None):
    c = c or TestClient(app)
    admin = _tok(c, "admin", "admin")
    applicant = _tok(c, "applicant1", "pw")
    auditor = _tok(c, "auditor1", "pw")
    cid = _mkcase(c, applicant)
    r = c.post("/cases/%s/prep/complete" % cid, headers=auditor)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "PREP_INCOMPLETE", r.text
    _set_status(cid, "document_pre_audit_requested")
    for k in ITEMS:
        r = c.patch("/cases/%s/prep/%s" % (cid, k), headers=auditor, json={"status": "confirmed"})
        assert r.status_code == 200, r.text
    r = c.post("/cases/%s/prep/complete" % cid, headers=auditor)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True, body
    assert body["result"] == "pass", body
    r = c.get("/cases/%s/gen-docs" % cid, headers=admin)
    assert r.status_code == 200, r.text
    assert any(d["doc_type"] == "mock_audit_notice" for d in r.json()), r.text
    db = SessionLocal()
    try:
        assert db.query(models.Notification).filter_by(event_type="mock_audit.notice").count() >= 1
    finally:
        db.close()
    r = c.post("/cases/%s/auditors" % cid, headers=admin, json={"username": "auditor2"})
    assert r.status_code == 200, r.text
    cid2 = _mkcase(c, applicant)
    _set_status(cid2, "document_pre_audit_requested")
    for k in ITEMS:
        c.patch("/cases/%s/prep/%s" % (cid2, k), headers=auditor, json={"status": "confirmed"})
    r = c.post("/cases/%s/auditors" % cid2, headers=admin, json={"username": "auditor2"})
    assert r.status_code == 200, r.text
    r = c.post("/cases/%s/prep/complete" % cid2, headers=auditor)
    assert r.status_code == 200, r.text
    assert r.json()["pending"] is True, r.text


def test_교육서류_배포_일괄동의(c=None):
    c = c or TestClient(app)
    admin = _tok(c, "admin", "admin")
    applicant = _tok(c, "applicant1", "pw")
    operator = _tok(c, "operator1", "pw")
    cid = _mkcase(c, applicant)
    r = c.get("/cases/%s/education-docs" % cid, headers=admin)
    assert r.status_code == 200, r.text
    docs = {d["code"]: d for d in r.json()["docs"]}
    for code in ("D-10", "D-11", "D-12", "D-13", "D-14"):
        assert docs[code]["status"] == "not_issued", docs[code]
    r = c.post("/cases/%s/education-docs/consent" % cid, headers=applicant, json={"name": "홍길동", "image": "data:image/png;base64,iVBORw0KGgo=", "agree": True})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "DOCS_NOT_ISSUED", r.text
    r = c.post("/cases/%s/education-docs/issue" % cid, headers=operator)
    assert r.status_code == 200, r.text
    assert len(r.json()["made"]) == 4, r.text
    r = c.get("/cases/%s/education-docs" % cid, headers=admin)
    docs = {d["code"]: d for d in r.json()["docs"]}
    assert docs["D-10"]["status"] == "issued", docs["D-10"]
    r = c.post("/cases/%s/education-docs/consent" % cid, headers=applicant, json={"name": "홍길동", "image": "data:image/png;base64,iVBORw0KGgo="})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "AGREE_REQUIRED", r.text
    r = c.post("/cases/%s/education-docs/consent" % cid, headers=applicant, json={"name": "홍길동", "image": "data:image/png;base64,iVBORw0KGgo=", "agree": True})
    assert r.status_code == 200, r.text
    assert len(r.json()["signed"]) == 4, r.text
    r = c.get("/cases/%s/education-docs" % cid, headers=admin)
    assert r.json()["consented"] is True, r.text
    docs = {d["code"]: d for d in r.json()["docs"]}
    assert docs["D-13"]["status"] == "signed", docs["D-13"]


def test_SRS모드_게이트(c=None):
    c = c or TestClient(app)
    admin = _tok(c, "admin", "admin")
    applicant = _tok(c, "applicant1", "pw")
    auditor = _tok(c, "auditor1", "pw")
    cid = _mkcase(c, applicant)
    _set_status(cid, "document_pre_audit_requested")
    _set_status(cid, "document_pre_audit_requested")
    for k in ITEMS:
        rr = c.patch("/cases/%s/prep/%s" % (cid, k), headers=auditor, json={"status": "confirmed"})
        assert rr.status_code == 200, rr.text
    os.environ["GLHAC_FORMAL_FLOW"] = "1"
    try:
        r = c.post("/cases/%s/prep/complete" % cid, headers=auditor)
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "EDUCATION_CONSENT_REQUIRED", r.text
        cid2 = _mkcase(c, applicant)
        _set_status(cid2, "document_pre_audit_requested")
        r = c.post("/cases/%s/mock-audit/decision" % cid2, headers=auditor, json={"result": "pass"})
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "PREP_INCOMPLETE", r.text
    finally:
        os.environ.pop("GLHAC_FORMAL_FLOW", None)