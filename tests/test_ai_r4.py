"""R4: audit5 독립 경계 119개 및 저장/업로드/안내 회귀.

scripts/t_boundary4.py의 quant 36 + screen 42 + classification 16 + identity 14
+ certificate_fields 6 + problem_verify 5 입력/기대값을 그대로 고정했다.
감사 폴더는 읽기 전용이며 이 테스트 실행에는 필요하지 않다.
"""
import base64
import io
import json
import unicodedata
import zipfile

import fitz
import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

import app.main as m
from app import intake, models, screening


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(m, "_RL_WRITE_HITS", {})
    monkeypatch.setattr(m, "_AUTOPARSE", False)
    monkeypatch.setattr(m, "autoparse_enqueue", lambda *a, **k: False)
    monkeypatch.setattr(m, "verify_enqueue", lambda *a, **k: False)
    monkeypatch.setattr(intake, "_ZIP_LOOKUP", False)
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda *a, **k: {})
    monkeypatch.setattr(intake.ai_local, "llm_text", lambda *a, **k: "")


@pytest.fixture
def case():
    with m.SessionLocal() as db:
        c = models.CaseApplication(org_id="org_demo", company_name="PT R4",
                                   email="r4@example.test", notify_consent=True)
        db.add(c)
        db.commit()
        yield db, c


def save_document(case, text, dtype="coa_msds", filename="CoA.txt", raw=None):
    db, c = case
    parsed = (intake.parse_typed(dtype, filename, raw) if raw is not None else
              intake.parse_typed_text(dtype, filename, text))
    doc = models.DocumentAsset(case_id=c.case_id, filename=filename, doc_type=parsed["doc_type"],
                               fields=parsed["fields"], text_full=parsed.get("text", text),
                               confidence=parsed.get("confidence", .9))
    db.add(doc)
    db.flush()
    m._apply_document_measurements(db, c, doc)
    db.commit()
    db.expire_all()
    return doc, db.query(models.MaterialMeasurement).filter_by(document_id=doc.document_id).all()


QUANT = [
    ('two_upper_bounds_result', 'Ethanol Result: ≤5%; Ethanol Result: 0.1%', 'review'),
    ('two_upper_ascii_result', 'Ethanol Result: <=5%; Ethanol Result: 0.1%', 'review'),
    ('two_upper_lt_result', 'Ethanol Result: <5%; Ethanol Result: 0.1%', 'review'),
    ('two_upper_bounds_reverse', 'Ethanol Result: 0.1%; Ethanol Result: ≤5%', 'review'),
    ('same_line_nd_second', 'Ethanol: 0.1%; Ethanol: ND', 'review'),
    ('same_line_nd_first', 'Ethanol: ND; Ethanol: 0.1%', 'review'),
    ('same_line_gt_second', 'Ethanol: 0.1%; Ethanol: >5%', 'fail'),
    ('same_line_gt_first', 'Ethanol: >5%; Ethanol: 0.1%', 'fail'),
    ('split_line_le_results', 'Ethanol Result: ≤5%\nEthanol Result: 0.1%', 'review'),
    ('result_header_unit', 'Parameter | Result (%) | Specification (%)\nEthanol | 3.2 | 0.5', 'fail'),
    ('unit_column', 'Parameter | Specification | Result | Unit\nEthanol | 0.5 | 3.2 | %', 'fail'),
    ('table_misaligned_high', 'Parameter | Specification | Result\nEthanol | 0.5% | 3.2% | retest', 'review'),
    ('two_analytes_one_row', 'Ethanol: 0.1% | Lead: 12 ppm', 'fail'),
    ('free_from_table', 'Parameter | Specification | Result\nFree from Lead | 2ppm | 12ppm', 'unknown'),
    ('free_from_line', 'Free from Lead: 12ppm', 'unknown'),
    ('ppm_high', 'Ethanol: 10000 ppm', 'fail'),
    ('ppm_threshold', 'Ethanol: 5000 ppm', 'pass'),
    ('lead_percent', 'Lead: 0.003 %', 'fail'),
    ('lead_ug_kg', 'Lead: 3000 ug/kg', 'fail'),
    ('nd_without_lod', 'Ethanol: ND', 'review'),
    ('nd_high_loq', 'Ethanol: ND (LOQ 5%)', 'review'),
    ('unicode_le_boundary', 'Ethanol: ≤0.5%', 'pass'),
    ('unicode_ge_boundary', 'Ethanol: ≥0.5%', 'review'),
    ('gt_boundary', 'Ethanol: >0.5%', 'fail'),
    ('lt_high', 'Ethanol: <5%', 'review'),
    ('legacy_dot_cas', 'Ethanol (CAS 64-17-5) .... 40%', 'fail'),
]
QUANT += [('dna_pending_' + str(i), 'Porcine DNA PCR: Not detected\nPorcine DNA PCR: ' + word, 'review')
          for i, word in enumerate(['Pending', 'Indeterminate', 'Not tested', 'Invalid', 'Inconclusive', '시험중', '판정 보류'])]
