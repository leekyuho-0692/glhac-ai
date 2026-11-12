"""R7 f2: final2-ai 독립 경계 186개 + 실제 업로드/승인 회귀.

감사 boundary6_cases.json 입력/기대값을 복사해 외부 감사 폴더 없이 실행한다.
LLM/외부 연결만 대체하며 저장, 판정, 발급 가드는 실제 구현을 사용한다.
"""
import base64
import io
import json
import os
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta
from types import SimpleNamespace

import fitz
import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app import intake, models, screening, cert_verify, main as m
from test_final_e2_rules import seed_reviewed_issue_prerequisites, seed_paid_receipt

OK = {"verdict": "ok", "doc_type_ok": True, "field_issues": []}
CERT_BODY = "HALAL CERTIFICATE\nCertificate No: HC-666666\nIssuer: BPJPH\nValid until: 2099-12-31"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(m, "_RL_WRITE_HITS", {})
    monkeypatch.setattr(m, "_AUTOPARSE", False)
    monkeypatch.setattr(m, "autoparse_enqueue", lambda *a, **k: False)
    monkeypatch.setattr(m, "verify_enqueue", lambda *a, **k: False)
    monkeypatch.setattr(intake, "_ZIP_LOOKUP", False)
    monkeypatch.setenv("GLHAC_FORMAL_FLOW", "0")
    monkeypatch.setenv("GLHAC_TWO_PERSON", "1")
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda system, *a, **k:
                        dict(OK) if "접수 검수자" in system else {})
    monkeypatch.setattr(intake.ai_local, "llm_text", lambda *a, **k: "")


@pytest.fixture
def api():
    with TestClient(m.app) as client:
        headers = {}
        for actor in ("applicant1", "operator1", "auditor1", "fatwa1"):
            r = client.post("/auth/login", json={"username": actor, "password": "pw"})
            assert r.status_code == 200, r.text
            headers[actor] = {"Authorization": "Bearer " + r.json()["token"]}

        def request(method, path, body=None, actor="applicant1"):
            return client.request(method, path, json=body, headers=headers[actor])
        yield request


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def new_case(api):
    return ok(api("POST", "/cases", {"company_name": "PT R7 F2"}))["case_id"]


def pdf(text):
    with fitz.open() as doc:
        doc.new_page().insert_text((40, 50), text, fontsize=10)
        return doc.tobytes()


def workbook(rows):
    book = Workbook()
    for row in rows:
        book.active.append(row)
    out = io.BytesIO()
    book.save(out)
    book.close()
    return out.getvalue()


def upload(api, cid, raw, name="CoA.pdf", dtype="coa_msds", route="parse-file"):
    if route == "parse-file":
        body = {"filename": name, "doc_type": dtype, "file_b64": base64.b64encode(raw).decode()}
    else:
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            archive.writestr(name, raw)
        body = {"zip_b64": base64.b64encode(out.getvalue()).decode()}
    return ok(api("POST", f"/cases/{cid}/{route}", body))


def ready(api, cid, ids=None):
    if ids is None:
        ids = [ok(api("POST", f"/cases/{cid}/materials", {"name": "Sugar"}))["material_id"]]
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        c.status, c.fatwa_status, c.pathway = "fatwa_approved", "approved", "reguler"
        product = models.Product(case_id=cid, name="Reviewed F2 product")
        db.add(product)
        db.flush()
        for mid in ids:
            db.add(models.ProductMaterial(case_id=cid, product_id=product.product_id, material_id=mid))
        invoice = models.Invoice(case_id=cid, service_type="pre_audit", amount=1, ppn=0, total=1, status="paid")
        db.add(invoice)
        seed_paid_receipt(db, invoice)
        seed_reviewed_issue_prerequisites(db, c)
        m.sm.freeze_scope(db, c)
        db.commit()


def issue(api, cid, allowed):
    r = api("POST", f"/cases/{cid}/certificate/issue", {"reason": "F2 regression"}, "operator1")
    if allowed:
        aid = ok(r)["approval_id"]
        ok(api("POST", f"/approvals/{aid}/approve", {"reason": "F2 checker"}, "fatwa1"))
    else:
        assert r.status_code == 409, r.text
    with m.SessionLocal() as db:
        assert bool(db.query(models.HalalCertificate).filter_by(case_id=cid).filter(
            models.HalalCertificate.status != "external").count()) is allowed


def evidence(api, cid, mid, text=CERT_BODY, filename="Halal Certificate.pdf", review=True):
    ok(api("POST", f"/cases/{cid}/materials/{mid}/evidence", {
        "evidence_type": "halal_certificate", "filename": filename,
        "file_b64": base64.b64encode(pdf(text)).decode(), "counts_as_evidence": True}))
    with m.SessionLocal() as db:
        did = db.query(models.DocumentAsset).filter_by(case_id=cid, material_id=mid).order_by(
            models.DocumentAsset.created_at.desc()).first().document_id
    ok(api("POST", f"/documents/{did}/reprocess", {}, "operator1"))
    if review:
        ok(api("PATCH", f"/documents/{did}/review", {"review_status": "approved"}, "auditor1"))
    return did

