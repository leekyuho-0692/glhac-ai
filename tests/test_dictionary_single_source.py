"""라벨은 사전 한 곳에서 나온다 — 표를 두 벌 두면 한쪽만 고쳐진다.

배경(실측): 같은 개념의 번역이 코드 여러 곳에 흩어져 있었다.
  · 유래(_SOURCE): 영어·인니어는 13개인데 한국어만 6개 — 화면에 'dairy'·'unknown'
    같은 코드값이 그대로 나왔다.
  · 같은 코드가 화면마다 다른 이름: animal 이 '동물'과 '동물성', unknown 이 '미상'과
    '출처불명', 증빙 12개 중 11개가 다른 표기.
  · 판정 상태는 사전과 코드가 서로 다른 값을 갖고 있었다.
심사 화면에서 같은 개념이 다르게 불리면 심사자가 다른 것으로 읽는다.

이 테스트가 지키는 것: 사전이 정본이고, 프런트 표는 그 사본이며, 둘이 갈라지면 실패한다.

실행: <venv>/bin/python -m pytest tests/test_dictionary_single_source.py -q
"""
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import domain_dict as dd  # noqa: E402

INDEX = os.path.join(os.path.dirname(__file__), "..", "app", "static", "index.html")
LANGS = ("ko", "en", "id")


def _js_table(name):
    """index.html 의 const XXX={...} 를 파이썬 dict 로."""
    s = open(INDEX, encoding="utf-8").read()
    for decl in ("const %s=" % name, "var %s=" % name, "let %s=" % name):
        if decl in s:
            i = s.index(decl)
            break
    else:
        raise AssertionError("표를 못 찾음: %s" % name)
    i = s.index("{", i)
    depth, j = 0, i
    while True:
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    out = {}
    for m in re.finditer(r'([\w.]+)\s*:\s*"((?:[^"\\]|\\.)*)"', s[i:j + 1]):
        out[m.group(1)] = m.group(2).replace('\\"', '"')
    return out


# ── 사전 자체의 완결성 ─────────────────────────────────────────────────

@pytest.mark.parametrize("axis,action", [
    ("MATERIAL_CAT", "screen_category"), ("SOURCE", "screen_source"),
    ("EVIDENCE", "evidence_code"), ("STATUS", "screen_status"), ("ENUM", "enum_code"),
])
def test_every_code_has_all_three_languages(axis, action):
    """한 언어만 빠지면 그 화면에 코드값이 그대로 나간다 — 실제로 그랬다."""
    tables = {lg: dd.code_labels(axis, action, lg) for lg in LANGS}
    codes = set().union(*[set(t) for t in tables.values()])
    missing = {c: [lg for lg in LANGS if not tables[lg].get(c)] for c in codes}
    missing = {c: lg for c, lg in missing.items() if lg}
    assert missing == {}, "%s 축에 언어가 빠진 코드: %s" % (axis, missing)


def test_alternatives_have_both_translations():
    """대체재 문구는 한국어를 키로 쓴다 — 영어·인니어가 함께 있어야 한다."""
    for lg in ("en", "id"):
        assert len(dd.axis_text_map("ALTERNATIVE", lg)) >= 20, lg


# ── 프런트 표 == 사전 (드리프트 차단) ────────────────────────────────

@pytest.mark.parametrize("table,lang", [("KO_LABEL", "ko"), ("EN_LABEL", "en"),
                                        ("ID_LABEL", "id")])
def test_frontend_enum_labels_match_the_dictionary(table, lang):
    """프런트 표는 사전의 사본이다. 한쪽만 고치면 여기서 잡힌다."""
    front = _js_table(table)
    book = dd.code_labels("ENUM", "enum_code", lang)
    diff = {k: (v, book.get(k)) for k, v in front.items() if book.get(k) != v}
    assert diff == {}, "프런트 %s 와 사전이 다름: %s" % (table, list(diff.items())[:5])


def test_dictionary_covers_every_frontend_code():
    """프런트에만 있는 코드가 생기면 사전에 추가해야 한다."""
    front = set(_js_table("KO_LABEL"))
    book = set(dd.code_labels("ENUM", "enum_code", "ko"))
    assert front - book == set(), "사전에 없는 코드: %s" % sorted(front - book)[:8]


# ── 서버 표가 사전에서 나오는가 ──────────────────────────────────────

def test_server_state_labels_come_from_the_dictionary():
    """서버가 자기 상태표를 따로 들고 있으면 프런트와 갈라진다."""
    import app.main as m
    assert m._STATE_KO == dd.code_labels("ENUM", "enum_code", "ko")
    assert m.state_label("certificate_issued", "id") == "Sertifikat diterbitkan"


