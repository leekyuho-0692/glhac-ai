"""Official annex: strict identity matching, conservative conflicts, shared gates."""
from collections import Counter
import json
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest

from app import intake, models, screening
from scripts import build_kma1360 as build
from test_axis_d1_freeze_issue import api, pending
from test_package_autofill import case as package_case, offline, _round3_ingest, names
from test_pending_material_guards import prepare, assert_pending_blocked


CATALOGUE = Path(__file__).resolve().parents[1] / "app/data/kma1360.json"
CONFIRMED = ["1,2-Hexanediol", "Propylene Glycol", "Benzyl Alcohol", "Sodium Hydroxide"]
UNCONFIRMED = ["Glycerin", "Lecithin", "Stearic acid", "Sodium Hyaluronate", "Gelatin",
               "Alcohol Denat.", "Ethanol", "Glyceryl Stearate", "Pork Gelatin", "Benzyl Alcohol from lard"]


@pytest.mark.parametrize("name", CONFIRMED + [
    "1310-73-2", "CAS: 6920-22-5", "CAS No. 57-55-6", "CAS 100-51-6",
    "Sodium Hydroxide (CAS 1310-73-2)", "  ＢＥＮＺＹＬ　ＡＬＣＯＨＯＬ  ",
    "Propylene-Glycol", "(1,2–Hexanediol)", "Propilen Glikol", "Natrium Hidroksida", "Benzyl (Alcohol)",
    "Benzyl Alcohol (CAS 100-51-6)", "Benzyl Alcohol 100-51-6",
])
def test_exact_name_and_valid_cas_are_confirmed(name, monkeypatch):
    # Test the KMA fallback in isolation: even safe ontology entries now take
    # precedence. Real seeded-ontology precedence is covered in test_kma1360_p1.
    monkeypatch.setattr(screening, "_CACHE", {"by_e": {}, "by_alias": {}, "alias_tok": [], "negated": None})
    result = screening.screen_merged(name)
    assert (result["result"], result["status"], result["severity"], result["decision_by"]) == (
        "PASS", "halal", "low", "kma1360")
    assert result["sources"] and all(s.startswith("KMA 1360/2021 Lampiran c.") for s in result["sources"])
    assert result["required_evidence"] == []
    assert result["v1_risk"] == "low"
    assert intake._material_autofill_allowed(name)
    explanation = screening.explain(name)
    assert explanation["status"] == "halal"
    assert set(result["sources"]) <= set(explanation["sources"])
    assert result["sources"][0] in explanation["explanation"]


@pytest.mark.parametrize("name", UNCONFIRMED)
def test_sensitive_or_partial_names_remain_unconfirmed(name):
    result = screening.screen_merged(name)
    assert result["decision_by"] != "kma1360"
    assert result["result"] in {"NEEDS_EVIDENCE", "BLOCK"}
    assert not intake._material_autofill_allowed(name)


@pytest.mark.parametrize("name", [
    "Buah segar", "fresh fruits", "Minyak atsiri", "Ikan air laut/tawar/payau segar",
    "Bahan yang dihasilkan dari hewan halal nonsembelihan", "Polimer sintetik",
    "Glyceryl Stearate", "Benzyl Alcohol solution", "Benzyl Alcohol (from lard)",
    "1,2-Hexanediol derivative", "2,1-Hexanediol", "Glycol Propylene", "Propylene",
    "Benzyl", "Sodium Hydroxide substitute", "X1310-73-2", "01310-73-2", "1310-73-20",
    "1310-73-3", "1310-73-2-X", "99999991310-73-2", "11310-73-2", "1310-73-2.5",
    "Isomer Unspecified", "Isomer Mixture", "Resin", "Sodium Salt", "Free Acid", "INCI", "Dimer",
])
def test_catalogue_never_uses_substrings_tokens_or_invalid_cas(name):
    assert screening._match_kma1360(name) == []


def test_legacy_exempt_terms_are_preserved_but_also_match_whole_names():
    assert len(screening.KMA1360_EXEMPT) == 35
    assert screening.is_kma1360("Water")
    assert screening.is_kma1360("Sodium-Hydroxide")
    assert not screening.is_kma1360("water-based unknown mixture")
    assert not screening.is_kma1360("Glyceryl Stearate")


@pytest.mark.parametrize("name,note", [
    ("1310-73-2 pork", ""), ("1310-73-2 gelatin", ""),
    ("Benzyl Alcohol", "from lard"), ("Benzyl Alcohol", "gelatin carrier"),
    ("Propylene Glycol", "porcine"), ("1,2-Hexanediol", "animal fat"),
    ("Glycerin (CAS 1310-73-2)", ""), ("Ethanol (CAS 100-51-6)", ""),
    ("Alcohol (CAS 100-51-6)", ""), ("Benzyl Alcohol (CAS 1310-73-2)", ""),
])
def test_risks_in_names_and_notes_override_cas_and_kma(name, note):
    result = screening.screen_merged(name, note=note)
    assert result["decision_by"] != "kma1360"
    assert result["status"] in {"haram", "mushbooh"}
    assert not intake._material_screening_safe(result)


