"""P1 배치 B 격리 테스트 — #3 사전심사 5단계 스텝바(프런트) · #4 자료함(Vault) · #5 종합 캘린더 · #6 OCR 자동배치.

배치 B의 백엔드 순수 신규는 #5 GET /ops/calendar 뿐(#3·#4는 프런트+기존 라우트, #6은 layout caption 확장).
따라서 여기서는 (a)종합캘린더 operator 200·전 케이스 일정 집약 (b)자료함 집약(기존 라우트 조합 스모크)
(c)비권한 종합캘린더 403 (d)#6 layout caption 라운드트립 을 검증한다.

주의(테스트 격리): tests 일괄 실행 시 공유 DB 상호오염 방지를 위해 app import 전에
고유 DB(glhac_p1b_test.db)를 강제 대입한다(setdefault 금지).
실행: <venv>/bin/python -m pytest tests/test_p1_batch_b.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_p1b_test.db"   # 격리 강제
os.environ["GLHAC_DEV"] = "1"                                   # 데모 계정 시드

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(tok):
    return {"Authorization": "Bearer " + tok}


def _mkcase(c, admin, name="P1BatchBCo"):
    return c.post("/cases", json={"org_id": "org_demo", "company_name": name},
                  headers=_h(admin)).json()["case_id"]


def test_a_ops_calendar_operator_aggregates_all_cases():
    """(a) 종합 캘린더 operator 200 + 전 케이스 현장실사 일정 집약(AuditPlan + onsite confirm)."""
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        op = _tok(c, "operator1", "pw")
        cid1 = _mkcase(c, admin, "AlphaFoods")
        cid2 = _mkcase(c, admin, "BetaFoods")
        # 케이스1: AuditPlan(LPH 예정일)
        r1 = c.post("/cases/%s/audit-plan" % cid1,
                    json={"lph_name": "LPPOM MUI", "scheduled_date": "2026-08-12"}, headers=_h(op))
        assert r1.status_code == 200, r1.text
        # 케이스2: onsite_schedule.confirm(오디터/운영자 확정일)
        r2 = c.post("/cases/%s/onsite-schedule/confirm" % cid2,
                    json={"date": "2026-08-20", "time": "10:00"}, headers=_h(op))
        assert r2.status_code == 200, r2.text
        # 종합 캘린더 집약
        cal = c.get("/ops/calendar", headers=_h(op))
        assert cal.status_code == 200, cal.text
        body = cal.json()
        dates = {e["date"]: e for e in body["events"]}
        assert "2026-08-12" in dates and "2026-08-20" in dates, body
        assert dates["2026-08-12"]["company_name"] == "AlphaFoods"
        assert dates["2026-08-12"]["source"] == "audit_plan"
        assert dates["2026-08-20"]["company_name"] == "BetaFoods"
        assert dates["2026-08-20"]["source"] == "onsite_schedule"
        assert dates["2026-08-20"]["status"] == "confirmed"
        assert dates["2026-08-20"]["time"] == "10:00"
        # by_date 그룹 + 집계 카운트
        assert body["count"] >= 2 and body["case_count"] >= 2
        assert body["by_date"]["2026-08-12"][0]["case_id"] == cid1
        # admin 도 통과(require_roles admin bypass)
        assert c.get("/ops/calendar", headers=_h(admin)).status_code == 200


def test_b_vault_aggregation_existing_routes_smoke():
    """(b) 자료함 집약 — 신규 라우트 없이 기존 4개 조회 라우트 조합 200 스모크."""
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        con = _tok(c, "consultant1", "pw")
        cid = _mkcase(c, admin, "VaultCo")
        for path in ("documents", "gen-docs", "materials", "certificate"):
            r = c.get("/cases/%s/%s" % (cid, path), headers=_h(con))
            assert r.status_code == 200, (path, r.text)
        # 미발급 인증서는 issued=False 로 정상 응답(에러 아님)
        assert c.get("/cases/%s/certificate" % cid, headers=_h(con)).json().get("issued") is False


def test_c_ops_calendar_forbidden_for_applicant():
    """(c) 비권한(applicant) 종합 캘린더 → 403."""
    with TestClient(app) as c:
        ap = _tok(c, "applicant1", "pw")
        r = c.get("/ops/calendar", headers=_h(ap))
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["code"] == "NOT_AUTHORIZED", r.text


def test_d_manual_layout_ocr_caption_roundtrip():
    """(d) #6 OCR 자동배치 — layout inserts.caption 라운드트립(스키마 무변경 payload 보존)."""
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        con = _tok(c, "consultant1", "pw")
        cid = _mkcase(c, admin, "ManualCo")
        payload = {"order": [], "inserts": {
            "org_chart": {"document_id": "docORG", "filename": "org.png",
                          "caption": "조직도 · 할랄 담당자 구조"}}}
        r = c.post("/cases/%s/sjph-manual/layout" % cid, json=payload, headers=_h(con))
        assert r.status_code == 200, r.text
        org = [s for s in r.json()["sections"] if s["key"] == "org_chart"][0]
        assert org["image"]["document_id"] == "docORG"
        assert org["image"]["caption"] == "조직도 · 할랄 담당자 구조", org
        # 재조회에도 보존
        g = c.get("/cases/%s/sjph-manual/layout" % cid, headers=_h(con)).json()
        org2 = [s for s in g["sections"] if s["key"] == "org_chart"][0]
        assert org2["image"]["caption"] == "조직도 · 할랄 담당자 구조", org2


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
