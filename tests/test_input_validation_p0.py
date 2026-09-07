"""입력 검증 P0 — 값이 들어오는 자리에서 막는다.

배경: 종전에는 입력을 거의 그대로 받고 마지막 상태 전이 가드에서만 걸렀다(28개 상태 중
9개에만 가드). schemas.py 622줄에 실제 제약이 5개뿐이었다. 그래서:

  · 원재료 유형·출처가 자유 문자열이라 오타가 그대로 저장됐다(사전에 코드가 있는데 대조 안 함)
  · 날짜가 문자열로 저장돼, 나중에 date.fromisoformat() 으로 읽는 쪽이 죽었다
    (만료 경보 main.py:2802, 인증서 유효성 판정 main.py:1065)
  · 필수 문자열에 공백만 넣어도 통과했다
  · 비밀번호 정책이 전혀 없었다

실행: <venv>/bin/python -m pytest tests/test_input_validation_p0.py -q
"""
import os
import sys

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_val_test.db")

from app import domain_dict as dd  # noqa: E402
from app import schemas as S  # noqa: E402


# ── ① 원재료 코드는 사전과 대조한다 ────────────────────────────────────────
@pytest.mark.parametrize("bad", ["엉터리", "0", "raw2", "그냥아무말"])
def test_unknown_material_type_is_rejected(bad):
    with pytest.raises(ValidationError):
        S.MaterialCreate(name="설탕", mat_type=bad)


@pytest.mark.parametrize("raw,code", [("raw", "raw"), ("Raw Material", "raw"),
                                      ("sanitizer", "cleaning"), ("sanitiser", "cleaning"),
                                      ("raw material 2", "raw"),   # 서류 표기 흔들림도 사전이 잡는다
                                      ("원재료", "raw"), ("세척제", "cleaning")])
def test_known_material_type_is_normalised_to_the_canonical_code(raw, code):
    assert S.MaterialCreate(name="설탕", mat_type=raw).mat_type == code


@pytest.mark.parametrize("bad", ["xx", "동물성", "unknown_source"])
def test_unknown_material_source_is_rejected(bad):
    with pytest.raises(ValidationError):
        S.MaterialCreate(name="설탕", source=bad)


def test_material_patch_uses_the_same_code_rule():
    """수정 경로만 열려 있으면 검증을 우회할 수 있다 — 생성과 같은 규칙을 쓴다."""
    assert S.MaterialPatch(mat_type="Raw Material").mat_type == "raw"
    with pytest.raises(ValidationError):
        S.MaterialPatch(source="엉터리")


def test_code_list_lives_in_the_dictionary_only():
    """코드 목록이 여러 곳에 복제되면 한쪽만 늘어난다 — 사전이 정본이다."""
    from app import main as m
    assert set(c for c, _ in m._ENUMS["material_type"]) == set(dd.MATERIAL_TYPE_CODES)
    assert set(c for c, _ in m._ENUMS["material_source"]) == set(dd.MATERIAL_SOURCE_CODES)
    assert m._mat_type_code("sanitizer") == dd.material_type_code("sanitizer") == "cleaning"


# ── ② 날짜는 ISO 만 받는다 ────────────────────────────────────────────────
@pytest.mark.parametrize("model,field", [
    (S.CaseProfileReq, "due_date"), (S.PenyeliaCreate, "cert_expiry"),
    (S.FindingReq, "due_date"), (S.AuditPlanReq, "scheduled_date"),
    (S.AuditPlanPatchReq, "scheduled_date"), (S.CarSubmitReq, "due_date"),
    (S.RegulationUpsertReq, "effective_date"),
])
@pytest.mark.parametrize("bad", ["2026-13-01", "2026/09/10", "내년", "2026-02-30", "9월10일"])
def test_non_iso_dates_are_rejected(model, field, bad):
    base = {"name": "X", "finding": "X", "description": "X", "title": "X"}
    kw = {k: v for k, v in base.items() if k in model.model_fields}
    kw[field] = bad
    with pytest.raises(ValidationError):
        model(**kw)


@pytest.mark.parametrize("given,stored", [("20260910", "2026-09-10"),
                                         ("2026-W37-1", "2026-09-07"),
                                         ("2026-09-10", "2026-09-10")])
def test_iso_shorthand_is_normalised_for_storage(given, stored):
    """3.11 fromisoformat 은 축약형도 받는다 — 저장 형식은 하나로 통일한다."""
    assert S.CaseProfileReq(due_date=given).due_date == stored


def test_stored_dates_can_always_be_parsed_back():
    """이 검증의 목적 — 저장된 값을 나중에 date.fromisoformat() 으로 읽는 코드가 있다."""
    from datetime import date
    v = S.CaseProfileReq(due_date="2026-12-01").due_date
    assert date.fromisoformat(v) == date(2026, 12, 1)
    assert S.CaseProfileReq(due_date="").due_date is None      # 미입력은 허용


# ── ③ 필수 문자열에 공백만 넣는 것은 안 넣은 것이다 ────────────────────────
@pytest.mark.parametrize("model,field", [(S.ProductCreate, "name"),
                                         (S.MaterialCreate, "name"),
                                         (S.PenyeliaCreate, "name"),
                                         (S.CaseProfileReq, "company_name")])
def test_blank_required_text_is_rejected(model, field):
    with pytest.raises(ValidationError):
        model(**{field: "   "})


def test_surrounding_spaces_are_trimmed_not_rejected():
    assert S.ProductCreate(name="  제품A  ").name == "제품A"


# ── ④ 비밀번호 정책 ──────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", ["pw", "1234567", "aaaaaaaa", ""])
def test_weak_password_is_rejected(bad):
    with pytest.raises(ValidationError):
        S.RegisterReq(username="u", password=bad)


def test_reasonable_password_passes():
    assert S.RegisterReq(username="u", password="halal2026").password == "halal2026"


def test_admin_paths_use_the_same_policy():
    """가입만 막고 관리자 생성이 뚫려 있으면 정책이 아니다."""
    with pytest.raises(ValidationError):
        S.AdminUserReq(username="u", password="pw", role="auditor")
    with pytest.raises(ValidationError):
        S.AdminUserPatchReq(password="pw")
    assert S.AdminUserPatchReq(password=None).password is None   # 변경 안 함은 허용


# ── ⑤ 사람이 읽을 수 있어야 검증이다 ──────────────────────────────────────
def test_validation_error_speaks_the_app_convention():
    """FastAPI 기본 422 는 detail 이 배열이라, {code} 만 보는 화면에서 사유가 빈칸이 된다.

    잘못 적었다는 건 아는데 무엇이 잘못됐는지 못 보는 상태가 가장 나쁘다."""
    import os as _os
    _os.environ.setdefault("GLHAC_DEV", "1")
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        tok = c.post("/auth/login", json={"username": "consultant1",
                                          "password": "pw"}).json()["token"]
        h = {"Authorization": "Bearer " + tok}
        cid = c.post("/cases", json={"company_name": "검증테스트"}, headers=h).json()["case_id"]
        r = c.patch("/cases/%s/profile" % cid, json={"due_date": "내년"}, headers=h)
        assert r.status_code == 422
        d = r.json()["detail"]
        assert isinstance(d, dict), "detail 이 배열이면 화면이 사유를 못 읽는다"
        assert d["code"] == "VALIDATION_ERROR"
        assert d["field"] == "due_date"
        assert "YYYY-MM-DD" in d["message"]
        assert not d["message"].startswith("Value error,"), "pydantic 접두어가 그대로 노출됐다"
