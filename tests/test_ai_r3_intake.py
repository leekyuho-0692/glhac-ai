"""R3A: audit4의 정량 28건과 실제 표/OCR/검증 실패를 독립 재현한다.

감사 폴더는 읽기 전용이며 테스트 실행에는 필요하지 않다. 기존 기대값은 수정하지 않는다.
"""
import base64
import io
import json
import sys
import zipfile
from pathlib import Path

import fitz
import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import intake, models
import app.main as m


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(m, "_RL_WRITE_HITS", {})
    monkeypatch.setattr(m, "_AUTOPARSE", False)
    monkeypatch.setattr(m, "verify_enqueue", lambda *a, **kw: False)
    monkeypatch.setattr(intake, "_ZIP_LOOKUP", False)
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda *a, **kw: {"error": "LLM_UNAVAILABLE"})


@pytest.fixture
def case():
    with m.SessionLocal() as db:
        case = models.CaseApplication(org_id="org_demo", company_name="PT R3A")
        db.add(case)
        db.commit()
        yield db, case


def save_measurements(case, text, filename="CoA.txt", raw=None):
    db, c = case
    parsed = (intake.parse_typed("coa_msds", filename, raw) if raw is not None else
              intake.parse_typed_text("coa_msds", filename, text))
    doc = models.DocumentAsset(case_id=c.case_id, filename=filename, doc_type="coa_msds",
                               fields=parsed["fields"], text_full=parsed.get("text", text), confidence=.9)
    db.add(doc)
    db.flush()
    m._apply_document_measurements(db, c, doc)
    db.commit()
    db.expire_all()
    rows = db.query(models.MaterialMeasurement).filter_by(document_id=doc.document_id).all()
    return doc, rows


# results/new_edges.json의 quant 28개 입력·정답 그대로. 구간 메타를 포함한 저장 판정으로 검증한다.
QUANT_CASES = [
    ('ethanol_high_lod', 'Ethanol: ND (LOD 5%)', 'review'),
    ('ethanol_high_bound', 'Ethanol: < 5%', 'review'),
    ('lead_high_bound', 'Lead: < 10 ppm', 'review'),
    ('lead_high_lod', 'Lead: ND (LOD 5000 ppb)', 'review'),
    ('ethanol_low_lod', 'Ethanol: ND (LOD 0.01%)', 'pass'),
    ('ethanol_low_bound', 'Ethanol: < 0.1%', 'pass'),
    ('lead_low_ppb', 'Lead: 120 ppb', 'pass'),
    ('lead_high_ppb', 'Lead: 3000 ppb', 'fail'),
    ('ethanol_comma', 'Kadar alkohol: 1,5 %', 'fail'),
    ('cas_plain', 'Ethanol (CAS 64-17-5): 40 %', 'fail'),
    ('cas_dots', 'Ethanol (CAS 64-17-5) .... 40 %', 'fail'),
    ('result_after_spec', 'Parameter | Specification | Result\nEthanol | 0.5 % | 3.2 %', 'fail'),
    ('result_before_spec', 'Parameter | Result | Specification\nEthanol | 0.1 % | 0.5 %', 'pass'),
    ('spec_after_positive', 'Parameter | Specification | Result\nLead | 2 ppm | 12 ppm', 'fail'),
    ('two_ethanol_low_first', 'Sample A\nEthanol: 0.1%\nSample B\nEthanol: 3.2%', 'fail'),
    ('two_ethanol_high_first', 'Sample B\nEthanol: 3.2%\nSample A\nEthanol: 0.1%', 'fail'),
    ('two_lead_low_first', 'Lead: 1 ppm\nLead: 12 ppm', 'fail'),
    ('ethanol_gt', 'Ethanol: > 5%', 'fail'),
    ('ethanol_ge', 'Ethanol: ≥ 5%', 'fail'),
    ('ethanol_le', 'Ethanol: ≤ 0.1%', 'pass'),
    ('pork_negative_then_unclear', 'Porcine DNA PCR: Not detected\nPorcine DNA PCR: Inconclusive', 'review'),
    ('pork_unclear_then_negative', 'Porcine DNA PCR: Inconclusive\nPorcine DNA PCR: Not detected', 'review'),
    ('pork_negative_then_positive', 'Porcine DNA PCR: Not detected\nPorcine DNA PCR: Detected', 'review'),
    ('control_detected', 'Porcine DNA PCR: Negative control\nSample result: Detected', 'fail'),
    ('control_not_detected', 'Porcine DNA PCR: Positive control\nSample result: Not detected', 'pass'),
    ('unrelated_alcohol', 'Alcohol-free formula. Contains 12% glycerin', 'unknown'),
    ('unrelated_lead', 'Lead-free pigment contains 10% iron oxide', 'unknown'),
    ('number_next_line', 'Ethanol\n3.2 %', 'fail'),
]


