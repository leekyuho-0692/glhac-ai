"""업체 자가가입의 필수 메일·동의와 이후 케이스 상속을 검증한다."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import models
from app.db import SessionLocal
from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _body(**extra):
    return {"username": "consent_" + uuid4().hex[:12], "password": "Passw0rd!23",
            "company_name": "메일 동의 테스트 업체", "company_email": "company@example.com",
            "email_consent": True, **extra}


def _assert_rejected(client, body, field=None, code="VALIDATION_ERROR"):
    with SessionLocal() as db:
        org_count = db.query(models.Org).count()
    r = client.post("/auth/register", json=body)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == code
    if field:
        assert r.json()["detail"]["field"] == field
    with SessionLocal() as db:
        assert db.query(models.User).filter_by(username=body["username"]).first() is None
        assert db.query(models.Org).count() == org_count
    return r


@pytest.mark.parametrize("field", ["company_email", "email_consent"])
def test_missing_required_field_is_422(client, field):
    body = _body()
    del body[field]
    _assert_rejected(client, body, field)


@pytest.mark.parametrize("email", [None, 123, "", "   ", "not-an-email", "a@localhost",
                                  "a b@example.com", "a@@example.com", "a" * 196 + "@a.co"])
def test_invalid_company_email_is_422(client, email):
    _assert_rejected(client, _body(company_email=email), "company_email")


def test_false_consent_is_rejected_without_creating_account(client):
    r = _assert_rejected(client, _body(email_consent=False), code="EMAIL_CONSENT_REQUIRED")
    assert r.json() == {"detail": {"code": "EMAIL_CONSENT_REQUIRED"}}


@pytest.mark.parametrize("email", ["  Company@Example.COM  ", " A" + "a" * 194 + "@A.CO "])
def test_signup_saves_company_email_and_consent_evidence(client, email):
    before = datetime.now(timezone.utc)
    r = client.post("/auth/register", json=_body(
        company_email=email, contacts={"pic": {"name": "담당자", "email": "pic@example.com"}}))
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        case = db.query(models.CaseApplication).filter_by(org_id=r.json()["org_id"]).one()
        assert case.email == email.strip().lower()
        assert case.notify_consent is True
        assert case.profile_ext["pic_email"] == "pic@example.com"
        consent = case.profile_ext["email_consent"]
        assert consent["agreed"] is True
        assert consent["version"] == "2026-11-11"
        assert consent["channel"] == "signup"
        agreed_at = datetime.fromisoformat(consent["at"])
        assert agreed_at.utcoffset() == timedelta(0)
        assert before <= agreed_at <= datetime.now(timezone.utc)
        org = db.get(models.Org, case.org_id)
        assert org.profile_ext["company_email"] == case.email
        assert org.profile_ext["email_consent"] == consent


def test_signup_without_case_preserves_org_email_and_inherits_into_new_case(client):
    r = client.post("/auth/register", json=_body(company_name=None, company_email="  Org@Example.COM "))
    assert r.status_code == 200, r.text
    org_id = r.json()["org_id"]
    with SessionLocal() as db:
        assert db.query(models.CaseApplication).filter_by(org_id=org_id).count() == 0
        org = db.get(models.Org, org_id)
        assert org.profile_ext["company_email"] == "org@example.com"
        consent = org.profile_ext["email_consent"]
        assert consent["agreed"] is True
        assert consent["version"] == "2026-11-11" and consent["channel"] == "signup"
        assert datetime.fromisoformat(consent["at"]).utcoffset() == timedelta(0)
    created = client.post("/cases", json={"company_name": "이후 등록 업체"},
                          headers={"Authorization": "Bearer " + r.json()["token"]})
    assert created.status_code == 200, created.text
    assert created.json()["email"] == "org@example.com"
    assert created.json()["notify_consent"] is True
    with SessionLocal() as db:
        case = db.get(models.CaseApplication, created.json()["case_id"])
        assert case.org_id == org_id and case.email == "org@example.com"
        assert case.notify_consent is True
        assert case.profile_ext["email_consent"] == consent


def test_company_email_fallback_keeps_existing_case_email(client):
    r = client.post("/auth/register", json=_body(company_name=None))
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        org = db.get(models.Org, r.json()["org_id"])
        org.profile_ext = {**org.profile_ext, "email": "existing@example.com"}
        db.commit()
    created = client.post("/cases", json={},
                          headers={"Authorization": "Bearer " + r.json()["token"]})
    assert created.status_code == 200, created.text
    assert created.json()["email"] == "existing@example.com"
    assert created.json()["notify_consent"] is False


def test_existing_org_without_signup_consent_still_creates_case(client):
    login = client.post("/auth/login", json={"username": "applicant1", "password": "pw"})
    assert login.status_code == 200, login.text
    created = client.post("/cases", json={"company_name": "기존 가입 업체"},
                          headers={"Authorization": "Bearer " + login.json()["token"]})
    assert created.status_code == 200, created.text
    assert created.json()["email"] is None
    assert created.json()["notify_consent"] is False
    assert "email_consent" not in created.json()["profile_ext"]


@pytest.mark.parametrize("role", ["auditor", "consultant", "fatwa_liaison", "admin"])
def test_staff_signup_still_succeeds_without_company_email_or_consent(client, role):
    r = client.post("/auth/staff-signup", json={
        "username": "staff_" + uuid4().hex[:12], "password": "Passw0rd!23", "requested_role": role})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending"
