"""비밀번호 초기화(관리자 → 각 계정) · 본인 비밀번호 변경(모든 계정).

실행: <venv>/bin/python -m pytest tests/test_password_reset.py -q
"""
import logging
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///" + tempfile.mktemp(suffix=".db"))
os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_SECRET", "test-" + "x" * 36)
logging.disable(logging.CRITICAL)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def _h(t):
    return {"Authorization": "Bearer " + t}


def _login(c, u, p):
    return c.post("/auth/login", json={"username": u, "password": p})


def _new_user(c, adm, role="auditor"):
    u = "pw" + uuid.uuid4().hex[:8]
    r = c.post("/admin/users", json={"username": u, "password": "Start1234!", "role": role, "org_id": "org_demo"},
               headers=_h(adm))
    assert r.status_code == 200, r.text
    return u, r.json()["user_id"]


def test_관리자가_각_계정_비밀번호를_초기화한다():
    with TestClient(app) as c:
        adm = _login(c, "admin", "admin").json()["token"]
        for role in ("auditor", "consultant", "applicant", "fatwa_liaison", "operator", "pendamping_pph"):
            u, uid = _new_user(c, adm, role)
            r = c.patch("/admin/users/" + uid, json={"password": "Gh-TempPass99"}, headers=_h(adm))
            assert r.status_code == 200, r.text
            assert _login(c, u, "Gh-TempPass99").status_code == 200, role
            assert _login(c, u, "Start1234!").status_code == 401, role


def test_관리자가_아니면_초기화할_수_없고_짧은_비밀번호는_거부된다():
    with TestClient(app) as c:
        adm = _login(c, "admin", "admin").json()["token"]
        _, uid = _new_user(c, adm)
        op = _login(c, "operator1", "pw").json()["token"]
        assert c.patch("/admin/users/" + uid, json={"password": "Gh-TempPass99"}, headers=_h(op)).status_code == 403
        assert c.patch("/admin/users/" + uid, json={"password": "short"}, headers=_h(adm)).status_code == 422


def test_본인_비밀번호_변경():
    with TestClient(app) as c:
        adm = _login(c, "admin", "admin").json()["token"]
        u, _ = _new_user(c, adm, "consultant")
        j = _login(c, u, "Start1234!").json()
        other = _login(c, u, "Start1234!").json()          # 다른 기기 세션
        # 현재 비밀번호 틀림 → 400(401 이면 화면이 세션 만료로 로그아웃시킨다)
        r = c.post("/auth/change-password", json={"current_password": "nope", "new_password": "NewPass1234"},
                   headers=_h(j["token"]))
        assert r.status_code == 400 and r.json()["detail"]["code"] == "BAD_CURRENT_PASSWORD"
        # 정책 위반·같은 비밀번호 → 422
        assert c.post("/auth/change-password", json={"current_password": "Start1234!", "new_password": "x"},
                      headers=_h(j["token"])).status_code == 422
        assert c.post("/auth/change-password", json={"current_password": "Start1234!", "new_password": "Start1234!"},
                      headers=_h(j["token"])).status_code == 422
        r = c.post("/auth/change-password", json={"current_password": "Start1234!", "new_password": "NewPass1234"},
                   headers=_h(j["token"]))
        assert r.status_code == 200, r.text
        new = r.json()
        assert c.get("/auth/me", headers=_h(new["token"])).status_code == 200          # 이 화면은 계속
        assert c.get("/auth/me", headers=_h(other["token"])).status_code == 401        # 다른 기기는 해제
        assert _login(c, u, "NewPass1234").status_code == 200
        assert _login(c, u, "Start1234!").status_code == 401


def test_로그인_안_하면_변경_불가():
    with TestClient(app) as c:
        r = c.post("/auth/change-password", json={"current_password": "a", "new_password": "NewPass1234"})
        assert r.status_code == 401


def test_화면에_버튼이_연결돼_있다():
    root = os.path.dirname(os.path.dirname(__file__))
    v4 = open(os.path.join(root, "app/static/v4/js/users.js"), encoding="utf-8").read()
    legacy = open(os.path.join(root, "app/static/index.html"), encoding="utf-8").read()
    assert "App.usPwReset" in v4 and "/auth/change-password" in v4 and "App.pwMine" in v4
    assert "resetUserPw(" in legacy and "changeMyPw()" in legacy and "/auth/change-password" in legacy
