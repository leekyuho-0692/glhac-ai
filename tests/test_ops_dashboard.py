"""M01 최고운영자 운영현황 대시보드(P0-3차) — 격리 테스트.

고유 DB(glhac_ops_test.db) · GLHAC_DEV=1 · TestClient 기반(서버 불필요).
실행: <venv>/bin/python -m pytest tests/test_ops_dashboard.py -q
검증: (a) operator 집계 200 (b) 신규업체 승인→상태전이+이벤트
      (c) 거절 사유 필수·이벤트 (d) 오디터 배정→Notification+담당수 반영 (e) applicant 403.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_ops_test.db"
os.environ["GLHAC_DEV"] = "1"

# 깨끗한 상태에서 시작(이전 실행 잔여 제거)
_DBFILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "glhac_ops_test.db")
if os.path.exists(_DBFILE):
    os.remove(_DBFILE)

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app import models  # noqa: E402
from app.db import SessionLocal  # noqa: E402


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(tok):
    return {"Authorization": "Bearer " + tok}


def _mk_case(status, company, org_id="org_demo", **extra):
    db = SessionLocal()
    try:
        c = models.CaseApplication(org_id=org_id, company_name=company, status=status, **extra)
        db.add(c)
        db.commit()
        return c.case_id
    finally:
        db.close()


def _events(case_id, action):
    db = SessionLocal()
    try:
        return (db.query(models.WorkflowEvent)
                .filter_by(case_id=case_id, action=action).count())
    finally:
        db.close()


def _case_status(case_id):
    db = SessionLocal()
    try:
        return db.get(models.CaseApplication, case_id).status
    finally:
        db.close()


def _auditor_id():
    db = SessionLocal()
    try:
        u = db.query(models.User).filter_by(role="auditor", org_id="org_demo").first()
        return u.user_id
    finally:
        db.close()


def test_a_operator_dashboard_200():
    """operator가 통합 대시보드·개별 집계 조회 200."""
    with TestClient(app) as c:
        tok = _tok(c, "operator1", "pw")
        r = c.get("/ops/dashboard", headers=_h(tok))
        assert r.status_code == 200, r.text
        d = r.json()
        for k in ("regions", "capacity", "pending_companies", "auditors"):
            assert k in d, d
        assert "by_status" in d["capacity"] and "auditor_load" in d["capacity"]
        # 개별 라우트도 200
        for path in ("/ops/regions", "/ops/capacity", "/ops/pending-companies", "/ops/auditors"):
            assert c.get(path, headers=_h(tok)).status_code == 200, path


def test_b_company_approve_transitions_and_records_event():
    """신규 업체 승인 → onboarding→application_draft 전이 + ops.company_approved 이벤트."""
    cid = _mk_case("onboarding", "PT Approve Test", nib="1234567890")
    with TestClient(app) as c:
        tok = _tok(c, "operator1", "pw")
        # 승인 전 대기목록에 노출
        pend = c.get("/ops/pending-companies", headers=_h(tok)).json()
        assert any(x["case_id"] == cid for x in pend["items"]), pend
        r = c.post("/ops/companies/%s/approve" % cid, headers=_h(tok))
        assert r.status_code == 200, r.text
        assert r.json()["transitioned_to"] == "application_draft", r.json()
    assert _case_status(cid) == "application_draft"
    assert _events(cid, "ops.company_approved") == 1


def test_c_reject_requires_reason_and_records_event():
    """거절 사유 필수(422) + 사유 제공 시 ops.company_rejected 이벤트 기록·대기목록 제외."""
    cid = _mk_case("onboarding", "PT Reject Test")
    with TestClient(app) as c:
        tok = _tok(c, "operator1", "pw")
        # 사유 없음 → 422
        r0 = c.post("/ops/companies/%s/reject" % cid, json={"reason": "  "}, headers=_h(tok))
        assert r0.status_code == 422 and r0.json()["detail"]["code"] == "REASON_REQUIRED", r0.text
        # 사유 제공 → 200
        r = c.post("/ops/companies/%s/reject" % cid,
                   json={"reason": "서류 미비"}, headers=_h(tok))
        assert r.status_code == 200, r.text
        pend = c.get("/ops/pending-companies", headers=_h(tok)).json()
        assert not any(x["case_id"] == cid for x in pend["items"]), "거절 건이 대기목록에 남음"
    assert _events(cid, "ops.company_rejected") == 1
    assert _case_status(cid) == "onboarding"   # 불법 전이 없음(상태 유지)


def test_d_assign_auditor_notifies_and_reflects_load():
    """오디터 배정 → Notification 생성 + 담당수 반영 + 미배정 목록에서 제외."""
    cid = _mk_case("document_pre_audit_requested", "PT Assign Test")
    aid = _auditor_id()
    db = SessionLocal()
    try:
        before_notif = db.query(models.Notification).filter_by(
            event_type="ops.auditor_assigned").count()
    finally:
        db.close()
    with TestClient(app) as c:
        tok = _tok(c, "operator1", "pw")
        auds = c.get("/ops/auditors", headers=_h(tok)).json()
        assert any(u["case_id"] == cid for u in auds["unassigned"]), "미배정 목록에 없음"
        r = c.post("/ops/cases/%s/assign-auditor" % cid,
                   json={"auditor_id": aid}, headers=_h(tok))
        assert r.status_code == 200, r.text
        assert r.json()["load"] >= 1, r.json()
        after = c.get("/ops/auditors", headers=_h(tok)).json()
        assert not any(u["case_id"] == cid for u in after["unassigned"]), "배정 후에도 미배정"
        assert any(a["user_id"] == aid and a["load"] >= 1 for a in after["auditors"]), after
        cap = c.get("/ops/capacity", headers=_h(tok)).json()
        assert any(a["user_id"] == aid and a["load"] >= 1 for a in cap["auditor_load"]), cap
    db = SessionLocal()
    try:
        after_notif = db.query(models.Notification).filter_by(
            event_type="ops.auditor_assigned").count()
    finally:
        db.close()
    assert after_notif == before_notif + 1, "오디터 알림 미생성"
    assert _events(cid, "ops.auditor_assigned") == 1


def test_e_applicant_forbidden():
    """비권한 역할(applicant)은 운영현황·액션 403."""
    cid = _mk_case("onboarding", "PT Forbidden Test")
    with TestClient(app) as c:
        tok = _tok(c, "applicant1", "pw")
        assert c.get("/ops/dashboard", headers=_h(tok)).status_code == 403
        assert c.get("/ops/regions", headers=_h(tok)).status_code == 403
        assert c.post("/ops/companies/%s/approve" % cid, headers=_h(tok)).status_code == 403


def test_approval_moves_out_of_intake_and_duplicate_has_no_side_effects():
    cid = _mk_case("onboarding", "Intake paging approval")
    with TestClient(app) as client:
        headers = _h(_tok(client, "admin", "admin"))
        def item():
            return next(x for x in client.get("/cases?limit=500", headers=headers).json()["items"] if x["case_id"] == cid)
        assert item()["intake_pending"] is True
        first = client.post("/ops/companies/%s/approve" % cid, headers=headers)
        assert first.status_code == 200
        assert item()["intake_pending"] is False
        assert item()["company_decision"] == "approved"
        with SessionLocal() as db:
            before = db.query(models.Notification).filter_by(case_id=cid, event_type="ops.company_approved").count()
        again = client.post("/ops/companies/%s/approve" % cid, headers=headers)
        assert again.status_code == 200 and again.json()["already_approved"] is True
        with SessionLocal() as db:
            assert db.query(models.Notification).filter_by(case_id=cid, event_type="ops.company_approved").count() == before
        assert _events(cid, "ops.company_approved") == 1


def test_approval_blocked_does_not_record_success(monkeypatch):
    from app import main
    cid = _mk_case("onboarding", "Blocked intake")
    monkeypatch.setattr(main.sm, "can_transition", lambda *args: (False, [{"code": "TEST_BLOCKER"}]))
    with TestClient(app) as client:
        headers = _h(_tok(client, "admin", "admin"))
        r = client.post("/ops/companies/%s/approve" % cid, headers=headers)
        assert r.status_code == 409 and r.json()["detail"]["blockers"] == [{"code": "TEST_BLOCKER"}]
    assert _case_status(cid) == "onboarding"
    assert _events(cid, "ops.company_approved") == 0


def test_non_pending_approval_rejected_and_rejected_case_not_pending():
    draft = _mk_case("application_draft", "Already drafting")
    rejected = _mk_case("onboarding", "Rejected intake")
    with TestClient(app) as client:
        headers = _h(_tok(client, "admin", "admin"))
        assert client.post("/ops/companies/%s/approve" % draft, headers=headers).status_code == 409
        assert client.post("/ops/companies/%s/reject" % rejected, headers=headers, json={"reason": "Test rejection"}).status_code == 200
        assert client.post("/ops/companies/%s/approve" % rejected, headers=headers).status_code == 409
        rows = {x["case_id"]: x for x in client.get("/cases?limit=500", headers=headers).json()["items"]}
        assert not rows[draft]["intake_pending"]
        assert rows[rejected]["company_decision"] == "rejected" and not rows[rejected]["intake_pending"]


def test_old_failed_approval_event_does_not_hide_pending_case():
    from app import main
    cid = _mk_case("onboarding", "Historical false success")
    with SessionLocal() as db:
        case = db.get(models.CaseApplication, cid)
        main.sm.record_event(db, case, case.status, case.status, "ops.company_approved", "admin", "old-admin", {"transitioned_to": None})
        db.commit()
    with TestClient(app) as client:
        headers = _h(_tok(client, "admin", "admin"))
        row = next(x for x in client.get("/cases?limit=500", headers=headers).json()["items"] if x["case_id"] == cid)
        assert row["intake_pending"] is True and row["company_decision"] is None
        assert client.post("/ops/companies/%s/approve" % cid, headers=headers).status_code == 200


def test_real_auditor_is_listed_and_demo_accounts_are_marked():
    from app import auth
    with SessionLocal() as db:
        auditor = models.User(username="real-auditor@example.com", password_hash=auth.hash_pw("TestOnly123!"),
                              role="auditor", org_id="org_demo")
        sample = models.User(username="test", password_hash=auth.hash_pw("TestOnly123!"), role="auditor", org_id="org_demo")
        db.add_all([auditor, sample])
        db.commit()
    with TestClient(app) as client:
        rows = client.get('/ops/auditors', headers=_h(_tok(client, 'admin', 'admin'))).json()['auditors']
        assert next(x for x in rows if x['username'] == 'auditor1')['is_demo'] is True
        assert next(x for x in rows if x['username'] == 'test')['is_demo'] is True
        actual = next(x for x in rows if x['username'] == 'real-auditor@example.com')
        assert actual['is_demo'] is False and actual['display_name'] == 'real-auditor@example.com'


def test_production_cannot_assign_demo_auditor(monkeypatch):
    from app import auth
    cid = _mk_case('document_pre_audit_requested', 'No demo assignment')
    with TestClient(app) as client:
        headers = _h(_tok(client, 'admin', 'admin'))
        with SessionLocal() as db:
            auditor = db.query(models.User).filter_by(username='auditor1').first()
            aid = auditor.user_id
        monkeypatch.setattr(auth, 'dev_mode', lambda: False)
        response = client.post('/ops/cases/' + cid + '/assign-auditor', headers=headers, json={'auditor_id': aid})
        assert response.status_code == 409
        assert response.json()['detail']['code'] == 'DEMO_AUDITOR_NOT_ASSIGNABLE'
    assert _events(cid, 'ops.auditor_assigned') == 0
