"""P0-3 사전심사 오디터 회신 루프 — WorkflowEvent(latest-wins) 저장 검증.
TestClient 기반(서버 불필요). 실행: <venv>/bin/python -m pytest tests/test_preassess_loop.py -q

주의(테스트 격리): 이 프로젝트는 tests 일괄 실행 시 여러 파일이 glhac_v3_test.db를 공유해
상호 오염되므로, 이 파일은 고유 DB(glhac_preassess_test.db)를 app import 전에 강제 대입해 격리한다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_preassess_test.db"   # setdefault 금지 — 격리 강제
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
    return c.post("/cases", json={"org_id": "org_demo", "company_name": "PreassessCo"},
                  headers=_h(admin)).json()["case_id"]


def _notif_count(case_id, role):
    db = SessionLocal()
    try:
        return db.query(models.Notification).filter_by(case_id=case_id, role=role).count()
    finally:
        db.close()


def test_review_recorded_and_get_reflects():
    """① review 기록 → GET reviewed=true·sections 반영·verdict latest-wins."""
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        aud = _tok(c, "auditor1", "pw")
        cid = _mkcase(c, admin)
        # 최초 조회 — 미검토
        g0 = c.get(f"/cases/{cid}/preassess/review", headers=_h(aud)).json()
        assert g0["reviewed"] is False and g0["review"] is None, g0
        # 검토 저장(보완)
        r1 = c.post(f"/cases/{cid}/preassess/review",
                    json={"sections": {"documents": {"ok": True, "note": "ok"},
                                       "materials": {"ok": False, "note": "출처 불명"},
                                       "process": {"ok": True, "note": ""}},
                    "verdict": "supplement", "note": "재료 보완 필요"}, headers=_h(aud))
        assert r1.status_code == 200 and r1.json()["verdict"] == "supplement", r1.text
        g1 = c.get(f"/cases/{cid}/preassess/review", headers=_h(aud)).json()
        assert g1["reviewed"] is True, g1
        assert g1["review"]["sections"]["materials"]["ok"] is False, g1["review"]
        assert g1["review"]["verdict"] == "supplement", g1["review"]
        # 재검토(ready) → latest-wins
        assert c.post(f"/cases/{cid}/preassess/review",
                      json={"sections": {"documents": {"ok": True}, "materials": {"ok": True},
                                         "process": {"ok": True}}, "verdict": "ready"},
                      headers=_h(aud)).status_code == 200
        g2 = c.get(f"/cases/{cid}/preassess/review", headers=_h(aud)).json()
        assert g2["review"]["verdict"] == "ready", g2["review"]


def test_doc_request_round_increments_and_notifies_applicant():
    """② doc-request round 1→2 누적 + 클라이언트(applicant) 알림 적재."""
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        aud = _tok(c, "auditor1", "pw")
        cid = _mkcase(c, admin)
        before = _notif_count(cid, "applicant")
        r1 = c.post(f"/cases/{cid}/preassess/doc-request",
                    json={"items": [{"doc_type": "process_flow", "note": "공정도 필요"}],
                          "message": "1차 요청"}, headers=_h(aud))
        assert r1.status_code == 200 and r1.json()["round"] == 1, r1.text
        r2 = c.post(f"/cases/{cid}/preassess/doc-request",
                    json={"items": [{"doc_type": "material_list"}], "message": "2차 요청"},
                    headers=_h(aud))
        assert r2.status_code == 200 and r2.json()["round"] == 2, r2.text
        # 알림 2건 적재(role=applicant)
        assert _notif_count(cid, "applicant") == before + 2, "applicant 알림 미적재"
        # GET 통합 — history 2건·최신 doc_request round=2
        g = c.get(f"/cases/{cid}/preassess/review", headers=_h(aud)).json()
        assert len(g["doc_request_history"]) == 2, g["doc_request_history"]
        assert g["doc_request"]["round"] == 2, g["doc_request"]


def test_resubmit_notifies_auditor():
    """③ resubmit → 오디터 알림 + round=현재 doc_request 회차."""
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        aud = _tok(c, "auditor1", "pw")
        ap = _tok(c, "applicant1", "pw")
        cid = _mkcase(c, admin)
        c.post(f"/cases/{cid}/preassess/doc-request",
               json={"items": [{"doc_type": "process_flow"}], "message": "요청"}, headers=_h(aud))
        before = _notif_count(cid, "auditor")
        r = c.post(f"/cases/{cid}/preassess/resubmit", json={"note": "보완 완료"}, headers=_h(ap))
        assert r.status_code == 200 and r.json()["round"] == 1, r.text
        assert _notif_count(cid, "auditor") == before + 1, "auditor 알림 미적재"
        g = c.get(f"/cases/{cid}/preassess/review", headers=_h(ap)).json()
        assert g["resubmit"]["round"] == 1 and g["resubmit"]["note"] == "보완 완료", g["resubmit"]


def test_validation_guards_422():
    """④ 가드 — 빈 items·잘못된 verdict·잘못된 sections 키 422."""
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        aud = _tok(c, "auditor1", "pw")
        cid = _mkcase(c, admin)
        # 빈 items
        rb = c.post(f"/cases/{cid}/preassess/doc-request",
                    json={"items": [], "message": "x"}, headers=_h(aud))
        assert rb.status_code == 422 and rb.json()["detail"]["code"] == "ITEMS_REQUIRED", rb.text
        # 잘못된 verdict
        rv = c.post(f"/cases/{cid}/preassess/review",
                    json={"sections": {"documents": {"ok": True}}, "verdict": "maybe"}, headers=_h(aud))
        assert rv.status_code == 422 and rv.json()["detail"]["code"] == "INVALID_VERDICT", rv.text
        # 잘못된 sections 키
        rs = c.post(f"/cases/{cid}/preassess/review",
                    json={"sections": {"nope": {"ok": True}}, "verdict": "ready"}, headers=_h(aud))
        assert rs.status_code == 422 and rs.json()["detail"]["code"] == "BAD_SECTION", rs.text
        # 오디터 라우트는 신청자에게 403
        assert c.post(f"/cases/{cid}/preassess/review",
                      json={"sections": {"documents": {"ok": True}}, "verdict": "ready"},
                      headers=_h(_tok(c, "applicant1", "pw"))).status_code == 403


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
