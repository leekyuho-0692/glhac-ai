"""R7 f3: C17-F2, N4, N5/N6 and P1 security regressions.

Reuses final2-sec/test_final2_security.py attack bytes and TestClient scenarios.
Fixtures are local; authentication, guards, parsers and transactions are real.
The report directories are read-only inputs, never runtime dependencies.
"""
import base64
import copy
import io
import json
import os
from pathlib import Path
import struct
import sys
import zipfile
import zlib
from datetime import date, timedelta
from uuid import uuid4

import pytest
from PIL import Image
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("GLHAC_LLM_PROVIDER", "none")
os.environ.setdefault("GLHAC_NOTIFY_WORKER", "0")
from app import ai_local, auth, cert_verify, main as m, models
from app.db import SessionLocal

PW = "Passw0rd!23"
NUMBER = "ID00410000500391022"
TERMINAL = ["withdrawn", "rejected", "revoked", "cancelled"]

@pytest.fixture(scope="module")
def client(fresh_db_per_module):
    with pytest.MonkeyPatch.context() as mp:
        for key in ("GLHAC_RATE_LIMIT_PER_MIN", "GLHAC_AUTOPARSE", "GLHAC_DOC_VERIFY",
                    "GLHAC_NOTIFY_WORKER", "GLHAC_ZIP_LOOKUP"):
            mp.setenv(key, "0")
        mp.setenv("GLHAC_CERT_VERIFY", "strict")
        mp.setattr(m, "_AUTOPARSE", False)
        mp.setattr(m, "_DOC_VERIFY", False)
        mp.setattr(ai_local, "llm_json", lambda *a, **kw: {"error": "LLM_UNAVAILABLE"})
        mp.setattr(ai_local, "llm_text", lambda *a, **kw: "")
        mp.setattr(ai_local, "ocr_available", lambda: False)
        mp.setattr(ai_local, "ocr_image", lambda *a, **kw: {"ok": False, "error": "LOCAL_OCR_DISABLED", "lines": []})
        mp.setattr(ai_local, "ocr_text_multi", lambda *a, **kw: {"ok": False, "error": "LOCAL_OCR_DISABLED", "text": ""})
        m._RL_WRITE_HITS.clear()
        with TestClient(m.app) as client:
            yield client
        m._RL_WRITE_HITS.clear()


def evidence(kind, **values):
    # pytest captures sanitized evidence only on assertion failure.
    print(kind, json.dumps(values, default=str, ensure_ascii=False))


def b64(data):
    return base64.b64encode(data).decode()


