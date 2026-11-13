"""CAS dictionary identities and the remaining leading-dot/zero-suffix noise."""
import json
from types import SimpleNamespace

import pytest

from app import intake, models, screening
from app.db import SessionLocal
from app.kma1360 import name_exclusion_reasons
from test_axis_d1_freeze_issue import api, pending
from test_kma1360_p1 import install_ontology
from test_pending_material_guards import assert_pending_blocked, prepare


CAS_NAMES = [
    ("107-41-5", ("Hexylene Glycol", "2-Methylpentane-2,4-Diol")),
    ("100-51-6", ("Benzyl Alcohol",)),
]
OCR_NAMES = [
    (".Pure Myrrh Gum", "ocr_leading_dot"),
    ("4-Phenylbutan-2-0ne", "ocr_suffix_zero"),
    ("(2E,4E)-Octa-2,4-Dien-1-0l", "ocr_suffix_zero"),
]


@pytest.mark.parametrize("cas,names", CAS_NAMES)
@pytest.mark.parametrize("as_alias", [False, True])
@pytest.mark.parametrize("status,severity,expected", [
    ("mushbooh", "high", "NEEDS_EVIDENCE"), ("haram", "high", "BLOCK"),
    ("halal", "low", "PASS"),
])
def test_cas_dictionary_names_and_aliases_precede_kma_for_every_status(
        monkeypatch, cas, names, as_alias, status, severity, expected):
    item = install_ontology(monkeypatch, "CAS dictionary entry" if as_alias else cas,
                            status, severity, {"en": [cas]} if as_alias else None)
    for name in names:
        result = screening.screen_merged(name)
        assert (result["decision_by"], result["result"], result["status"],
                result["severity"], result["matched_uid"]) == (
            "ontology", expected, status, severity, item.ingredient_uid)
        assert result["sources"] == item.sources
        assert intake._material_autofill_allowed(name) is (status == "halal")


def test_all_cas_numbers_on_direct_peers_are_dictionary_candidates(monkeypatch, tmp_path):
    # Only the second row shares the first row's CAS. Its other CAS must be
    # checked, without recursively connecting the third row's names or CAS.
    rows = [
        {"no": 1, "name_id": "Matched Compound", "cas": ["107-41-5"]},
        {"no": 2, "name_id": "Peer Compound", "cas": ["107-41-5", "100-51-6"]},
        {"no": 3, "name_id": "Indirect Compound", "cas": ["100-51-6", "57-55-6"]},
    ]
    path = tmp_path / "catalogue.json"
    path.write_text(json.dumps([{"section": "c.2", "page": 12, "name_en": None, **row}
                                for row in rows]))
    names, cas = screening._load_kma1360(path)
    monkeypatch.setattr(screening, "_KMA1360_NAMES", names)
    monkeypatch.setattr(screening, "_KMA1360_CAS", cas)
    item = install_ontology(monkeypatch, "100-51-6")
    result = screening.screen_merged("Matched Compound")
    assert (result["decision_by"], result["matched_uid"], result["result"]) == (
        "ontology", item.ingredient_uid, "NEEDS_EVIDENCE")
    install_ontology(monkeypatch, "57-55-6")
    result = screening.screen_merged("Matched Compound")
    assert (result["decision_by"], result["result"]) == ("kma1360", "PASS")


def test_cas_dictionary_conflict_keeps_the_stricter_named_candidate(monkeypatch):
    safe = install_ontology(monkeypatch, "107-41-5", "halal", "low")
    risk = models.IngredientOntology(
        ingredient_uid="ing.kma_cas_risk", canonical_name="Hexylene Glycol",
        aliases={}, default_status="mushbooh", severity="high",
        required_evidence=["source_declaration"])
    screening.load_ontology(SimpleNamespace(query=lambda _: SimpleNamespace(all=lambda: [safe, risk])))
    assert screening.screen_merged("2-Methylpentane-2,4-Diol")["matched_uid"] == risk.ingredient_uid


