"""가입 등록증 원본·본문 보존과 실패 격리. AI/OCR 응답은 합성값으로 대체한다."""
import base64
import hashlib
from unittest.mock import Mock
from uuid import uuid4

import fitz
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app import ai_local, intake, models
from app.db import SessionLocal
from app.document_extraction import extraction_receipt
import app.main as m


DOC_FIELDS = {"company_name": "서류추출 테스트 기업", "nib": "123-45-67890",
              "responsible_person": "김서류", "address": "서울특별시 강남구 샘플로 1"}
QUOTES = {key: label + value for (key, value), label in zip(DOC_FIELDS.items(),
          ("상호: ", "등록번호: ", "대표자: ", "사업장소재지: "))}
TEXT = "사업자등록증\n" + "\n".join(QUOTES.values()) + "\n본문 보존 검증" * 200 + "\n본문 마지막"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("가입 서류 테스트에서 실제 AI/OCR 호출 금지")

    monkeypatch.setattr(ai_local, "provider", lambda: "openai")
    monkeypatch.setattr(ai_local, "llm_json", forbidden)
    monkeypatch.setattr(ai_local, "_openai_chat", forbidden)
    monkeypatch.setattr(intake, "parse_file", forbidden)
    monkeypatch.setattr(intake, "_ZIP_LOOKUP", False)
    monkeypatch.setattr(m, "verify_enqueue", lambda *a, **kw: False)
    monkeypatch.setattr(m, "_OCR_EXTRACT_CACHE", m.OrderedDict())
    monkeypatch.setattr(m, "_RL_WRITE_HITS", {})
    monkeypatch.setattr(m.auth, "check_username_rate_limited", lambda ip: False)
    monkeypatch.setattr(m.auth, "record_username_check", lambda ip: None)


@pytest.fixture
def client():
    with TestClient(m.app) as client:
        yield client


@pytest.fixture
def document(monkeypatch):
    with fitz.open() as pdf:
        pdf.new_page()
        raw = pdf.tobytes()
    parser = Mock(return_value=TEXT)
    reply = {"doc_type": "nib_business_license", "confidence": .95,
             "fields": dict(DOC_FIELDS), "evidence": dict(QUOTES)}
    ai = Mock(return_value=reply)
    monkeypatch.setattr(intake, "parse_file", parser)
    monkeypatch.setattr(ai_local, "llm_json", ai)
    return {"b64": base64.b64encode(raw).decode(), "raw": raw, "parser": parser, "ai": ai, "reply": reply}


def _body(**extra):
    digits = str(uuid4().int)[:10]
    return {"username": "signupdoc_" + uuid4().hex[:12], "password": "Passw0rd!23",
            "company_email": "company@example.com", "email_consent": True,
            "company_name": "가입 폼에서 확정한 업체", "nib": digits[:3] + "-" + digits[3:5] + "-" + digits[5:],
            "responsible_person": "이신청", "address": "가입 폼 주소", **extra}


