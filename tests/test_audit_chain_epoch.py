import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_chainepoch_test.db"
os.environ["GLHAC_DEV"] = "1"
os.environ["GLHAC_NOTIFY_WORKER"] = "0"

from datetime import datetime

from fastapi.testclient import TestClient

from app.main import app
from app import models
from app.db import SessionLocal


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


def _new_case(c, admin, name):
    r = c.post("/cases", json={"org_id": "org_demo", "company_name": name}, headers=_h(admin))
    assert r.status_code == 200, r.text
    return r.json()["case_id"]


def _events(cid):
    db = SessionLocal()
    try:
        rows = (db.query(models.WorkflowEvent).filter_by(case_id=cid)
                .order_by(models.WorkflowEvent.created_at, models.WorkflowEvent.event_id).all())
        return [e.event_id for e in rows]
    finally:
        db.close()


def _tamper(event_id, created_at=None):
    db = SessionLocal()
    try:
        e = db.query(models.WorkflowEvent).filter_by(event_id=event_id).first()
        assert e is not None
        e.row_hash = "0" * 64
        if created_at is not None:
            e.created_at = created_at
        db.commit()
    finally:
        db.close()


def _verify(c, admin, cid):
    r = c.get(f"/cases/{cid}/audit-verify", headers=_h(admin))
    assert r.status_code == 200, r.text
    return r.json()


def _set_all_created_at(cid, base=datetime(2026, 7, 1, 0, 0, 0)):
    db = SessionLocal()
    try:
        rows = (db.query(models.WorkflowEvent).filter_by(case_id=cid)
                .order_by(models.WorkflowEvent.created_at, models.WorkflowEvent.event_id).all())
        for i, e in enumerate(rows):
            e.created_at = base.replace(minute=base.minute + i)
        db.commit()
    finally:
        db.close()


def test_정상_전부_검증():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        cid = _new_case(c, admin, "chain_epoch_ok")
        r = _verify(c, admin, cid)
        assert r["status"] == "ok"
        assert r["integrity_ok"] is True
        assert r["legacy"] == 0
        assert r["verified"] == r["count"]
        assert r["count"] >= 1


def test_경계이전_불일치는_이전서명체계():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        cid = _new_case(c, admin, "chain_epoch_legacy")
        eids = _events(cid)
        assert eids
        _set_all_created_at(cid, datetime(2026, 7, 1, 0, 0, 0))
        _tamper(eids[-1], created_at=datetime(2026, 8, 1, 0, 0, 0))
        r = _verify(c, admin, cid)
        assert r["status"] == "ok_with_legacy"
        assert r["integrity_ok"] is True
        assert r["legacy"] == 1
        assert r["legacy_until"].startswith("2026-08-01")
        assert r["broken_at"] is None


def test_경계이후_불일치는_깨짐():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        cid = _new_case(c, admin, "chain_epoch_broken")
        eids = _events(cid)
        assert eids
        _tamper(eids[-1])
        r = _verify(c, admin, cid)
        assert r["status"] == "broken"
        assert r["integrity_ok"] is False
        assert r["broken_at"] == eids[-1]


def test_경계_환경변수_조정(monkeypatch):
    monkeypatch.setenv("GLHAC_AUDIT_CHAIN_EPOCH", "2000-01-01T00:00:00")
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        cid = _new_case(c, admin, "chain_epoch_env")
        eids = _events(cid)
        assert eids
        _set_all_created_at(cid, datetime(2026, 7, 1, 0, 0, 0))
        _tamper(eids[-1], created_at=datetime(2026, 8, 1, 0, 0, 0))
        r = _verify(c, admin, cid)
        assert r["status"] == "broken"
