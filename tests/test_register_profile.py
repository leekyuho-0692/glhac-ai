"""가입 화면에서 받은 업체 정보·담당자를 초기 케이스에 빠짐없이 저장한다."""
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


def _register(client, **extra):
    body = {"username": "profile_" + uuid4().hex[:12], "password": "Passw0rd!23",
            "company_email": (extra.get("contacts", {}).get("pic") or {}).get("email") or "company@example.com",
            "email_consent": True,
            "company_name": "가입 테스트 업체", "nib": "120-81-00000", **extra}
    r = client.post("/auth/register", json=body)
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        c = db.query(models.CaseApplication).filter_by(org_id=r.json()["org_id"]).one()
        return {"nib": c.nib, "profile_ext": c.profile_ext or {}, "email": c.email, "phone": c.phone,
                "scheme": c.scheme, "logistics_scope": c.logistics_scope}


def test_가입_사업자번호_영문명_담당자와_분야를_저장한다(client):
    contacts = {
        "pic": {"name": "김담당", "title": "품질관리 과장", "phone": "010-1234-5678", "email": "pic@example.com"},
        "cp": {"name": "이책임", "title": "생산본부 부장", "phone": "02-123-4567", "email": "cp@example.com"},
    }
    c = _register(client, company_name_en="  GL Food Co., Ltd.  ", contacts=contacts, sectors=["food"])
    assert c["nib"] == "120-81-00000"
    pe = c["profile_ext"]
    assert pe["company_name_en"] == "GL Food Co., Ltd."
    assert pe["sector"] == "food" and pe["sectors"] == ["food"]
    for prefix, contact in contacts.items():
        for key, value in contact.items():
            assert pe[prefix + "_" + key] == value
    assert (c["email"], c["phone"]) == (contacts["pic"]["email"], contacts["pic"]["phone"])


@pytest.mark.parametrize("cp", [None, {}, {"name": " ", "title": "", "phone": None, "email": "  "}])
def test_선택_담당자2가_비면_키를_저장하지_않는다(client, cp):
    c = _register(client, contacts={"pic": {"name": "김담당"}, "cp": cp})
    assert c["profile_ext"]["pic_name"] == "김담당"
    assert not any(k.startswith("cp_") for k in c["profile_ext"])


def test_담당자_길이_상한과_영문명_200자는_정상_저장한다(client):
    contact = {"name": "가" * 100, "title": "나" * 100, "phone": "1" * 40, "email": "a" * 195 + "@a.co"}
    c = _register(client, company_name_en="A" * 200, contacts={"pic": contact, "cp": contact})
    assert c["profile_ext"]["company_name_en"] == "A" * 200
    for prefix in ("pic", "cp"):
        for key, size in (("name", 100), ("title", 100), ("phone", 40), ("email", 200)):
            assert len(c["profile_ext"][prefix + "_" + key]) == size
    assert len(c["email"]) == 200 and len(c["phone"]) == 40


@pytest.mark.parametrize("prefix", ["pic", "cp"])
@pytest.mark.parametrize("field,value", [
    ("name", "가" * 101), ("title", "나" * 101),
    ("phone", "1" * 41), ("phone", "12345"), ("phone", "010-ABCD-5678"),
    ("email", "a" * 196 + "@a.co"), ("email", "not-an-email"),
    ("email", "a@localhost"), ("email", "a b@example.com"),
], ids=["name-length", "title-length", "phone-length", "phone-short", "phone-format",
        "email-length", "email-no-at", "email-no-dot", "email-space"])
def test_담당자_길이와_형식_위반은_필드명과_함께_422(client, prefix, field, value):
    username = "invalid_" + uuid4().hex[:12]
    r = client.post("/auth/register", json={"company_email": "company@example.com", "email_consent": True, "username": username, "password": "Passw0rd!23",
                                            "contacts": {prefix: {field: value}}})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["field"] == "contacts.%s.%s" % (prefix, field)
    with SessionLocal() as db:
        assert db.query(models.User).filter_by(username=username).first() is None


@pytest.mark.parametrize("field,value", [("company_name_en", "A" * 201), ("nib", "12345")], ids=["company-length", "nib-format"])
def test_영문명_상한과_기존_사업자번호_검증을_유지한다(client, field, value):
    r = client.post("/auth/register", json={"company_email": "company@example.com", "email_consent": True, "username": "invalid_" + uuid4().hex[:12],
                                            "password": "Passw0rd!23", field: value})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["field"] == field


def test_가입시_물류_분야를_바로_초기_케이스에_적용한다(client):
    c = _register(client, sectors=["warehouse"])
    assert c["profile_ext"]["sectors"] == ["warehouse"]
    assert c["scheme"] == "logistics"
    assert c["logistics_scope"] == ["penyimpanan", "pengemasan"]
