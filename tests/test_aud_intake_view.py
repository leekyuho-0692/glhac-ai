"""Intake regression: actual HTTP writes, isolated DB and deterministic extraction fixtures."""
import base64
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import auth, intake, models, main as m
from app.db import engine


def ok(response, status=200):
    assert response.status_code == status, response.text[:700]
    return response.json()


def staff(role, org='org_demo'):
    with m.SessionLocal() as db:
        u = models.User(username='intake_' + uuid4().hex[:16], password_hash=auth.hash_pw('Passw0rd!23'), role=role, org_id=org)
        db.add(u)
        db.commit()
        return {'Authorization': 'Bearer ' + auth.make_token(u)}, u.user_id


def assign(cid, role, uid):
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        m.sm.record_event(db, c, c.status, c.status, 'ops.'+role+'_assigned', 'operator', 'fixture', {role+'_id': uid})
        db.commit()


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(m, '_AUTOPARSE', False)
    monkeypatch.setattr(m, 'verify_enqueue', lambda *a, **kw: False)
    monkeypatch.setattr(m, '_RL_WRITE_HITS', {})
    monkeypatch.setattr(intake.ai_local, 'llm_json', lambda *a, **kw: {'error': 'offline'})
    with TestClient(m.app) as client:
        registered = ok(client.post('/auth/register', json={
            'username': 'intake_'+uuid4().hex[:12]+'@example.com', 'password': 'Passw0rd!23',
            'company_name': 'Intake Foods '+uuid4().hex[:8], 'company_name_en': 'Intake Foods Ltd.',
            'company_email': 'company@example.com', 'email_consent': True,
            'nib': str(uuid4().int % 10**10).zfill(10), 'responsible_person': 'Representative', 'address': 'Seoul office',
            'sectors': ['food'], 'contacts': {
                'pic': {'name': 'Contact One', 'title': 'Manager', 'phone': '+821011112222', 'email': 'pic@example.com'},
                'cp': {'name': 'Contact Two', 'title': 'Director', 'phone': '+821033334444', 'email': 'cp@example.com'}}}))
        applicant = {'Authorization': 'Bearer '+registered['token']}
        cid = ok(client.get('/cases', headers=applicant))['items'][0]['case_id']
        roles = {'applicant': applicant}
        ids = {}
        for role in ('auditor', 'consultant', 'operator', 'admin', 'fatwa_liaison'):
            roles[role], ids[role] = staff(role)
        assign(cid, 'auditor', ids['auditor'])
        assign(cid, 'consultant', ids['consultant'])
        yield client, cid, roles, ids


def seed_document(client, cid, headers):
    uploaded = ok(client.post(f'/cases/{cid}/documents', headers=headers, json={
        'filename': 'company.txt', 'doc_type': 'nib_business_license',
        'file_b64': base64.b64encode(b'Intake Foods company address').decode()}))
    with m.SessionLocal() as db:
        doc = db.query(models.DocumentAsset).filter_by(case_id=cid).first()
        doc.doc_type = 'nib_business_license'
        doc.confidence = .9
        doc.text_full = 'Intake Foods company address'
        doc.fields = {
            'company_name': 'Intake Foods Ltd.', 'address': 'Suggested address',
            'office_phone': '1234', 'city': 'Seoul', 'material_names': ['Pending material'],
            'custom': {'visible': 'kept', '_raw': 'secret-marker', 'access_token': 'secret-marker'},
            '_sources': {'company_name': 'structural', 'address': 'llm', 'office_phone': 'manual', 'city': 'filename', 'material_names': 'llm'},
            '_verify': {'verdict': 'warn', 'field_issues': [{'field': 'address', 'issue': 'needs checking', 'token': 'secret-marker'}], '_raw': 'secret-marker'},
            '_truncated': {'kind': 'text_chars', 'limit': 1200}, '_secret': 'secret-marker',
            'password_hash': 'secret-marker',
            '_scope_pending': [{'kind': 'material_names', 'value': 'Pending material', 'reason': 'llm'}],
            '_autofill': {'changes': {'address': {'field': 'address', 'scope': 'case', 'before': 'Old office', 'value': 'New office', 'status': 'applied'}},
                          'conflicts': [{'field': 'address', 'scope': 'case', 'current': 'Seoul office', 'proposed': 'Suggested address', 'status': 'pending', 'reason': 'value_conflict'}]},
        }
        db.commit()
        return doc.document_id


