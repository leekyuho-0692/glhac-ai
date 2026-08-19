"""컨설턴트–클라이언트 유치 관계 — 영업 인센티브의 근거.

배경: 컨설턴트가 영업해서 클라이언트가 들어오는 게 선행 워크플로다. 그런데 시스템은
컨설턴트를 오디터와 같은 '배정' 규칙으로 묶어, 자기가 데려온 고객을 운영자가 배정해줄
때까지 보지 못했다. 둘은 성격이 다르다.
  · 오디터  — 심사기관이 케이스마다 배정한다.
  · 컨설턴트 — 업체가 들어올 때 이미 관계가 있다(초대 코드 또는 운영자 지정).
그리고 이 관계는 접근 권한만이 아니라 수수료를 누구에게 줄지의 근거이기도 하다.

실행: <venv>/bin/python -m pytest tests/test_consultant_referral.py -q
"""
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_referral_test.db")

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.main as m  # noqa: E402
import app.models as models  # noqa: E402

CON = "con_kim"          # 영업한 컨설턴트
OTHER = "con_other"      # 남의 컨설턴트
CLIENT_ORG = "org_client"


@pytest.fixture
def db(tmp_path):
    eng = create_engine("sqlite:///%s" % (tmp_path / "ref.db"))
    models.Base.metadata.create_all(bind=eng)
    s = sessionmaker(bind=eng)()
    s.add(models.User(user_id=CON, username="kim", password_hash="x", role="consultant",
                      org_id="org_demo"))
    s.add(models.User(user_id=OTHER, username="lee", password_hash="x", role="consultant",
                      org_id="org_demo"))
    s.add(models.ConsultantProfile(consultant_id=CON, display_name="김영업",
                                   commission_rate=10.0))
    s.add(models.ConsultantProfile(consultant_id=OTHER, display_name="이영업"))
    # 김영업이 데려온 업체
    s.add(models.Org(org_id=CLIENT_ORG, name="PT. KLIEN", consultant_id=CON,
                     consultant_linked_at=datetime.utcnow()))
    s.add(models.CaseApplication(case_id="c_client", org_id=CLIENT_ORG,
                                 company_name="PT. KLIEN"))
    # 아무도 데려오지 않은 업체
    s.add(models.Org(org_id="org_walkin", name="PT. WALKIN"))
    s.add(models.CaseApplication(case_id="c_walkin", org_id="org_walkin",
                                 company_name="PT. WALKIN"))
    s.commit()
    return s


def _user(uid, role="consultant"):
    return {"uid": uid, "role": role, "org_id": "org_demo", "username": uid}


def _can_case(db, user, case_id):
    try:
        m._assert_case_access(db, user, db.get(models.CaseApplication, case_id))
        return True
    except HTTPException:
        return False


# ── 유치 관계가 곧 접근 근거 ────────────────────────────────────────────

def test_referring_consultant_sees_the_client_without_assignment(db):
    """영업으로 데려온 고객은 배정을 기다리지 않는다 — 이게 막혀 있었다."""
    assert _can_case(db, _user(CON), "c_client")


def test_referring_consultant_sees_the_company_info(db):
    """케이스만 열리고 업체 정보가 막히면 심사 준비를 못 한다."""
    assert m._org_access_ok(db, _user(CON), CLIENT_ORG)


def test_other_consultant_is_blocked(db):
    """남이 영업한 고객은 보이지 않는다 — 수수료가 걸린 관계다."""
    assert not _can_case(db, _user(OTHER), "c_client")
    assert not m._org_access_ok(db, _user(OTHER), CLIENT_ORG)


def test_walkin_client_belongs_to_nobody(db):
    """유치자 없는 업체는 어느 컨설턴트에게도 자동으로 열리지 않는다."""
    assert not _can_case(db, _user(CON), "c_walkin")


def test_auditor_still_needs_assignment(db):
    """오디터는 유치 개념이 없다 — 배정 규칙 그대로."""
    aud = {"uid": "aud1", "role": "auditor", "org_id": "org_demo", "username": "aud1"}
    assert not _can_case(db, aud, "c_client")


def test_certifier_roles_are_unaffected(db):
    """인증기관 역할은 전과 같이 전 케이스를 본다."""
    for role in ("admin", "operator", "fatwa_liaison"):
        assert _can_case(db, _user("x", role), "c_client")


