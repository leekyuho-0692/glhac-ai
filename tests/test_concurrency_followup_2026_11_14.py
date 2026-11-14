"""독립 세션·스레드에서 먼저 커밋한 별칭과 문서 확인 이력을 보존한다."""
import base64
import copy
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import intake, main as m, models


OWNER = 'PT Concurrent Profile'
NIB = '9120001234567'
ADDRESS = 'Jl. Sudirman No. 24, Jakarta Selatan'
TEXT = f'NOMOR INDUK BERUSAHA (NIB)\nNama Perusahaan: {OWNER}\nNIB: {NIB}\nAlamat: {ADDRESS}\n'
USER = {'uid': 'applicant1', 'role': 'applicant', 'org_id': 'org_demo'}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(m, '_AUTOPARSE', False)
    monkeypatch.setattr(m, '_DOC_VERIFY', False)
    monkeypatch.setattr(m, 'verify_enqueue', lambda *args, **kwargs: False)
    monkeypatch.setattr(intake, '_ZIP_LOOKUP', False)
    monkeypatch.setattr(intake.ai_local, 'llm_json',
                        lambda *args, **kwargs: pytest.fail('외부 AI 호출 금지'))


@pytest.fixture
def http():
    with TestClient(m.app) as client:
        headers = {}
        for role in ('applicant', 'auditor'):
            response = client.post('/auth/login', json={'username': role + '1', 'password': 'pw'})
            assert response.status_code == 200, response.text
            headers[role] = {'Authorization': 'Bearer ' + response.json()['token']}
        yield client, headers


@pytest.fixture
def document(http):
    with m.SessionLocal() as db:
        case = models.CaseApplication(org_id='org_demo', company_name=OWNER, nib=NIB)
        db.add(case)
        db.flush()
        fields = {'company_name': OWNER, 'nib': NIB, '_autofill': {'changes': {
            'nib': {'field': 'nib', 'scope': 'case', 'before': None,
                    'value': NIB, 'status': 'applied'}}}}
        doc = models.DocumentAsset(case_id=case.case_id, filename='NIB.txt',
            doc_type='nib_business_license', fields=fields, text_full=TEXT,
            content_b64=base64.b64encode(TEXT.encode()).decode())
        db.add(doc)
        db.commit()
        yield case.case_id, doc.document_id
        # 다른 사례에서 같은 예시 번호를 쓰더라도 중복번호 검사를 바꾸지 않는다.
        db.get(models.CaseApplication, case.case_id).nib = None
        db.commit()


def approve_and_update(http, case_id, document_id):
    client, headers = http
    response = client.patch(f'/documents/{document_id}/review',
        json={'review_status': 'approved'}, headers=headers['auditor'])
    assert response.status_code == 200, response.text
    with m.SessionLocal() as db:
        m._lock_case(db, case_id)
        doc = db.get(models.DocumentAsset, document_id, populate_existing=True)
        preserved = {'_autofill': copy.deepcopy(doc.fields['_autofill']),
            '_scope_pending': [{'kind': 'material_names', 'value': 'Water', 'reason': 'scope_frozen'}],
            '_counts_as_evidence': False, '_upload_id': 'latest-upload',
            '_verify': {'verdict': 'warn', 'summary': '최신 검토 결과'}}
        assert preserved['_autofill']['changes']['nib']['status'] == 'confirmed'
        doc.fields = {**doc.fields, **preserved, 'latest_field': 'keep'}
        db.commit()
        return preserved


