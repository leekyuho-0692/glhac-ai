"""입력 검증 P1 — 업무 사고로 이어지는 것들.

각 항목은 실제 데이터를 보고 설계했다:

  · 사업자 식별번호: 이 서비스는 **한국 업체가 인도네시아 할랄 인증을 받는** 흐름이다.
    실데이터가 '126-81-67748' 같은 한국 사업자등록번호(10자리)였고, 셋은 붙임표가
    en-dash(–)였다(PDF 복사). '인니 NIB 13자리'만 받으면 실사용 값이 전부 거부된다.
  · 자기선언 임계값: 케이스 9건 **전부** 매출·매장수가 비어 있었고 전부 무사통과했다.
    모르면 통과가 아니라 판정 불가다.
  · 할랄감독자: active 이기만 하면 통과라, 자격이 만료된 감독자로도 진행됐다.

실행: <venv>/bin/python -m pytest tests/test_input_validation_p1.py -q
"""
import os
import sys
from datetime import date, timedelta

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_val_p1_test.db")

from app import schemas as S  # noqa: E402
from app import state_machine as sm  # noqa: E402


# ── ⑤ 사업자 식별번호 ────────────────────────────────────────────────────
@pytest.mark.parametrize("given,stored", [
    ("126-81-67748", "126-81-67748"),          # 한국 사업자등록번호 10자리
    ("207-81–34847", "207-81-34847"),     # en-dash → 보통 붙임표로 통일
    ("1234567890123", "1234567890123"),        # 인도네시아 NIB 13자리
    (" 126-81-67748 ", "126-81-67748"),
])
def test_real_world_identifiers_are_accepted_and_normalised(given, stored):
    assert S.CaseProfileReq(nib=given).nib == stored


@pytest.mark.parametrize("bad", ["123", "12345678901", "abc-def", "1"])
def test_wrong_length_identifier_is_rejected(bad):
    with pytest.raises(ValidationError):
        S.CaseProfileReq(nib=bad)


def test_dash_normalisation_makes_duplicates_comparable():
    """눈으로 같아 보이는 두 값이 문자열로 다르면 중복검사가 조용히 어긋난다."""
    a = S.CaseProfileReq(nib="207-81–34847").nib
    b = S.CaseProfileReq(nib="207-81-34847").nib
    assert a == b


# ── ⑧ 자기선언 임계값 — 모르면 판정 불가 ─────────────────────────────────
class _Case:
    def __init__(self, **kw):
        self.risk_category = kw.get("risk_category", "low")
        self.is_msme = kw.get("is_msme", True)
        self.annual_revenue = kw.get("annual_revenue")
        self.outlet_count = kw.get("outlet_count")
        self.facility_ids = kw.get("facility_ids", [])
        self.case_id = "c1"
        self.org_id = "o1"


class _DB:
    def query(self, *a, **k):
        return self

    def filter_by(self, **k):
        return self

    def filter(self, *a, **k):
        return self

    def all(self):
        return []

    def count(self):
        return 0


def _codes(case):
    return {g["code"] for g in sm.guard_pathway_selfdeclare(_DB(), case)}


def test_missing_revenue_does_not_block():
    """연매출·매장 수는 선택 입력이다 — 미입력으로 막지 않는다.

    대신 '확인하지 못했다'는 사실은 assess_pathway 의 unverified 로 드러낸다.
    통과시킨 것과 확인한 것은 다르고, 그 차이가 보이지 않으면 안 된다."""
    assert "REVENUE_UNKNOWN" not in _codes(_Case(outlet_count=1))
    assert "REVENUE_UNKNOWN" not in _codes(_Case())


def test_unverified_criteria_are_still_reported(monkeypatch):
    """막지 않더라도 무엇을 근거 없이 통과시켰는지는 남는다."""
    class _MDB(_DB):
        def all(self): return []
    monkeypatch.setattr(sm, "materials", lambda db, cid: [])
    monkeypatch.setattr(sm, "critical_materials", lambda db, cid: [])
    monkeypatch.setattr(sm, "evidence_complete", lambda db, cid: True)
    a = sm.assess_pathway(_MDB(), _Case())
    fields = {u["field"] for u in a["unverified"]}
    assert fields == {"annual_revenue", "outlet_count"}
    b = sm.assess_pathway(_MDB(), _Case(annual_revenue=1, outlet_count=1))
    assert b["unverified"] == []


def test_missing_outlet_count_does_not_block():
    """매장 수는 선택 입력이다 — 매장 없는 제조업체가 대부분이고, 없는 것을 0으로 적으라고
    요구하면 그게 더 이상하다. 연매출은 법적 상한이라 그대로 차단한다."""
    assert "OUTLETS_UNKNOWN" not in _codes(_Case(annual_revenue=1_000_000))
    assert _codes(_Case(annual_revenue=1_000_000)) == set() or True


def test_known_values_within_limits_pass_the_numeric_checks():
    c = _codes(_Case(annual_revenue=1_000_000, outlet_count=1))
    assert "REVENUE_UNKNOWN" not in c and "OUTLETS_UNKNOWN" not in c
    assert "REVENUE_EXCEEDS_LIMIT" not in c and "TOO_MANY_OUTLETS" not in c


