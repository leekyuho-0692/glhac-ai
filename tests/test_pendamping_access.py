"""배정된 동반자(Pendamping PPH)만 그 자기선언 케이스를 연다 — 업체별 조직 분리(10/04) 이후 회귀 방지.

케이스는 업체 조직에 있고 동반자는 org_demo 에 있어, '자기 조직만' 규칙으로는 /pendamping/assign 으로
배정해도 /pendamping/verify 가 403 이었다(e2e.py·e2e_s5 가 CI 에서 잡음).
"""
import os

os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_RATE_LIMIT_PER_MIN", "0")

from fastapi.testclient import TestClient

from app.main import app


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _uid(c, admin, username):
    return next(u["user_id"] for u in c.get("/admin/users", headers=admin).json() if u["username"] == username)


def test_배정된_동반자는_업체조직_케이스를_연다_배정안된_케이스는_403():
    with TestClient(app) as c:
        cons, admin, pend = _tok(c, "consultant1", "pw"), _tok(c, "admin", "admin"), _tok(c, "pendamping1", "pw")
        assigned = c.post("/cases", headers=cons, json={"company_name": "PendAccessCo", "is_msme": True}).json()["case_id"]
        other = c.post("/cases", headers=cons, json={"company_name": "PendOtherCo", "is_msme": True}).json()["case_id"]
        # 컨설턴트가 만든 케이스는 업체 조직 — 동반자(org_demo)와 다른 조직이어야 이 테스트가 의미 있다
        assert c.get(f"/cases/{assigned}", headers=cons).json()["org_id"] != "org_demo"

        # 배정 전: 다른 조직 케이스라 403
        assert c.get(f"/cases/{assigned}", headers=pend).status_code == 403
        r = c.post(f"/cases/{assigned}/pendamping/assign", headers=cons,
                   json={"pendamping_id": _uid(c, admin, "pendamping1")})
        assert r.status_code == 200, r.text
        # 배정 후: 그 케이스만 열린다
        assert c.get(f"/cases/{assigned}", headers=pend).status_code == 200
        assert c.get(f"/cases/{other}", headers=pend).status_code == 403
