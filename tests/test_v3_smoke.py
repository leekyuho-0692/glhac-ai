"""v3 변경 검증 — operator 역할 · mockAudit RBAC · 권한 이관(cert_issue).
TestClient 기반(서버 불필요). 실행: <venv>/bin/python tests/test_v3_smoke.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_v3_test.db")

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(tok):
    return {"Authorization": "Bearer " + tok}


def test_operator_role_seeded():
    """v3 신규 역할 operator 시드·로그인."""
    with TestClient(app) as c:
        r = c.post("/auth/login", json={"username": "operator1", "password": "pw"})
        assert r.status_code == 200, r.text
        assert r.json()["role"] == "operator", r.json()


def test_mockaudit_rbac():
    """모의심사 큐: operator·auditor 허용 / applicant 차단(클라이언트 미노출)."""
    with TestClient(app) as c:
        op = _tok(c, "operator1", "pw")
        ap = _tok(c, "applicant1", "pw")
        aud = _tok(c, "auditor1", "pw")
        assert c.get("/mock-audit/queue", headers=_h(op)).status_code == 200
        assert c.get("/mock-audit/queue", headers=_h(aud)).status_code == 200
        assert c.get("/mock-audit/queue", headers=_h(ap)).status_code == 403


def test_permission_transfer_cert_issue():
    """인증서 발급 권한 이관: consultant 차단(403) / operator·fatwa 역할 게이트 통과(≠403)."""
    with TestClient(app) as c:
        cons = _tok(c, "consultant1", "pw")
        op = _tok(c, "operator1", "pw")
        fat = _tok(c, "fatwa1", "pw")
        # consultant는 이제 발급 불가
        assert c.post("/cases/none/certificate/issue", headers=_h(cons)).status_code == 403
        # operator·fatwa_liaison는 역할 통과(케이스 없어 403이 아닌 404 등)
        assert c.post("/cases/none/certificate/issue", headers=_h(op)).status_code != 403
        assert c.post("/cases/none/certificate/issue", headers=_h(fat)).status_code != 403


def test_mockaudit_decision_validation():
    """모의심사 결정: 잘못된 결과/사유 누락 검증(케이스 없으면 404이지만 권한은 통과)."""
    with TestClient(app) as c:
        op = _tok(c, "operator1", "pw")
        # 권한은 통과해야 하므로 403이 아니어야 함
        r = c.post("/cases/none/mock-audit/decision", json={"result": "pass"}, headers=_h(op))
        assert r.status_code != 403, r.text


if __name__ == "__main__":
    tests = [test_operator_role_seeded, test_mockaudit_rbac,
             test_permission_transfer_cert_issue, test_mockaudit_decision_validation]
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
