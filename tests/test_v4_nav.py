import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_v4nav_test.db"
os.environ["GLHAC_DEV"] = "1"
os.environ["GLHAC_NOTIFY_WORKER"] = "0"

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
client.__enter__()   # startup(데모 계정 시드) 실행


_PW = {"admin": "admin"}


def _h(c, u, p=None):
    r = c.post("/auth/login", json={"username": u, "password": _PW.get(u, "pw")})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_기본_빈설정():
    h = _h(client, "applicant1", "Passw0rd!")
    r = client.get("/me/v4-nav", headers=h)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ui_role"] == "ent"
    assert j["role"]["hidden"] == []
    assert j["user"]["order"] == []


def test_개인_저장_초기화():
    h = _h(client, "applicant1", "Passw0rd!")
    r = client.put("/me/v4-nav", headers=h,
                   json={"hidden": ["ent-consultant"], "order": ["ent-home", "ent-docs"]})
    assert r.status_code == 200, r.text
    j = client.get("/me/v4-nav", headers=h).json()
    assert j["user"]["hidden"] == ["ent-consultant"]
    assert j["user"]["order"] == ["ent-home", "ent-docs"]
    r = client.post("/me/v4-nav/reset", headers=h)
    assert r.status_code == 200, r.text
    j = client.get("/me/v4-nav", headers=h).json()
    assert j["user"]["hidden"] == []


def test_잘못된_view_id_422():
    h = _h(client, "applicant1", "Passw0rd!")
    r = client.put("/me/v4-nav", headers=h, json={"hidden": ["<script>"]})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "BAD_NAV_VIEW"
    r = client.put("/me/v4-nav", headers=h, json={"hidden": "x"})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "BAD_NAV_HIDDEN"


def test_관리자_역할설정_권한():
    hc = _h(client, "consultant1", "Passw0rd!")
    r = client.put("/admin/v4-nav/cons", headers=hc, json={"hidden": ["cons-qna"], "order": []})
    assert r.status_code == 403, r.text
    ho = _h(client, "operator1", "Passw0rd!")
    r = client.put("/admin/v4-nav/cons", headers=ho, json={"hidden": ["cons-qna"], "order": []})
    assert r.status_code == 200, r.text
    j = client.get("/me/v4-nav", headers=hc).json()
    assert j["role"]["hidden"] == ["cons-qna"]
    assert j["ui_role"] == "cons"
    ha = _h(client, "admin", "Passw0rd!")
    j = client.get("/admin/v4-nav", headers=ha).json()
    assert j["roles"]["cons"]["hidden"] == ["cons-qna"]
    r = client.put("/admin/v4-nav/zzz", headers=ho, json={"hidden": [], "order": []})
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["code"] == "UNKNOWN_UI_ROLE"
    r = client.put("/admin/v4-nav/cons", headers=ho, json={"hidden": [], "order": []})
    assert r.status_code == 200, r.text


def test_개인설정은_본인만():
    hc = _h(client, "consultant1", "Passw0rd!")
    r = client.post("/me/v4-nav/reset", headers=hc)
    assert r.status_code == 200, r.text
    hap = _h(client, "applicant1", "Passw0rd!")
    r = client.put("/me/v4-nav", headers=hap,
                   json={"hidden": ["ent-consultant"], "order": []})
    assert r.status_code == 200, r.text
    j = client.get("/me/v4-nav", headers=hc).json()
    assert j["user"]["hidden"] == []