@pytest.mark.parametrize('name,text,expected', QUANT_CASES, ids=[c[0] for c in QUANT_CASES])
def test_audit_quantitative_28(case, name, text, expected):
    db, c = case
    doc, rows = save_measurements(case, text)
    assert m._case_quant_verdict(db, c.case_id) == expected
    assert {m._measurement_verdict(row) for row in rows} <= {"pass", "fail", "review"}
    blockers = m._quant_blockers(db, c.case_id)
    assert bool(blockers) == (expected in ("fail", "review"))
    if blockers:
        assert blockers[0]["document_id"] == doc.document_id
        with pytest.raises(m.HTTPException) as exc:
            m._assert_measurements_pass(db, c.case_id)
        assert exc.value.status_code == 409


@pytest.mark.parametrize('expression,value,qualifier,lod,expected', [
    ("<0.5%", .5, "<", .5, "pass"), ("≤0.5%", .5, "≤", .5, "pass"),
    ("ND (LOD 0.5%)", .5, "ND", .5, "pass"), (">0.5%", .5, ">", None, "fail"),
    ("≥0.5%", .5, "≥", None, "review"), (">0.1%", .1, ">", None, "review"),
    ("<=5%", 5, "≤", 5, "review"), (">=5%", 5, "≥", None, "fail"),
    ("ND", None, "ND", None, "review"), ("<LOD", None, "<", None, "review"),
    ("<LOD (0.01%)", .01, "<", .01, "pass"), ("<0.01", .01, "<", .01, "pass"),
])
def test_interval_endpoints_and_persisted_metadata(case, expression, value, qualifier, lod, expected):
    db, c = case
    doc, rows = save_measurements(case, "Ethanol: " + expression)
    assert len(rows) == 1
    row = rows[0]
    assert row.value == value
    meta = json.loads(row.note)["measurement_meta"]
    assert meta["qualifier"] == qualifier and meta.get("lod") == lod
    assert row.verdict == m._measurement_verdict(row) == expected
    # DocumentAsset가 없어져도 측정 행 자체가 구간 판정의 근거를 보존한다.
    row.document_id = None
    db.commit()
    db.expire_all()
    assert m._measurement_verdict(row) == expected


@pytest.mark.parametrize('qualifier,lod,expected', [("<", 5, "review"), ("ND", 5, "review"), ("ND", .01, "pass"),
                                                ("<", None, "review"), ("ND", None, "review")])
def test_legacy_zero_pass_uses_document_metadata(case, qualifier, lod, expected):
    db, c = case
    meta = {"qualifier": qualifier, **({"lod": lod} if lod is not None else {})}
    doc = models.DocumentAsset(case_id=c.case_id, doc_type="coa_msds", filename="CoA.txt",
                               fields={"measurements": {"ethanol_pct": 0}, "measurements_meta": {"ethanol_pct": meta}})
    db.add(doc)
    db.flush()
    row = models.MaterialMeasurement(case_id=c.case_id, document_id=doc.document_id,
                                     param_key="ethanol_pct", value=0, verdict="pass", recorded_by="ai")
    db.add(row)
    db.commit()
    assert m._measurement_verdict(row) == expected
    assert m._apply_document_measurements(db, c, doc) == 1
    db.commit()
    db.refresh(row)
    assert row.value == lod
    assert row.verdict == expected


