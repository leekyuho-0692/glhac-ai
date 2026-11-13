"""확인대기 원료는 사전상 halal/low 확정만 심의·발급 가드를 통과한다."""
import pytest

from app import main as m, models
from test_axis_d1_freeze_issue import PNG, api, case, freeze, ok, pending
from test_final_e2_rules import seed_selfdeclare_prerequisites


def prepare(api, phase):
    cid, _, _ = case(api)
    if phase in ('issue', 'checker'):
        freeze(api, cid)
        if phase == 'checker':
            approval = ok(api('POST', f'/cases/{cid}/certificate/issue', role='operator'))
            return cid, f"/approvals/{approval['approval_id']}/approve", {}, 'admin'
        return cid, f'/cases/{cid}/certificate/issue', None, 'operator'
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        c.pathway, c.risk_category, c.is_msme = 'self_declare', 'low', True
        c.status = 'self_declaration_submitted' if phase == 'entry' else 'committee_verification'
        seed_selfdeclare_prerequisites(db, c)
        db.commit()
    route = 'transition' if phase == 'entry' else 'committee/decide'
    body = {'to_state': 'committee_verification'} if phase == 'entry' else {'decision': 'approve'}
    return cid, f'/cases/{cid}/{route}', body, 'operator'


def assert_pending_blocked(response, name):
    assert response.status_code == 409, response.text
    detail = response.json()['detail']
    assert {'code': 'PENDING_CRITICAL_MATERIAL', 'materials': [name]} in detail.get('blockers', [detail])


@pytest.mark.parametrize('phase', ['entry', 'decision', 'issue', 'checker'])
@pytest.mark.parametrize('name', ['1,2-Hexanediol', 'Glycerin', 'Allantoin', 'Unknown cosmetic ingredient',
                                 'lard', 'gelatin'])
@pytest.mark.usefixtures('without_kma_catalogue')
def test_unverified_pending_material_blocks_every_existing_gate(api, phase, name):
    cid, route, body, role = prepare(api, phase)
    pending(cid, name)
    assert_pending_blocked(api('POST', route, body, role), name)
    with m.SessionLocal() as db:
        assert [item['value'] for item in m.sm.pending_extractions(db, cid)] == [name]
        assert db.query(models.HalalCertificate).filter_by(case_id=cid).count() == 0


@pytest.mark.parametrize('phase', ['entry', 'decision', 'issue', 'checker'])
@pytest.mark.parametrize('failure', [
    RuntimeError('screening unavailable'), None, {},
    {'error': 'screening failed'}, {'error': ''}, {'error': None},
    {'result': 'UNKNOWN'}, {'result': None}, {'status': 'unknown'}, {'severity': None},
    {'decision_by': 'unmatched_default'}, {'decision_by': None}, {'v1_risk': 'medium'},
])
def test_pending_screening_failures_block_instead_of_passing_or_crashing(api, monkeypatch, phase, failure):
    cid, route, body, role = prepare(api, phase)
    pending(cid, 'Water')
    real_screen = m.screening.screen_merged
    safe = real_screen('Water')

    def injected(name, *args, **kwargs):
        if name != 'Water':
            return real_screen(name, *args, **kwargs)
        if isinstance(failure, Exception):
            raise failure
        return {**safe, **failure} if failure else failure

    monkeypatch.setattr(m.screening, 'screen_merged', injected)
    assert_pending_blocked(api('POST', route, body, role), 'Water')


@pytest.mark.parametrize('phase', ['entry', 'decision', 'issue', 'checker'])
@pytest.mark.parametrize('name', [None, 'Water'])
def test_empty_or_dictionary_safe_pending_materials_allow_normal_flow(api, phase, name):
    cid, route, body, role = prepare(api, phase)
    if name:
        pending(cid, name)
    response = api('POST', route, body, role)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize('phase', ['entry', 'decision'])
@pytest.mark.usefixtures('without_kma_catalogue')
def test_auditor_dismissal_resolves_pending_material_blocker(api, phase):
    cid, route, body, role = prepare(api, phase)
    pending(cid, '1,2-Hexanediol')
    assert_pending_blocked(api('POST', route, body, role), '1,2-Hexanediol')
    result = ok(api('POST', f'/cases/{cid}/pending-extractions/apply', {
        'dismiss': ['1,2-Hexanediol'], 'reason': '원문 대조: 인증 대상에 포함되지 않은 후보'}, 'auditor'))
    assert result['count'] == 1
    assert ok(api('GET', f'/cases/{cid}/pending-extractions'))['items'] == []
    response = api('POST', route, body, role)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize('phase', ['entry', 'decision'])
@pytest.mark.usefixtures('without_kma_catalogue')
def test_auditor_application_resolves_pending_but_preserves_evidence_requirement(api, phase):
    cid, route, body, role = prepare(api, phase)
    pending(cid, '1,2-Hexanediol')
    assert_pending_blocked(api('POST', route, body, role), '1,2-Hexanediol')
    result = ok(api('POST', f'/cases/{cid}/pending-extractions/apply', {
        'values': ['1,2-Hexanediol'], 'reason': '원문 대조 후 원료 확인 적용'}, 'auditor'))
    assert result['applied']['materials'] == 1
    assert ok(api('GET', f'/cases/{cid}/pending-extractions'))['items'] == []
    response = api('POST', route, body, role)
    assert response.status_code == 409, response.text
    assert {item['code'] for item in response.json()['detail']['blockers']} == {'MATERIAL_EVIDENCE_INCOMPLETE'}
    with m.SessionLocal() as db:
        material = db.query(models.Material).filter_by(case_id=cid, name='1,2-Hexanediol').one()
        mid = material.material_id
        assert m.sm.material_safety_blockers(db, db.get(models.CaseApplication, cid)) == []
    ok(api('POST', f'/cases/{cid}/materials/{mid}/evidence', {
        'filename': 'source.png', 'file_b64': PNG, 'evidence_type': 'source_declaration'}))
    did = ok(api('GET', f'/cases/{cid}/materials/{mid}/evidence'))[0]['document_id']
    ok(api('PATCH', f'/documents/{did}/review', {'review_status': 'approved'}, 'auditor'))
    response = api('POST', route, body, role)
    assert response.status_code == 200, response.text
