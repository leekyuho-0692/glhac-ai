"""P3 UI 마감 — 백엔드 응답필드/집계 격리 스모크.

고유 DB(glhac_uip_test.db) · GLHAC_DEV=1 · TestClient 기반(서버 불필요).
실행: <venv>/bin/python -m pytest tests/test_ui_polish.py -q
검증 대상(백엔드 추가분만):
  - E2/M2  : GET /cases items 에 auditor_id·auditor_name (배정 반영)
  - M5     : /ops/pending-companies item 에 sector·created_at
  - M2/M3  : GET /auditor/dashboard 집계(담당·이번주현장·처리대기·미해결부적합)
  - M13    : /cases/{id}/resubmit-center preassess history[].actor/items + review_verdict
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_uip_test.db"
os.environ["GLHAC_DEV"] = "1"

_DBFILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "glhac_uip_test.db")
if os.path.exists(_DBFILE):
    os.remove(_DBFILE)

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app import models  # noqa: E402
from app.db import SessionLocal, engine, Base  # noqa: E402

# 테이블 선생성(첫 _mk_case 가 TestClient 진입 전이어도 안전). 유저 시드는 startup 이벤트.
Base.metadata.create_all(bind=engine)


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


def _auditor_id():
    db = SessionLocal()
    try:
        u = db.query(models.User).filter_by(role="auditor", org_id="org_demo").first()
        return u.user_id, u.username
    finally:
        db.close()


def test_m5_pending_has_sector_and_created_at():
    """신규업체 승인 대기 item 에 sector(profile_ext.business_type) + created_at."""
    cid = _mk_case("onboarding", "PT Sektor Test",
                   profile_ext={"business_type": "cosmetic"})
    with TestClient(app) as c:
        tok = _tok(c, "operator1", "pw")
        pend = c.get("/ops/pending-companies", headers=_h(tok)).json()
        item = next(x for x in pend["items"] if x["case_id"] == cid)
        assert item.get("sector") == "cosmetic", item
        assert item.get("created_at"), item


def test_e2_m2_cases_expose_auditor_after_assignment():
    """배정 후 GET /cases item 에 auditor_id·auditor_name 노출."""
    cid = _mk_case("document_pre_audit_requested", "PT Auditor Col")
    aid, aname = _auditor_id()
    with TestClient(app) as c:
        top = _tok(c, "operator1", "pw")
        r = c.post("/ops/cases/%s/assign-auditor" % cid,
                   json={"auditor_id": aid}, headers=_h(top))
        assert r.status_code == 200, r.text
        lst = c.get("/cases", headers=_h(top)).json()
        item = next(x for x in lst["items"] if x["case_id"] == cid)
        assert item.get("auditor_id") == aid, item
        assert item.get("auditor_name") == aname, item


def test_m2_m3_auditor_dashboard_aggregates():
    """오디터 대시보드 — 담당·이번주현장·처리대기·미해결부적합 집계."""
    cid = _mk_case("onsite_audit_scheduled", "PT Aud Dash")
    aid, aname = _auditor_id()
    # 이번 주 월요일 현장실사 예정 + 오픈 부적합 1건
    monday = (datetime.utcnow().date() - timedelta(days=datetime.utcnow().date().weekday()))
    db = SessionLocal()
    try:
        db.add(models.AuditPlan(case_id=cid, lph_name="LPH-X",
                                scheduled_date=monday.isoformat(), status="scheduled"))
        db.add(models.AuditFinding(case_id=cid, finding="temuan uji",
                                   severity="minor", status="open"))
        db.commit()
    finally:
        db.close()
    with TestClient(app) as c:
        top = _tok(c, "operator1", "pw")
        assert c.post("/ops/cases/%s/assign-auditor" % cid,
                      json={"auditor_id": aid}, headers=_h(top)).status_code == 200
        # 배정 수락 워크플로우(app): 대시보드 '담당'은 수락된 배정만 집계 → 배정 오디터가 수락
        atok = _tok(c, aname, "pw")
        acc = c.post("/cases/%s/assignment/respond" % cid,
                     json={"decision": "accepted"}, headers=_h(atok))
        assert acc.status_code == 200, acc.text
        d = c.get("/auditor/dashboard", headers=_h(atok)).json()
        for k in ("assigned_count", "week_onsite", "pending_review",
                  "open_findings", "week_start", "week_end"):
            assert k in d, d
        assert d["assigned_count"] >= 1, d
        assert d["week_onsite"] >= 1, d
        assert d["pending_review"] >= 1, d      # onsite_audit_scheduled ∈ AUDIT_STAGES
        assert d["open_findings"] >= 1, d
        # 비권한(applicant) 403
        cli = _tok(c, "applicant1", "pw")
        assert c.get("/auditor/dashboard", headers=_h(cli)).status_code == 403


def test_m13_resubmit_history_has_actor_items_and_verdict():
    """재전송 이력 — preassess history[].actor/items + review_verdict 필드."""
    cid = _mk_case("document_pre_audit_in_review", "PT Resubmit Hist")
    with TestClient(app) as c:
        top = _tok(c, "operator1", "pw")
        r = c.post("/cases/%s/preassess/doc-request" % cid,
                   json={"items": [{"doc_type": "npwp", "doc_type_ko": "사업자등록"}],
                         "message": "서류 보완 요청"}, headers=_h(top))
        assert r.status_code == 200, r.text
        rc = c.get("/cases/%s/resubmit-center" % cid, headers=_h(top)).json()
        pre = next(x for x in rc["requests"] if x["source"] == "preassess")
        assert "review_verdict" in pre, pre
        hist = pre.get("history") or []
        assert hist and hist[0].get("actor"), hist
        assert isinstance(hist[0].get("items"), list), hist
