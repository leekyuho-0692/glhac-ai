"""e3: R5-01~10 / C17. Audit-5's 170 independent boundaries, unchanged.

The previous 119 boundaries remain in test_ai_r4.py. This file contains the
original inputs/oracles locally; it never imports or writes to the audit folder.
"""
import base64
import io
import json
import os
import zipfile
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import fitz
import pytest
from fastapi.testclient import TestClient
from PIL import Image

os.environ.setdefault("GLHAC_LLM_PROVIDER", "none")
os.environ.setdefault("GLHAC_ZIP_LOOKUP", "0")
os.environ.setdefault("GLHAC_NOTIFY_WORKER", "0")
from app import intake, models, screening, cert_verify
from app import main as m
from test_final_e2_rules import seed_reviewed_issue_prerequisites, seed_paid_receipt

OK = {"verdict": "ok", "doc_type_ok": True, "field_issues": []}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(m, "_RL_WRITE_HITS", {})
    monkeypatch.setattr(m, "_AUTOPARSE", False)
    monkeypatch.setattr(m, "autoparse_enqueue", lambda *a, **k: False)
    monkeypatch.setattr(m, "verify_enqueue", lambda *a, **k: False)
    monkeypatch.setattr(intake, "_ZIP_LOOKUP", False)
    monkeypatch.setenv("GLHAC_RATE_LIMIT_PER_MIN", "0")
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda system, *a, **k:
                        dict(OK) if "접수 검수자" in system else {})
    monkeypatch.setattr(intake.ai_local, "llm_text", lambda *a, **k: "")
    monkeypatch.setattr(intake.ai_local, "ocr_image", lambda *a, **k:
                        {"ok": False, "error": "WORKER_TIMEOUT"})


@pytest.fixture(scope="module")
def client():
    with TestClient(m.app) as client:
        yield client


@pytest.fixture(scope="module")
def headers(client):
    result = {}
    for role, username, password in (("applicant", "applicant1", "pw"),
                                     ("operator", "operator1", "pw"),
                                     ("auditor", "auditor1", "pw"),
                                     ("admin", "admin", "admin")):
        response = client.post("/auth/login", json={"username": username, "password": password})
        assert response.status_code == 200, response.text
        result[role] = {"Authorization": "Bearer " + response.json()["token"]}
    return result


def new_case(client, headers):
    response = client.post("/cases", headers=headers["applicant"],
                           json={"company_name": "PT E3"})
    assert response.status_code == 200, response.text
    return response.json()["case_id"]


def upload(client, headers, cid, text, dtype="coa_msds", filename="CoA.txt", endpoint="parse-file"):
    raw = text.encode() if isinstance(text, str) else text
    return client.post(f"/cases/{cid}/{endpoint}", headers=headers["applicant"], json={
        "filename": filename, "doc_type": dtype, "file_b64": base64.b64encode(raw).decode()})