@pytest.mark.parametrize("endpoint", ["documents", "intake-view", "ai-extractions"])
@pytest.mark.parametrize("role", ["applicant", "consultant", "auditor", "operator", "admin", "fatwa_liaison"])
def test_advisory_filtered_on_every_document_read_response(setup, role, endpoint):
    client, cid, roles, ids = setup
    did = seed_document(client, cid, roles["applicant"])
    with m.SessionLocal() as db:
        doc = db.get(models.DocumentAsset, did)
        doc.fields = {**doc.fields, "_verify": {"verdict": "warn", "field_issues": [],
            "advisory": [{"field": "address", "issue": "STAFF_ONLY_OPINION"}]},
            "_analysis": {"partial": True, "message": "STAFF_ONLY_PARTIAL"},
            "_measurement_excluded": [{"row": "STAFF_ONLY_EXCLUDED", "keyword": "Ethanol",
                "reason": "explicit_test_result", "location": {"line": 1, "start": 0, "end": 7}}]}
        m._save_ai_extraction(db, cid, "parse_file", doc.fields)
        db.commit()
    payload = ok(client.get(f"/cases/{cid}/{endpoint}", headers=roles[role]))
    encoded = json.dumps(payload)
    assert ("STAFF_ONLY_EXCLUDED" in encoded) is (role in m.STAFF_ROLES)
    assert ("measurement_excluded" in encoded) is (role in m.STAFF_ROLES)
    assert ("STAFF_ONLY_OPINION" in encoded) is (role in m.STAFF_ROLES)
    assert ("STAFF_ONLY_PARTIAL" in encoded) is (role in m.STAFF_ROLES)


@pytest.mark.parametrize('role', ['applicant', 'auditor', 'consultant', 'operator', 'admin', 'fatwa_liaison'])
def test_authorized_roles(setup, role):
    client, cid, roles, ids = setup
    result = ok(client.get(f'/cases/{cid}/intake-view', headers=roles[role]))
    assert result['case_id'] == cid
    with m.SessionLocal() as db:
        log = db.query(models.AuditLog).filter_by(case_id=cid, action='intake.viewed').one()
        assert log.actor_role == role and log.resource_id == cid


@pytest.mark.parametrize('role', ['applicant', 'auditor', 'consultant'])
def test_unassigned_org_parity(setup, role):
    client, cid, roles, _ = setup
    outsider, _ = staff(role, 'org_other')
    expected = client.get(f'/cases/{cid}', headers=outsider).status_code
    assert expected in (403, 404)
    assert client.get(f'/cases/{cid}/intake-view', headers=outsider).status_code == expected
    with m.SessionLocal() as db:
        assert not db.query(models.AuditLog).filter_by(case_id=cid, action='intake.viewed').count()


def test_same_org_unassigned_auditor_and_missing_case(setup):
    client, cid, roles, _ = setup
    with m.SessionLocal() as db:
        org = db.get(models.CaseApplication, cid).org_id
    same_org, _ = staff('auditor', org)
    assert client.get(f'/cases/{cid}/intake-view', headers=same_org).status_code == client.get(f'/cases/{cid}', headers=same_org).status_code == 200
    assert client.get('/cases/not-found/intake-view', headers=roles['admin']).status_code == 404
    assert client.get(f'/cases/{cid}/intake-view').status_code == 401


