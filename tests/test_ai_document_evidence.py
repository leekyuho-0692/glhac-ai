"""V1~V3: 전체 원문 AI 추출/규칙 검산. 실 API와 고객 원본 사용 없음."""
import base64
from concurrent.futures import ThreadPoolExecutor
import copy
import time

import fitz
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from app import intake, ai_local, schemas
from app.document_extraction import company_key, document_chunks, INPUT_LIMITS
import app.main as m

TEXT = ("사업자등록증명\n등록번호:638-88-01141\n법인명(단체명):주식회사샘플코스\n"
        "대표자:김샘플\n사업장소재지:경기도 샘플시 샘플로 10\n본 점\n"
        "개 업 연 월 일:2018 년 06 월 28 일\n법인등록번호:120111-0944472\n"
        "샘플세무서\n담당부서:민원실\n담당자:이담당\n연락처:+82-32-670-9225")
FIELDS = {"company_name": "주식회사샘플코스", "nib": "638-88-01141",
          "address": "경기도 샘플시 샘플로 10", "establishment_date": "2018-06-28",
          "corporate_reg_no": "120111-0944472"}
EVIDENCE = {"company_name": "법인명(단체명):주식회사샘플코스", "nib": "등록번호:638-88-01141",
            "address": "사업장소재지:경기도 샘플시 샘플로 10",
            "establishment_date": "개 업 연 월 일:2018 년 06 월 28 일",
            "corporate_reg_no": "법인등록번호:120111-0944472"}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(ai_local, "provider", lambda: "openai")
    monkeypatch.setattr(ai_local, "_openai_chat", lambda *a, **k: pytest.fail("실 API 호출 금지"))
    monkeypatch.setattr(ai_local, "llm_json", lambda *a, **k: pytest.fail("가짜 LLM 응답 설정 필요"))
    monkeypatch.setattr(m, "_OCR_EXTRACT_CACHE", m.OrderedDict())
    monkeypatch.setattr(m, "_RL_WRITE_HITS", {})
    monkeypatch.setattr(m.auth, "check_username_rate_limited", lambda ip: False)
    monkeypatch.setattr(m.auth, "record_username_check", lambda ip: None)


def response(**fields):
    return {"doc_type": "nib_business_license", "confidence": .95,
            "fields": fields or dict(FIELDS), "evidence": dict(EVIDENCE)}


def test_ai_replaces_suspect_rule_and_never_fills_ai_omissions(monkeypatch):
    monkeypatch.setattr(ai_local, "llm_json", lambda *a, **k: response())
    rules = {"company_name": "(단체명):", "address": "경기도 샘플시 샘플로 10 본 점", "responsible_person": "김샘플"}
    r = intake._extract_ai_document("사업자등록증명.pdf", TEXT, rules=rules)
    assert {k: v for k, v in r['fields'].items() if not k.startswith('_')} == FIELDS
    for key in ('company_name', 'address'):
        meta = r['fields']['_field_meta'][key]
        assert meta['source'] == 'ai' and meta['review']
        assert meta['checks']['rule_match'] is False
        assert 'suspect_rule' in meta['reasons']
    assert 'responsible_person' not in r['fields']


@pytest.mark.parametrize('values,quote,key', [
    ({'address': '서울 가짜로 999'}, '서울 가짜로 999', 'address'),
    ({'address': '경기도 샘플시 샘플로 10'}, '서울 가짜로 999', 'address'),
    ({'nib': '1234567890123'}, '등록번호:638-88-01141', 'nib'),
    ({'company_name': '(단체명):'}, '법인명(단체명):주식회사샘플코스', 'company_name'),
    ({'phone': '+82-32-670-9225'}, '연락처:+82-32-670-9225', 'phone'),
])
def test_ungrounded_invalid_or_issuer_values_are_rejected(monkeypatch, values, quote, key):
    monkeypatch.setattr(ai_local, 'llm_json', lambda *a, **k: {**response(**values), 'evidence': {key: quote}})
    r = intake.parse_typed_text('nib_business_license', '사업자등록증명.pdf', TEXT)
    assert key not in r['fields']
    assert not r['fields']['_field_meta'][key]['accepted']
    assert r['fields']['_field_meta'][key]['review']


