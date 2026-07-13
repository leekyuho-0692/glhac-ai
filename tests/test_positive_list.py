"""SEHATI self-declare 긍정목록(known-halal) 게이트 — assess_pathway 정밀화 검증.
TestClient 기반(서버 불필요). 실행: <venv>/bin/python -m pytest tests/test_positive_list.py -q

격리: 이 프로젝트는 tests 일괄 실행 시 여러 파일이 공용 DB를 나눠 써 상호 오염되므로,
이 파일은 고유 DB(glhac_poslist_test.db)를 app import 전에 강제 대입해 격리한다.

게이트 정의: positive_listed(m) = matched_uid is not None AND screen_status == "halal".
미매칭(unmatched) 또는 비-halal(mushbooh/unknown/haram)은 긍정목록 미확인 → self-declare 부적격.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_poslist_test.db"   # setdefault 금지 — 격리 강제
os.environ["GLHAC_DEV"] = "1"   # 데모 계정 시드(테스트 전용)

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402


def _tok(c, u="admin", p="admin"):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _mkcase(c, h):
    return c.post("/cases", json={"org_id": "org_demo", "company_name": "PosListCo",
                                  "is_msme": True}, headers=h).json()["case_id"]


def _add(c, h, cid, name, **extra):
    r = c.post(f"/cases/{cid}/materials", json=dict(name=name, **extra), headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def _assess(c, h, cid):
    r = c.post(f"/cases/{cid}/pathway/assess", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_all_halal_matched_is_self_declare():
    """(a) 전 원재료 halal 매칭 → self_declare 제안 · non_positive_count=0 · NOT_ON_POSITIVE_LIST 없음."""
    with TestClient(app) as c:
        h = _tok(c)
        cid = _mkcase(c, h)
        _add(c, h, cid, "citric acid", e_number="E330")   # 온톨로지 halal 매칭
        a = _assess(c, h, cid)
        assert a["suggested_pathway"] == "self_declare", a
        assert a["non_positive_count"] == 0, a
        assert a["positive_listed_count"] == 1, a
        assert all(m["positive_listed"] for m in a["materials"]), a
        assert not any(b["code"] == "NOT_ON_POSITIVE_LIST" for b in a["blockers"]), a


def test_unmatched_material_blocks_self_declare():
    """(b) 미매칭 원재료 1건 추가 → self_declare 아님(reguler) · NOT_ON_POSITIVE_LIST(reason=unmatched)."""
    with TestClient(app) as c:
        h = _tok(c)
        cid = _mkcase(c, h)
        _add(c, h, cid, "citric acid", e_number="E330")            # halal
        _add(c, h, cid, "Zzyxx Mystery Powder 9981")               # 온톨로지 미매칭
        a = _assess(c, h, cid)
        assert a["suggested_pathway"] == "reguler", a
        assert a["non_positive_count"] == 1, a
        npb = [b for b in a["blockers"] if b["code"] == "NOT_ON_POSITIVE_LIST"]
        assert len(npb) == 1 and npb[0]["reason"] == "unmatched", a
        # 미매칭 원재료가 materials 목록에 unmatched로 표기되는지
        um = [m for m in a["materials"] if not m["positive_listed"]]
        assert len(um) == 1 and um[0]["reason"] == "unmatched", a


def test_mushbooh_material_is_non_positive():
    """(c) mushbooh(매칭됨·비-halal) 원재료 → non_positive(reason=not_halal) · self_declare 아님."""
    with TestClient(app) as c:
        h = _tok(c)
        cid = _mkcase(c, h)
        _add(c, h, cid, "citric acid", e_number="E330")            # halal
        _add(c, h, cid, "beeswax")                                 # 온톨로지 매칭 · mushbooh(low)
        a = _assess(c, h, cid)
        assert a["suggested_pathway"] == "reguler", a
        assert a["non_positive_count"] >= 1, a
        npb = [b for b in a["blockers"] if b["code"] == "NOT_ON_POSITIVE_LIST"]
        assert any(b["reason"] == "not_halal" for b in npb), a
        nh = [m for m in a["materials"] if m["reason"] == "not_halal"]
        assert len(nh) >= 1, a


if __name__ == "__main__":
    test_all_halal_matched_is_self_declare()
    test_unmatched_material_blocks_self_declare()
    test_mushbooh_material_is_non_positive()
    print("OK")
