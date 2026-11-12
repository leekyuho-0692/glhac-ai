"""2차 검수의 종결 경쟁·사라진 자동입력·예산 밖 Pork 재현을 고정한다."""
import base64
import copy
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import ai_local, intake, models, state_machine as sm
from app import document_extraction as extraction
import app.main as m


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(ai_local, "provider", lambda: "openai")
    monkeypatch.setattr(ai_local, "llm_json", lambda *a, **k: {
        "verdict": "ok", "doc_type_ok": True, "field_issues": []})
    monkeypatch.setattr(m, "verify_enqueue", lambda *a: False)
    monkeypatch.setattr(m, "_AUTOPARSE", False)
    monkeypatch.setattr(intake, "_ZIP_LOOKUP", False)
    monkeypatch.setenv("GLHAC_RATE_LIMIT_PER_MIN", "0")


@pytest.fixture
def memory_case():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        c = models.CaseApplication(org_id="round2-org", company_name="Sample", profile_ext={})
        db.add_all([models.Org(org_id="round2-org", name="Sample"), c])
        db.commit()
        yield db, c
    engine.dispose()


def _snapshot(case_id):
    with m.SessionLocal() as db:
        return {cls.__tablename__: sorted(json.dumps(
            {col.name: getattr(row, col.name) for col in cls.__table__.columns},
            sort_keys=True, default=str) for row in db.query(cls).filter_by(case_id=case_id))
            for cls in (models.CaseApplication, models.DocumentAsset, models.SjphEvidence,
                        models.HpasEvaluation, models.Material, models.Product,
                        models.MaterialMeasurement, models.WorkflowEvent, models.AuditLog,
                        models.Notification)}