@pytest.mark.parametrize('failure', ['none', '429', 'TIMEOUT', 'raise', 'malformed'])
def test_only_unavailable_llm_uses_rule_fallback(monkeypatch, failure):
    calls = []
    def fake(*args, **kw):
        calls.append(1)
        if failure == 'raise': raise TimeoutError('offline')
        return None if failure == 'malformed' else {'error': failure}
    monkeypatch.setattr(ai_local, 'llm_json', fake)
    if failure == 'none': monkeypatch.setattr(ai_local, 'provider', lambda: 'none')
    r = intake._extract_ai_document('등록증.pdf', TEXT, doc_type='nib_business_license')
    assert r['fields']['company_name'] == FIELDS['company_name']
    assert all(x['source'] == 'fallback' and x['review'] for x in r['fields']['_field_meta'].values())
    assert 'phone' not in r['fields']
    assert len(calls) == (0 if failure == 'none' else 1)


@pytest.mark.parametrize('stage', ['classify', 'parse', 'signup'])
@pytest.mark.parametrize('length', [2597, 39000, 85000])
def test_full_input_within_budget_and_explicit_partial_input_above_budget(monkeypatch, stage, length):
    text = ('0123456789' * (length // 10)) + '\n마지막원문값'
    calls = []
    monkeypatch.setattr(ai_local, 'llm_json', lambda system, user: calls.append(user) or
                        {'doc_type': 'other', 'fields': {}, 'evidence': {}})
    result = intake._extract_ai_document('scan.pdf', text, stage=stage)
    from app.document_extraction import PUBLIC_AI_MAX_CALLS, DOCUMENT_AI_MAX_CALLS
    chunks = document_chunks(text, 'openai', stage,
                             max_chunks=PUBLIC_AI_MAX_CALLS if stage == 'signup' else DOCUMENT_AI_MAX_CALLS - 1)
    assert len(calls) == len(chunks)
    assert all(chunk in call for chunk, call in zip(chunks, calls))
    assert '마지막원문값' in calls[-1]
    assert max(map(len, chunks)) <= INPUT_LIMITS['openai'][stage]
    if length < 40000: assert text in calls[0]
    assert bool(result['fields'].get('_analysis', {}).get('partial')) is chunks.partial
    if chunks.partial:
        assert result['fields']['_classification']['review']


def test_local_limits_remain_visible(monkeypatch):
    monkeypatch.setattr(ai_local, 'provider', lambda: 'ollama')
    calls = []
    monkeypatch.setattr(ai_local, 'llm_json', lambda s, u: calls.append(u) or response())
    r = intake.classify('등록증.pdf', TEXT + 'Z' * 5000)
    assert len(calls) == 1
    assert r['fields']['_truncated']['limit'] == 2000
    assert all(x['review'] for x in r['fields']['_field_meta'].values())


def test_verify_full_tail_dates_and_corporate_forms_ignores_opinion(monkeypatch):
    calls = []
    def verify(system, user):
        calls.append(user)
        return {'verdict': 'fail', 'doc_type_ok': True, 'field_issues': [
            {'field': 'corporate_reg_no', 'issue': 'not typically included in a business license'},
            {'field': 'company_name', 'issue': 'corporate form mismatch'}]}
    monkeypatch.setattr(ai_local, 'llm_json', verify)
    text = '안내문\n' * 3000 + TEXT
    result = intake.verify_extraction('nib_business_license', '사업자등록증명.pdf', text, FIELDS, '샘플코스')
    assert text in calls[0]
    assert result['field_issues'] == []
    assert result['advisory'] and not result['applicant_action_required']
    assert result['verdict'] != 'fail'
    assert '_truncated' not in result


@pytest.mark.parametrize('left,right', [('주식회사닥터제이코스','닥터제이코스'), ('㈜ 샘플','샘플'),
    ('(주) 샘플','샘플'), ('유한회사 샘플','샘플'), ('PT. Sample','Sample'), ('CV Sample','Sample'),
    ('Sample Co., Ltd.','Sample')])
def test_company_forms_compare_equal(left,right):
    assert company_key(left) == company_key(right)
    assert company_key(left) != company_key('다른업체')


@pytest.mark.parametrize('text,number,valid', [
    ('사업자등록증\n등록번호:638-88-01141','6388801141', True),
    ('사업자등록증\n등록번호:638 88 01141','638-88-01141', True),
    ('Nomor Induk Berusaha:1234567890123','1234567890123', True),
    ('Nomor Induk Berusaha:1234567890','1234567890', False),
    ('사업자등록증\n법인등록번호:120111-0944472','1201110944472', False),
])
def test_country_specific_identifiers(text, number, valid):
    assert intake.autofill_value_in_text('nib', number, text) is valid


@pytest.mark.parametrize('name,wrong', [('제품설명서_샘플.pdf','supplier_declaration'),
    ('Product Specification.pdf','supplier_declaration'), ('6. 샘플 사진.pdf','product_list')])
def test_product_references_never_satisfy_wrong_catalog(monkeypatch, name, wrong):
    monkeypatch.setattr(ai_local, 'llm_json', lambda *a, **k: {'doc_type': wrong,
                        'fields': {'product_names': ['샘플']}, 'evidence': {'product_names': ['샘플']}})
    r = intake.classify(name, 'Product Specification\n샘플')
    assert r['doc_type'] == 'other'
    assert r['fields']['_classification']['review']
    assert not intake.aggregate_fields([r])['products']


def pdf_body():
    with fitz.open() as doc:
        doc.new_page()
        return schemas.OCRExtractReq(image_b64=base64.b64encode(doc.tobytes()).decode())


def request():
    return Request({'type':'http','headers':[],'client':('127.0.0.1',1234)})


def test_same_file_hash_cache_prevents_repeated_and_concurrent_ai(monkeypatch):
    calls = []
    monkeypatch.setattr(intake, 'parse_file', lambda *a, **k: TEXT)
    def fake(*a, **kw):
        calls.append(1)
        time.sleep(.02)
        return response()
    monkeypatch.setattr(ai_local, 'llm_json', fake)
    body = pdf_body()
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: m.auth_ocr_extract(body, request()), range(3)))
    assert len(calls) == 1
    assert sum(r['cached'] for r in results) == 2
    assert results[0]['field_sources']['company_name'] == 'ai'
    results[0]['fields']['company_name'] = 'mutated'
    assert m.auth_ocr_extract(body, request())['fields']['company_name'] == FIELDS['company_name']


