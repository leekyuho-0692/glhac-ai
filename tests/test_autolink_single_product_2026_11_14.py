"""제품 합치기·엄격한 문서별 자동연결·프로필 저장 경합의 운영 회귀."""
import base64
import io
import json
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import app.main as m
from app import intake, models

KO = '닥토필 프로페셔널 포 페이스'
EN = 'DactorPeel Professional for Face'
MSDS_NAME = 'DactorPeel Professional For Face'


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(m, '_AUTOPARSE', False)
    monkeypatch.setattr(m, 'verify_enqueue', lambda *args, **kwargs: False)
    monkeypatch.setattr(intake.ai_local, 'llm_json',
                        lambda *args, **kwargs: pytest.fail('Unexpected external AI call'))


@pytest.fixture
def http():
    with TestClient(m.app) as client:
        headers = {}
        for role in ('applicant', 'operator', 'auditor'):
            response = client.post('/auth/login', json={'username': role + '1', 'password': 'pw'})
            assert response.status_code == 200, response.text
            headers[role] = {'Authorization': 'Bearer ' + response.json()['token']}
        yield client, headers


@pytest.fixture
def case(http):
    with m.SessionLocal() as db:
        c = models.CaseApplication(org_id='org_demo', company_name='Single product regression')
        keep, drop = (models.Product(name=KO), models.Product(name=EN))
        db.add(c)
        db.flush()
        keep.case_id = drop.case_id = c.case_id
        db.add_all([keep, drop])
        db.commit()
        yield db, c, keep, drop


def delete_product(case, http, *, merge=True, role='applicant', **params):
    db, c, keep, drop = case
    client, headers = http
    if merge:
        params.setdefault('merge_into', keep.product_id)
    response = client.delete(f'/cases/{c.case_id}/products/{drop.product_id}',
                             params=params, headers=headers[role])
    db.expire_all()
    return response


def links(db, c):
    return {(row.product_id, row.material_id) for row in
            db.query(models.ProductMaterial).filter_by(case_id=c.case_id)}


def test_merge_moves_links_once_preserves_aliases_and_audits(case, http):
    db, c, keep, drop = case
    keep_id, drop_id = keep.product_id, drop.product_id
    materials = [models.Material(case_id=c.case_id, name=name) for name in ('Water', 'Salt')]
    db.add_all(materials)
    db.flush()
    db.add_all([models.ProductMaterial(case_id=c.case_id, product_id=pid, material_id=mat.material_id)
                for pid, mat in [(keep_id, materials[0]), (drop_id, materials[0]),
                                 (drop_id, materials[1])]])
    c.profile_ext = {'country': 'KR', 'product_aliases': {
        keep_id: ['Face Peel'], drop_id: ['Face Peel', 'DactorPeel Face']}}
    db.commit()
    response = delete_product(case, http)
    assert response.status_code == 200, response.text
    assert db.get(models.Product, drop_id) is None
    assert links(db, c) == {(keep_id, mat.material_id) for mat in materials}
    assert db.query(models.ProductMaterial).filter_by(case_id=c.case_id).count() == 2
    assert c.profile_ext == {'country': 'KR', 'product_aliases': {
        keep_id: ['Face Peel', EN, 'DactorPeel Face']}}
    audit = db.query(models.AuditLog).filter_by(case_id=c.case_id, action='product.delete').one()
    assert audit.resource_id == drop_id
    assert audit.meta['merged_into'] == keep_id
    assert audit.meta['name'] == EN
    assert audit.meta['aliases'] == ['Face Peel', 'DactorPeel Face']
    assert audit.meta['material_links'] == 2
    assert not m.sm.rejected_product_autofill_names(db, c.case_id)


def test_concurrent_merges_preserve_both_aliases_after_case_lock(case, http, monkeypatch):
    db, c, keep, first = case
    second = models.Product(case_id=c.case_id, name='Alternate Face Serum')
    db.add(second)
    db.flush()
    case_id, keep_id = c.case_id, keep.product_id
    source_ids = [first.product_id, second.product_id]
    source_names = [first.name, second.name]
    c.profile_ext = {'country': 'KR', 'product_aliases': {
        keep_id: ['Face Peel'], source_ids[0]: ['First legacy'], source_ids[1]: ['Second legacy']}}
    db.commit()
    ready, completed = threading.Barrier(2), threading.Event()
    scope_open = m._assert_scope_open

    def synchronized_scope_open(row, **kwargs):
        session = m.inspect(row).session
        if row.case_id == case_id and not session.info.get('merge_synchronized'):
            session.info['merge_synchronized'] = True
            # 두 요청 모두 이전 profile_ext를 읽은 뒤, 한 합치기를 먼저 커밋한다.
            if ready.wait(timeout=10) == 0:
                assert completed.wait(10), '다른 합치기 요청이 완료되지 않았습니다'
        return scope_open(row, **kwargs)

    client, headers = http

    def merge(source_id):
        try:
            return client.delete(f'/cases/{case_id}/products/{source_id}',
                params={'merge_into': keep_id}, headers=headers['applicant'])
        finally:
            completed.set()

    monkeypatch.setattr(m, '_assert_scope_open', synchronized_scope_open)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(merge, source_ids))
    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
    monkeypatch.setattr(m, '_assert_scope_open', scope_open)
    db.expire_all()
    assert c.profile_ext['country'] == 'KR'
    assert set(c.profile_ext['product_aliases']) == {keep_id}
    assert set(c.profile_ext['product_aliases'][keep_id]) == {
        'Face Peel', *source_names, 'First legacy', 'Second legacy'}
    audits = db.query(models.AuditLog).filter_by(case_id=case_id, action='product.delete').all()
    assert {row.resource_id: row.meta['aliases'] for row in audits} == {
        source_ids[0]: ['First legacy'], source_ids[1]: ['Second legacy']}

    # 어느 요청이 먼저 끝나도 합친 이름의 문서 재분석이 제품을 되살리지 않는다.
    for name in source_names:
        parsed = document('merged-MSDS.txt', [name], [])
        stored = models.DocumentAsset(case_id=case_id, **parsed,
            content_b64=base64.b64encode(parsed['text_full'].encode()).decode())
        db.add(stored)
        db.commit()
        monkeypatch.setattr(intake.ai_local, 'llm_json', lambda *args, **kwargs: {
            'doc_type': 'coa_msds', 'confidence': .95, 'fields': parsed['fields']})
        response = client.post(f'/documents/{stored.document_id}/reprocess', headers=headers['operator'])
        assert response.status_code == 200, response.text
        assert response.json()['applied']['products'] == 0
    db.expire_all()
    assert [p.product_id for p in db.query(models.Product).filter_by(case_id=case_id)] == [keep_id]


@pytest.mark.parametrize('target', ['self', 'other-case', 'missing', 'empty'])
def test_merge_target_validation_has_no_side_effects(case, http, target):
    db, c, keep, drop = case
    if target == 'other-case':
        other = models.CaseApplication(org_id='org_demo', company_name='Other case')
        db.add(other)
        db.flush()
        foreign = models.Product(case_id=other.case_id, name='Foreign product')
        db.add(foreign)
        db.commit()
        target_id = foreign.product_id
    else:
        target_id = {'self': drop.product_id, 'missing': 'missing-product', 'empty': ''}[target]
    response = delete_product(case, http, merge_into=target_id)
    assert response.status_code == 422, response.text
    assert db.query(models.Product).filter_by(case_id=c.case_id).count() == 2
    assert not (c.profile_ext or {}).get('product_aliases')
    assert db.query(models.AuditLog).filter_by(case_id=c.case_id, action='product.delete').count() == 0


def test_merge_missing_source_is_not_found(case, http):
    _, c, keep, _ = case
    client, headers = http
    response = client.delete(f'/cases/{c.case_id}/products/missing',
        params={'merge_into': keep.product_id}, headers=headers['applicant'])
    assert response.status_code == 404


