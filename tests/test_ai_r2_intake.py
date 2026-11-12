"""AI R2: 감사 입력을 고정한 판독·측정값·Office·LLM 스키마 회귀.

원본 감사 자료는 읽기 전용이다. 큰 Office 재현본도 여기서 생성해 CI에서 독립 실행한다.
"""
import base64
import io
import sys
import unicodedata
import zipfile
from pathlib import Path

import docx
import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import intake, models
import app.main as m


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch):
    monkeypatch.setattr(m, "_RL_WRITE_HITS", {})
    monkeypatch.setattr(m, "_AUTOPARSE", False)
    monkeypatch.setattr(m, "verify_enqueue", lambda *a, **kw: False)
    monkeypatch.setattr(intake, "_ZIP_LOOKUP", False)
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda *a, **kw: {"error": "LLM_UNAVAILABLE"})


@pytest.fixture
def case():
    with TestClient(m.app, raise_server_exceptions=False) as client:
        response = client.post("/auth/login", json={"username": "applicant1", "password": "pw"})
        assert response.status_code == 200, response.text
        headers = {"Authorization": "Bearer " + response.json()["token"]}
        response = client.post("/cases", headers=headers, json={"company_name": "PT R2 Intake"})
        assert response.status_code == 200, response.text
        yield client, headers, response.json()["case_id"]


def _archive(entries, compression=zipfile.ZIP_DEFLATED):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=compression, compresslevel=9) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return out.getvalue()


