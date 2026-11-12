"""배포 전 검수 1~9: 검수 원문의 재현 입력을 실제 저장·검산 경로로 고정한다."""
import base64
import copy
import json
from concurrent.futures import ThreadPoolExecutor
import threading
from uuid import uuid4

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
    monkeypatch.setattr(m, "_RL_WRITE_HITS", {})


@pytest.fixture
def memory_case():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        c = models.CaseApplication(org_id="review-org", company_name="Sample", profile_ext={})
        db.add_all([models.Org(org_id="review-org", name="Sample"), c])
        db.commit()
        yield db, c
    engine.dispose()


@pytest.mark.parametrize("confirmed", [False, True])
def test_01_made_up_address_real_verification_reverts_only_unconfirmed(memory_case, confirmed):
    db, c = memory_case
    c.address = "Made up"
    doc = models.DocumentAsset(case_id=c.case_id, filename="scan.txt", doc_type="other",
        text_full="Company name: Sample\nAddress: Seoul", fields={"address": "Made up",
        "_autofill": {"changes": {"address": {"scope": "case", "field": "address",
            "before": "Previous", "value": "Made up", "status": "confirmed" if confirmed else "applied"}},
            "conflicts": []}})
    db.add(doc)
    db.commit()
    m._verify_doc(db, doc, c)
    assert doc.fields["_verify"]["verdict"] == "fail"
    assert doc.fields["_verify"]["field_issues"] == [
        {"field": "address", "code": "ungrounded_value", "issue": "원문 값·역할·형식 대조 불일치"}]
    assert c.address == ("Made up" if confirmed else "Previous")
    assert doc.fields["_autofill"]["changes"]["address"]["status"] == ("confirmed" if confirmed else "reverted")


@pytest.mark.parametrize("key", sorted(intake._BOOLEAN_FIELDS) + ["unknown_boolean"])
def test_02_irrelevant_boolean_evidence_never_accepted(monkeypatch, memory_case, key):
    db, c = memory_case
    text = "SJPH Manual / Document ID: ABC123"
    monkeypatch.setattr(ai_local, "llm_json", lambda *a, **k: {"doc_type": "sjph_manual",
        "fields": {key: True}, "evidence": {key: "Document ID: ABC123"}})
    parsed = intake.classify("manual.txt", text)
    meta = parsed["fields"]["_field_meta"][key]
    assert meta["accepted"] is False and meta["review"] is True
    if key.startswith("has_"):
        assert meta["checks"]["rule_match"] is False
    applied = {}
    m._process_doc(db, c, parsed, applied)
    db.flush()
    assert db.query(models.HpasEvaluation).filter_by(case_id=c.case_id, status="ok").count() == 0


@pytest.mark.parametrize("value,text,accepted", [
    (True, "Halal commitment policy", True),
    (True, "No halal commitment policy", False),
    (False, "No halal commitment policy", True),
    (False, "Document ID: ABC123", False),
])
def test_02_boolean_evidence_meaning_and_polarity(value, text, accepted):
    fields = intake._checked_fields({"has_commitment": value}, {"has_commitment": text},
                                    {"has_commitment": not text.startswith("No")}, text)
    assert fields["_field_meta"]["has_commitment"]["accepted"] is accepted


@pytest.mark.parametrize("returned", [[], ["Water"]])
@pytest.mark.parametrize("detected", ["material_list", "nib_business_license"])
def test_03_structured_water_pork_omission_remains_pending(monkeypatch, memory_case, returned, detected):
    db, c = memory_case
    text = "[전성분표]\n[제품/시트: Sample]\n[원재료목록]\nWater\nPork\n"
    assert intake._parse_ingredient_markers(text)[1] == ["Water", "Pork"]
    monkeypatch.setattr(ai_local, "llm_json", lambda *a, **k: {"doc_type": detected,
        "fields": {"material_names": returned} if returned else {},
        "evidence": {"material_names": returned}})
    parsed = intake.classify("전성분표.xlsx", text)
    fields = parsed["fields"]
    assert fields["_classification"]["review"]
    assert fields["_rule_omissions"]["material_names"] == (["Pork"] if returned else ["Water", "Pork"])
    assert "ai_omission" in fields["_field_meta"]["material_names"]["reasons"]
    assert fields["_tail_screening"]["keyword_risk"] == "high"
    doc = models.DocumentAsset(case_id=c.case_id, filename="전성분표.xlsx",
        doc_type=parsed["doc_type"], fields=fields, text_full=text)
    db.add(doc)
    db.commit()
    monkeypatch.setattr(ai_local, "llm_json", lambda *a, **k: {"verdict": "ok", "field_issues": []})
    m._verify_doc(db, doc, c)
    assert doc.fields["_tail_screening"]["keyword_risk"] == "high"
    assert {x["value"] for x in sm.pending_extractions(db, c.case_id) if x["kind"] == "material_names"} == {"Water", "Pork"}
    assert any(x["code"] == "PENDING_CRITICAL_MATERIAL" for x in sm.material_safety_blockers(db, c))