@pytest.mark.parametrize('guard,role,force,status,code', [
    ('frozen', 'operator', True, 409, 'SCOPE_FROZEN'),
    ('closed', 'operator', True, 409, 'CASE_CLOSED'),
    ('certificate', 'operator', False, 409, 'CERT_SCOPE_FROZEN'),
    ('certificate', 'applicant', True, 403, 'NOT_AUTHORIZED'),
    ('role', 'auditor', False, 403, 'NOT_AUTHORIZED'),
    ('organization', 'applicant', False, 403, 'ORG_FORBIDDEN'),
])
def test_merge_keeps_delete_guards(case, http, guard, role, force, status, code):
    db, c, keep, drop = case
    if guard == 'frozen':
        c.scope_frozen = True
    elif guard == 'closed':
        c.status = 'withdrawn'
    elif guard == 'organization':
        db.add(models.Org(org_id='merge-other-org', name='Other organization'))
        c.org_id = 'merge-other-org'
    elif guard == 'certificate':
        db.add(models.HalalCertificate(case_id=c.case_id, certificate_no='MERGE-GUARD',
            status='active', frozen_product_ids=[drop.product_id]))
    db.commit()
    response = delete_product(case, http, role=role, force=force)
    assert response.status_code == status, response.text
    assert response.json()['detail']['code'] == code
    assert db.query(models.Product).filter_by(case_id=c.case_id).count() == 2
    assert db.query(models.AuditLog).filter_by(case_id=c.case_id, action='product.delete').count() == 0


def test_operator_force_merge_keeps_certificate_cleanup(case, http):
    db, c, keep, drop = case
    cert = models.HalalCertificate(case_id=c.case_id, certificate_no='MERGE-FORCE',
        status='active', frozen_product_ids=[keep.product_id, drop.product_id])
    db.add(cert)
    db.commit()
    response = delete_product(case, http, role='operator', force=True)
    assert response.status_code == 200, response.text
    assert cert.frozen_product_ids == [keep.product_id]
    assert response.json()['removed_from_certificates'] == ['MERGE-FORCE']


@pytest.mark.parametrize('history,expected', [
    ('merge', 1), ('two-merges', 1), ('ordinary', 0), ('wrong-target', 0),
    ('missing-name', 0), ('blank-name', 0), ('merge-and-ordinary', 0),
])
def test_all_deletions_must_be_named_merges_into_survivor(case, http, history, expected):
    db, c, keep, drop = case
    response = delete_product(case, http, merge=history != 'ordinary')
    assert response.status_code == 200, response.text
    if history not in ('merge', 'ordinary'):
        meta = {'name': 'Earlier duplicate', 'merged_into': keep.product_id}
        if history == 'wrong-target':
            meta['merged_into'] = 'old-target'
        elif history == 'missing-name':
            meta.pop('name')
        elif history == 'blank-name':
            meta['name'] = '   '
        elif history == 'merge-and-ordinary':
            meta.pop('merged_into')
        m._audit(db, {'uid': 'operator1', 'role': 'operator'}, 'product.delete',
                 'product', 'earlier-product', c.case_id, meta, commit=False)
    material = models.Material(case_id=c.case_id, name='Water')
    db.add(material)
    db.flush()
    assert m._auto_link_pm(db, c.case_id, single_product=True, material_ids=[material.material_id],
                          material_sources={'water': {KO}}) == expected


@pytest.mark.parametrize('history_model', ['audit', 'event'])
def test_human_unlinked_pair_stays_unlinked_after_merge(case, http, history_model):
    db, c, keep, _ = case
    assert delete_product(case, http).status_code == 200
    water = models.Material(case_id=c.case_id, name='Water')
    salt = models.Material(case_id=c.case_id, name='Salt')
    db.add_all([water, salt])
    db.flush()
    payload = {'product_id': keep.product_id, 'material_id': water.material_id, 'linked': False}
    if history_model == 'audit':
        m._audit(db, {'uid': 'applicant1', 'role': 'applicant'}, 'matrix.link',
                 'case', c.case_id, c.case_id, payload, commit=False)
    else:
        m.sm.record_event(db, c, c.status, c.status, 'matrix/link', 'applicant', 'applicant1', payload)
    assert m._auto_link_pm(db, c.case_id, single_product=True,
        material_ids=[water.material_id, salt.material_id],
        material_sources={'water': {MSDS_NAME}, 'salt': {MSDS_NAME}}) == 1
    db.flush()
    assert links(db, c) == {(keep.product_id, salt.material_id)}


def document(filename, product_names, materials=('Water',), *, raw_coa=False, product_candidates=True):
    title = ('Certificate of Analysis\nRaw material supplier: Ingredient Supplier\n' if raw_coa else
             'Material Safety Data Sheet\n')
    text = title + ''.join(f'Product name: {name}\n' for name in product_names)
    text += 'Ingredients\n' + '\n'.join(f'{name} 10%' for name in materials)
    return {'filename': filename, 'doc_type': 'coa_msds', 'confidence': .95, 'text_full': text,
            'fields': {'product_names': list(product_names) if product_candidates else [],
                       'material_names': list(materials)}}


def ingest(case, http, docs, route):
    db, c, _, _ = case
    if route == 'aggregate':
        result = m._apply_intake_autofill(db, c, {
            'classified': docs, 'extracted': intake.aggregate_fields(docs)})
        db.commit()
        return result
    db.add_all([models.DocumentAsset(case_id=c.case_id, **doc) for doc in docs])
    db.commit()
    values = list(dict.fromkeys(name for doc in docs for name in doc['fields']['material_names']))
    body = {'values': values}
    if route == 'rename':
        body.update(renames={'Water': 'Purified Water'}, reason='Verified material name')
    client, headers = http
    response = client.post(f'/cases/{c.case_id}/pending-extractions/apply',
                           headers=headers['operator'], json=body)
    assert response.status_code == 200, response.text
    db.expire_all()
    return response.json()['applied']


@pytest.mark.parametrize('route', ['aggregate', 'apply', 'rename'])
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('merge', [False, True])
def test_operating_duplicate_merge_msds_and_supplier_coa(case, http, route, reverse, merge):
    db, c, keep, drop = case
    keep_id, drop_id = keep.product_id, drop.product_id
    assert not links(db, c)  # 운영에서 삭제된 영문 제품에는 연결이 없었다.
    response = delete_product(case, http, merge=merge, role='operator')
    assert response.status_code == 200, response.text
    materials = ['Water', 'Salt', 'Niacinamide', 'Citric Acid']
    docs = [document('finished-MSDS.txt', [MSDS_NAME], materials),
            document('supplier-COA.txt', ['innodiol®6'], raw_coa=True, product_candidates=False),
            document('unknown-MSDS.txt', [], ['Salt'])]
    if reverse:
        docs.reverse()
    applied = ingest(case, http, docs, route)
    assert applied['materials'] == 4
    assert applied['links'] == (2 if merge else 0)
    assert [p.product_id for p in db.query(models.Product).filter_by(case_id=c.case_id)] == [keep_id]
    expected = {(keep_id, mat.material_id) for mat in
                db.query(models.Material).filter_by(case_id=c.case_id)
                if mat.name in ('Niacinamide', 'Citric Acid')} if merge else set()
    assert links(db, c) == expected
    if merge:
        assert EN in c.profile_ext['product_aliases'][keep_id]
        assert drop_id not in c.profile_ext['product_aliases']
        assert not [item for item in m._pending_extractions(db, c.case_id)
                    if item['kind'] == 'product_names' and item['value'] == MSDS_NAME]
    # 같은 원본을 다시 집계해도 기존 원재료·연결은 추가되지 않는다.
    if route == 'aggregate':
        again = ingest(case, http, docs, route)
        assert again['materials'] == 0
        assert links(db, c) == expected