# Each tuple is a distinct input, not repeated randomized execution.
QUANT = [
 ('lead_ppb_high','Lead Result: 3000 ppb','fail'),
 ('lead_ppb_low','Lead Result: 100 ppb','pass'),
 ('lead_micro_sign','Lead Result: 3000 µg/kg','fail'),
 ('lead_greek_mu','Lead Result: 3000 μg/kg','blocked'),
 ('cadmium_mgkg','Cadmium Result: 2 mg/kg','fail'),
 ('mercury_mgkg','Mercury Result: 2 mg/kg','fail'),
 ('arsenic_mgkg','Arsenic Result: 5 mg/kg','fail'),
 ('ethanol_decimal_comma','Ethanol Result: 3,2 %','fail'),
 ('ethanol_fraction','Ethanol Result: .8%','fail'),
 ('ethanol_decimal_comma_small','Ethanol Result: 0,05%','pass'),
 ('two_result_columns','Parameter | Result (%) | Result (%)\nEthanol | 0.1 | 3.2','fail'),
 ('second_result_empty','Parameter | Result (%) | Result (%)\nEthanol | 0.1 | ','review'),
 ('indonesian_unit','Parameter | Hasil | Satuan\nEtanol | 3,2 | %','fail'),
 ('korean_unit','항목 | 결과 | 단위\n에탄올 | 3.2 | %','fail'),
 ('micro_header','Parameter | Result (µg/kg)\nLead | 3000','fail'),
 ('wrapped_result','Ethanol\nResult: 3.2%','fail'),
 ('three_results','Ethanol Result: 0.1%; Result: ND; Result: 3.2%','fail'),
 ('three_upper','Ethanol Result: 0.1%; Result: <=5%; Result: 0.2%','review'),
 ('missing_after_good','Ethanol Result: 0.1%\nEthanol Result:','review'),
 ('pending_after_good','Ethanol: 0.1%\nEthanol: Pending','blocked'),
 ('tbd_after_good','Ethanol: 0.1%\nEthanol: TBD','blocked'),
 ('nt_after_good','Ethanol: 0.1%\nEthanol: Not tested','blocked'),
 ('unknown_after_good','Ethanol: 0.1%\nEthanol: Unknown','blocked'),
 ('scientific_high','Ethanol Result: 1e1%','blocked'),
 ('scientific_negative_power','Ethanol Result: 8e-1%','blocked'),
 ('uncertainty_crosses','Ethanol Result: 0.1% ± 3%','blocked'),
 ('range_crosses','Ethanol Result: 0.1%-3.2%','blocked'),
 ('range_text','Ethanol Result: 0.1% to 3.2%','blocked'),
 ('unknown_units','Parameter | Result | Unit\nEthanol | 3.2 | mg/L','review'),
 ('missing_result_unit','Parameter | Result\nEthanol | 3.2','review'),
 ('blank_result','Parameter | Result (%)\nEthanol | ','review'),
 ('all_three_analytes','Ethanol: 0.1%; Lead: 0.1 ppm; Mercury: 5 ppm','fail'),
 ('duplicate_name_no_result_label','Ethanol: <=5%; Ethanol: 0.1%','review'),
 ('valid_low_then_spec','Parameter | Result (%) | Specification (%)\nEthanol | 0.1 | 5','pass'),
 ('table_after_prose','Parameter | Result (%)\nEthanol | 0.1\nNotes\nEthanol Result: 3.2%','fail'),
 ('table_then_unheaded_table','Parameter | Result (%)\nEthanol | 0.1\nNew test\nLead | 3 ppm','fail'),
]
DNA = [
 ('same_line_missing','Porcine DNA PCR: Not detected; Porcine DNA PCR:','review'),
 ('sample_id_pending','Porcine DNA PCR\nSample A: Negative\nSample B: Pending','review'),
 ('sample_id_positive','Porcine DNA PCR\nSample A: Negative\nSample B: Positive','review'),
 ('dna_unavailable','Porcine DNA PCR: Not detected\nPorcine DNA PCR: Unavailable','review'),
 ('dna_insufficient','Porcine DNA PCR: Not detected\nPorcine DNA PCR: Insufficient sample','review'),
 ('dna_no_result','Porcine DNA PCR: Not detected\nPorcine DNA PCR: No result','review'),
 ('dna_not_analyzed','Porcine DNA PCR: Not detected\nPorcine DNA PCR: Not analyzed','review'),
 ('dna_not_detected_control_invalid','Porcine DNA PCR\nSample: Not detected\nPositive control: Invalid','review'),
 ('dna_control_failed','Porcine DNA PCR\nSample: Not detected\nPositive control: Failed','review'),
 ('dna_control_wrong_polarity','Porcine DNA PCR\nSample: Not detected\nPositive control: Negative','review'),
 ('dna_negative_control_contamination','Porcine DNA PCR\nSample: Not detected\nNegative control: Positive','review'),
 ('dna_valid_controls','Porcine DNA PCR\nSample: Not detected\nPositive control: Positive\nNegative control: Negative','pass'),
 ('dna_positive_valid','Porcine DNA PCR\nSample: Detected\nPositive control: Positive\nNegative control: Negative','fail'),
 ('dna_table_pending_control','Test | Sample | Positive control | Negative control\nPorcine DNA PCR | Not detected | Pending | Negative','review'),
 ('dna_table_second_sample','Test | Sample A | Sample B\nPorcine DNA PCR | Not detected | Pending','review'),
 ('dna_wrapped_pending','Porcine DNA PCR\nResult:\nPending','review'),
 ('dna_not_yet_detected','Porcine DNA PCR: Not yet detected','review'),
 ('dna_unconfirmed_positive','Porcine DNA PCR: Presumptive positive, confirmation pending','review'),
]
CERT_BODY = 'HALAL CERTIFICATE\nCertificate No: HC987\nIssuer: BPJPH\nValid until: 2028-02-29'
CLASS = [
 ('halal_phone_ref','Halal certificate.pdf',CERT_BODY+'\nApplication reference: APP-91','halal_certificate'),
 ('quality_app_no','ISO 22000.pdf','ISO 22000 CERTIFICATE\nApplication number: Q91','quality_cert'),
 ('coa_app_serial','CoA.pdf','CERTIFICATE OF ANALYSIS\nApplication number: Q91\nEthanol: 2%','coa_msds'),
 ('halal_invalid_watermark','Halal certificate.pdf','HALAL CERTIFICATE\nVOID - NOT VALID FOR CERTIFICATION\nCertificate No: HC987','other'),
 ('halal_draft_separate','Halal certificate.pdf','HALAL CERTIFICATE\nDRAFT - NOT ISSUED\nCertificate No: HC987','other'),
 ('halal_specimen','Halal certificate.pdf','SPECIMEN HALAL CERTIFICATE\nNOT VALID','other'),
 ('halal_template','Halal certificate template.pdf','HALAL CERTIFICATE TEMPLATE\nFor illustration only','other'),
 ('sample_id_not_template','Halal certificate.pdf',CERT_BODY+'\nSample ID: 3','halal_certificate'),
 ('application_body_quote','Halal certificate.pdf',CERT_BODY+'\nProcedure: submit application form for renewal','halal_certificate'),
 ('application_second_line_quote','Halal certificate.pdf','HALAL CERTIFICATE\nRenewal: submit application form\nCertificate No: HC987','halal_certificate'),
 ('nib_request','NIB.pdf','PERMOHONAN NIB\nNot issued','other'),
 ('new_request_dashed','Halal certificate.pdf','HALAL CERTIFICATION REQUEST FORM\nComplete all fields','other'),
 ('application_fused','Halal_certificate_application.pdf','APPLICATION FOR HALAL CERTIFICATION','other'),
 ('coa_sample_report','CoA sample A.pdf','CERTIFICATE OF ANALYSIS\nSample A\nEthanol Result: 3.2%','coa_msds'),
 ('coa_draft','CoA draft.pdf','DRAFT CERTIFICATE OF ANALYSIS\nEthanol: 3.2%','other'),
 ('cert_reissue','Reissued Halal certificate.pdf',CERT_BODY,'halal_certificate'),
]
CERT = [
 ('certificate_number',CERT_BODY.replace('Certificate No:', 'Certificate Number:'),'HC987'),
 ('id_label',CERT_BODY.replace('Certificate No:', 'Nomor Sertifikat Halal:'),'HC987'),
 ('korean_label',CERT_BODY.replace('Certificate No:', '인증서 번호:'),'HC987'),
 ('hash_label',CERT_BODY.replace('Certificate No:', 'Certificate No. #'),'HC987'),
 ('business_before',CERT_BODY.replace('Certificate No:', 'NIB: 1234567890123\nCertificate No:'),'HC987'),
 ('business_after',CERT_BODY+'\nBusiness Registration No: 1234567890123','HC987'),
 ('contact_label',CERT_BODY+'\nContact number: 08123456789','HC987'),
 ('revision_label',CERT_BODY+'\nRevision No: 3','HC987'),
 ('numeric_explicit',CERT_BODY.replace('HC987','12345678901234'),'12345678901234'),
 ('numeric_leading_zero',CERT_BODY.replace('HC987','0123456789012'),'0123456789012'),
 ('numeric_hyphen',CERT_BODY.replace('HC987','123-456-7890'),'123-456-7890'),
 ('slash_format',CERT_BODY.replace('HC987','ID/HC/2026/009'),'ID/HC/2026/009'),
 ('invalid_only_phone','HALAL CERTIFICATE\nTelephone No: 08123456789\nIssuer: BPJPH\nValid until: 2028-02-29',None),
 ('invalid_only_nib','HALAL CERTIFICATE\nNIB: 1234567890123\nIssuer: BPJPH\nValid until: 2028-02-29',None),
 ('invalid_only_lot','HALAL CERTIFICATE\nBatch number: LOT123\nIssuer: BPJPH\nValid until: 2028-02-29',None),
 ('cert_number_wrapped',CERT_BODY.replace('No: HC987','No:\nHC987'),'HC987'),
]
# Verification of model-supplied fields: membership alone must not validate a
# phone/lot/certificate value as a business number (or the reverse).
BIZ = [
 ('nib_exact','nib','1234567890123','NIB: 1234567890123',False),
 ('nib_separators','nib','1234567890123','NIB: 1234-5678-90123',False),
 ('nib_phone','nib','0812345678901','Telephone: 0812345678901',True),
 ('nib_certificate','nib','1234567890123','Certificate No: 1234567890123',True),
 ('nib_lot','nib','1234567890123','Lot No: 1234567890123',True),
 ('nib_short_substring','nib','12345','NIB: 1234567890123',True),
 ('factory_phone','factory_reg_no','08123456789','Telephone: 08123456789',True),
 ('factory_substring','factory_reg_no','ABC123','Factory No: XABC1239',True),
 ('certificate_phone','cert_no','08123456789','Telephone: 08123456789',True),
 ('certificate_nib','cert_no','1234567890123','NIB: 1234567890123',True),
 ('certificate_lot','cert_no','LOT123','Lot No: LOT123',True),
 ('certificate_exact','cert_no','HC987','Certificate No: HC987',False),
]
NEGATION = [
 ('No pork, no lard',False),('Free from pork or lard',False),
 ('Not totally pork-free',True),('Not entirely free from lard',True),
 ('Not always pork-free',True),('Not guaranteed pork-free',True),
 ('Cannot guarantee pork-free',True),('Not completely without lard',True),
 ('No added pork; contains lard',True),('Pork is not present',False),
 ('Pork is absent; lard is present',True),('No pork. Lard',True),
 ('No pork: contains lard',True),('No pork / contains lard',True),
 ('Free from pork (lard added)',True),('돼지고기를 함유하지 않는다',False),
 ('돼지고기가 들어 있지 않음',False),('돼지고기를 포함하지 않음',False),
 ('돼지고기를 함유하지 않는 것은 아님',True),('라드가 전혀 없는 것은 아님',True),
 ('tidak mengandung babi tetapi mengandung lemak babi',True),
 ('bukan sepenuhnya bebas dari babi',True),('tanpa babi; mengandung babi',True),
 ('No pork or lard, contains gelatin',False),
]
SPELLING = [(sep.join(word),True) for word in ('PORK','LARD') for sep in ('_','—','\\','+','|','\u200b')]
SPELLING += [('P:O:R:K-free',False),('No P,O,R,K',False),('Contains P/O/R/K',True),('P;O;R;K and L:A:R:D',True)]
IDENTITY = [
 ('slash_components','Blend (A/B)','Blend (AB)',False),
 ('semicolon_components','Blend (A;B)','Blend (AB)',False),
 ('dot_components','Blend (A·B)','Blend (AB)',False),
 ('space_components','Blend (pork fat)','Blend (porkfat)',True),
 ('bracket_width','Blend［A，B］','Blend [A,B]',True),
 ('brace_width','Blend｛A；B｝','Blend {A;B}',True),
 ('plus_vs_minus','Blend (A+B)','Blend (A-B)',False),
 ('numeric_en_dash','Blend (1–2)','Blend (12)',False),
 ('numeric_minus','Blend (1-2)','Blend (1−2)',True),
 ('salt_hydrate','Salt (CuSO4·5H2O)','Salt (CuSO45H2O)',False),
 ('bracket_origin','Gelatin [bovine]','Gelatin [porcine]',False),
 ('nested_brackets','Blend (A[B,C])','Blend (AB,C)',False),
]
PROBLEMS = [
 ('valid_ok',{'_verify':OK},[],False),
 ('warn_cert',{'_verify':{**OK,'verdict':'warn','field_issues':[{'field':'cert_no','issue':'wrong'}]}},[],True),
 ('warn_expiry',{'_verify':{**OK,'verdict':'warn','field_issues':[{'field':'expiry_date','issue':'expired'}]}},[],True),
 ('warn_issuer',{'_verify':{**OK,'verdict':'warn','field_issues':[{'field':'issuer','issue':'wrong issuer'}]}},[],True),
 ('warn_materials',{'_verify':{**OK,'verdict':'warn','field_issues':[{'field':'material_names','issue':'pork omitted'}]}},[],True),
 ('warn_type',{'_verify':{**OK,'doc_type_ok':False}},[],True),
 ('failed',{'_verify':{**OK,'verdict':'fail','field_issues':[{'field':'issuer','issue':'wrong'}]}},[],True),
 ('dna_review',{},[('pork_dna',None,'review')],True),
 ('lead_review',{},[('lead_ppm',None,'review')],True),
 ('quant_fail',{},[('ethanol_pct',3.2,'fail')],True),
 ('dna_fail',{},[('pork_dna',1.0,'fail')],True),
 ('good_quant',{},[('ethanol_pct',0.1,'pass')],False),
 ('missing_cert',{'_missing_fields':['cert_no']},[],True),
 ('empty_missing',{'_missing_fields':[]},[],False),
 ('null_truncated',{'_truncated':None},[],False),
 ('false_truncated',{'_truncated':False},[],False),
 ('null_llm_error',{'_llm_error':None},[],False),
 ('false_llm_error',{'_llm_error':False},[],False),
 ('resolved_owner',{'_autofill':{'conflicts':[{'field':'nib','reason':'nib_conflict','status':'resolved'}]}},[],False),
 ('pending_owner',{'_autofill':{'conflicts':[{'field':'nib','reason':'nib_conflict','status':'pending'}]}},[],True),
]