def test_complete_intake_from_registration_and_writes(setup):
    client, cid, roles, ids = setup
    app = roles['applicant']
    ok(client.patch(f'/cases/{cid}/profile', headers=app, json={'email': 'case@example.com', 'halal_supervisor': 'Supervisor', 'profile_ext': {'establishment_date': '2020-01-02', 'corporate_reg_no': 'CORP-11', 'business_type': 'Food', 'custom_extension': {'note': 'Actual extension'}}}))
    fac = ok(client.post(f'/cases/{cid}/factories', headers=app, json={'name': 'Intake Plant', 'address': 'Busan factory street', 'reg_no': 'FACT-22', 'city': 'Busan', 'country': 'KR', 'zip': '12345', 'phone': '051-123-4567', 'pic_name': 'Plant Contact', 'profile_ext': {'cp_email': 'plant2@example.com', 'cp_title': 'Plant director'}}))
    product = ok(client.post(f'/cases/{cid}/products', headers=app, json={'name': 'Tea product', 'category': 'Beverages', 'description': 'Tea description'}))
    material = ok(client.post(f'/cases/{cid}/materials', headers=app, json={'name': 'Sugar', 'supplier': 'Supplier A', 'manufacturer': 'Maker B', 'origin': 'Indonesia', 'note': 'Batch note', 'declared_cert_no': 'DECLARED-22', 'mat_type': 'raw'}))
    did = seed_document(client, cid, app)
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        db.add(models.ProductMaterial(case_id=cid, product_id=product['product_id'], material_id=material['material_id']))
        db.add(models.MaterialEvidenceLink(case_id=cid, document_id=did, material_id=material['material_id'], link_type='DIRECT', match_basis='manual', link_reason='Reference'))
        db.commit()
    ok(client.post(f'/cases/{cid}/submit-application', headers=app))
    pending = ok(client.get(f'/cases/{cid}/pending-extractions', headers=app))
    result = ok(client.get(f'/cases/{cid}/intake-view', headers=roles['auditor']))
    assert result['company']['email'] == 'case@example.com'
    assert result['company']['company_email'] == 'company@example.com'
    assert result['company']['profile_ext']['company_name_en'] == 'Intake Foods Ltd.'
    assert result['company']['profile_ext']['corporate_reg_no'] == 'CORP-11'
    assert result['company']['profile_ext']['custom_extension']['note'] == 'Actual extension'
    assert result['contacts']['cp'] == {'name': 'Contact Two', 'title': 'Director', 'phone': '+821033334444', 'email': 'cp@example.com'}
    assert result['contacts']['pic']['email'] == 'pic@example.com'
    assert result['consent']['notify_consent'] is True
    assert set(result['consent']['email_consent']) >= {'agreed', 'at', 'version', 'channel'}
    assert result['facilities'][0]['address'] == 'Busan factory street'
    assert result['facilities'][0]['profile_ext']['cp_email'] == 'plant2@example.com'
    assert result['facilities'][0]['in_frozen_snapshot'] is False
    assert result['products'][0]['material_count'] == 1
    assert result['products'][0]['description'] == 'Tea description'
    mat = result['materials'][0]
    assert mat['origin'] == 'Indonesia' and mat['manufacturer'] == 'Maker B'
    assert mat['declared_cert_no'] == 'DECLARED-22' and mat['cert_no'] is None
    assert mat['evidence'][0]['document_id'] == did and mat['evidence'][0]['review_status'] == 'pending'
    doc = result['documents'][0]
    assert doc['sources']['address'] == 'llm' and doc['sources']['company_name'] == 'structural'
    assert doc['uploaded_at'] and doc['uploader_role'] == 'applicant'
    assert doc['verify']['verdict'] == 'warn' and doc['truncated'] is True
    assert doc['problem_reasons']
    assert result['pending']['items'] == pending['items']
    assert result['pending']['conflicts'] == pending['conflicts']
    assert result['summary']['pending_count'] == result['summary']['conflict_count'] == 1
    assert result['application']['submitted_at'] and result['application']['draft_state'] == 'completed'
    assert any(h['action'] == 'material.add' for h in result['history'])
    auto = next(h for h in result['history'] if h['action'] == 'autofill.applied')
    assert auto['at'] is None and auto['actor_id'] is None  # Not recorded; do not invent.
    assert auto['changes'] == [{'field': 'address', 'before': 'Old office', 'after': 'New office'}]
    assert 'secret-marker' not in json.dumps(result)