@pytest.mark.parametrize('route', ['aggregate', 'apply', 'rename'])
@pytest.mark.parametrize('source_case,expected', [
    ('finished-only', 1), ('matching-documents', 1),
    ('coa-only', 0), ('coa-and-finished', 0), ('two-products-in-one-document', 0),
    ('empty-product-in-one-document', 0), ('missing-product', 0),
    ('coa-and-unknown-msds', 0), ('coa-and-unlisted-msds', 0),
    ('unconfirmed-coa-and-finished', 0), ('unlisted-coa-and-finished', 0),
])
def test_every_document_source_must_match_current_product(case, http, route, source_case, expected):
    db, c, keep, _ = case
    assert delete_product(case, http).status_code == 200
    coa = document('supplier-COA.txt', ['innodiol®6'], raw_coa=True, product_candidates=False)
    finished = document('finished-MSDS.txt', [MSDS_NAME])
    docs = [coa]
    if source_case == 'finished-only':
        docs = [finished]
    elif source_case == 'matching-documents':
        docs = [finished, document('korean-MSDS.txt', [KO])]
    elif source_case == 'coa-and-finished':
        docs.append(finished)
    elif source_case == 'two-products-in-one-document':
        docs = [document('ambiguous-MSDS.txt', [MSDS_NAME, 'Other serum'])]
    elif source_case == 'empty-product-in-one-document':
        docs = [document('ambiguous-MSDS.txt', [MSDS_NAME, ''])]
    elif source_case == 'missing-product':
        docs = [document('unknown-MSDS.txt', [])]
    elif source_case in ('coa-and-unknown-msds', 'coa-and-unlisted-msds'):
        docs += [finished, document('other-MSDS.txt',
                 [] if source_case == 'coa-and-unknown-msds' else ['Other serum'],
                 product_candidates=False)]
    elif source_case == 'unconfirmed-coa-and-finished':
        coa['text_full'] = coa['text_full'].replace('Raw material supplier: Ingredient Supplier\n', '')
        docs.append(finished)
    elif source_case == 'unlisted-coa-and-finished':
        docs = [document('supplier-COA.txt', ['innodiol®6', 'Other serum'], raw_coa=True,
                         product_candidates=False), finished]
    applied = ingest(case, http, docs, route)
    assert applied['materials'] == 1
    assert applied['links'] == expected
    assert len(links(db, c)) == expected
    assert all(pid == keep.product_id for pid, _ in links(db, c))


@pytest.mark.parametrize('groups,expected', [
    ([], 0), ([set()], 0), ([{''}], 0), ([{EN, ''}], 0),
    ([{EN, 'Other serum'}], 0), ([{'innodiol®6'}], 0),
    ([{'innodiol®6'}, {MSDS_NAME}], 0), ([{MSDS_NAME}, {'innodiol®6'}], 0),
    ([{KO, EN}], 1), ([{KO}, {MSDS_NAME}], 1),
    ([{MSDS_NAME}, set()], 0), ([set(), {MSDS_NAME}], 0),
    ([{MSDS_NAME}, {''}], 0), ([{MSDS_NAME}, {EN, ''}], 0),
])
def test_material_sources_are_independent_document_sets(case, http, groups, expected):
    db, c, keep, _ = case
    assert delete_product(case, http).status_code == 200
    material = models.Material(case_id=c.case_id, name='Water')
    db.add(material)
    db.flush()
    assert m._auto_link_pm(db, c.case_id, single_product=True, material_ids=[material.material_id],
                          material_sources={'water': groups}) == expected


@pytest.mark.parametrize('route', ['aggregate', 'apply'])
def test_supplier_coa_does_not_borrow_other_materials_finished_evidence(case, http, route):
    db, c, keep, _ = case
    assert delete_product(case, http).status_code == 200
    applied = ingest(case, http, [document('finished-MSDS.txt', [MSDS_NAME], ['Salt']),
                                 document('supplier-COA.txt', ['innodiol®6'], raw_coa=True,
                                          product_candidates=False)], route)
    salt = db.query(models.Material).filter_by(case_id=c.case_id, name='Salt').one()
    assert applied['materials'] == 2
    assert links(db, c) == {(keep.product_id, salt.material_id)}


def test_pending_selection_keeps_source_of_candidate_outside_material_names(case, http):
    db, c, _, _ = case
    assert delete_product(case, http).status_code == 200
    pending = document('pending-MSDS.txt', ['Other serum'], [])
    pending['fields']['_scope_pending'] = [{'kind': 'material_names', 'value': 'Water'}]
    pending['fields']['_autofill'] = {'product_pending': [
        {'kind': 'product_names', 'value': 'Other serum'}]}
    applied = ingest(case, http, [pending, document('finished-MSDS.txt', [MSDS_NAME])], 'apply')
    assert applied['materials'] == 1
    assert applied['links'] == 0
    assert not links(db, c)


@pytest.mark.parametrize('reverse', [False, True])
def test_operating_reanalysis_links_only_materials_exclusive_to_merged_finished_msds(case, http, reverse):
    db, c, keep, drop = case
    keep_id, drop_id = keep.product_id, drop.product_id
    materials = ['1,2-Hexanediol', 'Citral', 'Disodium EDTA', 'Propanediol', 'Water', 'Butylene Glycol']
    finished_only = ['Salt', 'Niacinamide']
    sources = [
        ('5. DactorPeel Professional For Face_MSDS', [MSDS_NAME], {'dactorpeelprofessionalforface'}),
        ('1. DactorTec KISsoo(W)_COA', ['DactorTec™ KISsoo(W)'], {'dactortec™kissoo(w)'}),
        ('1. DactorTec KISsoo(W)_MSDS', [], {''}),
        ('8. InnoDiol 6_COA', ['InnoDiol® 6'], {'innodiol®6'}),
        ('8. InnoDiol 6_MSDS', ['InnoDiol® 6'], {'innodiol®6'}),
        ('11. Leafresh(NB)_MSDS', ['Leafresh(NB)'], {'leafresh(nb)'}),
        ('22. DactorTec KISsom_COA', ['DactorTecTM KISsom', 'DactorTec™ KISsom'],
         {'dactortectmkissom', 'dactortec™kissom'}),
    ]
    # 실제 출처 집합을 각 원료에 대조한다. 공급사 표기는 제품 후보 보류가 아닌 원문 라벨이다.
    docs = [document(filename, names, materials + (finished_only if index == 0 else []),
                     product_candidates=False)
            for index, (filename, names, _) in enumerate(sources)]
    for doc, (_, _, expected) in zip(docs, sources):
        assert intake._source_product_names(doc['fields'], doc['text_full']) == expected
        assert 'Certificate of Analysis' not in doc['text_full']
        assert 'Raw material' not in doc['text_full']
    db.add_all([models.DocumentAsset(case_id=c.case_id, **doc) for doc in docs])
    db.commit()
    original_ids = {doc.document_id for doc in db.query(models.DocumentAsset).filter_by(case_id=c.case_id)}
    assert not links(db, c)
    response = delete_product(case, http, role='operator')
    assert response.status_code == 200, response.text
    if reverse:
        docs.reverse()
    applied = ingest(case, http, docs, 'aggregate')
    assert applied['dup_skipped'] == 7  # 저장된 같은 문서들을 합치기 후 재분석한다.
    assert applied['materials'] == 8
    assert applied['links'] == 2
    rows = db.query(models.Material).filter_by(case_id=c.case_id).all()
    assert {row.name for row in rows} == set(materials + finished_only)
    assert links(db, c) == {(keep_id, row.material_id) for row in rows if row.name in finished_only}
    assert [(p.product_id, p.name) for p in db.query(models.Product).filter_by(case_id=c.case_id)] == [(keep_id, KO)]
    assert EN in c.profile_ext['product_aliases'][keep_id]
    assert db.get(models.Product, drop_id) is None
    assert {doc.document_id for doc in db.query(models.DocumentAsset).filter_by(case_id=c.case_id)} == original_ids


@pytest.mark.parametrize('route', ['aggregate', 'apply', 'rename'])
@pytest.mark.parametrize('guard', ['pending', 'rejected'])
def test_pending_or_rejected_product_source_blocks_only_its_material(case, http, route, guard):
    db, c, keep, _ = case
    assert delete_product(case, http).status_code == 200
    other_name = 'Other serum'
    if guard == 'pending':
        db.add(models.DocumentAsset(case_id=c.case_id, filename='product-candidate.txt', doc_type='other', confidence=.95,
            fields={'product_names': [other_name], '_autofill': {'product_pending': [
                {'kind': 'product_names', 'value': other_name}]}}))
    else:
        m.sm.record_event(db, c, c.status, c.status, 'extraction.review', 'operator', 'operator1',
            {'items': [{'kind': 'product_names', 'value': other_name}], 'dismiss': [other_name]})
        assert models.normalize_material_name(other_name) in m.sm._rejected_autofill_names(db, c.case_id, 'product')
    db.commit()
    docs = [document('finished-MSDS.txt', [MSDS_NAME], ['Water', 'Salt']),
            document('other-MSDS.txt', [other_name, ''], product_candidates=False)]
    applied = ingest(case, http, docs, route)
    salt = db.query(models.Material).filter_by(case_id=c.case_id, name='Salt').one()
    assert applied['materials'] == 2
    assert applied['links'] == 1
    assert links(db, c) == {(keep.product_id, salt.material_id)}


