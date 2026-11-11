"""Legacy list/detail parity and material actions: real API regression coverage."""
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import models
from app.db import SessionLocal
import app.main as m


ACCOUNTS = ["applicant1", "consultant1", "penyelia1", "pendamping1",
            "auditor1", "fatwa1", "operator1", "admin"]


def login(client, username):
    r = client.post("/auth/login", json={"username": username,
                    "password": "admin" if username == "admin" else "pw"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


@pytest.fixture
def world():
    with TestClient(m.app) as client:
        prefix = "uileg_" + uuid4().hex[:10]
        with SessionLocal() as db:
            users = {u.username: u.user_id for u in db.query(models.User).all()}
            other = models.Org(org_id=prefix, name="Uileg external")
            referral = models.Org(org_id=prefix + "_ref", name="Uileg referral",
                                  consultant_id=users["consultant1"])
            db.add_all([other, referral])
            db.flush()
            cases = {}
            # Interleave forbidden and accessible cases in sort order.
            for i, (name, org) in enumerate([
                ("own_unassigned", "org_demo"), ("cross_assigned", other.org_id),
                ("own_assigned", "org_demo"), ("reassigned", "org_demo"),
                ("foreign_unassigned", other.org_id), ("referral", referral.org_id),
                ("staff_assigned", other.org_id), ("tie_reassigned", "org_demo"),
            ]):
                cid = prefix + "_" + name
                db.add(models.CaseApplication(case_id=cid, org_id=org, company_name=name,
                       created_at=datetime(2030, 1, 1) + timedelta(minutes=i)))
                cases[name] = cid
            db.flush()
            for name in ["cross_assigned", "own_assigned", "reassigned", "tie_reassigned"]:
                db.add(models.PendampingAssignment(case_id=cases[name],
                       assignment_id=prefix + "_a_" + name,
                       pendamping_id=users["pendamping1"], assigned_at=datetime(2030, 1, 1)))
            for name, when in [("reassigned", datetime(2030, 1, 2)),
                               ("tie_reassigned", datetime(2030, 1, 1))]:
                db.add(models.PendampingAssignment(case_id=cases[name],
                       assignment_id=prefix + "_z_" + name,
                       pendamping_id="another_pendamping", assigned_at=when))
            for role, action in [("auditor", "ops.auditor_assigned"),
                                 ("consultant", m.CONSULTANT_ASSIGN_ACTION)]:
                db.add(models.WorkflowEvent(case_id=cases["staff_assigned"], action=action,
                       payload={role + "_id": users[role + "1"]}, row_hash=prefix + role))
            db.commit()
        yield client, cases


def visible_ids(client, headers, limit=500):
    offset, ids, total = 0, [], None
    while True:
        r = client.get("/cases", params={"limit": limit, "offset": offset}, headers=headers)
        assert r.status_code == 200, r.text
        data = r.json()
        if total is None:
            total = data["total"]
        assert data["total"] == total
        page = [row["case_id"] for row in data["items"]]
        assert len(page) == min(limit, max(total - offset, 0)), data
        ids.extend(page)
        if len(ids) >= total:
            break
        offset += limit
    assert len(ids) == len(set(ids)) == total
    for cid in ids:
        r = client.get(f"/cases/{cid}", headers=headers)
        assert r.status_code == 200, (cid, r.status_code, r.text)
    return set(ids)


@pytest.mark.parametrize("username", ACCOUNTS)
def test_every_listed_case_opens_and_every_accessible_fixture_is_listed(world, username):
    client, cases = world
    headers = login(client, username)
    visible = visible_ids(client, headers)
    for cid in cases.values():
        r = client.get(f"/cases/{cid}", headers=headers)
        assert r.status_code in (200, 403), r.text
        assert (cid in visible) == (r.status_code == 200)
    if username == "consultant1":
        assert cases["referral"] in visible and cases["staff_assigned"] in visible
    if username == "auditor1":
        assert cases["referral"] not in visible and cases["staff_assigned"] in visible


def test_pendamping_current_assignment_precedes_count_and_pagination(world):
    client, cases = world
    headers = login(client, "pendamping1")
    visible = visible_ids(client, headers, limit=1)
    assert visible & set(cases.values()) == {cases["cross_assigned"], cases["own_assigned"]}
    for name in ["own_unassigned", "reassigned", "foreign_unassigned", "tie_reassigned"]:
        assert client.get(f"/cases/{cases[name]}", headers=headers).status_code == 403
    with SessionLocal() as db:
        db.add(models.PendampingAssignment(case_id=cases["cross_assigned"],
               pendamping_id="another_pendamping", assigned_at=datetime(2031, 1, 1)))
        db.commit()
    assert cases["cross_assigned"] not in visible_ids(client, headers, limit=1)
    assert client.get(f"/cases/{cases['cross_assigned']}", headers=headers).status_code == 403


def test_list_calls_the_detail_guard_instead_of_a_second_policy(world, monkeypatch):
    client, cases = world
    headers = login(client, "admin")
    original = m._assert_case_access

    def deny_one(db, user, case):
        if case.case_id == cases["own_assigned"]:
            raise HTTPException(403, "test access revoked")
        return original(db, user, case)

    monkeypatch.setattr(m, "_assert_case_access", deny_one)
    assert cases["own_assigned"] not in visible_ids(client, headers)
    assert client.get(f"/cases/{cases['own_assigned']}", headers=headers).status_code == 403


def test_list_does_not_hide_unexpected_guard_errors(world, monkeypatch):
    client, _ = world
    headers = login(client, "admin")

    def unavailable(*args):
        raise HTTPException(503, "policy unavailable")

    monkeypatch.setattr(m, "_assert_case_access", unavailable)
    assert client.get("/cases", headers=headers).status_code == 503


@pytest.mark.parametrize("username", ACCOUNTS)
def test_material_actions_match_add_edit_delete_and_evidence_api(world, username):
    client, cases = world
    headers = login(client, username)
    cid = cases["own_assigned"]  # pendamping has genuine case access: any 403 is an action denial.
    assert client.get(f"/cases/{cid}", headers=headers).status_code == 200
    r = client.get("/rbac/actions", headers=headers)
    assert r.status_code == 200
    actions = r.json()["actions"]
    with SessionLocal() as db:
        material = models.Material(case_id=cid, name="uileg_fixture_salt")
        db.add(material)
        db.commit()
        mid = material.material_id
    r = client.post(f"/cases/{cid}/materials", json={"name": "uileg_new_water"}, headers=headers)
    assert r.status_code == (200 if actions["material.add"] else 403), r.text
    r = client.patch(f"/materials/{mid}", json={"manufacturer": "Uileg manufacturer"}, headers=headers)
    assert r.status_code == (200 if actions["material.add"] else 403), r.text
    r = client.post(f"/cases/{cid}/materials/{mid}/evidence",
                    json={"evidence_type": "coa", "filename": "uileg.pdf",
                          "file_b64": "JVBERi0xLjQK"}, headers=headers)
    assert r.status_code == (200 if actions["material.evidence"] else 403), r.text
    r = client.delete(f"/materials/{mid}", headers=headers)
    assert r.status_code == (200 if actions["material.delete"] else 403), r.text
