"""SRS LOGISTICS PACKAGE — 물류 패키지점검(scheme·complete·gaps·items)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_logipkg_test.db")
os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_NOTIFY_WORKER", "0")
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app                    # noqa: E402
from app import models                      # noqa: E402


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _mkcase(c, h, body):
    r = c.post("/cases", headers=h, json=body)
    assert r.status_code == 200, r.text
    return r.json()["case_id"]


def test_운송_패키지점검():
    with TestClient(app) as c:
        ha = _tok(c, "applicant1", "pw")
        cid = _mkcase(c, ha, {"company_name": "패키지운송", "sector": "transport"})
        r = c.get(f"/cases/{cid}/package-check", headers=ha)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["scheme"] == "logistics"
        assert j["complete"] is False
        assert "vehicle" in j["gaps"]
        assert "cleaning" in j["gaps"]
        assert "product" not in j["gaps"]
        it = j["items"]
        assert set(it.keys()) == {"company", "scope", "vehicle", "facility", "cleaning"}
        assert it["scope"]["ok"] is True
        assert it["scope"]["scope"] == ["pendistribusian"]
        assert it["vehicle"]["required"] is True
        assert it["vehicle"]["ok"] is False
        assert it["facility"]["required"] is False
        assert it["facility"]["ok"] is True
        assert it["cleaning"]["ok"] is False
        org_id = c.get(f"/cases/{cid}", headers=ha).json()["org_id"]
        rv = c.post(f"/orgs/{org_id}/vehicles", headers=ha, json={
            "plate_no": "B 1234 XYZ",
            "vehicle_type": "truck",
            "transport_type": "chilled",
            "capacity": "3 ton",
            "reg_no": "REG-001",
            "previous_cargo": "냉동식품",
            "previous_cargo_halal": True,
            "last_cleaned": "2024-01-01",
        })
        assert rv.status_code == 200, rv.text
        r2 = c.get(f"/cases/{cid}/package-check", headers=ha)
        assert r2.status_code == 200, r2.text
        assert r2.json()["items"]["vehicle"]["ok"] is True


def test_창고_패키지점검():
    with TestClient(app) as c:
        ha = _tok(c, "applicant1", "pw")
        cid = _mkcase(c, ha, {"company_name": "패키지창고", "sector": "warehouse"})
        r = c.get(f"/cases/{cid}/package-check", headers=ha)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["scheme"] == "logistics"
        assert j["complete"] is False
        it = j["items"]
        assert it["scope"]["ok"] is True
        assert it["scope"]["scope"] == ["penyimpanan", "pengemasan"]
        assert it["vehicle"]["required"] is False
        assert it["vehicle"]["ok"] is True
        assert it["facility"]["required"] is True
        assert it["facility"]["ok"] is False
        assert "facility" in j["gaps"]


def test_제품_점검_유지():
    with TestClient(app) as c:
        ha = _tok(c, "applicant1", "pw")
        cid = _mkcase(c, ha, {"company_name": "패키지제품"})
        r = c.get(f"/cases/{cid}/package-check", headers=ha)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["scheme"] == "product"
        it = j["items"]
        assert "product" in it
        assert "material" in it
        assert "factory" in it