@pytest.mark.parametrize("name", CONFIRMED + ["1310-73-2"])
@pytest.mark.parametrize("status", ["haram", "mushbooh"])
def test_explicit_ontology_conflicts_always_win(monkeypatch, name, status):
    item = models.IngredientOntology(ingredient_uid="ing.kma_conflict", canonical_name=name,
        aliases={"en": [name]}, default_status=status, severity="high", category="test",
        required_evidence=["source_declaration"], sources=["animal"])
    monkeypatch.setattr(screening, "_CACHE", {"by_e": {}, "by_alias": {}, "alias_tok": [], "negated": None})
    screening.load_ontology(SimpleNamespace(query=lambda _: SimpleNamespace(all=lambda: [item])))
    result = screening.screen_merged(name)
    assert result["decision_by"] == "ontology"
    assert result["status"] == status
    assert result["matched_uid"] == "ing.kma_conflict"
    assert not intake._material_autofill_allowed(name)


@pytest.mark.parametrize("variant", ["1,2 Hexanediol", "(１,２—Ｈｅｘａｎｅｄｉｏｌ)",
                                     "1,2 Hexanediol (CAS 6920-22-5)"])
def test_normalized_identity_cannot_bypass_dictionary_conflicts(monkeypatch, variant):
    item = models.IngredientOntology(ingredient_uid="ing.kma_conflict", canonical_name="1,2-Hexanediol",
        aliases={}, default_status="mushbooh", severity="medium", required_evidence=["source_declaration"])
    monkeypatch.setattr(screening, "_CACHE", {"by_e": {}, "by_alias": {}, "alias_tok": [], "negated": None})
    screening.load_ontology(SimpleNamespace(query=lambda _: SimpleNamespace(all=lambda: [item])))
    result = screening.screen_merged(variant)
    assert result["status"] == "mushbooh"
    assert result["decision_by"] == "ontology"
    assert not intake._material_autofill_allowed(variant)


@pytest.mark.parametrize("name,cas", [("Benzyl Alcohol", "100-51-6"), ("Sodium Hydroxide", "1310-73-2")])
@pytest.mark.parametrize("status", ["haram", "mushbooh"])
def test_cas_alone_cannot_bypass_a_named_ontology_conflict(monkeypatch, name, cas, status):
    item = models.IngredientOntology(ingredient_uid="ing.kma_conflict", canonical_name=name,
        aliases={}, default_status=status, severity="high", required_evidence=["source_declaration"])
    monkeypatch.setattr(screening, "_CACHE", {"by_e": {}, "by_alias": {}, "alias_tok": [], "negated": None})
    screening.load_ontology(SimpleNamespace(query=lambda _: SimpleNamespace(all=lambda: [item])))
    result = screening.screen_merged(cas)
    assert result["status"] == status
    assert result["decision_by"] == "ontology"
    assert not intake._material_autofill_allowed(cas)


def test_dangerous_e_number_also_wins_over_exact_name():
    result = screening.screen_merged("Benzyl Alcohol", e_number="E422")
    assert result["decision_by"] == "ontology"
    assert result["status"] == "mushbooh"


@pytest.mark.parametrize("name", CONFIRMED)
@pytest.mark.parametrize("route", ["aggregate", "parse-file", "reprocess"])
def test_real_catalogue_autoregisters_and_persists(package_case, monkeypatch, name, route):
    db, case = package_case
    applied = _round3_ingest(package_case, monkeypatch, route, f"Ingredients\n{name} | 100%", [name])
    assert applied["materials"] == 1
    assert applied["pending"] == 0
    assert names(db, case, models.Material) == [name]
    material = db.query(models.Material).filter_by(case_id=case.case_id).one()
    assert (material.screen_result, material.screen_status, material.screen_severity) == ("PASS", "halal", "low")


@pytest.mark.parametrize("phase", ["entry", "decision", "issue", "checker"])
@pytest.mark.parametrize("name", CONFIRMED)
def test_confirmed_pending_material_passes_all_existing_gates(api, phase, name):
    cid, route, body, role = prepare(api, phase)
    pending(cid, name)
    response = api("POST", route, body, role)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("phase", ["entry", "decision", "issue", "checker"])
@pytest.mark.parametrize("name", UNCONFIRMED)
def test_sensitive_pending_material_still_blocks_all_existing_gates(api, phase, name):
    cid, route, body, role = prepare(api, phase)
    pending(cid, name)
    assert_pending_blocked(api("POST", route, body, role), name)