@pytest.mark.parametrize("name,text,expected", QUANT + DNA, ids=[r[0] for r in QUANT + DNA])
def test_audit170_quant_and_dna(client, headers, name, text, expected):
    cid = new_case(client, headers)
    response = upload(client, headers, cid, text)
    assert response.status_code == 200, response.text
    with m.SessionLocal() as db:
        actual = m._case_quant_verdict(db, cid)
        blockers = m._quant_blockers(db, cid)
        if expected == "blocked":
            assert blockers, (actual, text)
        else:
            assert actual == expected, text


@pytest.mark.parametrize("name,filename,text,expected", CLASS, ids=[r[0] for r in CLASS])
def test_audit170_classification(name, filename, text, expected):
    assert intake.refine_doctype_reason(filename, None, text)[0] == expected


@pytest.mark.parametrize("name,text,expected", CERT, ids=[r[0] for r in CERT])
def test_audit170_certificate_fields(name, text, expected):
    assert intake.parse_typed_text("halal_certificate", "Halal certificate.txt", text)["fields"].get("cert_no") == expected


@pytest.mark.parametrize("name,key,value,text,warning", BIZ, ids=[r[0] for r in BIZ])
def test_audit170_field_roles(name, key, value, text, warning):
    dtype = {"nib": "nib_business_license", "factory_reg_no": "factory_registration", "cert_no": "halal_certificate"}[key]
    result = intake.verify_extraction(dtype, "document.txt", text, {key: value})
    assert (result["verdict"] != "ok") == warning, result