GROUPS = {'quant': [['lead_percent', 'Lead Result: 0.01%', 'fail'],
           ['mercury_ppb', 'Mercury Result: 8000 ppb', 'fail'],
           ['arsenic_micro', 'Arsenic Result: 8000 μg/kg', 'fail'],
           ['ethanol_ppm_low', 'Ethanol Result: 100 ppm', 'pass'],
           ['ethanol_mgkg_high', 'Ethanol Result: 8000 mg/kg', 'fail'],
           ['ethanol_small_exponent', 'Ethanol Result: 2.5e-2%', 'pass'],
           ['quant_repeat_line', 'Ethanol Result: 0.1%\nRepeat result: 3.2%', 'blocked'],
           ['quant_duplicate_assay', 'Ethanol Result: 0.1%\nDuplicate assay result: 3.2%', 'blocked'],
           ['quant_continued_row',
            'Parameter | Result (%)\nEthanol | 0.1\nEthanol (retest) | 3.2',
            'blocked'],
           ['quant_retest_column',
            'Parameter | Initial result (%) | Retest result (%)\nEthanol | 0.1 | 3.2',
            'blocked'],
           ['quant_control_column',
            'Parameter | Result (%) | Specification (%)\nEthanol | 0.02 | 0.5',
            'pass'],
           ['quant_qualifier_range', 'Ethanol Result: <=0.1% to 3.2%', 'blocked'],
           ['quant_qualifier_uncertainty', 'Ethanol Result: <0.1% +/- 3%', 'blocked'],
           ['quant_range_unicode_minus', 'Ethanol Result: 0.1% − 3.2%', 'blocked'],
           ['quant_range_fullwidth_tilde', 'Ethanol Result: 0.1% ～ 3.2%', 'blocked'],
           ['quant_pending_blank_line', 'Ethanol Result: 0.1%\n\nResult: Pending', 'blocked'],
           ['quant_status_later', 'Ethanol Result: 0.1%\nTest status: Invalid', 'blocked'],
           ['quant_error_after_good', 'Ethanol Result: 0.1%\nEthanol Result: Error', 'blocked'],
           ['quant_cancelled_after_good', 'Ethanol Result: 0.1%\nEthanol Result: Cancelled', 'blocked'],
           ['quant_nd_overflow', 'Ethanol Result: ND (LOD 1e309%)', 'blocked'],
           ['quant_nan_after_low', 'Ethanol Result: 0.1%; Result: NaN%', 'blocked'],
           ['quant_inf_after_low', 'Ethanol Result: 0.1%; Result: Infinity%', 'blocked'],
           ['quant_unit_mismatch', 'Parameter | Result (%) | Unit\nEthanol | 3.2 | ppm', 'blocked'],
           ['quant_unknown_unit', 'Ethanol Result: 3.2 g/L', 'blocked'],
           ['quant_negative_value', 'Ethanol Result: -0.1%', 'blocked'],
           ['quant_result_footnote', 'Ethanol Result: 0.1%*\n* Invalid run; repeat required', 'blocked'],
           ['quant_mixed_scientific', 'Lead Result: 2e3 μg/kg\nMercury Result: 5e3 ppb', 'fail'],
           ['quant_reversed_range', 'Ethanol Result: 4% to 0.1%', 'blocked'],
           ['quant_range_low_control', 'Ethanol Result: 0.01% to 0.02%', 'blocked'],
           ['quant_third_result',
            'Parameter | Result (%) | Result (%) | Result (%)\nEthanol | 0.1 | 0.2 | 3.3',
            'fail']],
 'dna': [['dna_control_wrap', 'Porcine DNA PCR\nSample: Negative\nPositive control:\nFailed', 'blocked'],
         ['dna_control_bare_wrap',
          'Porcine DNA PCR\nSample: Negative\nPositive control\nFailed',
          'blocked'],
         ['dna_negative_control_wrap',
          'Porcine DNA PCR\nSample: Negative\nNegative control:\nPositive',
          'blocked'],
         ['dna_control_table_rows',
          'Porcine DNA PCR\nType | Result\nSample | Negative\nPositive control | Failed',
          'blocked'],
         ['dna_control_table_wrong',
          'Porcine DNA PCR\nType | Result\nSample | Negative\nNegative control | Positive',
          'blocked'],
         ['dna_controls_global',
          'Positive control: Failed\nPorcine DNA PCR\nSample: Negative',
          'blocked'],
         ['dna_control_before_block',
          'Porcine DNA PCR\nPositive control: Negative\nSample: Negative',
          'blocked'],
         ['dna_sample_wrap', 'Porcine DNA PCR\nSample:\nDetected', 'blocked'],
         ['dna_test_invalid_later', 'Porcine DNA PCR: Negative\nRun status: Failed', 'blocked'],
         ['dna_ntc_contaminated', 'Porcine DNA PCR\nSample: Negative\nNTC: Contaminated', 'blocked'],
         ['dna_internal_failed',
          'Porcine DNA PCR\nSample: Negative\nInternal control: Failed',
          'blocked'],
         ['dna_amp_control_failed',
          'Porcine DNA PCR\nSample: Negative\nAmplification control: Failed',
          'blocked'],
         ['dna_undetermined', 'Porcine DNA PCR: Negative\nPorcine DNA PCR: Undetermined', 'blocked'],
         ['dna_not_performed', 'Porcine DNA PCR: Negative\nPorcine DNA PCR: Not performed', 'blocked'],
         ['dna_korean_control', '돼지 DNA PCR\n시료: 불검출\n양성 대조군: 음성', 'blocked'],
         ['dna_id_control', 'Porcine DNA PCR\nSampel: Negatif\nKontrol positif: Negatif', 'blocked'],
         ['dna_valid_table',
          'Test | Sample | Positive control | Negative control\n'
          'Porcine DNA PCR | Negative | Positive | Negative',
          'pass'],
         ['dna_valid_english',
          'Porcine DNA PCR\nSample: Negative\nPositive control: Valid\nNegative control: Valid',
          'pass'],
         ['dna_positive_english',
          'Porcine DNA PCR\nSample: Positive\nPositive control: Valid\nNegative control: Valid',
          'fail'],
         ['dna_unreadable_second',
          'Porcine DNA PCR: Negative\nPorcine DNA PCR: [unreadable]',
          'blocked']],
 'classification': [['void_conditional',
                     'Halal certificate.pdf',
                     'HALAL CERTIFICATE\n'
                     'Certificate No: HC-666666\n'
                     'Issuer: BPJPH\n'
                     'Valid until: 2099-12-31\n'
                     'Void if altered',
                     'halal_certificate'],
                    ['void_cert_conditional',
                     'Halal certificate.pdf',
                     'HALAL CERTIFICATE\n'
                     'Certificate No: HC-666666\n'
                     'Issuer: BPJPH\n'
                     'Valid until: 2099-12-31\n'
                     'This certificate is void if altered',
                     'halal_certificate'],
                    ['valid_signature_condition',
                     'Halal certificate.pdf',
                     'HALAL CERTIFICATE\n'
                     'Certificate No: HC-666666\n'
                     'Issuer: BPJPH\n'
                     'Valid until: 2099-12-31\n'
                     'Not valid without signature\n'
                     'Authorized signature: Signed',
                     'halal_certificate'],
                    ['revocation_instructions',
                     'Halal certificate.pdf',
                     'HALAL CERTIFICATE\n'
                     'Certificate No: HC-666666\n'
                     'Issuer: BPJPH\n'
                     'Valid until: 2099-12-31\n'
                     'Certificate may be revoked if conditions are violated',
                     'halal_certificate'],
                    ['historical_cancel',
                     'Halal certificate.pdf',
                     'HALAL CERTIFICATE\n'
                     'Certificate No: HC-666666\n'
                     'Issuer: BPJPH\n'
                     'Valid until: 2099-12-31\n'
                     'Previous certificate HC-000000 was cancelled',
                     'halal_certificate'],
                    ['draft_previous',
                     'Halal certificate.pdf',
                     'HALAL CERTIFICATE\n'
                     'Certificate No: HC-666666\n'
                     'Issuer: BPJPH\n'
                     'Valid until: 2099-12-31\n'
                     'Previous draft certificate has been replaced by this signed original',
                     'halal_certificate'],
                    ['template_policy',
                     'Halal certificate.pdf',
                     'HALAL CERTIFICATE\n'
                     'Certificate No: HC-666666\n'
                     'Issuer: BPJPH\n'
                     'Valid until: 2099-12-31\n'
                     'Certificate template revision: 4',
                     'halal_certificate'],
                    ['korean_condition',
                     'Halal certificate.pdf',
                     'HALAL CERTIFICATE\n'
                     'Certificate No: HC-666666\n'
                     'Issuer: BPJPH\n'
                     'Valid until: 2099-12-31\n'
                     '인증서는 변조 시 무효입니다',
                     'halal_certificate'],
                    ['cancelled_application',
                     'Halal certificate.pdf',
                     'HALAL CERTIFICATE\n'
                     'Certificate No: HC-666666\n'
                     'Issuer: BPJPH\n'
                     'Valid until: 2099-12-31\n'
                     'Application cancelled in 2025; certificate issued after new application in 2026',
                     'halal_certificate'],
                    ['specimen_signature',
                     'Halal certificate.pdf',
                     'HALAL CERTIFICATE\n'
                     'Certificate No: HC-666666\n'
                     'Issuer: BPJPH\n'
                     'Valid until: 2099-12-31\n'
                     'Signature specimen of authorised issuer: Example',
                     'halal_certificate'],
                    ['status_draft',
                     'Halal certificate.pdf',
                     'HALAL CERTIFICATE\n'
                     'Certificate No: HC-666666\n'
                     'Issuer: BPJPH\n'
                     'Valid until: 2099-12-31\n'
                     'Status: DRAFT',
                     'other'],
                    ['status_void',
                     'Halal certificate.pdf',
                     'HALAL CERTIFICATE\n'
                     'Certificate No: HC-666666\n'
                     'Issuer: BPJPH\n'
                     'Valid until: 2099-12-31\n'
                     'Status: VOID',
                     'other'],
                    ['status_revoked',
                     'Halal certificate.pdf',
                     'HALAL CERTIFICATE\n'
                     'Certificate No: HC-666666\n'
                     'Issuer: BPJPH\n'
                     'Valid until: 2099-12-31\n'
                     'Document status: REVOKED',
                     'other'],
                    ['status_cancelled',
                     'Halal certificate.pdf',
                     'HALAL CERTIFICATE\n'
                     'Certificate No: HC-666666\n'
                     'Issuer: BPJPH\n'
                     'Valid until: 2099-12-31\n'
                     'Status: CANCELLED',
                     'other'],
                    ['status_not_issued',
                     'Halal certificate.pdf',
                     'HALAL CERTIFICATE\n'
                     'Certificate No: HC-666666\n'
                     'Issuer: BPJPH\n'
                     'Valid until: 2099-12-31\n'
                     'Status: NOT ISSUED',
                     'other'],
                    ['watermark_draft',
                     'Halal certificate.pdf',
                     'HALAL CERTIFICATE\n'
                     'Certificate No: HC-666666\n'
                     'Issuer: BPJPH\n'
                     'Valid until: 2099-12-31\n'
                     '[DRAFT]',
                     'other'],
                    ['watermark_void',
                     'Halal certificate.pdf',
                     'HALAL CERTIFICATE\n'
                     'Certificate No: HC-666666\n'
                     'Issuer: BPJPH\n'
                     'Valid until: 2099-12-31\n'
                     '*** VOID ***',
                     'other'],
                    ['status_korean',
                     'Halal certificate.pdf',
                     'HALAL CERTIFICATE\n'
                     'Certificate No: HC-666666\n'
                     'Issuer: BPJPH\n'
                     'Valid until: 2099-12-31\n'
                     '상태: 미발급',
                     'other'],
                    ['status_id',
                     'Halal certificate.pdf',
                     'HALAL CERTIFICATE\n'
                     'Certificate No: HC-666666\n'
                     'Issuer: BPJPH\n'
                     'Valid until: 2099-12-31\n'
                     'Status: tidak berlaku',
                     'other'],
                    ['status_specimen',
                     'Halal certificate.pdf',
                     'HALAL CERTIFICATE\n'
                     'Certificate No: HC-666666\n'
                     'Issuer: BPJPH\n'
                     'Valid until: 2099-12-31\n'
                     'Document status: SPECIMEN',
                     'other']],
 'certificate_fields': [['cert_wrapped_crlf',
                         'HALAL CERTIFICATE\n'
                         'Certificate No:\r\n'
                         'HC-666666\n'
                         'Issuer: BPJPH\n'
                         'Valid until: 2099-12-31',
                         'HC-666666'],
                        ['cert_tab',
                         'HALAL CERTIFICATE\n'
                         'Certificate No.\tHC-666666\n'
                         'Issuer: BPJPH\n'
                         'Valid until: 2099-12-31',
                         'HC-666666'],
                        ['cert_pipe',
                         'HALAL CERTIFICATE\n'
                         'Certificate No. | HC-666666\n'
                         'Issuer: BPJPH\n'
                         'Valid until: 2099-12-31',
                         'HC-666666'],
                        ['cert_equals',
                         'HALAL CERTIFICATE\n'
                         'Certificate No = HC-666666\n'
                         'Issuer: BPJPH\n'
                         'Valid until: 2099-12-31',
                         'HC-666666'],
                        ['cert_fullwidth_colon',
                         'HALAL CERTIFICATE\n'
                         'Certificate No： HC-666666\n'
                         'Issuer: BPJPH\n'
                         'Valid until: 2099-12-31',
                         'HC-666666'],
                        ['cert_indonesian_newline',
                         'HALAL CERTIFICATE\n'
                         'Nomor Sertifikat Halal:\n'
                         'HC-666666\n'
                         'Issuer: BPJPH\n'
                         'Valid until: 2099-12-31',
                         'HC-666666'],
                        ['cert_ko_newline',
                         'HALAL CERTIFICATE\n인증 번호:\nHC-666666\nIssuer: BPJPH\nValid until: 2099-12-31',
                         'HC-666666'],
                        ['cert_ref_before',
                         'Reference No: REF-999999\n'
                         'HALAL CERTIFICATE\n'
                         'Certificate No: HC-666666\n'
                         'Issuer: BPJPH\n'
                         'Valid until: 2099-12-31',
                         'HC-666666'],
                        ['cert_contact_before',
                         'Contact No: 010-1234-5678\n'
                         'HALAL CERTIFICATE\n'
                         'Certificate No: HC-666666\n'
                         'Issuer: BPJPH\n'
                         'Valid until: 2099-12-31',
                         'HC-666666'],
                        ['cert_contact_after',
                         'HALAL CERTIFICATE\n'
                         'Certificate No: HC-666666\n'
                         'Issuer: BPJPH\n'
                         'Valid until: 2099-12-31\n'
                         'Contact: 010-1234-5678',
                         'HC-666666'],
                        ['cert_date_before',
                         'Issue date: 2026-10-01\n'
                         'HALAL CERTIFICATE\n'
                         'Certificate No: HC-666666\n'
                         'Issuer: BPJPH\n'
                         'Valid until: 2099-12-31',
                         'HC-666666'],
                        ['cert_numeric_spaces',
                         'HALAL CERTIFICATE\n'
                         'Certificate No: 012 345 678 9012\n'
                         'Issuer: BPJPH\n'
                         'Valid until: 2099-12-31',
                         '012 345 678 9012'],
                        ['cert_numeric_slashes',
                         'HALAL CERTIFICATE\n'
                         'Certificate No: 01/234/567/890\n'
                         'Issuer: BPJPH\n'
                         'Valid until: 2099-12-31',
                         '01/234/567/890'],
                        ['cert_no_value',
                         'HALAL CERTIFICATE\nCertificate No: \nIssuer: BPJPH\nValid until: 2099-12-31',
                         None],
                        ['cert_no_phone_only',
                         'HALAL CERTIFICATE\nTelephone number: 01012345678\nValid until: 2099-12-31',
                         None],
                        ['cert_number_symbol',
                         'HALAL CERTIFICATE\n'
                         'Certificate №: HC-666666\n'
                         'Issuer: BPJPH\n'
                         'Valid until: 2099-12-31',
                         'HC-666666']],
 'field_roles': [['nib_ko', 'nib', '1234567890', '사업자등록번호: 123-45-67890', False],
                 ['nib_id_wrap', 'nib', '1234567890123', 'Nomor Induk Berusaha:\n1234567890123', False],
                 ['nib_title_phone', 'nib', '1234567890123', 'NIB\nTelephone: 1234567890123', True],
                 ['nib_named_owner',
                  'nib',
                  '1234567890123',
                  'Supplier NIB: 1234567890123\nApplicant: PT AUDIT SIX',
                  True],
                 ['nib_pipe_roles', 'nib', '1234567890123', 'NIB | Telephone\n | 1234567890123', True],
                 ['nib_note_role',
                  'nib',
                  '1234567890123',
                  'NIB: see attached\nTelephone: 1234567890123',
                  True],
                 ['nib_unsupported_length', 'nib', '123456789012', 'NIB: 123456789012', True],
                 ['nib_decimal_not_id', 'nib', '1234567890123', 'NIB: 1234567890123.45', True],
                 ['nib_suffix_letter', 'nib', '1234567890123', 'NIB: 1234567890123X', True],
                 ['factory_id_label',
                  'factory_reg_no',
                  'FR-666666',
                  'Nomor Registrasi Pabrik: FR-666666',
                  False],
                 ['factory_newline',
                  'factory_reg_no',
                  'FR-666666',
                  'Factory Registration No:\nFR-666666',
                  False],
                 ['factory_ko', 'factory_reg_no', 'FR-666666', '공장 등록 번호: FR-666666', False],
                 ['factory_phone_under_title',
                  'factory_reg_no',
                  '1234567890',
                  'FACTORY REGISTRATION\nTelephone: 1234567890',
                  True],
                 ['cert_issuer_role', 'cert_no', 'HC-666666', 'Issuer Reference: HC-666666', True],
                 ['cert_batch_under_title',
                  'cert_no',
                  'HC-666666',
                  'HALAL CERTIFICATE\nBatch No: HC-666666',
                  True],
                 ['cert_spaced_label', 'cert_no', 'HC-666666', 'Certificate Number: HC-666666', False]],
 'negative_context': [['돼지고기를 함유하지 않는다는 보장은 없다', True],
                      ['돼지고기를 함유하지 않는다고 단정할 수 없다', True],
                      ['라드 무첨가라고 할 수 없다', True],
                      ['라드가 없다는 뜻은 아니다', True],
                      ['Not necessarily pork-free', True],
                      ['Cannot certify pork-free', True],
                      ['Cannot confirm free from lard', True],
                      ['Not assured to be free from pork', True],
                      ['Pork is not absent', True],
                      ['No absence of pork is guaranteed', True],
                      ['Not without pork', True],
                      ['Without pork, but with lard', True],
                      ['No pork ingredients; pork fat added', True],
                      ['Free from lard except pork lard', True],
                      ['No pork and no lard ingredients', False],
                      ['No pork; no lard; no blood', False],
                      ['돼지고기는 전혀 포함하지 않는다', False],
                      ['라드를 전혀 함유하지 않음', False],
                      ['Tanpa babi dan tanpa darah', False],
                      ['Bebas dari babi dan darah', False]],
 'spelling': [['P\u2060O\u2060R\u2060K', True],
              ['P\u200dO\u200dR\u200dK', True],
              ['P\ufeffO\ufeffR\ufeffK', True],
              ['P∕O∕R∕K', True],
              ['P⁄O⁄R⁄K', True],
              ['P•O•R•K', True],
              ['L\u2060A\u2060R\u2060D', True],
              ['L\u200dA\u200dR\u200dD', True],
              ['L\ufeffA\ufeffR\ufeffD', True],
              ['L∕A∕R∕D', True],
              ['L⁄A⁄R⁄D', True],
              ['L•A•R•D', True],
              ['No P\u2060O\u2060R\u2060K', False],
              ['P∕O∕R∕K-free', False],
              ['Collard greens, basil', False],
              ['Rumex acetosa leaves', False]],
 'identity': [['decimal', 'Blend (1.5%)', 'Blend (15%)', False],
              ['range', 'Blend (2~3%)', 'Blend (23%)', False],
              ['plus', 'Blend (A+B)', 'Blend (AB)', False],
              ['percent', 'Blend (1%)', 'Blend (1)', False],
              ['underscore', 'Blend (A_B)', 'Blend (AB)', False],
              ['hash', 'Blend (Grade#1)', 'Blend (Grade1)', False],
              ['nested_origin', 'Blend (Gelatin[porcine])', 'Blend (Gelatin[bovine])', False],
              ['paren_empty', 'Water ()', 'Water', False],
              ['curly_component', 'Blend {A+B}', 'Blend {AB}', False],
              ['unicode_nfc', 'Café (A+B)', 'Café (A+B)', True],
              ['fullwidth_nested', 'Blend（A，B）', 'Blend(A,B)', True],
              ['outer_middle', 'Vitamin B·C', 'Vitamin BC', False],
              ['inner_bullet', 'Blend (A•B)', 'Blend (AB)', False],
              ['slash', 'Blend (A/B/C)', 'Blend (AB/C)', False],
              ['paren_case', 'Gelatin (BOVINE)', 'gelatin (bovine)', True],
              ['different_origin', 'Oil (국내산)', 'Oil (중국산)', False]],
 'problem_documents': [['expiry_passed', {'expiry_date': '2000-01-01'}, [], True],
                       ['expiry_invalid', {'expiry_date': 'not-a-date'}, [], True],
                       ['expiry_future', {'expiry_date': '2099-12-31'}, [], False],
                       ['scope_pending', {'_scope_pending': {'material_names': ['Pork']}}, [], True],
                       ['verify_ingredients',
                        {'_verify': {'verdict': 'warn',
                                     'field_issues': [{'field': 'ingredients', 'issue': 'mismatch'}]}},
                        [],
                        True],
                       ['verify_scope',
                        {'_verify': {'verdict': 'warn',
                                     'field_issues': [{'field': 'scope', 'issue': 'mismatch'}]}},
                        [],
                        True],
                       ['verify_cert_alias',
                        {'_verify': {'verdict': 'warn',
                                     'field_issues': [{'field': 'certificate_no',
                                                       'issue': 'mismatch'}]}},
                        [],
                        True],
                       ['verify_business_alias',
                        {'_verify': {'verdict': 'warn',
                                     'field_issues': [{'field': 'business_reg_no',
                                                       'issue': 'mismatch'}]}},
                        [],
                        True],
                       ['verify_factory_address',
                        {'_verify': {'verdict': 'warn',
                                     'field_issues': [{'field': 'factory_address',
                                                       'issue': 'different factory'}]}},
                        [],
                        True],
                       ['verify_product_names',
                        {'_verify': {'verdict': 'warn',
                                     'field_issues': [{'field': 'product_names',
                                                       'issue': 'different products'}]}},
                        [],
                        True],
                       ['verify_cosmetic_note',
                        {'_verify': {'verdict': 'warn',
                                     'field_issues': [{'field': 'note', 'issue': 'punctuation'}]}},
                        [],
                        False],
                       ['explicit_invalid', {'_cert_not_valid': 'revoked'}, [], True],
                       ['resolved_empty_scope', {'_scope_pending': {}}, [], False],
                       ['empty_issues', {'_verify': {'verdict': 'ok', 'field_issues': []}}, [], False],
                       ['clean_lead', {}, [['lead_ppm', 0.01, 'pass']], False],
                       ['bad_lead', {}, [['lead_ppm', 30, 'fail']], True],
                       ['bad_mercury', {}, [['mercury_ppm', 5, 'fail']], True],
                       ['uncertain_cadmium', {}, [['cadmium_ppm', None, 'review']], True],
                       ['manual_dna_negative', {}, [['pork_dna', 0, 'pass']], False],
                       ['empty_original_flag', {'_original_not_stored': False}, [], False]],
 'certificate_verification': [['model_Lot No',
                               {'cert_no': 'HC-666666', 'expiry_date': '2099-12-31'},
                               'HALAL CERTIFICATE\nLot No: HC-666666',
                               False],
                              ['model_Batch No',
                               {'cert_no': 'HC-666666', 'expiry_date': '2099-12-31'},
                               'HALAL CERTIFICATE\nBatch No: HC-666666',
                               False],
                              ['model_Reference No',
                               {'cert_no': 'HC-666666', 'expiry_date': '2099-12-31'},
                               'HALAL CERTIFICATE\nReference No: HC-666666',
                               False],
                              ['model_Application No',
                               {'cert_no': 'HC-666666', 'expiry_date': '2099-12-31'},
                               'HALAL CERTIFICATE\nApplication No: HC-666666',
                               False],
                              ['no_label_no_text',
                               {'cert_no': 'HC-666666', 'expiry_date': '2099-12-31'},
                               'HALAL CERTIFICATE\nIssued to PT SIX',
                               False],
                              ['correct_explicit',
                               {'cert_no': 'HC-666666', 'expiry_date': '2099-12-31'},
                               'HALAL CERTIFICATE\n'
                               'Certificate No: HC-666666\n'
                               'Issuer: BPJPH\n'
                               'Valid until: 2099-12-31',
                               True],
                              ['mismatch_explicit',
                               {'cert_no': 'HC-999999', 'expiry_date': '2099-12-31'},
                               'HALAL CERTIFICATE\n'
                               'Certificate No: HC-666666\n'
                               'Issuer: BPJPH\n'
                               'Valid until: 2099-12-31',
                               False],
                              ['status_revoked',
                               {'cert_no': 'HC-666666', 'expiry_date': '2099-12-31'},
                               'HALAL CERTIFICATE\n'
                               'Certificate No: HC-666666\n'
                               'Issuer: BPJPH\n'
                               'Valid until: 2099-12-31\n'
                               'Status: REVOKED',
                               False],
                              ['conditional_valid',
                               {'cert_no': 'HC-666666', 'expiry_date': '2099-12-31'},
                               'HALAL CERTIFICATE\n'
                               'Certificate No: HC-666666\n'
                               'Issuer: BPJPH\n'
                               'Valid until: 2099-12-31\n'
                               'Void if altered',
                               True],
                              ['expired',
                               {'cert_no': 'HC-666666', 'expiry_date': '2000-01-01'},
                               'HALAL CERTIFICATE\n'
                               'Certificate No: HC-666666\n'
                               'Issuer: BPJPH\n'
                               'Valid until: 2000-01-01',
                               False],
                              ['ocr_error',
                               {'cert_no': 'HC-666666',
                                'expiry_date': '2099-12-31',
                                '_ocr_error': 'CORRUPT'},
                               'HALAL CERTIFICATE\n'
                               'Certificate No: HC-666666\n'
                               'Issuer: BPJPH\n'
                               'Valid until: 2099-12-31',
                               False],
                              ['evidence_opt_out',
                               {'cert_no': 'HC-666666',
                                'expiry_date': '2099-12-31',
                                '_counts_as_evidence': False},
                               'HALAL CERTIFICATE\n'
                               'Certificate No: HC-666666\n'
                               'Issuer: BPJPH\n'
                               'Valid until: 2099-12-31',
                               False]]}