QUANT += [
    ('dna_not_negative', 'Porcine DNA PCR: Not negative', 'review'),
    ('dna_control_positive', 'Porcine DNA PCR: Negative control\nSample result: Detected', 'fail'),
    ('dna_control_negative', 'Porcine DNA PCR: Positive control\nSample result: Not detected', 'pass'),
]

SCREEN = [(word, True) for word in ['Lard','Pork','L a r d','L.A.R.D','P.O.R.K']]
SCREEN += [(separator.join(word), True) for word in ['LARD','PORK'] for separator in [':',',',';','!','?','/','·']]
SCREEN += [
    ('Not free from pork', True), ('Not without lard', True), ('Does not contain pork', False),
    ('Free from pork and lard', False), ('Without pork and no lard', False),
    ('No pork or contains lard', True), ('No pork (contains lard)', True),
    ('Pork-free; contains lard', True), ('Does not contain pork but contains lard', True),
    ('Not entirely pork-free', True), ('Not completely free from pork', True),
    ('Not free from pork and lard', True), ('Never without pork', True),
    ('tidak mengandung babi', False), ('tidak bebas dari babi', True),
    ('돼지고기가 없음', False), ('돼지고기를 함유하지 않음', False),
    ('라드가 없지 않은 쇼트닝', True), ('No pork, contains lard', True),
    ('Collard greens', False), ('L.A.R.D-free shortening', False),
    ('Rum-free flavour', False), ('Free from pork\nContains lard', True),
]

CLASSES = [
    ('application_form', 'Halal certification application form.pdf', 'APPLICATION FORM\nRequest for halal certification.', 'other'),
    ('normal_halal', 'Halal certificate.pdf', 'HALAL CERTIFICATE\nCertificate No: HC123\nIssuer: BPJPH\nValid until: 2027-01-01', 'halal_certificate'),
    ('halal_application_ref', 'Halal certificate.pdf', 'HALAL CERTIFICATE\nApplication No: APP123\nCertificate No: HC123\nIssuer: BPJPH\nValid until: 2027-01-01', 'halal_certificate'),
    ('quality_application_ref', 'HACCP.pdf', 'HACCP CERTIFICATE\nApplication reference: APP123\nCertificate No: Q123\nValid until: 2027-01-01', 'quality_cert'),
    ('nib_application_ref', 'NIB.pdf', 'NOMOR INDUK BERUSAHA\nApplication No: A123\nNIB: 1234567890123', 'nib_business_license'),
    ('coa_application_context', 'CoA.pdf', 'CERTIFICATE OF ANALYSIS\nApplication: food use\nEthanol: 3.2%', 'coa_msds'),
    ('materials_application_context', 'Daftar Bahan.xlsx', 'DAFTAR BAHAN\nApplication: confectionery\nWater\nSalt', 'material_list'),
    ('proposal', 'Proposal for halal certification.pdf', 'PROPOSAL FOR HALAL CERTIFICATION', 'other'),
    ('requirements_reversed', 'Requirements for halal certificate.pdf', 'DOCUMENT REQUIREMENTS\nList of required documents.', 'other'),
    ('sample_certificate', 'Halal certificate sample.pdf', 'SAMPLE HALAL CERTIFICATE\nNot valid for certification.', 'other'),
    ('draft_certificate', 'Draft Halal Certificate.pdf', 'DRAFT HALAL CERTIFICATE\nFor review only. Not issued.', 'other'),
    ('certificate_request_hyphen', 'Halal certificate request-form.pdf', 'REQUEST-FORM\nPlease fill in your information.', 'other'),
    ('normal_quality', 'HACCP.pdf', 'HACCP CERTIFICATE\nCertificate No: Q123\nValid until: 2027-01-01', 'quality_cert'),
    ('normal_nib', 'NIB.pdf', 'NOMOR INDUK BERUSAHA\nNIB: 1234567890123', 'nib_business_license'),
    ('normal_coa', 'CoA.pdf', 'CERTIFICATE OF ANALYSIS\nEthanol: 3.2%', 'coa_msds'),
    ('normal_material', 'Daftar Bahan.xlsx', 'DAFTAR BAHAN\nWater\nSalt', 'material_list'),
]

