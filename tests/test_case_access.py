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


# ── 인증서 발급 사전조건 ─────────────────────────────────────────────────
# 발급 가드에 상태 검사가 없어 현장심사를 건너뛴 케이스에도 인증서가 나갔다(실측:
# consultant_review 에서 발급). 게다가 발급 함수의 상태 전이는 조용히 건너뛰어져
# '인증서는 있는데 진행 단계는 심사 중'인 케이스가 남았다.

def _case(db, status, **kw):
    c = models.CaseApplication(case_id="c_iss", org_id=OWN, company_name="발급 검증",
                               status=status, fatwa_status=kw.get("fatwa", "approved"),
                               scope_frozen=kw.get("frozen", True))
    db.add(c)
    db.commit()
    return c


def test_issue_blocked_before_review_is_done(tmp_path):
    """심사 단계를 마치지 않은 케이스는 발급되지 않는다."""
    db = _db(tmp_path)
    c = _case(db, "consultant_review")
    with pytest.raises(HTTPException) as e:
        m._issue_guards(db, c, c.case_id)
    assert e.value.detail["code"] == "STATE_NOT_READY"
    assert e.value.detail["status"] == "consultant_review"


@pytest.mark.parametrize("status", ["fatwa_approved", "committee_verification"])
def test_issue_allowed_from_legitimate_states(tmp_path, status):
    """정규(파트와 승인)·자기선언(위원회 검증) 두 경로 모두에서 발급이 열린다."""
    db = _db(tmp_path)
    c = _case(db, status)
    m._issue_guards(db, c, c.case_id)      # 예외가 없으면 통과


def test_ready_states_come_from_the_transition_table(tmp_path):
    """발급 가능 상태를 손으로 적지 않는다 — 전이표가 바뀌면 가드도 따라가야 한다."""
    import app.state_machine as sm2
    assert m._issue_ready_states() == {
        f for f, tos in sm2.TRANSITIONS.items() if "certificate_issued" in tos}


def test_already_issued_case_is_not_blocked(tmp_path):
    """이미 발급된 케이스는 상태 검사로 막지 않는다 — 재조회가 실패하면 안 된다."""
    db = _db(tmp_path)
    c = _case(db, "certificate_issued")
    m._issue_guards(db, c, c.case_id)


def test_fatwa_and_scope_guards_still_apply(tmp_path):
    """상태가 맞아도 파트와 승인·범위 동결이 없으면 발급되지 않는다(기존 가드 유지)."""
    db = _db(tmp_path)
    c = _case(db, "fatwa_approved", fatwa="provisional")
    with pytest.raises(HTTPException) as e:
        m._issue_guards(db, c, c.case_id)
    assert e.value.detail["code"] == "FATWA_NOT_APPROVED"
    c.fatwa_status, c.scope_frozen = "approved", False
    db.commit()
    with pytest.raises(HTTPException) as e2:
        m._issue_guards(db, c, c.case_id)
    assert e2.value.detail["code"] == "SCOPE_NOT_FROZEN"


# ── 조직 자원 접근 — 케이스가 열리면 그 업체 정보도 열려야 한다 ──────────
# 배경(리허설 실측): 배정된 컨설턴트가 케이스 상세는 200 인데 같은 업체의 할랄감독자·
# 시설·SIHALAL 신원은 403 이었다. 케이스 접근은 배정을 인정하는데 조직 자원은 org_id 를
# 직접 비교했기 때문이다. 그 결과 신규 가입 업체는 정규 경로(SIHALAL 신원 확인 필요)로
# 진행할 수 없었다.

def _org_ok(db, user, org):
    return m._org_access_ok(db, user, org)


def test_own_org_is_always_visible(tmp_path):
    db = _db(tmp_path)
    assert _org_ok(db, _user("consultant"), OWN)


@pytest.mark.parametrize("role", ["admin", "operator", "fatwa_liaison"])
def test_certifier_roles_see_every_org(tmp_path, role):
    """인증기관 역할은 조직을 넘어 본다 — 판정이 업무다."""
    db = _db(tmp_path)
    assert _org_ok(db, _user(role), OTHER)


def test_assignment_opens_the_company_behind_the_case(tmp_path):
    """배정받으면 그 케이스의 업체 정보도 열린다 — 이게 막혀 심사가 멈췄다."""
    db = _db(tmp_path)
    u = _user("consultant", "con1")
    assert not _org_ok(db, u, OTHER)
    _assign(db, "c_new", m.CONSULTANT_ASSIGN_ACTION, "consultant_id", "con1")
    assert _org_ok(db, u, OTHER)


def test_assignment_does_not_open_unrelated_orgs(tmp_path):
    """배정은 그 케이스의 조직만 연다 — 전체 개방이 되면 안 된다."""
    db = _db(tmp_path)
    db.add(models.CaseApplication(case_id="c_third", org_id="org_third",
                                  company_name="제3의 업체"))
    db.commit()
    _assign(db, "c_new", m.CONSULTANT_ASSIGN_ACTION, "consultant_id", "con1")
    u = _user("consultant", "con1")
    assert _org_ok(db, u, OTHER)
    assert not _org_ok(db, u, "org_third")


def test_auditor_assignment_also_opens_the_org(tmp_path):
    """오디터도 마찬가지 — 현장심사에 업체 시설 정보가 필요하다."""
    db = _db(tmp_path)
    u = _user("auditor", "aud1")
    assert not _org_ok(db, u, OTHER)
    _assign(db, "c_new", "ops.auditor_assigned", "auditor_id", "aud1")
    assert _org_ok(db, u, OTHER)


def test_empty_org_is_refused(tmp_path):
    """org_id 가 비면 통과시키지 않는다 — 빈 값이 만능 열쇠가 되면 안 된다."""
    db = _db(tmp_path)
    assert not _org_ok(db, _user("admin"), "")
    assert not _org_ok(db, _user("consultant"), None)