def request(c, method, path, actor=None, **kwargs):
    if actor is not None:
        kwargs['headers'] = {'Authorization': 'Bearer ' + actor['token']}
    r = c.request(method, path, **kwargs)
    info = {'method': method, 'path': path, 'status': r.status_code}
    if r.headers.get('content-type', '').startswith('application/json'):
        body = r.json()
        if isinstance(body, dict):
            detail = body.get('detail')
            info['code'] = detail.get('code') if isinstance(detail, dict) else body.get('code')
            for key in ('status', 'invoice_status', 'count', 'found', 'queued', 'applied', 'to', 'from', 'revoked', 'autoparse_queued'):
                if key in body:
                    info[key] = body[key]
    evidence('http', **info)
    return r


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
    body = {'username': 'sec2_' + uuid4().hex[:16], 'password': PW, 'company_email': 'local@example.invalid', 'email_consent': True,
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


def user_for(role, org, template):
    u = add(models.User(username='final_' + uuid4().hex[:14], role=role, org_id=org,
                        password_hash=get(models.User, template['uid']).password_hash))
    return {**auth.make_tokens(u), 'uid': u.user_id, 'org_id': org, 'role': role}


@pytest.fixture
def world(client, actors):
    owner = register(client)
    cid = new_case(owner, address='Existing address', email='company@example.invalid', notify_consent=True)
    out = {'owner': owner, 'outsider': actors['b'], 'consultant': actors['cons'],
           'auditor': actors['aud'], 'operator': actors['op'], 'fatwa': actors['fatwa'],
           'admin': actors['admin'], 'anonymous': None}
    for key, role, org in [('unassigned_consultant', 'consultant', actors['b']['org_id']),
            ('unassigned_auditor', 'auditor', actors['b']['org_id']),
            ('supervisor', 'penyelia_halal', owner['org_id']),
            ('foreign_supervisor', 'penyelia_halal', actors['b']['org_id']),
            ('companion', 'pendamping_pph', actors['b']['org_id']),
            ('unassigned_companion', 'pendamping_pph', actors['b']['org_id'])]:
        out[key] = user_for(role, org, owner)
    for action, field, actor in [(m.CONSULTANT_ASSIGN_ACTION, 'consultant_id', out['consultant']),
                                ('ops.auditor_assigned', 'auditor_id', out['auditor'])]:
        with SessionLocal() as db:
            c = db.get(models.CaseApplication, cid)
            m.sm.record_event(db, c, c.status, c.status, action, 'operator', out['operator']['uid'], {field: actor['uid']})
            db.commit()
    add(models.PendampingAssignment(case_id=cid, pendamping_id=out['companion']['uid']))
    return cid, out


def problem(cid, fields=None):
    return add(models.DocumentAsset(case_id=cid, filename='private-problem.txt', doc_type='other',
        confidence=0.9, fields=fields if fields is not None else {'_ocr_error': 'CORRUPT'},
        text_full='Local synthetic problem document.', content_b64=b64(b'local evidence'))).document_id


def conflict(cid, field='address', scope='case', current='Existing address', proposed='Candidate address', **extra):
    rec = {'field': field, 'scope': scope, 'current': current, 'proposed': proposed,
           'reason': 'value_conflict', 'status': 'pending', **extra}
    d = add(models.DocumentAsset(case_id=cid, filename='candidate.txt', doc_type='nib_business_license',
        confidence=0.9, text_full='Local candidate evidence', content_b64=b64(b'Local candidate evidence'),
        fields={'_autofill': {'conflicts': [rec]}}))
    return d.document_id, {'conflict_id': d.document_id + ':0', 'value': proposed}


def change(cls, key, **values):
    with SessionLocal() as db:
        obj = db.get(cls, key)
        for k, v in values.items():
            setattr(obj, k, v)
        db.commit()


def no_write_snapshot(cid):
    """Relevant domain state, excluding read-access logs and unrelated orgs."""
    classes = [models.CaseApplication, models.DocumentAsset, models.Material, models.Product,
               models.WorkflowEvent, models.Notification, models.AuditPlan, models.MaterialMeasurement,
               models.Invoice, models.Contract, models.AuditFinding, models.ApprovalRequest]
    state = {}
    with SessionLocal() as db:
        for cls in classes:
            objects = db.query(cls).filter_by(case_id=cid).all()
            state[cls.__tablename__] = sorted([
                {col.name: getattr(x, col.name) for col in cls.__table__.columns} for x in objects
            ], key=lambda x: json.dumps(x, sort_keys=True, default=str))
    return json.dumps(state, sort_keys=True, default=str)


def upload(c, actor, cid, filename, raw, doc_type='other', endpoint='documents'):
    return request(c, 'POST', f'/cases/{cid}/{endpoint}', actor,
                   json={'filename': filename, 'file_b64': b64(raw), 'doc_type': doc_type})


def prepare_issue_evidence(cid, invoice_id, auditor, checker):
    with SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        inv = db.get(models.Invoice, invoice_id)
        inv.currency = inv.currency or 'KRW'
        db.add(models.Payment(case_id=cid, invoice_id=invoice_id, amount=inv.total,
                              currency=inv.currency, status='confirmed', method='local_fixture'))
        for el in m.HPAS_ELEMENTS:
            db.add(models.HpasEvaluation(case_id=cid, element=el, status='ok'))
        if not m.sm.has_active_penyelia(db, c.org_id):
            db.add(models.PenyeliaHalal(org_id=c.org_id, name='Local reviewed supervisor', status='active'))
        report = models.GeneratedDocument(case_id=cid, org_id=c.org_id, doc_type='audit_report',
            version=1, status='approved', content='Local reviewed scope for certificate number control')
        db.add(report)
        db.flush()
        # 현재 HEAD의 생성·서명·심의 이력은 같은 심사 범위를 참조해야 한다.
        subject = m.sm.review_subject(db, c)
        m.sm.record_event(db, c, c.status, c.status, 'audit_report.generated', 'auditor', auditor,
                          {**m._report_version(report), 'review_subject': subject})
        m.sm.record_event(db, c, c.status, c.status, 'audit_report.sign', 'auditor', auditor,
                          {'name': 'Local reviewer', **m._report_version(report), 'review_subject': subject})
        missing, context = m._fatwa_gate_check(db, c)
        assert not missing, missing
        ar = models.ApprovalRequest(case_id=cid, org_id=c.org_id, action_type='audit_report.send_fatwa',
            requested_by=auditor, decided_by=checker, status='approved',
            payload={'review_basis': context['review_basis']})
        db.add(ar)
        db.flush()
        m.sm.record_event(db, c, c.status, 'fatwa_review', 'audit_report.send_fatwa', 'auditor', auditor,
            {'approval_id': ar.id, 'review_basis': context['review_basis'], 'gate': 'passed'})
        m.sm.record_event(db, c, c.status, c.status, 'fatwa.provisional', 'fatwa_liaison', None,
                          {'decision': 'approved', 'review_basis': context['review_basis']})
        m.sm.record_event(db, c, c.status, c.status, 'fatwa.final_approve', 'operator', checker,
                          {'review_basis': context['review_basis']})
        db.commit()


def png_with_bad_pixels():
    def chunk(tag, value):
        return struct.pack('>I', len(value)) + tag + value + struct.pack('>I', zlib.crc32(tag + value))
    return (b'\x89PNG\r\n\x1a\n'
        + chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 2, 8, 2, 0, 0, 0))
        + chunk(b'IDAT', b'This is not a zlib pixel stream') + chunk(b'IEND', b''))


