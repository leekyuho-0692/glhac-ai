"""H4/H5/H6: 판독 실패, 표기 우회, 처리 상한의 조용한 누락 회귀."""
import base64
import io
import os
import sys
import zipfile

import fitz
import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app.main as m
from app import intake, models, screening


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch):
    # DB/온톨로지 시드는 conftest의 fresh_db_per_module을 사용한다.
    monkeypatch.setattr(m, "_RL_WRITE_HITS", {})
    monkeypatch.setattr(m, "_AUTOPARSE", False)
    monkeypatch.setattr(m, "verify_enqueue", lambda *a, **kw: False)
    monkeypatch.setattr(intake, "_ZIP_LOOKUP", False)
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda *a, **kw: {"error": "LLM_UNAVAILABLE"})


def _token(client, username="applicant1"):
    response = client.post("/auth/login", json={"username": username, "password": "pw"})
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["token"]}


@pytest.fixture
def case():
    with TestClient(m.app) as client:
        headers = _token(client)
        response = client.post("/cases", headers=headers, json={"company_name": "PT Parse Regression"})
        assert response.status_code == 200, response.text
        yield client, headers, response.json()["case_id"]


def _b64(data):
    return base64.b64encode(data).decode()


def _zip(filename, data):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr(filename, data)
    return out.getvalue()


def _pdf(pages=1, hazard_page=None, encrypted=False):
    with fitz.open() as doc:
        for index in range(pages):
            page = doc.new_page()
            body = "Material list page %03d: water and sugar" % (index + 1)
            if index + 1 == hazard_page:
                body += "\nLemak babi"
            page.insert_text((50, 60), body)
        options = {"encryption": fitz.PDF_ENCRYPT_AES_256,
                   "owner_pw": "owner", "user_pw": "secret"} if encrypted else {}
        return doc.tobytes(**options)


def _xlsx(rows):
    wb = Workbook()
    for row in rows:
        wb.active.append(row)
    out = io.BytesIO()
    wb.save(out)
    wb.close()
    return out.getvalue()


def _upload(case, filename, data, doc_type="material_list"):
    client, headers, cid = case
    response = client.post(f"/cases/{cid}/parse-file", headers=headers,
                           json={"filename": filename, "file_b64": _b64(data), "doc_type": doc_type})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("failure,expected", [
    ({"ok": False, "error": "TIMEOUT"}, "TIMEOUT"),
    ({"ok": False, "error": "WORKER_TIMEOUT"}, "TIMEOUT"),
    ({"ok": False, "error": "WORKER_FAILED"}, "ERROR"),
    (TimeoutError("worker timed out"), "TIMEOUT"),
    (RuntimeError("broken worker"), "ERROR"),
])
def test_ocr_failure_is_not_empty_success(monkeypatch, failure, expected):
    def ocr(*args, **kwargs):
        if isinstance(failure, Exception):
            raise failure
        return failure
    monkeypatch.setattr(intake.ai_local, "ocr_image", ocr)
    text = intake.parse_file("사업자등록증.png", b"image")
    assert text == ""  # 기존 문자열 호출 계약 보존
    for result in (intake.classify("사업자등록증.png", text),
                   intake.parse_typed("nib_business_license", "사업자등록증.png", b"image")):
        assert result["fields"]["_ocr_error"] == expected
        assert result["fields"]["_empty"] is True
        assert result["confidence"] == 0


def test_successful_fallback_does_not_remain_failed(monkeypatch):
    def ocr(path, lang=None):
        return ({"ok": True, "lines": [{"text": "Recovered text", "confidence": 0.99}]}
                if lang else {"ok": False, "error": "TIMEOUT"})
    monkeypatch.setattr(intake.ai_local, "ocr_image", ocr)
    result = intake.classify("사업자등록증.png", intake.parse_file("사업자등록증.png", b"image"))
    assert "_ocr_error" not in result["fields"]
    assert "_empty" not in result["fields"]