PAIRS = [
    ('hazard_qualifier', 'Water', 'Water (pork extract)', False),
    ('animal_origin', 'Gelatin (bovine)', 'Gelatin (porcine)', False),
    ('country_qualifier', 'Water (국내산)', 'Water (중국산)', False),
    ('country_vs_none', 'Water', 'Water (국내산)', False),
    ('width', 'Water（pork extract）', 'Water (pork extract)', True),
    ('nfc_nfd', '젤라틴', unicodedata.normalize('NFD','젤라틴'), True),
    ('nested_origin', 'Water (A(국내산))', 'Water (A(중국산))', False),
    ('unclosed_origin', 'Water (국내산', 'Water (중국산', False),
    ('outside_origin_suffix', 'Water 국내산', 'Water 중국산', True),
    ('component_list', 'Blend (AB,C)', 'Blend (A,BC)', False),
    ('numeric_range', 'Blend (1-2)', 'Blend (12)', False),
    ('square_country', 'Water [국내산]', 'Water [중국산]', False),
    ('brace_country', 'Water {국내산}', 'Water {중국산}', False),
    ('stereoisomer', 'L-lysine', 'D-lysine', False),
]

CERTS = [
    ('normal', 'HALAL CERTIFICATE\nCertificate No: HC123\nIssuer: BPJPH\nValid until: 2027-01-01', 'HC123'),
    ('telephone_after', 'HALAL CERTIFICATE\nCertificate No: HC123\nTelephone No: 021555123\nIssuer: BPJPH\nValid until: 2027-01-01', 'HC123'),
    ('registration_after', 'HALAL CERTIFICATE\nCertificate No: HC123\nRegistration No: NIB456\nIssuer: BPJPH\nValid until: 2027-01-01', 'HC123'),
    ('lot_after', 'HALAL CERTIFICATE\nCertificate No: HC123\nLot No: LOT789\nIssuer: BPJPH\nValid until: 2027-01-01', 'HC123'),
    ('page_after', 'HALAL CERTIFICATE\nCertificate No: HC123\nPage No: 2\nIssuer: BPJPH\nValid until: 2027-01-01', 'HC123'),
    ('inline_tel', 'HALAL CERTIFICATE Certificate No: HC123 Telephone No: 021555123 Issuer: BPJPH Valid until: 2027-01-01', 'HC123'),
]

VERIFY = [
    ('invalid_scalar', {'verdict': 'ok', 'field_issues': 7}, True),
    ('false_string', {'verdict': 'ok', 'doc_type_ok': 'false'}, True),
    ('false_boolean', {'verdict': 'ok', 'doc_type_ok': False, 'field_issues': []}, True),
    ('field_warning', {'verdict': 'ok', 'field_issues': {'cert_no': 'wrong number'}}, True),
    ('valid_ok', {'verdict': 'ok', 'doc_type_ok': True, 'field_issues': []}, False),
]
assert sum(map(len, (QUANT, SCREEN, CLASSES, PAIRS, CERTS, VERIFY))) == 119


@pytest.mark.parametrize("name,text,expected", QUANT, ids=[c[0] for c in QUANT])
def test_audit_quant_36(case, name, text, expected):
    db, c = case
    doc, rows = save_document(case, text)
    assert m._case_quant_verdict(db, c.case_id) == expected
    assert bool(m._quant_blockers(db, c.case_id)) == (expected in ("review", "fail"))
    if expected == "review":
        assert {r["code"] for r in m._problem_document_reasons(doc, rows)} & {"QUANT_REVIEW", "PORK_DNA_UNCLEAR"}


