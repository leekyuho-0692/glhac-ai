"""A08: 현장심사 일정 캘린더(클라이언트 주도 조율) — 격리 DB 스모크.
스키마 무변경(WorkflowEvent onsite_schedule.*) 검증. 실행: <venv>/bin/python tests/test_onsite_calendar.py
8800 금지 · 고유 격리 DB명 · os.environ 직접대입.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_v3_onsitecal_test.db"
os.environ["GLHAC_DEV"] = "1"

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(t):
    return {"Authorization": "Bearer " + t}


def _mkcase(c, adm):
    return c.post("/cases", json={"company_name": "CalCo", "org_id": "org_demo"},
                  headers=_h(adm)).json()["case_id"]


def test_client_propose_then_auditor_confirm():
    """클라이언트 제시 → 오디터 확정. latest-wins 상태·이력 검증."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        ap = _tok(c, "applicant1", "pw")
        aud = _tok(c, "auditor1", "pw")
        cid = _mkcase(c, adm)
        # 초기 상태 none
        st = c.get(f"/cases/{cid}/onsite-schedule", headers=_h(ap)).json()
        assert st["status"] == "none" and st["client_dates"] == [], st
        # 클라이언트 제시
        r = c.post(f"/cases/{cid}/onsite-schedule/propose",
                   json={"dates": ["2026-07-21", "2026-07-22", "2026-07-23"], "note": "오전 선호"},
                   headers=_h(ap))
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "proposed"
        assert r.json()["client_dates"] == ["2026-07-21", "2026-07-22", "2026-07-23"], r.json()
        # 오디터 확정
        r2 = c.post(f"/cases/{cid}/onsite-schedule/confirm",
                    json={"date": "2026-07-22", "time": "10:00"}, headers=_h(aud))
        assert r2.status_code == 200, r2.text
        assert r2.json()["status"] == "confirmed"
        assert r2.json()["confirmed"] == {"date": "2026-07-22", "time": "10:00"}, r2.json()
        # 이력에 propose+confirm 2건
        kinds = [h["kind"] for h in r2.json()["history"]]
        assert kinds == ["propose", "confirm"], kinds


def test_reschedule_roundtrip():
    """제시일 불가 → 오디터 후보일 재제안(재조율) → 확정."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        ap = _tok(c, "applicant1", "pw")
        aud = _tok(c, "auditor1", "pw")
        cid = _mkcase(c, adm)
        c.post(f"/cases/{cid}/onsite-schedule/propose", json={"dates": ["2026-07-17"]}, headers=_h(ap))
        r = c.post(f"/cases/{cid}/onsite-schedule/reschedule",
                   json={"dates": ["2026-07-21", "2026-07-22", "2026-07-23"]}, headers=_h(aud))
        assert r.status_code == 200 and r.json()["status"] == "reproposed", r.text
        assert r.json()["auditor_dates"] == ["2026-07-21", "2026-07-22", "2026-07-23"], r.json()
        # 재제안 시 클라 제시일은 그대로 유지(양측 이력 병행 표시)
        assert r.json()["client_dates"] == ["2026-07-17"], r.json()
        r2 = c.post(f"/cases/{cid}/onsite-schedule/confirm", json={"date": "2026-07-23"}, headers=_h(aud))
        assert r2.json()["status"] == "confirmed" and r2.json()["confirmed"]["date"] == "2026-07-23"


def test_rbac_and_validation():
    """오디터는 propose 불가(403) · 클라이언트는 confirm 불가(403) · 빈 dates 400."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        ap = _tok(c, "applicant1", "pw")
        aud = _tok(c, "auditor1", "pw")
        cid = _mkcase(c, adm)
        # 오디터가 클라이언트 제시 시도 → 403
        assert c.post(f"/cases/{cid}/onsite-schedule/propose", json={"dates": ["2026-07-21"]},
                      headers=_h(aud)).status_code == 403
        # 클라이언트가 확정 시도 → 403
        assert c.post(f"/cases/{cid}/onsite-schedule/confirm", json={"date": "2026-07-21"},
                      headers=_h(ap)).status_code == 403
        # 빈 dates → 400
        assert c.post(f"/cases/{cid}/onsite-schedule/propose", json={"dates": []},
                      headers=_h(ap)).status_code == 400
        # 확정 date 누락 → 400
        assert c.post(f"/cases/{cid}/onsite-schedule/confirm", json={}, headers=_h(aud)).status_code == 400


if __name__ == "__main__":
    tests = [test_client_propose_then_auditor_confirm, test_reschedule_roundtrip, test_rbac_and_validation]
    ok = 0
    for fn in tests:
        try:
            fn(); ok += 1; print("[PASS]", fn.__name__)
        except AssertionError as e:
            print("[FAIL]", fn.__name__, "—", e)
        except Exception as e:  # noqa
            import traceback; traceback.print_exc(); print("[ERROR]", fn.__name__, "—", type(e).__name__, e)
    print(f"\n{ok}/{len(tests)} passed")
    sys.exit(0 if ok == len(tests) else 1)
