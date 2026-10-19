"""운영 10/17 역수신분(메일 복구·가입 컨설턴트 검색)을 감사 기준에 맞춘 부분의 부정 테스트."""
import os

os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_SECRET", "test-prod1017-" + "x" * 40)

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app import auth, mail_features as mf


def _probe_app():
    app = FastAPI()

    @app.get("/ip")
    def ip(request: Request):
        return {"ip": mf._client_ip(request)}

    return app


def test_reset_rate_limit_uses_real_client_ip_behind_proxy():
    """nginx 뒤에서는 request.client 가 전부 같은 주소라, 그걸 키로 쓰면 'IP 당' 제한이 전체 합계 제한이 된다."""
    c = TestClient(_probe_app())
    # 클라이언트가 위조해 넣은 첫 값이 아니라 nginx 가 끝에 붙인 실제 접속 IP 를 쓴다.
    assert c.get("/ip", headers={"X-Forwarded-For": "1.1.1.1, 203.0.113.7"}).json()["ip"] == "203.0.113.7"
    assert c.get("/ip", headers={"X-Forwarded-For": "198.51.100.9"}).json()["ip"] == "198.51.100.9"


def test_reset_limit_is_per_client_not_global():
    """한 클라이언트가 한도를 다 써도 다른 클라이언트의 재설정 요청은 막히지 않아야 한다."""
    mf._hits.clear()
    for _ in range(20):
        mf.rate_limit(("reset-ip", "203.0.113.7"), 20)
    try:
        mf.rate_limit(("reset-ip", "203.0.113.7"), 20)
        raise AssertionError("한도 초과가 막히지 않았다")
    except HTTPException as e:
        assert e.status_code == 429
    mf.rate_limit(("reset-ip", "198.51.100.9"), 20)   # 다른 IP 는 통과
    mf._hits.clear()


def test_consultant_search_is_rate_limited_per_ip():
    """가입 전 공개 검색으로 컨설턴트 로그인 아이디를 무한정 열거하지 못하게 한다."""
    from app.main import app
    auth._USERNAME_CHECK_ATTEMPTS.clear()
    with TestClient(app) as c:
        h = {"X-Forwarded-For": "203.0.113.50"}
        codes = [c.get("/auth/consultants/search", params={"q": "co"}, headers=h).status_code for _ in range(25)]
        assert 200 in codes and codes[-1] == 429
        # 다른 IP 는 영향 없음
        assert c.get("/auth/consultants/search", params={"q": "co"},
                     headers={"X-Forwarded-For": "203.0.113.51"}).status_code == 200
    auth._USERNAME_CHECK_ATTEMPTS.clear()


def test_mask_login_hides_email_and_id():
    from app.main import _mask_login
    assert _mask_login("consultant@example.com") == "co***@e***"
    assert _mask_login("budi") == "bu***"
    assert "example.com" not in _mask_login("consultant@example.com")