def office_without_relationships(ext):
    if ext == 'docx':
        part, typ = 'word/document.xml', 'wordprocessingml.document'
        xml = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p/></w:body></w:document>'
    else:
        part, typ = 'xl/workbook.xml', 'spreadsheetml.sheet'
        xml = '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Missing" sheetId="1" r:id="rId1"/></sheets></workbook>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/' + part + '" ContentType="application/vnd.openxmlformats-officedocument.' + typ + '.main+xml"/></Types>')
        z.writestr(part, xml)
    return buf.getvalue()


@pytest.mark.parametrize('ext', ['png', 'docx', 'xlsx', 'doc', 'xls', 'hwp'])
@pytest.mark.parametrize('endpoint', ['documents', 'parse-file'])
def test_deeper_corrupt_structure_is_rejected(client, world, ext, endpoint):
    cid, a = world
    if ext == 'png':
        raw = png_with_bad_pixels()
        with pytest.raises(OSError):
            with Image.open(io.BytesIO(raw)) as im:
                im.load()
    elif ext in ('docx', 'xlsx'):
        raw = office_without_relationships(ext)
        if ext == 'docx':
            from docx import Document
            with pytest.raises(KeyError):
                Document(io.BytesIO(raw))
        else:
            import openpyxl
            with pytest.raises(KeyError):
                openpyxl.load_workbook(io.BytesIO(raw))
    else:
        raw = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'Not an OLE compound document'
    r = upload(client, a['owner'], cid, 'broken.' + ext, raw, 'other', endpoint)
    stored = rows(models.DocumentAsset, case_id=cid)
    evidence('deeper_structure', extension=ext, endpoint=endpoint, status=r.status_code,
             bytes=len(raw), stored=len(stored), fields=stored[0].fields if stored else None)
    expect(r, 415)
    assert not stored


@pytest.mark.parametrize('endpoint', ['documents', 'parse-file', 'intake-zip', 'intake-zip-stream', 'material_evidence', 'ocr-extract'])
@pytest.mark.parametrize('valid', [True, False])
def test_invalid_pixels_rejected_at_each_ingress(client, world, endpoint, valid):
    cid, a = world
    raw = png_with_bad_pixels()
    if valid:
        buf = io.BytesIO()
        Image.new('RGB', (2, 2), 'white').save(buf, format='PNG')
        raw = buf.getvalue()
        with Image.open(io.BytesIO(raw)) as im:
            im.load()
    if endpoint.startswith('intake-zip'):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr('first.txt', b'Valid local text')
            z.writestr('broken.png', raw)
        r = request(client, 'POST', f'/cases/{cid}/{endpoint}', a['owner'], json={'zip_b64': b64(buf.getvalue())})
    elif endpoint == 'material_evidence':
        mat = add(models.Material(case_id=cid, name='Sugar'))
        r = request(client, 'POST', f'/cases/{cid}/materials/{mat.material_id}/evidence', a['owner'],
            json={'filename': 'broken.png', 'file_b64': b64(raw), 'evidence_type': 'coa'})
    elif endpoint == 'ocr-extract':
        r = request(client, 'POST', '/auth/ocr-extract', json={'image_b64': b64(raw)})
    else:
        r = upload(client, a['owner'], cid, 'broken.png', raw, 'other', endpoint)
    evidence('deeper_ingress', endpoint=endpoint, valid=valid, status=r.status_code,
             stored=len(rows(models.DocumentAsset, case_id=cid)))
    expect(r, 200 if valid else 415)
    if not valid:
        assert not rows(models.DocumentAsset, case_id=cid)


