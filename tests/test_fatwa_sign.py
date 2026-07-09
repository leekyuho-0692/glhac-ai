"""파트와 위원 전자서명 신규 라우트 격리 테스트 — 고유 DB(setdefault 금지, 직접 대입으로 격리 강제).
실행: <venv>/bin/python tests/test_fatwa_sign.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_fatwasign_test.db"   # setdefault 금지 — 격리 강제
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
    return c.post("/cases", json={"company_name": "Fatwa Iso Co", "org_id": "org_demo"},
                  headers=_h(tok)).json()["case_id"]


def test_sign_roundtrip_latest_wins():
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid = _mkcase(c, adm)
        assert c.get(f"/cases/{cid}/fatwa/sign", headers=_h(adm)).json() == {"signatures": {}}
        assert c.post(f"/cases/{cid}/fatwa/sign", json={"member": "chairman", "image": SIG, "name": "Dr. Ahmad"},
                      headers=_h(adm)).status_code == 200
        c.post(f"/cases/{cid}/fatwa/sign", json={"member": "chairman", "image": SIG, "name": "Dr. Ahmad Hidayat"}, headers=_h(adm))
        c.post(f"/cases/{cid}/fatwa/sign", json={"member": "member1", "image": SIG, "name": "Ust. Farhan"}, headers=_h(adm))
        got = c.get(f"/cases/{cid}/fatwa/sign", headers=_h(adm)).json()["signatures"]
        assert got["chairman"]["name"] == "Dr. Ahmad Hidayat", got   # latest-wins per member
        assert got["member1"]["name"] == "Ust. Farhan", got
        assert got["chairman"]["image"].startswith("data:image/"), got
        assert got["chairman"]["at"], got                            # 시각 메타 노출


def test_sign_validation():
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid = _mkcase(c, adm)
        assert c.post(f"/cases/{cid}/fatwa/sign", json={"member": "", "image": SIG},
                      headers=_h(adm)).status_code == 422
        assert c.post(f"/cases/{cid}/fatwa/sign", json={"member": "chairman", "image": "not-a-dataurl"},
                      headers=_h(adm)).status_code == 422
        assert c.post(f"/cases/{cid}/fatwa/sign", json={"member": "chairman", "image": "data:image/png;base64," + "A" * 400001},
                      headers=_h(adm)).status_code == 413


def test_sign_org_isolation():
    """존재하지 않는/타 조직 케이스 서명 조회 차단(_get_case 격리)."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        assert c.get("/cases/nonexistent-case-id/fatwa/sign", headers=_h(adm)).status_code == 404


if __name__ == "__main__":
    tests = [test_sign_roundtrip_latest_wins, test_sign_validation, test_sign_org_isolation]
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