@pytest.mark.parametrize('payload', [{'renames': {'Pending material': 'Renamed material'}}, {'dismiss': ['Pending material']}, {'values': ['Pending material']}])
def test_reviewer_reason_and_applicant_protection(setup, payload):
    client, cid, roles, ids = setup
    seed_document(client, cid, roles['applicant'])
    endpoint = f'/cases/{cid}/pending-extractions/apply'
    if 'values' not in payload:
        r = client.post(endpoint, headers=roles['applicant'], json={**payload, 'reason': 'Applicant reason'})
        assert r.status_code == 403 and r.json()['detail']['code'] == 'REVIEWER_REQUIRED'
    r = client.post(endpoint, headers=roles['auditor'], json=payload)
    assert r.status_code == 422 and r.json()['detail']['code'] == 'REASON_REQUIRED'
    ok(client.post(endpoint, headers=roles['auditor'], json={**payload, 'reason': 'Compared with original'}))
    result = ok(client.get(f'/cases/{cid}/intake-view', headers=roles['auditor']))
    assert result['pending']['count'] == 0
    reviews = [h for h in result['history'] if h['action'] == 'extraction.review']
    assert reviews[0]['details']['reason'] == 'Compared with original'
    assert reviews[0]['actor_id'] == ids['auditor'] and reviews[0]['at']


def test_applicant_unchanged_apply_and_discussion(setup):
    client, cid, roles, _ = setup
    seed_document(client, cid, roles['applicant'])
    ok(client.post(f'/cases/{cid}/pending-extractions/apply', headers=roles['applicant'], json={'values': ['Pending material']}))
    ok(client.post(f'/cases/{cid}/discussions', headers=roles['applicant'], json={'kind': 'comment', 'text': 'Please review the extracted name'}))
    assert ok(client.get(f'/cases/{cid}/discussions', headers=roles['auditor']))[0]['text'] == 'Please review the extracted name'


def test_factory_create_patch_and_freeze(setup):
    client, cid, roles, _ = setup
    body = {'name': 'Factory', 'address': 'Full address', 'reg_no': 'REG', 'city': 'City', 'country': 'KR', 'zip': '11111', 'pic_name': 'PIC', 'pic_phone': '010', 'cp_name': 'CP', 'phone': '123', 'profile_ext': {'cp_email': 'cp@factory.example', 'custom': 'stored'}}
    f = ok(client.post(f'/cases/{cid}/factories', headers=roles['applicant'], json=body))
    fid = f['facility_id']
    assert all(f[k] == body[k] for k in ('name','address','reg_no','city','country','zip'))
    assert f['profile_ext']['pic_phone'] == '010' and f['profile_ext']['custom'] == 'stored'
    ok(client.patch('/facilities/'+fid, headers=roles['applicant'], json={'address': 'Edited', 'profile_ext': {'custom': 'edited'}}))
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        m.sm.freeze_scope(db, c, 'auditor', 'fixture')
        db.commit()
    assert client.post(f'/cases/{cid}/factories', headers=roles['applicant'], json=body).status_code == 409
    assert client.patch('/facilities/'+fid, headers=roles['applicant'], json={'address': 'Lost'}).status_code == 409
    result = ok(client.get(f'/cases/{cid}/intake-view', headers=roles['auditor']))
    f = result['facilities'][0]
    assert f['scope_frozen'] and f['in_frozen_snapshot']
    assert f['address'] == f['frozen_snapshot']['address'] == 'Edited'