@pytest.mark.parametrize('extension', ['xlsx', 'pdf'])
def test_real_office_pdf_result_column(case, extension):
    if extension == 'xlsx':
        wb = Workbook()
        wb.active.append(['Parameter', 'Specification', 'Result'])
        wb.active.append(['Ethanol', '0.5 %', '3.2 %'])
        out = io.BytesIO()
        wb.save(out)
        wb.close()
        raw = out.getvalue()
    else:
        with fitz.open() as pdf:
            page = pdf.new_page()
            page.insert_text((50, 60), 'CERTIFICATE OF ANALYSIS\nParameter | Specification | Result\nEthanol | 0.5 % | 3.2 %')
            raw = pdf.tobytes()
    doc, rows = save_measurements(case, '', 'CoA_spec_result.' + extension, raw)
    assert [(row.value, row.verdict) for row in rows] == [(3.2, 'fail')]


@pytest.mark.parametrize('spec,result', [('Specification', 'Result'), ('Spec', 'Result'), ('Limit', 'Result'),
                                      ('Standard', 'Result'), ('Batas', 'Hasil'), ('Syarat', 'Hasil'),
                                      ('규격', '결과'), ('기준', '측정값')])
@pytest.mark.parametrize('reverse', [False, True])
def test_multilingual_result_headers(case, spec, result, reverse):
    header, values = ([spec, result], ['≤0.5%', '3.2%']) if not reverse else ([result, spec], ['3.2%', '≤0.5%'])
    _, rows = save_measurements(case, 'Parameter | ' + ' | '.join(header) + '\nEthanol | ' + ' | '.join(values))
    assert [(row.value, row.verdict) for row in rows] == [(3.2, 'fail')]


@pytest.mark.parametrize('text', ['Ethanol ≤0.5% | 3.2%', 'Ethanol max 0.5% | 3.2%',
                                'Ethanol maks 0.5% | 3.2%', 'Ethanol 0.5% 이하 | 3.2%',
                                'Ethanol 3.2% | ≤0.5%', 'Ethanol Specification ≤0.5% | Result 3.2%',
                                'Ethanol 0.5% max | 3.2%', 'Ethanol 0.5% maks | 3.2%',
                                'Ethanol (CAS 64-17-5) ≤0.5% | Result 3.2%',
                                'Ethanol (CAS 64-17-5) .... max 0.5% | Result 3.2%'])
def test_unheaded_specification_is_not_a_result(case, text):
    _, rows = save_measurements(case, text)
    assert [(row.value, row.verdict) for row in rows] == [(3.2, 'fail')]


@pytest.mark.parametrize('text', ['Ethanol: 0.1%\nEthanol: 3.2%', 'Ethanol: 3.2%\nEthanol: 0.1%',
                                'Ethanol: 0.1%; Ethanol: 3.2%',
                                'Parameter | Result | Result\nEthanol | 0.1% | 3.2%'])
def test_all_results_survive_idempotent_reprocessing(case, text):
    db, c = case
    doc, rows = save_measurements(case, text)
    assert sorted(row.value for row in rows) == [.1, 3.2]
    ids = {row.measurement_id for row in rows}
    assert m._apply_document_measurements(db, c, doc) == 2
    db.commit()
    after = db.query(models.MaterialMeasurement).filter_by(document_id=doc.document_id).all()
    assert {row.measurement_id for row in after} == ids
    assert m._case_quant_verdict(db, c.case_id) == 'fail'


@pytest.mark.parametrize('text,expected', [('Ethanol: 0.1%\nGlycerin 12%', {'ethanol_pct': .1}),
    ('Ethanol: 0.1% Glycerin 12%', {'ethanol_pct': .1}),
    ('Parameter | Specification | Result\nLead-free | 2ppm | 12ppm', {})])
def test_other_analytes_remain_out_of_result_scope(text, expected):
    assert intake.extract_measurements(text) == expected


