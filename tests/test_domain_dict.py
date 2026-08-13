"""도메인 사전 계약 검증 — 사전이 커져도 아래 성질은 깨지면 안 된다.

배경: 인도네시아 원문 서류가 들어오면서 같은 개념이 문서·화면·판정에서 제각각 다뤄졌다.
사전은 세 언어 표면형을 한 표준 키로 모으고, 그 키에 붙은 시스템 Action까지 돌려준다.
실행: <venv>/bin/python -m pytest tests/test_domain_dict.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import domain_dict as D  # noqa: E402


def test_loads_without_duplicate_surface():
    """같은 표면형이 두 표준 키에 걸리면 조회가 갈린다 — 사전 오류로 본다."""
    r = D.load()
    assert r["terms"] > 0 and r["surface"] > 0
    assert r["duplicates"] == [], "표면형 중복: %s" % r["duplicates"]


def test_three_languages_resolve_to_one_key():
    """인니어·한국어·영어가 같은 키로 모여야 동의어 통합이 성립한다."""
    for group in [("penyelia halal", "할랄감독자", "Halal Supervisor"),
                  ("ketidaksesuaian", "부적합", "non-conformity"),
                  ("bahan baku", "원료", "raw material"),
                  ("kemasan", "포장재", "packaging")]:
        keys = {D.lookup(x) for x in group}
        assert len(keys) == 1 and None not in keys, "%s → %s" % (group, keys)


def test_actions_carry_system_meaning():
    """번역만이 아니라 시스템 동작을 들고 있어야 도메인 사전이다."""
    assert D.actions("PENYELIA_HALAL").get("requires_signature") is True
    assert D.actions("KETIDAKSESUAIAN").get("creates_finding") is True
    assert D.actions("KEPUTUSAN_FATWA").get("unlocks") == "certificate_issue"
    assert D.actions("BPJPH").get("issues_certificate") is True


def test_material_axis_separates_non_ingredients():
    """세척제·포장재는 성분 판정 대상이 아니다 — 성분 사전에 섞으면 판정이 오염된다."""
    for raw in ("BAHAN BAKU", "BAHAN TAMBAHAN", "BAHAN PENOLONG"):
        key, screen = D.material_type(raw)
        assert key and screen is True, raw
    for raw in ("CLEANING AGENT", "KEMASAN"):
        key, screen = D.material_type(raw)
        assert key and screen is False, raw


def test_unknown_material_type_still_screens():
    """모르는 자재유형은 스크리닝을 건너뛰지 않는다 — 빠뜨리는 쪽이 더 위험하다."""
    key, screen = D.material_type("듣도 보도 못한 유형")
    assert key is None and screen is True


def test_doc_labels_cover_all_doc_types():
    """intake.DOC_KO/EN/ID가 이 사전에서 파생되므로 doc_type이 하나라도 비면 화면이 깨진다."""
    from app.intake import DOC_TYPES
    for lang in ("ko", "en", "id"):
        labels = D.doc_labels(lang)
        for dt in DOC_TYPES:
            assert labels.get(dt), "%s/%s 표기 없음" % (lang, dt)


def test_indonesian_filenames_map_to_doc_types():
    """인니어 원문 파일명이 서류 유형·증빙 항목으로 이어져야 한다(실제 제출 파일명)."""
    assert D.doc_type_of("Diagram alir proses produksi") == "process_flow"
    assert D.doc_type_of("Daftar bahan") == "material_list"
    assert D.doc_type_of("Pernyataan bebas babi") == "supplier_declaration"
    assert D.evidence_key_of("Catatan pembelian barang") == "purchase_log"
    assert D.evidence_key_of("Catatan hasil produksi") == "production_log"
    assert D.evidence_key_of("Denah ruang produksi") == "facility_layout"


def test_unit_dictionary_blocks_mistranslation():
    """'개'가 anjing(犬)이 되던 사고 — 단위는 표로 고정하고 금지어를 둔다."""
    assert D.actions("UNIT_PIECE").get("translate_by") == "table_only"
    forbidden = D.forbidden_translations("id")
    assert "anjing" in forbidden
    units = D.unit_surfaces()
    for u in ("kg", "liter", "buah", "sachet", "bungkus"):
        assert u in units, u


def test_lookup_does_not_partial_match():
    """부분문자열 오탐 차단 — 'san'이 'pisang' 안에 걸려 플라스틱이 바나나가 됐던 종류."""
    assert D.lookup("bah") is None
    assert D.lookup("pem") is None


def test_lookup_unknown_returns_none():
    """사전에 없으면 지어내지 않는다 — 호출부가 원문을 유지할 수 있어야 한다."""
    assert D.lookup("이런 말은 사전에 없다") is None
    assert D.label("NO_SUCH_KEY", "ko") is None
    assert D.actions("NO_SUCH_KEY") == {}
