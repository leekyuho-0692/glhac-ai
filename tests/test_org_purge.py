"""고아 조직 정리 — 빈 껍데기만 지우고, 사람이 판단할 것은 남긴다.

배경: 테스트·리허설이 계정을 만들 때마다 자체 org 를 만든다. 케이스를 정리해도
조직은 남아 18개가 쌓였다(실측). 다만 조직을 함부로 지우면 그 조직의 계정이 소속 없는
상태가 되고, 케이스가 딸린 조직은 업무 데이터가 통째로 사라진다.

실행: <venv>/bin/python -m pytest tests/test_org_purge.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_orgpurge_test.db")

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.main as m  # noqa: E402
import app.models as models  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(m.auth, "dev_mode", lambda: True)
    eng = create_engine("sqlite:///%s" % (tmp_path / "orgpurge.db"))
    models.Base.metadata.create_all(bind=eng)
    s = sessionmaker(bind=eng)()
    s.add(models.Org(org_id="org_empty", name="빈 조직"))
    s.add(models.Org(org_id="org_with_case", name="케이스 있는 조직"))
    s.add(models.Org(org_id="org_with_user", name="사용자 있는 조직"))
    s.add(models.Org(org_id="org_facility_only", name="시설만 남은 조직"))
    s.add(models.CaseApplication(case_id="c1", org_id="org_with_case", company_name="업체"))
    s.add(models.User(username="someone", password_hash="x", role="applicant",
                      org_id="org_with_user"))
    s.add(models.Facility(org_id="org_facility_only", name="공장"))
    s.commit()
    return s


ADMIN = {"role": "admin", "uid": "admin1", "org_id": "*", "username": "admin"}


def _run(db, **kw):
    body = {"dry_run": True}
    body.update(kw)
    return m.admin_purge_orphan_orgs(body=body, user=ADMIN, db=db)


def test_dry_run_is_the_default(db):
    """미리보기가 기본이다 — 실수로 지워지면 안 된다."""
    r = m.admin_purge_orphan_orgs(body={}, user=ADMIN, db=db)
    assert r["dry_run"] is True
    assert db.query(models.Org).count() == 4


def test_orgs_with_cases_are_kept(db):
    """케이스가 있는 조직은 지우지 않는다 — 업무 데이터가 딸려 있다."""
    r = _run(db)
    ids = {x["org_id"] for x in r["cases"]}
    assert "org_with_case" not in ids
    assert any(s["org_id"] == "org_with_case" and "케이스" in s["reason"]
               for s in r["skipped"])


def test_orgs_with_users_are_kept(db):
    """사용자가 있는 조직은 지우지 않는다 — 계정이 소속을 잃는다."""
    r = _run(db)
    ids = {x["org_id"] for x in r["cases"]}
    assert "org_with_user" not in ids
    assert any(s["org_id"] == "org_with_user" and "사용자" in s["reason"]
               for s in r["skipped"])


def test_empty_and_facility_only_orgs_are_targeted(db):
    """빈 조직과 부속물만 남은 조직이 대상이다."""
    r = _run(db)
    ids = {x["org_id"] for x in r["cases"]}
    assert ids == {"org_empty", "org_facility_only"}
    fac = next(x for x in r["cases"] if x["org_id"] == "org_facility_only")
    assert fac["rows"].get("facility") == 1


def test_confirm_is_required(db):
    """확인 문구 없이는 지워지지 않는다."""
    with pytest.raises(HTTPException) as e:
        _run(db, dry_run=False, reason="정리")
    assert e.value.detail["code"] == "CONFIRM_REQUIRED"


def test_reason_is_required(db):
    """사유는 감사로그에 남는다 — 없으면 거부."""
    with pytest.raises(HTTPException) as e:
        _run(db, dry_run=False, confirm="PURGE")
    assert e.value.detail["code"] == "REASON_REQUIRED"


def test_count_mismatch_aborts(db):
    """미리보기와 개수가 다르면 멈춘다 — 그새 데이터가 바뀐 것이다."""
    with pytest.raises(HTTPException) as e:
        _run(db, dry_run=False, confirm="PURGE", reason="정리", expect_delete=99)
    assert e.value.detail["code"] == "COUNT_MISMATCH"


def test_apply_deletes_only_the_targets(db):
    """실행하면 대상만 사라지고 나머지는 그대로."""
    r = _run(db, dry_run=False, confirm="PURGE", reason="테스트 잔재 정리", expect_delete=2)
    assert sorted(r["deleted"]) == ["org_empty", "org_facility_only"]
    left = {o.org_id for o in db.query(models.Org).all()}
    assert left == {"org_with_case", "org_with_user"}
    assert db.query(models.Facility).count() == 0


def test_audit_log_survives(db):
    """조직이 사라져도 '있었다는 사실'은 남는다."""
    db.add(models.AuditLog(org_id="org_empty", action="something"))
    db.commit()
    before = db.query(models.AuditLog).count()
    _run(db, dry_run=False, confirm="PURGE", reason="정리", expect_delete=2)
    assert db.query(models.AuditLog).count() >= before


def test_org_ids_narrows_the_target(db):
    """org_ids 를 주면 그 조직만 본다 — 범위를 좁혀 실행할 수 있어야 한다."""
    r = _run(db, org_ids=["org_empty"])
    assert {x["org_id"] for x in r["cases"]} == {"org_empty"}