@pytest.mark.parametrize('role,force,scope_frozen,status,code', [
    ('applicant', False, False, 409, 'CERT_SCOPE_FROZEN'),
    ('applicant', True, False, 403, 'NOT_AUTHORIZED'),
    ('consultant', False, False, 409, 'CERT_SCOPE_FROZEN'),
    ('consultant', True, False, 403, 'NOT_AUTHORIZED'),
    ('operator', False, False, 409, 'CERT_SCOPE_FROZEN'),
    ('operator', True, False, 200, None),
    ('admin', False, False, 409, 'CERT_SCOPE_FROZEN'),
    ('admin', True, False, 200, None),
    ('operator', True, True, 409, 'SCOPE_FROZEN'),
    ('admin', True, True, 409, 'SCOPE_FROZEN'),
])
def test_merge_target_certificate_requires_force_and_privileged_role(
        case, http, role, force, scope_frozen, status, code):
    db, c, keep, drop = case
    client, headers = http
    if role not in headers:
        credentials = {'username': 'admin', 'password': 'admin'} if role == 'admin' else {
            'username': role + '1', 'password': 'pw'}
        login = client.post('/auth/login', json=credentials)
        assert login.status_code == 200, login.text
        headers[role] = {'Authorization': 'Bearer ' + login.json()['token']}
    keep_id, drop_id = keep.product_id, drop.product_id
    material = models.Material(case_id=c.case_id, name='Water')
    db.add(material)
    db.flush()
    db.add(models.ProductMaterial(case_id=c.case_id, product_id=drop_id,
                                  material_id=material.material_id))
    cert = models.HalalCertificate(case_id=c.case_id, certificate_no='TARGET-ONLY',
        status='active', frozen_product_ids=[keep_id])
    db.add(cert)
    c.profile_ext = {'product_aliases': {keep_id: ['Keep alias'], drop_id: ['Drop alias']}}
    c.scope_frozen = scope_frozen
    db.commit()

    response = delete_product(case, http, role=role, force=force)
    assert response.status_code == status, response.text
    assert cert.frozen_product_ids == [keep_id]
    assert c.scope_frozen == scope_frozen
    audits = db.query(models.AuditLog).filter_by(case_id=c.case_id, action='product.delete')
    if code:
        assert response.json()['detail']['code'] == code
        if code == 'CERT_SCOPE_FROZEN':
            assert response.json()['detail']['certificates'] == ['TARGET-ONLY']
        assert db.get(models.Product, drop_id) is not None
        assert links(db, c) == {(drop_id, material.material_id)}
        assert c.profile_ext['product_aliases'] == {
            keep_id: ['Keep alias'], drop_id: ['Drop alias']}
        assert audits.count() == 0
    else:
        assert db.get(models.Product, drop_id) is None
        assert links(db, c) == {(keep_id, material.material_id)}
        assert c.profile_ext['product_aliases'] == {keep_id: ['Keep alias', EN, 'Drop alias']}
        assert response.json()['removed_from_certificates'] == []
        assert audits.one().meta['removed_from_certificates'] == []


@pytest.mark.parametrize('route', ['aggregate', 'apply', 'rename'])
@pytest.mark.parametrize('candidate_origin', ['aggregate', 'scope', 'rule-omission'])
def test_canonical_pending_product_blocks_shared_material(case, http, route, candidate_origin):
    db, c, keep, drop = case
    keep.name = 'Face Serum'
    db.delete(drop)  # 삭제 이력 없이 제품이 하나인 초기 상태를 만든다.
    db.commit()
    other = document('body-MSDS.txt', ['Body Serum'],
                     product_candidates=candidate_origin == 'aggregate')
    if candidate_origin == 'scope':
        other['fields']['_scope_pending'] = [{'kind': 'product_names', 'value': 'Body Serum'}]
    elif candidate_origin == 'rule-omission':
        other['fields']['_rule_omissions'] = {'product_names': ['Body Serum']}
    docs = [document('face-MSDS.txt', ['Face Serum'], ['Water', 'Salt']), other]

    applied = ingest(case, http, docs, route)
    pending = m.sm.pending_extractions(db, c.case_id)
    assert {item['value'] for item in pending if item['kind'] == 'product_names'} == {'Body Serum'}
    assert all(not (doc.fields or {}).get('_autofill', {}).get('product_pending')
               for doc in db.query(models.DocumentAsset).filter_by(case_id=c.case_id))
    salt = db.query(models.Material).filter_by(case_id=c.case_id, name='Salt').one()
    assert applied['materials'] == 2
    assert applied['links'] == 1
    assert links(db, c) == {(keep.product_id, salt.material_id)}


def test_canonical_pending_product_is_scoped_to_its_case(case, http):
    db, c, keep, drop = case
    keep.name = 'Face Serum'
    db.delete(drop)
    foreign = models.CaseApplication(org_id='org_demo', company_name='Foreign pending product')
    db.add(foreign)
    db.flush()
    db.add(models.DocumentAsset(case_id=foreign.case_id,
        **document('foreign-body-MSDS.txt', ['Body Serum'])))
    db.commit()
    assert any(item['kind'] == 'product_names' and item['value'] == 'Body Serum'
               for item in m.sm.pending_extractions(db, foreign.case_id))

    applied = ingest(case, http, [document('face-MSDS.txt', ['Face Serum'])], 'aggregate')
    assert not [item for item in m.sm.pending_extractions(db, c.case_id) if item['kind'] == 'product_names']
    assert applied['materials'] == applied['links'] == 1
    assert len(links(db, c)) == 1


@pytest.mark.parametrize('route', ['aggregate', 'apply', 'rename'])
@pytest.mark.parametrize('source_name', [KO, MSDS_NAME])
def test_resolved_pending_product_name_and_alias_allow_new_links(case, http, route, source_name):
    db, c, keep, _ = case
    doc = document('resolved-MSDS.txt', [source_name])
    doc['fields']['_autofill'] = {'product_pending': [
        {'kind': 'product_names', 'value': source_name, 'reason': 'possible_same_product'}]}
    stored = models.DocumentAsset(case_id=c.case_id, **doc)
    db.add(stored)
    db.commit()
    assert delete_product(case, http).status_code == 200
    assert stored.fields['_autofill']['product_pending']
    assert not [item for item in m.sm.pending_extractions(db, c.case_id) if item['kind'] == 'product_names']

    applied = ingest(case, http, [doc], route)
    material = db.query(models.Material).filter_by(case_id=c.case_id).one()
    assert applied['materials'] == applied['links'] == 1
    assert links(db, c) == {(keep.product_id, material.material_id)}
    assert EN in c.profile_ext['product_aliases'][keep.product_id]
    assert not [item for item in m.sm.pending_extractions(db, c.case_id) if item['kind'] == 'product_names']


@pytest.mark.parametrize('source_name', [KO, MSDS_NAME, 'Legacy alias'])
def test_other_product_pending_keeps_strict_matching_after_alias_merge(case, http, source_name):
    db, c, keep, drop = case
    c.profile_ext = {'product_aliases': {drop.product_id: ['Legacy alias']}}
    db.commit()
    assert delete_product(case, http).status_code == 200
    docs = [document('known-MSDS.txt', [source_name], ['Water', 'Salt']),
            document('other-MSDS.txt', ['Other serum'])]

    applied = ingest(case, http, docs, 'aggregate')
    pending_names = {m._norm_material(item['value']) for item in m.sm.pending_extractions(db, c.case_id)
                     if item['kind'] == 'product_names'}
    # 합친 이름은 해결되고, 다른 제품 문서에도 나타난 원료만 엄격 규칙에서 제외된다.
    assert pending_names == {'otherserum'}
    salt = db.query(models.Material).filter_by(case_id=c.case_id, name='Salt').one()
    assert applied['materials'] == 2
    assert applied['links'] == 1
    assert links(db, c) == {(keep.product_id, salt.material_id)}