def _upload(case, name, data, doc_type="coa_msds", route="parse-file"):
    client, headers, cid = case
    payload = ({"zip_b64": base64.b64encode(_archive({name: data}, zipfile.ZIP_STORED)).decode()}
               if route == "intake-zip" else
               {"filename": name, "file_b64": base64.b64encode(data).decode(), "doc_type": doc_type})
    response = client.post(f"/cases/{cid}/{route}", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _documents(case):
    client, headers, cid = case
    return client.get(f"/cases/{cid}/documents", headers=headers).json()


@pytest.mark.parametrize("text,expected", [
    ("Porcine DNA PCR: Negative control\nSample result: Detected", {"pork_dna": 1.0}),
    ("Porcine DNA PCR: Positive control\nSample result: Not detected", {"pork_dna": 0.0}),
    ("DNA babi PCR: kontrol negatif\nHasil sampel: terdeteksi", {"pork_dna": 1.0}),
    ("DNA babi PCR: kontrol positif\nSampel: tidak terdeteksi", {"pork_dna": 0.0}),
    ("돼지 DNA PCR: 음성 대조군\n시료 결과: 검출", {"pork_dna": 1.0}),
    ("돼지 DNA PCR: 양성 대조\n검체 결과: 불검출", {"pork_dna": 0.0}),
    ("Porcine DNA PCR: Negative control\nResult of sample: Detected", {"pork_dna": 1.0}),
    ("Porcine DNA PCR: Negative control", {"pork_dna_unclear": True}),
    ("Porcine DNA PCR: Positive control\nResult: Not detected", {"pork_dna_unclear": True}),
    ("Porcine DNA PCR: Negative control\nSalmonella sample: Detected", {"pork_dna_unclear": True}),
    ("Porcine DNA PCR: Detected | Salmonella: Negative", {"pork_dna": 1.0}),
    ("Porcine DNA PCR\nSample | Control\nDetected | Negative", {"pork_dna": 1.0}),
    ("Test | Sample | Negative control\nPorcine DNA PCR | Detected | Negative", {"pork_dna": 1.0}),
    ("Test | Positive control | Sampel\nPorcine DNA PCR | Positive | Not detected", {"pork_dna": 0.0}),
    ("Sample | Control\nPorcine DNA PCR | Detected | Negative", {"pork_dna": 1.0}),
    ("Porcine DNA PCR | Sample: Detected | Negative control: Negative", {"pork_dna": 1.0}),
    ("Porcine DNA PCR: Negative control, Sample result: Detected", {"pork_dna": 1.0}),
    ("Test | Sample | Control\nPorcine DNA PCR | | Negative", {"pork_dna_unclear": True}),
    ("Porcine DNA PCR\nSample | Control\nsee attachment | Negative", {"pork_dna_unclear": True}),
])
def test_control_and_sample_results(text, expected):
    assert intake.extract_measurements(text) == expected


@pytest.mark.parametrize("text,verdict", [
    ("Porcine DNA PCR: Negative control\nSample result: Detected", "fail"),
    ("Porcine DNA PCR: Positive control\nSample result: Not detected", "pass"),
    ("Porcine DNA PCR: Negative control", "review"),
])
def test_control_verdict_is_persisted(case, text, verdict):
    _upload(case, "CoA.txt", text.encode())
    with m.SessionLocal() as db:
        row = db.query(models.MaterialMeasurement).filter_by(case_id=case[2], param_key="pork_dna").one()
        assert row.verdict == verdict
    if verdict == "review":
        client, headers, cid = case
        staff = client.post("/auth/login", json={"username":"operator1", "password":"pw"}).json()
        problems = client.get(f"/cases/{cid}/problem-documents", headers={"Authorization":"Bearer " + staff["token"]}).json()
        assert any(r["code"] == "PORK_DNA_UNCLEAR" for d in problems["documents"] for r in d["reasons"])


@pytest.mark.parametrize("text", [
    "Alcohol-free formula. Contains 12% glycerin", "Alcohol - free formula: 12%",
    "Bebas alkohol. Gliserin 12%", "무알코올 배합: 글리세린 12%",
    "Alcohol glycerin 12%", "Ethanol: glycerin content 12%", "Ethanol\nGlycerin 12%",
    "Ethanol CAS 64-17-5", "Alcohol bebas 12%",
])
def test_other_ingredients_and_free_claims_are_not_measurements(text):
    assert intake.extract_measurements(text) == {}


def test_intervening_analyte_owns_the_value():
    assert intake.extract_measurements("Lead cadmium 2 ppm") == {"cadmium_ppm": 2.0}


@pytest.mark.parametrize("text,key,value,qualifier,lod", [
    ("Ethanol: 1,5 %", "ethanol_pct", 1.5, None, None),
    ("Ethanol (CAS 64-17-5): 0.2%", "ethanol_pct", .2, None, None),
    ("Ethanol 64-17-5 0.1%", "ethanol_pct", .1, None, None),
    ("Timbal (Pb): 1500 ppb", "lead_ppm", 1.5, None, None),
    ("Lead: 1,5 mg/kg", "lead_ppm", 1.5, None, None),
    ("Cadmium: 5 ppb", "cadmium_ppm", .005, None, None),
    ("Mercury: 2 ppm", "mercury_ppm", 2, None, None),
    ("Arsenic: 3 mg/kg", "arsenic_ppm", 3, None, None),
    ("Ethanol: ND (LOD 0.01%)", "ethanol_pct", .01, "ND", .01),
    ("Ethanol: Not detected (LOD: 0,01%)", "ethanol_pct", .01, "ND", .01),
    ("Ethanol: <LOD (LOD 0.01%)", "ethanol_pct", .01, "<", .01),
    ("Ethanol: <LOD (0.01%)", "ethanol_pct", .01, "<", .01),
    ("Ethanol: <0.01", "ethanol_pct", .01, "<", .01),
    ("Ethanol: <0,01 %", "ethanol_pct", .01, "<", .01),
    ("Lead: ND (LOD 10 ppb)", "lead_ppm", .01, "ND", .01),
    ("Timbal (Pb): <10 ppb", "lead_ppm", .01, "<", .01),
    ("Ethanol: ND", "ethanol_pct", None, "ND", None),
    ("Lead: <LOD", "lead_ppm", None, "<", None),
    ("Ethanol\n0.1%", "ethanol_pct", .1, None, None),
])
def test_quant_values_and_qualifiers(text, key, value, qualifier, lod):
    result = intake.parse_typed_text("coa_msds", "CoA.txt", text)
    if value is None:
        assert result["fields"]["measurements"][key] is None
    else:
        assert result["fields"]["measurements"][key] == pytest.approx(value)
    meta = result["fields"]["measurements_meta"][key]
    assert meta.get("qualifier") == qualifier
    assert meta.get("lod") == lod
    assert meta["unit"] == ("%" if key == "ethanol_pct" else "ppm")


def test_quant_metadata_survives_database(case):
    _upload(case, "CoA.txt", b"Ethanol: ND (LOD 0.01%)\nTimbal (Pb): <10 ppb")
    doc = _documents(case)[0]
    assert doc["fields"]["measurements_meta"]["ethanol_pct"]["qualifier"] == "ND"
    assert doc["fields"]["measurements_meta"]["lead_ppm"]["lod"] == .01
    with m.SessionLocal() as db:
        rows = db.query(models.MaterialMeasurement).filter_by(case_id=case[2]).all()
        assert {r.param_key: r.value for r in rows} == {"ethanol_pct": .01, "lead_ppm": .01}


def _office(ext, text="Normal document"):
    out = io.BytesIO()
    if ext == "docx":
        document = docx.Document()
        document.add_paragraph(text)
        document.save(out)
    else:
        wb = Workbook()
        wb.active.append([text])
        wb.save(out)
        wb.close()
    return out.getvalue()


def _replace_entry(raw, name, content, compression=zipfile.ZIP_DEFLATED):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = {i.filename: archive.read(i) for i in archive.infolist()}
    entries[name] = content
    return _archive(entries, compression)


@pytest.fixture(scope="module")
def docx_bomb():
    # 감사 재현과 같은 100,000개 단락: 텍스트 21,099,999자, 압축 파일 약 128KB.
    paragraph = "<w:p><w:r><w:t>" + "Audit document repeated paragraph. " * 6 + "</w:t></w:r></w:p>"
    xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           '<w:body>' + paragraph * 100000 + '</w:body></w:document>')
    raw = _replace_entry(_office("docx"), "word/document.xml", xml)
    assert 120000 < len(raw) < 140000
    return raw