def test_ip_rate_limit_returns_reviewed_fallback_without_ai(monkeypatch):
    monkeypatch.setattr(m.auth, 'check_username_rate_limited', lambda ip: True)
    monkeypatch.setattr(intake, 'parse_file', lambda *a, **k: TEXT)
    r = m.auth_ocr_extract(pdf_body(), request())
    assert r['fields']['company_name'] == FIELDS['company_name']
    assert r['field_sources']['company_name'] == 'fallback' and r['review']
    assert not m._OCR_EXTRACT_CACHE


def test_chunk_merging_and_conflicts_are_explicit(monkeypatch):
    text = 'company_name: First\n' + 'x' * 40000 + '\ncompany_name: Last'
    seq = iter([{'doc_type':'other','fields':{'company_name':'First'},'evidence':{'company_name':'company_name: First'}},
                {'doc_type':'other','fields':{'company_name':'Last'},'evidence':{'company_name':'company_name: Last'}}])
    monkeypatch.setattr(ai_local, 'llm_json', lambda *a, **k: next(seq))
    r = intake.classify('scan.pdf', text)
    assert 'chunk_conflict' in r['fields']['_field_meta']['company_name']['reasons']


def test_verify_chunks_cover_the_tail_and_do_not_forward_opinion(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_local, 'llm_json', lambda s, u: calls.append(u) or
                        {'verdict': 'ok', 'doc_type_ok': True, 'field_issues': []})
    text = 'line\n' * 17000 + TEXT
    r = intake.verify_extraction('nib_business_license','등록증.pdf',text,FIELDS,'샘플코스')
    assert len(calls) >= 3 and '120111-0944472' in calls[-1]
    assert not r['field_issues'] and not r.get('_truncated')


def test_issuer_contact_in_english_is_not_business_phone(monkeypatch):
    text = 'Certificate of Business Registration\nTax Office\nTelephone: +82-32-670-9225'
    assert not intake._field_value_grounded('phone','+82-32-670-9225',text)
    assert intake._field_value_grounded('phone','+82-32-670-9225',text+'\nCompany phone: +82-32-670-9225')