def test_chained_merges_preserve_all_aliases_after_reanalysis(case, http):
    db, c, middle, first = case
    client, headers = http
    last = models.Product(case_id=c.case_id, name='Final Face Serum')
    db.add(last)
    db.flush()
    first_id, middle_id, last_id = first.product_id, middle.product_id, last.product_id
    c.profile_ext = {'product_aliases': {
        first_id: ['Earliest alias'], middle_id: ['Middle alias'], last_id: ['Final alias']}}
    db.commit()
    assert delete_product(case, http).status_code == 200
    response = client.delete(f'/cases/{c.case_id}/products/{middle_id}',
        params={'merge_into': last_id}, headers=headers['applicant'])
    assert response.status_code == 200, response.text
    db.expire_all()
    expected = {'Final alias', KO, 'Middle alias', EN, 'Earliest alias'}
    assert set(c.profile_ext['product_aliases'][last_id]) == expected
    assert not m.sm.rejected_product_autofill_names(db, c.case_id)

    applied = ingest(case, http, [document('chain-MSDS.txt', [EN, 'Earliest alias'], [])], 'aggregate')
    assert applied['products'] == 0
    assert set(c.profile_ext['product_aliases']) == {last_id}
    assert set(c.profile_ext['product_aliases'][last_id]) == expected
    assert db.get(models.Product, first_id) is None
    assert db.get(models.Product, middle_id) is None
    assert not m.sm.rejected_product_autofill_names(db, c.case_id)

    # 최종 생존 제품까지 일반 삭제하면 예전 이름도 더는 자동등록할 수 없다.
    response = client.delete(f'/cases/{c.case_id}/products/{last_id}', headers=headers['applicant'])
    assert response.status_code == 200, response.text
    db.expire_all()
    assert {m._norm_material(name) for name in expected | {'Final Face Serum'}} <= (
        m.sm.rejected_product_autofill_names(db, c.case_id))


@pytest.mark.parametrize('ending', ['missing', 'ordinary', 'cycle', 'self-cycle', 'other-case',
                                    'foreign-survivor', 'unnamed'])
def test_merge_chain_rejects_unresolved_or_cyclic_history(case, ending):
    db, c, keep, _ = case
    first_id, middle_id = models.uid(), models.uid()
    actor = {'uid': 'operator1', 'role': 'operator'}
    m._audit(db, actor, 'product.delete', 'product', first_id, c.case_id,
        {'name': 'First candidate', 'aliases': ['Earliest alias'], 'merged_into': middle_id}, commit=False)
    owner = c
    meta = {'name': 'Middle candidate', 'merged_into': keep.product_id}
    if ending == 'ordinary':
        meta.pop('merged_into')
    elif ending == 'cycle':
        meta['merged_into'] = first_id
    elif ending == 'self-cycle':
        meta['merged_into'] = middle_id
    elif ending in ('other-case', 'foreign-survivor'):
        foreign = models.CaseApplication(org_id='org_demo', company_name='Foreign merge history')
        db.add(foreign)
        db.flush()
        if ending == 'other-case':
            owner = foreign
        else:
            product = models.Product(case_id=foreign.case_id, name='Foreign survivor')
            db.add(product)
            db.flush()
            meta['merged_into'] = product.product_id
    elif ending == 'unnamed':
        meta['name'] = '   '
    if ending != 'missing':
        m._audit(db, actor, 'product.delete', 'product', middle_id, owner.case_id, meta, commit=False)
    db.commit()

    rejected = m.sm.rejected_product_autofill_names(db, c.case_id)
    assert {'firstcandidate', 'earliestalias'} <= rejected
    assert ('middlecandidate' in rejected) == (ending not in ('missing', 'other-case', 'unnamed'))
    assert m._norm_material(keep.name) not in rejected


def test_failed_other_reanalysis_preserves_error_and_strict_sources(case, http, monkeypatch):
    db, c, keep, _ = case
    assert delete_product(case, http).status_code == 200
    text = 'No | Product | Category\n1 | Face | Cosmetics\n2 | Body | Cosmetics\n'
    failed = models.DocumentAsset(case_id=c.case_id, filename='table.txt', doc_type='other',
        content_b64=base64.b64encode(text.encode()).decode())
    db.add(failed)
    db.commit()
    monkeypatch.setattr(intake.ai_local, 'llm_json', lambda *args, **kwargs: {'error': 'TIMEOUT'})
    client, headers = http
    response = client.post(f'/documents/{failed.document_id}/reprocess', headers=headers['operator'])
    assert response.status_code == 200, response.text
    db.expire_all()
    assert failed.doc_type == 'other'
    assert failed.confidence == 0.0
    assert failed.fields['_llm_error']
    applied = ingest(case, http, [document('face-MSDS.txt', [MSDS_NAME], ['Water', 'Salt']),
        document('unknown-MSDS.txt', [], product_candidates=False)], 'aggregate')
    salt = db.query(models.Material).filter_by(case_id=c.case_id, name='Salt').one()
    assert applied['materials'] == 2
    assert applied['links'] == 1
    assert links(db, c) == {(keep.product_id, salt.material_id)}


def test_failed_profile_retry_preserves_error_and_strict_sources(case, http, monkeypatch):
    db, c, keep, _ = case
    assert delete_product(case, http).status_code == 200
    text = 'No | Product | Category\n1 | Face | Cosmetics\n2 | Body | Cosmetics\n'
    failed = models.DocumentAsset(case_id=c.case_id, filename='사업자등록증.txt',
        doc_type='nib_business_license', confidence=.95, fields={'_llm_error': 'TIMEOUT'},
        content_b64=base64.b64encode(text.encode()).decode())
    db.add(failed)
    db.commit()
    monkeypatch.setattr(intake.ai_local, 'provider', lambda: 'openai')
    monkeypatch.setattr(intake.ai_local, 'llm_json', lambda *args, **kwargs: {'error': 'TIMEOUT'})
    client, headers = http
    response = client.post(f'/cases/{c.case_id}/profile/autofill', headers=headers['applicant'])
    assert response.status_code == 200, response.text
    db.expire_all()
    assert failed.fields['_llm_error'] == 'TIMEOUT'
    assert failed.doc_type == 'nib_business_license'
    applied = ingest(case, http, [document('face-MSDS.txt', [MSDS_NAME], ['Water', 'Salt']),
        document('body-MSDS.txt', [], product_candidates=False)], 'aggregate')
    salt = db.query(models.Material).filter_by(case_id=c.case_id, name='Salt').one()
    assert applied['materials'] == 2
    assert applied['links'] == 1
    assert links(db, c) == {(keep.product_id, salt.material_id)}


@pytest.mark.parametrize('route', ['profile', 'reprocess', 'reupload'])
@pytest.mark.parametrize('failure', ['llm', 'empty-llm', 'ocr'])
def test_extraction_retries_clear_errors_only_after_success(case, http, monkeypatch, route, failure):
    db, c, _, _ = case
    text = '사업자등록증\n추출 재시도 회귀 문서'
    content = base64.b64encode(text.encode()).decode()
    preserved = {'_autofill': {'changes': {}}, '_scope_pending': [],
                 '_counts_as_evidence': False, '_upload_id': 'original-upload'}
    doc = models.DocumentAsset(case_id=c.case_id, filename='사업자등록증.txt',
        doc_type='nib_business_license', confidence=.95, text_full=text,
        content_b64=content, file_hash=m._sha256_b64(content),
        fields={'_llm_error': 'OLD_LLM', '_ocr_error': 'OLD_OCR', **preserved})
    db.add(doc)
    db.commit()
    client, headers = http
    for success in (False, True):
        parsed_text = (intake._ParsedText('', {'_ocr_error': 'TIMEOUT'})
                       if not success and failure == 'ocr' else text)
        result = {'doc_type': 'nib_business_license', 'confidence': .95,
                  'fields': {} if not success and failure == 'empty-llm' else {'_parser_revision': 2}}
        if not success and failure != 'ocr':
            result['llm_error'] = 'TIMEOUT'
        monkeypatch.setattr(intake, 'parse_file', lambda *args, **kwargs: parsed_text)
        monkeypatch.setattr(intake, 'classify', lambda *args, **kwargs: result)
        monkeypatch.setattr(intake, 'parse_typed_text', lambda *args, **kwargs: result)
        if route == 'reupload':
            row = {**result, 'filename': doc.filename, 'content_b64': content, 'text_full': parsed_text}
            applied = m._apply_intake_autofill(db, c, {'classified': [row], 'extracted': {}})
            assert applied['dup_skipped'] == 1
            db.commit()
        else:
            endpoint = (f'/cases/{c.case_id}/profile/autofill' if route == 'profile' else
                        f'/documents/{doc.document_id}/reprocess?apply=false')
            response = client.post(endpoint, headers=headers['applicant' if route == 'profile' else 'operator'])
            assert response.status_code == 200, response.text
        db.expire_all()
        if success:
            assert '_llm_error' not in doc.fields
            assert '_ocr_error' not in doc.fields
            assert doc.fields['_parser_revision'] == 2
        else:
            assert doc.fields['_llm_error'] == ('OLD_LLM' if failure == 'ocr' else 'TIMEOUT')
            assert doc.fields['_ocr_error'] == ('TIMEOUT' if failure == 'ocr' else 'OLD_OCR')
        assert {key: doc.fields[key] for key in preserved} == preserved