def test_03_ollama_tail_still_screens_pork(monkeypatch):
    monkeypatch.setattr(ai_local, "provider", lambda: "ollama")
    monkeypatch.setattr(ai_local, "llm_json", lambda *a, **k: {"doc_type": "material_list", "fields": {}})
    parsed = intake.classify("ingredients.txt", "Ingredients\n" + "x" * 3000 + "\nPork")
    assert parsed["fields"]["_tail_screening"]["keyword_risk"] == "high"
    assert parsed["fields"]["_classification"]["review"]


@pytest.mark.parametrize("concurrent", [False, True])
def test_04_upload_id_saved_idempotent_scoped_and_preserved_on_reparse(monkeypatch, concurrent):
    with TestClient(m.app) as client:
        login = client.post("/auth/login", json={"username": "applicant1", "password": "pw"}).json()
        headers = {"Authorization": "Bearer " + login["token"]}
        cid = client.post("/cases", headers=headers, json={"company_name": "Sample"}).json()["case_id"]
        payload = {"filename": "same.txt", "file_b64": base64.b64encode(b"Document ID: ABC123").decode(),
                   "upload_id": str(uuid4())}
        def upload():
            return client.post(f"/cases/{cid}/documents", headers=headers, json=payload)
        if concurrent:
            barrier = threading.Barrier(2)
            def simultaneous(_):
                barrier.wait(timeout=5)
                return upload()
            with ThreadPoolExecutor(max_workers=2) as pool:
                first, again = list(pool.map(simultaneous, range(2)))
        else:
            first, again = upload(), upload()
        assert first.status_code == 200, first.text
        assert first.json()["upload_id"] == payload["upload_id"]
        assert again.status_code == 200, again.text
        assert again.json()["document_id"] == first.json()["document_id"]
        conflict = client.post(f"/cases/{cid}/documents", headers=headers,
            json={**payload, "file_b64": base64.b64encode(b"Different content").decode()})
        assert conflict.status_code == 409
        with m.SessionLocal() as db:
            doc = db.get(models.DocumentAsset, first.json()["document_id"])
            owner = doc.uploaded_by
            assert owner and doc.uploader_role == "applicant"
            monkeypatch.setattr(ai_local, "llm_json", lambda *a, **k: {"doc_type": "other", "fields": {}})
            m._reprocess_doc(db, doc, db.get(models.CaseApplication, cid))
            assert doc.fields["_upload_id"] == payload["upload_id"]
            # 오래된 타 업로더 문서는 같은 키가 있어도 그 업로더에게만 공개한다.
            db.add(models.DocumentAsset(case_id=cid, filename="same.txt", content_b64=payload["file_b64"],
                uploaded_by="someone-else", fields={"_upload_id": "other-upload-1234"}))
            db.commit()
        documents = client.get(f"/cases/{cid}/documents", headers=headers).json()
        assert len(documents) == 2
        assert next(d for d in documents if d["uploaded_by"] != owner)["upload_id"] is None
        assert next(d for d in documents if d["uploaded_by"] == owner)["upload_id"] == payload["upload_id"]


def test_05_twenty_mib_txt_has_total_budget_across_reextract_and_verify(monkeypatch, memory_case):
    db, c = memory_case
    monkeypatch.setattr(extraction, "DOCUMENT_AI_MAX_CALLS", 4)
    calls = []
    text = "FIRST\n" + "x" * (20 * 1024 * 1024 - 12) + "\nLAST"
    assert len(extraction.document_chunks(text, "openai", "classify")) == 4
    def fake(system, user):
        calls.append(user)
        return {"doc_type": "consent", "fields": {}, "verdict": "ok", "field_issues": []}
    monkeypatch.setattr(ai_local, "llm_json", fake)
    monkeypatch.setattr(intake, "parse_file", lambda *a, **k: text)
    doc = models.DocumentAsset(case_id=c.case_id, filename="large.txt", content_b64="eA==", doc_type="other")
    db.add(doc)
    db.commit()
    m._reprocess_doc(db, doc, c, apply=False)
    m._verify_doc(db, doc, c)
    assert len(calls) == 4
    assert any("FIRST" in call for call in calls) and any("LAST" in call for call in calls)
    assert doc.fields["_ai_budget"]["used"] == 4
    assert doc.fields["_analysis"]["partial"]
    assert any(x.get("code") == "partial_analysis" for x in doc.fields["_verify"]["advisory"])
    assert "부분 분석" in doc.fields["_verify"]["summary"]
    assert all(not m._problem_requires_applicant(r) for r in m._problem_document_reasons(doc))
    m._verify_doc(db, doc, c)
    assert len(calls) == 4  # 큐 재전송도 남은 예산만 사용한다.