@pytest.mark.parametrize("ext", ["docx", "xlsx"])
@pytest.mark.parametrize("limit", ["ratio", "single", "total", "entries"])
def test_office_limits_are_checked_before_library_load(monkeypatch, ext, limit):
    raw = _office(ext)
    setting = {"ratio": "ZIP_MAX_COMPRESSION_RATIO", "single": "ZIP_MAX_SINGLE_FILE",
               "total": "ZIP_MAX_UNCOMPRESSED", "entries": "ZIP_MAX_ENTRIES"}[limit]
    monkeypatch.setattr(intake, setting, 1)
    def forbidden(*a, **kw):
        pytest.fail("Office loader ran before archive validation")
    if ext == "docx":
        monkeypatch.setattr(docx, "Document", forbidden)
    else:
        import openpyxl
        monkeypatch.setattr(openpyxl, "load_workbook", forbidden)
    text = intake.parse_file("data." + ext, raw)
    assert text == "" and text.parse_fields["_ocr_error"] == "BOMB"


@pytest.mark.parametrize("route", ["parse-file", "intake-zip"])
def test_128kb_docx_does_not_store_21m_characters(case, docx_bomb, route):
    # R7: 부분 헤더만 검사한 거대 본문은 업로드 단계에서 거부한다.
    # 기존 원본의 직접 재처리도 여전히 BOMB로 격리하고 설명을 제공해야 한다.
    client, headers, cid = case
    payload = ({"zip_b64": base64.b64encode(_archive({"docx_bomb.docx": docx_bomb})).decode()}
               if route == "intake-zip" else
               {"filename": "docx_bomb.docx", "file_b64": base64.b64encode(docx_bomb).decode(), "doc_type": "quality_cert"})
    response = client.post(f"/cases/{cid}/{route}", headers=headers, json=payload)
    assert response.status_code == 415, response.text
    assert _documents(case) == []
    text = intake.parse_file("docx_bomb.docx", docx_bomb)
    assert text == "" and text.parse_fields["_ocr_error"] == "BOMB"
    reasons = m._problem_document_reasons(models.DocumentAsset(
        filename="docx_bomb.docx", fields=text.parse_fields, content_b64=base64.b64encode(docx_bomb).decode()))
    assert [r["code"] for r in reasons] == ["BOMB"]
    for lang in ("ko", "en", "id"):
        assert m._problem_document_explanation(reasons[0], lang)


@pytest.mark.parametrize("ext", ["docx", "xlsx"])
def test_office_text_is_bounded_and_truncation_persisted(monkeypatch, case, ext):
    monkeypatch.setattr(intake, "OFFICE_MAX_TEXT_CHARS", 40)
    raw = _office(ext, "Normal document " * 10)
    _upload(case, "data." + ext, raw)
    with m.SessionLocal() as db:
        stored = db.query(models.DocumentAsset).filter_by(case_id=case[2]).one()
        assert len(stored.text_full) == 40
        assert stored.fields["_truncated"]["kind"] == "chars"
        assert stored.fields["_truncated"]["total"] > 40


