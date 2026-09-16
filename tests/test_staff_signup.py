"""스태프 가입 승인 + 관리자 클라이언트 초대 QR.

컨설턴트·오디터·파트와·관리자 자가가입은 곧 권한상승이라, 신청은 대기(pending)로만
받고 관리자 승인 시에만 User 가 생긴다. 승인 전에는 로그인 자체가 안 돼야 한다.
관리자 클라이언트 초대는 특정 컨설턴트에 안 묶여(귀속 없음) 가입만 시킨다.

실행: <venv>/bin/python -m pytest tests/test_staff_signup.py -q
"""
import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///" + tempfile.mktemp(suffix=".db"))
os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_SECRET", "test-" + "x" * 36)
logging.disable(logging.CRITICAL)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def _admin(c):
    return {"Authorization": "Bearer " + c.post(
        "/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]}


def test_스태프가입은_승인전_로그인이_막힌다():
    with TestClient(app) as c:
        r = c.post("/auth/staff-signup", json={
            "username": "staffaud1", "password": "secret12",
            "requested_role": "auditor", "display_name": "김오디터"})
        assert r.status_code == 200 and r.json()["status"] == "pending"
        # 승인 전에는 users 에 없어 로그인 불가
        assert c.post("/auth/login", json={"username": "staffaud1",
                                           "password": "secret12"}).status_code == 401


def test_승인해야_계정이_생기고_역할대로_로그인된다():
    with TestClient(app) as c:
        h = _admin(c)
        rid = c.post("/auth/staff-signup", json={
            "username": "staffcon1", "password": "secret12",
            "requested_role": "consultant"}).json()["request_id"]
        lst = c.get("/admin/staff-signups", headers=h).json()
        assert lst["pending_count"] >= 1
        ap = c.post(f"/admin/staff-signups/{rid}/approve", headers=h)
        assert ap.status_code == 200 and ap.json()["role"] == "consultant"
        lr = c.post("/auth/login", json={"username": "staffcon1", "password": "secret12"})
        assert lr.status_code == 200 and lr.json()["role"] == "consultant"
        # 재승인은 막힌다
        assert c.post(f"/admin/staff-signups/{rid}/approve", headers=h).status_code == 409


def test_거부하면_계정이_안_생긴다():
    with TestClient(app) as c:
        h = _admin(c)
        rid = c.post("/auth/staff-signup", json={
            "username": "staffrej1", "password": "secret12",
            "requested_role": "admin"}).json()["request_id"]
        assert c.post(f"/admin/staff-signups/{rid}/reject", headers=h,
                      json={"reason": "미승인"}).status_code == 200
        assert c.post("/auth/login", json={"username": "staffrej1",
                                           "password": "secret12"}).status_code == 401


def test_잘못된_역할은_거부된다():
    with TestClient(app) as c:
        r = c.post("/auth/staff-signup", json={
            "username": "staffbad1", "password": "secret12", "requested_role": "superuser"})
        assert r.status_code == 422


def test_비관리자는_승인목록을_못_본다():
    with TestClient(app) as c:
        # applicant1(데모)로는 관리자 목록 접근 불가
        tok = c.post("/auth/login", json={"username": "applicant1", "password": "pw"}).json().get("token")
        h = {"Authorization": "Bearer " + tok}
        assert c.get("/admin/staff-signups", headers=h).status_code in (401, 403)


def test_관리자_클라이언트초대는_컨설턴트_귀속이_없다():
    with TestClient(app) as c:
        h = _admin(c)
        info = c.get("/admin/client-invite", headers=h).json()
        code = info["code"]
        chk = c.get(f"/invites/{code}/check").json()
        assert chk["valid"] and chk["kind"] == "client" and chk["consultant"] is None
        # QR 이미지 생성
        png = c.get("/admin/client-invite/qr?fmt=png", headers=h)
        assert png.status_code == 200 and png.headers["content-type"] == "image/png"
        # 이 코드로 가입 → org 에 컨설턴트 귀속 없음
        rg = c.post("/auth/register", json={
            "username": "clientco1", "password": "secret12",
            "company_name": "PT 하우스클라이언트", "invite_code": code})
        assert rg.status_code == 200
        assert rg.json()["referral"] == {"client_invite": True}


def test_클라이언트초대_코드는_항상_같다():
    with TestClient(app) as c:
        h = _admin(c)
        a = c.get("/admin/client-invite", headers=h).json()["code"]
        b = c.get("/admin/client-invite", headers=h).json()["code"]
        assert a == b   # 하우스 초대는 하나(재발급 안 함)


def test_관리자_클라이언트초대_발급목록회수():
    """관리자가 컨설턴트처럼 클라이언트 초대를 발급·목록·회수한다. 대표는 회수 불가."""
    with TestClient(app) as c:
        h = _admin(c)
        lst0 = c.get("/admin/client-invites", headers=h).json()
        assert any(i["is_primary"] for i in lst0["items"])          # 대표 코드 존재
        nv = c.post("/admin/client-invites", headers=h,
                    json={"company_name": "PT 부스", "max_uses": 50, "expires_days": 14}).json()
        assert nv["code"] and nv["max_uses"] == 50
        lst = c.get("/admin/client-invites", headers=h).json()["items"]
        iid = [i["invite_id"] for i in lst if not i["is_primary"]][0]
        assert c.get(f"/admin/client-invites/{iid}/qr?fmt=png", headers=h).status_code == 200
        assert c.post(f"/admin/client-invites/{iid}/revoke", headers=h).status_code == 200
        # 대표 회수는 400
        pid = [i["invite_id"] for i in lst if i["is_primary"]][0]
        assert c.post(f"/admin/client-invites/{pid}/revoke", headers=h).status_code == 400


def test_스태프_가입링크와_직행URL():
    """관리자만 스태프 가입 링크를 얻고, 클라이언트 초대는 앱 회원가입 직행 URL이다."""
    import os
    os.environ.setdefault("GLHAC_APP_URL", "https://glhac.co.kr")
    with TestClient(app) as c:
        h = _admin(c)
        sl = c.get("/admin/staff-signup-link", headers=h).json()
        assert sl["url"].endswith("/ui/?signup=staff")
        assert c.get("/admin/staff-signup-link/qr?fmt=png", headers=h).status_code == 200
        # 클라이언트 초대 url 은 앱 회원가입 화면 직행(?ref=)
        ci = c.get("/admin/client-invite", headers=h).json()
        assert "/ui/?ref=" in ci["url"]
        # 비관리자 차단
        tok = c.post("/auth/login", json={"username": "applicant1", "password": "pw"}).json().get("token")
        assert c.get("/admin/staff-signup-link",
                     headers={"Authorization": "Bearer " + tok}).status_code in (401, 403)