@pytest.mark.parametrize('stage', ['ocr', 'llm'])
def test_profile_retry_exceptions_are_saved_as_failures(case, http, monkeypatch, stage):
    db, c, _, _ = case
    doc = models.DocumentAsset(case_id=c.case_id, filename='사업자등록증.txt',
        doc_type='nib_business_license', fields={}, content_b64=base64.b64encode(b'Original').decode())
    db.add(doc)
    db.commit()

    def fail(*args, **kwargs):
        raise RuntimeError('extraction failed')

    monkeypatch.setattr(intake, 'parse_file', fail if stage == 'ocr' else lambda *args, **kwargs: 'Original')
    monkeypatch.setattr(intake, 'classify', fail)
    client, headers = http
    response = client.post(f'/cases/{c.case_id}/profile/autofill', headers=headers['applicant'])
    assert response.status_code == 200, response.text
    db.expire_all()
    assert doc.fields['_' + stage + '_error'] == 'ERROR'


def test_document_review_preserves_empty_analysis_error_markers(case, http):
    db, c, _, _ = case
    errors = {'_llm_error': '', '_ocr_error': None}
    doc = models.DocumentAsset(case_id=c.case_id, filename='failed.txt', doc_type='other',
                               fields={**errors, '_autofill': {'changes': {}}})
    db.add(doc)
    db.commit()
    client, headers = http
    response = client.patch(f'/documents/{doc.document_id}/review', headers=headers['auditor'],
                            json={'review_status': 'approved'})
    assert response.status_code == 200, response.text
    db.expire_all()
    assert {key: doc.fields[key] for key in errors} == errors


@pytest.mark.parametrize('confidence', [.2, 0.0])
def test_profile_reclassification_updates_confidence(case, http, monkeypatch, confidence):
    db, c, _, _ = case
    text = 'No | Product | Category\n1 | Face Serum | Cosmetics\n2 | Body Serum | Cosmetics\n'
    doc = models.DocumentAsset(case_id=c.case_id, filename='사업자등록증.txt',
        doc_type='nib_business_license', confidence=.95, fields={},
        content_b64=base64.b64encode(text.encode()).decode())
    db.add(doc)
    db.commit()
    monkeypatch.setattr(intake, 'parse_file', lambda *args, **kwargs: text)
    monkeypatch.setattr(intake, 'classify', lambda *args, **kwargs: {
        'doc_type': 'other', 'confidence': confidence, 'fields': {'_parser_revision': 2}})
    client, headers = http
    response = client.post(f'/cases/{c.case_id}/profile/autofill', headers=headers['applicant'])
    assert response.status_code == 200, response.text
    db.expire_all()
    assert doc.doc_type == 'other'
    assert doc.confidence == confidence
    assert doc.text_full == text


@pytest.mark.parametrize('route', ['parse-file', 'profile/autofill'])
def test_business_autofill_waiting_for_analysis_preserves_concurrent_merge(
        case, http, monkeypatch, route):
    db, c, keep, drop = case
    case_id, keep_id, drop_id = c.case_id, keep.product_id, drop.product_id
    c.nib = '1234567890'
    c.profile_ext = {'country': 'KR', 'product_aliases': {
        keep_id: ['Face Peel'], drop_id: ['Legacy Face Serum']}}
    fields = {'company_name': c.company_name, 'nib': c.nib, 'business_type': 'Manufacturing'}
    text = (f'사업자등록증\n상호: {c.company_name}\n사업자등록번호: {c.nib}\n'
            'Business type: Manufacturing\n')
    content = base64.b64encode(text.encode()).decode()
    if route == 'profile/autofill':
        db.add(models.DocumentAsset(case_id=case_id, filename='사업자등록증.txt',
            doc_type='nib_business_license', confidence=.95, fields={}, content_b64=content))
    db.commit()
    waiting, resume = threading.Event(), threading.Event()

    def analyze(*args, **kwargs):
        waiting.set()
        assert resume.wait(10), '분석 대기 중 합치기가 완료되지 않았습니다'
        return {'doc_type': 'nib_business_license', 'confidence': .95,
                'fields': dict(fields), 'text': text}

    monkeypatch.setattr(intake, 'parse_typed', analyze)
    monkeypatch.setattr(intake, 'classify', analyze)
    client, headers = http
    body = ({'filename': '사업자등록증.txt', 'doc_type': 'nib_business_license',
             'file_b64': content} if route == 'parse-file' else {})
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, f'/cases/{case_id}/{route}',
                             headers=headers['applicant'], json=body)
        try:
            assert waiting.wait(10), '등록증 분석이 시작되지 않았습니다'
            response = delete_product(case, http)
            assert response.status_code == 200, response.text
            # 분석 시작 후 저장된 별개의 프로필 키도 함께 보존해야 한다.
            c.profile_ext = {**c.profile_ext, 'concurrent_note': 'keep latest'}
            db.commit()
        finally:
            resume.set()
        response = future.result(timeout=15)
    assert response.status_code == 200, response.text
    db.expire_all()
    assert c.profile_ext == {'country': 'KR', 'concurrent_note': 'keep latest',
        'business_type': 'Manufacturing', 'company_name_en': fields['company_name'],
        'product_aliases': {keep_id: ['Face Peel', EN, 'Legacy Face Serum']}}
    assert db.get(models.Product, drop_id) is None
    stored = db.query(models.DocumentAsset).filter_by(case_id=case_id).one()
    assert stored.fields['_autofill']['changes']['profile.business_type']['value'] == 'Manufacturing'


@pytest.mark.parametrize('route', ['intake-zip', 'intake-zip-stream', 'parse-file'])
@pytest.mark.parametrize('incoming', ['paired', 'merged-name'])
def test_upload_waiting_for_analysis_preserves_concurrent_merge(
        case, http, monkeypatch, route, incoming):
    db, c, keep, drop = case
    if incoming == 'merged-name':
        keep.name, drop.name = 'Primary Face Serum', 'Alternate Face Serum'
    case_id, keep_id, drop_id = c.case_id, keep.product_id, drop.product_id
    keep_name, drop_name = keep.name, drop.name
    c.profile_ext = {'country': 'KR', 'product_aliases': {
        keep_id: ['Face Peel'], drop_id: ['Legacy Face Serum']}}
    db.commit()
    names = [keep_name, drop_name] if incoming == 'paired' else [drop_name]
    fields = {'product_names': names}
    if incoming == 'paired':
        fields.update(product_name_ko=keep_name, product_name_en=drop_name)
    text = 'Product list\n' + '\n'.join(names)
    waiting, resume = threading.Event(), threading.Event()

    def analyze(*args, **kwargs):
        waiting.set()
        assert resume.wait(10), '분석 대기 중 합치기가 완료되지 않았습니다'
        return {'doc_type': 'product_list', 'confidence': .95, 'fields': fields,
                'text': text, 'text_full': text, 'excerpt': text}

    monkeypatch.setattr(intake, 'classify', analyze)
    monkeypatch.setattr(intake, 'parse_typed', analyze)
    if route == 'parse-file':
        body = {'filename': 'product_list.txt', 'doc_type': 'product_list',
                'file_b64': base64.b64encode(text.encode()).decode()}
    else:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('product_list.txt', text)
        body = {'zip_b64': base64.b64encode(archive.getvalue()).decode()}
    client, headers = http
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, f'/cases/{case_id}/{route}',
                             headers=headers['applicant'], json=body)
        try:
            assert waiting.wait(10), '업로드 분석이 시작되지 않았습니다'
            response = delete_product(case, http)
            assert response.status_code == 200, response.text
            assert 'Legacy Face Serum' in c.profile_ext['product_aliases'][keep_id]
        finally:
            resume.set()
        response = future.result(timeout=15)
    assert response.status_code == 200, response.text
    if route == 'intake-zip-stream':
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        assert events[-1]['type'] == 'result', events
        applied = events[-1]['applied']
    else:
        applied = response.json()['applied']
    db.expire_all()
    assert applied['products_added' if route == 'parse-file' else 'products'] == 0
    assert [p.product_id for p in db.query(models.Product).filter_by(case_id=case_id)] == [keep_id]
    assert db.get(models.Product, drop_id) is None
    assert c.profile_ext == {'country': 'KR', 'product_aliases': {
        keep_id: ['Face Peel', drop_name, 'Legacy Face Serum']}}