@pytest.mark.parametrize('word', ['Inconclusive', 'Invalid', 'Equivocal', 'Tidak dapat ditentukan', '판정불가'])
@pytest.mark.parametrize('layout', ['block', 'separate', 'table', 'explicit_sample'])
def test_unresolved_dna_always_requires_review(case, word, layout):
    if layout == 'block':
        text = 'Porcine DNA PCR: Not detected\nResult: ' + word
    elif layout == 'separate':
        text = 'Porcine DNA PCR: Not detected\nPorcine DNA PCR: ' + word
    elif layout == 'explicit_sample':
        text = 'Porcine DNA PCR: ' + word + '\nSample result: Not detected'
    else:
        text = 'Test | Sample | Control\nPorcine DNA PCR | Not detected | Positive\nPorcine DNA PCR | ' + word + ' | Positive'
    _, rows = save_measurements(case, text)
    assert [(row.value, row.verdict) for row in rows] == [(None, 'review')]


@pytest.mark.parametrize('title', ['Formulir Permohonan Sertifikat Halal', 'Application for Halal Certification',
                                 '할랄인증 신청서', 'Proposal for halal certification',
                                 '할랄 인증 견적서', 'Halal certificate request form'])
def test_application_does_not_become_certificate(title):
    text = title + '\nCertificate No: ID12345\nIssuer: BPJPH\nValid until: 2027-01-01'
    for result in (intake.classify('Halal certificate.txt', text),
                   intake.parse_typed_text('halal_certificate', 'Halal certificate.txt', text)):
        assert result['doc_type'] == 'other'
        assert not intake.aggregate_fields([result])['certificates']


OCR_TEXT = 'SURAT KETERANGAN HALAL No: ID00110012345678 PT SUPPLIER GELATIN Berlaku sampai 2027-01-01'


@pytest.mark.parametrize('text', [OCR_TEXT, OCR_TEXT + '\n',
    'SERTIFIKAT HALAL No. Sertifikat: ID00110012345678 Berlaku s/d 2027-01-01 BPJPH'])
def test_actual_one_line_ocr_fields(text):
    for result in (intake.classify('Sertifikat Halal.png', text),
                   intake.parse_typed_text('halal_certificate', 'Sertifikat Halal.png', text)):
        assert result['fields']['cert_no'] == 'ID00110012345678'
        assert result['fields']['expiry_date'] == '2027-01-01'
        assert result['confidence'] == .9


@pytest.mark.parametrize('body,count', [('HALAL CERTIFICATE', 0), ('Certificate No: ID123', 1),
    ('Certificate No: ID123\nIssuer: BPJPH', 2), ('Issuer: BPJPH\nValid until: 2027-01-01', 2)])
def test_certificate_confidence_requires_two_evidence_fields(body, count):
    result = intake.classify('Halal Certificate.txt', body)
    assert (result['confidence'] == .9) == (count >= 2)
    if count < 2:
        assert result['confidence'] < .5
    assert result['fields']['_missing_fields']
    doc = models.DocumentAsset(filename='Halal Certificate.txt', doc_type='halal_certificate', text_full=body,
                               fields=result['fields'], confidence=result['confidence'])
    reasons = m._problem_document_reasons(doc)
    assert 'FIELDS_MISSING' in {r['code'] for r in reasons}
    if count < 2:
        assert 'LOW_CONFIDENCE' in {r['code'] for r in reasons}
    for lang in ('ko', 'en', 'id'):
        assert all(m._problem_document_explanation(r, lang) for r in reasons)