@pytest.mark.parametrize("name,blocked", NEGATION + SPELLING, ids=[r[0] for r in NEGATION + SPELLING])
def test_audit170_screening(name, blocked):
    assert (screening.screen_merged(name, None, None, None, False, True, "")["result"] == "BLOCK") == blocked


@pytest.mark.parametrize("name,left,right,equal", IDENTITY, ids=[r[0] for r in IDENTITY])
def test_audit170_identity(name, left, right, equal):
    assert (models.normalize_material_name(left) == models.normalize_material_name(right)) == equal


@pytest.mark.parametrize("name,fields,measurements,wanted", PROBLEMS, ids=[r[0] for r in PROBLEMS])
def test_audit170_problem_documents(name, fields, measurements, wanted):
    doc = SimpleNamespace(document_id="audit", fields=fields, text_full=CERT_BODY,
                          text_excerpt=None, confidence=.9, content_b64="")
    rows = [SimpleNamespace(document_id="audit", param_key=k, value=v, verdict=w,
                            note=None, recorded_by="manual") for k, v, w in measurements]
    assert bool(m._problem_document_reasons(doc, rows)) == wanted


def pdf_bytes(text):
    with fitz.open() as doc:
        doc.new_page().insert_text((40, 50), text)
        return doc.tobytes()


