import os
import sys
import json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_alepoch_test.db"
os.environ["GLHAC_DEV"] = "1"
os.environ["GLHAC_NOTIFY_WORKER"] = "0"

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app import models  # noqa: E402
from app.db import SessionLocal  # noqa: E402


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(t):
    return {"Authorization": "Bearer " + t}


def test_경계이전_서명불일치는_이전서명체계():
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid = c.post("/cases", json={"org_id": "org_demo", "company_name": "AuditEpochCo"}, headers=_h(adm)).json()["case_id"]
        for _ in range(3):
            assert c.get(f"/cases/{cid}", headers=_h(adm)).status_code == 200
        db = SessionLocal()
        try:
            row = (db.query(models.AuditLog)
                   .filter(models.AuditLog.meta.isnot(None))
                   .order_by(models.AuditLog.created_at.desc())
                   .all())
            target = None
            for r in row:
                if r.meta and "_chain" in r.meta:
                    target = r
                    break
            assert target is not None, "no chained audit log"
            m = dict(target.meta)
            ch = dict(m["_chain"])
            ch["row"] = "0" * 64
            m["_chain"] = ch
            target.meta = m
            target.created_at = datetime(2026, 8, 1)
            action = target.action
            db.add(target)
            db.commit()
        finally:
            db.close()
        v = c.get("/admin/audit-log-verify", params={"action": action}, headers=_h(adm))
        assert v.status_code == 200, v.text
        js = v.json()
        assert js["tampered_count"] == 0, js
        assert js["legacy"] >= 1, js


def test_경계이후_서명불일치는_위조():
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid = c.post("/cases", json={"org_id": "org_demo", "company_name": "AuditEpochCo"}, headers=_h(adm)).json()["case_id"]
        for _ in range(3):
            assert c.get(f"/cases/{cid}", headers=_h(adm)).status_code == 200
        db = SessionLocal()
        try:
            row = (db.query(models.AuditLog)
                   .filter(models.AuditLog.meta.isnot(None))
                   .order_by(models.AuditLog.created_at.desc())
                   .all())
            target = None
            for r in row:
                if r.meta and "_chain" in r.meta:
                    target = r
                    break
            assert target is not None, "no chained audit log"
            m = dict(target.meta)
            ch = dict(m["_chain"])
            ch["row"] = "0" * 64
            m["_chain"] = ch
            target.meta = m
            action = target.action
            rid = target.id
            db.add(target)
            db.commit()
        finally:
            db.close()
        v = c.get("/admin/audit-log-verify", params={"action": action}, headers=_h(adm))
        assert v.status_code == 200, v.text
        js = v.json()
        assert js["tampered_count"] >= 1, js
        assert rid in js["tampered"], js


def test_요약문구_이전서명체계_포함():
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid = c.post("/cases", json={"org_id": "org_demo", "company_name": "AuditEpochCo"}, headers=_h(adm)).json()["case_id"]
        for _ in range(3):
            assert c.get(f"/cases/{cid}", headers=_h(adm)).status_code == 200
        v = c.get("/admin/audit-log-verify", headers=_h(adm))
        assert v.status_code == 200, v.text
        js = v.json()
        assert "이전 서명 체계" in js["summary"], js