@pytest.mark.parametrize('merge_second', [False, True], ids=['ordinary-delete', 'merge-delete'])
def test_delete_after_merge_keeps_latest_aliases(http, monkeypatch, merge_second):
    client, headers = http
    with m.SessionLocal() as db:
        case = models.CaseApplication(org_id='org_demo', company_name='Concurrent products')
        db.add(case)
        db.flush()
        products = [models.Product(case_id=case.case_id, name=name)
                    for name in ('Keep serum', 'Merged serum', 'Removed serum')]
        db.add_all(products)
        db.flush()
        keep, merged, removed = [row.product_id for row in products]
        case.profile_ext = {'country': 'KR', 'product_aliases': {
            keep: ['Keep alias'], merged: ['Merged alias'], removed: ['Removed alias']}}
        db.commit()
        case_id = case.case_id

    waiting, resume = threading.Event(), threading.Event()
    scope_open = m._assert_scope_open

    def delayed_scope(row, **kwargs):
        if row.case_id == case_id and not waiting.is_set():
            waiting.set()  # 제품 삭제 요청은 이미 이전 profile_ext를 읽었다.
            assert resume.wait(10), '앞선 합치기가 완료되지 않았습니다'
        return scope_open(row, **kwargs)

    monkeypatch.setattr(m, '_assert_scope_open', delayed_scope)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.delete, f'/cases/{case_id}/products/{removed}',
            params={'merge_into': keep} if merge_second else {}, headers=headers['applicant'])
        try:
            assert waiting.wait(10), '삭제 요청이 잠금 전 조회에 도달하지 않았습니다'
            response = client.delete(f'/cases/{case_id}/products/{merged}',
                params={'merge_into': keep}, headers=headers['applicant'])
            assert response.status_code == 200, response.text
            with m.SessionLocal() as db:
                current = m._lock_case(db, case_id)
                aliases = {**current.profile_ext['product_aliases'], removed: ['Latest removed alias']}
                current.profile_ext = {**current.profile_ext, 'city': 'Seoul', 'product_aliases': aliases}
                db.commit()
        finally:
            resume.set()
        response = future.result(timeout=10)
    assert response.status_code == 200, response.text
    with m.SessionLocal() as db:
        aliases = ['Keep alias', 'Merged serum', 'Merged alias']
        if merge_second:
            aliases += ['Removed serum', 'Latest removed alias']
        assert db.get(models.CaseApplication, case_id).profile_ext == {
            'country': 'KR', 'city': 'Seoul', 'product_aliases': {keep: aliases}}
        assert {row.product_id for row in db.query(models.Product).filter_by(case_id=case_id)} == {keep}
        audit = db.query(models.AuditLog).filter_by(case_id=case_id, action='product.delete',
                                                   resource_id=removed).one()
        assert audit.meta['aliases'] == ['Latest removed alias']


@pytest.mark.parametrize('analysis', ['success', 'ocr-error', 'llm-error', 'empty'])
def test_profile_reanalysis_preserves_confirmation_committed_during_parse(
        document, http, monkeypatch, analysis):
    case_id, document_id = document
    waiting, resume = threading.Event(), threading.Event()

    def parse(*args, **kwargs):
        waiting.set()
        assert resume.wait(10), '사람의 승인이 완료되지 않았습니다'
        if analysis == 'ocr-error':
            raise RuntimeError('OCR unavailable')
        return TEXT

    def classify(*args, **kwargs):
        if analysis == 'llm-error':
            raise RuntimeError('LLM unavailable')
        return {'doc_type': 'nib_business_license', 'confidence': .95,
                'fields': {'company_name': OWNER, 'nib': NIB, 'address': ADDRESS}
                if analysis == 'success' else {}}

    monkeypatch.setattr(intake, 'parse_file', parse)
    monkeypatch.setattr(intake, 'classify', classify)
    client, headers = http
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, f'/cases/{case_id}/profile/autofill',
                             headers=headers['applicant'])
        try:
            assert waiting.wait(10), '프로필 재분석이 시작되지 않았습니다'
            preserved = approve_and_update(http, case_id, document_id)
        finally:
            resume.set()
        response = future.result(timeout=10)
    assert response.status_code == 200, response.text
    with m.SessionLocal() as db:
        doc = db.get(models.DocumentAsset, document_id)
        assert doc.review_status == 'approved'
        assert doc.fields['_autofill']['changes']['nib'] == preserved['_autofill']['changes']['nib']
        assert {key: doc.fields.get(key) for key in preserved if key != '_autofill'} == {
            key: value for key, value in preserved.items() if key != '_autofill'}
        if analysis == 'success':
            assert doc.fields['address'] == ADDRESS
        elif analysis in ('ocr-error', 'llm-error'):
            assert doc.fields['_' + analysis.replace('-', '_')] == 'ERROR'