def xlsx_bytes(rows):
    from openpyxl import Workbook
    book, output = Workbook(), io.BytesIO()
    for row in rows:
        book.active.append(row)
    book.save(output)
    book.close()
    return output.getvalue()


def zip_bytes(filename, raw):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(filename, raw)
    return output.getvalue()


def ready_case(cid, material_name="Sugar", mid=None):
    """Synthetic local business prerequisites; issue and second approval stay real."""
    with m.SessionLocal() as db:
        case = db.get(models.CaseApplication, cid)
        case.status, case.fatwa_status, case.pathway = "fatwa_approved", "approved", "reguler"
        product = models.Product(case_id=cid, name="Reviewed E3 product")
        material = db.get(models.Material, mid) if mid else models.Material(case_id=cid, name=material_name)
        db.add_all([product, material])
        db.flush()
        db.add(models.ProductMaterial(case_id=cid, product_id=product.product_id, material_id=material.material_id))
        invoice = models.Invoice(case_id=cid, service_type="pre_audit", amount=1, ppn=0, total=1, status="paid")
        db.add(invoice)
        seed_paid_receipt(db, invoice)
        seed_reviewed_issue_prerequisites(db, case)
        m.sm.freeze_scope(db, case)
        db.commit()
        return material.material_id


@pytest.mark.parametrize("text,lower,upper", [
    ("0.1% to 3.2%", .1, 3.2), ("0.1%-3.2%", .1, 3.2),
    ("0.1 to 3.2%", .1, 3.2), ("0.1% ± 3%", -2.9, 3.1),
    ("0.4 ± 0.2%", .2, .6), ("0.4% +/- 2000 ppm", .2, .6),
])
def test_r501_interval_preserved_and_issue_blocked(client, headers, text, lower, upper):
    cid = new_case(client, headers)
    response = upload(client, headers, cid, pdf_bytes("CERTIFICATE OF ANALYSIS\nEthanol Result: " + text), filename="CoA.pdf") if "±" not in text else upload(client, headers, cid, "Ethanol Result: " + text)
    assert response.status_code == 200, response.text
    with m.SessionLocal() as db:
        row = db.query(models.MaterialMeasurement).filter_by(case_id=cid).one()
        meta = json.loads(row.note)["measurement_meta"]
        assert meta["lower"] == pytest.approx(lower)
        assert meta["upper"] == pytest.approx(upper)
        assert row.value is None and row.verdict == "review"
    ready_case(cid)
    response = client.post(f"/cases/{cid}/certificate/issue", headers=headers["operator"], json={})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "MEASUREMENT_REVIEW_REQUIRED"
    with m.SessionLocal() as db:
        assert not db.query(models.HalalCertificate).filter_by(case_id=cid).count()


@pytest.mark.parametrize("control,status", [
    (control, status) for control in ("Positive control", "Negative control")
    for status in ("Failed", "Invalid", "Not valid", "Tidak valid", "부적합", "Pending")
] + [("Positive control", "Not detected"), ("Negative control", "Detected"),
     ("Kontrol positif", "Tidak terdeteksi"), ("음성 대조군", "검출")])
@pytest.mark.parametrize("layout", ["prose", "table"])
def test_r502_invalid_controls(client, headers, control, status, layout):
    text = (f"Porcine DNA PCR\nSample: Not detected\n{control}: {status}" if layout == "prose" else
            f"Test | Sample | {control}\nPorcine DNA PCR | Not detected | {status}")
    cid = new_case(client, headers)
    response = upload(client, headers, cid, text)
    assert response.status_code == 200, response.text
    with m.SessionLocal() as db:
        assert m._case_quant_verdict(db, cid) == "review"
    ready_case(cid)
    response = client.post(f"/cases/{cid}/certificate/issue", headers=headers["operator"], json={})
    assert response.status_code == 409, response.text


@pytest.mark.parametrize("name", ["돼지고기를 함유하지 않는 것은 아님", "라드가 없지 않다",
                                  "돼지고기가 아니라고 할 수 없다", "돼지고기가 전혀 없는 것은 아님",
                                  "P\u200bO\u200cR\u200dK", "L\ufeffA\u2060R\u200bD"])
def test_r503_hazard_issue_guard(client, headers, name):
    cid = new_case(client, headers)
    ready_case(cid, name)
    response = client.post(f"/cases/{cid}/certificate/issue", headers=headers["operator"], json={})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "HARAM_MATERIAL"
    with m.SessionLocal() as db:
        assert not db.query(models.HalalCertificate).filter_by(case_id=cid).count()