def test_catalogue_completeness_provenance_and_all_cas_digits():
    rows = json.loads(CATALOGUE.read_text(encoding="utf-8"))
    # Keep the original six-field provenance contract/asserts intact. The new
    # per-alias exclusion metadata is validated separately in test_kma1360_p1.
    for row in rows:
        row.pop("name_index", None)
    assert len(rows) >= 4100
    assert Counter(row["section"] for row in rows) == {
        "a.1": 33, "a.2": 8, "a.3": 5, "a.4": 3, "b": 6, "c.1": 19, "c.2": 4038}
    assert len({(row["section"], row["no"]) for row in rows}) == len(rows)
    assert rows == sorted(rows, key=lambda row: (build.SECTIONS.index(row["section"]), row["no"], row["page"]))
    assert all(set(row) == {"no", "section", "name_id", "name_en", "cas", "page"} for row in rows)
    assert all(5 <= row["page"] <= 182 and row["name_id"] for row in rows)
    assert sum(bool(row["cas"]) for row in rows) > 3800
    assert all(build.valid_cas(cas) and screening._valid_cas(cas) for row in rows for cas in row["cas"])
    by_key = {(row["section"], row["no"]): row for row in rows}
    assert by_key["c.2", 3831] == {"no": 3831, "section": "c.2", "name_id": "1,2-Hexanediol",
                                  "name_en": None, "cas": ["6920-22-5"], "page": 172}
    assert by_key["c.2", 338]["cas"] == ["1310-73-2"]
    assert by_key["c.2", 424]["name_en"] == "Propylene Glycol"
    assert by_key["c.2", 54]["name_en"] == "Benzyl Alcohol"
    assert by_key["c.2", 713]["name_id"] == "Cyclohexanol"
    assert by_key["c.2", 2039]["name_id"] == by_key["c.2", 2040]["name_id"] == "Methyl 3-Hexenoate"
    assert "Polivinil Alkohol" in by_key["b", 9]["name_id"]
    assert by_key["c.2", 3231]["name_en"] == "Basic Yellow 57"
    assert by_key["c.2", 3231]["page"] == 145
    assert by_key["c.2", 396]["cas"] == []  # printed 7790-56-3 fails the checksum
    assert not any((row["section"] == "a.1" and row["no"] == 25)
                   or (row["section"] == "c.1" and row["no"] == 6) for row in rows)


@pytest.mark.parametrize("name", ["Poly(Propylene Glycol) Macromolecule", "Tin(II) Oxide",
                                  "Mepiramin Maleat (Mepyramin", "(R)-Limonene",
                                  "Nerolidol (Isomer Unspecified)", "Opopanax (Resin)",
                                  "Benzoyl Peroxide (Inci)", "Sodium Chloride (NaCl)"])
def test_chemical_parentheses_are_not_partial_english_aliases(name):
    assert build.split_translation(name) == (name, None)


def test_stereochemistry_in_translation_is_preserved():
    assert build.split_translation("Ectoin ((S)-2-Methyl Acid)") == ("Ectoin", "(S)-2-Methyl Acid")
    assert build.split_translation("Compound (R)-Example (Specific Name)") == (
        "Compound (R)-Example", "Specific Name")


def test_cas_extraction_checks_full_number_and_rejects_ocr_guessing():
    audit = {"cas_format_failures": [], "cas_checksum_failures": []}
    result = build.parse_cas(["1310 - 73 - 2", "154 702-15-5", "100-51-6 / 57-55-6",
                              "1310-73-3", "01310-73-2", "6920-22~5", "157.16-30-0"], {}, audit)
    assert result == ["100-51-6", "1310-73-2", "154702-15-5", "57-55-6"]
    assert audit["cas_checksum_failures"] == [{"value": "1310-73-3"}]
    assert len(audit["cas_format_failures"]) == 3


def test_pdf_wrapped_names_cas_and_page_continuations_are_reconstructed():
    with fitz.open() as doc:
        for page_number in (1, 2):
            page = doc.new_page(width=606, height=927)
            page.insert_text((80, 80), "2. Bahan Kimia Hasil Sintesis Anorganik dan Organik")
            page.insert_text((80, 110), "No")
            page.insert_text((115, 110), "Chemical Abstract Service")
            page.insert_text((185, 125), "(CAS)")
            page.insert_text((380, 110), "Jenis Bahan")
            if page_number == 1:
                page.insert_text((80, 150), "1")
                page.insert_text((115, 150), "100-51-6")
                page.insert_text((290, 150), "Benzil Alkohol (Benzyl")
            else:
                page.insert_text((290, 150), "Alcohol)")
                page.insert_text((80, 190), "2")
                page.insert_text((115, 190), "1310-")
                page.insert_text((115, 205), "73-2")
                page.insert_text((290, 190), "Natrium Hidroksida (Sodium")
                page.insert_text((290, 205), "Hydroxide)")
        rows, audit = build.extract_document(doc)
    assert rows == [
        {"no": 1, "section": "c.2", "name_id": "Benzil Alkohol", "name_en": "Benzyl Alcohol",
         "cas": ["100-51-6"], "page": 1},
        {"no": 2, "section": "c.2", "name_id": "Natrium Hidroksida", "name_en": "Sodium Hydroxide",
         "cas": ["1310-73-2"], "page": 2},
    ]
    assert audit["page_continuations"] == [{"page": 2, "section": "c.2", "no": 1}]
    assert audit["cas_format_failures"] == audit["cas_checksum_failures"] == []


@pytest.mark.parametrize("content", [None, "{invalid", "{}", '[{"section":"c.2"}]'])
def test_unavailable_catalogue_fails_closed(tmp_path, content):
    path = tmp_path / "kma1360.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    assert screening._load_kma1360(path) == ({}, {})