@pytest.mark.parametrize("status", sorted(sm.TERMINAL_STATES))
@pytest.mark.parametrize("pause_call", [1, 2], ids=["classification", "typed-extraction"])
def test_01_autoparse_after_http_close_preserves_all_records(monkeypatch, status, pause_call):
    entered, release = threading.Event(), threading.Event()
    calls = []
    text = ("x" * 310 + "\nKEPUTUSAN PENETAPAN TIM MANAJEMEN HALAL\nCompany name: Sample\nAddress: Seoul\n"
            "Certificate of Business Registration\nBusiness Taxpayer ID Number: 638-88-01141")

    def llm(system, user):
        calls.append(user)
        if len(calls) == pause_call:
            entered.set()
            assert release.wait(15), "종결 요청 뒤 모델 응답을 재개하지 못함"
        return {"doc_type": "nib_business_license", "confidence": .99,
                "fields": {"company_name": "Sample", "address": "Seoul", "nib": "638-88-01141"},
                "evidence": {"company_name": "Company name: Sample", "address": "Address: Seoul",
                             "nib": "Business Taxpayer ID Number: 638-88-01141"}}

    monkeypatch.setattr(ai_local, "llm_json", llm)
    monkeypatch.setattr(m, "_AUTOPARSE", True)
    with TestClient(m.app) as client:
        token = client.post("/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
        headers = {"Authorization": "Bearer " + token}
        cid = client.post("/cases", headers=headers, json={"company_name": "Sample"}).json()["case_id"]
        response = client.post(f"/cases/{cid}/documents", headers=headers, json={
            "filename": "scan.txt", "file_b64": base64.b64encode(text.encode()).decode()})
        assert response.status_code == 200, response.text
        assert response.json()["autoparse_queued"]
        did = response.json()["document_id"]
        failed = m._autoparse_state["failed"]
        try:
            assert entered.wait(10), "실제 자동 파싱 큐가 모델 호출에 도달하지 못함"
            if status == "revoked":
                with m.SessionLocal() as db:
                    db.get(models.CaseApplication, cid).status = "certificate_issued"
                    db.add(models.HalalCertificate(case_id=cid, certificate_no="R2-" + cid, status="active"))
                    db.commit()
                operator = client.post("/auth/login", json={"username": "operator1", "password": "pw"}).json()["token"]
                request = client.post(f"/cases/{cid}/certificate/revoke",
                                      headers={"Authorization": "Bearer " + operator},
                                      json={"reason": "종결 경쟁 재현"})
                assert request.status_code == 200, request.text
                closed = client.post(f"/approvals/{request.json()['approval_id']}/approve",
                                     headers=headers)
            else:
                closed = client.post(f"/cases/{cid}/close", headers=headers,
                                     json={"status": status, "reason": "종결 경쟁 재현"})
            assert closed.status_code == 200, closed.text
            before = _snapshot(cid)
        finally:
            release.set()
            deadline = time.monotonic() + 15
            while did in m._autoparse_inflight and time.monotonic() < deadline:
                time.sleep(.01)
        assert did not in m._autoparse_inflight
        assert m._autoparse_state["failed"] == failed
        assert _snapshot(cid) == before
        with m.SessionLocal() as db:
            assert db.get(models.CaseApplication, cid).status == status
            assert db.get(models.DocumentAsset, did).text_full is None
            assert db.query(models.SjphEvidence).filter_by(case_id=cid, item_key="halal_supervisor").count() == 0


@pytest.mark.parametrize("status,current,expected", [
    ("applied", "Made up", "Previous"),
    ("confirmed", "Made up", "Made up"),
    ("applied", "Manually changed", "Manually changed"),
])
@pytest.mark.parametrize("returned", [{}, {"address": "Seoul"}], ids=["missing-field", "replaced-field"])
def test_02_reextract_checks_owned_unconfirmed_values(memory_case, monkeypatch, status, current, expected, returned):
    db, c = memory_case
    c.address = current
    text = "Company name: Sample\nAddress: Seoul"
    doc = models.DocumentAsset(case_id=c.case_id, filename="scan.txt", doc_type="other",
        content_b64=base64.b64encode(text.encode()).decode(), text_full=text,
        fields={"address": "Made up", "_autofill": {"changes": {
            "address": {"scope": "case", "field": "address", "before": "Previous",
                        "value": "Made up", "status": status}}, "conflicts": []}})
    db.add(doc)
    db.commit()
    monkeypatch.setattr(ai_local, "llm_json", lambda *a, **k: {
        "doc_type": "other", "fields": copy.deepcopy(returned), "evidence": {"address": "Address: Seoul"}})
    m._reprocess_doc(db, doc, c)
    assert doc.fields.get("address") == returned.get("address")
    assert doc.fields["_autofill"]["changes"]["address"]["status"] == status
    monkeypatch.setattr(ai_local, "llm_json", lambda *a, **k: {
        "verdict": "ok", "doc_type_ok": True, "field_issues": []})
    m._verify_doc(db, doc, c)
    assert c.address == expected
    changes = doc.fields["_autofill"]["changes"]
    assert changes["address"]["status"] == (
        "confirmed" if status == "confirmed" else "reverted" if current == "Made up" else "superseded")
    assert doc.fields["_verify"]["verdict"] == ("ok" if status == "confirmed" else "fail")
    conflicts = copy.deepcopy(doc.fields["_autofill"]["conflicts"])
    m._verify_doc(db, doc, c)
    assert c.address == expected
    assert doc.fields["_autofill"]["conflicts"] == conflicts


@pytest.mark.parametrize("scope,field,value,text", [
    ("profile", "office_phone", "02-9999-9999", "Document ID: 02-9999-9999"),
    ("facility", "reg_no", "ABC123", "Document ID: ABC123"),
    ("facility_profile", "phone", "02-9999-9999", "Document ID: 02-9999-9999"),
])
def test_02_missing_profile_and_facility_values_keep_role_validation(memory_case, scope, field, value, text):
    db, c = memory_case
    facility = models.Facility(org_id=c.org_id, name="Sample", reg_no=value,
                               profile_ext={field: value})
    db.add(facility)
    db.flush()
    c.facility_ids = [facility.facility_id]
    c.profile_ext = {field: value}
    change = {"scope": scope, "field": field, "before": "Previous", "value": value,
              "status": "applied", "present": True, "facility_id": facility.facility_id}
    doc = models.DocumentAsset(case_id=c.case_id, filename="scan.txt", doc_type="other", text_full=text,
        fields={"_autofill": {"changes": {"owned": change}, "conflicts": []}})
    db.add(doc)
    db.commit()
    m._verify_doc(db, doc, c)
    assert doc.fields["_verify"]["verdict"] == "fail"
    assert doc.fields["_autofill"]["changes"]["owned"]["status"] == "reverted"
    assert (c.profile_ext[field] if scope == "profile" else facility.reg_no if scope == "facility"
            else facility.profile_ext[field]) == "Previous"


@pytest.mark.parametrize("provider", ["openai", "ollama", "none"])
def test_03_full_text_rules_find_pork_outside_ai_budget(memory_case, monkeypatch, provider):
    db, c = memory_case
    monkeypatch.setattr(ai_local, "provider", lambda: provider)
    monkeypatch.setattr(extraction, "DOCUMENT_AI_MAX_CALLS", 8)
    # 검수 원문: 앞 Water, 약 40만 자 중간 Pork. 구조화 목록 마커는 없다.
    text = "Ingredients\nWater\n" + ("x" * 99 + "\n") * 2000 + "Pork\n" + ("x" * 99 + "\n") * 2000
    calls = []

    def llm(system, user):
        calls.append(user)
        names = [name for name in ("Water", "Pork") if name in user]
        return {"doc_type": "material_list", "confidence": .99,
                "fields": {"material_names": names}, "evidence": {"material_names": names},
                "verdict": "ok", "doc_type_ok": True, "field_issues": []}

    monkeypatch.setattr(ai_local, "llm_json", llm)
    doc = models.DocumentAsset(case_id=c.case_id, filename="ingredients.txt", doc_type="material_list",
                              content_b64=base64.b64encode(text.encode()).decode())
    db.add(doc)
    db.commit()
    m._reprocess_doc(db, doc, c)
    m._verify_doc(db, doc, c)
    if provider == "openai":
        assert len(calls) == 8
    assert all("Pork" not in call for call in calls)
    assert doc.fields["_tail_screening"]["keyword_risk"] == "high"
    pending = sm.pending_extractions(db, c.case_id)
    assert "Pork (swine)" in {item["value"] for item in pending if item["kind"] == "material_names"}
    assert {"code": "PENDING_CRITICAL_MATERIAL", "materials": ["Pork (swine)"]} in sm.material_safety_blockers(db, c)


@pytest.mark.parametrize("source,expected", [("Pork", True), ("Gelatin", True), ("Pork-free", False)])
def test_03_source_risks_survive_verification_without_truncation(memory_case, monkeypatch, source, expected):
    db, c = memory_case
    text = "Ingredients\n" + source
    monkeypatch.setattr(ai_local, "llm_json", lambda *a, **k: {"doc_type": "material_list", "fields": {}})
    result = intake.classify("ingredients.txt", text)
    doc = models.DocumentAsset(case_id=c.case_id, filename="ingredients.txt", doc_type="material_list",
                              fields=result["fields"], text_full=text)
    db.add(doc)
    db.commit()
    monkeypatch.setattr(ai_local, "llm_json", lambda *a, **k: {"verdict": "ok", "field_issues": []})
    m._verify_doc(db, doc, c)
    assert bool(sm.material_safety_blockers(db, c)) is expected
    assert len(sm.pending_extractions(db, c.case_id)) == int(expected)


@pytest.mark.parametrize("source,expected", [
    ("Ethanol Result: 0.02%", False),
    ("Ethanol Result: 0.02%\nIngredients: Ethanol", True),
    ("Porcine DNA PCR: Negative", False),
    ("Porcine DNA PCR: Negative\nIngredients: Pork", True),
])
def test_03_measured_analyte_does_not_hide_separate_material(memory_case, monkeypatch, source, expected):
    db, c = memory_case
    text = "CERTIFICATE OF ANALYSIS\n" + source
    monkeypatch.setattr(ai_local, "llm_json", lambda *a, **k: {"doc_type": "coa_msds", "fields": {}})
    result = intake.parse_typed_text("coa_msds", "CoA.txt", text)
    assert result["fields"]["measurements"]
    doc = models.DocumentAsset(case_id=c.case_id, filename="CoA.txt", doc_type="coa_msds",
                              fields=result["fields"], text_full=text)
    db.add(doc)
    db.commit()
    assert bool(sm.material_safety_blockers(db, c)) is expected
