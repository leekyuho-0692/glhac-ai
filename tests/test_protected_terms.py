"""번역이 바꾸면 안 되는 도메인 고유명사.

배경(실측): 같은 문장을 3회 번역시켰더니 1회에서 'penyelia halal'(할랄감독자)이
'pemegang kehalalan'이라는 없는 말로 바뀌었다. 대문자 약어(BPJPH·SJPH·SIHALAL)는
살아남는데 소문자 복합어인 직책·제도 이름이 번역 대상으로 오인돼 창작된다.
심사 문서에서 기관·직책 이름이 바뀌면 그 문서는 틀린 문서다.

지키는 성질
  · 보호 목록은 사전(domain_dict)에서 파생한다 — 표를 또 손으로 만들지 않는다.
  · 원문에 없던 용어는 요구하지 않는다(없는 말을 넣으라는 뜻이 되면 더 나쁘다).
  · 사전의 표기형(surface)은 건드리지 않는다 — 서류 분류 매칭이 흔들린다.

실행: <venv>/bin/python -m pytest tests/test_protected_terms.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import domain_dict as dd  # noqa: E402


@pytest.mark.parametrize("term", ["BPJPH", "SIHALAL", "SJPH", "LPH", "PPH", "SEHATI",
                                  "Penyelia Halal", "Majelis Ulama Indonesia"])
def test_core_institution_and_role_names_are_protected(term):
    """기관·직책·제도 이름은 보호된다 — 여기가 비면 문서가 조용히 틀려진다."""
    assert term in dd.protected_terms("id"), term


def test_protected_terms_come_from_the_dictionary():
    """목록은 사전에서 파생한다 — 손으로 유지하는 표를 또 만들지 않는다."""
    terms = dd.protected_terms("id")
    # ROLE/ORG 축의 인니어 라벨이 그대로 들어와 있어야 한다
    assert "Lembaga Pemeriksa Halal" in terms      # LPH (ORG)
    assert "Tim Manajemen Halal" in terms          # TIM_HALAL (ROLE)


def test_longer_terms_come_first():
    """긴 용어가 짧은 용어를 포함한다(MUI ⊂ LPPOM MUI) — 긴 것부터 봐야 오검출이 없다."""
    terms = dd.protected_terms("id")
    assert terms.index("LPPOM MUI") < terms.index("MUI")
    assert all(len(a) >= len(b) for a, b in zip(terms, terms[1:]))


def test_missing_protected_detects_a_fabricated_translation():
    """실제로 났던 사고를 잡는다 — penyelia halal → pemegang kehalalan."""
    src = "Manual SJPH dan surat penetapan penyelia halal disertakan."
    bad = "Manual SJPH dan surat penetapan pemegang kehalalan disertakan."
    assert dd.missing_protected(src, bad, "id") == ["Penyelia Halal"]


def test_good_translation_raises_nothing():
    """용어가 보존된 번역은 재시도를 부르지 않는다."""
    src = "Manual SJPH dan penyelia halal, diperiksa oleh LPH."
    good = "Manual SJPH dan penyelia halal, diperiksa oleh LPH."
    assert dd.missing_protected(src, good, "id") == []


def test_terms_absent_from_the_source_are_not_required():
    """원문에 없던 용어를 요구하지 않는다 — 없는 말을 넣으라는 뜻이 되면 더 나쁘다."""
    assert dd.missing_protected("teks biasa tanpa istilah", "plain translated text", "id") == []


def test_detection_is_case_insensitive():
    """표기 대소문자가 흔들려도 보존으로 본다 — 대소문자는 오역이 아니다."""
    assert dd.missing_protected("dokumen sjph", "dokumen SJPH", "id") == []


def test_english_side_also_has_a_list():
    """영어 화면도 같은 보호를 받는다."""
    assert "BPJPH" in dd.protected_terms("en")


def test_surface_forms_are_untouched():
    """표기형 사전은 그대로여야 한다 — 여기 손대면 서류 분류가 흔들린다(회귀 방지)."""
    assert dd.doc_type_of("Diagram alir proses produksi") == "process_flow"
    assert dd.doc_type_of("Catatan pembelian barang") == "other"
