"""s1: audit2 security regressions, adapted to tests/conftest.py isolation.

Source: /private/tmp/claude-501/glhac-audit2-sec2/test_security_regression.py.
Freeze/autoparse controls belong to the separate freeze axis and are not copied.
Initial transitions follow the explicit state_machine policy; approvals stay denied.
"""
import os
import shutil
import sys

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))
os.environ.setdefault('GLHAC_DEV', '1')
os.environ.setdefault('GLHAC_NOTIFY_WORKER', '0')
os.environ.setdefault('GLHAC_LLM_PROVIDER', 'none')
import base64
from collections import Counter
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
import subprocess
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
from app import auth, intake, main as m, models, screening
from test_final_e2_rules import seed_reviewed_issue_prerequisites, seed_paid_receipt
from app.db import SessionLocal

@pytest.fixture(scope='module')
def client(fresh_db_per_module, tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        for key, value in {'GLHAC_RATE_LIMIT_PER_MIN': '0', 'GLHAC_AUTOPARSE': '0',
                           'GLHAC_DOC_VERIFY': '0', 'GLHAC_LLM_PROVIDER': 'none',
                           'GLHAC_NOTIFY_WORKER': '0', 'GLHAC_ZIP_LOOKUP': '0',
                           'GLHAC_UPLOAD_DIR': str(tmp_path_factory.mktemp('s1_uploads'))}.items():
            mp.setenv(key, value)
        m._RL_WRITE_HITS.clear()
        with TestClient(m.app) as c:
            yield c
        m._RL_WRITE_HITS.clear()


PW = 'Passw0rd!23'
MAX_BYTES = 20 * 1024 * 1024
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')
CSV = '원재료명,공급사\nSugar,Local supplier\nGelatin,Local supplier\n'.encode()


def b64(data):
    return base64.b64encode(data).decode()


def request(c, method, path, actor=None, **kwargs):
    if actor is not None:
        kwargs['headers'] = {'Authorization': 'Bearer ' + actor['token']}
    return c.request(method, path, **kwargs)


def expect(r, statuses):
    if isinstance(statuses, int):
        statuses = (statuses,)
    try:
        safe = r.json()
        if isinstance(safe, dict):
            safe = {k: v for k, v in safe.items() if k not in ('token', 'refresh_token')}
        text = json.dumps(safe, ensure_ascii=False)[:400]
    except ValueError:
        text = r.text[:400]
    assert r.status_code in statuses, f'expected {statuses}, got {r.status_code}: {text}'
    return r


def code(r):
    d = r.json().get('detail')
    return d.get('code') if isinstance(d, dict) else r.json().get('code')


def add(obj):
    with SessionLocal() as db:
        db.add(obj)
        db.commit()
        db.refresh(obj)
        db.expunge(obj)
        return obj


def get(cls, key):
    with SessionLocal() as db:
        obj = db.get(cls, key)
        if obj is not None:
            db.expunge(obj)
        return obj


def rows(cls, **filters):
    with SessionLocal() as db:
        return db.query(cls).filter_by(**filters).all()


def new_case(actor, **kw):
    defaults = dict(org_id=actor['org_id'], company_name='Local audit ' + uuid4().hex[:10], status='onboarding')
    defaults.update(kw)
    return add(models.CaseApplication(**defaults)).case_id


def invoice(cid, status='waiting_payment'):
    return add(models.Invoice(case_id=cid, service_type='pre_audit', amount=1_000_000,
                              ppn=110_000, total=1_110_000, status=status)).invoice_id


def register(c, company=None, **extra):
    body = {'username': 'sec2_' + uuid4().hex[:16], 'password': PW, 'company_email': 's1@example.invalid', 'email_consent': True,
            'company_name': company or 'Local audit ' + uuid4().hex[:10], **extra}
    r = expect(request(c, 'POST', '/auth/register', json=body), 200)
    actor = r.json()
    actor['uid'] = auth.verify_token(actor['token'])['uid']
    return actor


@pytest.fixture(scope='module')
def actors(client):
    out = {'a': register(client, 'Local Company A'), 'b': register(client, 'Local Company B')}
    for key, name, password in [('cons', 'consultant1', 'pw'), ('op', 'operator1', 'pw'),
                                 ('aud', 'auditor1', 'pw'), ('fatwa', 'fatwa1', 'pw'), ('admin', 'admin', 'admin')]:
        r = expect(request(client, 'POST', '/auth/login', json={'username': name, 'password': password}), 200)
        out[key] = r.json()
        out[key]['uid'] = auth.verify_token(r.json()['token'])['uid']
    return out


@pytest.fixture(scope='module')
def pdf_bytes():
    import fitz
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((50, 80), 'Nama Perusahaan: LOCAL AUDIT COMPANY\nNama Pimpinan: Local Tester\n'
                         'Alamat Perusahaan: Local test address\nNomor Induk Berusaha: 1234567890123\n'
                         'This is synthetic local regression evidence only.')
        return doc.tobytes()


@pytest.fixture(scope='module')
def materials_xlsx():
    # Same Indonesian table structure as tests/test_intake_cost.py, no LLM needed.
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Daftar Bahan Halal'
    ws.append(['Daftar Bahan Halal (Local audit)'])
    ws.append(['No', 'Nama', 'Jenis Bahan', 'Produsen', 'Negara', 'Supplier'])
    ws.append(['1', 'Sugar', 'BAHAN BAKU', 'Local Maker', 'INDONESIA', 'Local Supplier'])
    ws.append(['2', 'Gelatin', 'BAHAN BAKU', 'Local Maker', 'INDONESIA', 'Local Supplier'])
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


def upload(c, actor, cid, filename='evidence.png', raw=PNG, doc_type='other', endpoint='documents'):
    return request(c, 'POST', f'/cases/{cid}/{endpoint}', actor,
                   json={'filename': filename, 'file_b64': b64(raw), 'doc_type': doc_type})


def seed_pending(cid):
    return add(models.DocumentAsset(case_id=cid, filename='pending.txt', doc_type='material_list',
               fields={'material_names': ['Pending Sugar'], '_sources': {'material_names': 'llm'}},
               content_b64=b64(b'Local pending materials'), content_type='text/plain')).document_id


def material_names(cid):
    return sorted(x.name for x in rows(models.Material, case_id=cid))


# 1. October authorization regressions, with positive controls.
def test_org_same_company_registration_isolated(client):
    a = register(client, 'Identical local company')
    b = register(client, 'Identical local company', org_id=a['org_id'], role='admin')
    assert a['org_id'] != b['org_id']
    assert b['role'] == 'applicant'
    cid = rows(models.CaseApplication, org_id=a['org_id'])[0].case_id
    expect(request(client, 'GET', f'/cases/{cid}', a), 200)
    expect(request(client, 'GET', f'/cases/{cid}', b), (403, 404))


def test_org_self_profile_cannot_change_org(client, actors):
    a, b = actors['a'], actors['b']
    r = request(client, 'PATCH', '/me/profile', a, json={'org_id': b['org_id']})
    unchanged = get(models.User, a['uid']).org_id == a['org_id']
    expect(r, (403, 422))
    assert unchanged
    expect(request(client, 'PATCH', '/me/profile', a, json={'contact_email': 'audit@example.invalid'}), 200)


def test_case_cannot_move_to_other_org(client, actors):
    cid = new_case(actors['a'])
    r = request(client, 'PATCH', f'/cases/{cid}/profile', actors['a'], json={'org_id': actors['b']['org_id']})
    assert r.status_code in (200, 403, 422), r.text[:400]
    current_org = get(models.CaseApplication, cid).org_id
    assert current_org == actors['a']['org_id']
    expect(request(client, 'GET', f'/cases/{cid}', actors['b']), (403, 404))


def test_consultant_cannot_self_assign(client, actors):
    cid = new_case(actors['a'])
    expect(request(client, 'GET', f'/cases/{cid}', actors['cons']), (403, 404))
    r = request(client, 'POST', f'/cases/{cid}/assign-consultant', actors['cons'],
                json={'consultant_id': actors['cons']['uid']})
    expect(r, (403, 404))
    assert not rows(models.WorkflowEvent, case_id=cid, action=m.CONSULTANT_ASSIGN_ACTION)
    expect(request(client, 'POST', f'/cases/{cid}/assign-consultant', actors['op'],
                   json={'consultant_id': actors['cons']['uid']}), 200)
    expect(request(client, 'GET', f'/cases/{cid}', actors['cons']), 200)


def test_consultant_cannot_claim_by_company_name(client, actors):
    a = register(client, 'Unassigned ' + uuid4().hex[:8])
    cid = rows(models.CaseApplication, org_id=a['org_id'])[0].case_id
    r = expect(request(client, 'POST', '/cases', actors['cons'], json={'company_name': get(models.CaseApplication, cid).company_name}), 200)
    assert r.json()['org_id'] != a['org_id']
    assert get(models.Org, a['org_id']).consultant_id is None
    expect(request(client, 'GET', f'/cases/{cid}', actors['cons']), (403, 404))


@pytest.mark.parametrize('mode', ['pay', 'status', 'payment'])
def test_applicant_cannot_complete_payment(client, actors, mode):
    cid = new_case(actors['a'], status='consultant_review')
    iid = invoice(cid)
    if mode == 'pay':
        r = request(client, 'PATCH', f'/invoices/{iid}/pay', actors['a'])
    elif mode == 'status':
        r = request(client, 'PATCH', f'/invoices/{iid}/status', actors['a'], json={'status': 'paid'})
    else:
        r = request(client, 'POST', f'/invoices/{iid}/payment', actors['a'], json={'method': 'card', 'amount': 1_110_000})
    current = get(models.Invoice, iid).status
    assert get(models.CaseApplication, cid).status == 'consultant_review'
    if mode == 'payment':
        expect(r, 200)
        assert r.json()['status'] == 'pending'
        assert current == 'need_verification'
    else:
        expect(r, 403)
        assert current == 'waiting_payment'
        expect(request(client, 'PATCH', f'/invoices/{iid}/pay', actors['op']), 200)
        assert get(models.Invoice, iid).status == 'paid'


def test_applicant_cannot_override_contract_fee(client, actors):
    cid = new_case(actors['a'])
    r = request(client, 'POST', f'/cases/{cid}/contract/generate', actors['a'], json={'fee': 50_000_000})
    expect(r, 403)
    assert code(r) == 'FEE_OVERRIDE_FORBIDDEN'
    assert not rows(models.Contract, case_id=cid)
    expect(request(client, 'POST', f'/cases/{cid}/contract/generate', actors['op'], json={'fee': 1_000_000}), 200)
    assert rows(models.Contract, case_id=cid)[0].fee == 1_000_000


def test_applicant_cannot_sign_other_org(client, actors):
    cid = new_case(actors['a'])
    ct = add(models.Contract(case_id=cid, org_id=actors['a']['org_id'], status='sent', fee=1000))
    url = f'/contracts/{ct.contract_id}/sign'
    expect(request(client, 'POST', url, actors['b'], params={'party': 'A', 'name': 'Local outsider'}), (403, 404))
    assert not get(models.Contract, ct.contract_id).signatures
    expect(request(client, 'POST', url, actors['a'], params={'party': 'B', 'name': 'Local applicant'}), 403)
    expect(request(client, 'POST', url, actors['a'], params={'party': 'A', 'name': 'Local owner'}), 200)
    assert len(get(models.Contract, ct.contract_id).signatures) == 1


@pytest.mark.parametrize('initial,target', [
    ('onboarding', 'application_draft'),
    ('ai_pre_assessment_ready', 'ai_pre_assessment_running'),
    ('consultant_review', 'document_pre_audit_requested'),
    ('fatwa_review', 'fatwa_approved'),
    ('fatwa_approved', 'certificate_issued'),
])
def test_applicant_transition_policy(client, actors, initial, target):
    cid = new_case(actors['a'], status=initial)
    r = request(client, 'POST', f'/cases/{cid}/transition', actors['a'], json={'to_state': target})
    current = get(models.CaseApplication, cid).status
    allowed = initial in ('onboarding', 'ai_pre_assessment_ready')
    expect(r, 200 if allowed else 403)
    assert current == (target if allowed else initial)


@pytest.mark.parametrize('role', ['aud', 'admin'])
def test_major_nc_cannot_be_deleted(client, actors, role):
    cid = new_case(actors['a'])
    if role == 'aud':
        add(models.WorkflowEvent(case_id=cid, action='ops.auditor_assigned', actor_type='operator',
                                 actor_id=actors['op']['uid'], payload={'auditor_id': actors['aud']['uid']}))
    f = add(models.AuditFinding(case_id=cid, finding='Local Major NC', severity='major'))
    r = request(client, 'DELETE', f'/findings/{f.finding_id}', actors[role])
    expect(r, 409)
    assert code(r) == 'MAJOR_FINDING_DELETE_FORBIDDEN'
    assert get(models.AuditFinding, f.finding_id) is not None
    minor = add(models.AuditFinding(case_id=cid, finding='Local minor NC', severity='minor'))
    expect(request(client, 'DELETE', f'/findings/{minor.finding_id}', actors[role]), 200)


def test_arbitrary_material_cert_no_does_not_promote(client, actors):
    cid = new_case(actors['a'])
    r = expect(request(client, 'POST', f'/cases/{cid}/materials', actors['a'],
                       json={'name': 'Gelatin', 'cert_no': 'USER-CHOSEN-NOT-VERIFIED'}), 200)
    screen = r.json()['screen']
    assert screening._cert_verify_mode() == 'strict'
    assert not screen.get('cert_promoted')
    assert screen['status'] != 'halal'
    assert screen['result'] != 'CLEARED'


def test_user_cannot_persist_arbitrary_material_cert_no(client, actors):
    # Literal requested policy, stronger than October's "do not promote to halal" rule.
    cid = new_case(actors['a'])
    chosen = 'ARBITRARY-NOT-AN-ISSUED-CERTIFICATE'
    r = request(client, 'POST', f'/cases/{cid}/materials', actors['a'],
                json={'name': 'Gelatin', 'cert_no': chosen})
    stored = [x.cert_no for x in rows(models.Material, case_id=cid)]
    assert r.status_code in (200, 403, 422), r.text[:400]
    assert chosen not in stored, f'arbitrary cert_no persisted, HTTP {r.status_code}; screening trust is tested separately'


def test_issued_certificate_number_is_server_generated(client, actors):
    cid = new_case(actors['a'], status='fatwa_approved', fatwa_status='approved', scope_frozen=False, pathway='reguler')
    with SessionLocal() as db:
        case = db.get(models.CaseApplication, cid)
        product = models.Product(case_id=cid, name='Reviewed product')
        material = models.Material(case_id=cid, name='sugar')
        db.add_all([product, material])
        db.flush()
        db.add(models.ProductMaterial(case_id=cid, product_id=product.product_id,
                                      material_id=material.material_id))
        seed_reviewed_issue_prerequisites(db, case)
        m.sm.freeze_scope(db, case)
        db.commit()
    inv = invoice(cid, 'paid')
    with SessionLocal() as db:
        seed_paid_receipt(db, db.get(models.Invoice, inv))
        db.commit()
    chosen = 'USER-CHOSEN-CERTIFICATE'
    expect(request(client, 'POST', f'/cases/{cid}/certificate/issue', actors['a'],
                   json={'reason': 'local regression', 'cert_no': chosen}), 403)
    r = expect(request(client, 'POST', f'/cases/{cid}/certificate/issue', actors['op'],
                       json={'reason': 'local regression', 'cert_no': chosen, 'certificate_no': chosen}), 200)
    assert r.json()['status'] == 'pending'
    aid = r.json()['approval_id']
    expect(request(client, 'POST', f'/approvals/{aid}/approve', actors['fatwa'], json={'reason': 'local check'}), 200)
    cert = rows(models.HalalCertificate, case_id=cid)[0]
    assert cert.certificate_no != chosen
    assert cert.certificate_no == 'HC-' + cert.id[:8].upper()


def test_approvals_list_hides_other_org(client, actors):
    a, b = actors['a'], actors['b']
    cida, cidb = new_case(a), new_case(b)
    for cid, actor in ((cida, a), (cidb, b)):
        add(models.ApprovalRequest(case_id=cid, org_id=actor['org_id'], action_type='certificate.issue',
                                   requested_by=actors['op']['uid'], reason='local', payload={}))
    r = expect(request(client, 'GET', '/approvals', b), 200)
    visible = {x['case_id'] for x in r.json()}
    assert cidb in visible and cida not in visible
    r = expect(request(client, 'GET', '/approvals', actors['cons']), 200)
    assert cida not in {x['case_id'] for x in r.json()}
    r = expect(request(client, 'GET', '/approvals', actors['op']), 200)
    assert {cida, cidb} <= {x['case_id'] for x in r.json()}


def test_facilities_list_hides_other_org(client, actors):
    # A fresh org prevents the earlier valid consultant assignment from granting access.
    owner = register(client, 'Isolated facility company')
    f = add(models.Facility(org_id=owner['org_id'], name='Private facility'))
    path = f'/orgs/{owner["org_id"]}/facilities'
    expect(request(client, 'GET', path, actors['b']), (403, 404))
    expect(request(client, 'GET', path, actors['cons']), (403, 404))
    r = expect(request(client, 'GET', path, owner), 200)
    assert f.facility_id in {x['facility_id'] for x in r.json()}


# 2. HTML text context and actual inline-handler context (no hand-written escape substitute).
def node(js, value):
    p = subprocess.run([shutil.which('node') or '/opt/homebrew/bin/node', '-e', js], input=json.dumps(value), text=True,
                       capture_output=True, cwd=ROOT, timeout=10)
    assert p.returncode == 0, p.stderr[:1000]
    return json.loads(p.stdout)


@pytest.fixture(scope='module')
def ui_source(client):
    return expect(request(client, 'GET', '/ui/index.html'), 200).text


def escape_source(src):
    return next(x for x in src.splitlines() if x.startswith('function esc(s)'))


class HTMLProbe(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags, self.text, self.handlers, self.attributes = [], [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.append(attrs)
        self.handlers.extend(value for key, value in attrs if key == 'onclick')

    def handle_data(self, data):
        self.text.append(data)


def test_html_text_context_escapes_user_input(client, actors, ui_source):
    cid = new_case(actors['a'])
    payload = '<img src=x onerror="window.__audit_marker=1"> & \' "'
    expect(request(client, 'PATCH', f'/cases/{cid}/profile', actors['a'], json={'company_name': payload}), 200)
    r = expect(request(client, 'GET', f'/cases/{cid}', actors['a']), 200)
    stored = r.json()['company_name']
    js = "const fs=require('fs'),vm=require('vm');const x=JSON.parse(fs.readFileSync(0,'utf8'));const c={};vm.createContext(c);vm.runInContext(x.src,c);process.stdout.write(JSON.stringify(c.esc(x.value)));"
    rendered = node(js, {'src': escape_source(ui_source), 'value': stored})
    p = HTMLProbe()
    p.feed('<span>' + rendered + '</span>')
    assert p.tags == ['span']
    assert ''.join(p.text) == payload


@pytest.mark.parametrize('surface,payload', [
    ('mock_image', "a');window.__audit_marker=1;//.png"),
    ('document_button', 'a&quot;);window.__audit_marker=1;//.png'),
    ('id_only_control', 'a&quot;);window.__audit_marker=1;//.png'),
])
def test_stored_filename_cannot_inject_onclick(client, actors, ui_source, surface, payload):
    cid = new_case(actors['a'])
    if surface == 'document_button':
        mat = add(models.Material(case_id=cid, name='Sugar'))
        url = f'/cases/{cid}/materials/{mat.material_id}/evidence'
        expect(request(client, 'POST', url, actors['a'], json={
            'filename': payload, 'file_b64': b64(PNG), 'evidence_type': 'coa',
            'counts_as_evidence': False}), 200)
        ds = expect(request(client, 'GET', url, actors['op']), 200).json()
        assert len(ds) == 1
        d = ds[0]
    else:
        up = expect(upload(client, actors['a'], cid, payload, PNG, 'mock_evidence_material_storage'), 200)
        did = up.json()['document_id']
        if surface == 'mock_image':
            detail = expect(request(client, 'GET', f'/cases/{cid}/mock-audit/detail', actors['op']), 200).json()
            ds = [d for section in detail['sections'] for d in section['evidence']]
        else:
            ds = expect(request(client, 'GET', f'/cases/{cid}/documents', actors['op']), 200).json()
        d = next(x for x in ds if x['document_id'] == did)
    assert d['filename'] == payload and d['has_file'] is True
    if surface == 'id_only_control':
        line = next(x for x in ui_source.splitlines() if '${d.has_file?`<button' in x and 'onclick="docViewer' in x)
        fragment = re.search(r'onclick="(.+?)"(?=\s+[\w-]+=|>)', line).group(1)
        rendered = node(RENDER_JS, {'context': {'d': d}, 'src': escape_source(ui_source),
                                    'expression': '`' + fragment + '`'})
        probe = HTMLProbe()
        probe.feed('<button onclick="' + rendered + '">open</button>')
        result = node("const fs=require('fs'),vm=require('vm');const x=JSON.parse(fs.readFileSync(0,'utf8'));const c={window:{__audit_marker:0},docViewer:()=>{}};vm.createContext(c);vm.runInContext(x.handler,c);process.stdout.write(JSON.stringify(c.window.__audit_marker));", {'handler': probe.handlers[0]})
        assert result == 0
        return
    if surface == 'mock_image':
        line = next(x for x in ui_source.splitlines() if 'if(img)return' in x and 'data-mevimg=' in x)
    else:
        line = next(x for x in ui_source.splitlines() if '${d.has_file?`<button' in x and "safeClick('docViewer'" in x)
    expression = re.search(r"\$\{(safeClick\('docViewer'.+?\))\}", line).group(1)
    assert_safe_event(ui_source, expression, {'d': d}, 'docViewer', [d['document_id'], payload])


RENDER_JS = "const fs=require('fs'),vm=require('vm');const x=JSON.parse(fs.readFileSync(0,'utf8'));const c=x.context;vm.createContext(c);vm.runInContext(x.src,c);process.stdout.write(JSON.stringify(vm.runInContext(x.expression,c)));"


def safe_event_source(src):
    start = src.index('function safeClick(')
    return src[start:src.index('/* end safe data events */', start)]


def assert_safe_event(src, expression, context, action, expected):
    helper = next(x for x in src.splitlines() if x.startswith('function safeClick('))
    rendered = node(RENDER_JS, {'context': context, 'src': escape_source(src) + '\n' + helper,
                                'expression': expression})
    probe = HTMLProbe()
    probe.feed('<button ' + rendered + '>open</button>')
    assert probe.tags == ['button'] and not probe.handlers
    attrs = dict(probe.attributes[0])
    assert set(attrs) == {'data-safe-click', 'data-safe-args'}
    assert json.loads(attrs['data-safe-args']) == expected
    js = """const fs=require('fs'),vm=require('vm');
const x=JSON.parse(fs.readFileSync(0,'utf8'));let listener,calls=[];
const c={window:{__audit_marker:0},document:{addEventListener:(type,fn,capture)=>{if(type!=='click'||!capture)throw Error('delegation');listener=fn;}}};
c[x.action]=(...args)=>calls.push(args);vm.createContext(c);vm.runInContext(x.src,c);
const target={dataset:{safeClick:x.attrs['data-safe-click'],safeArgs:x.attrs['data-safe-args']}};
listener({target:{closest:()=>target},preventDefault:()=>{},stopPropagation:()=>{}});
process.stdout.write(JSON.stringify({calls,marker:c.window.__audit_marker}));"""
    result = node(js, {'src': safe_event_source(src), 'action': action, 'attrs': attrs})
    assert result == {'calls': [expected], 'marker': 0}


# 3. Anonymous endpoint size/rate limits, measured with a functioning general limiter.
def test_register_rejects_oversized_body(client):
    # No documented register limit exists. This probe exceeds even the 20 MiB upload ceiling.
    username = 'large_' + uuid4().hex[:12]
    r = request(client, 'POST', '/auth/register', json={'username': username, 'password': PW, 'company_email': 's1@example.invalid', 'email_consent': True,
                'company_name': 'Local size probe', 'address': 'A' * (MAX_BYTES + 1)})
    users = rows(models.User, username=username)
    persisted = bool(users)
    expect(r, (413, 422))
    assert not persisted


def test_general_write_rate_limit_control(client, monkeypatch):
    monkeypatch.setenv('GLHAC_RATE_LIMIT_PER_MIN', '3')
    m._RL_WRITE_HITS.clear()
    statuses = [request(client, 'POST', '/cases', json={'company_name': 'Local rate control'}).status_code for _ in range(4)]
    assert statuses == [401, 401, 401, 429]
    m._RL_WRITE_HITS.clear()


@pytest.mark.parametrize('endpoint', ['register', 'ocr-extract'])
def test_anonymous_rate_limit_enforced(client, pdf_bytes, monkeypatch, endpoint):
    monkeypatch.setenv('GLHAC_RATE_LIMIT_PER_MIN', '120')
    m._RL_WRITE_HITS.clear()
    auth._USERNAME_CHECK_ATTEMPTS.clear()
    started = time.monotonic()
    statuses = []
    for i in range(125):
        if endpoint == 'register':
            body = {'username': 'rate_' + uuid4().hex[:16], 'password': PW, 'company_email': 's1@example.invalid', 'email_consent': True, 'company_name': 'Local rate probe'}
        else:
            body = {'image_b64': b64(pdf_bytes)}
        statuses.append(request(client, 'POST', '/auth/' + endpoint, json=body).status_code)
    elapsed = time.monotonic() - started
    assert elapsed < 60, 'request series exceeded one rate window; result inconclusive'
    assert statuses[0] == 200
    assert 429 in statuses, f'{endpoint}: 125 requests in {elapsed:.2f}s, statuses={dict(Counter(statuses))}'


@pytest.mark.parametrize('delta', [0, 1], ids=['at_limit', 'over_limit'])
def test_ocr_size_boundary(client, pdf_bytes, delta):
    raw = pdf_bytes + b' ' * (MAX_BYTES + delta - len(pdf_bytes))
    r = request(client, 'POST', '/auth/ocr-extract', json={'image_b64': b64(raw)})
    expect(r, 200 if delta == 0 else 413)
    if delta == 0:
        assert r.json()['ocr_available'] is True
        assert r.json()['fields']['nib'] == '1234567890123'
    else:
        assert code(r) == 'UPLOAD_TOO_LARGE'


# 4. Document isolation: authorized controls prove the routes and resources exist.
@pytest.mark.parametrize('operation', ['parse', 'upload', 'pending_rename', 'pending_dismiss', 'verify', 'file'])
def test_documents_cross_org_denied(client, actors, operation):
    cid = new_case(actors['a'])
    did = seed_pending(cid)
    before_fields = get(models.DocumentAsset, did).fields
    before_profile = get(models.CaseApplication, cid).profile_ext
    if operation in ('parse', 'upload'):
        endpoint = 'parse-file' if operation == 'parse' else 'documents'
        r = upload(client, actors['b'], cid, 'local.txt', b'Local document evidence', 'other', endpoint)
    elif operation.startswith('pending'):
        body = {'renames': {'Pending Sugar': 'Sugar'}} if operation == 'pending_rename' else {'dismiss': ['Pending Sugar']}
        body['reason'] = 'Checked pending material against the source document'
        pending = expect(request(client, 'GET', f'/cases/{cid}/pending-extractions', actors['a']), 200)
        assert 'Pending Sugar' in pending.text, pending.text
        r = request(client, 'POST', f'/cases/{cid}/pending-extractions/apply', actors['b'], json=body)
    elif operation == 'verify':
        r = request(client, 'POST', f'/cases/{cid}/documents/verify', actors['b'])
    else:
        r = request(client, 'GET', f'/documents/{did}/file', actors['b'])
    expect(r, (403, 404))
    assert len(rows(models.DocumentAsset, case_id=cid)) == 1
    assert get(models.DocumentAsset, did).fields == before_fields
    assert get(models.CaseApplication, cid).profile_ext == before_profile
    assert material_names(cid) == []
    if operation in ('parse', 'upload'):
        expect(upload(client, actors['a'], cid, 'local.txt', b'Local document evidence', 'other', endpoint), 200)
    elif operation.startswith('pending'):
        expect(request(client, 'POST', f'/ops/cases/{cid}/assign-auditor', actors['op'],
                       json={'auditor_id': actors['aud']['uid']}), 200)
        ok = expect(request(client, 'POST', f'/cases/{cid}/pending-extractions/apply', actors['aud'], json=body), 200)
        assert ok.json()['count'] == 1
        assert material_names(cid) == (['Sugar'] if operation == 'pending_rename' else [])
    elif operation == 'verify':
        # consultant passes the role gate, so this checks the org gate too.
        expect(request(client, 'POST', f'/cases/{cid}/documents/verify', actors['cons']), (403, 404))
        expect(request(client, 'POST', f'/cases/{cid}/assign-consultant', actors['op'],
                       json={'consultant_id': actors['cons']['uid']}), 200)
        ok = expect(request(client, 'POST', f'/cases/{cid}/documents/verify', actors['cons']), 200)
        assert ok.json()['found'] == 1
    else:
        ok = expect(request(client, 'GET', f'/documents/{did}/file', actors['a']), 200)
        assert ok.content == b'Local pending materials'


@pytest.mark.parametrize('endpoint', ['documents', 'parse-file'])
@pytest.mark.parametrize('filename', ['../outside.txt', '..\\outside.txt', '/tmp/glhac-audit-path-probe.txt'])
def test_upload_filename_cannot_escape_storage(client, actors, endpoint, filename):
    cid = new_case(actors['a'])
    before = {str(p): p.stat().st_size for p in Path(os.environ['GLHAC_UPLOAD_DIR']).rglob('*') if p.is_file()}
    r = upload(client, actors['a'], cid, filename, b'Local traversal marker', 'other', endpoint)
    # Rejecting a path name or retaining it solely as DB metadata are both safe.
    assert r.status_code in (200, 400, 415, 422), r.text[:400]
    after = {str(p): p.stat().st_size for p in Path(os.environ['GLHAC_UPLOAD_DIR']).rglob('*') if p.is_file()}
    assert before == after
    assert not (ROOT / 'outside.txt').exists()
    if r.status_code == 200:
        docs = rows(models.DocumentAsset, case_id=cid)
        assert len(docs) == 1
        assert base64.b64decode(docs[0].content_b64) == b'Local traversal marker'
        download = expect(request(client, 'GET', f'/documents/{docs[0].document_id}/file', actors['a']), 200)
        assert download.content == b'Local traversal marker'


@pytest.mark.parametrize('endpoint', ['documents', 'parse-file'])
def test_upload_known_magic_mismatch_rejected(client, actors, endpoint):
    cid = new_case(actors['a'])
    r = upload(client, actors['a'], cid, 'disguised.pdf', PNG, 'other', endpoint)
    expect(r, 415)
    assert code(r) == 'CONTENT_MISMATCH'
    assert not rows(models.DocumentAsset, case_id=cid)


@pytest.mark.parametrize('endpoint', ['documents', 'parse-file'])
@pytest.mark.parametrize('filename', ['not_pdf.pdf', 'not_docx.docx'])
def test_upload_unknown_content_mismatch_rejected(client, actors, endpoint, filename):
    cid = new_case(actors['a'])
    r = upload(client, actors['a'], cid, filename, b'This plain text is not the claimed file format.', 'other', endpoint)
    expect(r, 415)
    assert not rows(models.DocumentAsset, case_id=cid)


@pytest.mark.parametrize('endpoint', ['documents', 'parse-file'])
def test_upload_extension_allowlist_enforced(client, actors, endpoint):
    cid = new_case(actors['a'])
    expect(upload(client, actors['a'], cid, 'unsupported.exe', b'Local inert test text', 'other', endpoint), 415)
    assert not rows(models.DocumentAsset, case_id=cid)


@pytest.mark.parametrize('endpoint', ['documents', 'parse-file'])
@pytest.mark.parametrize('delta', [0, 1], ids=['at_limit', 'over_limit'])
def test_upload_size_boundary(client, actors, pdf_bytes, endpoint, delta):
    cid = new_case(actors['a'])
    raw = pdf_bytes + b' ' * (MAX_BYTES + delta - len(pdf_bytes))
    r = upload(client, actors['a'], cid, 'boundary.pdf', raw, 'other', endpoint)
    expect(r, 200 if delta == 0 else 413)
    if delta:
        assert code(r) == 'UPLOAD_TOO_LARGE'
        assert not rows(models.DocumentAsset, case_id=cid)
    else:
        assert len(rows(models.DocumentAsset, case_id=cid)) == 1


# 5. Custom two-segment HMAC tokens: no assumption that these are standard JWTs.
@pytest.mark.parametrize('variant', ['signature', 'alg_none', 'expired', 'role_tamper', 'org_tamper', 'refresh_as_access'])
def test_invalid_tokens_rejected(client, actors, variant):
    a = actors['a']
    raw, sig = a['token'].split('.')
    payload = dict(auth.verify_token(a['token']))
    expect(request(client, 'GET', '/auth/me', a), 200)
    if variant == 'signature':
        token = raw + '.' + ('0' if sig[0] != '0' else '1') + sig[1:]
    elif variant == 'alg_none':
        head = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').decode().rstrip('=')
        token = head + '.' + raw + '.'
    elif variant == 'expired':
        # Valid test-secret signature, expiry alone must cause rejection.
        token = auth.make_token(get(models.User, a['uid']), ttl=-60)
    elif variant in ('role_tamper', 'org_tamper'):
        payload['role' if variant == 'role_tamper' else 'org_id'] = 'admin' if variant == 'role_tamper' else actors['b']['org_id']
        forged = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
        token = forged + '.' + sig
    else:
        token = a['refresh_token']
    r = request(client, 'GET', '/auth/me', {'token': token})
    expect(r, 401)
    assert code(r) == ('REFRESH_NOT_ALLOWED' if variant == 'refresh_as_access' else 'INVALID_TOKEN')


def test_logout_revokes_old_access_and_refresh(client):
    a = register(client)
    before = get(models.User, a['uid']).token_version or 0
    expect(request(client, 'GET', '/auth/me', a), 200)
    r = expect(request(client, 'POST', '/auth/refresh', json={'refresh_token': a['refresh_token']}), 200)
    refreshed = {'token': r.json()['token']}
    expect(request(client, 'POST', '/auth/logout', a), 200)
    after = get(models.User, a['uid']).token_version
    assert after == before + 1
    for old in (a, refreshed):
        r = expect(request(client, 'GET', '/auth/me', old), 401)
        assert code(r) == 'TOKEN_REVOKED'
    r = expect(request(client, 'POST', '/auth/refresh', json={'refresh_token': a['refresh_token']}), 401)
    assert code(r) == 'TOKEN_REVOKED'
    fresh = expect(request(client, 'POST', '/auth/login', json={'username': a['username'], 'password': PW}), 200).json()
    expect(request(client, 'GET', '/auth/me', fresh), 200)


# Added boundary/bypass controls: inspect stored state, not only response status.
@pytest.mark.parametrize('field,limit', [
    ('username', 80), ('password', 256), ('company_name', 200), ('company_name_en', 200),
    ('company_email', 200), ('consultant_id', 64), ('invite_code', 64), ('nib', 40),
    ('responsible_person', 100), ('address', 2000), ('factory_address', 2000), ('business_type', 100),
])
def test_register_string_limits(client, field, limit):
    body = {'username': 'bound_' + uuid4().hex[:12], 'password': PW,
            'company_email': 's1@example.invalid', 'email_consent': True}
    body[field] = 'A' * limit + 'b'
    expect(client.post('/auth/register', json=body), 422)
    assert not rows(models.User, username=body['username'])


@pytest.mark.parametrize('extra', [
    {'sectors': ['food'] * 6}, {'sectors': ['f' * 65]},
    {'contacts': {'pic': {'name': 'a' * 101}}},
    {'contacts': {'cp': {'title': 'a' * 101}}},
])
def test_register_nested_limits(client, extra):
    body = {'username': 'nested_' + uuid4().hex[:12], 'password': PW,
            'company_email': 's1@example.invalid', 'email_consent': True, **extra}
    expect(client.post('/auth/register', json=body), 422)
    assert not rows(models.User, username=body['username'])


@pytest.mark.parametrize('endpoint', ['register', 'ocr-extract'])
@pytest.mark.parametrize('length_header', [None, '1'])
def test_public_body_bytes_even_without_truthful_length(client, monkeypatch, endpoint, length_header):
    limit = 4096
    monkeypatch.setattr(m, '_PUBLIC_BODY_MAX_BYTES', limit)
    monkeypatch.setattr(m, '_PUBLIC_UPLOAD_BODY_MAX_BYTES', limit)
    # Multibyte ignored metadata bypasses individual field validation, but not actual byte limits.
    body = json.dumps({'unrecognized': '한' * (limit // 2)}, ensure_ascii=False).encode()
    headers = {'Content-Type': 'application/json'}
    if length_header is not None:
        headers['Content-Length'] = length_header
    r = client.post('/auth/' + endpoint, content=iter([body[:100], body[100:]]), headers=headers)
    expect(r, 413)
    assert code(r) == 'REQUEST_TOO_LARGE'


@pytest.mark.parametrize('delta', [0, 1])
def test_register_body_exact_byte_boundary(client, delta):
    body = {'username': 'exact_' + uuid4().hex[:12], 'password': PW,
            'company_email': 's1@example.invalid', 'email_consent': True}
    raw = json.dumps(body).encode()
    raw += b' ' * (64 * 1024 + delta - len(raw))
    expect(client.post('/auth/register', content=raw, headers={'Content-Type': 'application/json'}),
           413 if delta else 200)
    assert bool(rows(models.User, username=body['username'])) == (delta == 0)


def test_chunked_body_stops_receiving_at_limit(monkeypatch):
    import asyncio
    monkeypatch.setattr(m, '_PUBLIC_BODY_MAX_BYTES', 4)
    chunks = iter([b'123', b'45'])
    sent = []
    async def receive():
        return {'type': 'http.request', 'body': next(chunks), 'more_body': True}
    async def send(message):
        sent.append(message)
    async def downstream(*args):
        pytest.fail('oversized request reached JSON parser/endpoint')
    asyncio.run(m._PublicBodyLimitMiddleware(downstream)(
        {'type': 'http', 'method': 'POST', 'path': '/auth/register/', 'headers': []}, receive, send))
    assert sent[0]['status'] == 413


def test_public_ip_budget_shared_cannot_rotate_tokens_or_forwarded_headers(client, actors, monkeypatch):
    monkeypatch.setenv('GLHAC_RATE_LIMIT_PER_MIN', '100')
    monkeypatch.setenv('GLHAC_PUBLIC_RATE_LIMIT_PER_MIN', '2')
    m._RL_WRITE_HITS.clear()
    statuses = []
    for i, actor in enumerate([actors['a'], actors['b'], actors['admin']]):
        path = '/auth/register' if i % 2 == 0 else '/auth/ocr-extract'
        statuses.append(client.post(path, json={}, headers={
            'Authorization': 'Bearer ' + actor['token'],
            'X-Forwarded-For': f'198.51.100.{i}'}).status_code)
    assert statuses == [422, 422, 429]
    expect(client.post('/auth/login', json={'username': 'operator1', 'password': 'pw'}), 200)
    monkeypatch.setenv('GLHAC_RATE_LIMIT_PER_MIN', '0')
    expect(client.post('/auth/register', json={}), 422)  # master switch overrides public limit
    m._RL_WRITE_HITS.clear()


def test_public_rate_window_expires_and_ips_are_separate(client, monkeypatch):
    monkeypatch.setenv('GLHAC_RATE_LIMIT_PER_MIN', '1')
    monkeypatch.delenv('GLHAC_PUBLIC_RATE_LIMIT_PER_MIN', raising=False)
    m._RL_WRITE_HITS.clear()
    expect(client.post('/auth/register', json={}), 422)
    expect(client.post('/auth/register', json={}), 429)
    # A distinct ASGI peer gets its own budget; XFF is intentionally not a peer.
    with TestClient(m.app, client=('198.51.100.8', 50100)) as other:
        expect(other.post('/auth/register', json={}), 422)
    m._RL_WRITE_HITS['public:ip:testclient'] = [time.time() - 61]
    expect(client.post('/auth/register', json={}), 422)
    m._RL_WRITE_HITS.clear()


@pytest.mark.parametrize('ext', ['pdf', 'png', 'jpg', 'jpeg', 'gif', 'webp', 'docx', 'xlsx',
                                'doc', 'xls', 'hwp', 'mp4', 'mov', 'm4v', 'webm'])
def test_all_binary_extensions_reject_unidentified_bytes(client, actors, ext):
    cid = new_case(actors['a'])
    expect(upload(client, actors['a'], cid, 'plain.' + ext, b'plain text'), 415)
    assert not rows(models.DocumentAsset, case_id=cid)


@pytest.mark.parametrize('filename,raw', [
    ('bad.pdf', b'%PDFx-not-a-header'), ('bad.png', b'\x89PNG-not-the-magic'),
    ('bad.gif', b'GIF8xx'), ('bad.webp', b'RIFF\x00\x00\x00\x00WAVEaudio'),
    ('bad.docx', b'PK\x03\x04fake zip'), ('bad.mp4', b'\x00\x00\x00\x00ftypisom0000'),
])
def test_incomplete_or_ambiguous_signatures_rejected(client, actors, filename, raw):
    cid = new_case(actors['a'])
    expect(upload(client, actors['a'], cid, filename, raw), 415)
    assert not rows(models.DocumentAsset, case_id=cid)


@pytest.mark.parametrize('ext', ['docx', 'xlsx'])
@pytest.mark.parametrize('damage', ['missing_types', 'wrong_type', 'bad_main_xml', 'missing_main'])
def test_office_structure_checked_before_storage(client, actors, ext, damage):
    import zipfile
    part, ctype = ('word/document.xml', 'wordprocessingml.document') if ext == 'docx' else ('xl/workbook.xml', 'spreadsheetml.sheet')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as archive:
        if damage != 'missing_types':
            declared = 'application/wrong' if damage == 'wrong_type' else 'application/vnd.openxmlformats-officedocument.' + ctype + '.main+xml'
            archive.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/' + part + '" ContentType="' + declared + '"/></Types>')
        if damage != 'missing_main':
            archive.writestr(part, '<unclosed>' if damage == 'bad_main_xml' else '<document/>')
    cid = new_case(actors['a'])
    expect(upload(client, actors['a'], cid, 'file.' + ext, buf.getvalue()), 415)
    assert not rows(models.DocumentAsset, case_id=cid)


@pytest.mark.parametrize('ext', ['docx', 'xlsx', 'pdf', 'png'])
def test_valid_binary_documents_still_upload(client, actors, pdf_bytes, materials_xlsx, ext):
    import docx
    buf = io.BytesIO()
    doc = docx.Document()
    doc.add_paragraph('Local signature regression')
    doc.save(buf)
    raw = {'docx': buf.getvalue(), 'xlsx': materials_xlsx, 'pdf': pdf_bytes, 'png': PNG}[ext]
    cid = new_case(actors['a'])
    expect(upload(client, actors['a'], cid, 'valid.' + ext, raw), 200)
    assert len(rows(models.DocumentAsset, case_id=cid)) == 1


@pytest.mark.parametrize('mode', ['strict', 'off'])
@pytest.mark.parametrize('input_field', ['cert_no', 'declared_cert_no'])
def test_unverified_numbers_are_durable_declarations_on_add_and_patch(client, actors, monkeypatch, mode, input_field):
    monkeypatch.setenv('GLHAC_CERT_VERIFY', mode)
    cid = new_case(actors['a'])
    response = expect(request(client, 'POST', f'/cases/{cid}/materials', actors['a'],
                              json={'name': 'Gelatin', input_field: 'USER-UNKNOWN-123456'}), 200).json()
    mid = response['material_id']
    assert not response['screen']['cert_promoted'] and response['cert_no'] is None
    assert get(models.Material, mid).cert_no is None
    row = expect(request(client, 'GET', f'/cases/{cid}/materials', actors['a']), 200).json()[0]
    assert row['declared_cert_no'] == 'USER-UNKNOWN-123456'
    r = expect(request(client, 'PATCH', f'/materials/{mid}', actors['a'],
                       json={input_field: 'SECOND-UNKNOWN-654321'}), 200).json()
    assert not r['screen']['cert_promoted'] and r['cert_no'] is None
    assert r['declared_cert_no'] == 'SECOND-UNKNOWN-654321'
    assert get(models.Material, mid).cert_no is None
    events = rows(models.WorkflowEvent, case_id=cid, action='material.certificate')
    assert len(events) == 2 and events[-1].payload['declared_cert_no'] == 'SECOND-UNKNOWN-654321'
    cleared = expect(request(client, 'PATCH', f'/materials/{mid}', actors['a'], json={input_field: ''}), 200).json()
    assert cleared['cert_no'] is None and cleared['declared_cert_no'] is None


@pytest.mark.parametrize('state', ['approved', 'pending', 'rejected', 'expired', 'other_org', 'other_material'])
def test_certificate_reference_requires_review_scope_and_validity(client, actors, state):
    cid = new_case(actors['a'])
    mid = expect(request(client, 'POST', f'/cases/{cid}/materials', actors['a'], json={'name': 'Gelatin'}), 200).json()['material_id']
    number = 'ID00410000500391022'
    doc_cid = new_case(actors['b']) if state == 'other_org' else cid
    doc_mid = add(models.Material(case_id=doc_cid, name='Different material')).material_id if state == 'other_material' else None
    document = add(models.DocumentAsset(case_id=doc_cid, material_id=doc_mid, filename='cert.txt',
        doc_type='halal_certificate', review_status=state if state in ('pending', 'rejected') else 'approved',
        content_b64=b64(('Halal Certificate\nCertificate No: ' + number).encode()),
        fields={'cert_no': number, 'expiry_date': '2000-01-01' if state == 'expired' else '2099-01-01'}))
    result = expect(request(client, 'PATCH', f'/materials/{mid}', actors['a'], json={'cert_no': number}), 200).json()
    verified = state == 'approved'
    assert result['cert_no'] == (number if verified else None)
    assert result['declared_cert_no'] == (None if verified else number)
    assert bool(result['screen']['cert_promoted']) == verified
    event = rows(models.WorkflowEvent, case_id=cid, action='material.certificate')[-1]
    assert event.payload['document_id'] == (document.document_id if verified else None)


@pytest.mark.parametrize('payload', [
    "a');window.__audit_marker=1;//", 'a&quot;);window.__audit_marker=1;//',
    '<img src=x onerror="window.__audit_marker=1">', '\\"\'&\n\u2028\u2029',
])
@pytest.mark.parametrize('action', ['matToggle', 'renameMaterial', 'matEvidenceList', 'editMaterial',
                                    'applyDocCompany', 'editProductDesc', 'facDel', 'askQ',
                                    'approveStaff', 'rejectStaff', 'resetUserPw'])
def test_user_data_event_arguments_roundtrip_without_execution(ui_source, action, payload):
    expression = re.search(r"\$\{(safeClick\('" + action + r"'.+?\))\}(?=\s|>)", ui_source).group(1)
    context = {'id': 'case-id', 'pid': 'product-id', 'org': 'org-id', 'caseId': 'case-id', 'CURCASE': 'case-id',
               'm': {'material_id': 'mat-id', 'name': payload, 'manufacturer': payload, 'mat_type_code': '', 'cert_no': payload, 'q': payload},
               'd': {'description': payload}, 'f': {'facility_id': 'fac-id', 'name': payload},
               'coChk': {'suggested_company': payload}, 'c': {'company_name': payload},
               'r': {'request_id': 'req-id', 'username': payload}, 'u': {'user_id': 'uid', 'username': payload}}
    expected = {
        'matToggle': ['mat-id', payload], 'renameMaterial': ['mat-id', payload],
        'matEvidenceList': ['case-id', 'mat-id', payload],
        'editMaterial': ['mat-id', {'manufacturer': payload, 'mat_type_code': '', 'cert_no': payload}],
        'applyDocCompany': ['case-id', payload, payload], 'editProductDesc': ['case-id', 'product-id', payload],
        'facDel': ['fac-id', payload, 'org-id', 'case-id'], 'askQ': ['case-id', payload],
        'approveStaff': ['req-id', payload], 'rejectStaff': ['req-id', payload], 'resetUserPw': ['uid', payload],
    }[action]
    assert_safe_event(ui_source, expression, context, action, expected)


@pytest.mark.parametrize('endpoint', ['intake-zip', 'intake-zip-stream'])
@pytest.mark.parametrize('extension', ['pdf', 'docx', 'xlsx', 'png'])
def test_archive_cannot_bypass_binary_signature_guard(client, actors, endpoint, extension):
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as archive:
        archive.writestr('first.txt', 'Legitimate text must not be partially saved on rejection.')
        archive.writestr('disguised.' + extension, 'Plain text in a binary document extension.')
    cid = new_case(actors['a'])
    result = request(client, 'POST', f'/cases/{cid}/{endpoint}', actors['a'],
                     json={'zip_b64': b64(buf.getvalue())})
    expect(result, 415)
    assert code(result) == 'CONTENT_MISMATCH'
    assert not rows(models.DocumentAsset, case_id=cid)


@pytest.mark.parametrize('endpoint', ['intake-zip', 'intake-zip-stream'])
def test_valid_archive_still_uploads(client, actors, pdf_bytes, endpoint):
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as archive:
        archive.writestr('document.pdf', pdf_bytes)
        archive.writestr('readme.txt', 'Local valid package.')
    cid = new_case(actors['a'])
    result = expect(request(client, 'POST', f'/cases/{cid}/{endpoint}', actors['a'],
                            json={'zip_b64': b64(buf.getvalue())}), 200)
    if endpoint.endswith('stream'):
        events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith('data: ')]
        assert events[-1]['type'] == 'result'
    assert len(rows(models.DocumentAsset, case_id=cid)) == 2