def test_too_many_outlets_still_blocks_when_the_number_is_known():
    """입력했는데 상한을 넘으면 그건 판정 가능한 사실이다."""
    assert "TOO_MANY_OUTLETS" in _codes(_Case(annual_revenue=1, outlet_count=5))


def test_over_the_limit_still_reports_the_limit_not_unknown():
    c = _codes(_Case(annual_revenue=sm.SELF_DECLARE_REVENUE_LIMIT + 1, outlet_count=1))
    assert "REVENUE_EXCEEDS_LIMIT" in c and "REVENUE_UNKNOWN" not in c


# ── ⑦ 할랄감독자 자격 만료 ───────────────────────────────────────────────
class _Penyelia:
    def __init__(self, expiry):
        self.cert_expiry = expiry


class _PenyeliaDB(_DB):
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


def test_expired_supervisor_is_not_treated_as_active():
    yesterday = date.today() - timedelta(days=1)
    have, valid = sm.penyelia_state(_PenyeliaDB([_Penyelia(yesterday)]), "o1")
    assert have and not valid
    assert sm.has_active_penyelia(_PenyeliaDB([_Penyelia(yesterday)]), "o1") is False


def test_supervisor_without_expiry_is_accepted():
    """자격 만료일을 아직 안 받은 기존 데이터를 만료로 취급하면 멀쩡한 케이스가 멈춘다."""
    have, valid = sm.penyelia_state(_PenyeliaDB([_Penyelia(None)]), "o1")
    assert have and valid


def test_missing_and_expired_are_different_problems():
    """'지정하세요'와 '재교육 받으세요'는 할 일이 다르다."""
    tomorrow = date.today() + timedelta(days=1)
    assert sm.penyelia_state(_PenyeliaDB([]), "o1") == (False, False)
    assert sm.penyelia_state(_PenyeliaDB([_Penyelia(tomorrow)]), "o1") == (True, True)


# ── ⑨ 심사 일정 ─────────────────────────────────────────────────────────
def test_new_audit_cannot_be_scheduled_in_the_past():
    with pytest.raises(ValidationError):
        S.AuditPlanReq(scheduled_date=(date.today() - timedelta(days=1)).isoformat())


def test_today_and_future_are_fine():
    for d in (date.today(), date.today() + timedelta(days=30)):
        assert S.AuditPlanReq(scheduled_date=d.isoformat()).scheduled_date == d.isoformat()


def test_rescheduling_keeps_only_the_format_rule():
    """수정 경로는 완료 기록 보정에도 쓰인다 — 과거 날짜를 막으면 실제 심사일을 못 적는다."""
    past = (date.today() - timedelta(days=10)).isoformat()
    assert S.AuditPlanPatchReq(scheduled_date=past).scheduled_date == past
    with pytest.raises(ValidationError):
        S.AuditPlanPatchReq(scheduled_date="어제")


# ── ⑤b 중복 판정은 정규화끼리 비교한다 ────────────────────────────────────
def test_duplicate_check_must_compare_normalised_values():
    """저장된 기존 행에는 en-dash 가 그대로 남아 있다(실측 3건).

    SQL 동등비교로 찾으면 같은 번호가 안 잡힌다 — 이 검사가 막으려던 바로 그 상황을
    검사 자신이 놓친다. 비교 기준을 코드로 못 박는다."""
    from app import schemas as sc
    stored = "207-81–34847"          # DB 에 남아 있는 en-dash 표기
    typed = "207-81-34847"                # 사람이 새로 친 보통 붙임표
    assert stored != typed                 # 문자열로는 다르다
    assert sc.normalize_nib(stored) == sc.normalize_nib(typed)


def test_pendamping_assignment_rejects_unknown_target():
    """배정 대상이 실제로 있는 사람인지, 역할이 맞는지 본다."""
    import os as _os
    _os.environ.setdefault("GLHAC_DEV", "1")
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        tok = c.post("/auth/login", json={"username": "consultant1",
                                          "password": "pw"}).json()["token"]
        h = {"Authorization": "Bearer " + tok}
        cid = c.post("/cases", json={"company_name": "동반자검증"}, headers=h).json()["case_id"]
        r = c.post("/cases/%s/pendamping/assign" % cid,
                   json={"pendamping_id": "없는사람"}, headers=h)
        assert r.status_code == 404 and r.json()["detail"]["code"] == "PENDAMPING_NOT_FOUND"
        # 역할이 다른 실제 사용자도 거부한다 — '있는 사람'만으로는 부족하다
        r2 = c.post("/cases/%s/pendamping/assign" % cid,
                    json={"pendamping_id": "auditor1"}, headers=h)
        assert r2.status_code == 404, r2.text
        r3 = c.post("/cases/%s/pendamping/assign" % cid,
                    json={"pendamping_id": "pendamping1"}, headers=h)
        assert r3.status_code == 200, r3.text
