"""P3 정리 배치 격리 테스트 — E7 현장심사 종합 의견 서버 영속.

E7: onsite-opinion POST(WorkflowEvent onsite.opinion, latest-wins) → GET 반영,
    AI 보고서(gen_audit_report)에 '오디터 종합 의견' 섹션 포함, 비권한(applicant) 저장 403.

주의(테스트 격리): tests 일괄 실행 시 공유 DB 상호오염 방지를 위해 app import 전에
고유 DB(glhac_p3_test.db)를 강제 대입한다(setdefault 금지).
실행: <venv>/bin/python -m pytest tests/test_p3_cleanup.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_p3_test.db"   # 격리 강제
os.environ["GLHAC_DEV"] = "1"                                  # 데모 계정 시드

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(tok):
    return {"Authorization": "Bearer " + tok}


def _mk_case(c, admin):
    r = c.post("/cases", json={"org_id": "org_demo", "company_name": "P3 Cleanup Co"},
               headers=_h(admin))
    assert r.status_code == 200, r.text
    return r.json()["case_id"]


def test_a_onsite_opinion_persist_get_and_latest_wins():
    """(a) 현장의견 POST 저장 → GET 반영 · latest-wins(마지막 저장이 이김)."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        aud = _tok(c, "auditor1", "pw")
        cid = _mk_case(c, adm)
        # 초기: 빈 문자열
        assert c.get(f"/cases/{cid}/onsite-opinion", headers=_h(aud)).json()["opinion"] == ""
        # 오디터 저장
        r1 = c.post(f"/cases/{cid}/onsite-opinion",
                    json={"opinion": "1차 종합 의견 — 대체로 양호."}, headers=_h(aud))
        assert r1.status_code == 200, r1.text
        assert c.get(f"/cases/{cid}/onsite-opinion",
                     headers=_h(aud)).json()["opinion"] == "1차 종합 의견 — 대체로 양호."
        # 재저장 → latest-wins
        c.post(f"/cases/{cid}/onsite-opinion",
               json={"opinion": "2차 최종 의견 — 시정 후 적합."}, headers=_h(aud))
        assert c.get(f"/cases/{cid}/onsite-opinion",
                     headers=_h(adm)).json()["opinion"] == "2차 최종 의견 — 시정 후 적합."


def test_b_audit_report_includes_opinion():
    """(b) AI 현장심사 보고서에 오디터 종합 의견 섹션 포함."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        aud = _tok(c, "auditor1", "pw")
        cid = _mk_case(c, adm)
        # 의견 없을 때: 섹션 없음
        rep0 = c.post(f"/cases/{cid}/audit-report", headers=_h(aud)).json()["report"]
        assert "오디터 종합 의견" not in rep0, rep0
        # 의견 저장 후: 섹션 + 본문 포함
        c.post(f"/cases/{cid}/onsite-opinion",
               json={"opinion": "현장 위생 우수, 원료 트레이스 확인됨."}, headers=_h(aud))
        rep1 = c.post(f"/cases/{cid}/audit-report", headers=_h(aud)).json()["report"]
        assert "오디터 종합 의견" in rep1, rep1
        assert "현장 위생 우수, 원료 트레이스 확인됨." in rep1, rep1


def test_c_opinion_rbac_forbidden_for_applicant():
    """(c) 비권한(applicant)의 의견 저장 403 — 역할 게이트가 케이스 조회보다 먼저."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        ap = _tok(c, "applicant1", "pw")
        cid = _mk_case(c, adm)
        assert c.post(f"/cases/{cid}/onsite-opinion",
                      json={"opinion": "무단 저장 시도"}, headers=_h(ap)).status_code == 403


if __name__ == "__main__":
    tests = [test_a_onsite_opinion_persist_get_and_latest_wins,
             test_b_audit_report_includes_opinion,
             test_c_opinion_rbac_forbidden_for_applicant]
    ok = 0
    for fn in tests:
        try:
            fn(); ok += 1; print("[PASS]", fn.__name__)
        except Exception as e:  # noqa
            import traceback
            traceback.print_exc()
            print("[FAIL]", fn.__name__, "—", e)
    print(f"\n{ok}/{len(tests)} passed")
    sys.exit(0 if ok == len(tests) else 1)