@pytest.mark.parametrize("marker", ["DRAFT - NOT ISSUED", "VOID", "SPECIMEN", "CONTOH", "견본", "무효", "취소"])
@pytest.mark.parametrize("route", ["parse-file", "intake-zip"])
def test_r505_invalid_certificate_checklist_and_reason(client, headers, marker, route):
    cid = new_case(client, headers)
    text = CERT_BODY.replace("HALAL CERTIFICATE", "HALAL CERTIFICATE\n" + marker)
    if route == "parse-file":
        response = upload(client, headers, cid, text, "halal_certificate", "Halal Certificate.txt")
    else:
        response = client.post(f"/cases/{cid}/intake-zip", headers=headers["applicant"], json={
            "zip_b64": base64.b64encode(zip_bytes("Halal Certificate.txt", text.encode())).decode()})
    assert response.status_code == 200, response.text
    with m.SessionLocal() as db:
        doc = db.query(models.DocumentAsset).filter_by(case_id=cid).one()
        assert doc.doc_type == "other"
        assert doc.fields["_cert_not_valid"]
        assert not doc.fields["_counts_as_evidence"]
    checklist = client.get(f"/cases/{cid}/doc-checklist", headers=headers["applicant"])
    assert checklist.status_code == 200, checklist.text
    assert "halal_certificate" in {row["doc_type"] for row in checklist.json()["missing"]}
    for lang in ("ko", "en", "id"):
        result = client.get(f"/cases/{cid}/problem-documents?lang={lang}", headers=headers["applicant"]).json()
        reasons = result["documents"][0]["reasons"]
        assert "CERT_NOT_VALID" in {r["code"] for r in reasons}
        assert all(r["explanation"] for r in reasons)


@pytest.mark.parametrize("label,number,accepted", [
    ("NIB", "1234567890123", True), ("NIB", "1234-5678-90123", True),
    ("사업자등록번호", "123-45-67890", True),
    ("Telephone", "0812345678901", False), ("Certificate No", "1234567890123", False),
    ("Lot No", "1234567890123", False), ("NIB", "12345", False),
    ("NIB", "ABC1234567890123", False),
])
def test_r506_model_identifier_autofill(client, headers, monkeypatch, label, number, accepted):
    cid = new_case(client, headers)
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda system, *a, **k:
                        dict(OK) if "접수 검수자" in system else {"company_name": "PT E3", "nib": number})
    text = "NOMOR INDUK BERUSAHA\nCompany: PT E3\n" + label + ": " + number
    response = upload(client, headers, cid, text, "nib_business_license", "NIB.txt")
    assert response.status_code == 200, response.text
    with m.SessionLocal() as db:
        case = db.get(models.CaseApplication, cid)
        doc = db.query(models.DocumentAsset).filter_by(case_id=cid).one()
        assert bool(case.nib) == accepted
        result = intake.verify_extraction(doc.doc_type, doc.filename, doc.text_full, doc.fields)
        assert (result["verdict"] == "ok") == accepted
        if not accepted:
            assert doc.fields["_rejected_identifiers"]["nib"] == number


@pytest.mark.parametrize("fields", [{"cert_no": "HC-111111", "expiry_date": "2099-01-01"},
                                    {"expiry_date": "2099-01-01"}])
def test_r507_certificate_number_roles(fields):
    doc = {"doc_type": "halal_certificate", "fields": fields,
           "text": "HALAL CERTIFICATE\nCertificate No: HC-111111\nLot HC-222222"}
    assert cert_verify.verify("HC-111111", [doc])["verdict"] == "VERIFIED"
    assert cert_verify.verify("HC-222222", [doc])["verdict"] == "MISMATCH"
    assert not screening.screen_merged("Gelatin", cert_no="HC-222222", docs=[doc])["cert_promoted"]


@pytest.mark.parametrize("number", ["0123456789012", "010-1234-5678", "+62 21 555 1234",
                                    "(021) 5551234", "02 555 1234"])
def test_r504_explicit_number_preserves_raw_candidate(number):
    text = CERT_BODY.replace("HC987", number)
    parsed = intake.parse_typed_text("halal_certificate", "Halal Certificate.txt", text)
    assert parsed["fields"]["cert_no"] == number
    assert "cert_no" not in parsed["fields"].get("_missing_fields", [])
    assert intake.autofill_value_in_text("cert_no", number, text)


def approved_evidence(client, headers, cid, mid, *, expiry="2099-01-01", names=None):
    with m.SessionLocal() as db:
        doc = models.DocumentAsset(case_id=cid, material_id=mid, filename="Halal Certificate.txt",
                                   doc_type="halal_certificate", content_b64=base64.b64encode(CERT_BODY.encode()).decode(),
                                   text_full=CERT_BODY, fields={"cert_no": "HC-111111", "expiry_date": expiry,
                                                                "material_names": names or ["Gelatin"]})
        db.add(doc)
        db.commit()
        did = doc.document_id
    response = client.patch(f"/documents/{did}/review", headers=headers["auditor"], json={"review_status": "approved"})
    assert response.status_code == 200, response.text
    return did


@pytest.mark.parametrize("mutation", ["expired", "expiry_today", "future", "missing_expiry", "invalid_expiry",
                                      "new_pending_version", "new_approved_wrong_scope", "reprocessed",
                                      "scope_changed", "scope_item_changed", "void", "corrupt", "unlinked"])