@pytest.mark.parametrize("entry", ["parse", "zip", "reprocess"])
@pytest.mark.parametrize("failed", [True, False], ids=["timeout", "blank"])
def test_empty_documents_never_satisfy_checklist_and_failures_can_retry(case, monkeypatch, entry, failed):
    client, headers, cid = case
    ocr_result = {"ok": False, "error": "TIMEOUT"} if failed else {"ok": True, "lines": []}
    monkeypatch.setattr(intake.ai_local, "ocr_image", lambda *a, **kw: ocr_result)
    filename, data = "사업자등록증.png", base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABpfZFQAAAAABJRU5ErkJggg==')
    if entry == "parse":
        # 잘못 선택한 유형을 파일명으로 교정할 때도 빈 문자열의 실패 메타데이터를 보존한다.
        result = _upload(case, filename, data, "factory_registration")
        assert result["doc_type"] == "nib_business_license"
    elif entry == "zip":
        response = client.post(f"/cases/{cid}/intake-zip", headers=headers,
                               json={"zip_b64": _b64(_zip(filename, data))})
        assert response.status_code == 200, response.text
        row = next(x for x in response.json()["checklist"] if x["doc_type"] == "nib_business_license")
        assert row["satisfied"] is False
    else:
        with m.SessionLocal() as db:
            doc = models.DocumentAsset(case_id=cid, filename=filename, doc_type="nib_business_license",
                                       content_b64=_b64(data), confidence=0.6, fields={})
            db.add(doc)
            db.commit()
            did = doc.document_id
        response = client.post(f"/documents/{did}/reprocess?apply=false", headers=_token(client, "operator1"))
        assert response.status_code == 200, response.text
    docs = client.get(f"/cases/{cid}/documents", headers=headers).json()
    assert len(docs) == 1
    assert docs[0]["fields"].get("_ocr_error") == ("TIMEOUT" if failed else None)
    checklist = client.get(f"/cases/{cid}/doc-checklist", headers=headers).json()
    row = next(x for x in checklist["checklist"] if x["doc_type"] == "nib_business_license")
    assert row["satisfied"] is False and row["status"] == "unreadable"
    package = client.get(f"/cases/{cid}/package-check", headers=headers).json()
    assert len(package["unreadable"]) == 1
    pending = client.get(f"/cases/{cid}/pending-extractions", headers=headers).json()
    assert pending["failed_count"] == int(failed)
    if failed:
        assert pending["failed"][0]["error"] == "TIMEOUT"
        assert pending["failed"][0]["stage"] == "ocr"
    queued = []
    monkeypatch.setattr(m, "autoparse_enqueue", lambda did, keep_doc_type=False: queued.append((did, keep_doc_type)) or True)
    response = client.post(f"/cases/{cid}/documents/reprocess-pending", headers=_token(client, "operator1"))
    assert response.status_code == 200, response.text
    assert response.json()["found"] == response.json()["queued"] == int(failed)
    assert queued == ([(docs[0]["document_id"], True)] if failed else [])


@pytest.mark.parametrize("reason", ["ENCRYPTED", "CORRUPT"])
def test_pdf_failure_reason_survives_upload(case, reason):
    if reason == "ENCRYPTED":
        result = _upload(case, "사업자등록증.pdf", _pdf(encrypted=True), "nib_business_license")
    else:
        # Legacy damaged originals remain quarantined when reprocessed.
        client, headers, cid = case
        with m.SessionLocal() as db:
            doc = models.DocumentAsset(case_id=cid, filename="사업자등록증.pdf", doc_type="nib_business_license",
                                       content_b64=_b64(b"%PDF-1.4 broken"))
            db.add(doc)
            db.commit()
            result = {"extracted": m._reprocess_doc(db, doc, db.get(models.CaseApplication, cid),
                                                    keep_doc_type=True)["fields"]}
    assert result["extracted"]["_ocr_error"] == reason
    client, headers, cid = case
    assert client.get(f"/cases/{cid}/pending-extractions", headers=headers).json()["failed"][0]["error"] == reason


def test_successful_reprocessing_clears_previous_ocr_failure(case, monkeypatch):
    monkeypatch.setattr(intake.ai_local, "ocr_image", lambda *a, **kw: {"ok": False, "error": "TIMEOUT"})
    _upload(case, "사업자등록증.png", base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABpfZFQAAAAABJRU5ErkJggg=='), "nib_business_license")
    client, headers, cid = case
    did = client.get(f"/cases/{cid}/documents", headers=headers).json()[0]["document_id"]
    monkeypatch.setattr(intake.ai_local, "ocr_image", lambda *a, **kw: {
        "ok": True, "lines": [{"text": "사업자등록번호 1234567890 업체명 PT Parse", "confidence": 0.99}]})
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda system, user, **kw:
                        {"doc_type": "nib_business_license", "confidence": 0.9, "fields": {}}
                        if "doc_type" in system else {"nib": "1234567890"})
    response = client.post(f"/documents/{did}/reprocess?apply=false", headers=_token(client, "operator1"))
    assert response.status_code == 200, response.text
    assert "_ocr_error" not in response.json()["fields"]
    assert "_empty" not in response.json()["fields"]
    assert client.get(f"/cases/{cid}/pending-extractions", headers=headers).json()["failed_count"] == 0
    row = next(x for x in client.get(f"/cases/{cid}/doc-checklist", headers=headers).json()["checklist"]
               if x["doc_type"] == "nib_business_license")
    assert row["satisfied"] is True


