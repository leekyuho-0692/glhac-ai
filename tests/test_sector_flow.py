"""SRS SECTOR FLOW — 분야(sector)별 scheme·서류요건·초대 메타·목록 필터."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_sector_test.db")
os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_NOTIFY_WORKER", "0")
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app                    # noqa: E402
from app import models                      # noqa: E402
from app import intake                      # noqa: E402
from app.db import SessionLocal             # noqa: E402


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _mkcase(c, h, body):
    r = c.post("/cases", headers=h, json=body)
    assert r.status_code == 200, r.text
    return r.json()["case_id"]


def test_분야별_scheme_기본값():
    assert intake.sector_defaults("warehouse") == ("logistics", ["penyimpanan", "pengemasan"])
    assert intake.sector_defaults("transport") == ("logistics", ["pendistribusian"])
    assert intake.sector_defaults("food") == ("product", None)


def test_서류요건_분야_추가():
    r = intake.doc_requirements(None, None, None, scheme="product", sector="cosmetics")
    assert "coa_msds" in r["required"]
    assert "factory_registration" in r["notes"]
    r2 = intake.doc_requirements(None, None, None, scheme="product", sector="food")
    assert "coa_msds" in r2["required"]
    assert r2["notes"] == {}
    r3 = intake.doc_requirements(None, None, None, scheme="logistics", logistics_scope=["pendistribusian"])
    assert "vehicle_list" in r3["required"]
    assert "warehouse_layout" not in r3["required"]


def test_케이스_생성_분야_저장():
    with TestClient(app) as c:
        h = _tok(c, "applicant1", "pw")
        wid = _mkcase(c, h, {"company_name": "창고Co", "sector": "warehouse"})
        w = c.get("/cases/%s" % wid, headers=h).json()
        assert w["scheme"] == "logistics"
        assert w["logistics_scope"] == ["penyimpanan", "pengemasan"]
        assert w["profile_ext"]["sector"] == "warehouse"
        tid = _mkcase(c, h, {"company_name": "수송Co", "sector": "transport"})
        t = c.get("/cases/%s" % tid, headers=h).json()
        assert t["scheme"] == "logistics"
        assert t["logistics_scope"] == ["pendistribusian"]
        cid = _mkcase(c, h, {"company_name": "화장품Co", "sector": "cosmetics"})
        m = c.get("/cases/%s" % cid, headers=h).json()
        assert m["scheme"] == "product"
        assert m["profile_ext"]["sectors"] == ["cosmetics"]


def test_초대_분야_메타와_공개확인_비노출():
    with TestClient(app) as c:
        ch = _tok(c, "consultant1", "pw")
        r = c.post("/consultant/invites", headers=ch, json={"company_name": "분야테스트", "sectors": ["cosmetics", "food"], "contact_name": "김담당", "phone": "010-1", "expires_days": 30, "max_uses": 1})
        assert r.status_code == 200, r.text
        code = r.json()["code"]
        assert r.json()["sectors"] == ["cosmetics", "food"]
        k = c.get("/invites/%s/check" % code)
        assert k.status_code == 200, k.text
        assert k.json()["valid"] is True
        assert k.json()["sectors"] == ["cosmetics", "food"]
        assert "contact_name" not in k.json()


def test_가입_분야_프리필():
    with TestClient(app) as c:
        ch = _tok(c, "consultant1", "pw")
        r = c.post("/consultant/invites", headers=ch, json={"company_name": "분야테스트", "sectors": ["cosmetics", "food"], "contact_name": "김담당", "phone": "010-1", "expires_days": 30, "max_uses": 1})
        code = r.json()["code"]
        import random, string
        un = "sec_" + "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(8))
        g = c.post("/auth/register", json={"username": un, "password": "Passw0rd!23", "company_name": "분야테스트", "invite_code": code})
        assert g.status_code == 200, g.text
        h = {"Authorization": "Bearer " + g.json()["token"]}
        lst = c.get("/cases", headers=h).json()
        assert len(lst["items"]) >= 1
        cid = lst["items"][0]["case_id"]
        d = c.get("/cases/%s" % cid, headers=h).json()
        assert d["sector"] == "cosmetics"
        assert d["profile_ext"]["sectors"] == ["cosmetics", "food"]
        un2 = "sec_" + "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(8))
        g2 = c.post("/auth/register", json={"username": un2, "password": "Passw0rd!23", "company_name": "수송가입", "sectors": ["transport"]})
        assert g2.status_code == 200, g2.text
        h2 = {"Authorization": "Bearer " + g2.json()["token"]}
        lst2 = c.get("/cases", headers=h2).json()
        cid2 = lst2["items"][0]["case_id"]
        d2 = c.get("/cases/%s" % cid2, headers=h2).json()
        assert d2["sector"] == "transport"
        assert d2["scheme"] == "logistics"


def test_컨설턴트_변경요청_승인_반려():
    with TestClient(app) as c:
        ah = _tok(c, "applicant1", "pw")
        r = c.post("/orgs/org_demo/consultant-change-request", headers=ah, json={"reason": "응답 지연", "preferred_consultant": "consultant1"})
        assert r.status_code == 200, r.text
        aid = r.json()["approval_id"]
        assert r.json()["status"] == "pending"
        r2 = c.post("/orgs/org_demo/consultant-change-request", headers=ah, json={"preferred_consultant": "consultant1"})
        assert r2.status_code == 422, r2.text
        assert r2.json()["detail"]["code"] == "REASON_REQUIRED"
        r3 = c.post("/orgs/org_demo/consultant-change-request", headers=ah, json={"reason": "응답 지연", "preferred_consultant": "consultant1"})
        assert r3.status_code == 409, r3.text
        assert r3.json()["detail"]["code"] == "APPROVAL_PENDING"
        oh = _tok(c, "operator1", "pw")
        apprs = c.get("/approvals", headers=oh).json()
        items = apprs["items"] if isinstance(apprs, dict) else apprs
        found = [a for a in items if a.get("approval_id") == aid]
        assert found and found[0]["action_type"] == "consultant.change"
        assert found[0]["can_decide"] is True
        x = c.post("/approvals/%s/approve" % aid, headers=ah)
        assert x.status_code == 403, x.text
        ap = c.post("/approvals/%s/approve" % aid, headers=oh)
        assert ap.status_code == 200, ap.text
        assert ap.json()["result"]["changed"] is True
        db = SessionLocal()
        try:
            cu = db.query(models.User).filter_by(username="consultant1").first()
            assert ap.json()["result"]["consultant_id"] == cu.user_id
            org = db.query(models.Org).filter_by(org_id="org_demo").first()
            assert org.consultant_id == cu.user_id
            db.close()
        finally:
            db.close()
        r4 = c.post("/orgs/org_demo/consultant-change-request", headers=ah, json={"reason": "재요청", "preferred_consultant": "consultant1"})
        aid2 = r4.json()["approval_id"]
        rj = c.post("/approvals/%s/reject" % aid2, headers=oh, json={"reason": "사유"})
        assert rj.status_code == 200, rj.text
        assert rj.json()["status"] == "rejected"


def test_목록_분야_필터():
    with TestClient(app) as c:
        oh = _tok(c, "operator1", "pw")
        r = c.get("/cases", headers=oh, params={"sector": "warehouse"})
        assert r.status_code == 200, r.text
        for it in r.json()["items"]:
            assert it["sector"] == "warehouse"
        r2 = c.get("/cases", headers=oh, params={"search": "분야테스트"})
        assert r2.status_code == 200, r2.text
        for it in r2.json()["items"]:
            assert "분야테스트" in it["company_name"]
        r3 = c.get("/cases", headers=oh, params={"status": "onboarding"})
        assert r3.status_code == 200, r3.text
        for it in r3.json()["items"]:
            assert it["status"] == "onboarding"