@pytest.mark.parametrize('name,text,wanted', GROUPS['quant'] + GROUPS['dna'], ids=lambda v: str(v)[:80])
def test_report_quant_and_dna(api, name, text, wanted):
    cid = new_case(api)
    upload(api, cid, text.encode(), 'CoA.txt')
    with m.SessionLocal() as db:
        verdict = m._case_quant_verdict(db, cid)
        blockers = m._quant_blockers(db, cid)
        assert bool(blockers) if wanted == 'blocked' else verdict == wanted
        if wanted in ('blocked', 'review', 'fail'):
            assert ok(api('GET', f'/cases/{cid}/problem-documents'))['count'] > 0


@pytest.mark.parametrize('name,filename,text,wanted', GROUPS['classification'])
def test_report_certificate_status(name, filename, text, wanted):
    assert intake.refine_doctype_reason(filename, None, text)[0] == wanted
    assert intake.parse_typed_text('halal_certificate', filename, text)['doc_type'] == wanted


@pytest.mark.parametrize('name,text,wanted', GROUPS['certificate_fields'])
def test_report_certificate_labels(name, text, wanted):
    assert intake.parse_typed_text('halal_certificate', 'Halal certificate.txt', text)['fields'].get('cert_no') == wanted


@pytest.mark.parametrize('name,key,value,text,warning', GROUPS['field_roles'])
def test_report_identifier_roles(name, key, value, text, warning):
    dtype = 'nib_business_license' if key == 'nib' else 'factory_registration' if key == 'factory_reg_no' else 'halal_certificate'
    result = intake.verify_extraction(dtype, 'document.txt', text, {key: value})
    assert (result['verdict'] != 'ok') is warning