@pytest.mark.parametrize("name", [
    "돼지지방(라드)", "돼지지방", "돈지", "라드", "주정(알코올)", "주정",
    "Rhum", "Mirin", "Angciu", "ang ciu", "Sake", "Champagne", "Tuak", "Brem",
    "맛술", "미림", "청주", "Hog casing", "B4bi", "L.A.R.D", "Lаrd",
    "Pig skin collagen", "ＬＡＲＤ", "Lαrd", "1ard", "p1g", "p0rk", "b33r",
    "ΜΙRΙN", "RΗUM",
    "Salt (lard)", "Lard (salt)", "Sugar (Mirin)", "Mirin (sugar)",
])
def test_haram_spelling_variants_are_blocked(name):
    result = screening.screen_merged(name)
    assert (result["result"], result["severity"], result["status"]) == ("BLOCK", "high", "haram"), result


@pytest.mark.parametrize("name", [
    "porkfat", "pork-fat", "lemakbabi", "minyakbabi", "porkgelatin", "lardoil",
])
def test_haram_compounds_are_blocked_and_scanned(name):
    result = screening.screen_merged(name)
    assert (result["result"], result["severity"], result["status"]) == ("BLOCK", "high", "haram"), result
    scan = screening.screen_text(name)
    assert scan["verdict"] == "high"
    assert scan["haram_hits"]


@pytest.mark.parametrize("name,alias", [
    ("hamburger", "ham"), ("serum", "rum"), ("pisang", "san"), ("santan", "san"),
])
def test_short_latin_aliases_do_not_match_inside_words(name, alias):
    item = models.IngredientOntology(default_status="haram", severity="high")
    assert not screening._contains_alias(name, alias, item)
    assert screening._contains_alias(alias, alias, item)
    assert screening.screen_merged(name)["status"] != "haram"
    assert not screening.screen_text(name)["haram_hits"]


@pytest.mark.parametrize("name", ["Gélatine", "Non-pork gelatin", "Non‑pork gelatin", "pork-free gelatin",
                                  "babi-free gelatin", "bebas babi gelatin", "무돼지 젤라틴"])
def test_gelatin_and_negated_pork_keep_generic_source_check(name):
    expected = screening.screen_merged("gelatin")
    actual = screening.screen_merged(name)
    for key in ("result", "severity", "status", "matched_uid", "required_evidence"):
        assert actual[key] == expected[key], (name, key, actual)
    assert actual["result"] == "NEEDS_EVIDENCE"
    assert not screening.screen_text(name)["haram_hits"]


def test_short_haram_alias_does_not_match_inside_a_korean_word():
    assert screening.screen_merged("개선된 향료")["result"] != "BLOCK"
    assert not screening.screen_text("개선된 향료")["haram_hits"]


@pytest.mark.parametrize("name,number", [
    ("E120", "E120"), ("E-120", "E120"), ("E 120", "E120"), ("Ｅ－１２０", "E120"),
    ("코치닐추출색소", "E120"), ("코치닐", "E120"), ("카민", "E120"),
    ("cochineal", "E120"), ("carmine", "E120"), ("E-471", "E471"), ("E–471", "E471"),
    ("Emulsifier (E-471)", "E471"),
])
def test_names_and_e_numbers_use_the_same_ontology_grade(name, number):
    # E120은 현재 사전의 mushbooh/medium 정책을 유지한다. 판정 정책 변경과 매칭 수정을 분리.
    expected = screening.screen_merged("", e_number=number)
    actual = screening.screen_merged(name)
    for key in ("result", "severity", "status", "matched_uid"):
        assert actual[key] == expected[key], (name, key, actual)
    assert expected["matched_uid"] in {h["uid"] for h in screening.screen_text(name)["ontology_hits"]}