@pytest.mark.parametrize("name,blocked", SCREEN, ids=[c[0] for c in SCREEN])
def test_audit_screen_42(name, blocked):
    assert (screening.screen_merged(name)["result"] == "BLOCK") is blocked
    assert (screening.screen_raw(name)["result"] == "BLOCK") is blocked
    assert bool(screening.screen_text(name)["haram_hits"]) is blocked


@pytest.mark.parametrize("name,filename,text,expected", CLASSES, ids=[c[0] for c in CLASSES])
def test_audit_classification_16(name, filename, text, expected):
    assert intake.refine_doctype_reason(filename, None, text)[0] == expected


@pytest.mark.parametrize("name,left,right,equal", PAIRS, ids=[c[0] for c in PAIRS])
def test_audit_identity_14(name, left, right, equal):
    keys = [models.normalize_material_name(x) for x in (left, right)]
    assert (keys[0] == keys[1]) is equal
    assert keys == [m._norm_material(x) for x in (left, right)]


@pytest.mark.parametrize("name,text,expected", CERTS, ids=[c[0] for c in CERTS])
def test_audit_certificate_6(name, text, expected):
    parsed = intake.parse_typed_text("halal_certificate", "Halal Certificate.txt", text)
    assert parsed["fields"].get("cert_no") == expected


@pytest.mark.parametrize("name,response,problem", VERIFY, ids=[c[0] for c in VERIFY])
def test_audit_verify_5(case, monkeypatch, name, response, problem):
    db, c = case
    doc, _ = save_document(case, CERTS[0][1], "halal_certificate", "Halal Certificate.txt")
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda *a, **k: response)
    m._verify_doc(db, doc, c)
    result = m._problem_documents_result(db, c)
    assert (result["count"] > 0) is problem
    if problem:
        codes = {r["code"] for d in result["documents"] for r in d["reasons"]}
        assert ("VERIFY_ERROR" if name == "invalid_scalar" else "VERIFY_WARN") in codes
        assert result["recipient_type"] != "applicant"


@pytest.mark.parametrize("expression,expected", [
    ("<=5%", "review"), ("≤5%", "review"), ("<5%", "review"),
    ("ND", "review"), ("ND (LOD 5%)", "review"), (">5%", "fail"),
])
@pytest.mark.parametrize("reverse", [False, True])
def test_r401_observations_survive_reprocessing(case, expression, expected, reverse):
    db, c = case
    results = [expression, "0.1%"]
    if reverse:
        results.reverse()
    doc, rows = save_document(case, "; ".join("Ethanol Result: " + x for x in results))
    assert len(rows) == 2
    metadata = doc.fields["measurements_meta"]["ethanol_pct"]["observations"]
    assert len(metadata) == 2
    assert sorted(x["occurrence"] for x in map(lambda r: json.loads(r.note), rows)) == [0, 1]
    ids = {r.measurement_id for r in rows}
    assert m._apply_document_measurements(db, c, doc) == 2
    db.commit()
    assert {r.measurement_id for r in db.query(models.MaterialMeasurement).filter_by(document_id=doc.document_id)} == ids
    assert m._case_quant_verdict(db, c.case_id) == expected
    assert all(r.document_id == doc.document_id for r in rows)


@pytest.mark.parametrize("text,count,expected", [
    ("Ethanol Result: <=5%; Result: 0.1%", 2, "review"),
    ("Ethanol Result: 0.1%; Result: ND", 2, "review"),
    ("Ethanol Result: ND (LOD 5%); Result: 0.1%", 2, "review"),
    ("Ethanol Result: 3.2%; Result: <=5%", 2, "fail"),
    ("Ethanol: 0.1%; Ethanol: 0.1%", 2, "pass"),
    ("Lead (Pb): 12 ppm", 1, "fail"),
])
def test_r401_result_labels_and_aliases(case, text, count, expected):
    db, c = case
    _, rows = save_document(case, text)
    assert len(rows) == count
    assert m._case_quant_verdict(db, c.case_id) == expected


def xlsx_bytes(rows):
    wb = Workbook()
    for row in rows:
        wb.active.append(row)
    out = io.BytesIO()
    wb.save(out)
    wb.close()
    return out.getvalue()


