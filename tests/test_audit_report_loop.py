"""P0-4 현장 심사보고서 재심 루프 + 오디터 E-서명 + 파트와 전달 게이트 검증.
TestClient 기반(서버 불필요). 실행: <venv>/bin/python -m pytest tests/test_audit_report_loop.py -q

주의(테스트 격리): 이 프로젝트는 tests 일괄 실행 시 여러 파일이 공유 DB로 상호 오염되므로,
이 파일은 고유 DB(glhac_auditrep_test.db)를 app import 전에 강제 대입해 격리한다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_auditrep_test.db"   # setdefault 금지 — 격리 강제
os.environ["GLHAC_DEV"] = "1"   # 데모 계정 시드(테스트 전용)

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


def _mkcase(c, admin):
    return c.post("/cases", json={"org_id": "org_demo", "company_name": "AuditRepCo"},
                  headers=_h(admin)).json()["case_id"]


def _notif_count(case_id, role):
    db = SessionLocal()
    try:
        return db.query(models.Notification).filter_by(case_id=case_id, role=role).count()
    finally:
        db.close()


def _event_count(case_id, action):
    db = SessionLocal()
    try:
        return db.query(models.WorkflowEvent).filter_by(case_id=case_id, action=action).count()
    finally:
        db.close()


def _make_approved_report(c, admin, cid):
    """audit-report 생성 → approve → gen_doc_id 반환."""
    r = c.post(f"/cases/{cid}/audit-report", headers=_h(admin))
    assert r.status_code == 200, r.text
    gid = r.json()["gen_doc_id"]
    ap = c.post(f"/gen-docs/{gid}/approve", headers=_h(admin))
    assert ap.status_code == 200, ap.text
    return gid


def _make_sjph_complete(c, admin, cid):
    """HPAS 5요소 ok + PenyeliaHalal active — SJPH complete 게이트 충족."""
    for el in ("commitment", "materials", "process", "product", "monitoring"):
        rr = c.patch(f"/cases/{cid}/sjph", json={"element": el, "status": "ok"}, headers=_h(admin))
        assert rr.status_code == 200, rr.text
    pr = c.post("/orgs/org_demo/penyelia", json={"name": "Pen Halal A"}, headers=_h(admin))
    assert pr.status_code == 200, pr.text


def test_return_round_increments_and_notifies_applicant():
    """① return round 1→2 누적 + 클라이언트(applicant) 알림 적재. comment 필수(422)."""
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        aud = _tok(c, "auditor1", "pw")
        cid = _mkcase(c, admin)
        before = _notif_count(cid, "applicant")
        # comment 누락 → 422
        rb = c.post(f"/cases/{cid}/audit-report/return", json={"comment": "  "}, headers=_h(aud))
        assert rb.status_code == 422 and rb.json()["detail"]["code"] == "COMMENT_REQUIRED", rb.text
        r1 = c.post(f"/cases/{cid}/audit-report/return", json={"comment": "표지 누락"}, headers=_h(aud))
        assert r1.status_code == 200 and r1.json()["round"] == 1, r1.text
        r2 = c.post(f"/cases/{cid}/audit-report/return", json={"comment": "서명란 누락"}, headers=_h(aud))
        assert r2.status_code == 200 and r2.json()["round"] == 2, r2.text
        assert _notif_count(cid, "applicant") == before + 2, "applicant 알림 미적재"
        st = c.get(f"/cases/{cid}/audit-report/status", headers=_h(aud)).json()
        assert st["return_count"] == 2, st
        assert st["latest_return"]["round"] == 2 and st["latest_return"]["comment"] == "서명란 누락", st


def test_resubmit_notifies_auditor():
    """② resubmit → 오디터 알림 + round=현재 return 회차."""
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        aud = _tok(c, "auditor1", "pw")
        ap = _tok(c, "applicant1", "pw")
        cid = _mkcase(c, admin)
        c.post(f"/cases/{cid}/audit-report/return", json={"comment": "보완"}, headers=_h(aud))
        before = _notif_count(cid, "auditor")
        r = c.post(f"/cases/{cid}/audit-report/resubmit", json={"note": "수정 완료"}, headers=_h(ap))
        assert r.status_code == 200 and r.json()["round"] == 1, r.text
        assert _notif_count(cid, "auditor") == before + 1, "auditor 알림 미적재"
        st = c.get(f"/cases/{cid}/audit-report/status", headers=_h(ap)).json()
        assert st["latest_resubmit"]["round"] == 1 and st["latest_resubmit"]["note"] == "수정 완료", st


def test_sign_gate_requires_approved_report():
    """③ sign 게이트 — 미approved면 409 REPORT_NOT_APPROVED, approved면 성공·status 반영."""
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        aud = _tok(c, "auditor1", "pw")
        cid = _mkcase(c, admin)
        # 보고서 없음/미승인 → 409
        r0 = c.post(f"/cases/{cid}/audit-report/sign", json={"name": "Auditor Kim"}, headers=_h(aud))
        assert r0.status_code == 409 and r0.json()["detail"]["code"] == "REPORT_NOT_APPROVED", r0.text
        # draft만 생성(미승인) → 여전히 409
        c.post(f"/cases/{cid}/audit-report", headers=_h(admin))
        r1 = c.post(f"/cases/{cid}/audit-report/sign", json={"name": "Auditor Kim"}, headers=_h(aud))
        assert r1.status_code == 409, r1.text
        # approve 후 → 성공
        _make_approved_report(c, admin, cid)
        # name 누락 → 422
        rn = c.post(f"/cases/{cid}/audit-report/sign", json={"name": " "}, headers=_h(aud))
        assert rn.status_code == 422 and rn.json()["detail"]["code"] == "NAME_REQUIRED", rn.text
        r2 = c.post(f"/cases/{cid}/audit-report/sign", json={"name": "Auditor Kim"}, headers=_h(aud))
        assert r2.status_code == 200 and r2.json()["name"] == "Auditor Kim", r2.text
        st = c.get(f"/cases/{cid}/audit-report/status", headers=_h(aud)).json()
        assert st["signed"] and st["signed"]["name"] == "Auditor Kim", st


def test_send_fatwa_gate_blocks_and_passes():
    """④ send-fatwa 게이트 — 서명/SJPH 없으면 409 FATWA_GATE_BLOCKED+missing,
    전부 충족 시 성공(gate passed) + 이벤트 기록. 전이 가드로 fatwa_review 직접전이가
    막혀도 게이트 통과+이벤트 기록으로 단언(LLM/상태머신 미의존)."""
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        aud = _tok(c, "auditor1", "pw")
        cid = _mkcase(c, admin)
        # 아무 요건 미충족 → 409 + missing 3종
        r0 = c.post(f"/cases/{cid}/audit-report/send-fatwa", headers=_h(aud))
        assert r0.status_code == 409, r0.text
        d0 = r0.json()["detail"]
        assert d0["code"] == "FATWA_GATE_BLOCKED", d0
        assert set(d0["missing"]) == {"REPORT_NOT_APPROVED", "AUDITOR_SIGN_REQUIRED", "SJPH_INCOMPLETE"}, d0
        # 보고서 승인 + 서명(단 SJPH 미완) → 여전히 SJPH_INCOMPLETE만 남음
        _make_approved_report(c, admin, cid)
        c.post(f"/cases/{cid}/audit-report/sign", json={"name": "Auditor Kim"}, headers=_h(aud))
        r1 = c.post(f"/cases/{cid}/audit-report/send-fatwa", headers=_h(aud))
        assert r1.status_code == 409, r1.text
        assert r1.json()["detail"]["missing"] == ["SJPH_INCOMPLETE"], r1.json()
        # SJPH 완성 → 게이트 통과
        _make_sjph_complete(c, admin, cid)
        before_ev = _event_count(cid, "audit_report.send_fatwa")
        before_notif = _notif_count(cid, "fatwa_liaison")
        r2 = c.post(f"/cases/{cid}/audit-report/send-fatwa", headers=_h(aud))
        assert r2.status_code == 200 and r2.json()["gate"] == "passed", r2.text
        assert _event_count(cid, "audit_report.send_fatwa") == before_ev + 1, "send_fatwa 이벤트 미기록"
        assert _notif_count(cid, "fatwa_liaison") == before_notif + 1, "샤리아 알림 미적재"
        st = c.get(f"/cases/{cid}/audit-report/status", headers=_h(aud)).json()
        assert st["fatwa_gate"]["ready"] is True and st["sent_fatwa"] is True, st


def test_reconfirm_decision_guard_422():
    """⑤ reconfirm decision 검증 — ok|hold 외 422, 정상은 latest-wins 반영."""
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        aud = _tok(c, "auditor1", "pw")
        cid = _mkcase(c, admin)
        rb = c.post(f"/cases/{cid}/audit-report/reconfirm", json={"decision": "maybe"}, headers=_h(aud))
        assert rb.status_code == 422 and rb.json()["detail"]["code"] == "INVALID_DECISION", rb.text
        r = c.post(f"/cases/{cid}/audit-report/reconfirm",
                   json={"decision": "ok", "note": "확인"}, headers=_h(aud))
        assert r.status_code == 200 and r.json()["decision"] == "ok", r.text
        st = c.get(f"/cases/{cid}/audit-report/status", headers=_h(aud)).json()
        assert st["latest_reconfirm"]["decision"] == "ok", st
        # 오디터 라우트는 신청자에게 403
        assert c.post(f"/cases/{cid}/audit-report/reconfirm", json={"decision": "ok"},
                      headers=_h(_tok(c, "applicant1", "pw"))).status_code == 403


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
