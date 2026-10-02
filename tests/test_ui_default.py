"""UI 기본 화면(Legacy/V4) 분기 동작 테스트

실행: <venv>/bin/python -m pytest tests/test_ui_default.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_uidefault_test.db")

from fastapi.testclient import TestClient   # noqa: E402

from app.main import app                    # noqa: E402


def _legacy(monkeypatch):
    monkeypatch.delenv("GLHAC_UI_DEFAULT", raising=False)


def _v4(monkeypatch):
    monkeypatch.setenv("GLHAC_UI_DEFAULT", "v4")


def test_기본설정에서_루트는_ui로_리다이렉트된다(monkeypatch):
    _legacy(monkeypatch)
    with TestClient(app) as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/ui/"


def test_기본설정에서_루트는_ref쿼리를_보존한다(monkeypatch):
    _legacy(monkeypatch)
    with TestClient(app) as c:
        r = c.get("/?ref=ABC", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/ui/?ref=ABC"


def test_기본설정에서_ui는_구화면을_서빙한다(monkeypatch):
    _legacy(monkeypatch)
    with TestClient(app) as c:
        r = c.get("/ui/")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert "no-cache" in r.headers.get("cache-control", "")
        assert len(r.text) > 1000


def test_v4설정에서_루트는_v4로_리다이렉트된다(monkeypatch):
    _v4(monkeypatch)
    with TestClient(app) as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/ui/v4/"


def test_v4설정에서_루트는_ref쿼리를_보존한다(monkeypatch):
    _v4(monkeypatch)
    with TestClient(app) as c:
        r = c.get("/?ref=ABC", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/ui/v4/?ref=ABC"


def test_v4설정에서_ui는_v4로_리다이렉트된다(monkeypatch):
    _v4(monkeypatch)
    with TestClient(app) as c:
        r = c.get("/ui/", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/ui/v4/"


def test_v4설정에서_ui는_쿼리를_보존하며_리다이렉트된다(monkeypatch):
    _v4(monkeypatch)
    with TestClient(app) as c:
        r = c.get("/ui/?signup=staff", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/ui/v4/?signup=staff"


def test_v4설정에서도_구화면과_새화면모두_접근가능하다(monkeypatch):
    _v4(monkeypatch)
    with TestClient(app) as c:
        legacy = c.get("/ui/index.html")
        assert legacy.status_code == 200
        new = c.get("/ui/v4/")
        assert new.status_code == 200


def test_대소문자와_공백이_허용되고_알수없는값은_legacy다(monkeypatch):
    monkeypatch.setenv("GLHAC_UI_DEFAULT", " V4 ")
    with TestClient(app) as c:
        r = c.get("/", follow_redirects=False)
        assert r.headers["location"] == "/ui/v4/"

    monkeypatch.setenv("GLHAC_UI_DEFAULT", "new")
    with TestClient(app) as c:
        r = c.get("/", follow_redirects=False)
        assert r.headers["location"] == "/ui/"


def test_인계코드_리다이렉트가_설정에_따라_달라진다(monkeypatch):
    _legacy(monkeypatch)
    with TestClient(app) as c:
        token = c.post(
            "/auth/login", json={"username": "applicant1", "password": "pw"}
        ).json()["token"]
        r = c.post("/auth/handoff/issue", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert "/ui/#handoff=" in r.json()["redirect"]

    _v4(monkeypatch)
    with TestClient(app) as c:
        token = c.post(
            "/auth/login", json={"username": "applicant1", "password": "pw"}
        ).json()["token"]
        r = c.post("/auth/handoff/issue", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert "/ui/v4/#handoff=" in r.json()["redirect"]


def test_기본설정에서_ui에_대한_HEAD는_허용된다(monkeypatch):
    _legacy(monkeypatch)
    with TestClient(app) as c:
        r = c.head("/ui/")
        assert r.status_code != 405
        assert r.status_code == 200