@pytest.mark.parametrize("ext", ["docx", "xlsx"])
def test_office_sample_control_table_keeps_column_positions(ext):
    rows = [["Test", "Sample", "Control"], ["Porcine DNA PCR", "Detected", "Negative"]]
    out = io.BytesIO()
    if ext == "docx":
        document = docx.Document()
        table = document.add_table(rows=2, cols=3)
        for row, values in zip(table.rows, rows):
            for cell, value in zip(row.cells, values):
                cell.text = value
        document.save(out)
    else:
        wb = Workbook()
        for row in rows:
            wb.active.append(row)
        wb.save(out)
        wb.close()
    text = intake.parse_file("CoA." + ext, out.getvalue())
    assert intake.extract_measurements(text) == {"pork_dna": 1.0}


@pytest.mark.parametrize("ext,entry", [("docx", "word/document.xml"), ("xlsx", "xl/workbook.xml")])
@pytest.mark.parametrize("damage", ["zip", "xml", "crc"])
def test_corrupt_office_is_not_ocr_failure(ext, entry, damage):
    raw = _office(ext)
    if damage == "zip":
        raw = b"PK\x03\x04broken package"
    elif damage == "xml":
        raw = _replace_entry(raw, entry, "<broken>")
    else:
        raw = _replace_entry(raw, entry, "<unique-r2-crc/>", zipfile.ZIP_STORED)
        raw = raw.replace(b"<unique-r2-crc/>", b"<unique-r2-CRC/>")
    result = intake.parse_typed("coa_msds", "data." + ext, raw)
    assert result["fields"]["_ocr_error"] == "CORRUPT"
    assert result["text"] == "" and result["confidence"] == 0


@pytest.mark.parametrize("fields", [None, [], "bad", 42])
def test_invalid_fields_are_normalised(monkeypatch, fields):
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda *a, **kw: {
        "doc_type": "material_list", "fields": fields, "confidence": "high"})
    result = intake.classify("unknown.txt", "Neutral document body")
    assert result["fields"] == {} and result["llm_error"] == "BAD_RESPONSE"
    assert result["confidence"] == 0


def test_llm_cannot_inject_parser_metadata_or_nested_scalars(monkeypatch):
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda *a, **kw: {
        "doc_type": "coa_msds", "fields": {"_truncated": "bad", "issuer": {"bad": "shape"},
                                          "measurements": {"pork_dna": 0}}, "confidence": "high"})
    result = intake.classify("unknown.txt", "Neutral document body")
    assert result["fields"] == {} and result["llm_error"] == "BAD_RESPONSE"


@pytest.mark.parametrize("confidence,expected", [("high", .9), ("medium", .5), ("low", .25),
    ("nonsense", .5), (None, .5), ([], .5), ("nan", .5), ("inf", .5), ("0.8", .8)])
def test_confidence_is_a_finite_number(monkeypatch, confidence, expected):
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda *a, **kw: {
        "doc_type": "material_list", "fields": {}, "confidence": confidence})
    assert intake.classify("unknown.txt", "Neutral document body")["confidence"] == expected


@pytest.mark.parametrize("reply", [
    {"doc_type": "nib_business_license", "fields": None, "confidence": .9},
    {"doc_type": "nib_business_license", "fields": {}, "confidence": "high"},
    {"doc_type": "material_list", "fields": {"material_names": "Gelatin, Lard\nSugar"}, "confidence": .9},
])
def test_audit_malformed_responses_do_not_return_500(case, monkeypatch, reply):
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda *a, **kw: reply)
    _upload(case, "unknown.txt", b"Neutral document containing ingredient information.", route="intake-zip")
    fields = _documents(case)[0]["fields"]
    if reply["fields"] is None:
        assert fields["_llm_error"] == "BAD_RESPONSE"
    if isinstance(reply["fields"], dict) and "material_names" in reply["fields"]:
        assert fields["material_names"] == ["Gelatin", "Lard", "Sugar"]