@pytest.mark.parametrize('source_name', [KO, EN, 'Face Peel'])
@pytest.mark.parametrize('route', ['aggregate', 'reprocess'])
def test_two_merges_into_same_product_reanalysis_keeps_aliases_resolved(
        case, http, monkeypatch, source_name, route):
    db, c, keep, _ = case
    second = models.Product(case_id=c.case_id, name='Face Peel')
    face = document('face-MSDS.txt', [source_name], ['Water', 'Salt'])
    stored_face = models.DocumentAsset(case_id=c.case_id, **face,
        content_b64=base64.b64encode(face['text_full'].encode()).decode())
    db.add_all([second, stored_face,
        models.DocumentAsset(case_id=c.case_id, **document('unknown-MSDS.txt', []))])
    db.commit()
    assert delete_product(case, http).status_code == 200
    client, headers = http
    response = client.delete(f'/cases/{c.case_id}/products/{second.product_id}',
        params={'merge_into': keep.product_id}, headers=headers['applicant'])
    assert response.status_code == 200, response.text
    db.expire_all()
    assert c.profile_ext['product_aliases'][keep.product_id] == [EN, 'Face Peel']

    if route == 'aggregate':
        applied = ingest(case, http, [face], route)
    else:
        monkeypatch.setattr(intake.ai_local, 'llm_json', lambda *args, **kwargs: {
            'doc_type': 'coa_msds', 'confidence': .95, 'fields': face['fields']})
        response = client.post(f'/documents/{stored_face.document_id}/reprocess', headers=headers['operator'])
        assert response.status_code == 200, response.text
        applied = response.json()['applied']
        db.expire_all()
    assert not [item for item in m.sm.pending_extractions(db, c.case_id) if item['kind'] == 'product_names']
    assert not stored_face.fields.get('_autofill', {}).get('product_pending')
    assert [p.product_id for p in db.query(models.Product).filter_by(case_id=c.case_id)] == [keep.product_id]
    rows = db.query(models.Material).filter_by(case_id=c.case_id).all()
    assert {row.name for row in rows} == {'Water', 'Salt'}
    assert applied['materials'] == 2
    assert applied['links'] == 1
    assert links(db, c) == {(keep.product_id, row.material_id) for row in rows if row.name == 'Salt'}


@pytest.mark.parametrize('shared_alias', [False, True])
def test_multiple_aliases_still_detect_cross_product_conflicts(case, shared_alias):
    _, c, keep, other = case
    other.name = 'Body Serum'
    aliases = {keep.product_id: [EN, 'Face Peel'], other.product_id: ['Body Peel']}
    expected = {m._norm_material(name) for name in (KO, EN, 'Face Peel', 'Body Serum', 'Body Peel')}
    if shared_alias:
        aliases[other.product_id].append('Face Peel')
    assert m.sm.product_alias_conflicts([keep, other], aliases, []) == (expected if shared_alias else set())
    paired = models.DocumentAsset(case_id=c.case_id, filename='paired.txt', doc_type='coa_msds',
        text_full=f'제품명: {KO} / Body Serum', fields={'product_names': [KO, 'Body Serum']})
    assert m.sm.product_alias_conflicts([keep, other], aliases, [paired]) == expected


@pytest.mark.parametrize('dedup_key', ['hash', 'filename'])
def test_reupload_preserves_fields_committed_by_frozen_reanalysis(case, http, monkeypatch, dedup_key):
    db, c, keep, _ = case
    assert delete_product(case, http).status_code == 200
    original = document('pending-MSDS.txt', [], [])
    extracted = document('pending-MSDS.txt', [], ['Water'])
    content = base64.b64encode(extracted['text_full'].encode()).decode()
    stored = models.DocumentAsset(case_id=c.case_id, **original, content_b64=content,
        file_hash=m._sha256_b64(content) if dedup_key == 'hash' else None)
    db.add_all([stored, models.DocumentAsset(case_id=c.case_id,
        **document('unknown-MSDS.txt', [], product_candidates=False))])
    db.commit()
    case_id, document_id, product_id = c.case_id, stored.document_id, keep.product_id
    cached, resume = threading.Event(), threading.Event()
    monkeypatch.setattr(intake.ai_local, 'llm_json', lambda *args, **kwargs: {
        'doc_type': 'coa_msds', 'confidence': .95, 'fields': extracted['fields']})

    def reupload():
        with m.SessionLocal() as upload_db:
            upload_case = upload_db.get(models.CaseApplication, case_id)
            stale = upload_db.get(models.DocumentAsset, document_id)
            assert '_scope_pending' not in stale.fields
            cached.set()
            assert resume.wait(10), '동결 재분석이 완료되지 않았습니다'
            assert '_scope_pending' not in stale.fields  # identity map은 여전히 이전 값이다.
            incoming = document('pending-MSDS.txt', [], [])
            incoming['fields']['_parser_revision'] = 2
            if dedup_key == 'hash':
                incoming['content_b64'] = content
            docs = [incoming, document('face-MSDS.txt', [MSDS_NAME], ['Water', 'Salt'])]
            applied = m._apply_intake_autofill(upload_db, upload_case, {
                'classified': docs, 'extracted': intake.aggregate_fields(docs)})
            upload_db.commit()
            return applied

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(reupload)
        try:
            assert cached.wait(10), '재업로드의 문서 조회가 완료되지 않았습니다'
            with m.SessionLocal() as other:
                latest = m._lock_case(other, case_id)
                m.sm.freeze_scope(other, latest)
                other.commit()
                result = m._reprocess_doc(other, other.get(models.DocumentAsset, document_id), latest)
                assert result['applied']['scope_frozen']
                pending = result['fields']['_scope_pending']
                assert any(x['kind'] == 'material_names' and x['value'] == 'Water' for x in pending)
                latest = m._lock_case(other, case_id)
                current = other.get(models.DocumentAsset, document_id, populate_existing=True)
                preserved = {'_scope_pending': pending, '_autofill': {'changes': {}},
                             '_counts_as_evidence': False, '_upload_id': 'reviewed-upload'}
                current.fields = {**current.fields, **preserved}
                m.sm.lock_scope(other, latest)
                latest.scope_frozen = False
                other.commit()
        finally:
            resume.set()
        applied = future.result(timeout=10)
    db.expire_all()
    assert applied['dup_skipped'] == 1
    assert applied['materials'] == 2
    assert applied['links'] == 1
    salt = db.query(models.Material).filter_by(case_id=case_id, name='Salt').one()
    assert links(db, c) == {(product_id, salt.material_id)}
    assert c.scope_frozen is False
    assert stored.fields['_parser_revision'] == 2
    assert {key: stored.fields[key] for key in preserved} == preserved
    assert db.query(models.DocumentAsset).filter_by(case_id=case_id).count() == 3


def test_strict_autolink_reads_latest_case_aliases_after_lock(case, http):
    db, c, keep, _ = case
    assert delete_product(case, http).status_code == 200
    rows = [models.Material(case_id=c.case_id, name=name) for name in ('Water', 'Salt')]
    db.add_all(rows)
    db.commit()
    case_id, product_id = c.case_id, keep.product_id
    assert EN in c.profile_ext['product_aliases'][product_id]
    with m.SessionLocal() as other:
        latest = m._lock_case(other, case_id)
        latest.profile_ext = {**latest.profile_ext, 'product_aliases': {product_id: []}}
        other.commit()
    assert EN in c.profile_ext['product_aliases'][product_id]

    assert m._auto_link_pm(db, case_id, single_product=True,
        material_ids=[row.material_id for row in rows],
        material_sources={'water': [{MSDS_NAME}], 'salt': [{KO}]}) == 1
    db.flush()
    assert links(db, c) == {(product_id, rows[1].material_id)}