def test_r508_issue_revalidates_evidence(client, headers, mutation):
    cid = new_case(client, headers)
    mid = ready_case(cid, "Gelatin")
    expiry = {"expired": "2000-01-01", "expiry_today": date.today().isoformat(),
              "missing_expiry": None, "invalid_expiry": "not-a-date"}.get(mutation, "2099-01-01")
    did = approved_evidence(client, headers, cid, mid, expiry=expiry)
    with m.SessionLocal() as db:
        doc = db.get(models.DocumentAsset, did)
        if mutation.startswith("new_"):
            newer = models.DocumentAsset(case_id=cid, material_id=mid, filename=doc.filename, doc_type=doc.doc_type,
                                         content_b64=doc.content_b64, text_full=doc.text_full,
                                         fields={**doc.fields, "material_names": ["Salt"]}, review_status="pending")
            db.add(newer)
            db.flush()
            new_id = newer.document_id
        elif mutation == "reprocessed":
            m.sm.record_event(db, db.get(models.CaseApplication, cid), "fatwa_approved", "fatwa_approved",
                              "documents.reprocess", "system", None, {"document_id": did})
        elif mutation == "scope_changed":
            doc.fields = {**doc.fields, "material_names": ["Salt"]}
        elif mutation == "scope_item_changed":
            db.add(models.CertificateScopeItem(case_id=cid, document_id=did, certificate_no="HC-111111",
                                               scope_type="INGREDIENT", scope_value="Salt"))
        elif mutation == "void":
            doc.text_full += "\nVOID"
        elif mutation == "corrupt":
            doc.fields = {**doc.fields, "_ocr_error": "CORRUPT"}
        elif mutation == "unlinked":
            doc.material_id = None
        db.commit()
    if mutation == "new_approved_wrong_scope":
        response = client.patch(f"/documents/{new_id}/review", headers=headers["auditor"], json={"review_status": "approved"})
        assert response.status_code == 200, response.text
    response = client.post(f"/cases/{cid}/certificate/issue", headers=headers["operator"], json={})
    if mutation in ("expiry_today", "future"):
        assert response.status_code == 200, response.text
        approval = client.post(f"/approvals/{response.json()['approval_id']}/approve", headers=headers["admin"], json={})
        assert approval.status_code == 200, approval.text
    else:
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "CRITICAL_MATERIAL_NO_EVIDENCE"
    with m.SessionLocal() as db:
        assert bool(db.query(models.HalalCertificate).filter_by(case_id=cid).count()) == (mutation in ("expiry_today", "future"))


def test_r508_second_approval_rechecks_expiry(client, headers):
    cid = new_case(client, headers)
    mid = ready_case(cid, "Gelatin")
    did = approved_evidence(client, headers, cid, mid)
    response = client.post(f"/cases/{cid}/certificate/issue", headers=headers["operator"], json={})
    assert response.status_code == 200, response.text
    with m.SessionLocal() as db:
        doc = db.get(models.DocumentAsset, did)
        doc.fields = {**doc.fields, "expiry_date": "2000-01-01"}
        db.commit()
    response = client.post(f"/approvals/{response.json()['approval_id']}/approve", headers=headers["admin"], json={})
    assert response.status_code == 409, response.text
    with m.SessionLocal() as db:
        assert not db.query(models.HalalCertificate).filter_by(case_id=cid).count()


@pytest.mark.parametrize("kind", ["warn_expiry", "warn_issuer", "warn_materials", "quant_fail", "dna_fail"])
def test_r509_notification_recipient(client, headers, kind):
    cid = new_case(client, headers)
    text = CERT_BODY if kind.startswith("warn_") else "Ethanol: 3.2%" if kind == "quant_fail" else "Porcine DNA PCR: Detected"
    dtype = "halal_certificate" if kind.startswith("warn_") else "coa_msds"
    assert upload(client, headers, cid, text, dtype, "document.txt").status_code == 200
    with m.SessionLocal() as db:
        case = db.get(models.CaseApplication, cid)
        case.email, case.notify_consent = "e3@example.test", True
        doc = db.query(models.DocumentAsset).filter_by(case_id=cid).one()
        if kind.startswith("warn_"):
            field = {"warn_expiry": "expiry_date", "warn_issuer": "issuer", "warn_materials": "material_names"}[kind]
            doc.fields = {**doc.fields, "_verify": {**OK, "verdict": "warn", "field_issues": [{"field": field, "issue": "check"}]}}
        db.commit()
    for lang in ("ko", "en", "id"):
        response = client.get(f"/cases/{cid}/problem-documents?lang={lang}", headers=headers["applicant"])
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["count"] == 1
        expected = "VERIFY_WARN" if kind.startswith("warn_") else "QUANT_FAIL"
        assert expected in {r["code"] for r in result["documents"][0]["reasons"]}
        assert result["recipient_type"] == ("operator" if kind.startswith("warn_") else "applicant")


@pytest.mark.parametrize("separator", ["·", "・", "•", "‧", "∙", "⋅"])
@pytest.mark.parametrize("route", ["parse-file", "intake-zip"])
def test_r510_real_material_table_preserves_separator(client, headers, separator, route):
    cid = new_case(client, headers)
    raw = xlsx_bytes([["Daftar Bahan Halal"],
                      ["No", "Nama Bahan", "Jenis Bahan", "Produsen", "Negara", "No. Sertifikat Halal"],
                      [1, "Blend(A" + separator + "B)", "Bahan", "PT LOCAL", "ID", ""],
                      [2, "Blend(AB)", "Bahan", "PT LOCAL", "ID", ""]])
    if route == "parse-file":
        response = upload(client, headers, cid, raw, "material_list", "Daftar Bahan.xlsx")
    else:
        response = client.post(f"/cases/{cid}/intake-zip", headers=headers["applicant"], json={
            "zip_b64": base64.b64encode(zip_bytes("Daftar Bahan.xlsx", raw)).decode()})
    assert response.status_code == 200, response.text
    with m.SessionLocal() as db:
        assert {v.name for v in db.query(models.Material).filter_by(case_id=cid)} == {"Blend(A" + separator + "B)", "Blend(AB)"}