@pytest.mark.parametrize('writer', ['autofill', 'owned', 'protect', 'confirm'])
def test_cached_document_writers_keep_latest_fields_and_local_changes(document, http, writer):
    case_id, document_id = document
    with m.SessionLocal() as db:
        doc = db.get(models.DocumentAsset, document_id)
        meta = {**doc.fields['_autofill'], 'conflicts': [
            {'field': 'address', 'scope': 'case', 'current': None, 'proposed': ADDRESS,
             'status': 'pending', 'reason': 'value_conflict'},
            {'field': 'responsible_person', 'scope': 'case', 'current': None, 'proposed': 'Budi',
             'status': 'pending', 'reason': 'value_conflict'}]}
        doc.fields = {**doc.fields, 'address': ADDRESS, '_autofill': meta}
        db.commit()
    waiting, resume = threading.Event(), threading.Event()

    def write():
        with m.SessionLocal() as db:
            case = db.get(models.CaseApplication, case_id)
            doc = db.get(models.DocumentAsset, document_id)
            assert doc.fields['_autofill']['changes']['nib']['status'] == 'applied'
            waiting.set()
            assert resume.wait(10), '다른 세션의 저장이 완료되지 않았습니다'
            assert 'latest_field' not in doc.fields  # identity map에 남은 잠금 전 문서
            if writer == 'autofill':
                m.autofill_profile(case_id, user=USER, db=db)
            elif writer == 'owned':
                m._apply_owned_autofill(db, case, doc)
                db.commit()
            elif writer == 'protect':
                # 실제 프로필 편집처럼 같은 트랜잭션의 미저장 변경도 보존해야 한다.
                case.phone = '+821012345678'
                m._protect_autofill(db, {'nib'}, USER['uid'], case_id=case_id)
                assert case.phone == '+821012345678'
                db.commit()
            else:
                m._assert_scope_open(case)
                choices = [{'conflict_id': f'{document_id}:0', 'value': ADDRESS},
                           {'conflict_id': f'{document_id}:1', 'value': 'Budi'}]
                m._confirm_autofill(db, case, choices, USER)
                db.commit()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(write)
        try:
            assert waiting.wait(10), '문서 조회가 완료되지 않았습니다'
            preserved = approve_and_update(http, case_id, document_id)
        finally:
            resume.set()
        future.result(timeout=10)
    with m.SessionLocal() as db:
        fields = db.get(models.DocumentAsset, document_id).fields
        assert fields.get('latest_field') == 'keep'
        assert fields['_autofill']['changes']['nib']['status'] == 'confirmed'
        assert {key: fields.get(key) for key in preserved if key != '_autofill'} == {
            key: value for key, value in preserved.items() if key != '_autofill'}
        if writer == 'confirm':
            assert [row['status'] for row in fields['_autofill']['conflicts']] == ['confirmed', 'confirmed']
            assert db.get(models.CaseApplication, case_id).address == ADDRESS
            assert db.get(models.CaseApplication, case_id).responsible_person == 'Budi'
        if writer == 'protect':
            assert db.get(models.CaseApplication, case_id).phone == '+821012345678'


def test_document_review_refreshes_fields_after_scope_lock(document, http, monkeypatch):
    case_id, document_id = document
    waiting, resume = threading.Event(), threading.Event()
    lock_scope = m.sm.lock_scope

    def delayed_lock(db, case):
        if case.case_id == case_id and not waiting.is_set():
            waiting.set()
            assert resume.wait(10), '문서 갱신이 완료되지 않았습니다'
        return lock_scope(db, case)

    monkeypatch.setattr(m.sm, 'lock_scope', delayed_lock)
    client, headers = http
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.patch, f'/documents/{document_id}/review',
            json={'review_status': 'approved'}, headers=headers['auditor'])
        try:
            assert waiting.wait(10), '문서 승인이 잠금 전 조회에 도달하지 않았습니다'
            with m.SessionLocal() as db:
                m._lock_case(db, case_id)
                doc = db.get(models.DocumentAsset, document_id)
                fields = copy.deepcopy(doc.fields)
                fields.update(latest_field='keep', _verify={'verdict': 'warn', 'summary': 'latest'})
                fields['_autofill']['changes']['address'] = {
                    'scope': 'case', 'field': 'address', 'value': ADDRESS, 'status': 'applied'}
                doc.fields = fields
                db.commit()
        finally:
            resume.set()
        response = future.result(timeout=10)
    assert response.status_code == 200, response.text
    with m.SessionLocal() as db:
        doc = db.get(models.DocumentAsset, document_id)
        assert doc.fields.get('latest_field') == 'keep'
        assert doc.fields['_verify'] == {'verdict': 'warn', 'summary': 'latest'}
        assert {key: value['status'] for key, value in doc.fields['_autofill']['changes'].items()} == {
            'nib': 'confirmed', 'address': 'confirmed'}


