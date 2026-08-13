"""필수 서류는 신청 경로에 따라 다르다 — 경로별 요건표 검증.

배경: 필수 목록이 한국 정규 경로 기준 7종으로 고정돼 있어, 인도네시아 소규모
자기선언(SEHATI) 완성본인 CV. CITRA PRATAMA가 '서류 부족 2건'으로 표시됐다.
자료가 부족한 게 아니라 체크리스트가 그 경로에 맞지 않았다.

이 테스트가 지키는 성질
  · 면제된 서류는 '없음'이 아니라 '해당 없음'으로 남는다(누락과 구분).
  · 대체 충족은 무엇으로 갈음했는지 반드시 드러난다.
  · 모르는 경로는 넓은 쪽(정규)을 쓴다 — 요구를 빠뜨리는 쪽이 더 위험하다.

실행: <venv>/bin/python -m pytest tests/test_doc_checklist_pathway.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.intake import (DOC_ALT_SATISFY, DOC_NOT_APPLICABLE,  # noqa: E402
                        REQUIRED_DOCS, required_docs)


def test_self_declare_drops_factory_registration():
    """인니 소규모 자기선언은 NIB가 시설 등록을 갈음한다 — 공장등록증을 요구하지 않는다."""
    req = required_docs("self_declare")
    assert "factory_registration" not in req
    assert "nib_business_license" in req, "NIB까지 빠지면 사업자 확인 자체가 사라진다"


def test_reguler_keeps_full_list():
    """정규 경로는 기존 7종 그대로 — 이 변경이 한국 케이스 요건을 낮추면 안 된다."""
    assert required_docs("reguler") == REQUIRED_DOCS
    assert "factory_registration" in required_docs("reguler")


def test_unknown_pathway_falls_back_to_widest():
    """경로 미정(undetermined)·오타·None은 정규 기준. 덜 요구하는 쪽으로 폴백하면 누락이 통과한다."""
    for pw in (None, "", "undetermined", "오타경로"):
        assert required_docs(pw) == REQUIRED_DOCS, pw


def test_exempt_doc_carries_reason():
    """면제는 사유가 있어야 한다 — 사유 없이 사라지면 심사자가 면제인지 누락인지 모른다."""
    na = DOC_NOT_APPLICABLE["self_declare"]
    assert "factory_registration" in na
    assert na["factory_registration"].strip(), "면제 사유가 비어 있다"


def test_alternative_satisfaction_names_its_basis():
    """대체 충족은 '무엇으로' 갈음했는지 밝힌다 — 업로드 충족과 구분되지 않으면 안 된다."""
    alt = DOC_ALT_SATISFY["self_declare"]["halal_certificate"]
    assert alt["by"] == "supplier_cert_no"
    assert alt["note"].strip()


def test_no_pathway_exempts_core_evidence():
    """어떤 경로도 원재료·공정·제품·SJPH는 면제하지 못한다 — 판정의 근거 자체다."""
    core = {"product_list", "process_flow", "material_list", "sjph_manual"}
    for pw in ("reguler", "self_declare", "undetermined"):
        assert core <= set(required_docs(pw)), pw


def test_exempt_and_required_do_not_overlap():
    """같은 서류가 필수이면서 해당 없음일 수 없다 — 표가 서로 어긋나면 화면이 모순된다."""
    for pw, na in DOC_NOT_APPLICABLE.items():
        assert not (set(na) & set(required_docs(pw))), pw


def test_alternative_targets_are_actually_required():
    """요구하지도 않는 서류에 대체 충족을 달아두면 죽은 규칙이 된다."""
    for pw, alt in DOC_ALT_SATISFY.items():
        assert set(alt) <= set(required_docs(pw)), pw


# ── 관할·규모 축 ────────────────────────────────────────────────────────
# 경로만으로는 부족했다. CV. CITRA PRATAMA는 육류·가금 원료 때문에 판정이 reguler로
# 나오는데, 인도네시아 소규모 사업자에게는 '공장등록증'이라는 서류 제도 자체가 없다.
from app.intake import doc_requirements  # noqa: E402


def test_indonesian_msme_exempt_from_factory_registration_even_on_reguler():
    """정규 경로여도 인니 소규모는 공장등록증을 낼 수 없다 — NIB가 시설 등록을 겸한다."""
    r = doc_requirements("reguler", "Indonesia", True)
    assert "factory_registration" not in r["required"]
    assert r["not_applicable"]["factory_registration"].strip()


def test_korean_company_still_needs_factory_registration():
    """관할 규칙이 한국 케이스로 새면 안 된다 — 실제 요건을 낮추는 사고가 된다."""
    r = doc_requirements("reguler", "대한민국", True)
    assert "factory_registration" in r["required"]
    assert not r["not_applicable"]


def test_indonesian_large_company_still_needs_facility_document():
    """면제는 소규모에 한한다 — 규모가 크면 시설 서류를 요구한다."""
    assert "factory_registration" in doc_requirements("reguler", "Indonesia", False)["required"]


def test_unknown_country_does_not_exempt():
    """국가 미상은 면제하지 않는다 — 모를 때는 넓게 요구하는 쪽이 안전하다."""
    for ctry in (None, "", "어느 나라"):
        assert "factory_registration" in doc_requirements("reguler", ctry, True)["required"], ctry


def test_alt_satisfaction_drops_when_doc_not_required():
    """요구되지 않게 된 서류에 대체 충족이 남으면 죽은 규칙이다."""
    r = doc_requirements("self_declare", "Indonesia", True)
    assert set(r["alt"]) <= set(r["required"])


def test_exempt_reason_always_present():
    """면제에는 예외 없이 사유가 붙는다."""
    for pw, ctry, ms in [("self_declare", "Indonesia", True), ("reguler", "Indonesia", True),
                         ("self_declare", None, None)]:
        for dt, why in doc_requirements(pw, ctry, ms)["not_applicable"].items():
            assert why and why.strip(), (pw, ctry, ms, dt)
