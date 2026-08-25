"""L3 정규화 할랄팀 조직 모델(halal_org) — 회귀테스트.

- GET /cases/{id}/halal-org: 기존 필드 파생(top·penyelia·PIC·CP) 정규화 반환
- PUT /cases/{id}/halal-org: 부서대표(members) 저장(profile_ext.halal_org, 마이그레이션 없음)
- 검증·불변성: 잘못된 role/division 보정, 기존 필드 불변, 추가멤버가 L2 대조 대상 포함

실행: <venv>/bin/pytest tests/test_org_halal_model_l3.py -q
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_org_l3_test.db")
os.environ.setdefault("GLHAC_DEV", "1")

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

_PNG_1x1 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgYGAA"
            "AAAEAAH2FzhVAAAAAElFTkSuQmCC")


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _seed(c, h):
    cid = c.post("/cases", json={"company_name": "PT Sinar Halal"}, headers=h).json()["case_id"]
    c.patch(f"/cases/{cid}/profile", json={"responsible_person": "홍길동", "halal_supervisor": "김철수",
            "profile_ext": {"pic_name": "이영희", "cp_name": "박민수"}}, headers=h)
    c.post("/orgs/org_demo/penyelia", json={"name": "최지훈"}, headers=h)
    return cid


def test_halal_org_get_derived():
    with TestClient(app) as c:
        h = _tok(c, "consultant1", "pw")
        cid = _seed(c, h)
        o = c.get(f"/cases/{cid}/halal-org", headers=h).json()
        assert o["top_mgmt"]["name"] == "홍길동"
        pen = {p["name"] for p in o["penyelia"]}
        assert {"김철수", "최지훈"} <= pen
        roles = {(m["name"], m["role"], m["source"]) for m in o["members"]}
        assert ("이영희", "coordinator", "form") in roles
        assert ("박민수", "liaison", "form") in roles


def test_halal_org_put_add_member_and_persist():
    with TestClient(app) as c:
        h = _tok(c, "consultant1", "pw")
        cid = _seed(c, h)
        r = c.put(f"/cases/{cid}/halal-org",
                  json={"members": [{"name": "홍부장", "title": "품질팀장", "role": "qc", "division": "qc"}]},
                  headers=h)
        assert r.status_code == 200, r.text
        o = c.get(f"/cases/{cid}/halal-org", headers=h).json()
        man = [m for m in o["members"] if m["source"] == "manual"]
        assert len(man) == 1 and man[0]["name"] == "홍부장" and man[0]["role"] == "qc" and man[0]["division"] == "qc"
        # 기존 필드 불변(대표·PIC 그대로)
        assert o["top_mgmt"]["name"] == "홍길동"
        assert any(m["name"] == "이영희" for m in o["members"])


def test_halal_org_put_validation_coerce():
    with TestClient(app) as c:
        h = _tok(c, "consultant1", "pw")
        cid = _seed(c, h)
        c.put(f"/cases/{cid}/halal-org",
              json={"members": [{"name": "미상", "role": "invalid_role", "division": "bogus"}]}, headers=h)
        o = c.get(f"/cases/{cid}/halal-org", headers=h).json()
        m = [x for x in o["members"] if x["source"] == "manual"][0]
        assert m["role"] == "member" and m["division"] == ""   # 잘못된 값 보정


def test_extra_member_included_in_reconcile():
    with TestClient(app) as c:
        h = _tok(c, "consultant1", "pw")
        cid = _seed(c, h)
        # 기준: 담당자 5명(대표·할랄감독자·penyelia·PIC·CP)
        d = c.post(f"/cases/{cid}/documents",
                   json={"filename": "org.png", "file_b64": _PNG_1x1, "doc_type": "manual_section"},
                   headers=h).json()
        c.post(f"/cases/{cid}/sjph-manual/layout",
               json={"inserts": {"org_chart": {"document_id": d["document_id"], "filename": "org.png"}}}, headers=h)
        # penyelia는 org 단위라 같은 프로세스의 앞선 테스트가 org_demo에 더 남겼을 수 있다.
        # 이 테스트의 주장은 '몇 명인가'가 아니라 '부서대표를 더하면 대조 대상도 하나 는다'이므로
        # 절대 수치가 아니라 증분으로 판정한다(전체 실행에서만 깨지던 원인).
        base = c.post(f"/cases/{cid}/halal-org/reconcile", json={}, headers=h).json()["summary"]["total"]
        assert base >= 5, "대표·할랄감독자·penyelia·PIC·CP 최소 5명이 대조 대상"
        c.put(f"/cases/{cid}/halal-org",
              json={"members": [{"name": "홍부장", "role": "qc"}]}, headers=h)
        after = c.post(f"/cases/{cid}/halal-org/reconcile", json={}, headers=h).json()["summary"]["total"]
        assert after == base + 1, "추가 부서대표가 L2 대조 대상에 포함되어야 함"


if __name__ == "__main__":
    test_halal_org_get_derived()
    test_halal_org_put_add_member_and_persist()
    test_halal_org_put_validation_coerce()
    test_extra_member_included_in_reconcile()
    print("✅ L3 정규화 조직모델 테스트 전부 통과")
