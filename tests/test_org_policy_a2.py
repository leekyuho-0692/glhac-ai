"""A-2 Penyelia 자격 정책 — 무슬림·임명장(SK)·교육증명 필드 + 오디터 '높음' 플래그.

- penyelia_quals PUT 저장/GET 반영, members와 부분 업데이트(상호 미소실)
- reconcile: 자격 미비 → penyelia_unqualified(높음), 충족 → 플래그 없음

실행: <venv>/bin/pytest tests/test_org_policy_a2.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_org_a2_test.db")
os.environ.setdefault("GLHAC_DEV", "1")

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

_PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgYGAA"
        "AAAEAAH2FzhVAAAAAElFTkSuQmCC")


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _seed(c, h, company):
    cid = c.post("/cases", json={"company_name": company}, headers=h).json()["case_id"]
    c.patch(f"/cases/{cid}/profile", json={"responsible_person": "홍길동",
            "halal_supervisor": "김철수"}, headers=h)
    return cid


def _peny(c, h, cid, name):
    o = c.get(f"/cases/{cid}/halal-org", headers=h).json()
    return next((p for p in o["penyelia"] if p["name"] == name), None)


def test_penyelia_quals_default_and_save():
    with TestClient(app) as c:
        h = _tok(c, "consultant1", "pw")
        cid = _seed(c, h, "PT Qual A")
        p = _peny(c, h, cid, "김철수")
        assert p and p["is_muslim"] is False and p["sk"] is False and p["training_cert"] is False
        # 자격 저장
        c.put(f"/cases/{cid}/halal-org",
              json={"penyelia_quals": {"김철수": {"is_muslim": True, "sk": True, "training_cert": True}}}, headers=h)
        p2 = _peny(c, h, cid, "김철수")
        assert p2["is_muslim"] and p2["sk"] and p2["training_cert"]


def test_partial_update_does_not_wipe_members():
    with TestClient(app) as c:
        h = _tok(c, "consultant1", "pw")
        cid = _seed(c, h, "PT Qual B")
        # 멤버 저장
        c.put(f"/cases/{cid}/halal-org", json={"members": [{"name": "정하늘", "role": "qc"}]}, headers=h)
        # 자격만 저장(멤버 미포함) → 멤버 유지되어야
        c.put(f"/cases/{cid}/halal-org", json={"penyelia_quals": {"김철수": {"is_muslim": True}}}, headers=h)
        o = c.get(f"/cases/{cid}/halal-org", headers=h).json()
        assert any(m["name"] == "정하늘" and m["source"] == "manual" for m in o["members"]), "멤버 소실됨"
        assert _peny(c, h, cid, "김철수")["is_muslim"] is True


def test_reconcile_flags_unqualified_penyelia():
    with TestClient(app) as c:
        h = _tok(c, "consultant1", "pw")
        cid = _seed(c, h, "PT Qual C")
        d = c.post(f"/cases/{cid}/documents",
                   json={"filename": "org.png", "file_b64": _PNG, "doc_type": "manual_section"}, headers=h).json()
        c.post(f"/cases/{cid}/sjph-manual/layout",
               json={"inserts": {"org_chart": {"document_id": d["document_id"], "filename": "org.png"}}}, headers=h)
        # 자격 미비 상태 → 김철수 penyelia_unqualified(높음) 플래그
        j = c.post(f"/cases/{cid}/halal-org/reconcile", json={}, headers=h).json()
        unq = [m for m in j["mismatches"] if m["type"] == "penyelia_unqualified" and m["name"] == "김철수"]
        assert unq and unq[0]["severity"] == "high", j["mismatches"]
        # 자격 충족 → 김철수 플래그 사라짐
        c.put(f"/cases/{cid}/halal-org",
              json={"penyelia_quals": {"김철수": {"is_muslim": True, "sk": True, "training_cert": True}}}, headers=h)
        j2 = c.post(f"/cases/{cid}/halal-org/reconcile", json={}, headers=h).json()
        assert not [m for m in j2["mismatches"] if m["type"] == "penyelia_unqualified" and m["name"] == "김철수"]


if __name__ == "__main__":
    test_penyelia_quals_default_and_save()
    test_partial_update_does_not_wipe_members()
    test_reconcile_flags_unqualified_penyelia()
    print("✅ A-2 Penyelia 자격 테스트 통과")