@pytest.mark.parametrize("key", ["material_names", "product_names", "ingredients"])
def test_typed_list_strings_are_not_iterated_as_characters(monkeypatch, key):
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda *a, **kw: {key: "Sugar, Salt\nWater"})
    result = intake.parse_typed_text("coa_msds", "CoA.txt", "Sugar Salt Water")
    assert result["fields"][key] == ["Sugar", "Salt", "Water"]


def test_chunked_string_list_is_normalised(monkeypatch):
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda *a, **kw: {"names": "Sugar, Salt\nWater"})
    result = intake.parse_typed_text("material_list", "unknown.txt", "Sugar Salt Water\n" * 200)
    assert result["fields"]["material_names"] == ["Sugar", "Salt", "Water"]


@pytest.mark.parametrize("name,expected", [("scan.txt", 0), ("NIB.txt", .9)])
def test_none_provider_does_not_save_error_or_claim_llm_confidence(name, expected):
    result = intake.parse_typed_text("nib_business_license", name, "Unrecognised body text")
    assert result["fields"] == {}
    assert "llm_error" not in result and result["confidence"] == expected


def test_none_provider_long_list_is_not_confident():
    result = intake.parse_typed_text("material_list", "scan.txt", "Unrecognised body text\n" * 200)
    assert result["confidence"] == 0 and "error" not in result["fields"]


@pytest.mark.parametrize("name,doc_type,text,expected", [
    ("Sertifikat Halal.pdf", "halal_certificate", "SERTIFIKAT HALAL\nNo ID00110012345678\nBPJPH\nBerlaku 2027-01-01",
     {"cert_no": "ID00110012345678", "issuer": "BPJPH", "expiry_date": "2027-01-01"}),
    ("할랄인증서.pdf", "halal_certificate", "인증번호: HC-12345\n발급기관: KMF\n유효기간: 2026.01.01 ~ 2027.01.01",
     {"cert_no": "HC-12345", "issuer": "KMF", "expiry_date": "2027-01-01"}),
    ("Diagram Alir Proses.pdf", "process_flow", "Diagram alir\n1 mix\n2 bake", {"process_steps": ["mix", "bake"]}),
    ("공정흐름도.pdf", "process_flow", "원료입고 → 배합 → 가열 → 포장", {"process_steps": ["원료입고", "배합", "가열", "포장"]}),
    ("Manual SJPH.pdf", "sjph_manual", "Komitmen\nBahan\nProses\nProduk\nPemantauan",
     {"has_commitment": True, "has_materials": True, "has_process": True, "has_product": True, "has_monitoring": True}),
])
def test_filename_decision_still_extracts_fields_without_llm(monkeypatch, name, doc_type, text, expected):
    def forbidden(*a, **kw):
        pytest.fail("A deterministic parser must not call the LLM")
    monkeypatch.setattr(intake.ai_local, "llm_json", forbidden)
    for result in (intake.classify(name, text), intake.parse_typed_text(doc_type, name, text)):
        assert result["doc_type"] == doc_type and {k:v for k,v in result["fields"].items() if not k.startswith("_")} == expected


def test_filename_parsers_feed_existing_certificate_and_process_storage(case):
    _upload(case, "Sertifikat Halal.txt", b"SERTIFIKAT HALAL\nNo ID00110012345678\nBerlaku 2027-01-01",
            route="intake-zip")
    _upload(case, "Diagram Alir Proses.txt", b"Diagram alir\n1 mix\n2 bake", route="intake-zip")
    with m.SessionLocal() as db:
        certificate = db.query(models.HalalCertificate).filter_by(case_id=case[2]).one()
        assert certificate.certificate_no == "ID00110012345678"
        process = db.query(models.HpasEvaluation).filter_by(case_id=case[2], element="process").one()
        assert process.status == "ok" and "mix" in process.note and "bake" in process.note


def test_missing_parser_uses_existing_llm_path(monkeypatch):
    calls = []
    monkeypatch.setattr(intake.ai_local, "PROVIDER", "openai")
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda *a, **kw: calls.append(a) or {
        "doc_type": "origin_certificate", "fields": {"issuer": "Example issuer"}, "confidence": .9})
    assert intake.classify("Certificate of Origin.txt", "Country of origin Indonesia\nIssuer: Example issuer")["fields"]["issuer"] == "Example issuer"
    assert len(calls) == 1
    calls.clear()
    monkeypatch.setattr(intake.ai_local, "PROVIDER", "none")
    assert intake.classify("Certificate of Origin.txt", "Country of origin Indonesia")["fields"] == {}
    assert not calls


