"""문구 다국어 커버리지 — 문서·화면에 한국어가 새지 않게 지킨다.

배경: 인도네시아어로 뽑은 사전심사 보고서에 'Alternatif: 할랄 전용 라인…' 이 한국어로
남아 있었다(실측). 온톨로지 문구는 435항목에서 파생되므로 하나만 빠져도 문서에 드러난다.

실행: <venv>/bin/python -m pytest tests/test_i18n_coverage.py -q
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import screening as S  # noqa: E402

HAN = re.compile(r"[가-힣]")
ONTO = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "ontology_data.json")


def _onto():
    return json.load(open(ONTO, encoding="utf-8"))


def test_every_alternative_has_en_and_id():
    """대안 문구는 3개 언어가 다 있어야 한다 — 없으면 문서에 한국어가 그대로 찍힌다."""
    alts = {a for t in _onto() for a in (t.get("alternatives") or []) if HAN.search(a)}
    assert alts, "대안 문구가 하나도 없다 — 온톨로지 파일을 확인하라"
    assert [a for a in alts if a not in S._ALT_EN] == []
    assert [a for a in alts if a not in S._ALT_ID] == []


def test_every_evidence_key_has_en_and_id():
    """증빙 항목명도 마찬가지 — 보고서 '필요 증빙' 칸에 그대로 나간다."""
    keys = {e for t in _onto() for e in (t.get("required_evidence") or [])}
    assert keys
    assert [k for k in keys if k not in S._EVID_EN] == []
    assert [k for k in keys if k not in S._EVID_ID] == []
    # 한국어 표가 비어 있어 화면에 'halal_slaughter_cert' 코드가 그대로 나왔다(실측).
    assert [k for k in keys if k not in S._EVID_KO] == []


def test_translated_values_are_not_korean():
    """번역칸에 한국어를 복사해 두면 사전을 타도 안 바뀐다(화면 사전에서 19건 나왔다)."""
    for name in ("_ALT_EN", "_ALT_ID", "_EVID_EN", "_EVID_ID"):
        table = getattr(S, name)
        bad = [k for k, v in table.items() if HAN.search(v or "")]
        assert bad == [], "%s 값에 한국어: %s" % (name, bad[:3])


def test_category_and_status_labels_cover_all_values():
    """온톨로지에 쓰인 분류·판정값이 표기 표에 다 있어야 한다."""
    cats = {t.get("category") for t in _onto() if t.get("category")}
    stats = {t.get("default_status") for t in _onto() if t.get("default_status")}
    for tbl, vals, label in ((S._CAT_EN, cats, "분류(EN)"), (S._CAT_ID, cats, "분류(ID)"),
                             (S._STATUS_EN, stats, "판정(EN)"), (S._STATUS_ID, stats, "판정(ID)")):
        missing = [v for v in vals if v not in tbl]
        assert missing == [], "%s 누락: %s" % (label, missing)


# ── 화면 문구(index.html) ────────────────────────────────────────────────
# T() 폴백 규칙: UI_STRINGS 에 있으면 인니어 → 없으면 '한국어 · English' 병기는 영어 →
# 둘 다 아니면 한국어가 그대로 노출된다. 마지막 경우를 0으로 유지한다.
INDEX = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "static", "index.html")


def _dict_block(s, name):
    i = s.find("const %s=" % name)
    if i < 0:
        i = s.find("var %s=" % name)
    start = s.index("{", i)
    depth, k, instr, esc = 0, start, False, False
    while k < len(s):
        ch = s[k]
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
        else:
            if ch == '"':
                instr = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
        k += 1
    return s[start:k + 1]


def _pairs(block):
    return re.findall(r'"((?:[^"\\]|\\.)*)"\s*:\s*"((?:[^"\\]|\\.)*)"', block)


def test_no_korean_leaks_to_indonesian_screen():
    """T() 로 감싼 한국어 문구는 인니어 표기가 있어야 한다 — 없으면 화면에 한국어가 뜬다."""
    s = open(INDEX, encoding="utf-8").read()
    ui = _dict_block(s, "UI_STRINGS")
    body = s.replace(ui, "").replace(_dict_block(s, "EN_STRINGS"), "")
    keys = {a for a, _ in _pairs(ui)}
    used = {m.group(2) for m in re.finditer(r"""T\(\s*(['"])((?:(?!\1).)*)\1\s*\)""", body)
            if HAN.search(m.group(2))}
    missing = sorted(k for k in used if k not in keys)
    assert missing == [], "인니어 표기 없음 %d건: %s" % (len(missing), missing[:5])


def test_indonesian_values_are_not_korean():
    """번역칸에 한국어를 그대로 복사해 두면 사전을 타도 화면이 안 바뀐다(실측 19건)."""
    s = open(INDEX, encoding="utf-8").read()
    bad = [a for a, v in _pairs(_dict_block(s, "UI_STRINGS")) if HAN.search(v)]
    assert bad == [], "인니어 값에 한국어 %d건: %s" % (len(bad), bad[:5])