def _register(client, body):
    response = client.post("/auth/register", json=body)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("doc_type,cached,filename", [
    ("nib_business_license", False, "사업자등록증.pdf"),
    ("nib_business_license", True, "사업자등록증.pdf"),
    ("factory_registration", False, "공장등록증.pdf"),
    ("factory_registration", True, None),
])
def test_signup_keeps_document_text_checklist_and_form_values(client, document, doc_type, cached, filename):
    document["reply"]["doc_type"] = doc_type
    if cached:
        response = client.post("/auth/ocr-extract", json={"image_b64": document["b64"]})
        assert response.status_code == 200, response.text
        assert response.json()["cached"] is False
        assert "signup_document" not in response.json()
        assert "본문 마지막" not in response.json()["raw"]
    body = _body(biz_doc_b64="data:application/pdf;base64," + document["b64"], biz_doc_filename=filename)
    metadata = {"company_name": {"source": "ai", "review": True, "evidence": body["company_name"]}}
    body["extraction_receipt"] = extraction_receipt({"company_name": body["company_name"]}, metadata, m.auth.SECRET)
    registered = _register(client, body)
    with SessionLocal() as db:
        case = db.query(models.CaseApplication).filter_by(org_id=registered["org_id"]).one()
        user = db.query(models.User).filter_by(username=body["username"]).one()
        doc = db.query(models.DocumentAsset).filter_by(case_id=case.case_id).one()
        assert doc.doc_type == doc_type
        assert doc.content_b64 == document["b64"]
        assert doc.text_full == TEXT and doc.text_excerpt == TEXT[:300]
        assert doc.confidence == .95 and doc.review_status == "pending"
        assert doc.filename == (filename or "upload.pdf")
        assert doc.filename_en == m._fn_en(doc.filename)
        assert doc.content_type == "application/pdf"
        assert doc.file_hash == hashlib.sha256(document["raw"]).hexdigest()
        assert doc.uploaded_by == user.user_id and doc.uploader_role == "applicant"
        assert {k: doc.fields[k] for k in DOC_FIELDS} == DOC_FIELDS
        assert doc.fields["_field_meta"]["company_name"]["source"] == "ai"
        for key in ("company_name", "nib", "responsible_person", "address"):
            assert getattr(case, key) == body[key]
        assert case.profile_ext["_registration_extraction"] == metadata
        assert db.get(models.Org, case.org_id).name == body["company_name"]
        uploaded = db.query(models.WorkflowEvent).filter_by(case_id=case.case_id, action="documents.signup_upload").one()
        assert uploaded.actor_id == user.user_id and uploaded.actor_type == "applicant"
        assert uploaded.payload == {"document_id": doc.document_id, "doc_type": doc_type}
        case_id = case.case_id
    response = client.get(f"/cases/{case_id}/doc-checklist", headers={"Authorization": "Bearer " + registered["token"]})
    assert response.status_code == 200, response.text
    row = next(row for row in response.json()["checklist"] if row["doc_type"] == doc_type)
    assert row["satisfied"] is True and row["file_count"] == 1
    document["parser"].assert_called_once()
    document["ai"].assert_called_once()  # 공개 OCR → 가입도 같은 본문·AI 결과를 재사용한다.


def _assert_no_documents(registered, body, *, failed=False):
    with SessionLocal() as db:
        case = db.query(models.CaseApplication).filter_by(org_id=registered["org_id"]).one()
        user = db.query(models.User).filter_by(username=body["username"]).one()
        assert case.company_name == body["company_name"] and case.nib == body["nib"]
        assert db.query(models.DocumentAsset).filter_by(case_id=case.case_id).count() == 0
        events = db.query(models.WorkflowEvent).filter_by(case_id=case.case_id).all()
        assert any(ev.action == "case.create.register" for ev in events)
        assert not any(ev.action == "documents.signup_upload" for ev in events)
        failures = [ev for ev in events if ev.action == "documents.signup_upload_failed"]
        assert len(failures) == int(failed)
        if failed:
            assert failures[0].actor_id == user.user_id
            assert failures[0].payload["error"]
            return failures[0].payload


def test_signup_without_document_preserves_existing_behavior(client):
    body = _body()
    _assert_no_documents(_register(client, body), body)


@pytest.mark.parametrize("b64,code", [("not base64!", "BAD_UPLOAD"),
                                     (base64.b64encode(b"not a PDF").decode(), "CONTENT_MISMATCH")])
def test_bad_upload_records_failure_without_blocking_signup(client, b64, code):
    body = _body(biz_doc_b64=b64, biz_doc_filename="사업자등록증.pdf")
    payload = _assert_no_documents(_register(client, body), body, failed=True)
    assert payload["detail"]["code"] == code


@pytest.mark.parametrize("failure", ["parser", "extraction", "flush"])
def test_document_exceptions_rollback_only_savepoint(client, document, monkeypatch, failure):
    if failure == "parser":
        document["parser"].side_effect = RuntimeError("synthetic OCR failure")
    elif failure == "extraction":
        monkeypatch.setattr(intake, "_extract_ai_document", Mock(side_effect=RuntimeError("synthetic extraction failure")))

    def violate_not_null(mapper, connection, target):
        target.filename = None  # 실제 INSERT 실패로 SQLAlchemy 세션까지 실패 상태로 만든다.

    if failure == "flush":
        event.listen(models.DocumentAsset, "before_insert", violate_not_null)
    try:
        body = _body(biz_doc_b64=document["b64"], biz_doc_filename="사업자등록증.pdf")
        payload = _assert_no_documents(_register(client, body), body, failed=True)
        if failure == "flush":
            assert payload["error"] == "IntegrityError"
    finally:
        if failure == "flush":
            event.remove(models.DocumentAsset, "before_insert", violate_not_null)


def test_non_applicant_document_is_skipped(client, document):
    document["reply"]["doc_type"] = "supplier_declaration"
    body = _body(biz_doc_b64=document["b64"], biz_doc_filename="사업자등록증.pdf")
    _assert_no_documents(_register(client, body), body)