@pytest.mark.parametrize('name,blocked', GROUPS['negative_context'] + GROUPS['spelling'])
def test_report_risk_negation(name, blocked):
    assert (screening.screen_merged(name)['result'] == 'BLOCK') is blocked
    assert (screening.screen_raw(name)['result'] == 'BLOCK') is blocked


@pytest.mark.parametrize('name,left,right,equal', GROUPS['identity'])
def test_report_material_identity(name, left, right, equal):
    assert (models.normalize_material_name(left) == models.normalize_material_name(right)) is equal


@pytest.mark.parametrize('name,fields,measurements,wanted', GROUPS['problem_documents'])
def test_report_problem_reasons(name, fields, measurements, wanted):
    doc = SimpleNamespace(document_id='f2', fields=fields, text_full=CERT_BODY, text_excerpt=None,
                          confidence=.9, content_b64='', filename='certificate.txt')
    measures = [SimpleNamespace(document_id='f2', param_key=k, value=v, verdict=w, note=None,
                               recorded_by='manual') for k, v, w in measurements]
    reasons = m._problem_document_reasons(doc, measures)
    assert bool(reasons) is wanted
    for reason in reasons:
        for lang in ('ko', 'en', 'id'):
            assert m._problem_document_explanation(reason, lang)


@pytest.mark.parametrize('name,fields,text,wanted', GROUPS['certificate_verification'])
def test_report_certificate_trust(name, fields, text, wanted):
    doc = {'doc_type': 'halal_certificate', 'filename': 'Halal certificate.txt', 'fields': fields,
           'text': text, 'review_status': 'approved'}
    assert (cert_verify.verify(fields['cert_no'], [doc])['verdict'] == 'VERIFIED') is wanted