def office_bytes(ext, valid):
    if valid:
        if ext == "xlsx":
            return xlsx_bytes([["Item", "Value"], ["Sugar", 1]])
        from docx import Document
        doc, buf = Document(), io.BytesIO()
        doc.add_paragraph("Local E3 document")
        doc.save(buf)
        return buf.getvalue()
    part, typename = ("word/document.xml", "wordprocessingml.document") if ext == "docx" else ("xl/workbook.xml", "spreadsheetml.sheet")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/' + part + '" ContentType="application/vnd.openxmlformats-officedocument.' + typename + '.main+xml"/></Types>')
        archive.writestr(part, "<this_is_not_an_office_document/>")
    return buf.getvalue()


def binary_bytes(ext, valid):
    if ext in ("docx", "xlsx"):
        return office_bytes(ext, valid)
    if not valid:
        return {"pdf": b"%PDF-1.7\nThis is plain text, no objects or xref or trailer.",
                "png": b"\x89PNG\r\n\x1a\nNot IHDR or valid image chunks.",
                "jpg": b"\xff\xd8\xff\xe0\x00\x10JFIF\x00fake\xff\xd9"}[ext]
    if ext == "pdf":
        return pdf_bytes("Valid E3 document")
    out = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(out, format="PNG" if ext == "png" else "JPEG")
    return out.getvalue()


INGRESS = ("documents", "parse-file", "intake-zip", "intake-zip-stream", "material_evidence", "ocr-extract")


@pytest.mark.parametrize("ext,route", [(ext, route) for ext in ("pdf", "png", "jpg", "docx", "xlsx") for route in INGRESS])
@pytest.mark.parametrize("valid", [False, True], ids=["corrupt", "valid"])
def test_c17_structure_all_upload_paths(client, headers, ext, route, valid):
    cid = new_case(client, headers)
    raw, filename = binary_bytes(ext, valid), "proof." + ext
    if route.startswith("intake-zip"):
        response = client.post(f"/cases/{cid}/{route}", headers=headers["applicant"], json={
            "zip_b64": base64.b64encode(zip_bytes(filename, raw)).decode()})
    elif route == "material_evidence":
        response = client.post(f"/cases/{cid}/materials", headers=headers["applicant"], json={"name": "Sugar"})
        mid = response.json()["material_id"]
        response = client.post(f"/cases/{cid}/materials/{mid}/evidence", headers=headers["applicant"], json={
            "filename": filename, "file_b64": base64.b64encode(raw).decode(), "evidence_type": "coa_msds"})
    elif route == "ocr-extract":
        response = client.post("/auth/ocr-extract", json={"image_b64": base64.b64encode(raw).decode()})
    else:
        response = upload(client, headers, cid, raw, "other", filename, route)
    accepted = valid and not (route == "ocr-extract" and ext in ("docx", "xlsx"))
    assert response.status_code == (200 if accepted else 415), response.text
    with m.SessionLocal() as db:
        assert db.query(models.DocumentAsset).filter_by(case_id=cid).count() == int(accepted and route != "ocr-extract")


@pytest.mark.parametrize("text,expected", [
    ("Ethanol: 0.1%\nResult: Pending", "review"),
    ("Ethanol: 0.1%\nPending", "review"),
    ("Ethanol\nResult: 0.1%", "pass"),
    ("Ethanol: 0.1%\nNotes\nPending", "pass"),
    ("Ethanol Result: 1e999%", "review"),
    ("Ethanol Result: ND (LOD 1e999%)", "review"),
])
def test_r501_wrapped_and_nonfinite_observations(client, headers, text, expected):
    cid = new_case(client, headers)
    response = upload(client, headers, cid, text)
    assert response.status_code == 200, response.text
    with m.SessionLocal() as db:
        assert m._case_quant_verdict(db, cid) == expected


@pytest.mark.parametrize("mutation", ["void_new_version", "contradictory_scope", "new_review_wrong_material"])
def test_r508_latest_scope_even_when_document_reclassified(client, headers, mutation):
    cid = new_case(client, headers)
    mid = ready_case(cid, "Gelatin")
    did = approved_evidence(client, headers, cid, mid)
    with m.SessionLocal() as db:
        doc = db.get(models.DocumentAsset, did)
        if mutation == "void_new_version":
            db.add(models.DocumentAsset(case_id=cid, filename=doc.filename, doc_type="other", material_id=mid,
                                         fields={"_cert_not_valid": "VOID"}, content_b64=doc.content_b64))
        elif mutation == "contradictory_scope":
            doc.fields = {**doc.fields, "scope": "Salt"}
        else:
            m.sm.record_event(db, db.get(models.CaseApplication, cid), "fatwa_approved", "fatwa_approved",
                              "documents.review", "auditor", None,
                              {"document_id": did, "review_status": "approved", "material_scope": []})
        db.commit()
    response = client.post(f"/cases/{cid}/certificate/issue", headers=headers["operator"], json={})
    assert response.status_code == 409, response.text