@pytest.mark.parametrize('route', ['parse-file', 'intake-zip'])
@pytest.mark.parametrize('title', ['Formulir Permohonan Sertifikat Halal', 'Application for Halal Certification', '할랄인증 신청서'])
def test_application_upload_does_not_satisfy_certificate_checklist(route, title):
    with TestClient(m.app) as client:
        token = client.post('/auth/login', json={'username': 'applicant1', 'password': 'pw'}).json()['token']
        headers = {'Authorization': 'Bearer ' + token}
        cid = client.post('/cases', headers=headers, json={'company_name': 'PT R3A'}).json()['case_id']
        raw = (title + '\nPlease complete applicant information.').encode()
        if route == 'intake-zip':
            out = io.BytesIO()
            with zipfile.ZipFile(out, 'w') as archive:
                archive.writestr('Halal certificate.txt', raw)
            payload = {'zip_b64': base64.b64encode(out.getvalue()).decode()}
        else:
            payload = {'filename': 'Halal certificate.txt', 'doc_type': 'halal_certificate',
                       'file_b64': base64.b64encode(raw).decode()}
        response = client.post(f'/cases/{cid}/{route}', headers=headers, json=payload)
        assert response.status_code == 200, response.text
        docs = client.get(f'/cases/{cid}/documents', headers=headers).json()
        assert [d['doc_type'] for d in docs] == ['other']
        checklist = client.get(f'/cases/{cid}/doc-checklist', headers=headers).json()['checklist']
        assert not next(row for row in checklist if row['doc_type'] == 'halal_certificate')['satisfied']


@pytest.mark.parametrize('error', ['BOMB', 'CORRUPT', 'TIMEOUT'])
def test_unreadable_certificate_uses_parse_reason(error):
    text = intake._ParsedText('', {'_ocr_error': error})
    result = intake.parse_typed_text('quality_cert', 'HACCP.pdf', text)
    assert '_missing_fields' not in result['fields']


def test_completed_180_item_extraction_clears_only_resolved_limit(monkeypatch):
    names = ['IngredientAudit%03d' % i for i in range(180)]
    text = 'DAFTAR BAHAN\n' + '\n'.join(names)
    def fake(system, user):
        selected = [name for name in names if name in user]
        return ({'doc_type': 'material_list', 'confidence': .9, 'fields': {'material_names': selected}}
                if '분류기' in system else {'names': selected})
    monkeypatch.setattr(intake.ai_local, 'llm_json', fake)
    result = intake.classify('Daftar Bahan.txt', text)
    assert result['fields']['material_names'] == names
    assert '_truncated' not in result['fields']
    parsed = intake._ParsedText(text, {'_truncated': {'kind': 'pages', 'limit': 40, 'total': 60}})
    result = intake.classify('Daftar Bahan.txt', parsed)
    assert result['fields']['_truncated']['kind'] == 'pages'


def test_failed_followup_chunk_keeps_truncation(monkeypatch):
    text = 'DAFTAR BAHAN\n' + 'Ingredient\n' * 300
    monkeypatch.setattr(intake.ai_local, 'llm_json', lambda system, user:
                        {'doc_type': 'material_list', 'confidence': .9, 'fields': {}} if '분류기' in system else {'error': 'TIMEOUT'})
    assert intake.classify('Daftar Bahan.txt', text)['fields']['_truncated']['kind'] == 'llm_input'


@pytest.mark.parametrize('response', [{'verdict': 'ok', 'field_issues': 7},
    {'verdict': 'ok', 'field_issues': ['wrong']}, {'verdict': 'ok', 'field_issues': [{'field': 'x', 'issue': 7}]},
    {'verdict': 'ok', 'doc_type_ok': []}, {'verdict': 'wrong'}, None])
def test_invalid_verification_is_persisted_and_reported(case, monkeypatch, response):
    db, c = case
    doc = models.DocumentAsset(case_id=c.case_id, filename='HACCP.txt', doc_type='quality_cert',
                               text_full='HACCP CERTIFICATE\nCertificate No: Q12345', fields={}, confidence=.9)
    db.add(doc)
    db.commit()
    monkeypatch.setattr(intake.ai_local, 'llm_json', lambda *a, **kw: response)
    m._verify_doc(db, doc, c)
    db.refresh(doc)
    assert doc.fields['_verify']['llm_error'] == 'BAD_RESPONSE'
    assert 'VERIFY_ERROR' in {r['code'] for r in m._problem_document_reasons(doc)}


