"""A09: 보완·재전송 센터(집약 조회) — 격리 DB 스모크.
스키마 무변경(기존 WorkflowEvent·finding·CAR 조회만) 검증.
실행: <venv>/bin/python tests/test_resubmit_center.py
8800 금지 · 고유 격리 DB명 · os.environ 직접대입.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_v3_resubmit_test.db"
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
    return c.post("/cases", json={"company_name": "ResubCo", "org_id": "org_demo"},
                  headers=_h(adm)).json()["case_id"]


def _center(c, cid, tok):
    r = c.get(f"/cases/{cid}/resubmit-center", headers=_h(tok))
    assert r.status_code == 200, r.text
    return r.json()


def test_empty_case_has_no_requests():
    """요청이 하나도 없으면 빈 리스트 + counts 전부 0."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        ap = _tok(c, "applicant1", "pw")
        cid = _mkcase(c, adm)
        j = _center(c, cid, ap)
        assert j["requests"] == [], j
        assert j["counts"] == {"pending": 0, "resubmitted": 0, "resolved": 0, "total": 0}, j


def test_application_return_appears():
    """신청서 반려 → application 출처 요청 pending 노출."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cons = _tok(c, "consultant1", "pw")
        ap = _tok(c, "applicant1", "pw")
        cid = _mkcase(c, adm)
        r = c.post(f"/cases/{cid}/return-application",
                   json={"reason": "사업자등록증 재첨부 필요"}, headers=_h(cons))
        assert r.status_code == 200, r.text
        j = _center(c, cid, ap)
        appreq = [x for x in j["requests"] if x["source"] == "application"]
        assert len(appreq) == 1, j
        assert appreq[0]["status"] == "pending"
        assert "재첨부" in appreq[0]["reason"]
        assert j["counts"]["pending"] >= 1


def test_preassess_loop_pending_then_resubmitted():
    """사전심사 추가서류 요청 → pending, 클라이언트 재제출 → resubmitted."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        aud = _tok(c, "auditor1", "pw")
        ap = _tok(c, "applicant1", "pw")
        cid = _mkcase(c, adm)
        r = c.post(f"/cases/{cid}/preassess/doc-request",
                   json={"items": [{"doc_type": "rph_cert", "doc_type_ko": "할랄 도축 증명서"}],
                         "message": "젤라틴 도축증명 필요"}, headers=_h(aud))
        assert r.status_code == 200, r.text
        j = _center(c, cid, ap)
        pre = [x for x in j["requests"] if x["source"] == "preassess"]
        assert len(pre) == 1 and pre[0]["status"] == "pending", j
        assert pre[0]["round"] == 1 and "할랄 도축 증명서" in pre[0]["items"]
        # 클라이언트 재제출 → resubmitted
        r2 = c.post(f"/cases/{cid}/preassess/resubmit", json={"note": "도축증명 첨부"}, headers=_h(ap))
        assert r2.status_code == 200, r2.text
        pre2 = [x for x in _center(c, cid, ap)["requests"] if x["source"] == "preassess"][0]
        assert pre2["status"] == "resubmitted", pre2


def test_audit_report_loop_states():
    """현장보고서 보완 반려 → pending → 재제출 → resubmitted → 수정확인 ok → resolved."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        aud = _tok(c, "auditor1", "pw")
        ap = _tok(c, "applicant1", "pw")
        cid = _mkcase(c, adm)
        assert c.post(f"/cases/{cid}/audit-report/return",
                      json={"comment": "위생 사진 보완"}, headers=_h(aud)).status_code == 200
        ar = [x for x in _center(c, cid, ap)["requests"] if x["source"] == "audit_report"][0]
        assert ar["status"] == "pending" and "위생" in ar["reason"], ar
        assert c.post(f"/cases/{cid}/audit-report/resubmit",
                      json={"note": "재촬영 첨부"}, headers=_h(ap)).status_code == 200
        ar2 = [x for x in _center(c, cid, ap)["requests"] if x["source"] == "audit_report"][0]
        assert ar2["status"] == "resubmitted", ar2
        assert c.post(f"/cases/{cid}/audit-report/reconfirm",
                      json={"decision": "ok"}, headers=_h(aud)).status_code == 200
        ar3 = [x for x in _center(c, cid, ap)["requests"] if x["source"] == "audit_report"][0]
        assert ar3["status"] == "resolved", ar3


def test_car_finding_pending_then_resubmitted():
    """현장 지적(finding) → car 출처 pending, 시정조치 제출 → resubmitted."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        aud = _tok(c, "auditor1", "pw")
        ap = _tok(c, "applicant1", "pw")
        cid = _mkcase(c, adm)
        fr = c.post(f"/cases/{cid}/findings",
                    json={"area": "hygiene", "finding": "세척 소독 기록 미비", "severity": "minor"},
                    headers=_h(aud))
        assert fr.status_code == 200, fr.text
        fid = fr.json()["finding_id"]
        car = [x for x in _center(c, cid, ap)["requests"] if x["source"] == "car"]
        assert len(car) == 1 and car[0]["status"] == "pending", car
        assert car[0]["finding_id"] == fid
        assert c.post(f"/findings/{fid}/car",
                      json={"description": "세척 기록 양식 도입"}, headers=_h(ap)).status_code == 200
        car2 = [x for x in _center(c, cid, ap)["requests"] if x["source"] == "car"][0]
        assert car2["status"] == "resubmitted", car2


def test_org_isolation_enforced():
    """다른 org 사용자는 케이스 조회 불가(403/404)."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid = _mkcase(c, adm)
        # org_demo 소속이 아닌 신규 가입자
        reg = c.post("/auth/register", json={"username": "outsider_rc", "password": "halal-test-1",
                                             "company_name": "OtherCo"})
        assert reg.status_code == 200, reg.text
        other = reg.json()["token"]
        r = c.get(f"/cases/{cid}/resubmit-center", headers=_h(other))
        assert r.status_code in (403, 404), r.status_code


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        print("[PASS]", fn.__name__)
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")