@pytest.mark.parametrize('state', ['onboarding'] + TERMINAL)
def test_sjph_get_does_not_create_domain_records_after_close(client, world, state):
    cid, a = world
    if state in ('withdrawn', 'rejected', 'cancelled'):
        expect(request(client, 'POST', f'/cases/{cid}/close', a['operator'],
            json={'status': state, 'reason': 'Local read-side write probe'}), 200)
    elif state == 'revoked':
        change(models.CaseApplication, cid, status=state)
    assert not rows(models.HpasEvaluation, case_id=cid)
    expect(request(client, 'GET', f'/cases/{cid}/sjph', a['outsider']), (403, 404))
    assert not rows(models.HpasEvaluation, case_id=cid)
    r = request(client, 'GET', f'/cases/{cid}/sjph', a['owner'])
    after = rows(models.HpasEvaluation, case_id=cid)
    evidence('closed_get_write', state=state, status=r.status_code,
             inserted=len(after), statuses=[x.status for x in after])
    expect(r, 200)
    assert len(after) == 0
    assert len(r.json()['elements']) == 5


@pytest.mark.parametrize('operation', ['notify', 'choice', 'material'])
def test_replaced_consultant_cannot_continue_mutating(client, world, operation):
    """Historical read access is allowed; current edits must follow reassignment."""
    cid, a = world
    new = a['unassigned_consultant']
    with SessionLocal() as db:
        assert db.get(models.Org, a['owner']['org_id']).consultant_id != a['consultant']['uid']
    problem(cid)
    _, choice = conflict(cid)
    expect(request(client, 'POST', f'/cases/{cid}/assign-consultant', a['operator'],
        json={'consultant_id': new['uid'], 'reason': 'Replace previous consultant'}), 200)
    expect(request(client, 'GET', f'/cases/{cid}/problem-documents', new), 200)
    # The product explicitly retains historical read access; that alone is not a finding.
    expect(request(client, 'GET', f'/cases/{cid}/problem-documents', a['consultant']), 200)
    if operation == 'notify':
        path, body = f'/cases/{cid}/problem-documents/notify', {}
    elif operation == 'choice':
        path, body = f'/cases/{cid}/pending-extractions/apply', {'autofill_choices': [choice]}
    else:
        path, body = f'/cases/{cid}/materials', {'name': 'After replacement', 'declared_cert_no': 'UNVERIFIED'}
    before = no_write_snapshot(cid)
    r = request(client, 'POST', path, a['consultant'], json=body)
    evidence('former_staff_write', role='consultant', operation=operation,
             status=r.status_code, changed=no_write_snapshot(cid) != before,
             former=a['consultant']['uid'], current=new['uid'])
    expect(r, (403, 404))
    assert no_write_snapshot(cid) == before