@pytest.mark.parametrize("entry,limit", [("typed", 2500), ("classify", 2000)])
def test_general_llm_input_window_is_marked(monkeypatch, entry, limit):
    calls = []
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda s, u: calls.append(u) or {
        "doc_type": "nib_business_license", "fields": {}, "confidence": .9})
    text = "filler line with business terms\n" * 150 + "NIB 9120001234567"
    result = (intake.parse_typed_text("nib_business_license", "NIB.txt", text) if entry == "typed"
              else intake.classify("NIB.txt", text))
    assert result["fields"]["_truncated"] == {"kind": "llm_input", "limit": limit, "total": len(text)}
    assert "9120001234567" not in calls[0]


# 감사 scripts/t_cls.py의 정답 35개를 그대로 고정한다(기존 정답 25개 포함).
@pytest.mark.parametrize("name,expected", [
    ("사업자등록증.pdf", "nib_business_license"), ("NIB PT Maju.pdf", "nib_business_license"),
    ("Nomor Induk Berusaha.pdf", "nib_business_license"), ("공장등록증명서.pdf", "factory_registration"),
    ("Izin Usaha Industri.pdf", "factory_registration"), ("제품목록.xlsx", "product_list"),
    ("Daftar Produk.xlsx", "product_list"), ("공정흐름도.pdf", "process_flow"),
    ("Diagram Alir Proses.pdf", "process_flow"), ("Sertifikat Halal Gelatin.pdf", "halal_certificate"),
    ("Halal Certificate - Supplier.pdf", "halal_certificate"), ("SKH supplier.pdf", "halal_certificate"),
    ("원산지증명서.pdf", "origin_certificate"), ("원재료목록.xlsx", "material_list"),
    ("Daftar Bahan.xlsx", "material_list"), ("Komposisi Bahan.xlsx", "material_list"),
    ("MSDS_glycerin.pdf", "coa_msds"), ("Lembar Data Keselamatan Bahan.pdf", "coa_msds"),
    ("CoA lot 23.pdf", "coa_msds"), ("시험성적서.pdf", "coa_msds"),
    ("Surat Pernyataan Bebas Babi.pdf", "supplier_declaration"), ("공급사 확인서.pdf", "supplier_declaration"),
    ("HACCP 인증서.pdf", "quality_cert"), ("ISO22000.pdf", "quality_cert"),
    ("Manual SJPH.pdf", "sjph_manual"), ("할랄 매뉴얼.pdf", "sjph_manual"),
    ("Denah Gudang.pdf", "warehouse_layout"), ("Daftar Kendaraan.xlsx", "vehicle_list"),
    ("SOP Pembersihan.pdf", "cleaning_sop"), ("Halal certificate HACCP combined.pdf", "halal_certificate"),
    ("할랄인증서_ISO_첨부.pdf", "halal_certificate"), ("NIB_halal_cert.pdf", "halal_certificate"),
    ("declaration_halal_certificate.pdf", "halal_certificate"), ("Product label.jpg", "other"),
    ("scan001.pdf", None),
])
def test_all_35_audit_filename_classifications(name, expected):
    assert intake.refine_doctype_reason(name, None)[0] == expected


@pytest.mark.parametrize("name,expected", [("SURAT KETERANGAN HALAL.pdf", "halal_certificate"),
    ("Lembar Data Keselamatan Bahan", "coa_msds"), ("Izin Usaha Industri", "factory_registration")])
def test_additional_audit_names_and_titles(name, expected):
    assert intake.refine_doctype_reason(name, None)[0] == expected
    assert intake.refine_doctype_reason("scan.txt", None, name)[0] == expected


def test_nfd_filename_is_classified_as_nfc():
    name = unicodedata.normalize("NFD", "원재료목록.xlsx")
    assert intake.refine_doctype_reason(name, None)[0] == "material_list"


@pytest.mark.parametrize("route", ["parse-file", "documents"])
def test_nfd_single_upload_filename_is_stored_as_nfc(case, route):
    expected = "시험성적서.txt"
    name = unicodedata.normalize("NFD", expected)
    assert name != expected
    _upload(case, name, b"Ethanol: 0.1%", route=route)
    with m.SessionLocal() as db:
        document = db.query(models.DocumentAsset).filter_by(case_id=case[2]).one()
        assert document.filename == expected