@pytest.mark.parametrize('route', ['parse-file', 'intake-zip'])
@pytest.mark.parametrize('text,allowed', [
    ('Ethanol Result: 0.02%', True),
    ('Ethanol Result: 0.1%\nRepeat result: 3.2%', False),
    ('Ethanol Result: 0.1%\n\nResult: Pending', False),
    ('Ethanol Result: 0.1%\nTest status: Invalid', False),
    ('Ethanol Result: <0.1% +/- 3%', False),
    ('Positive control: Failed\nPorcine DNA PCR\nSample: Negative', False),
    ('Porcine DNA PCR: Negative\nRun status: Failed', False),
])
def test_upload_quant_blocks_issue(api, route, text, allowed):
    cid = new_case(api)
    upload(api, cid, pdf('CERTIFICATE OF ANALYSIS\n' + text), route=route)
    ready(api, cid)
    issue(api, cid, allowed)


@pytest.mark.parametrize('route', ['parse-file', 'intake-zip'])
def test_retest_xlsx_blocks_issue(api, route):
    cid = new_case(api)
    raw = workbook([['Parameter', 'Initial result (%)', 'Retest result (%)'], ['Ethanol', .1, 3.2]])
    upload(api, cid, raw, 'CoA.xlsx', route=route)
    ready(api, cid)
    issue(api, cid, False)