@pytest.mark.parametrize('damage', ['valid', 'missing_expiry', 'malformed_expiry', 'filename_draft', 'fulltext_draft', 'counts_false'])
@pytest.mark.parametrize('mode', ['strict', 'off'])
def test_certificate_reference_preserves_validity_context(client, world, monkeypatch, damage, mode):
    """Existing reviewed document rows must retain negative evidence at the API gate."""
    cid, a = world
    monkeypatch.setenv('GLHAC_CERT_VERIFY', mode)
    mid = add(models.Material(case_id=cid, name='Gelatin')).material_id
    number = 'ID00410000500391022'
    fields = {'cert_no': number, 'expiry_date': '2099-01-01'}
    if damage == 'missing_expiry':
        fields.pop('expiry_date')
    elif damage == 'malformed_expiry':
        fields['expiry_date'] = 'not-a-date'
    elif damage == 'counts_false':
        fields['_counts_as_evidence'] = False
    text = 'HALAL CERTIFICATE\nCertificate No: ' + number
    fulltext = text + ('\nDRAFT - NOT VALID' if damage == 'fulltext_draft' else '')
    did = add(models.DocumentAsset(case_id=cid, material_id=mid,
        filename='DRAFT-certificate.txt' if damage == 'filename_draft' else 'certificate.txt',
        doc_type='halal_certificate', review_status='pending', fields=fields,
        text_excerpt=text, text_full=fulltext,
        content_b64=b64(fulltext.encode()))).document_id
    expect(request(client, 'PATCH', f'/documents/{did}/review', a['auditor'], json={'review_status': 'approved'}), 200)
    r = expect(request(client, 'PATCH', f'/materials/{mid}', a['owner'], json={'cert_no': number}), 200)
    body = r.json()
    # Review again after the material identity now includes the selected number.
    # The clean control then has valid review provenance; malformed evidence must
    # still fail the downstream issuance gate even when its UI result is CLEARED.
    expect(request(client, 'PATCH', f'/documents/{did}/review', a['auditor'], json={'review_status': 'approved'}), 200)
    product = add(models.Product(case_id=cid, name='Local reviewed product'))
    add(models.ProductMaterial(case_id=cid, product_id=product.product_id, material_id=mid))
    change(models.CaseApplication, cid, status='fatwa_approved', fatwa_status='approved', pathway='reguler')
    prepare_issue_evidence(cid, invoice(cid, 'paid'), a['auditor']['uid'], a['operator']['uid'])
    with SessionLocal() as db:
        m.sm.freeze_scope(db, db.get(models.CaseApplication, cid))
        db.commit()
    issued = request(client, 'POST', f'/cases/{cid}/certificate/issue', a['operator'], json={'reason': 'Local guard control'})
    expect(issued, 200 if damage == 'valid' else 409)
    with SessionLocal() as db:
        issue_blockers = m.sm.material_safety_blockers(db, db.get(models.CaseApplication, cid))
    evidence('certificate_context', damage=damage, cert_no=body['cert_no'],
        promoted=body['screen']['cert_promoted'], result=body['screen']['result'],
        issue_blockers=issue_blockers, issue_status=issued.status_code, issue_code=code(issued),
        filename=get(models.DocumentAsset, did).filename)
    assert bool(body['screen']['cert_promoted']) == (damage == 'valid')
    assert body['cert_no'] == (number if damage == 'valid' else None)


@pytest.mark.parametrize('review', ['pending', 'rejected', 'rework'])
@pytest.mark.parametrize('linked', [True, False])
@pytest.mark.parametrize('mode', ['strict', 'off'])
def test_certificate_review_downgrade_invalidates_cached_promotion(client, world, monkeypatch, review, linked, mode):
    cid, a = world
    monkeypatch.setenv('GLHAC_CERT_VERIFY', mode)
    mid = add(models.Material(case_id=cid, name='Gelatin')).material_id
    number = 'ID00410000500391022'
    did = add(models.DocumentAsset(case_id=cid, material_id=mid if linked else None,
        filename='certificate.txt', doc_type='halal_certificate', review_status='approved',
        fields={'cert_no': number, 'expiry_date': '2099-01-01'},
        text_full='HALAL CERTIFICATE\nCertificate No: ' + number,
        text_excerpt='HALAL CERTIFICATE\nCertificate No: ' + number,
        content_b64=b64(('HALAL CERTIFICATE\nCertificate No: ' + number).encode()))).document_id
    r = expect(request(client, 'PATCH', f'/materials/{mid}', a['owner'], json={'cert_no': number}), 200)
    assert r.json()['screen']['cert_promoted']
    expect(request(client, 'PATCH', f'/documents/{did}/review', a['auditor'], json={'review_status': review}), 200)
    material = next(x for x in expect(request(client, 'GET', f'/cases/{cid}/materials', a['owner']), 200).json() if x['material_id'] == mid)
    with SessionLocal() as db:
        issue_blockers = m.sm.material_safety_blockers(db, db.get(models.CaseApplication, cid))
    evidence('certificate_downgrade', review=review, linked=linked,
        cert_no=material['cert_no'], result=material['result'], status=material['status'], issue_blockers=issue_blockers)
    assert material['result'] not in ('CLEARED', 'PASS') and material['status'] != 'halal'

    assert material['cert_no'] is None
    assert material['declared_cert_no'] == number
    assert get(models.Material, mid).cert_no is None