def test_unknown_code_is_returned_as_is():
    """모르는 코드는 지어내지 않고 그대로 — 빈 문자열은 화면에 구멍을 낸다."""
    import app.main as m
    assert m.state_label("no_such_state") == "no_such_state"


def test_screening_tables_come_from_the_dictionary():
    """스크리닝 표기도 사전에서 나온다."""
    from app import screening as sc
    assert sc._SOURCE_KO == dd.code_labels("SOURCE", "screen_source", "ko")
    assert sc._CAT_ID == dd.code_labels("MATERIAL_CAT", "screen_category", "id")


def test_the_korean_source_labels_that_were_missing_are_filled():
    """화면에 코드값이 그대로 나오던 유래 7종 — 회귀 방지."""
    from app import screening as sc
    for code in ("dairy", "human", "insect", "marine", "petrochemical", "unknown", "microbe"):
        v = sc._SOURCE_KO.get(code)
        assert v and not re.match(r"^[a-z_]+$", v), "%s → %r" % (code, v)


def test_ocr_pipeline_and_screening_agree():
    """같은 개념을 두 화면이 다르게 부르지 않는다 — 실제로 갈라져 있었다."""
    from app import ocr_pipeline as op
    from app import screening as sc
    assert op._srcs("ko") == sc._SOURCE_KO
    assert op._evs("ko") == sc._EVID_KO
    assert op._sts("ko") == sc._STATUS_KO


# ── 프런트 폴백표는 사전의 사본이다 ─────────────────────────────────────
# 화면 라벨은 서버(/i18n/labels)에서 받는다. 프런트 표는 서버를 못 받았을 때의
# 폴백으로만 남는다. 사본이 정본과 갈라지면 폴백이 틀린 값을 내므로 여기서 막는다.
# 실측: 서류명 12개 중 6개, 증빙 9개 중 7개가 서로 달랐고 '할랄 인증서' vs
# '공급사 할랄 인증서'처럼 뜻이 갈리는 차이도 있었다.

FALLBACK_TABLES = [
    ("DOC_KO_LABEL", "DOC", None),
    ("EVID_KO", "EVIDENCE", "evidence_code"),
    ("BLOCKER_KO", "BLOCKER", "blocker_code"),
    ("WS_STAGE_KO", "WS_STAGE", "ws_stage"),
    ("VAULT_GD_LABEL", "VAULT_DOC", "vault_doc"),
    ("_ORG_DIV_KO", "ORG_DIV", "org_div"),
    ("REG_STATE_KO", "REG_STATE", "reg_state"),
    ("SEV_KO", "SEVERITY", "severity"),
    ("INTAKE_KO", "INTAKE_ERROR", "intake_error"),
    ("GATE_KO", "GATE", "gate_code"),
    ("GENDOC_KO", "GEN_DOC", "gen_doc"),
]


@pytest.mark.parametrize("table,axis,action", FALLBACK_TABLES)
def test_frontend_fallback_matches_the_dictionary(table, axis, action):
    """폴백표의 모든 코드가 사전에 있고 한국어 표기가 같아야 한다."""
    front = _js_table(table)
    book = dd.doc_labels("ko") if action is None else dd.code_labels(axis, action, "ko")
    diff = {k: (v, book.get(k)) for k, v in front.items() if book.get(k) != v}
    assert diff == {}, "%s 와 사전이 다름: %s" % (table, list(diff.items())[:4])


@pytest.mark.parametrize("table,axis,action", FALLBACK_TABLES)
def test_every_fallback_code_has_indonesian(table, axis, action):
    """폴백표에 있는 코드는 인니어 표기가 있어야 한다 — 없으면 그 화면에 한글이 남는다."""
    front = set(_js_table(table))
    book = dd.doc_labels("id") if action is None else dd.code_labels(axis, action, "id")
    missing = sorted(c for c in front if not book.get(c))
    assert missing == [], "%s: 인니어 없는 코드 %s" % (table, missing)


def test_label_endpoint_serves_every_ui_group():
    """/i18n/labels 가 화면이 쓰는 묶음을 모두 내려주는가."""
    import app.main as m
    used = {"enum", "doc", "evidence", "severity", "blocker", "ws_stage", "vault_doc",
            "org_div", "reg_state", "intake_error", "gate", "gen_doc"}
    assert used <= set(m._UI_LABEL_AXES), sorted(used - set(m._UI_LABEL_AXES))


def test_label_endpoint_returns_three_languages():
    """세 언어 모두 응답한다 — 한 언어가 비면 그 화면 전체가 코드값으로 나간다."""
    import app.main as m
    for lg in LANGS:
        out = m.i18n_labels(lang=lg)
        assert out["lang"] == lg
        assert out["labels"]["enum"].get("certificate_issued")
        assert out["labels"]["doc"].get("sjph_manual")