@pytest.mark.parametrize('extra,allowed', [('Void if altered', True), ('Status: REVOKED', False), ('Status: DRAFT', False), ('', True)])
def test_evidence_status_issue(api, extra, allowed):
    cid = new_case(api)
    mid = ok(api('POST', f'/cases/{cid}/materials', {'name': 'Gelatin'}))['material_id']
    evidence(api, cid, mid, CERT_BODY + '\n' + extra)
    ready(api, cid, [mid])
    issue(api, cid, allowed)


@pytest.mark.parametrize('same_number', [False, True])
def test_unrelated_material_same_filename_keeps_evidence(api, same_number):
    cid = new_case(api)
    mids = [ok(api('POST', f'/cases/{cid}/materials', {'name': name}))['material_id'] for name in ('Gelatin', 'Glycerin')]
    for i, mid in enumerate(mids):
        number = 'HC-111111' if same_number or i == 0 else 'HC-222222'
        evidence(api, cid, mid, CERT_BODY.replace('HC-666666', number) + '\nSupplier: ' + ('ALPHA' if i == 0 else 'BETA'))
    with m.SessionLocal() as db:
        assert all(m.sm.approved_material_evidence(db, db.get(models.Material, mid)) for mid in mids)
    ready(api, cid, mids)
    issue(api, cid, True)