@pytest.mark.parametrize("collection,id_key,route,extra", [
    ("materials", "material_id", "evidence",
     {"evidence_type": "coa_msds", "counts_as_evidence": False}),
    ("products", "product_id", "photo", {}),
])
def test_nfd_linked_upload_filename_is_stored_as_nfc(case, collection, id_key, route, extra):
    client, headers, cid = case
    response = client.post(f"/cases/{cid}/{collection}", headers=headers, json={"name": "Sugar"})
    assert response.status_code == 200, response.text
    entity_id = response.json()[id_key]
    expected = "첨부자료.txt"
    response = client.post(f"/cases/{cid}/{collection}/{entity_id}/{route}", headers=headers,
                           json={"filename": unicodedata.normalize("NFD", expected),
                                 "file_b64": base64.b64encode(b"Attachment").decode(), **extra})
    assert response.status_code == 200, response.text
    with m.SessionLocal() as db:
        document = db.query(models.DocumentAsset).filter_by(case_id=cid).one()
        assert document.filename == expected


def test_nfd_sjph_upload_and_replacement_filenames_are_stored_as_nfc(case):
    client, headers, cid = case
    for expected in ("교육자료.txt", "교육자료_수정.txt"):
        response = client.post(f"/cases/{cid}/sjph-evidence", headers=headers,
                               json={"item_key": "training",
                                     "filename": unicodedata.normalize("NFD", expected),
                                     "file_b64": base64.b64encode(b"Training").decode()})
        assert response.status_code == 200, response.text
        with m.SessionLocal() as db:
            evidence = db.query(models.SjphEvidence).filter_by(case_id=cid, item_key="training").one()
            assert evidence.filename == expected
            assert db.get(models.DocumentAsset, evidence.document_id).filename == expected


def test_nfd_legacy_filename_is_stored_as_nfc_when_reprocessed(case):
    client, _, cid = case
    expected = "시험성적서.txt"
    _upload(case, unicodedata.normalize("NFD", expected), b"Ethanol: 0.1%", route="documents")
    with m.SessionLocal() as db:
        document = db.query(models.DocumentAsset).filter_by(case_id=cid).one()
        document.filename = unicodedata.normalize("NFD", expected)  # 정규화 전 저장된 문서 재현
        document_id = document.document_id
        db.commit()
    response = client.post("/auth/login", json={"username": "operator1", "password": "pw"})
    assert response.status_code == 200, response.text
    operator_headers = {"Authorization": "Bearer " + response.json()["token"]}
    response = client.post(f"/documents/{document_id}/reprocess", headers=operator_headers)
    assert response.status_code == 200, response.text
    with m.SessionLocal() as db:
        assert db.get(models.DocumentAsset, document_id).filename == expected


def test_nfd_legacy_filename_is_stored_as_nfc_when_autofiled(case):
    client, headers, cid = case
    expected = "내부심사.txt"
    with m.SessionLocal() as db:
        document = models.DocumentAsset(case_id=cid, filename=unicodedata.normalize("NFD", expected),
                                        doc_type="sjph_manual", text_excerpt="Internal Audit")
        db.add(document)
        db.commit()
    response = client.post(f"/cases/{cid}/sjph-evidence/autofile", headers=headers)
    assert response.status_code == 200, response.text
    with m.SessionLocal() as db:
        evidence = db.query(models.SjphEvidence).filter_by(case_id=cid).one()
        assert evidence.filename == expected


def test_nfd_feedback_image_filename_is_stored_as_nfc(case):
    client, headers, _ = case
    response = client.post("/feedback", headers=headers, json={"title": "NFC filename"})
    assert response.status_code == 200, response.text
    feedback_id = response.json()["feedback_id"]
    expected = "화면.png"
    response = client.post(f"/feedback/{feedback_id}/images", headers=headers,
                           json={"filename": unicodedata.normalize("NFD", expected),
                                 "file_b64": base64.b64encode(b"Image").decode()})
    assert response.status_code == 200, response.text
    with m.SessionLocal() as db:
        image = db.query(models.FeedbackImage).filter_by(feedback_id=feedback_id).one()
        assert image.filename == expected
