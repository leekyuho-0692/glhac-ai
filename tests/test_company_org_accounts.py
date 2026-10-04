"""기업별 조직·계정 — 스태프가 대신 만든 신청은 업체 전용 조직으로, 업체 계정은 그 조직에 발급.

예전엔 컨설턴트가 만든 신청이 컨설턴트 조직(org_demo)에 들어가 업체 6곳이 한 조직에 섞이고
업체 계정이 없었다(운영 실측 2026-10-03).

실행: <venv>/bin/python -m pytest tests/test_company_org_accounts.py -q
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

from app import models  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402


def _h(t):
    return {"Authorization": "Bearer " + t}


def _tok(c, u, p):
    return c.post("/auth/login", json={"username": u, "password": p}).json()["token"]


def _uid(name):
    db = SessionLocal()
    try:
        return db.query(models.User).filter_by(username=name).first().user_id
    finally:
        db.close()


def _org(org_id):
    db = SessionLocal()
    try:
        o = db.get(models.Org, org_id)
        return (o.name, o.consultant_id) if o else None
    finally:
        db.close()


def test_컨설턴트가_만든_신청은_업체_전용_조직에_들어간다():
    with TestClient(app) as c:
        cons = _tok(c, "consultant1", "pw")
        name = "업체" + uuid.uuid4().hex[:6]
        r = c.post("/cases", json={"company_name": name}, headers=_h(cons))
        assert r.status_code == 200, r.text
        org = r.json()["org_id"]
        assert org not in ("org_demo", "*")
        assert _org(org) == (name, _uid("consultant1"))           # 담당 컨설턴트로 연결
        # 같은 업체 두 번째 신청은 같은 조직
        assert c.post("/cases", json={"company_name": name}, headers=_h(cons)).json()["org_id"] == org
        # 다른 업체는 다른 조직
        assert c.post("/cases", json={"company_name": name + "B"}, headers=_h(cons)).json()["org_id"] != org
        # 컨설턴트 목록에 보인다
        lst = c.get("/cases", headers=_h(cons)).json()
        items = lst if isinstance(lst, list) else lst.get("items") or lst.get("cases")
        assert any(x["org_id"] == org for x in items)


def test_업체명_없이는_만들_수_없고_기업_계정은_자기_조직에_만든다():
    with TestClient(app) as c:
        assert c.post("/cases", json={}, headers=_h(_tok(c, "consultant1", "pw"))).status_code == 422
        app_tok = _tok(c, "applicant1", "pw")
        assert c.post("/cases", json={"company_name": "아무이름"}, headers=_h(app_tok)).json()["org_id"] == "org_demo"


def test_관리자는_org_id_지정_또는_업체_조직_자동():
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        assert c.post("/cases", json={"company_name": "x", "org_id": "org_demo"}, headers=_h(adm)).json()["org_id"] == "org_demo"
        org = c.post("/cases", json={"company_name": "관리자생성" + uuid.uuid4().hex[:4]}, headers=_h(adm)).json()["org_id"]
        assert org not in ("org_demo", "*") and _org(org)[1] is None


def test_기업_계정_발급과_로그인():
    with TestClient(app) as c:
        cons = _tok(c, "consultant1", "pw")
        name = "발급" + uuid.uuid4().hex[:6]
        case = c.post("/cases", json={"company_name": name}, headers=_h(cons)).json()
        org = case["org_id"]
        u = "co" + uuid.uuid4().hex[:8]
        r = c.post("/orgs/%s/accounts" % org, json={"username": u}, headers=_h(cons))
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["company_role"] == "client_admin" and d["temp_password"].startswith("Gh-")
        j = c.post("/auth/login", json={"username": u, "password": d["temp_password"]}).json()
        assert j["role"] == "applicant" and j["org_id"] == org
        lst = c.get("/cases", headers=_h(j["token"])).json()
        items = lst if isinstance(lst, list) else lst.get("items") or lst.get("cases")
        assert [x["case_id"] for x in items] == [case["case_id"]]   # 자기 업체 신청만
        # 두 번째 계정은 업무자, 중복 409, 잘못된 아이디 422
        assert c.post("/orgs/%s/accounts" % org, json={"username": u + "b"}, headers=_h(cons)).json()["company_role"] == "client_staff"
        assert c.post("/orgs/%s/accounts" % org, json={"username": u}, headers=_h(cons)).status_code == 409
        assert c.post("/orgs/%s/accounts" % org, json={"username": "a b"}, headers=_h(cons)).status_code == 422


def test_발급_권한():
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        org = c.post("/cases", json={"company_name": "권한" + uuid.uuid4().hex[:6]}, headers=_h(adm)).json()["org_id"]
        body = {"username": "p" + uuid.uuid4().hex[:8]}
        assert c.post("/orgs/%s/accounts" % org, json=body, headers=_h(_tok(c, "consultant1", "pw"))).status_code == 403  # 담당 아님
        assert c.post("/orgs/%s/accounts" % org, json=body, headers=_h(_tok(c, "applicant1", "pw"))).status_code == 403
        assert c.post("/orgs/org_demo/accounts", json=body, headers=_h(adm)).status_code == 404       # 공용 조직 불가
        assert c.post("/orgs/%s/accounts" % org, json=body, headers=_h(_tok(c, "operator1", "pw"))).status_code == 200
