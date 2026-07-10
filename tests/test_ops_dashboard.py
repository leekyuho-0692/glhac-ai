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
