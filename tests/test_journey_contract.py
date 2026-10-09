import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_journey_test.db"
os.environ["GLHAC_DEV"] = "1"
os.environ["GLHAC_NOTIFY_WORKER"] = "0"

from fastapi.testclient import TestClient

from app.main import app
from app import models
from app.db import SessionLocal


def test_계약_confirmed_이면_여정_완료():
    with TestClient(app) as c:
        r = c.post("/auth/login", json={"username": "admin", "password": "admin"})
        token = r.json()["token"]
        h = {"Authorization": "Bearer " + token}

        r = c.post("/cases", json={"org_id": "org_demo", "company_name": "JourneyCo"}, headers=h)
        cid = r.json()["case_id"]

        db = SessionLocal()
        ct = models.Contract(case_id=cid)
        ct.status = "confirmed"
        db.add(ct)
        db.commit()
        db.close()

        r = c.get("/cases/%s/journey" % cid, headers=h)
        assert r.status_code == 200
        stages = r.json()["stages"]
        contract = [s for s in stages if s["key"] == "contract"][0]
        assert contract["status"] == "done"

        db = SessionLocal()
        ct = db.query(models.Contract).filter(models.Contract.case_id == cid).first()
        ct.status = "requested"
        db.commit()
        db.close()

        r = c.get("/cases/%s/journey" % cid, headers=h)
        assert r.status_code == 200
        stages = r.json()["stages"]
        contract = [s for s in stages if s["key"] == "contract"][0]
        assert contract["status"] != "done"