@pytest.mark.parametrize('ext', ['docx', 'xlsx'])
@pytest.mark.parametrize('damage', ['valid', 'root_relation', 'missing_target', 'bad_xml'])
def test_office_relationships_and_body_are_readable(client, world, ext, damage):
    cid, actors = world
    buf = io.BytesIO()
    if ext == 'docx':
        from docx import Document
        doc = Document()
        doc.add_paragraph('Local valid document')
        doc.save(buf)
        target = 'word/styles.xml'
    else:
        from openpyxl import Workbook
        book = Workbook()
        book.active.append(['Valid', 1])
        book.save(buf)
        book.close()
        target = 'xl/worksheets/sheet1.xml'
    raw = buf.getvalue()
    if damage != 'valid':
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw)) as original, zipfile.ZipFile(buf, 'w') as altered:
            for info in original.infolist():
                if info.filename == ('_rels/.rels' if damage == 'root_relation' else target if damage == 'missing_target' else ''):
                    continue
                altered.writestr(info.filename, b'<broken>' if damage == 'bad_xml' and info.filename == target else original.read(info))
        raw = buf.getvalue()
    expect(upload(client, actors['owner'], cid, 'sample.' + ext, raw), 200 if damage == 'valid' else 415)
    assert len(rows(models.DocumentAsset, case_id=cid)) == (1 if damage == 'valid' else 0)


def compound_document(ext):
    """Deterministic CFB fixture: real sector chains and named typed streams."""
    free, end, fat = 0xffffffff, 0xfffffffe, 0xfffffffd
    if ext == 'doc':
        word = bytearray(4096)
        struct.pack_into('<HH', word, 0, 0xa5ec, 0xc1)
        streams = [('WordDocument', bytes(word)), ('0Table', b'table'.ljust(4096, b'\0'))]
    elif ext == 'xls':
        book = struct.pack('<HHHH', 0x809, 4, 0x600, 5) + struct.pack('<HH', 0xa, 0)
        streams = [('Workbook', book.ljust(4096, b'\0'))]
    else:
        header = bytearray(4096)
        header[:18] = b'HWP Document File\0'
        header[35] = 5
        streams = [('FileHeader', bytes(header)), ('DocInfo', b'info'.ljust(4096, b'\0')),
                   ('BodyText/Section0', b'text'.ljust(4096, b'\0'))]
    # Two directory sectors, one FAT sector, regular stream sectors.
    directory = bytearray(1024)
    table = [free] * 128
    table[0], table[1], table[2] = 1, end, fat
    def entry(index, name, kind, start=end, length=0, right=free, child=free):
        off = index * 128
        encoded = (name + '\0').encode('utf-16le')
        directory[off:off + len(encoded)] = encoded
        struct.pack_into('<HBBIII', directory, off + 64, len(encoded), kind, 1, free, right, child)
        struct.pack_into('<IQ', directory, off + 116, start, length)
    entry(0, 'Root Entry', 5, child=1)
    data = []
    start = 3
    for index, (name, payload) in enumerate(streams, 1):
        next_index = index + 1 if index < len(streams) else free
        if '/' in name:
            parent, name = name.split('/')
            entry(index, parent, 1, child=index + 1)
            index += 1
            next_index = free
        entry(index, name, 2, start, len(payload), right=next_index)
        sectors = len(payload) // 512
        for sid in range(start, start + sectors):
            table[sid] = sid + 1 if sid < start + sectors - 1 else end
        data.append(payload)
        start += sectors
    header = bytearray(512)
    header[:8] = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
    struct.pack_into('<HHHHH', header, 24, 0x3e, 3, 0xfffe, 9, 6)
    struct.pack_into('<IIIIIIIII', header, 40, 0, 1, 0, 0, 4096, end, 0, end, 0)
    struct.pack_into('<109I', header, 76, 2, *([free]*108))
    return bytes(header + directory) + struct.pack('<128I', *table) + b''.join(data)


@pytest.mark.parametrize('ext', ['doc', 'xls', 'hwp'])
@pytest.mark.parametrize('damage', ['valid', 'truncated', 'cyclic_fat', 'wrong_stream'])
def test_compound_container_chains_and_required_streams(client, world, ext, damage):
    cid, actors = world
    raw = compound_document(ext)
    if damage == 'truncated':
        raw = raw[:-512]
    elif damage == 'cyclic_fat':
        raw = bytearray(raw)
        struct.pack_into('<I', raw, 1536 + 3*4, 3)
        raw = bytes(raw)
    elif damage == 'wrong_stream':
        name = {'doc': 'WordDocument', 'xls': 'Workbook', 'hwp': 'FileHeader'}[ext]
        raw = raw.replace(name.encode('utf-16le'), ('X'*len(name)).encode('utf-16le'))
    expect(upload(client, actors['owner'], cid, 'sample.' + ext, raw), 200 if damage == 'valid' else 415)
    assert len(rows(models.DocumentAsset, case_id=cid)) == (1 if damage == 'valid' else 0)