@pytest.mark.parametrize('response,expected_issues,doc_ok', [
    ({'verdict': 'ok', 'field_issues': None}, [], None),
    ({'verdict': 'ok', 'field_issues': {'company_name': 'wrong'}}, [{'field': 'company_name', 'issue': 'wrong'}], None),
    ({'verdict': 'ok', 'doc_type_ok': 'false'}, [], False),
])
def test_recoverable_verification_types_are_normalised(monkeypatch, response, expected_issues, doc_ok):
    monkeypatch.setattr(intake.ai_local, 'llm_json', lambda *a, **kw: response)
    result = intake.normalise_verification(response)
    assert result['field_issues'] == expected_issues and result['doc_type_ok'] is doc_ok
    assert 'llm_error' not in result
    if expected_issues or doc_ok is False:
        assert result['verdict'] == 'warn'


def test_verification_exception_is_not_silently_lost(case, monkeypatch):
    db, c = case
    doc = models.DocumentAsset(case_id=c.case_id, filename='CoA.txt', doc_type='coa_msds',
                               text_full='Certificate of Analysis', fields={}, confidence=.9)
    db.add(doc)
    db.commit()
    def malformed(*a, **kw):
        raise TypeError('invalid verification shape')
    monkeypatch.setattr(intake, 'verify_extraction', malformed)
    m._verify_doc(db, doc, c)
    db.refresh(doc)
    assert doc.fields['_verify']['llm_error'] == 'BAD_RESPONSE'


@pytest.mark.parametrize('long_fields,long_text,expected_limits', [(True, False, {1500}), (False, True, {8000}), (True, True, {1500, 8000})])
def test_verification_input_limits_survive_database(case, monkeypatch, long_fields, long_text, expected_limits):
    db, c = case
    names = ['Salt%d' % i for i in range(1100)]
    text = '\n'.join(names) if long_text else 'Salt'
    fields = {'material_names': names} if long_fields else {}
    doc = models.DocumentAsset(case_id=c.case_id, filename='Daftar Bahan.txt', doc_type='material_list',
                               text_full=text, fields=fields, confidence=.95)
    db.add(doc)
    db.commit()
    monkeypatch.setattr(intake.ai_local, 'llm_json', lambda *a, **kw: {'verdict': 'ok', 'field_issues': []})
    m._verify_doc(db, doc, c)
    db.refresh(doc)
    markers = doc.fields.get('_truncations', [doc.fields['_truncated']])
    assert {marker['limit'] for marker in markers if marker['kind'] == 'verify_input'} == expected_limits
    assert 'TRUNCATED' not in {r['code'] for r in m._problem_document_reasons(doc)}
    assert {w['truncated']['limit'] for w in m._document_parse_warnings(doc)} == expected_limits
    assert all(w['severity'] == 'info' for w in m._document_parse_warnings(doc))
    doc.text_full, doc.fields = 'Salt', {'material_names': ['Salt'], '_truncated': doc.fields['_truncated']}
    db.commit()
    m._verify_doc(db, doc, c)
    db.refresh(doc)
    assert '_truncated' not in doc.fields


@pytest.mark.parametrize('body,expected', [('Ethanol: <5%', 'review'), ('Ethanol: >5%', 'fail'),
                                        ('Ethanol: ND (LOD 5%)', 'review')])
def test_upload_and_report_use_interval_verdict(body, expected):
    with TestClient(m.app) as client:
        token = client.post('/auth/login', json={'username': 'applicant1', 'password': 'pw'}).json()['token']
        headers = {'Authorization': 'Bearer ' + token}
        cid = client.post('/cases', headers=headers, json={'company_name': 'PT R3A'}).json()['case_id']
        response = client.post(f'/cases/{cid}/parse-file', headers=headers,
            json={'filename': 'CoA.txt', 'doc_type': 'coa_msds', 'file_b64': base64.b64encode(body.encode()).decode()})
        assert response.status_code == 200, response.text
        measurements = client.get(f'/cases/{cid}/measurements', headers=headers).json()
        report = client.get(f'/cases/{cid}/material-report', headers=headers).json()
        assert measurements['verdict'] == report['quant_verdict'] == expected
        assert measurements['items'][0]['value'] == 5
        assert measurements['items'][0]['verdict'] == expected