def test_signed_receipt_expiry_and_domain_separation(monkeypatch):
    from app import document_extraction as ex
    meta = {'company_name': {'source':'ai','review':True}}
    token = ex.extraction_receipt(FIELDS,meta,m.auth.SECRET)
    assert ex.receipt_metadata(token,FIELDS,m.auth.SECRET) == meta
    assert not m.auth.verify_token(token)
    now = time.time()
    monkeypatch.setattr(ex.time,'time',lambda:now+1801)
    assert ex.receipt_metadata(token,FIELDS,m.auth.SECRET) == {}


@pytest.mark.parametrize('filename,selected,detected,text', [
    ('unknown.txt','nib_business_license','coa_msds','MATERIAL SAFETY DATA SHEET\nSample'),
    ('제품설명서_샘플.txt','supplier_declaration','supplier_declaration','Product Specification\nSample'),
    ('제품 사진.txt','product_list','product_list','Product photos\nSample'),
])
def test_upload_persists_ai_type_and_review_metadata(monkeypatch, filename, selected, detected, text):
    monkeypatch.setattr(m,'_AUTOPARSE',False)
    monkeypatch.setattr(m,'verify_enqueue',lambda *a:False)
    monkeypatch.setattr(ai_local,'llm_json',lambda *a, **k: {'doc_type':detected,'confidence':.9,'fields':{},'evidence':{}})
    with TestClient(m.app) as client:
        token=client.post('/auth/login',json={'username':'applicant1','password':'pw'}).json()['token']
        headers={'Authorization':'Bearer '+token}
        cid=client.post('/cases',headers=headers,json={'company_name':'Integration sample'}).json()['case_id']
        r=client.post(f'/cases/{cid}/parse-file',headers=headers,json={'filename':filename,'doc_type':selected,
                     'file_b64':base64.b64encode(text.encode()).decode()})
        assert r.status_code==200,r.text
        expected='coa_msds' if filename=='unknown.txt' else 'other'
        assert r.json()['doc_type']==expected
        [doc]=client.get(f'/cases/{cid}/documents',headers=headers).json()
        assert doc['doc_type']==expected
        assert doc['fields']['_classification']['source'] in ('ai','rule')
        assert not any(v for k,v in doc['fields'].items() if not k.startswith('_'))


def test_actual_style_multiline_date_and_business_taxpayer_label():
    text=('사업자등록증명\n사\n업\n자\n등\n록\n번\n호\nBusiness Taxpayer ID Number\n638-88-01141\n'
          '개\n업\n일\nD a t e\no f\nB u s i n e s s\nS t a r t\n2018년(Year) 06월(Month) 28일(Day)')
    assert intake._field_value_grounded('establishment_date','2018-06-28',text)
    assert intake._field_value_grounded('nib','638-88-01141',text)


def test_auditor_notes_are_not_in_applicant_document_response():
    fields={'_verify':{'verdict':'warn','field_issues':[],'advisory':[{'field':'company_name','issue':'opinion'}]}}
    assert 'advisory' not in m._document_visible_fields(fields,'applicant')['_verify']
    assert m._document_visible_fields(fields,'auditor')['_verify']['advisory']
    assert fields['_verify']['advisory']


@pytest.mark.parametrize('registered,remains', [('샘플코스', False), ('다른업체', True)])
def test_reverification_clears_only_legal_form_false_name_warning(monkeypatch, registered, remains):
    monkeypatch.setattr(ai_local, 'llm_json', lambda *a, **k:
                        {'verdict': 'ok', 'doc_type_ok': True, 'field_issues': []})
    with TestClient(m.app) as client:
        token = client.post('/auth/login', json={'username':'applicant1','password':'pw'}).json()['token']
        cid = client.post('/cases', headers={'Authorization':'Bearer '+token},
                          json={'company_name':registered}).json()['case_id']
        with m.SessionLocal() as db:
            case = db.get(m.models.CaseApplication, cid)
            doc = m.models.DocumentAsset(case_id=cid, filename='사업자등록증명.pdf',
                doc_type='nib_business_license', text_full=TEXT, fields={**FIELDS,
                    '_verify': {'verdict':'warn','field_issues':[{'field':'company_name',
                        'code':'company_name_mismatch','issue':'previous name mismatch'}]}})
            db.add(doc)
            db.commit()
            m._verify_doc(db, doc, case)
            assert any(x.get('code') == 'company_name_mismatch'
                       for x in doc.fields['_verify']['field_issues']) is remains
