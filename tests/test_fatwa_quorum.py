"""파트와 정족수 하드게이트 격리 테스트 — 고유 DB(직접 대입으로 격리 강제).
결정문 생성(POST /cases/{id}/fatwa/decree) 진입 시 위원장 서명 + 서명 위원 총 ≥ 2 강제.
실행: <venv>/bin/python tests/test_fatwa_quorum.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_fatwaquorum_test.db"   # setdefault 금지 — 격리 강제
os.environ.setdefault("GLHAC_DEV", "1")

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

SIG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
       "AAAAC0lEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC")


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(t):
    return {"Authorization": "Bearer " + t}


def _mkcase(c, tok):
    return c.post("/cases", json={"company_name": "Quorum Iso Co", "org_id": "org_demo"},
                  headers=_h(tok)).json()["case_id"]


def _sign(c, tok, cid, member):
    return c.post(f"/cases/{cid}/fatwa/sign",
                  json={"member": member, "image": SIG, "name": member.title()},
                  headers=_h(tok))


def test_no_chairman_blocks():
    """위원장 미서명(위원 1인만) → 409 FATWA_QUORUM_NOT_MET (NO_CHAIRMAN 포함)."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid = _mkcase(c, adm)
        _sign(c, adm, cid, "member1")
        r = c.post(f"/cases/{cid}/fatwa/decree", json={}, headers=_h(adm))
        assert r.status_code == 409, r.text
        d = r.json()["detail"]
        assert d["code"] == "FATWA_QUORUM_NOT_MET", d
        assert "NO_CHAIRMAN" in d["missing"], d


def test_chairman_only_blocks():
    """위원장 1인만 서명(총 1인) → 409 (NEED_2_MEMBERS 포함)."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid = _mkcase(c, adm)
        _sign(c, adm, cid, "chairman")
        r = c.post(f"/cases/{cid}/fatwa/decree", json={}, headers=_h(adm))
        assert r.status_code == 409, r.text
        d = r.json()["detail"]
        assert d["code"] == "FATWA_QUORUM_NOT_MET", d
        assert "NEED_2_MEMBERS" in d["missing"], d


def test_chairman_plus_two_passes():
    """위원장 + 위원 2인(총 3인) → 결정문 생성 통과(200)."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid = _mkcase(c, adm)
        _sign(c, adm, cid, "chairman")
        _sign(c, adm, cid, "member1")
        _sign(c, adm, cid, "member2")
        r = c.post(f"/cases/{cid}/fatwa/decree", json={}, headers=_h(adm))
        assert r.status_code == 200, r.text
        assert r.json().get("gen_doc_id"), r.text


def test_chairman_plus_one_min_quorum():
    """최소 정족수: 위원장 + 위원 1인(총 2인) → 통과(200)."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid = _mkcase(c, adm)
        _sign(c, adm, cid, "chairman")
        _sign(c, adm, cid, "member1")
        r = c.post(f"/cases/{cid}/fatwa/decree", json={}, headers=_h(adm))
        assert r.status_code == 200, r.text


if __name__ == "__main__":
    fns = [test_no_chairman_blocks, test_chairman_only_blocks,
           test_chairman_plus_two_passes, test_chairman_plus_one_min_quorum]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print("  ok:", fn.__name__)
    print("PASS %d/%d" % (passed, len(fns)))
