"""워크플로우 모니터 집약 엔드포인트 격리 테스트 — GET /admin/workflow-monitor.
읽기전용 집약(신규 쓰기·스키마 변경 없음) + RBAC(ops/admin) 검증.
격리 DB(고유명, os.environ 직접대입). 실행: <venv>/bin/python tests/test_workflow_monitor.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
# 격리: 스모크(glhac_v3_test.db)와 다른 고유 DB, 직접 대입(setdefault 아님 — 확실히 격리)
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_wfmon_test.db"
os.environ["GLHAC_DEV"] = "1"

from fastapi.testclient import TestClient  # noqa: E402
from conftest import app_db_file  # noqa: E402
from app.main import app  # noqa: E402


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(tok):
    return {"Authorization": "Bearer " + tok}


def test_workflow_monitor_rbac():
    """ops·admin 허용 / applicant·auditor 차단(전역 운영 모니터, ops·admin 전용)."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        op = _tok(c, "operator1", "pw")
        ap = _tok(c, "applicant1", "pw")
        aud = _tok(c, "auditor1", "pw")
        assert c.get("/admin/workflow-monitor", headers=_h(adm)).status_code == 200
        assert c.get("/admin/workflow-monitor", headers=_h(op)).status_code == 200
        assert c.get("/admin/workflow-monitor", headers=_h(ap)).status_code == 403
        assert c.get("/admin/workflow-monitor", headers=_h(aud)).status_code == 403


def test_workflow_monitor_shape_and_fold():
    """집약 스키마: cases/pipeline/gates_summary/totals. 케이스 생성→모니터에 반영(fold)."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid = c.post("/cases", json={"company_name": "WFMon Co", "org_id": "org_demo"},
                     headers=_h(adm)).json()["case_id"]
        d = c.get("/admin/workflow-monitor", headers=_h(adm)).json()
        for k in ("cases", "pipeline", "gates_summary", "totals"):
            assert k in d, (k, d.keys())
        row = next((r for r in d["cases"] if r["case_id"] == cid), None)
        assert row is not None, "생성 케이스가 모니터에 나타나야 함"
        # 필수 필드(핸드오프·게이트·다음액션)
        for f in ("status", "status_label", "phase_key", "phase_label", "owner",
                  "next_state", "blockers", "blocker_count", "gates"):
            assert f in row, (f, row)
        assert set(row["gates"]) == {"payment", "ai_report", "fatwa"}, row["gates"]
        assert row["owner"] in ("client", "consultant", "auditor", "sharia", "ops", "admin")
        # 파이프라인 집계 합 == totals.cases (모든 케이스가 한 단계에 귀속)
        psum = sum(p["count"] for p in d["pipeline"])
        assert psum == d["totals"]["cases"], (psum, d["totals"])


def test_workflow_monitor_gate_wait_on_fatwa_stage():
    """게이트 판단 fold: fatwa_review 단계면 파트와 게이트가 'wait'로 잡혀야 함(read-only 추론)."""
    import sqlite3
    dbfile = app_db_file()   # 환경변수가 아니라 앱 엔진이 단일 출처(conftest 주석 참조)
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid = c.post("/cases", json={"company_name": "GateCo", "org_id": "org_demo"},
                     headers=_h(adm)).json()["case_id"]
        conn = sqlite3.connect(dbfile)
        conn.execute("UPDATE case_application SET status='fatwa_review' WHERE case_id=?", (cid,))
        conn.commit()
        conn.close()
        d = c.get("/admin/workflow-monitor", headers=_h(adm)).json()
        row = next(r for r in d["cases"] if r["case_id"] == cid)
        assert row["gates"]["fatwa"] == "wait", row["gates"]
        assert row["owner"] == "sharia", row["owner"]   # 파트와 단계 소유자=샤리아


def test_workflow_monitor_no_write_side_effect():
    """읽기전용: 호출 전후 케이스 상태·건수 불변(집약 조회만)."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid = c.post("/cases", json={"company_name": "RO Co", "org_id": "org_demo"},
                     headers=_h(adm)).json()["case_id"]
        before = c.get(f"/cases/{cid}", headers=_h(adm)).json()["status"]
        c.get("/admin/workflow-monitor", headers=_h(adm))
        c.get("/admin/workflow-monitor", headers=_h(adm))
        after = c.get(f"/cases/{cid}", headers=_h(adm)).json()["status"]
        assert before == after, (before, after)


if __name__ == "__main__":
    tests = [test_workflow_monitor_rbac,
             test_workflow_monitor_shape_and_fold,
             test_workflow_monitor_gate_wait_on_fatwa_stage,
             test_workflow_monitor_no_write_side_effect]
    ok = 0
    for fn in tests:
        try:
            fn(); ok += 1; print("[PASS]", fn.__name__)
        except AssertionError as e:
            print("[FAIL]", fn.__name__, "—", e)
        except Exception as e:  # noqa
            import traceback; traceback.print_exc()
            print("[ERROR]", fn.__name__, "—", type(e).__name__, e)
    print(f"\n{ok}/{len(tests)} passed")
    sys.exit(0 if ok == len(tests) else 1)
