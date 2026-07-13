"""법적효력 Phase 4(플랫폼 컴플라이언스) 격리 테스트.

검증 항목:
  (a) 모든 응답에 보안 헤더(X-Frame-Options·X-Content-Type-Options·CSP) 존재.
      HSTS는 http 요청에서 미부여(로컬 개발 무손상).
  (b) GET /admin/compliance — admin 200·항목별 status·시크릿 미노출. 비권한 403.
  (c) 레이트리밋 — 낮은 한도(env)로 유도 시 write 초과분 429 RATE_LIMITED.
  (d) 기존 API·SPA(/ui/) 200 — 보안헤더/레이트리밋이 무손상.

격리: app import 전에 고유 DB·낮은 레이트리밋 한도를 os.environ에 직접 대입(공유 DB 오염 방지).
실행: <venv>/bin/python -m pytest tests/test_phase4_compliance.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_p4_test.db"   # 격리 강제
os.environ["GLHAC_DEV"] = "1"                                  # 데모 계정 시드
os.environ["GLHAC_SECRET"] = "p4-" + "x" * 40                  # 기본시크릿 부팅차단 회피
os.environ["GLHAC_RATE_LIMIT_PER_MIN"] = "5"                   # (c) 낮은 한도로 429 유도

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(tok):
    return {"Authorization": "Bearer " + tok}


def test_a_security_headers_present_no_hsts_on_http():
    """(a) 보안 헤더 존재 · HSTS는 http에서 미부여."""
    with TestClient(app) as c:
        r = c.get("/public-config")
        assert r.status_code == 200, r.text
        assert r.headers.get("X-Frame-Options") == "DENY"
        assert r.headers.get("X-Content-Type-Options") == "nosniff"
        assert r.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
        csp = r.headers.get("Content-Security-Policy") or ""
        assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
        # 로컬 http → HSTS 미부여
        assert "Strict-Transport-Security" not in r.headers


def test_a2_hsts_present_when_forwarded_https():
    """(a) X-Forwarded-Proto=https 이면 HSTS 부여."""
    with TestClient(app) as c:
        r = c.get("/public-config", headers={"X-Forwarded-Proto": "https"})
        assert r.status_code == 200
        assert "max-age=" in (r.headers.get("Strict-Transport-Security") or "")


def test_b_compliance_admin_ok_items_no_secret_leak():
    """(b) admin 200 · 항목별 status · 시크릿 미노출."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        r = c.get("/admin/compliance", headers=_h(adm))
        assert r.status_code == 200, r.text
        d = r.json()
        items = d["items"]
        assert len(items) >= 10
        allowed = {"met", "pending", "not_configured", "action_required"}
        keys = set()
        for it in items:
            assert it["status"] in allowed, it
            assert it.get("detail"), it
            keys.add(it["key"])
        for expect in ["security_headers", "rate_limiting", "pii_encryption",
                       "audit_chain", "pse_registration", "app_secret"]:
            assert expect in keys, keys
        # 보안헤더·레이트리밋은 met, 감사체인 met
        by = {it["key"]: it for it in items}
        assert by["security_headers"]["status"] == "met"
        assert by["rate_limiting"]["status"] == "met"
        assert by["audit_chain"]["status"] == "met"
        # 시크릿 값 미노출 — 응답 본문에 실제 시크릿/키가 없어야 함
        blob = r.text
        assert os.environ["GLHAC_SECRET"] not in blob
        assert "GLHAC_ENC_KEY" not in blob or "GLHAC_ENC_KEY=" not in blob
        assert d["summary"]["met"] >= 3


def test_b2_compliance_forbidden_for_non_admin():
    """(b) 비권한(applicant) 403."""
    with TestClient(app) as c:
        ap = _tok(c, "applicant1", "pw")
        r = c.get("/admin/compliance", headers=_h(ap))
        assert r.status_code == 403, r.text


def test_c_rate_limit_write_429():
    """(c) write 요청이 활성 한도 초과 시 429 RATE_LIMITED. (import 순서 무관 — 실제 한도를 읽어 초과)."""
    import app.main as m
    limit = m._RL_WRITE_MAX
    m._RL_WRITE_HITS.clear()   # 이전 테스트 잔존 카운터 제거(멱등)
    try:
        with TestClient(app) as c:
            cons = _tok(c, "consultant1", "pw")
            saw_429 = False
            code = None
            for _ in range(limit + 3):
                r = c.post("/cases", json={"org_id": "org_demo", "company_name": "P4RL Co"},
                           headers=_h(cons))
                if r.status_code == 429:
                    saw_429 = True
                    code = r.json().get("code")
                    break
            assert saw_429, "레이트리밋 429가 발생하지 않음"
            assert code == "RATE_LIMITED"
    finally:
        m._RL_WRITE_HITS.clear()   # 다른 테스트 파일 예산 오염 방지(단일 프로세스 조합 실행 대비)


def test_d_existing_api_and_spa_unaffected():
    """(d) 기존 API·SPA(/ui/) 200 — 보안헤더·레이트리밋 무손상."""
    with TestClient(app) as c:
        # SPA 로드
        r = c.get("/ui/")
        assert r.status_code == 200, r.text
        assert r.headers.get("X-Frame-Options") == "DENY"
        assert "<html" in r.text.lower() or "loginview" in r.text.lower()
        # 로그인(무제한 write 경로 — /auth 제외 확인)
        r2 = c.post("/auth/login", json={"username": "admin", "password": "admin"})
        assert r2.status_code == 200, r2.text
        # GET API 정상(레이트리밋 미적용)
        adm = r2.json()["token"]
        for _ in range(10):
            g = c.get("/admin/compliance", headers=_h(adm))
            assert g.status_code == 200


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