@pytest.mark.parametrize('filename', ['Halal Certificate.pdf', 'renamed replacement.pdf'])
def test_new_pending_evidence_blocks_previous(api, filename):
    cid = new_case(api)
    mid = ok(api('POST', f'/cases/{cid}/materials', {'name': 'Gelatin'}))['material_id']
    old = evidence(api, cid, mid)
    evidence(api, cid, mid, CERT_BODY.replace('HC-666666', 'HC-777777'), filename, review=False)
    with m.SessionLocal() as db:
        assert not m.sm.approved_material_evidence(db, db.get(models.Material, mid))
    reasons = ok(api('GET', f'/cases/{cid}/problem-documents'))
    assert any(d['document_id'] == old for d in reasons['documents'])
    ready(api, cid, [mid])
    issue(api, cid, False)


@pytest.mark.parametrize('expiry', ['2000-01-01', 'not-a-date'])
def test_expiry_has_problem_guidance_and_blocks_issue(api, expiry):
    cid = new_case(api)
    mid = ok(api('POST', f'/cases/{cid}/materials', {'name': 'Gelatin'}))['material_id']
    evidence(api, cid, mid, CERT_BODY.replace('2099-12-31', expiry))
    assert ok(api('GET', f'/cases/{cid}/problem-documents'))['count'] > 0
    ready(api, cid, [mid])
    issue(api, cid, False)


@pytest.mark.parametrize('route', ['parse-file', 'intake-zip'])
@pytest.mark.parametrize('names', [['Blend(A_B)', 'Blend(AB)'], ['Blend(Grade#1)', 'Blend(Grade1)']])
def test_workbook_material_identifiers(api, route, names):
    rows = [['Daftar Bahan Halal'], ['No', 'Nama Bahan', 'Jenis Bahan', 'Produsen', 'Negara', 'No. Sertifikat Halal']]
    rows += [[i + 1, n, 'Bahan', 'PT LOCAL', 'ID', ''] for i, n in enumerate(names)]
    cid = new_case(api)
    upload(api, cid, workbook(rows), 'Daftar Bahan.xlsx', 'material_list', route)
    with m.SessionLocal() as db:
        assert {x.name for x in db.query(models.Material).filter_by(case_id=cid)} == set(names)


@pytest.mark.parametrize('role', ['Supplier', 'Manufacturer', 'Pemasok', '공급사'])
@pytest.mark.parametrize('route', ['parse-file', 'intake-zip'])
def test_supplier_identifier_not_autofilled(api, monkeypatch, role, route):
    fields = {'company_name': 'PT R7 F2', 'nib': '1234567890123'}
    monkeypatch.setattr(intake.ai_local, 'llm_json', lambda system, *a, **kw:
                        dict(OK) if '접수 검수자' in system else
                        {'doc_type': 'nib_business_license', 'fields': fields, 'confidence': .9} if '분류기' in system else dict(fields))
    cid = new_case(api)
    text = 'NOMOR INDUK BERUSAHA\nApplicant: PT R7 F2\n' + role + ': PT OTHER\nNIB: 1234567890123'
    upload(api, cid, text.encode(), 'NIB.txt', 'nib_business_license', route)
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        d = db.query(models.DocumentAsset).filter_by(case_id=cid).one()
        m._verify_doc(db, d, c)
        assert c.nib != '1234567890123'
        assert d.fields['_verify']['verdict'] != 'ok'


@pytest.mark.parametrize('fields', [{'nib': True}, {'nib': 1234567890123}, {'cert_no': True},
                                  {'employee_count': 'many'}, {'employee_count': -1}, {'signed': 'false'},
                                  {'expiry_date': True}, {'company_name': 123}, {'process_steps': True}])
def test_semantic_field_types_rejected(fields):
    normal, failure = intake._normalise_fields(fields)
    assert failure.get('llm_error') == 'BAD_RESPONSE'
    assert not normal


@pytest.mark.parametrize('fields', [{'city': 'Surabaya', 'country': 'Malaysia'}, {'establishment_date': '1999-01-01'},
                                  {'employee_count': 999}, {'company_name': 'Jakarta Selatan'}, {'phone': '+629991234'}])
def test_unsubstantiated_derived_values_warn(fields):
    text = 'NIB: 9120-0012-34567\nNama Perusahaan: PT. MAJU HALAL SEJAHTERA\nAlamat: Jl. Sudirman No. 24, Jakarta Selatan\nTelp (021) 555-1234'
    assert intake.verify_extraction('nib_business_license', 'NIB.txt', text, fields)['verdict'] != 'ok'


@pytest.mark.parametrize('fields', [{'nib': '9120001234567'}, {'company_name': 'PT MAJU HALAL SEJAHTERA'},
                                  {'address': 'Jalan Sudirman No. 24, Jakarta Selatan'}, {'phone': '+62215551234'}])
def test_equivalent_normalized_values_no_warning(fields):
    text = 'NIB: 9120-0012-34567\nNama Perusahaan: PT. MAJU HALAL SEJAHTERA\nAlamat: Jl. Sudirman No. 24, Jakarta Selatan\nTelp (021) 555-1234'
    assert intake.verify_extraction('nib_business_license', 'NIB.txt', text, fields)['verdict'] == 'ok'


@pytest.mark.parametrize('long_fields', [False, True])
def test_partial_llm_window_never_claims_full_verification(long_fields):
    text = ('filler\n' * 2000 + 'NIB: 7777777777777') if not long_fields else '\n'.join('Material%04d' % i for i in range(200))
    fields = {'nib': '7777777777777'} if not long_fields else {'material_names': text.splitlines()}
    result = intake.verify_extraction('nib_business_license' if not long_fields else 'material_list', 'input.txt', text, fields)
    assert result['verdict'] != 'ok'
    assert result.get('_truncated')


@pytest.mark.parametrize('change', [{'expiry_date': None}, {'expiry_date': 'bad'},
                                  {'_verify': {'verdict': 'warn', 'field_issues': [{'field': 'cert_no', 'issue': 'lot'}]}},
                                  {'_verify': {'verdict': 'unknown', 'llm_error': 'TIMEOUT'}}, {'_counts_as_evidence': False}])
def test_certificate_incomplete_or_warn_cannot_promote(change):
    fields = {'cert_no': 'HC-666666', 'expiry_date': '2099-12-31', **change}
    doc = {'doc_type': 'halal_certificate', 'fields': fields, 'text': CERT_BODY, 'review_status': 'approved'}
    assert cert_verify.verify('HC-666666', [doc])['verdict'] != 'VERIFIED'