@pytest.mark.parametrize('state', ['onboarding'] + TERMINAL)
def test_readiness_also_has_no_hpas_write(client, world, state):
    cid, actors = world
    change(models.CaseApplication, cid, status=state)
    before = no_write_snapshot(cid)
    expect(request(client, 'GET', f'/cases/{cid}/readiness', actors['owner']), 200)
    assert not rows(models.HpasEvaluation, case_id=cid)
    assert no_write_snapshot(cid) == before

def test_sjph_explicit_write_persists_defaults_and_rejects_closed_write(client, world):
    cid, actors = world
    expect(request(client, 'PATCH', f'/cases/{cid}/sjph', actors['consultant'],
                   json={'element': 'commitment', 'status': 'ok', 'note': 'Reviewer checked'}), 200)
    before = {r.element: (r.status, r.note) for r in rows(models.HpasEvaluation, case_id=cid)}
    assert len(before) == 5 and before['commitment'] == ('ok', 'Reviewer checked')
    expect(request(client, 'POST', f'/cases/{cid}/close', actors['operator'],
                   json={'status': 'withdrawn', 'reason': 'Local fixture closed'}), 200)
    expect(request(client, 'PATCH', f'/cases/{cid}/sjph', actors['consultant'],
                   json={'element': 'commitment', 'status': 'gap'}), 409)
    assert {r.element: (r.status, r.note) for r in rows(models.HpasEvaluation, case_id=cid)} == before


def promoted_material(client, world, name='Gelatin', doc_id=None):
    cid, actors = world
    mid = add(models.Material(case_id=cid, name=name)).material_id
    if doc_id is None:
        text = f'Halal Certificate\nCertificate No: {NUMBER}\nExpiry date: 2099-01-01'
        doc_id = add(models.DocumentAsset(case_id=cid, filename='shared-certificate.txt',
            doc_type='halal_certificate', review_status='pending', content_b64=b64(text.encode()),
            text_excerpt=text, text_full=text, fields={'cert_no': NUMBER, 'expiry_date': '2099-01-01'})).document_id
        expect(request(client, 'PATCH', f'/documents/{doc_id}/review', actors['auditor'],
                       json={'review_status': 'approved'}), 200)
    body = expect(request(client, 'PATCH', f'/materials/{mid}', actors['owner'], json={'cert_no': NUMBER}), 200).json()
    assert body['screen']['cert_promoted']
    return mid, doc_id


@pytest.mark.parametrize('cause', ['reprocess', 'replacement', 'reclassification', 'expiry', 'legacy_review'])
def test_every_reference_invalidated_for_document_changes(client, world, cause):
    cid, actors = world
    one, did = promoted_material(client, world)
    two, _ = promoted_material(client, world, name='Beef gelatin', doc_id=did)
    if cause == 'legacy_review':
        # Simulate references written by build 0635cd7, before explicit N:M links.
        with SessionLocal() as db:
            db.query(models.MaterialEvidenceLink).filter_by(case_id=cid).delete()
            db.commit()
        expect(request(client, 'PATCH', f'/documents/{did}/review', actors['auditor'], json={'review_status': 'rejected'}), 200)
    elif cause == 'reprocess':
        expect(request(client, 'POST', f'/documents/{did}/reprocess?apply=false', actors['consultant']), 200)
    elif cause == 'replacement':
        expect(upload(client, actors['owner'], cid, 'shared-certificate.txt', b'Replacement awaiting review', 'halal_certificate'), 200)
    elif cause == 'reclassification':
        expect(request(client, 'PATCH', f'/documents/{did}/reclassify', actors['consultant'], json={'doc_type': 'other'}), 200)
    else:
        change(models.DocumentAsset, did, fields={'cert_no': NUMBER, 'expiry_date': '2000-01-01'})
    before = no_write_snapshot(cid)
    material_rows = expect(request(client, 'GET', f'/cases/{cid}/materials', actors['owner']), 200).json()
    for mid in (one, two):
        row = next(x for x in material_rows if x['material_id'] == mid)
        assert row['cert_no'] is None and row['declared_cert_no'] == NUMBER
        assert row['result'] not in ('PASS', 'CLEARED') and row['status'] != 'halal'
        assert get(models.Material, mid).cert_no is None
    assert no_write_snapshot(cid) == before
    chain = expect(request(client, 'GET', f'/cases/{cid}/audit-verify', actors['operator']), 200).json()
    assert chain['integrity_ok'] and chain['anchor_ok'], chain


