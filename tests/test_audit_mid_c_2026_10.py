"""2026-10 감사 중간 결함 묶음 MC(v4 프런트·운영) 중 서버 쪽 MC5·MC6 테스트.

실제 TestClient 로 정적 자산의 압축·캐시 헤더와 /health 의 build 필드를 검증한다.
(MC2·MC4 의 JS 동작은 node 로 따로 검증한다.)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_auditest_c_2026_10.db")
os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_NOTIFY_WORKER", "0")
os.environ.setdefault("GLHAC_RATE_LIMIT_PER_MIN", "0")

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

_APP_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app")
_V4 = os.path.join(_APP_DIR, "static", "v4")
_VERSION = os.path.join(_APP_DIR, "VERSION")
client = TestClient(app)
_GZ = {"Accept-Encoding": "gzip"}


def _big(path_dir, ext):
    """1024바이트를 넘는 실제 자산 하나를 고른다(압축 대상 보장)."""
    for name in sorted(os.listdir(path_dir)):
        full = os.path.join(path_dir, name)
        if name.endswith(ext) and os.path.getsize(full) > 1024:
            return name
    raise AssertionError(f"{path_dir} 에 1KB 넘는 {ext} 가 없음")


def test_mc5_js_asset_gzip_and_cache():
    name = _big(os.path.join(_V4, "js"), ".js")
    r = client.get(f"/ui/v4/js/{name}", headers=_GZ)
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    assert r.headers["cache-control"] == "public, max-age=3600"


def test_mc5_css_and_font_cache():
    r = client.get("/ui/v4/fonts.css", headers=_GZ)
    assert r.status_code == 200
    assert "max-age=3600" in r.headers["cache-control"]
    font = _big(os.path.join(_V4, "fonts"), ".woff2")
    r = client.get(f"/ui/v4/fonts/{font}", headers=_GZ)
    assert r.status_code == 200
    assert "max-age=3600" in r.headers["cache-control"]


def test_mc5_html_stays_no_store():
    for p in ("/ui/v4/index.html", "/ui/v4/", "/ui/index.html", "/ui/"):
        r = client.get(p, headers=_GZ, follow_redirects=False)
        assert r.status_code == 200, p
        cc = r.headers["cache-control"]
        assert "no-store" in cc and "no-cache" in cc, (p, cc)
        assert "max-age=3600" not in cc, p


def test_mc5_legacy_ui_assets_not_cached():
    """1시간 캐시는 /ui/v4 자산만 — 구 화면 쪽 정적 파일(js·css)은 예전처럼 no-store."""
    found = [f for f in os.listdir(os.path.join(_APP_DIR, "static")) if f.endswith((".js", ".css"))]
    if not found:
        return
    r = client.get(f"/ui/{found[0]}")
    assert r.status_code == 200
    assert "no-store" in r.headers["cache-control"]


def test_mc5_api_response_not_publicly_cached():
    r = client.get("/health", headers=_GZ)
    assert "max-age=3600" not in r.headers.get("cache-control", "")
    assert "public" not in r.headers.get("cache-control", "")


def test_mc5_gzip_middleware_registered_once():
    from starlette.middleware.gzip import GZipMiddleware
    n = sum(1 for m in app.user_middleware if m.cls is GZipMiddleware)
    assert n == 1
    assert [m for m in app.user_middleware if m.cls is GZipMiddleware][0].kwargs["minimum_size"] == 1024


def test_mc6_health_build_from_version_file():
    backup = None
    if os.path.exists(_VERSION):
        with open(_VERSION, encoding="utf-8") as f:
            backup = f.read()
    try:
        with open(_VERSION, "w", encoding="utf-8") as f:
            f.write("abc1234-dirty 2026-10-13 10:00:00 KST\n")
        d = client.get("/health").json()
        assert d["build"] == "abc1234-dirty 2026-10-13 10:00:00 KST"
        assert d["service"] == "glhac-ai"
    finally:
        if backup is None:
            os.remove(_VERSION)
        else:
            with open(_VERSION, "w", encoding="utf-8") as f:
                f.write(backup)


def test_mc6_health_build_unknown_without_version_file():
    backup = None
    if os.path.exists(_VERSION):
        with open(_VERSION, encoding="utf-8") as f:
            backup = f.read()
        os.remove(_VERSION)
    try:
        assert client.get("/health").json()["build"] == "unknown"
        # 빈 파일도 unknown
        with open(_VERSION, "w", encoding="utf-8") as f:
            f.write("\n")
        assert client.get("/health").json()["build"] == "unknown"
    finally:
        if os.path.exists(_VERSION):
            os.remove(_VERSION)
        if backup is not None:
            with open(_VERSION, "w", encoding="utf-8") as f:
                f.write(backup)


def test_mc6_version_file_is_gitignored():
    root = os.path.dirname(_APP_DIR)
    with open(os.path.join(root, ".gitignore"), encoding="utf-8") as f:
        lines = [x.strip() for x in f.read().splitlines()]
    assert "app/VERSION" in lines
    assert ".pytest_cache/" in lines   # 줄바꿈 누락으로 두 항목이 붙지 않았는지