def test_certificate_unapproved_cannot_promote():
    doc = {'doc_type': 'halal_certificate', 'fields': {'cert_no': 'HC-666666', 'expiry_date': '2099-12-31'},
           'text': CERT_BODY, 'review_status': 'pending'}
    assert cert_verify.verify('HC-666666', [doc])['verdict'] != 'VERIFIED'


def test_intake_standalone_geocoding_is_opt_in():
    env = dict(os.environ)
    env.pop('GLHAC_ZIP_LOOKUP', None)
    result = subprocess.run([sys.executable, '-c', 'from app import intake; assert not intake._ZIP_LOOKUP'],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('label', ['Lot No', 'Batch No', 'Reference No', 'Application No'])
def test_fresh_zip_llm_number_role_cannot_promote(api, monkeypatch, label):
    fields = {'cert_no': 'HC-888888', 'issuer': 'BPJPH', 'expiry_date': '2099-12-31'}
    monkeypatch.setattr(intake.ai_local, 'llm_json', lambda system, *a, **kw:
                        dict(OK) if '접수 검수자' in system else
                        {'doc_type': 'halal_certificate', 'fields': fields, 'confidence': .9} if '분류기' in system else dict(fields))
    cid = new_case(api)
    mid = ok(api('POST', f'/cases/{cid}/materials', {'name': 'Gelatin'}))['material_id']
    text = 'Attached statement\n' + label + ': HC-888888\nIssuer: BPJPH\nValid until: 2099-12-31'
    upload(api, cid, pdf(text), 'attachment.pdf', 'other', 'intake-zip')
    with m.SessionLocal() as db:
        d = db.query(models.DocumentAsset).filter_by(case_id=cid).one()
        did = d.document_id
        m._verify_doc(db, d, db.get(models.CaseApplication, cid))
    ok(api('PATCH', f'/documents/{did}/review', {'review_status': 'approved'}, 'auditor1'))
    changed = ok(api('PATCH', f'/materials/{mid}', {'cert_no': 'HC-888888'}))
    assert not changed.get('cert_no')
    assert changed.get('cert_promoted') is not True
    ready(api, cid, [mid])
    issue(api, cid, False)


def test_approval_rechecks_new_document_version(api):
    cid = new_case(api)
    mid = ok(api('POST', f'/cases/{cid}/materials', {'name': 'Gelatin'}))['material_id']
    evidence(api, cid, mid)
    ready(api, cid, [mid])
    aid = ok(api('POST', f'/cases/{cid}/certificate/issue', {}, 'operator1'))['approval_id']
    # 승인 대기 중 새 증빙 제출은 허용되지만, 이전 심사로 발급해서는 안 된다.
    evidence(api, cid, mid, CERT_BODY.replace('HC-666666', 'HC-999999'), review=False)
    response = api('POST', f'/approvals/{aid}/approve', {}, 'fatwa1')
    assert response.status_code == 409, response.text
    with m.SessionLocal() as db:
        assert not db.query(models.HalalCertificate).filter_by(case_id=cid).count()


@pytest.mark.parametrize('fields,text', [
    ({'country': 'Indonesia', 'city': 'Jakarta', 'zip': '12345'}, 'Address: Jakarta, Indonesia 12345'),
    ({'expiry_date': '2099-12-31'}, 'Valid until: 31/12/2099'),
    ({'employee_count': 20}, 'Employee count: 20'),
    ({'signed': False}, 'Unsigned application'),
])
def test_semantic_values_positive_controls(fields, text):
    assert intake._normalise_fields(fields)[1] == {}
    assert intake.verify_extraction('other', 'test.txt', text, fields)['verdict'] == 'ok'


@pytest.mark.parametrize('line', [
    'This certificate is revoked. Contact us if you have questions.',
    'This certificate is void; previous certificates are also void.',
])
def test_conditional_word_does_not_hide_current_revocation(line):
    assert intake.certificate_invalid_marker('Halal Certificate.pdf', CERT_BODY + '\n' + line)


def test_supplier_word_inside_company_name_is_not_owner_role():
    assert intake.autofill_value_in_text('nib', '1234567890123',
        'NOMOR INDUK BERUSAHA\nPT SUPPLIER LAIN\nNIB: 1234567890123')
    assert intake.autofill_value_in_text('nib', '1234567890123',
        'Supplier: PT OTHER\nNIB: 9999999999999\nApplicant: PT R7 F2\nNIB: 1234567890123')


def test_date_role_cannot_be_borrowed_from_another_field():
    result = intake.verify_extraction('halal_certificate', 'certificate.txt',
        'Issue date: 2099-01-01\nValid until: 2025-01-01', {'expiry_date': '2099-01-01'})
    assert result['verdict'] != 'ok'


@pytest.mark.parametrize('title', ['Sertifikat Halal', 'HALAL CERTIFICATE'])
def test_title_and_explicit_certificate_number_are_separate(title):
    text = title + '\nNo: OLD999\nCertificate No: HC-666666\nValid until: 2099-12-31'
    assert intake._deterministic_fields('halal_certificate', text, explicit_only=True)['cert_no'] == 'HC-666666'


def test_reviewed_hallucinated_certificate_cannot_support_issue(api):
    cid = new_case(api)
    mid = ok(api('POST', f'/cases/{cid}/materials', {'name': 'Gelatin'}))['material_id']
    did = evidence(api, cid, mid)
    with m.SessionLocal() as db:
        doc = db.get(models.DocumentAsset, did)
        doc.text_full = doc.text_excerpt = CERT_BODY.replace('Certificate No:', 'Lot No:')
        doc.fields = {'cert_no': 'HC-666666', 'expiry_date': '2099-12-31'}
        db.commit()
    ready(api, cid, [mid])
    issue(api, cid, False)


def test_reviewed_replacement_resolves_superseded_guidance(api):
    cid = new_case(api)
    mid = ok(api('POST', f'/cases/{cid}/materials', {'name': 'Gelatin'}))['material_id']
    old = evidence(api, cid, mid)
    evidence(api, cid, mid, CERT_BODY.replace('HC-666666', 'HC-999999'))
    documents = ok(api('GET', f'/cases/{cid}/problem-documents'))['documents']
    assert not any(d['document_id'] == old and any(r['code'] == 'EVIDENCE_SUPERSEDED' for r in d['reasons'])
                   for d in documents)
    ready(api, cid, [mid])
    issue(api, cid, True)