def test_certificate_reference_has_explicit_relationship(client, world):
    cid, actors = world
    mid, did = promoted_material(client, world)
    links = rows(models.MaterialEvidenceLink, case_id=cid, material_id=mid, document_id=did)
    assert len(links) == 1 and links[0].match_basis == 'certificate_number'
    # Updating another field must not duplicate the reference.
    expect(request(client, 'PATCH', f'/materials/{mid}', actors['owner'], json={'supplier': 'Current supplier'}), 200)
    assert len(rows(models.MaterialEvidenceLink, case_id=cid, material_id=mid, document_id=did)) == 1


@pytest.mark.parametrize('state', ['onboarding', 'withdrawn'])
def test_clock_expiry_projects_current_trust_without_get_write(client, world, monkeypatch, state):
    cid, actors = world
    mid, did = promoted_material(client, world)
    change(models.CaseApplication, cid, status=state)
    before = no_write_snapshot(cid)
    class FutureDate(date):
        @classmethod
        def today(cls):
            return cls(2100, 1, 1)
    monkeypatch.setattr(cert_verify, 'date', FutureDate)
    body = expect(request(client, 'GET', f'/cases/{cid}/materials', actors['owner']), 200).json()
    row = next(x for x in body if x['material_id'] == mid)
    assert row['cert_no'] is None and row['declared_cert_no'] == NUMBER
    assert row['result'] not in ('CLEARED', 'PASS') and row['status'] != 'halal'
    assert no_write_snapshot(cid) == before


@pytest.mark.parametrize('status', ['APPROVED', 'anything', '', 'approved\n'])
def test_document_review_only_accepts_defined_states(client, world, status):
    cid, actors = world
    mid, did = promoted_material(client, world)
    before = no_write_snapshot(cid)
    expect(request(client, 'PATCH', f'/documents/{did}/review', actors['auditor'], json={'review_status': status}), 422)
    assert no_write_snapshot(cid) == before


def test_current_consultant_can_write_but_former_cannot_use_indirect_ids(client, world):
    cid, actors = world
    mid = add(models.Material(case_id=cid, name='Gelatin')).material_id
    new = actors['unassigned_consultant']
    expect(request(client, 'POST', f'/cases/{cid}/assign-consultant', actors['operator'],
                   json={'consultant_id': new['uid']}), 200)
    before = no_write_snapshot(cid)
    expect(request(client, 'PATCH', f'/materials/{mid}', actors['consultant'], json={'supplier': 'Old'}), 403)
    assert no_write_snapshot(cid) == before
    expect(request(client, 'PATCH', f'/materials/{mid}', new, json={'supplier': 'Current'}), 200)
    assert get(models.Material, mid).supplier == 'Current'


@pytest.mark.parametrize('expiry', [None, 'not-a-date'])
def test_verifier_unknown_expiry_is_not_verified(expiry):
    doc = {'doc_type': 'halal_certificate', 'fields': {'cert_no': NUMBER, 'expiry_date': expiry},
           'text_full': 'HALAL CERTIFICATE\nCertificate No: ' + NUMBER}
    result = cert_verify.verify(NUMBER, [doc])
    assert result['verdict'] == 'UNVERIFIED_EXPIRY'
    assert not cert_verify.promotes(result)


def test_verifier_selects_valid_document_among_same_number_copies():
    docs = [{'document_id': str(i), 'doc_type': 'halal_certificate',
             'text_full': 'HALAL CERTIFICATE\nCertificate No: ' + NUMBER,
             'fields': {'cert_no': NUMBER, 'expiry_date': expiry}}
            for i, expiry in enumerate(['2000-01-01', '2099-01-01'])]
    result = cert_verify.verify(NUMBER, docs)
    assert cert_verify.promotes(result) and result['matched_document']['document_id'] == '1'


def test_certificate_number_requires_stored_original(client, world):
    cid, actors = world
    mid = add(models.Material(case_id=cid, name='Gelatin')).material_id
    add(models.DocumentAsset(case_id=cid, doc_type='halal_certificate', review_status='approved',
        filename='missing.txt', fields={'cert_no': NUMBER, 'expiry_date': '2099-01-01'}))
    result = expect(request(client, 'PATCH', f'/materials/{mid}', actors['owner'], json={'cert_no': NUMBER}), 200).json()
    assert result['cert_no'] is None and result['declared_cert_no'] == NUMBER
    assert not result['screen']['cert_promoted']