def test_e_numbers_and_safe_ontology_cannot_override_haram_keywords():
    for source in (None, "plant", "mineral", "animal", "synthetic"):
        result = screening.screen_merged("Pig skin collagen (E330)", source=source, cert_no="ID00410000500391022")
        assert result["result"] == "BLOCK" and result["severity"] == "high"
    assert screening.screen_merged("Water", note="contains pork")["result"] == "BLOCK"
    assert screening.screen_merged("Water", note="animal origin")["result"] == "NEEDS_EVIDENCE"
    assert screening.screen_merged("Non-pork gelatin, lard")["result"] == "BLOCK"
    assert screening.extract_e_numbers("E1200 E-471 E330") == ["E1200", "E471", "E330"]
    assert screening.extract_e_number("E330 E-471") == "E471"


@pytest.mark.parametrize("mode", ["off", "format", "strict"])
def test_alcohol_category_cannot_clear_with_only_a_verified_certificate(monkeypatch, mode):
    monkeypatch.setenv("GLHAC_CERT_VERIFY", mode)
    number = "ID00410000500391022"
    docs = [{"doc_type": "halal_certificate", "fields": {"cert_no": number, "expiry_date": "2099-01-01"},
             "text": "Sertifikat Halal\nCertificate No: " + number + "\nValid until: 2099-01-01"}]
    result = screening.screen_merged("Ethanol", cert_no=number, docs=docs)
    assert result["cert_verification"]["verdict"] == "VERIFIED"
    assert result["result"] == "NEEDS_EVIDENCE" and result["cert_promoted"] is False
    assert screening.screen_merged("Gelatin", cert_no=number, docs=docs)["result"] == "CLEARED"


@pytest.mark.parametrize("entry", ["parse", "zip"])
def test_document_material_registration_populates_e_number(case, entry):
    data = _xlsx([["No", "Nama Bahan", "Jenis Bahan", "Produsen"],
                  [1, "E-471", "Raw material", "Supplier"]])
    client, headers, cid = case
    if entry == "parse":
        _upload(case, "Daftar Bahan.xlsx", data)
    else:
        response = client.post(f"/cases/{cid}/intake-zip", headers=headers,
                               json={"zip_b64": _b64(_zip("Daftar Bahan.xlsx", data))})
        assert response.status_code == 200, response.text
    with m.SessionLocal() as db:
        material = db.query(models.Material).filter_by(case_id=cid).one()
        assert material.e_number == "E471"
        assert material.matched_uid == screening._CACHE["by_e"]["E471"].ingredient_uid


def _assert_tail_haram(fields):
    scan = fields["_tail_screening"]
    assert scan["verdict"] == "high"
    assert "ing.lard" in {hit["uid"] for hit in scan["haram_hits"]}


def test_pdf_page_limit_reports_and_scans_page_56_without_extra_ocr_or_llm(case, monkeypatch):
    calls = []
    def no_ocr(*a, **kw):
        pytest.fail("텍스트 PDF/상한 뒤 페이지에서 OCR을 호출함")
    monkeypatch.setattr(intake.ai_local, "ocr_image", no_ocr)
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda s, u, **kw: calls.append(u) or {"material_names": ["Water"]})
    result = _upload(case, "long_60p.pdf", _pdf(60, 56))
    fields = result["extracted"]
    assert fields["_truncated"] == {"kind": "pages", "limit": 40, "total": 60}
    _assert_tail_haram(fields)
    assert calls and all("Lemak babi" not in body and "page 056" not in body for body in calls)
    client, headers, cid = case
    docs = client.get(f"/cases/{cid}/documents", headers=headers).json()
    warning = docs[0]["warnings"][0]
    assert warning["code"] == "PARSE_TRUNCATED" and warning["truncated"] == fields["_truncated"]
    assert warning["tail_screening"] == fields["_tail_screening"]
    package = client.get(f"/cases/{cid}/package-check", headers=headers).json()
    assert package["warnings"][0]["truncated"] == fields["_truncated"]
    assert package["complete"] is False


def test_pdf_character_limit_scans_native_tail(monkeypatch):
    monkeypatch.setattr(intake, "PDF_MAX_TEXT_CHARS", 100)
    monkeypatch.setattr(intake.ai_local, "ocr_image", lambda *a, **kw: pytest.fail("상한 뒤 OCR"))
    text = intake.parse_file("list.pdf", _pdf(8, 8))
    result = intake.classify("list.pdf", text)
    assert len(text) == 100
    marker = result["fields"]["_truncated"]
    assert marker["kind"] == "chars" and marker["limit"] == 100 and marker["total"] > 100
    _assert_tail_haram(result["fields"])


