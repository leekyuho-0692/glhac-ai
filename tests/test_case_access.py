"""케이스 접근 경계 — 조직 격리는 지키되 인증기관·배정은 인정한다.

배경: 회원가입하면 그 회사만의 org 가 생긴다. 그런데 샤리아·최고운영자는 데모 조직
소속이라 ORG_FORBIDDEN 으로 막혔고, 결과적으로 신규 가입 업체는 인증서까지 갈 수
없었다(실측: 논스톱 시나리오 21~26단계 전부 403).

여기서 지키는 성질
  · 인증기관 역할(관리자·최고운영자·샤리아)은 조직을 넘어 본다 — 심사·판정이 업무다.
  · 오디터·컨설턴트는 자기 조직 + '배정받은' 케이스만. 배정 없이 남의 서류를 열면 안 된다.
  · 신청기업·할랄감독자·동반자는 자기 조직만. 여기가 뚫리면 업체 정보가 새어나간다.

실행: <venv>/bin/python -m pytest tests/test_case_access.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_access_test.db")

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.main as m  # noqa: E402
import app.models as models  # noqa: E402

OWN, OTHER = "org_staff", "org_newco"


def _db(tmp_path):
    eng = create_engine("sqlite:///%s" % (tmp_path / "access.db"))
    models.Base.metadata.create_all(bind=eng)
    db = sessionmaker(bind=eng)()
    db.add(models.CaseApplication(case_id="c_own", org_id=OWN, company_name="기존 업체"))
    db.add(models.CaseApplication(case_id="c_new", org_id=OTHER,
                                  company_name="신규 가입 업체"))
    db.commit()
    return db


def _user(role, uid="u1", org=OWN):
    return {"role": role, "uid": uid, "org_id": org, "username": uid}


def _can(db, user, case_id):
    c = db.get(models.CaseApplication, case_id)
    try:
        m._assert_case_access(db, user, c)
        return True
    except HTTPException:
        return False


def _assign(db, case_id, action, key, uid):
    db.add(models.WorkflowEvent(case_id=case_id, action=action,
                                payload={key: uid}, row_hash="x"))
    db.commit()


@pytest.mark.parametrize("role", ["admin", "operator", "fatwa_liaison"])
def test_certifier_roles_cross_org(tmp_path, role):
    """인증기관 역할은 신규 가입 업체를 본다 — 못 보면 인증서 발급이 불가능하다."""
    db = _db(tmp_path)
    assert _can(db, _user(role), "c_new")


@pytest.mark.parametrize("role", ["applicant", "penyelia_halal", "pendamping_pph"])
def test_company_side_roles_stay_in_their_org(tmp_path, role):
    """업체 측 역할은 남의 회사 케이스를 절대 못 본다."""
    db = _db(tmp_path)
    assert _can(db, _user(role), "c_own")
    assert not _can(db, _user(role), "c_new")


def test_auditor_needs_assignment(tmp_path):
    """오디터는 배정 전에는 못 보고, 배정 후에 열린다."""
    db = _db(tmp_path)
    u = _user("auditor", "aud1")
    assert not _can(db, u, "c_new")
    _assign(db, "c_new", "ops.auditor_assigned", "auditor_id", "aud1")
    assert _can(db, u, "c_new")


def test_consultant_needs_assignment(tmp_path):
    """컨설턴트도 마찬가지 — 배정 개념이 없어 신규 업체를 아예 못 받던 문제."""
    db = _db(tmp_path)
    u = _user("consultant", "con1")
    assert not _can(db, u, "c_new")
    _assign(db, "c_new", m.CONSULTANT_ASSIGN_ACTION, "consultant_id", "con1")
    assert _can(db, u, "c_new")


def test_assignment_is_per_person(tmp_path):
    """다른 사람에게 배정된 케이스는 열리지 않는다 — 배정이 전체 개방이 되면 안 된다."""
    db = _db(tmp_path)
    _assign(db, "c_new", "ops.auditor_assigned", "auditor_id", "aud1")
    assert not _can(db, _user("auditor", "aud2"), "c_new")
    assert not _can(db, _user("consultant", "con1"), "c_new")


def test_own_org_still_works_without_assignment(tmp_path):
    """자기 조직 케이스는 배정 없이도 본다 — 기존 동작을 깨지 않는다."""
    db = _db(tmp_path)
    for role in ("auditor", "consultant", "applicant"):
        assert _can(db, _user(role), "c_own")


def test_assigned_case_ids_are_scoped_to_the_user(tmp_path):
    """목록 필터도 사람 단위여야 한다 — 상세와 목록 규칙이 어긋나면 안 된다."""
    db = _db(tmp_path)
    _assign(db, "c_new", "ops.auditor_assigned", "auditor_id", "aud1")
    assert m._assigned_case_ids(db, "aud1") == {"c_new"}
    assert m._assigned_case_ids(db, "aud2") == set()