@pytest.mark.parametrize('select_nib', [True, False], ids=['complete-identity', 'final-duplicate'])
def test_confirm_company_and_nib_in_generated_candidate_order(http, monkeypatch, select_nib):
    client, headers = http
    old_name, new_name = f'PT Original Followup {select_nib}', f'PT Renamed Followup {select_nib}'
    old_nib, new_nib = ('9120007654321', '9120007654322') if select_nib else ('9120007654323', '9120007654324')
    with m.SessionLocal() as db:
        existing = models.CaseApplication(org_id='org_demo', company_name=old_name, nib=old_nib)
        case = models.CaseApplication(org_id='org_demo', company_name=old_name, nib=old_nib)
        db.add_all([existing, case])
        db.flush()
        doc = models.DocumentAsset(case_id=case.case_id, filename='new-nib.txt',
            doc_type='nib_business_license', fields={'company_name': new_name, 'nib': new_nib},
            text_full=f'NOMOR INDUK BERUSAHA (NIB)\nNama Perusahaan: {new_name}\nNIB: {new_nib}')
        db.add(doc)
        db.flush()
        m._apply_owned_autofill(db, case, doc)
        db.commit()
        case_id, document_id = case.case_id, doc.document_id

    response = client.get(f'/cases/{case_id}/pending-extractions', headers=headers['applicant'])
    assert response.status_code == 200, response.text
    conflicts = response.json()['conflicts']
    # 실제 생성 순서를 유지한다. NIB를 앞으로 정렬하면 중간 flush 회귀를 숨긴다.
    assert [row['field'] for row in conflicts[:2]] == ['company_name', 'nib']
    identities = []
    validate = m._assert_unique_case_identity

    def record_identity(db, case):
        if case.case_id == case_id:
            identities.append((case.company_name, case.nib))
        return validate(db, case)

    monkeypatch.setattr(m, '_assert_unique_case_identity', record_identity)
    response = client.post(f'/cases/{case_id}/pending-extractions/apply',
        json={'autofill_choices': [{'conflict_id': row['conflict_id'], 'value': row['proposed']}
                                   for row in conflicts if select_nib or row['field'] != 'nib']},
        headers=headers['applicant'])
    assert identities and set(identities) == {(new_name, new_nib if select_nib else old_nib)}
    if not select_nib:
        assert response.status_code == 409, response.text
        assert response.json()['detail']['code'] == 'NIB_ALREADY_USED'
        with m.SessionLocal() as db:
            case = db.get(models.CaseApplication, case_id)
            assert (case.company_name, case.nib) == (old_name, old_nib)
            records = db.get(models.DocumentAsset, document_id).fields['_autofill']['conflicts']
            assert all(row['status'] == 'pending' for row in records)
        return
    assert response.status_code == 200, response.text
    assert len(response.json()['autofill_confirmed']) == len(conflicts)
    with m.SessionLocal() as db:
        case = db.get(models.CaseApplication, case_id)
        assert (case.company_name, case.nib) == (new_name, new_nib)
        records = db.get(models.DocumentAsset, document_id).fields['_autofill']['conflicts']
        assert [row['status'] for row in records] == ['confirmed'] * len(conflicts)


@pytest.mark.parametrize('reprocess', [False, True], ids=['stored-address', 'reparsed-address'])
def test_profile_zip_lookup_does_not_hold_write_lock(document, http, monkeypatch, reprocess):
    import httpx

    case_id, document_id = document
    if not reprocess:
        with m.SessionLocal() as db:
            doc = db.get(models.DocumentAsset, document_id)
            doc.fields = {**doc.fields, 'address': ADDRESS}
            db.commit()
    monkeypatch.setattr(intake, 'parse_file', lambda *args, **kwargs: TEXT)
    monkeypatch.setattr(intake, 'classify', lambda *args, **kwargs: {
        'doc_type': 'nib_business_license', 'confidence': .95,
        'fields': {'company_name': OWNER, 'nib': NIB, 'address': ADDRESS}})
    monkeypatch.setattr(intake, '_ZIP_LOOKUP', True)
    monkeypatch.setattr(intake, '_ZIP_CACHE', {})
    waiting, resume = threading.Event(), threading.Event()

    def lookup(url, **kwargs):
        assert url == 'https://nominatim.openstreetmap.org/search'
        waiting.set()
        assert resume.wait(10), '주소 조회 중 다른 세션이 저장하지 못했습니다'
        return httpx.Response(200, json=[{'address': {'postcode': '12920'}}])

    monkeypatch.setattr(httpx, 'get', lookup)
    client, headers = http
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, f'/cases/{case_id}/profile/autofill',
                             headers=headers['applicant'])
        try:
            assert waiting.wait(10), '주소 조회에 도달하지 않았습니다'
            with m.SessionLocal() as db:
                db.connection().exec_driver_sql('PRAGMA busy_timeout=100')
                case = m._lock_case(db, case_id)
                case.phone = '+821099998888'
                doc = db.get(models.DocumentAsset, document_id)
                doc.fields = {**doc.fields, '_verify': {'verdict': 'warn', 'summary': 'during lookup'}}
                db.commit()
        finally:
            resume.set()
        response = future.result(timeout=10)
    assert response.status_code == 200, response.text
    assert response.json()['extracted']['address'] == ADDRESS
    with m.SessionLocal() as db:
        assert db.get(models.CaseApplication, case_id).phone == '+821099998888'
        assert db.get(models.DocumentAsset, document_id).fields['_verify']['summary'] == 'during lookup'