def test_client_list_includes_referred_orgs(db):
    """내 고객 목록에 유치 업체가 들어온다."""
    assert m._my_client_org_ids(db, CON) == {CLIENT_ORG}
    assert m._my_client_org_ids(db, OTHER) == set()


# ── 초대 코드 ───────────────────────────────────────────────────────────

def _invite(db, **kw):
    kw.setdefault("code", "AAAA-BBBB")
    kw.setdefault("consultant_id", CON)
    kw.setdefault("max_uses", 1)
    kw.setdefault("used_count", 0)
    kw.setdefault("expires_at", datetime.utcnow() + timedelta(days=30))
    inv = models.ConsultantInvite(**kw)
    db.add(inv)
    db.commit()
    return inv


def test_valid_code_shows_who_referred(db):
    """가입 전 화면이 '누구 담당으로 들어가는지'를 보여준다."""
    _invite(db, company_name="PT. KLIEN")
    r = m.check_invite("AAAA-BBBB", db=db)
    assert r["valid"] and r["consultant"] == "김영업"


def test_revoked_code_is_refused(db):
    """회수된 코드로는 들어오지 못한다 — 관계와 수수료가 틀어진다."""
    _invite(db, revoked_at=datetime.utcnow())
    assert m.check_invite("AAAA-BBBB", db=db) == {"valid": False, "reason": "REVOKED"}


def test_expired_code_is_refused(db):
    _invite(db, expires_at=datetime.utcnow() - timedelta(days=1))
    assert m.check_invite("AAAA-BBBB", db=db)["reason"] == "EXPIRED"


def test_used_up_code_is_refused(db):
    """1회용 코드를 다시 쓰지 못한다."""
    _invite(db, max_uses=1, used_count=1)
    assert m.check_invite("AAAA-BBBB", db=db)["reason"] == "USED_UP"


def test_unknown_code_is_refused(db):
    assert m.check_invite("ZZZZ-ZZZZ", db=db)["reason"] == "NOT_FOUND"


def test_code_check_does_not_leak_contact_details(db):
    """가입 전 화면은 표기명만 본다 — 연락처·계좌가 새면 안 된다."""
    p = db.get(models.ConsultantProfile, CON)
    p.phone, p.bank_account = "010-0000-0000", "1234567890"
    db.commit()
    _invite(db)
    r = m.check_invite("AAAA-BBBB", db=db)
    assert set(r) == {"valid", "company_name", "consultant"}


# ── 수수료 근거 ─────────────────────────────────────────────────────────

def _invoice(db, case_id, amount, status="paid"):
    db.add(models.Invoice(case_id=case_id, amount=amount, total=amount, status=status))
    db.commit()


def test_only_paid_invoices_count(db):
    """미결제 청구서는 실적이 아니다 — 받지도 않은 돈에 수수료를 줄 수 없다."""
    _invoice(db, "c_client", 1_000_000, "paid")
    _invoice(db, "c_client", 9_000_000, "waiting_payment")
    rows, base = m._commission_base(db, CON)
    assert base == 1_000_000 and len(rows) == 1


def test_other_consultants_clients_are_excluded(db):
    """남의 고객 매출은 내 실적이 아니다."""
    _invoice(db, "c_walkin", 5_000_000, "paid")
    _, base = m._commission_base(db, CON)
    assert base == 0


def test_commission_needs_a_rate(db):
    """요율이 없으면 금액을 지어내지 않는다 — 운영자가 정해야 나온다."""
    _invoice(db, "c_client", 1_000_000, "paid")
    r = m._commission_report(db, OTHER, None, None)      # 이영업은 요율 미설정
    assert r["commission_rate"] is None and r["commission_amount"] is None


def test_commission_is_computed_from_the_rate(db):
    _invoice(db, "c_client", 5_000_000, "paid")
    r = m._commission_report(db, CON, None, None)
    assert r["base_amount"] == 5_000_000 and r["commission_amount"] == 500_000


def test_payout_refuses_without_a_rate(db):
    """요율 없이 지급 기록을 만들지 않는다."""
    with pytest.raises(HTTPException) as e:
        m.create_payout(body=type("B", (), {"consultant_id": OTHER,
                                            "period_from": "2026-01-01",
                                            "period_to": "2026-12-31", "note": None})(),
                        user={"uid": "ops", "role": "operator", "org_id": "org_demo"},
                        db=db)
    assert e.value.detail["code"] == "RATE_NOT_SET"
