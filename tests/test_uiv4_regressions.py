"""uiv4: submit actual browser-form payloads to the API and verify persisted identity."""
import base64
import hashlib
import json
from pathlib import Path
import subprocess

import pytest
from fastapi.testclient import TestClient
from app import main, intake, models
from app.db import SessionLocal

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def payloads():
    result = subprocess.run(['node', str(ROOT / 'tests/fixtures/uiv4_frontend.cjs')],
                            check=True, capture_output=True, text=True)
    return {(r['lang'], r['sector']): r['body'] for r in json.loads(result.stdout)}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, 'autoparse_enqueue', lambda *a, **k: False)
    with TestClient(main.app) as c:
        yield c


def login(client, username, password='pw'):
    r = client.post('/auth/login', json={'username': username, 'password': password})
    assert r.status_code == 200, r.text
    return {'Authorization': 'Bearer ' + r.json()['token']}


@pytest.mark.parametrize('lang', ['ko', 'en', 'id'])
@pytest.mark.parametrize('sector', intake.SECTORS)
def test_translated_signup_form_persists_sector(client, payloads, lang, sector):
    payload = payloads[lang, sector]
    assert payload['sectors'] == [sector]
    response = client.post('/auth/register', json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    headers = {'Authorization': 'Bearer ' + result['token']}
    listing = client.get('/cases', headers=headers)
    assert listing.status_code == 200
    case = next(x for x in listing.json()['items'] if x['company_name'] == payload['company_name'])
    assert case['sector'] == sector
    with SessionLocal() as db:
        org = db.get(models.Org, result['org_id'])
        saved = db.get(models.CaseApplication, case['case_id'])
        assert org.profile_ext['sectors'] == [sector]
        assert saved.profile_ext['sectors'] == [sector]
        assert saved.email == payload['company_email'] and saved.notify_consent is True


def test_same_filename_changed_bytes_persist_as_distinct_sha256_versions(client):
    headers = login(client, 'applicant1')
    r = client.post('/cases', headers=headers, json={'company_name': 'uiv4 upload'})
    assert r.status_code == 200, r.text
    cid = r.json()['case_id']
    ids = []
    for content in [b'AAAA', b'BBBB']:
        r = client.post(f'/cases/{cid}/documents', headers=headers,
                        json={'filename': 'uiv4.txt', 'file_b64': base64.b64encode(content).decode()})
        assert r.status_code == 200, r.text
        ids.append(r.json()['document_id'])
    assert ids[0] != ids[1]
    docs = client.get(f'/cases/{cid}/documents', headers=headers).json()
    assert {x['file_hash'] for x in docs} == {hashlib.sha256(x).hexdigest() for x in [b'AAAA', b'BBBB']}


@pytest.mark.parametrize('role,password,signup_status', [('operator1','pw',403),('admin','admin',200)])
def test_member_panels_have_distinct_permissions(client, role, password, signup_status):
    headers = login(client, role, password)
    assert client.get('/admin/users', headers=headers).status_code == 200
    signups = client.get('/admin/staff-signups?status=pending', headers=headers)
    assert signups.status_code == signup_status
    if signup_status == 200:
        assert signups.json()['items'] == []
    else:
        assert 'items' not in signups.json()
    actions = client.get('/rbac/actions', headers=headers)
    assert actions.status_code == 200
    assert bool(actions.json()['actions'].get('admin.user.manage')) == (role == 'admin')
