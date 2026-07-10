"""G1 격리 테스트 — POST /cases/{id}/fatwa/sign 역할가드(require_roles fatwa_liaison·operator).
GET(조회)은 케이스 소유 클라이언트도 200 유지. 고유 DB · 직접 대입으로 격리 강제.
실행: <venv>/bin/python -m pytest tests/test_fatwa_sign_guard.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_fsign_test.db"   # setdefault 금지 — 격리 강제
os.environ["GLHAC_DEV"] = "1"

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
    return c.post("/cases", json={"company_name": "Sign Guard Co", "org_id": "org_demo"},
                  headers=_h(tok)).json()["case_id"]


def test_fatwa_liaison_can_sign():
    """(a) fatwa_liaison POST sign 200."""
    with TestClient(app) as c:
        applicant = _tok(c, "applicant1", "pw")
        cid = _mkcase(c, applicant)
        fatwa = _tok(c, "fatwa1", "pw")
        r = c.post(f"/cases/{cid}/fatwa/sign",
                   json={"member": "chairman", "image": SIG, "name": "Dr. Ahmad"},
                   headers=_h(fatwa))
        assert r.status_code == 200, r.text


def test_operator_can_sign():
    """(a') operator POST sign 200."""
    with TestClient(app) as c:
        applicant = _tok(c, "applicant1", "pw")
        cid = _mkcase(c, applicant)
        op = _tok(c, "operator1", "pw")
        r = c.post(f"/cases/{cid}/fatwa/sign",
                   json={"member": "member1", "image": SIG, "name": "Ops"},
                   headers=_h(op))
        assert r.status_code == 200, r.text


def test_applicant_cannot_sign():
    """(b) applicant/client POST sign 403(역할가드)."""
    with TestClient(app) as c:
        applicant = _tok(c, "applicant1", "pw")
        cid = _mkcase(c, applicant)
        r = c.post(f"/cases/{cid}/fatwa/sign",
                   json={"member": "chairman", "image": SIG, "name": "Nope"},
                   headers=_h(applicant))
        assert r.status_code == 403, r.text


def test_owner_client_can_get_sign():
    """(c) GET sign은 케이스 소유 클라이언트도 200(조회 유지)."""
    with TestClient(app) as c:
        applicant = _tok(c, "applicant1", "pw")
        cid = _mkcase(c, applicant)
        r = c.get(f"/cases/{cid}/fatwa/sign", headers=_h(applicant))
        assert r.status_code == 200, r.text


if __name__ == "__main__":
    tests = [test_fatwa_liaison_can_sign, test_operator_can_sign,
             test_applicant_cannot_sign, test_owner_client_can_get_sign]
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