@pytest.mark.parametrize('operation', ['confirm-first', 'confirm-last', 'edit-facility', 'edit-facility-typed'])
def test_facility_autofill_locks_cases_in_order_from_first_acquisition(http, monkeypatch, operation):
    from app import db as app_db

    client, headers = http
    with m.SessionLocal() as db:
        facility = models.Facility(org_id='org_demo', name='Ordered plant', address='Old address')
        db.add(facility)
        db.flush()
        cases = [models.CaseApplication(case_id=f'00-{operation}-{suffix}', org_id='org_demo',
                                       company_name='Ordered company') for suffix in ('a', 'b')]
        # 앞 순서 케이스는 문서만 있고, 시설 편집의 기존 범위 가드에는 포함되지 않는다.
        cases[1].facility_ids = [facility.facility_id]
        db.add_all(cases)
        db.flush()
        for case in cases:
            db.add(models.DocumentAsset(case_id=case.case_id, filename='factory.txt',
                doc_type='factory_registration', fields={'_autofill': {
                    'changes': {'facility.address': {'field': 'address', 'scope': 'facility',
                        'facility_id': facility.facility_id, 'value': 'Old address', 'status': 'applied'}},
                    'conflicts': [{'field': 'address', 'scope': 'facility',
                        'facility_id': facility.facility_id, 'current': 'Old address',
                        'proposed': 'New address', 'status': 'pending', 'reason': 'value_conflict'}]}}))
        db.commit()
        case_ids, facility_id = [case.case_id for case in cases], facility.facility_id

    acquired = []

    def record_lock(db, key):
        if key.startswith('wf:') and key[3:] not in acquired:
            acquired.append(key[3:])
        # PostgreSQL의 키별 획득 순서를 기록하되 실제 저장은 격리된 SQLite DB만 사용한다.
        conn = db.connection()
        if not conn.connection.driver_connection.in_transaction:
            conn.exec_driver_sql('BEGIN IMMEDIATE')

    monkeypatch.setattr(m.engine.dialect, 'name', 'postgresql')
    monkeypatch.setattr(app_db, 'lock_chain_tail', record_lock)
    monkeypatch.setattr(m, 'lock_chain_tail', record_lock)
    monkeypatch.setattr(m.sm, 'lock_chain_tail', record_lock)
    if operation == 'edit-facility-typed':
        with m.SessionLocal() as db:
            result = m.update_facility(facility_id, m.schemas.FacilityUpdateReq(address='Old address'),
                                       user=USER, db=db)
        assert result['facility_id'] == facility_id
    elif operation == 'edit-facility':
        response = client.patch(f'/facilities/{facility_id}', json={'address': 'Old address'},
                                headers=headers['applicant'])
    else:
        case_id = case_ids[0 if operation == 'confirm-first' else 1]
        pending = client.get(f'/cases/{case_id}/pending-extractions', headers=headers['applicant'])
        assert pending.status_code == 200, pending.text
        conflict = pending.json()['conflicts'][0]
        response = client.post(f'/cases/{case_id}/pending-extractions/apply',
            json={'autofill_choices': [{'conflict_id': conflict['conflict_id'], 'value': conflict['current']}]},
            headers=headers['applicant'])
    if operation != 'edit-facility-typed':
        assert response.status_code == 200, response.text
    assert set(case_ids) <= set(acquired)
    assert acquired == sorted(acquired), acquired
    with m.SessionLocal() as db:
        docs = db.query(models.DocumentAsset).filter(models.DocumentAsset.case_id.in_(case_ids)).all()
        assert all(doc.fields['_autofill']['changes']['facility.address']['status'] == 'confirmed'
                   for doc in docs)