XLSX_RESULTS = [
    (["Parameter", "Result (%)", "Specification (%)"], ["Ethanol", 3.2, .5], 3.2, "fail"),
    (["Parameter", "Specification", "Result", "Unit"], ["Ethanol", .5, 3.2, "%"], 3.2, "fail"),
    (["Parameter", "Result (ppm)"], ["Ethanol", 5000], .5, "pass"),
    (["Parameter", "Result [ppm]"], ["Ethanol", 10000], 1, "fail"),
    (["Parameter", "Result %"], ["Ethanol", 3.2], 3.2, "fail"),
    (["Parameter", "Hasil", "Satuan"], ["Lead", 3000, "ug/kg"], 3, "fail"),
    (["Parameter", "Result (μg/kg)"], ["Lead", 3000], 3, "fail"),
    (["항목", "결과", "단위"], ["납", .003, "%"], 30, "fail"),
    (["Parameter", "Result (%)"], ["Ethanol", "Pending"], None, "review"),
    (["Parameter", "Specification", "Result (%)"], ["Ethanol", .5, None], None, "review"),
    (["Parameter", "Result (%)"], ["Ethanol", "not tested"], None, "review"),
    (["Parameter", "Result (%)"], ["Ethanol", "<0.1 bananas"], None, "review"),
    (["Parameter", "Result (%)"], ["Ethanol", "0.1% - 3.2%"], None, "review"),
    (["Parameter", "Result (%)"], ["Ethanol", "ND (LOD 0.01%)"], .01, "pass"),
    (["Parameter", "Result", "Unit"], ["Lead", 3.2, "mg/L"], None, "review"),
    (["Parameter", "Result (%)", "Unit"], ["Ethanol", 3.2, "ppm"], None, "review"),
    (["Parameter", "Specification", "Result"], ["Ethanol", ".5%", "3.2%", "retest"], None, "review"),
]


@pytest.mark.parametrize("header,data,value,expected", XLSX_RESULTS)
def test_r402_real_xlsx_units_and_unparsed_results(case, header, data, value, expected):
    doc, rows = save_document(case, "", filename="CoA.xlsx", raw=xlsx_bytes([header, data]))
    assert len(rows) == 1
    if value is None:
        assert rows[0].value is None
    else:
        assert rows[0].value == pytest.approx(value)
    assert rows[0].verdict == m._measurement_verdict(rows[0]) == expected
    assert json.loads(rows[0].note)["measurement_meta"]["line"] == 2
    if expected == "review":
        assert "QUANT_REVIEW" in {r["code"] for r in m._problem_document_reasons(doc, rows)}


@pytest.mark.parametrize("status", ["Pending", "Not tested", "To follow", "Menunggu", "미실시", "진행 중", "", "Awaiting analysis"])
@pytest.mark.parametrize("layout", ["separate", "result", "sample", "table"])
def test_r403_all_unconfirmed_blocks_require_review(case, status, layout):
    if layout == "separate":
        text = "Porcine DNA PCR: Not detected\nPorcine DNA PCR: " + status
    elif layout == "result":
        text = "Porcine DNA PCR: Not detected\nResult: " + status
    elif layout == "sample":
        text = "Porcine DNA PCR: Not detected\nSample result: " + status
    else:
        text = "Test | Control | Sample\nPorcine DNA PCR | Positive | Not detected\nPorcine DNA PCR | Positive | " + status
    doc, rows = save_document(case, text)
    assert [(r.param_key, r.value, r.verdict) for r in rows] == [("pork_dna", None, "review")]
    assert "PORK_DNA_UNCLEAR" in {r["code"] for r in m._problem_document_reasons(doc, rows)}


@pytest.mark.parametrize("text,expected", [
    ("Porcine DNA PCR: Negative; Porcine DNA PCR: Pending", "review"),
    ("Porcine DNA PCR: Negative\nSalmonella: Pending", "pass"),
    ("Test | Control | Sample\nPorcine DNA PCR | Positive | Negative\nPorcine DNA PCR | Positive | Negative", "pass"),
    ("Porcine DNA PCR: Negative control\nSample result: Detected", "fail"),
    ("Porcine DNA (PCR) | Result\nDetected", "fail"),
    ("Porcine DNA Test Report\nSample: Gelatin\nPorcine DNA: Detected", "fail"),
    ("Porcine DNA Test Report\nSample: Gelatin\nPorcine DNA:", "review"),
    ("Porcine DNA (PCR) | Result", "review"),
])
def test_r403_result_scope_and_controls(case, text, expected):
    db, c = case
    save_document(case, text)
    assert m._case_quant_verdict(db, c.case_id) == expected