def test_blank_pdf_tail_is_not_sent_to_ocr(monkeypatch):
    monkeypatch.setattr(intake, "PDF_MAX_PAGES", 1)
    monkeypatch.setattr(intake.ai_local, "ocr_image", lambda *a, **kw: pytest.fail("상한 뒤 OCR"))
    with fitz.open(stream=_pdf(), filetype="pdf") as doc:
        doc.new_page()  # 읽을 텍스트가 없는 상한 밖 페이지
        data = doc.tobytes()
    result = intake.parse_typed("material_list", "list.pdf", data)
    assert result["fields"]["_truncated"] == {"kind": "pages", "limit": 1, "total": 2}


def test_page_and_chunk_limits_preserve_both_tail_detections(monkeypatch):
    calls = []
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda s, u, **kw: calls.append(u) or {"names": ["Water"]})
    with fitz.open() as doc:
        for page_no in range(1, 61):
            body = ("Ordinary material description " * 3 + "\n") * 30
            if page_no == 30:
                body += "Lemak babi"
            if page_no == 56:
                body += "Mirin"
            doc.new_page().insert_text((50, 60), body, fontsize=8)
        data = doc.tobytes()
    result = intake.parse_typed("material_list", "long.pdf", data)
    assert len(calls) == 12
    assert all("Lemak babi" not in body and "Mirin" not in body for body in calls)
    fields = result["fields"]
    assert {marker["kind"] for marker in fields["_truncations"]} == {"pages", "chunks"}
    _assert_tail_haram(fields)
    assert "ing.ethanol_beverage" in {h["uid"] for h in fields["_tail_screening"]["haram_hits"]}


def test_generic_xlsx_limit_reports_and_scans_tail():
    rows = [["Ordinary note row %04d with additional details" % i] for i in range(1500)] + [["Lemak babi"]]
    full = "\n".join(row[0] for row in rows)
    text = intake.parse_file("long.xlsx", _xlsx(rows))
    assert len(text) == 20000 and "Lemak babi" not in text
    for result in (intake.classify("long.xlsx", text), intake.parse_typed_text("material_list", "long.xlsx", text)):
        assert result["fields"]["_truncated"] == {"kind": "chars", "limit": 20000, "total": len(full)}
        _assert_tail_haram(result["fields"])


@pytest.mark.parametrize("entry", ["classify", "typed"])
def test_list_chunk_limit_reports_and_scans_unprocessed_tail(monkeypatch, entry):
    text = "\n".join("BAHAN-%04d keterangan panjang untuk baris ini ......................" % i
                     for i in range(1500)) + "\nLemak babi"
    calls = []
    def llm(system, user, **kwargs):
        if "서류 분류기" in system:
            return {"doc_type": "material_list", "confidence": 0.9, "fields": {}}
        calls.append(user)
        return {"names": [line for line in user.splitlines() if line.startswith("BAHAN")]}
    monkeypatch.setattr(intake.ai_local, "llm_json", llm)
    result = (intake.classify("list.txt", text) if entry == "classify"
              else intake.parse_typed_text("material_list", "list.txt", text))
    fields = result["fields"]
    assert len(calls) == 12
    assert fields["_truncated"] == {"kind": "chunks", "limit": 12, "total": len(intake._chunk_text(text))}
    assert len(fields["material_names"]) < 1500
    assert all("Lemak babi" not in body for body in calls)
    _assert_tail_haram(fields)


def test_short_text_has_no_truncation_warning():
    result = intake.parse_typed("material_list", "short.txt", b"Water\nSugar")
    assert "_truncated" not in result["fields"]
    assert "_tail_screening" not in result["fields"]


@pytest.mark.parametrize("names,truncated", [(["Water", "Sugar"], False), (["Water", "Sugar", "Lemak babi"], True)])
def test_item_cap_only_warns_when_output_is_incomplete(monkeypatch, names, truncated):
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda *a, **kw: {"names": names})
    fields = {}
    assert intake._extract_list_complete("list.txt", "\n".join(names), "원재료명", cap=2, fields=fields) == ["Water", "Sugar"]
    assert ("_truncated" in fields) is truncated
    if truncated:
        _assert_tail_haram(fields)