def test_formal_application_and_recursive_secret_filter(setup):
    client, cid, roles, _ = setup
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        c.status = 'consultant_review'
        c.profile_ext = {**c.profile_ext, 'password': 'secret-marker', 'nested': [{'api_key': 'secret-marker', 'visible': 'kept'}], '_internal': 'secret-marker', 'pending_handled': ['secret-marker']}
        m.sm.record_event(db, c, c.status, c.status, 'eligibility.verdict', 'auditor', 'fixture', {'verdict': 'eligible'})
        db.commit()
    ok(client.post(f'/cases/{cid}/formal-application', headers=roles['applicant'], json={
        'process_summary': 'Mix and pack', 'halal_management': 'Trace materials', 'preferred_audit_period': '2027-01',
        'signer_name': 'Representative', 'non_halal_handling': False, 'sales_channels': 'Retail',
        'pledge': True, 'signature': 'data:image/png;base64,test-signature'}))
    result = ok(client.get(f'/cases/{cid}/intake-view', headers=roles['consultant']))
    formal = result['application']['formal_application']
    assert formal['signed'] is True and formal['pledge'] is True
    assert formal['fields']['process_summary'] == 'Mix and pack'
    assert 'secret-marker' not in json.dumps(result) and 'test-signature' not in json.dumps(result)
    def inspect_keys(value):
        if isinstance(value, dict):
            for k, v in value.items():
                assert not k.startswith('_') and not any(x in k.lower() for x in ('password', 'token', 'hash', 'api_key'))
                inspect_keys(v)
        elif isinstance(value, list):
            for v in value: inspect_keys(v)
    inspect_keys(result)


def test_read_only_and_constant_query_count(setup, monkeypatch):
    client, cid, roles, _ = setup
    seed_document(client, cid, roles['applicant'])
    def forbidden(*args, **kwargs):
        raise AssertionError('Read view attempted extraction or data mutation')
    monkeypatch.setattr(m, '_apply_agg_to_case', forbidden)
    monkeypatch.setattr(m, '_verify_doc', forbidden)
    monkeypatch.setattr(intake.ai_local, 'llm_json', forbidden)
    def read():
        queries = []
        def capture(conn, cursor, statement, parameters, context, executemany):
            queries.append(statement)
        event.listen(engine, 'before_cursor_execute', capture)
        try: result = ok(client.get(f'/cases/{cid}/intake-view', headers=roles['auditor']))
        finally: event.remove(engine, 'before_cursor_execute', capture)
        writes = [s for s in queries if s.lstrip().upper().startswith(('INSERT','UPDATE','DELETE'))]
        assert all('audit_log' in s or 'chain' in s for s in writes), writes
        return sum(s.lstrip().upper().startswith('SELECT') for s in queries), result
    small, _ = read()
    with m.SessionLocal() as db:
        for i in range(30):
            db.add(models.Product(case_id=cid, name=f'Product {i}'))
            db.add(models.Material(case_id=cid, name=f'Material {i}'))
            db.add(models.DocumentAsset(case_id=cid, filename=f'File {i}.txt', fields={'company_name': str(i)}, text_full='Text', confidence=.9))
        db.commit()
    large, result = read()
    assert large <= small + 1, (small, large)
    assert len(result['documents']) == 31 and len(result['products']) == 30 and len(result['materials']) == 30


@pytest.mark.parametrize('state,before,after', [('applied','Original','Extracted'), ('confirmed','Extracted','Chosen'), ('reverted','Extracted','Original')])
def test_autofill_history_direction_without_invented_time(setup, state, before, after):
    client, cid, roles, ids = setup
    did = seed_document(client, cid, roles['applicant'])
    with m.SessionLocal() as db:
        doc = db.get(models.DocumentAsset, did)
        change = {'field':'address', 'scope':'case', 'before':'Original', 'value':'Extracted', 'status':state}
        if state == 'confirmed':
            change.update(selected='Chosen', confirmed_by=ids['auditor'])
        doc.fields = {**doc.fields, '_autofill':{'changes':{'address':change}}}
        db.commit()
    result = ok(client.get(f'/cases/{cid}/intake-view', headers=roles['auditor']))
    row = next(h for h in result['history'] if h['action'] == 'autofill.'+state)
    assert row['changes'] == [{'field':'address','before':before,'after':after}]
    assert row['at'] is None
    if state == 'confirmed': assert row['actor_role'] == 'auditor'
