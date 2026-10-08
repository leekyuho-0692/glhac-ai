"""SRS FORMAL FLOW — 양식 업로드·설정·권한표."""
import os, sys, io, zipfile, base64, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_UPLOAD_DIR", tempfile.mkdtemp())
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_forms_test.db")
os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_NOTIFY_WORKER", "0")
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app                    # noqa: E402
from app import models                      # noqa: E402
from app.db import SessionLocal             # noqa: E402

c = TestClient(app)

REVIEW = {"sections": {"documents": {"ok": True, "note": ""}, "materials": {"ok": True, "note": ""}, "process": {"ok": True, "note": ""}}, "verdict": "ready", "note": "가능"}


def _tok(u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _docx_b64():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<x/>")
    return base64.b64encode(buf.getvalue()).decode()


admin = operator1 = auditor1 = auditor2 = auditor3 = consultant1 = applicant1 = fatwa1 = None


def setup_module(module):
    global admin, operator1, auditor1, auditor2, auditor3, consultant1, applicant1, fatwa1
    admin = _tok("admin", "admin")
    operator1 = _tok("operator1", "pw")
    auditor1 = _tok("auditor1", "pw")
    auditor2 = _tok("auditor2", "pw")
    auditor3 = _tok("auditor3", "pw")
    consultant1 = _tok("consultant1", "pw")
    applicant1 = _tok("applicant1", "pw")
    fatwa1 = _tok("fatwa1", "pw")


def _mkcase(cid_status=None):
    r = c.post("/cases", headers=admin, json={"org_id": "org_demo", "company_name": "FormsCo"})
    assert r.status_code == 200, r.text
    return r.json()["case_id"]


def _user_id(username):
    db = SessionLocal()
    try:
        return db.query(models.User).filter_by(username=username).first().user_id
    finally:
        db.close()


def test_양식_업로드_버전_활성화_다운로드():
    r = c.get("/admin/forms", headers=operator1)
    assert r.status_code == 200, r.text
    items = {it["key"]: it for it in r.json()["items"]}
    assert "factory_audit" in items and "sjph_manual" in items
    assert items["factory_audit"]["using_default"] is True
    assert items["factory_audit"]["active_version"] is None

    data = _docx_b64()
    r = c.post("/admin/forms/factory_audit", headers=operator1, json={"filename": "x.docx", "file_b64": data, "note": "v1"})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 1
    r = c.post("/admin/forms/factory_audit", headers=operator1, json={"filename": "x.docx", "file_b64": data, "note": "v2"})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2

    r = c.get("/admin/forms", headers=operator1)
    items = {it["key"]: it for it in r.json()["items"]}
    assert items["factory_audit"]["active_version"] == 2
    assert len(items["factory_audit"]["versions"]) == 2
    assert items["factory_audit"]["using_default"] is False

    r = c.get("/admin/forms/factory_audit/download?version=1", headers=operator1)
    assert r.status_code == 200, r.text
    assert r.content == base64.b64decode(data)
    assert "wordprocessingml" in r.headers.get("content-type", "")

    r = c.get("/admin/forms/factory_audit/download", headers=operator1)
    assert r.status_code == 200, r.text
    assert r.content == base64.b64decode(data)

    r = c.post("/admin/forms/factory_audit/activate", headers=operator1, json={"version": 1})
    assert r.status_code == 200, r.text
    r = c.get("/admin/forms", headers=operator1)
    items = {it["key"]: it for it in r.json()["items"]}
    assert items["factory_audit"]["active_version"] == 1

    r = c.post("/admin/forms/factory_audit/activate", headers=operator1, json={"version": 0})
    assert r.status_code == 200, r.text
    r = c.get("/admin/forms", headers=operator1)
    items = {it["key"]: it for it in r.json()["items"]}
    assert items["factory_audit"]["active_version"] is None
    assert items["factory_audit"]["using_default"] is True

    r = c.post("/admin/forms/factory_audit/activate", headers=operator1, json={"version": 9})
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["code"] == "VERSION_NOT_FOUND"

    r = c.get("/admin/forms/factory_audit/download", headers=operator1)
    assert r.status_code == 200, r.text


def test_양식_검증_권한():
    r = c.post("/admin/forms/nope_key", headers=operator1, json={"filename": "x.docx", "file_b64": _docx_b64(), "note": ""})
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["code"] == "FORM_NOT_FOUND"

    r = c.post("/admin/forms/factory_audit", headers=operator1, json={"filename": "x.pdf", "file_b64": _docx_b64(), "note": ""})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "DOCX_REQUIRED"

    bad = base64.b64encode(b"not a zip").decode()
    r = c.post("/admin/forms/factory_audit", headers=operator1, json={"filename": "x.docx", "file_b64": bad, "note": ""})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "NOT_A_DOCX"

    r = c.post("/admin/forms/factory_audit", headers=applicant1, json={"filename": "x.docx", "file_b64": _docx_b64(), "note": ""})
    assert r.status_code == 403, r.text

    r = c.get("/admin/forms", headers=applicant1)
    assert r.status_code == 403, r.text


def test_문서_열람_권한표():
    cid = _mkcase()
    r = c.post("/cases/%s/preassess/review" % cid, headers=auditor1, json=REVIEW)
    assert r.status_code == 200, r.text
    r = c.post("/cases/%s/ai-second-analysis" % cid, headers=auditor1)
    assert r.status_code == 200, r.text
    gen_doc_id = r.json()["gen_doc_id"]

    r = c.get("/gen-docs/%s" % gen_doc_id, headers=consultant1)
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "DOC_RESTRICTED"

    r = c.get("/gen-docs/%s" % gen_doc_id, headers=applicant1)
    assert r.status_code == 403, r.text

    r = c.get("/gen-docs/%s" % gen_doc_id, headers=auditor1)
    assert r.status_code == 200, r.text
    assert r.json()["gen_doc_id"] == gen_doc_id

    r = c.get("/cases/%s/gen-docs" % cid, headers=consultant1)
    assert r.status_code == 200, r.text
    keys = [d.get("doc_type") for d in r.json()]
    assert "ai_second_analysis" not in keys


def test_심사설정_검증():
    r = c.get("/admin/audit-settings", headers=operator1)
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["max_cases_per_auditor"] == 6
    assert s["decision_rule"] == "majority"
    assert s["training_hours"] == 4
    assert s["quorum"] == 3

    r = c.post("/admin/audit-settings", headers=operator1, json={"max_cases_per_auditor": 2})
    assert r.status_code == 200, r.text
    r = c.get("/admin/audit-settings", headers=operator1)
    assert r.json()["max_cases_per_auditor"] == 2

    r = c.post("/admin/audit-settings", headers=operator1, json={"quorum": 5})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "QUORUM_FIXED"

    r = c.post("/admin/audit-settings", headers=operator1, json={"decision_rule": "x"})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "BAD_DECISION_RULE"

    r = c.post("/admin/audit-settings", headers=operator1, json={"training_hours": 0})
    assert r.status_code == 422, r.text

    r = c.post("/admin/audit-settings", headers=operator1, json={"max_cases_per_auditor": 6})
    assert r.status_code == 200, r.text


def test_담당건수_상한():
    aid3 = _user_id("auditor3")
    r = c.post("/admin/audit-settings", headers=operator1, json={"max_cases_per_auditor": 1})
    assert r.status_code == 200, r.text
    try:
        a = _mkcase()
        r = c.post("/ops/cases/%s/assign-auditor" % a, headers=operator1, json={"auditor_id": aid3})
        assert r.status_code == 200, r.text

        b = _mkcase()
        r = c.post("/ops/cases/%s/assign-auditor" % b, headers=operator1, json={"auditor_id": aid3})
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "AUDITOR_OVERLOADED"
    finally:
        r = c.post("/admin/audit-settings", headers=operator1, json={"max_cases_per_auditor": 6})
        assert r.status_code == 200, r.text