@pytest.mark.parametrize("phase", ["entry", "decision", "issue", "checker"])
@pytest.mark.parametrize("cas,names", CAS_NAMES)
@pytest.mark.parametrize("as_alias", [False, True])
def test_four_cas_dictionary_counterexamples_block_every_gate(api, monkeypatch, phase, cas, names, as_alias):
    for name in names:
        cid, route, body, role = prepare(api, phase)
        with SessionLocal() as db:
            item = models.IngredientOntology(
                ingredient_uid="ing.kma_cas_gate", canonical_name="CAS dictionary entry" if as_alias else cas,
                aliases={"en": [cas]} if as_alias else {}, default_status="mushbooh", severity="high",
                required_evidence=["source_declaration"])
            items = db.query(models.IngredientOntology).all() + [item]
        with monkeypatch.context() as context:
            context.setattr(screening, "_CACHE", {"by_e": {}, "by_alias": {}, "alias_tok": [], "negated": None})
            screening.load_ontology(SimpleNamespace(query=lambda _: SimpleNamespace(all=lambda: items)))
            assert not intake._material_autofill_allowed(name)
            pending(cid, name)
            assert_pending_blocked(api("POST", route, body, role), name)
            with SessionLocal() as db:
                assert db.query(models.HalalCertificate).filter_by(case_id=cid).count() == 0


@pytest.mark.parametrize("name,reason", OCR_NAMES)
def test_three_ocr_counterexamples_remain_unconfirmed(name, reason):
    assert reason in name_exclusion_reasons(name)
    assert screening._match_kma1360(name) == []
    result = screening.screen_merged(name)
    assert (result["result"], result["status"]) == ("NEEDS_EVIDENCE", "mushbooh")
    assert result["decision_by"] != "kma1360"
    assert not intake._material_autofill_allowed(name)


@pytest.mark.parametrize("phase", ["entry", "decision", "issue", "checker"])
@pytest.mark.parametrize("name,reason", OCR_NAMES)
def test_three_ocr_counterexamples_block_every_gate(api, phase, name, reason):
    cid, route, body, role = prepare(api, phase)
    pending(cid, name)
    assert_pending_blocked(api("POST", route, body, role), name)


@pytest.mark.parametrize("name,reason", OCR_NAMES + [
    ("Cyclohexan-0ne", "ocr_suffix_zero"), ("Cyclohexan-0l", "ocr_suffix_zero"),
    ("Cyclohexan-2–0NE", "ocr_suffix_zero"),
])
def test_noise_is_excluded_with_missing_or_stale_metadata_and_cas_is_preserved(tmp_path, name, reason):
    assert reason in name_exclusion_reasons(name)
    for field in ("name_id", "name_en"):
        for metadata in ({}, {field: {"indexable": True}}):
            row = {"no": 1, "section": "c.2", "page": 12, "name_id": "Valid Compound",
                   "name_en": None, "cas": ["100-51-6"], "name_index": metadata, field: name}
            path = tmp_path / "catalogue.json"
            path.write_text(json.dumps([row]))
            names, cas = screening._load_kma1360(path)
            assert screening._kma_key(name) not in names
            assert row[field] == name
            assert cas["100-51-6"] == [row]


@pytest.mark.parametrize("name", [
    "4-Phenylbutan-2-One", "Cyclohexan-one", "Cyclohexan-ol", "(2E,4E)-Octa-2,4-Dien-1-Ol",
    "1,2-Hexanediol", "2-Methylpentane-2,4-Diol", "C12", "C12-15 Alkyl Benzoate",
    "Bicyclo[2.2.1]Heptane", "107-41-5", "100-51-6",
])
def test_normal_suffixes_locants_and_cas_are_not_ocr_noise(name):
    assert name_exclusion_reasons(name) == []


@pytest.mark.parametrize("name", [
    "Hexylene Glycol", "2-Methylpentane-2,4-Diol", "Benzyl Alcohol", "1,2-Hexanediol",
    "Propylene Glycol", "107-41-5", "100-51-6", "Benzyl Alcohol (100-51-6)",
])
def test_normal_catalogue_matches_remain_confirmed(name):
    result = screening.screen_merged(name)
    assert (result["result"], result["status"], result["severity"]) == ("PASS", "halal", "low")
    assert intake._material_autofill_allowed(name)
