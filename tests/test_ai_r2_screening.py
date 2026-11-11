"""R2 screening 회귀: 감사 결과와 기존 판정 입력을 외부 파일 의존 없이 고정한다.

출처: glhac-audit3-ai/results/{extended,metrics_edges}.json,
logs/{t_screen,t_screen2}.log 및 기존 screening 테스트.
감사에서 틀렸던 부정/철자/INS/Collard 사례만 R2 요구에 맞게 바로잡는다.
E120 등 온톨로지의 기존 판정 정책은 변경하지 않는다.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import models, screening


_RESULT_FIELDS = ("result", "status", "severity", "matched_uid")


def _decision(result):
    return tuple(result[key] for key in _RESULT_FIELDS)


AUDIT_EXTENDED_CASES = [
    ('Collard greens', 'PASS', 'halal', 'low', 'ing.vegetable'),
    ('Collard', 'PASS', 'halal', 'low', 'ing.vegetable'),
    ('Swiss chard', 'NEEDS_EVIDENCE', 'mushbooh', 'low', None),
    ('Mustard seed', 'NEEDS_EVIDENCE', 'mushbooh', 'low', None),
    ('Rice flour', 'PASS', 'halal', 'low', 'ing.rice'),
    ('Water', 'PASS', 'halal', 'low', 'ing.water'),
    ('Salt', 'PASS', 'halal', 'low', 'ing.salt'),
    ('Sugar', 'PASS', 'halal', 'low', 'ing.sugar'),
    ('Hamburger', 'NEEDS_EVIDENCE', 'mushbooh', 'low', None),
    ('Serum', 'NEEDS_EVIDENCE', 'mushbooh', 'low', None),
    ('Non-pork gelatin', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.gelatin'),
    ('Pork-free gelatin', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.gelatin'),
    ('Lard-free vegetable shortening', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.shortening'),
    ('Rum-free flavour', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.flavor'),
    ('Alcohol-free flavour', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.flavor'),
    ('L a r d', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('P o r k', 'BLOCK', 'haram', 'high', 'ing.pork'),
    ('INS 120', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.carmine'),
    ('B4bi', 'BLOCK', 'haram', 'high', 'ing.pork'),
    ('Lаrd', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('L.A.R.D', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('Hog casing', 'BLOCK', 'haram', 'high', 'ing.pork'),
    ('돼지지방(라드)', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('Mirin', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('E120', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.carmine'),
]


AUDIT_BASELINE_CASES = [
    ('Collard greens', 'PASS', 'halal', 'low', 'ing.vegetable'),
    ('Lard-free vegetable shortening', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.shortening'),
    ('Rum-free flavour', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.flavor'),
    ('돼지지방(라드)', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('Mirin', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('Lаrd', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('L.A.R.D', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('B4bi', 'BLOCK', 'haram', 'high', 'ing.pork'),
    ('Hog casing', 'BLOCK', 'haram', 'high', 'ing.pork'),
    ('E120', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.carmine'),
    ('L a r d', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('INS 120', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.carmine'),
]


AUDIT_LEGACY_CASES = [
    ('Tepung terigu', 'PASS', 'halal', 'low', 'ing.wheat'),
    ('Gula pasir', 'PASS', 'halal', 'low', 'ing.sugar'),
    ('Lemak babi', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('Gelatin sapi', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.gelatin'),
    ('Minyak babi', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('Angciu', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('Arak masak', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('Rhum', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('Mirin', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('Kuas bulu babi', 'BLOCK', 'haram', 'high', 'ing.pork'),
    ('Etanol', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.ethanol'),
    ('Alkohol', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('Lard', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('L a r d', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('L.A.R.D', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('Lardd', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('LARD', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('Lard™', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('Ⅼard', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('Lаrd', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('Pork fat', 'BLOCK', 'haram', 'high', 'ing.pork_derivative'),
    ('Porc', 'NEEDS_EVIDENCE', 'mushbooh', 'low', None),
    ('Gelatine', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.gelatin'),
    ('Gélatine', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.gelatin'),
    ('Gelatina', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.gelatin'),
    ('E120', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.carmine'),
    ('E-120', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.carmine'),
    ('E 120', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.carmine'),
    ('e120', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.carmine'),
    ('INS 120', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.carmine'),
    ('E471', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.mono_diglycerides'),
    ('E-471', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.mono_diglycerides'),
    ('Karmin', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.carmine'),
    ('Kochineal', 'NEEDS_EVIDENCE', 'mushbooh', 'low', None),
    ('Darah', 'BLOCK', 'haram', 'high', 'ing.blood'),
    ('Babi', 'BLOCK', 'haram', 'high', 'ing.pork'),
    ('B4bi', 'BLOCK', 'haram', 'high', 'ing.pork'),
    ('Wine vinegar', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('Cuka anggur', 'NEEDS_EVIDENCE', 'mushbooh', 'low', 'ing.vinegar'),
    ('Rum flavour', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('Bacon flavour', 'BLOCK', 'haram', 'high', 'ing.pork'),
    ('Ham', 'BLOCK', 'haram', 'high', None),
    ('Hamburger', 'NEEDS_EVIDENCE', 'mushbooh', 'low', None),
    ('Champagne', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('Sake', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('Tuak', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('Brem', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('Khamr', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('Collagen', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.collagen'),
    ('Kolagen', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.collagen'),
    ('L-cysteine', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.l_cysteine'),
    ('E920', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.l_cysteine'),
    ('Whey', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.whey'),
    ('Rennet', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.rennet'),
    ('Pepsin', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.pepsin'),
    ('Lipase', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.lipase_enz'),
    ('Shortening', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.shortening'),
    ('Emulsifier', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.emulsifier_generic'),
    ('돼지지방(라드)', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('라드', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('돈지', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('젤라틴', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.gelatin'),
    ('주정(알코올)', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('주정', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('맛술', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('미림', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('청주', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('코치닐추출색소', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.carmine'),
    ('카민', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.carmine'),
    ('돼지고기', 'BLOCK', 'haram', 'high', 'ing.pork'),
    ('돈육', 'BLOCK', 'haram', 'high', 'ing.pork'),
    ('소고기', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.beef'),
    ('Gelatin (halal certified)', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.gelatin'),
    ('Non-pork gelatin', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.gelatin'),
    ('Alcohol-free flavour', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.flavor'),
    ('Ethyl alcohol', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.ethanol'),
    ('Spirit vinegar', 'NEEDS_EVIDENCE', 'mushbooh', 'low', 'ing.vinegar'),
    ('Vanilla extract', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.vanilla_extract'),
    ('Natural flavour', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.flavor'),
    ('Glycerin', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.glycerin'),
    ('Mono- and diglycerides', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.mono_diglycerides'),
    ('Pig skin collagen', 'BLOCK', 'haram', 'high', 'ing.pork'),
    ('Porcine gelatin', 'BLOCK', 'haram', 'high', 'ing.pork_derivative'),
    ('Swine', 'BLOCK', 'haram', 'high', 'ing.pork'),
    ('Hog casing', 'BLOCK', 'haram', 'high', 'ing.pork'),
    ('Sapi', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.beef'),
    ('Ayam', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.chicken'),
    ('Daging babi giling', 'BLOCK', 'haram', 'high', 'ing.pork'),
    ('Minyak goreng', 'PASS', 'halal', 'low', 'ing.cooking_oil'),
    ('Air', 'PASS', 'halal', 'low', 'ing.water'),
    ('Garam', 'PASS', 'halal', 'low', 'ing.salt'),
    ('Salt', 'PASS', 'halal', 'low', 'ing.salt'),
    ('Water', 'PASS', 'halal', 'low', 'ing.water'),
    ('Pisang', 'PASS', 'halal', 'low', 'ing.banana'),
]


EXISTING_SCREENING_CASES = [
    ('돼지지방(라드)', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('돼지지방', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('돈지', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('라드', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('주정(알코올)', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('주정', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('Rhum', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('Mirin', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('Angciu', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('ang ciu', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('Sake', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('Champagne', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('Tuak', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('Brem', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('맛술', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('미림', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('청주', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('Hog casing', 'BLOCK', 'haram', 'high', 'ing.pork'),
    ('B4bi', 'BLOCK', 'haram', 'high', 'ing.pork'),
    ('L.A.R.D', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('Lаrd', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('Pig skin collagen', 'BLOCK', 'haram', 'high', 'ing.pork'),
    ('ＬＡＲＤ', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('Lαrd', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('1ard', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('p1g', 'BLOCK', 'haram', 'high', 'ing.pork'),
    ('p0rk', 'BLOCK', 'haram', 'high', 'ing.pork'),
    ('b33r', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('ΜΙRΙN', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('RΗUM', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('Salt (lard)', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('Lard (salt)', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('Sugar (Mirin)', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('Mirin (sugar)', 'BLOCK', 'haram', 'high', 'ing.ethanol_beverage'),
    ('porkfat', 'BLOCK', 'haram', 'high', 'ing.pork'),
    ('pork-fat', 'BLOCK', 'haram', 'high', 'ing.pork_derivative'),
    ('lemakbabi', 'BLOCK', 'haram', 'high', 'ing.pork'),
    ('minyakbabi', 'BLOCK', 'haram', 'high', 'ing.pork'),
    ('porkgelatin', 'BLOCK', 'haram', 'high', 'ing.pork'),
    ('lardoil', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('hamburger', 'NEEDS_EVIDENCE', 'mushbooh', 'low', None),
    ('serum', 'NEEDS_EVIDENCE', 'mushbooh', 'low', None),
    ('pisang', 'PASS', 'halal', 'low', 'ing.banana'),
    ('santan', 'PASS', 'halal', 'low', 'ing.coconut'),
    ('Gélatine', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.gelatin'),
    ('Non-pork gelatin', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.gelatin'),
    ('Non‑pork gelatin', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.gelatin'),
    ('pork-free gelatin', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.gelatin'),
    ('babi-free gelatin', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.gelatin'),
    ('bebas babi gelatin', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.gelatin'),
    ('무돼지 젤라틴', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.gelatin'),
    ('E120', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.carmine'),
    ('E-120', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.carmine'),
    ('E 120', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.carmine'),
    ('Ｅ－１２０', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.carmine'),
    ('코치닐추출색소', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.carmine'),
    ('코치닐', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.carmine'),
    ('카민', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.carmine'),
    ('cochineal', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.carmine'),
    ('carmine', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.carmine'),
    ('E-471', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.mono_diglycerides'),
    ('E–471', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.mono_diglycerides'),
    ('Emulsifier (E-471)', 'NEEDS_EVIDENCE', 'mushbooh', 'medium', 'ing.mono_diglycerides'),
    ('개선된 향료', 'NEEDS_EVIDENCE', 'mushbooh', 'low', None),
    ('Pig skin collagen (E330)', 'BLOCK', 'haram', 'high', 'ing.pork'),
    ('Non-pork gelatin, lard', 'BLOCK', 'haram', 'high', 'ing.lard'),
    ('Ethanol', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.ethanol'),
    ('Gelatin', 'NEEDS_EVIDENCE', 'mushbooh', 'high', 'ing.gelatin'),
    ('Air Pam', 'PASS', 'halal', 'low', 'ing.water'),
    ('air pdam', 'PASS', 'halal', 'low', 'ing.water'),
    ('Air Keran', 'PASS', 'halal', 'low', 'ing.water'),
    ('air ledeng', 'PASS', 'halal', 'low', 'ing.water'),
    ('tap water', 'PASS', 'halal', 'low', 'ing.water'),
    ('air kelapa', 'PASS', 'halal', 'low', 'ing.coconut'),
    ('air jeruk nipis', 'PASS', 'halal', 'low', 'ing.citrus_fruit'),
    ('Bahan Tidak Dikenal', 'NEEDS_EVIDENCE', 'mushbooh', 'low', None),
]


@pytest.mark.parametrize("name,result,status,severity,uid", [
    pytest.param(*row, id=f"{source}-{index}-{row[0]}")
    for source, rows in (
        ("extended", AUDIT_EXTENDED_CASES),
        ("baseline", AUDIT_BASELINE_CASES),
        ("legacy", AUDIT_LEGACY_CASES),
        ("existing", EXISTING_SCREENING_CASES),
    )
    for index, row in enumerate(rows)
])
def test_audit_and_existing_screening_cases(name, result, status, severity, uid):
    assert _decision(screening.screen_merged(name)) == (result, status, severity, uid)
    # 이름 판정은 어순/토큰 매칭도 쓰므로 본문 스캔과 대표 UID가 다를 수 있다.
    # 기존 pork-fat: 이름=pork_derivative, 본문=pork. 두 경로의 haram 탐지를 고정한다.
    scan = screening.screen_text(name)
    if status == "haram" and uid is not None:
        assert scan["haram_hits"]
    elif status != "haram":
        assert not scan["haram_hits"]


# 전체 위험 별칭에 대한 속성 검사: 새 사전 항목도 돼지 전용 분기로 빠지면 안 된다.
_ONTOLOGY_ROWS = json.loads((Path(__file__).resolve().parents[1] / "app/ontology_data.json").read_text())
_RISK_ALIASES = sorted({
    alias
    for row in _ONTOLOGY_ROWS
    if row["default_status"] == "haram" or row["severity"] == "high"
    for alias in [row["canonical_name"], *(a for group in row["aliases"].values() for a in group)]
})


@pytest.mark.parametrize("alias", _RISK_ALIASES)
@pytest.mark.parametrize("template", ["{}-free flavour", "free from {} flavour"])
def test_every_high_or_haram_alias_supports_negation(alias, template):
    name = template.format(alias)
    assert _decision(screening.screen_merged(name)) == (
        "NEEDS_EVIDENCE", "mushbooh", "medium", "ing.flavor")
    assert screening.screen_raw(name)["matched_uid"] == "ing.flavor"
    scan = screening.screen_text(name)
    assert not scan["haram_hits"]
    assert not [hit for hit in scan["ontology_hits"] if hit["severity"] == "high"]
    assert screening.analyze_text_risk(name) == "medium"


@pytest.mark.parametrize("alias", ["rum", "lard", "mirin", "heparin", "gelatin", "alcohol", "주정", "돼지"])
@pytest.mark.parametrize("template", [
    "{}-free flavour", "{} free flavour", "free from {} flavour", "free-from {} flavour",
    "non-{} flavour", "non‑{} flavour", "bebas {} flavour", "tanpa {} flavour",
    "무-{} flavour", "무{} flavour", "無{} flavour",
])
def test_negation_forms_only_remove_the_named_risk(alias, template):
    name = template.format(alias)
    assert screening.screen_merged(name)["result"] == "NEEDS_EVIDENCE"
    assert not screening.screen_text(name)["haram_hits"]
    assert screening.analyze_text_risk(name) == "medium"
    # 부정된 별칭 뒤 별도 성분은 원래의 high/haram 판정을 유지한다.
    assert screening.screen_merged(name + ", contains lard")["result"] == "BLOCK"
    assert "ing.lard" in {hit["uid"] for hit in screening.screen_text(name + ", with lard")["haram_hits"]}


@pytest.mark.parametrize("name", [
    "contains rum", "with lard", "alcohol-free flavour, contains rum",
    "alcohol-free flavour with lard", "rum-free flavour; contains rum",
    "free from lard, with lard", "non-rum flavour with lard", "tanpa rum, lard",
    "Non-pork gelatin, lard", "Rum-free flavour\nHeparin", "Lard-free flavour, contains blood",
    "Rum-free flavour (lard)", "Rum-free flavour / lard", "lard, rum-free flavour",
    "Alcohol-free flavour with heparin", "Rum-free flavour with porkfat",
])
def test_positive_occurrences_survive_negation(name):
    for result in (screening.screen_raw(name), screening.screen_merged(name)):
        assert (result["result"], result["status"], result["severity"]) == ("BLOCK", "haram", "high")
    scan = screening.screen_text(name)
    assert scan["verdict"] == "high" and scan["haram_hits"]


@pytest.mark.parametrize("name,uid", [
    ("Non-pork gelatin", "ing.gelatin"), ("무돼지 젤라틴", "ing.gelatin"),
    ("Alcohol-free gelatin", "ing.gelatin"), ("Rum-free flavour with collagen", "ing.collagen"),
    ("Lard-free vegetable shortening", "ing.shortening"),
])
def test_remaining_generic_ingredients_still_require_evidence(name, uid):
    result = screening.screen_merged(name)
    assert result["result"] == "NEEDS_EVIDENCE" and result["matched_uid"] == uid
    assert result["required_evidence"]
    assert not screening.screen_text(name)["haram_hits"]


@pytest.mark.parametrize("note,result", [
    ("rum-free flavour", "PASS"), ("contains rum", "BLOCK"),
    ("alcohol-free with lard", "BLOCK"), ("non-pork gelatin", "NEEDS_EVIDENCE"),
])
def test_note_negation_and_positive_hazards(note, result):
    assert screening.screen_merged("Water", note=note)["result"] == result


@pytest.mark.parametrize("name,uid", [
    ("L a r d", "ing.lard"), ("B a b i", "ing.pork"), ("P o r k", "ing.pork"),
    ("R u m", "ing.ethanol_beverage"), ("Ｌ ａ ｒ ｄ", "ing.lard"),
    ("L\ta\tr\td", "ing.lard"), ("L\u00a0a\u00a0r\u00a0d", "ing.lard"),
    ("flavour with L a r d", "ing.lard"), ("B a b i oil", "ing.pork"),
    ("sugar (L a r d)", "ing.lard"), ("Collard greens, B a b i", "ing.pork"),
])
def test_spelled_word_runs_match_all_screening_entry_points(name, uid):
    assert _decision(screening.screen_raw(name)) == ("BLOCK", "haram", "high", uid)
    assert _decision(screening.screen_merged(name)) == ("BLOCK", "haram", "high", uid)
    assert uid in {hit["uid"] for hit in screening.screen_text(name)["haram_hits"]}
    assert screening.analyze_text_risk(name) == "high"


@pytest.mark.parametrize("name", [
    "La r d", "L ar d", "L a rd", "la rd", "Ba bi", "B ab i", "P or k",
    "L a r dandelion", "vitamin B a blend", "L\na r d", "C o l l a r d greens",
])
def test_partial_spelling_and_line_boundaries_do_not_join_words(name):
    assert screening.screen_merged(name)["result"] != "BLOCK"
    assert not screening.screen_text(name)["haram_hits"]


@pytest.mark.parametrize("name", ["L a r d-free flavour", "free from B a b i gelatin", "R u m-free flavour"])
def test_spelled_word_candidates_keep_negation(name):
    assert screening.screen_merged(name)["result"] == "NEEDS_EVIDENCE"
    assert not screening.screen_text(name)["haram_hits"]


@pytest.mark.parametrize("number,result,status,severity,uid", [
    ("E120", "NEEDS_EVIDENCE", "mushbooh", "medium", "ing.carmine"),
    ("E441", "NEEDS_EVIDENCE", "mushbooh", "high", "ing.gelatin"),
    ("E471", "NEEDS_EVIDENCE", "mushbooh", "medium", "ing.mono_diglycerides"),
    ("E542", "NEEDS_EVIDENCE", "mushbooh", "high", "ing.bone_phosphate"),
    ("E904", "NEEDS_EVIDENCE", "mushbooh", "low", "ing.shellac"),
    ("E920", "NEEDS_EVIDENCE", "mushbooh", "high", "ing.l_cysteine"),
    ("E1510", "NEEDS_EVIDENCE", "mushbooh", "low", None),
])
def test_audit_explicit_e_number_cases(number, result, status, severity, uid):
    assert _decision(screening.screen_merged("x", e_number=number)) == (result, status, severity, uid)


@pytest.mark.parametrize("name,number", [
    ("INS 120", "E120"), ("INS120", "E120"), ("ins120", "E120"), ("INS-120", "E120"),
    ("ＩＮＳ １２０", "E120"), ("Colour (INS 120)", "E120"),
    ("INS 471", "E471"), ("INS471", "E471"), ("INS 472e", "E472E"),
    ("INS1200", "E1200"), ("INS 1000", "E1000"),
])
def test_ins_numbers_use_the_existing_e_number_lookup(name, number):
    assert screening.extract_e_numbers(name) == [number]
    expected = _decision(screening.screen_merged("", e_number=number))
    assert _decision(screening.screen_merged(name)) == expected
    assert _decision(screening.screen_merged("", e_number=name)) == expected
    assert _decision(screening.screen_raw(name)) == expected
    assert expected[-1] in {hit["uid"] for hit in screening.screen_text(name)["ontology_hits"]}


@pytest.mark.parametrize("text,numbers", [
    ("INS 120, E-120, INS120", ["E120"]),
    ("E1200 E-471 INS 120", ["E1200", "E471", "E120"]),
    ("BINS120 INS12000 INS120ab E120ab", []),
    ("E330 INS 471 INS 1000", ["E330", "E471", "E1000"]),
])
def test_number_boundaries_deduplication_and_risk_order(text, numbers):
    assert screening.extract_e_numbers(text) == numbers
    if numbers == ["E330", "E471", "E1000"]:
        assert screening.extract_e_number(text) == "E1000"
        assert screening.screen_merged(text)["result"] == "BLOCK"


@pytest.mark.parametrize("name", ["Collard", "Collard greens", "Collards", "fresh collard greens"])
def test_dictionary_words_exclude_internal_aliases(name):
    assert _decision(screening.screen_merged(name)) == ("PASS", "halal", "low", "ing.vegetable")
    assert screening.screen_raw(name)["matched_uid"] == "ing.vegetable"
    assert not screening.screen_text(name)["haram_hits"]


@pytest.mark.parametrize("name", [
    "porkfat", "lemakbabi", "lardoil", "minyakbabi", "porkgelatin", "xlard", "zzlard",
    "collardlard", "collard greens with lard", "lard with collard greens",
    "Collard greens (porkfat)", "Collard greens, INS 1000",
])
def test_dictionary_word_exclusion_does_not_hide_other_hazards(name):
    assert screening.screen_merged(name)["result"] == "BLOCK"
    assert screening.screen_text(name)["haram_hits"]


@pytest.mark.parametrize("prefix,known_status,expected", [
    ("ab", "halal", False), ("a", "halal", True), ("ab", "mushbooh", True), ("ab", "haram", True),
])
def test_dictionary_exclusion_is_general_and_requires_two_prefix_letters(monkeypatch, prefix, known_status, expected):
    # 특정 채소명 예외가 아니라 사전에 새로 들어온 단어에도 적용되는 규칙이다.
    risk = models.IngredientOntology(default_status="haram", severity="high")
    known = models.IngredientOntology(default_status=known_status, severity="low")
    word = prefix + "lard" + "leaf"
    monkeypatch.setitem(screening._CACHE, "by_alias", {word: known})
    assert screening._contains_alias(word, "lard", risk) is expected
    assert screening._contains_alias(word + " with lard", "lard", risk)


@pytest.mark.parametrize("status,severity", [("haram", "medium"), ("mushbooh", "high")])
def test_new_ontology_risk_aliases_receive_negation_on_reload(monkeypatch, status, severity):
    item = models.IngredientOntology(ingredient_uid="test.risk", canonical_name="Novel extract",
                                    aliases={"en": ["novelalias"]}, default_status=status, severity=severity)
    monkeypatch.setattr(screening, "_CACHE", {"by_alias": {}, "by_e": {}, "alias_tok": [], "negated": None})
    screening.load_ontology(SimpleNamespace(query=lambda _: SimpleNamespace(all=lambda: [item])))
    assert screening._match("novelalias", None) is item
    for name in ("novelalias-free", "non-novelalias", "free from novelalias", "bebas novelalias",
                 "tanpa novelalias", "무-novelalias", "無novelalias"):
        assert screening._match(name, None) is None
        assert not screening.scan_text(name)


@pytest.mark.parametrize("name,number", [("INS 441-free flavour", "E441"), ("free from E1000 flavour", "E1000")])
def test_negated_risk_numbers_do_not_reappear_but_explicit_number_fields_remain(name, number):
    assert screening.screen_merged(name)["matched_uid"] == "ing.flavor"
    assert not [h for h in screening.screen_text(name)["ontology_hits"] if h["severity"] == "high"]
    assert screening.screen_merged(name, e_number=number)["matched_uid"] == screening._CACHE["by_e"][number].ingredient_uid


def test_audit_whole_document_scan():
    scan = screening.screen_text("Komposisi: tepung, gula, E120, angciu, mirin, rhum")
    assert scan["verdict"] == "high"
    assert {h["uid"] for h in scan["haram_hits"]} == {"ing.ethanol_beverage"}
    assert {"ing.carmine", "ing.sugar"} <= {h["uid"] for h in scan["ontology_hits"]}


