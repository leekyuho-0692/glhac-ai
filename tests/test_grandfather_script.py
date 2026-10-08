"""SRS FORMAL FLOW — grandfather 스크립트 검증."""
import os
import sys
import subprocess
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_gf_test.db")
os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_NOTIFY_WORKER", "0")
os.environ.setdefault("GLHAC_FORMAL_FLOW", "0")
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app                    # noqa: E402
from app import models                      # noqa: E402
from app.db import SessionLocal             # noqa: E402


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _set_status(cid, status):
    db = SessionLocal()
    try:
        ca = db.query(models.CaseApplication).filter_by(case_id=cid).first()
        ca.status = status
        db.commit()
    finally:
        db.close()


def test_grandfather_script():
    os.environ["GLHAC_FORMAL_FLOW"] = "1"
    try:
        with TestClient(app) as c:
            admin = _tok(c, "admin", "admin")
            r = c.post("/cases", headers=admin, json={"org_id": "org_demo", "company_name": "AComp"})
            assert r.status_code == 200, r.text
            cid_a = r.json()["case_id"]
            r = c.post("/cases", headers=admin, json={"org_id": "org_demo", "company_name": "BComp"})
            assert r.status_code == 200, r.text
            cid_b = r.json()["case_id"]
            _set_status(cid_b, "consultant_review")
        from app.db import engine as _eng
        dburl = str(_eng.url)
        r = subprocess.run([sys.executable, "scripts/formal_flow_grandfather.py", "--db", dburl], cwd=ROOT, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert "대상 케이스" in r.stdout
        assert "BComp" in r.stdout
        assert "AComp" not in r.stdout
        assert "변경 없음" in r.stdout
        r = subprocess.run([sys.executable, "scripts/formal_flow_grandfather.py", "--db", dburl, "--apply"], cwd=ROOT, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert "처리 완료" in r.stdout
        r = subprocess.run([sys.executable, "scripts/formal_flow_grandfather.py", "--db", dburl], cwd=ROOT, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert "대상 케이스 0건" in r.stdout
        with TestClient(app) as c:
            admin = _tok(c, "admin", "admin")
            r = c.get(f"/cases/{cid_b}/eligibility", headers=admin)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body.get("verdict") == "eligible"
            assert body.get("formal", {}).get("status") == "accepted"
    finally:
        os.environ["GLHAC_FORMAL_FLOW"] = "0"