@pytest.fixture
def http_case():
    with TestClient(m.app) as client:
        response = client.post("/auth/login", json={"username": "applicant1", "password": "pw"})
        assert response.status_code == 200, response.text
        headers = {"Authorization": "Bearer " + response.json()["token"]}
        response = client.post("/cases", headers=headers, json={"company_name": "PT R4 HTTP"})
        assert response.status_code == 200, response.text
        yield client, headers, response.json()["case_id"]


def upload(http_case, filename, raw, dtype, route):
    client, headers, cid = http_case
    if route == "intake-zip":
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            archive.writestr(filename, raw)
        payload = {"zip_b64": base64.b64encode(out.getvalue()).decode()}
    else:
        payload = {"filename": filename, "doc_type": dtype, "file_b64": base64.b64encode(raw).decode()}
    response = client.post(f"/cases/{cid}/{route}", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    return client.get(f"/cases/{cid}/documents", headers=headers).json()


@pytest.mark.parametrize("route", ["parse-file", "intake-zip"])
@pytest.mark.parametrize("name,filename,text,expected", CLASSES, ids=[c[0] for c in CLASSES])
def test_r404_classification_through_upload(http_case, route, name, filename, text, expected):
    client, headers, cid = http_case
    docs = upload(http_case, filename.rsplit(".", 1)[0] + ".txt", text.encode(),
                  expected if expected != "other" else "halal_certificate", route)
    assert [d["doc_type"] for d in docs] == [expected]
    if expected == "coa_msds":
        report = client.get(f"/cases/{cid}/material-report", headers=headers).json()
        assert report["quant_verdict"] == "fail"
    if expected == "other":
        checklist = client.get(f"/cases/{cid}/doc-checklist", headers=headers).json()["checklist"]
        assert not next(x for x in checklist if x["doc_type"] == "halal_certificate")["satisfied"]


@pytest.mark.parametrize("label", ["Phone", "Telephone No", "Tel", "Telp", "Fax", "NIB", "Reg. No", "Application No", "Lot No"])
@pytest.mark.parametrize("reverse", [False, True])
def test_r405_number_label_roles(case, label, reverse):
    lines = ["Certificate No:HC123", label + ":021555123"]
    if reverse:
        lines.reverse()
    doc, _ = save_document(case, "HALAL CERTIFICATE\n" + "\n".join(lines) +
                           "\nIssuer: BPJPH\nValid until: 2027-01-01", "halal_certificate", "Halal Certificate.txt")
    assert doc.fields["cert_no"] == "HC123"


@pytest.mark.parametrize("label", ["Certificate No", "Certificate number", "Sertifikat No", "Nomor Sertifikat", "No. Sertifikat", "인증번호"])
def test_r405_explicit_certificate_label_beats_bare_number(label):
    text = "HALAL CERTIFICATE\nNo: OLD999\n" + label + ": HC123\nNo: NEW999"
    assert intake.parse_typed_text("halal_certificate", "Halal Certificate.txt", text)["fields"]["cert_no"] == "HC123"


@pytest.mark.parametrize("number", ["021555123", "010-1234-5678", "+62 21 555 1234", "(021) 5551234", "02 555 1234"])
def test_r405_phone_shaped_certificate_is_missing(number):
    text = "HALAL CERTIFICATE\nTelephone No: " + number + "\nIssuer: BPJPH\nValid until: 2027-01-01"
    parsed = intake.parse_typed_text("halal_certificate", "Halal Certificate.txt", text)
    assert not parsed["fields"].get("cert_no")
    assert "cert_no" in parsed["fields"]["_missing_fields"]


@pytest.mark.parametrize("route", ["parse-file", "intake-zip"])
def test_r405_real_pdf_preserves_canonical_number(http_case, route):
    with fitz.open() as pdf:
        pdf.new_page().insert_text((50, 60), CERTS[1][1])
        raw = pdf.tobytes()
    docs = upload(http_case, "Halal Certificate.pdf", raw, "halal_certificate", route)
    assert docs[0]["fields"]["cert_no"] == "HC123"
    if route == "intake-zip":  # ZIP 집계가 인증서 후보도 반영한다. 단건 파싱은 DocumentAsset에 저장한다.
        with m.SessionLocal() as db:
            certs = db.query(models.HalalCertificate).filter_by(case_id=http_case[2]).all()
            assert [cert.certificate_no for cert in certs] == ["HC123"]


@pytest.mark.parametrize("name", ["A, B, C", "Vitamin A, B, C", "P, O, rice, K", "L, A, rice, D",
                                 "C.O.L.L.A.R.D greens", "A,L,A,R,D", "P,O,R,K,S", "P,O,R\nK", "L:A:R:D-free",
                                 "P.O.R.K.S", "P/O/R/K/S", "P-O-R-K-S"])
def test_r406_spelling_false_positive_controls(name):
    assert screening.screen_merged(name)["result"] != "BLOCK"
    assert not screening.screen_text(name)["haram_hits"]


@pytest.mark.parametrize("separator", [" ", ".", ",", ":", ";", "!", "?", "-", "/", "·"])
@pytest.mark.parametrize("word", ["LARD", "PORK"])
def test_r406_all_separators_preserve_word_length(separator, word):
    assert screening.screen_merged(separator.join(word))["result"] == "BLOCK"


@pytest.mark.parametrize("name,left,right,equal", PAIRS, ids=[c[0] for c in PAIRS])
def test_r407_identity_through_http(http_case, name, left, right, equal):
    client, headers, cid = http_case
    for index, text in enumerate((left, right)):
        response = client.post(f"/cases/{cid}/materials", headers=headers, json={"name": text})
        assert response.status_code == (409 if index and equal else 200), response.text
        if response.status_code == 409:
            assert response.json()["detail"]["code"] == "DUPLICATE_MATERIAL"
    with m.SessionLocal() as db:
        rows = db.query(models.Material).filter_by(case_id=cid).all()
        assert len(rows) == (1 if equal else 2)


@pytest.mark.parametrize("left,right", [("Blend(AB,C)", "Blend(A,BC)"), ("Blend(AB;C)", "Blend(A;BC)"),
                                      ("Blend(AB/C)", "Blend(A/BC)"), ("Water[A]", "Water(A)"),
                                      ("Water{A}", "Water[A]")])
def test_r407_delimiters_and_bracket_kinds_are_identity(left, right):
    assert models.normalize_material_name(left) != models.normalize_material_name(right)


@pytest.mark.parametrize("field", ["cert_no", "nib", "company_name", "factory_reg_no"])
@pytest.mark.parametrize("lang", ["ko", "en", "id"])
def test_r408_important_warning_is_staff_only(case, monkeypatch, field, lang):
    db, c = case
    doc, _ = save_document(case, CERTS[0][1], "halal_certificate", "Halal Certificate.txt")
    doc.fields = {**doc.fields, "_verify": {"verdict": "warn", "field_issues": [{"field": field, "issue": "mismatch"}]}}
    db.commit()
    result = m._problem_documents_result(db, c, lang)
    assert result["count"] == 1
    reasons = result["documents"][0]["reasons"]
    assert [r["code"] for r in reasons] == ["VERIFY_WARN"]
    assert reasons[0]["explanation"] and "{" not in reasons[0]["explanation"]
    monkeypatch.setenv("GLHAC_PROBLEM_DOC_NOTIFY_DELAY_S", "0")
    assert m._notify_problem_documents(db, c)["notified"]
    notice = db.query(models.Notification).filter_by(case_id=c.case_id, event_type="problem.documents").one()
    assert notice.role in ("auditor", "operator")
    assert notice.payload["params"]["staff_only"] is True


@pytest.mark.parametrize("lang", ["ko", "en", "id"])
def test_r409_review_is_document_scoped_and_localized(case, lang):
    doc, rows = save_document(case, "Ethanol: <5%")
    reasons = m._problem_document_reasons(doc, rows)
    reason = next(r for r in reasons if r["code"] == "QUANT_REVIEW")
    assert len(m.PROBLEM_DOC_MSG["QUANT_REVIEW"][lang]) == 3
    explanation = m._problem_document_explanation(reason, lang)
    assert explanation and "{" not in explanation
    other, _ = save_document(case, "Ethanol: 0.1%")
    assert "QUANT_REVIEW" not in {r["code"] for r in m._problem_document_reasons(other, rows)}


@pytest.mark.parametrize("route", ["parse-file", "intake-zip"])
@pytest.mark.parametrize("kind,expected,count", [("repeated", "review", 2), ("header", "fail", 1), ("blank", "review", 1)])
def test_r401_r402_upload_report_and_review_queue(http_case, route, kind, expected, count):
    if kind == "repeated":
        filename, raw = "CoA.txt", b"Ethanol Result: <=5%; Ethanol Result: 0.1%"
    else:
        filename = "CoA.xlsx"
        raw = xlsx_bytes([["Parameter", "Result (%)"], ["Ethanol", 3.2 if kind == "header" else None]])
    docs = upload(http_case, filename, raw, "coa_msds", route)
    client, headers, cid = http_case
    report = client.get(f"/cases/{cid}/material-report", headers=headers).json()
    measurements = client.get(f"/cases/{cid}/measurements", headers=headers).json()
    assert report["quant_verdict"] == measurements["verdict"] == expected
    assert len(measurements["items"]) == count
    assert {x["document_id"] for x in measurements["items"]} == {docs[0]["document_id"]}
    if expected == "review":
        staff = client.post("/auth/login", json={"username":"operator1", "password":"pw"}).json()
        problems = client.get(f"/cases/{cid}/problem-documents", headers={"Authorization":"Bearer " + staff["token"]}).json()
        assert "QUANT_REVIEW" in {r["code"] for d in problems["documents"] for r in d["reasons"]}


@pytest.mark.parametrize("extra,role", [(None, "operator"), ("VERIFY_ERROR", "operator"), ("CORRUPT", "applicant")])
def test_r408_mixed_reasons_follow_existing_recipient_rules(case, extra, role):
    db, c = case
    doc, _ = save_document(case, CERTS[0][1], "halal_certificate", "Halal Certificate.txt")
    verification = {"verdict": "warn", "doc_type_ok": False, "field_issues": []}
    if extra == "VERIFY_ERROR":
        verification["llm_error"] = "BAD_RESPONSE"
    doc.fields = {**doc.fields, "_verify": verification, **({"_ocr_error": "CORRUPT"} if extra == "CORRUPT" else {})}
    db.commit()
    assert m._problem_documents_result(db, c)["recipient_type"] == role


def test_r408_noncritical_warning_and_existing_ownership_path(case):
    db, c = case
    doc, _ = save_document(case, CERTS[0][1], "halal_certificate", "Halal Certificate.txt")
    doc.fields = {**doc.fields, "_verify": {"verdict": "warn", "field_issues": [{"field": "phone", "issue": "format"}]}}
    assert m._problem_document_reasons(doc) == []
    doc.fields = {**doc.fields, "_verify": {"verdict": "warn", "field_issues": [
        {"field": "company_name", "code": "company_name_mismatch", "issue": "different owner"}]}}
    db.commit()
    result = m._problem_documents_result(db, c)
    assert [r["code"] for r in result["documents"][0]["reasons"]] == ["OWNERSHIP_MISMATCH"]
    assert result["recipient_type"] == "operator"


@pytest.mark.parametrize("note", [None, "legacy imported value"])
@pytest.mark.parametrize("qualifier", ["<", "ND"])
def test_r409_legacy_pass_is_reviewed_from_supplied_document(note, qualifier):
    doc = models.DocumentAsset(document_id="r4_legacy", doc_type="coa_msds", confidence=.9,
                               text_full="Ethanol: <5%", fields={"measurements_meta": {
                                   "ethanol_pct": {"qualifier": qualifier, "lod": 5}}})
    row = models.MaterialMeasurement(document_id=doc.document_id, param_key="ethanol_pct", value=0,
                                     verdict="pass", recorded_by="ai", note=note)
    # DB 세션이 없는 객체만으로도 보고서와 동일한 구간 판정·안내를 제공한다.
    assert m._measurement_verdict(row, document=doc) == "review"
    assert [r["code"] for r in m._problem_document_reasons(doc, [row])] == ["QUANT_REVIEW"]
    assert row.verdict == "pass" and row.value == 0