def test_05_public_ocr_uses_lower_budget(monkeypatch):
    from starlette.requests import Request
    monkeypatch.setattr(extraction, "PUBLIC_AI_MAX_CALLS", 2)
    monkeypatch.setattr(m, "_OCR_EXTRACT_CACHE", m.OrderedDict())
    monkeypatch.setattr(m.auth, "check_username_rate_limited", lambda *a: False)
    monkeypatch.setattr(m.auth, "record_username_check", lambda *a: None)
    monkeypatch.setattr(intake, "parse_file", lambda *a, **k: "FIRST" + "x" * 200000 + "LAST")
    calls = []
    monkeypatch.setattr(ai_local, "llm_json", lambda s, u: calls.append(u) or {"doc_type": "nib_business_license", "fields": {}})
    import fitz
    with fitz.open() as pdf:
        pdf.new_page()
        body = m.schemas.OCRExtractReq(image_b64=base64.b64encode(pdf.tobytes()).decode())
    result = m.auth_ocr_extract(body, Request({"type": "http", "headers": [], "client": ("127.0.0.1", 1234)}))
    assert result["ocr_available"]
    assert len(calls) == 2 < extraction.DOCUMENT_AI_MAX_CALLS
    assert "FIRST" in calls[0] and "LAST" in calls[-1]
    assert "advisory" not in result


APPLICANT_CODES = {"ENCRYPTED", "CORRUPT", "BOMB", "EMPTY_TEXT", "OCR_FAILED", "CERT_NOT_VALID",
                   "CERT_EXPIRED", "QUANT_FAIL", "EVIDENCE_SUPERSEDED", "ORIGINAL_NOT_STORED"}


@pytest.mark.parametrize("code", sorted(m.PROBLEM_DOC_MSG))
def test_06_every_problem_code_has_explicit_recipient_policy(code):
    assert m.APPLICANT_PROBLEM_CODES == APPLICANT_CODES
    assert m._problem_requires_applicant({"code": code}) is (code in APPLICANT_CODES)
    assert m._problem_requires_applicant({"code": "VERIFY_FAILED", "params": {"action_required": True}})
    assert not m._problem_requires_applicant({"code": "UNRECOGNIZED"})


@pytest.mark.parametrize("role", ["applicant", "consultant", "penyelia_halal", "pendamping_pph", "auditor", "operator", "admin", "fatwa_liaison"])
def test_07_common_role_filter_covers_nested_responses_without_mutation(role):
    payload = {"documents": [{"fields": {"_verify": {"advisory": ["PRIVATE NOTE"]}},
                              "verify": {"advisory": ["PRIVATE NOTE"]}}],
               "history": [{"details": {"advisory": ["PRIVATE NOTE"]}}]}
    original = copy.deepcopy(payload)
    visible = m._document_visible_fields(payload, role)
    assert ("PRIVATE NOTE" in json.dumps(visible)) is (role in m.STAFF_ROLES)
    assert payload == original


@pytest.mark.parametrize("filename,text,wrong", [
    ("중소기업확인서.pdf", "중소기업확인서\n사업자등록번호: 638-88-01141", "nib_business_license"),
    ("제품설명서.pdf", "제품설명서\nSample", "supplier_declaration"),
    ("제품 사진.pdf", "Product photos\nSample", "product_list"),
])
@pytest.mark.parametrize("mode", ["classify", "typed", "queue"])
def test_09_reference_rules_override_active_ai(monkeypatch, memory_case, filename, text, wrong, mode):
    db, c = memory_case
    monkeypatch.setattr(ai_local, "llm_json", lambda *a, **k: {"doc_type": wrong,
        "fields": {"nib": "638-88-01141", "product_names": ["Sample"]},
        "evidence": {"nib": "사업자등록번호: 638-88-01141", "product_names": ["Sample"]}})
    if mode == "queue":
        monkeypatch.setattr(intake, "parse_file", lambda *a, **k: text)
        doc = models.DocumentAsset(case_id=c.case_id, filename=filename, doc_type=wrong, content_b64="eA==")
        db.add(doc)
        db.commit()
        result = m._reprocess_doc(db, doc, c, keep_doc_type=True, apply=False)
    else:
        result = (intake.classify(filename, text) if mode == "classify" else
                  intake.parse_typed_text(wrong, filename, text))
    assert result["doc_type"] == "other"
    assert result["fields"]["_classification"]["review"]
    assert "nib" not in result["fields"] and "product_names" not in result["fields"]