@pytest.mark.parametrize('doc_type', ['coa_msds', 'product_label'])
def test_unsaved_upload_is_included_in_strict_source_check(case, http, doc_type):
    db, c, keep, _ = case
    assert delete_product(case, http).status_code == 200
    db.add(models.DocumentAsset(case_id=c.case_id, **document('unknown-MSDS.txt', [])))
    db.commit()
    incoming = document('incoming.txt', [MSDS_NAME], ['Water', 'Salt'])
    incoming['doc_type'] = doc_type
    doc = models.DocumentAsset(case_id=c.case_id, **incoming)
    # parse-file 경로처럼 스크리닝 전에는 문서를 아직 세션에 추가하지 않는다.
    assert doc not in db
    applied = m._apply_agg_to_case(db, c, intake.aggregate_fields([incoming]), documents=[doc])
    db.flush()
    rows = db.query(models.Material).filter_by(case_id=c.case_id).all()
    assert applied['materials'] == 2
    assert applied['links'] == 1
    assert links(db, c) == {(keep.product_id, row.material_id) for row in rows
                           if row.name == 'Salt'}


@pytest.mark.parametrize('same_case', [False, True])
@pytest.mark.parametrize('route', ['aggregate', 'reprocess'])
def test_other_document_scope_pending_preserves_strict_sources_on_single_document_reanalysis(
        case, http, monkeypatch, same_case, route):
    db, c, keep, _ = case
    assert delete_product(case, http).status_code == 200
    owner = c
    if not same_case:
        owner = models.CaseApplication(org_id='org_demo', company_name='Foreign scope pending')
        db.add(owner)
        db.flush()
    pending = document('pending-MSDS.txt', [], [])
    pending['fields']['_scope_pending'] = [{'kind': 'material_names', 'value': 'Water'}]
    stored_pending = models.DocumentAsset(case_id=owner.case_id, **pending)
    face = document('face-MSDS.txt', [MSDS_NAME], ['Water', 'Salt'])
    stored_face = models.DocumentAsset(case_id=c.case_id, **face,
        content_b64=base64.b64encode(face['text_full'].encode()).decode())
    db.add_all([stored_pending, stored_face])
    db.commit()
    assert c.scope_frozen is False
    assert not [item for item in m.sm.pending_extractions(db, c.case_id) if item['kind'] == 'product_names']

    if route == 'aggregate':
        applied = ingest(case, http, [face], route)
        assert applied['dup_skipped'] == 1
    else:
        monkeypatch.setattr(intake.ai_local, 'llm_json', lambda *args, **kwargs: {
            'doc_type': 'coa_msds', 'confidence': .95, 'fields': {
                'product_names': [MSDS_NAME], 'material_names': ['Water', 'Salt']}})
        client, headers = http
        response = client.post(f'/documents/{stored_face.document_id}/reprocess',
                               headers=headers['operator'])
        assert response.status_code == 200, response.text
        applied = response.json()['applied']
        db.expire_all()
    assert applied['materials'] == 2
    assert applied['links'] == (1 if same_case else 2)
    rows = db.query(models.Material).filter_by(case_id=c.case_id).all()
    assert links(db, c) == {(keep.product_id, row.material_id) for row in rows
                           if not same_case or row.name == 'Salt'}
    assert stored_pending.fields['_scope_pending'] == pending['fields']['_scope_pending']


@pytest.mark.parametrize('route', ['aggregate', 'apply', 'rename'])
@pytest.mark.parametrize('reverse', [False, True], ids=['face-first', 'body-first'])
def test_scope_pending_sources_survive_reanalysis_in_both_document_orders(case, http, route, reverse):
    db, c, keep, drop = case
    keep.name = 'Face Serum'
    db.delete(drop)
    c.scope_frozen = True
    db.commit()
    face = document('face-MSDS.txt', ['Face Serum'], ['Water', 'Salt'])
    body = document('body-MSDS.txt', ['Body Serum'])
    docs = [body, face] if reverse else [face, body]
    frozen = ingest(case, http, docs, 'aggregate')
    assert frozen['scope_frozen']
    assert not db.query(models.Material).filter_by(case_id=c.case_id).count()

    # 동결 중 재분석에서 Water 추출이 사라져도 이전 보류와 그 출처는 남는다.
    body = document('body-MSDS.txt', ['Body Serum'], [])
    reanalyzed = ingest(case, http, [body], 'aggregate')
    assert reanalyzed['scope_frozen']
    assert reanalyzed['dup_skipped'] == 1
    stored_body = db.query(models.DocumentAsset).filter_by(case_id=c.case_id, filename='body-MSDS.txt').one()
    assert stored_body.fields['material_names'] == []
    assert any(item['kind'] == 'material_names' and item['value'] == 'Water'
               for item in stored_body.fields['_scope_pending'])
    c.scope_frozen = False
    db.commit()

    if route == 'aggregate':
        # 동결 해제 후 Face 한 문서만 재분석해도 저장된 Body 보류 출처를 함께 검사한다.
        applied = ingest(case, http, [face], route)
    else:
        client, headers = http
        selection = {'values': ['Water', 'Salt']}
        if route == 'rename':
            selection.update(renames={'Water': 'Purified Water'}, reason='Verified material name')
        response = client.post(f'/cases/{c.case_id}/pending-extractions/apply',
                               headers=headers['operator'], json=selection)
        assert response.status_code == 200, response.text
        db.expire_all()
        applied = response.json()['applied']
    salt = db.query(models.Material).filter_by(case_id=c.case_id, name='Salt').one()
    assert applied['materials'] == 2
    assert applied['links'] == 1
    assert links(db, c) == {(keep.product_id, salt.material_id)}
    assert [p.product_id for p in db.query(models.Product).filter_by(case_id=c.case_id)] == [keep.product_id]


@pytest.mark.parametrize('alias_origin', ['product-name', 'product-alias'])
def test_rejected_name_preserved_by_manual_merge_survives_reanalysis(case, http, alias_origin):
    db, c, keep, drop = case
    client, headers = http
    legacy = 'Legacy Serum'
    db.delete(drop)
    db.add(models.DocumentAsset(case_id=c.case_id, **document('legacy-candidate.txt', [legacy], [])))
    db.commit()
    response = client.post(f'/cases/{c.case_id}/pending-extractions/apply', headers=headers['operator'],
        json={'dismiss': [legacy], 'reason': 'Earlier review rejection'})
    assert response.status_code == 200, response.text
    response = client.post(f'/cases/{c.case_id}/products', headers=headers['applicant'],
        json={'name': legacy if alias_origin == 'product-name' else 'Legacy product'})
    assert response.status_code == 200, response.text
    source_id = response.json()['product_id']
    db.expire_all()
    if alias_origin == 'product-alias':
        c.profile_ext = {**(c.profile_ext or {}), 'product_aliases': {source_id: [legacy]}}
        db.commit()
    response = client.delete(f'/cases/{c.case_id}/products/{source_id}',
        params={'merge_into': keep.product_id}, headers=headers['applicant'])
    assert response.status_code == 200, response.text
    db.expire_all()
    assert legacy in c.profile_ext['product_aliases'][keep.product_id]

    for material_name in ('Water', 'Salt'):
        applied = ingest(case, http, [document('legacy-MSDS.txt', [legacy], [material_name])], 'aggregate')
        assert legacy in c.profile_ext['product_aliases'][keep.product_id]
        assert applied['products'] == 0
        assert applied['materials'] == applied['links'] == 1
        assert [p.product_id for p in db.query(models.Product).filter_by(case_id=c.case_id)] == [keep.product_id]
        assert m._norm_material(legacy) in m.sm.rejected_product_autofill_names(db, c.case_id)
    assert len(links(db, c)) == 2
    assert not [item for item in m.sm.pending_extractions(db, c.case_id) if item['kind'] == 'product_names']


def test_rejected_alias_without_matching_merge_evidence_is_still_removed(case, http):
    db, c, keep, _ = case
    assert delete_product(case, http).status_code == 200
    legacy = 'Unmerged rejected alias'
    c.profile_ext = {**c.profile_ext, 'product_aliases': {keep.product_id: [EN, legacy]}}
    m.sm.record_event(db, c, c.status, c.status, 'extraction.review', 'operator', 'operator1',
        {'items': [{'kind': 'product_names', 'value': legacy}], 'dismiss': [legacy], 'reason': 'Other product'})
    db.commit()
    ingest(case, http, [document('known-MSDS.txt', [MSDS_NAME], [])], 'aggregate')
    assert c.profile_ext['product_aliases'][keep.product_id] == [EN]
