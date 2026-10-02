"""P2(UI개편 기반) — step8·hold·/cases 필드확장·nav-counts(N2)·todo(N3)·동반오디터(N6).

실행: <venv>/bin/python -m pytest tests/test_p2_nav.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_p2nav_test.db")

from fastapi.testclient import TestClient   # noqa: E402

from app.main import app                    # noqa: E402


def _tok(c, u="applicant1", p="pw"):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _case(c, h, name="PT Nav Uji"):
    r = c.post("/cases", json={"company_name": name}, headers=h)
    assert r.status_code in (200, 201), r.text
    return r.json()["case_id"]


def test_cases_목록에_step8_필드가_추가된다():
    with TestClient(app) as c:
        h = _tok(c)
        cid = _case(c, h)
        it = next(x for x in c.get("/cases", headers=h).json()["items"] if x["case_id"] == cid)
        for k in ["step8", "step8_label", "done", "hold", "owner",
                  "consultant", "main_auditor", "co_auditors", "next_action"]:
            assert k in it, k
        # 신규 케이스 = 접수(step 0), 담당 client, 미완료
        assert it["step8"] == 0 and it["step8_label"] == "접수"
        assert it["owner"] == "client" and it["done"] is False
        assert it["co_auditors"] == []


def test_journey_에_step8_블록이_붙는다():
    with TestClient(app) as c:
        h = _tok(c)
        cid = _case(c, h)
        j = c.get(f"/cases/{cid}/journey", headers=h).json()
        assert "step8" in j and j["step8"]["step"] == 0
        assert j["step8"]["labels"][0] == "접수" and len(j["step8"]["labels"]) == 8
        # 기존 11단계도 유지(하위호환)
        assert len(j["stages"]) == 11


def test_me_nav_counts_역할별_집계():
    with TestClient(app) as c:
        h = _tok(c)                       # applicant
        _case(c, h)
        d = c.get("/me/nav-counts", headers=h).json()
        for k in ["action_required", "hold", "in_progress"]:
            assert k in d
        assert d["in_progress"] >= 1 and d["action_required"] >= 1   # 접수=client 담당
        # 관리자: 스태프 가입 대기 키
        ha = _tok(c, "admin", "admin")
        da = c.get("/me/nav-counts", headers=ha).json()
        assert "staff_signup_pending" in da
        # 오디터: 배정 대기 키
        hau = _tok(c, "auditor1", "pw")
        dau = c.get("/me/nav-counts", headers=hau).json()
        assert "assignment_pending" in dau


def test_me_todo_담당_케이스를_돌려준다():
    with TestClient(app) as c:
        h = _tok(c)
        cid = _case(c, h)
        d = c.get("/me/todo", headers=h).json()
        assert d["count"] >= 1
        row = next(x for x in d["items"] if x["case_id"] == cid)
        assert row["mine"] is True and row["owner"] == "client"


def test_동반_오디터_추가조회해제_N6():
    with TestClient(app) as c:
        h = _tok(c)
        cid = _case(c, h)
        ha = _tok(c, "admin", "admin")
        # 추가 (username)
        r = c.post(f"/cases/{cid}/auditors", json={"username": "auditor2"}, headers=ha)
        assert r.status_code == 200, r.text
        # 조회
        au = c.get(f"/cases/{cid}/auditors", headers=ha).json()
        assert any(x["name"] == "auditor2" for x in au["co"])
        co_uid = next(x["user_id"] for x in au["co"] if x["name"] == "auditor2")
        # /cases 항목에도 반영
        it = next(x for x in c.get("/cases", headers=ha).json()["items"] if x["case_id"] == cid)
        assert co_uid in it["co_auditors"]
        # 동반 오디터 본인도 케이스가 보인다(가시성)
        h2 = _tok(c, "auditor2", "pw")
        assert any(x["case_id"] == cid for x in c.get("/cases", headers=h2).json()["items"])
        # 비오디터 추가 거부
        r2 = c.post(f"/cases/{cid}/auditors", json={"username": "consultant1"}, headers=ha)
        assert r2.status_code == 422
        # 해제
        r3 = c.request("DELETE", f"/cases/{cid}/auditors/{co_uid}", headers=ha)
        assert r3.status_code == 200, r3.text
        au2 = c.get(f"/cases/{cid}/auditors", headers=ha).json()
        assert all(x["user_id"] != co_uid for x in au2["co"])
