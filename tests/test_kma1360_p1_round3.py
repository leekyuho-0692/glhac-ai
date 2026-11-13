"""Third review: context gating, CAS peers, annotation aliases and OCR noise."""
from collections import Counter
import json
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest

from app import intake, models, screening
from app.db import SessionLocal
from app.kma1360 import excluded_name_metadata, name_exclusion_reasons, normalize_name
from scripts import build_kma1360 as build
from test_axis_d1_freeze_issue import api, pending
from test_kma1360_p1 import install_ontology
from test_pending_material_guards import assert_pending_blocked, prepare


CATALOGUE = Path(__file__).resolve().parents[1] / "app/data/kma1360.json"
OCR_NAMES = ["Bismuth Sub.carbonate", "Lilia!", "3-Methyl-1,2-Cyclopentanedi9ne"]
CONFLICTS = [("Hexylene Glycol", "2-Methylpentane-2,4-Diol", "107-41-5"),
             ("Bénzyl Alcohol", "Benzyl Alcohol", "100-51-6")]


@pytest.mark.parametrize("note", ["contains ethanol", "glycerin carrier"])
@pytest.mark.parametrize("name", ["57-55-6", "CAS No. 57-55-6"])
def test_risky_notes_are_reflected_in_final_result_and_match_no_catalogue(monkeypatch, note, name):
    result = screening.screen_merged(name, note=note)
    assert (result["result"], result["status"], result["severity"], result["v1_risk"]) == (
        "NEEDS_EVIDENCE", "mushbooh", "medium", "medium")
    assert result["required_evidence"] == ["source_declaration"]
    assert not intake._material_screening_safe(result)
    monkeypatch.setattr(screening, "_KMA1360_NAMES", {})
    monkeypatch.setattr(screening, "_KMA1360_CAS", {})
    assert result == screening.screen_merged(name, note=note)


@pytest.mark.parametrize("extra", [{"note": "contains ethanol"}, {"note": "glycerin carrier"},
    {"source": "synthetic"}, {"source": "plant"}, {"e_number": "E422"},
    {"cert_no": "unverified"}, {"docs": [SimpleNamespace()]}, {"source_known": False}])
def test_no_kma_lookup_can_influence_any_contextual_decision(monkeypatch, extra):
    expected = screening.screen_merged("57-55-6", **extra)
    def forbidden_lookup(_):
        pytest.fail("KMA lookup ran despite kma_name_only=False")
    monkeypatch.setattr(screening, "_match_kma1360", forbidden_lookup)
    assert screening.screen_merged("57-55-6", **extra) == expected


@pytest.mark.parametrize("extra", [{"note": "contains ethanol"}, {"source": "synthetic"},
    {"e_number": "E422"}, {"cert_no": "unverified"}, {"docs": [SimpleNamespace()]}, {"source_known": False}])
def test_direct_material_screening_uses_the_same_context_gate(monkeypatch, extra):
    material = SimpleNamespace(**{"name": "57-55-6", "e_number": None, "source_known": True,
                                  "evidence_provided": False, **extra})
    def forbidden_lookup(_):
        pytest.fail("Direct material screen used KMA despite extra context")
    monkeypatch.setattr(screening, "_match_kma1360", forbidden_lookup)
    assert screening.screen(material)["matched_uid"] != "ing.propylene_glycol"


@pytest.mark.parametrize("source", ["unknown", "synthetic"])
def test_direct_v1_rule_never_uses_catalogue_with_context(monkeypatch, source):
    def forbidden_lookup(_):
        pytest.fail("Direct v1 rule used KMA despite extra context")
    monkeypatch.setattr(screening, "_match_kma1360", forbidden_lookup)
    assert screening.v1_rule("Benzyl Alcohol", source, None)[1] == "medium"


def test_document_evidence_passes_its_docs_context_to_dictionary_lookup(monkeypatch):
    from app import evidence_map
    install_ontology(monkeypatch, "Propylene Glycol", "halal", "low")
    material = SimpleNamespace(name="57-55-6", e_number=None, source_known=True, evidence_provided=False)
    def forbidden_lookup(_):
        pytest.fail("Evidence coverage used KMA despite submitted documents")
    monkeypatch.setattr(screening, "_match_kma1360", forbidden_lookup)
    monkeypatch.setattr(evidence_map, "coverage", lambda required, docs, **kwargs: {"required": required})
    assert screening.document_evidence(material, [SimpleNamespace()]) == {"required": ["composition_breakdown"]}


@pytest.mark.parametrize("canonical,peer,cas", CONFLICTS)
@pytest.mark.parametrize("status,severity", [("mushbooh", "high"), ("haram", "high"), ("halal", "low")])
@pytest.mark.parametrize("as_alias", [False, True])
def test_all_statuses_and_unicode_aliases_on_cas_peers_take_precedence(
        monkeypatch, canonical, peer, cas, status, severity, as_alias):
    item = install_ontology(monkeypatch, "Unrelated canonical" if as_alias else canonical,
        status, severity, {"en": [canonical]} if as_alias else None)
    for query in (peer, cas, f"{peer} (CAS {cas})"):
        result = screening.screen_merged(query)
        assert (result["decision_by"], result["status"], result["severity"], result["matched_uid"]) == (
            "ontology", status, severity, item.ingredient_uid)
        assert intake._material_autofill_allowed(query) is (status == "halal")


def test_cas_peer_conflict_keeps_stricter_dictionary_item(monkeypatch):
    safe = install_ontology(monkeypatch, "2-Methylpentane-2,4-Diol", "halal", "low")
    risk = models.IngredientOntology(ingredient_uid="ing.peer_risk", canonical_name="Hexylene Glycol",
        aliases={}, default_status="mushbooh", severity="high", required_evidence=["source_declaration"])
    screening.load_ontology(SimpleNamespace(query=lambda _: SimpleNamespace(all=lambda: [safe, risk])))
    assert screening.screen_merged(safe.canonical_name)["matched_uid"] == risk.ingredient_uid


@pytest.mark.parametrize("name", ["Bénzyl Alcohol", "Be\u0301nzyl Alcohol", "ＢÉＮＺＹＬ—ＡＬＣＯＨＯＬ",
                                  " (Benzyl)  Alcohol "])
def test_dictionary_and_kma_share_one_nfkd_identity_function(monkeypatch, name):
    assert screening._kma_key is normalize_name
    assert normalize_name(name) == "benzyl alcohol"
    item = install_ontology(monkeypatch, name)
    assert screening._CACHE["by_kma_name"]["benzyl alcohol"] is item
    assert screening.screen_merged("100-51-6")["matched_uid"] == item.ingredient_uid


@pytest.mark.parametrize("annotation", ["Approx. 45%", "approx 45 percent", "ca. 45%", "45% w/w",
    "45 % w / v", "99.5%", "45", "45 aqueous", "50:50", "1 / 2", "20 ppm", "contains ethanol",
    "technical grade", "see note", "active matter", "NaaP04", "Na3PO4", "C12"])
def test_concentration_ratio_notes_and_formulae_are_never_split_into_aliases(annotation):
    name = f"Compound ({annotation})"
    assert build.split_translation(name) == (name, None)


@pytest.mark.parametrize("alias", ["Benzyl Alcohol", "Propylene Glycol", "2-Ethoxybenzamide",
    "(S)-2-Methyl Acid", "Basic Yellow 57", "Benzophenone-4", "PEG-25 PABA"])
def test_substance_names_and_nomenclature_numbers_still_split(alias):
    assert build.split_translation(f"Compound ({alias})") == ("Compound", alias)


def test_extractor_preserves_mixture_percentages_and_reports_all_parenthetical_annotations():
    name = "Mixture (Approx. 55%) And Compound (Approx. 45%)"
    with fitz.open() as doc:
        page = doc.new_page(width=1000, height=927)
        for xy, text in [((80, 80), "2. Bahan Kimia Hasil Sintesis Anorganik dan Organik"),
                         ((80, 110), "No"), ((185, 125), "(CAS)"), ((380, 110), "Jenis Bahan"),
                         ((80, 150), "1"), ((115, 150), "54717-12-3"), ((290, 150), name)]:
            page.insert_text(xy, text)
        rows, audit = build.extract_document(doc)
    assert rows[0]["name_id"] == name
    assert rows[0]["name_en"] is None
    assert rows[0]["cas"] == ["54717-12-3"]
    assert [x["annotation"] for x in audit["parenthetical_annotations"]] == ["Approx. 55%", "Approx. 45%"]
    assert not audit["translation_candidates"][0]["accepted"]


@pytest.mark.parametrize("name,reason", [(OCR_NAMES[0], "ocr_intraword_dot"),
    (OCR_NAMES[1], "ocr_exclamation"), (OCR_NAMES[2], "ocr_intraword_digit"),
    ("Benzyl|Alcohol", "ocr_noise"), ("Benzyl!!Alcohol", "repeated_special_characters")])
def test_ocr_patterns_are_rejected_even_with_stale_or_missing_metadata(tmp_path, name, reason):
    assert reason in name_exclusion_reasons(name)
    for metadata in ({}, {"name_id": {"indexable": True}}):
        row = {"no": 1, "section": "c.2", "page": 12, "name_id": name,
               "name_en": None, "cas": ["100-51-6"], "name_index": metadata}
        path = tmp_path / "catalogue.json"
        path.write_text(json.dumps([row]))
        names, cas = screening._load_kma1360(path)
        assert names == {}
        assert cas["100-51-6"] == [row]


@pytest.mark.parametrize("name", ["1,2-Hexanediol", "3-Methyl-1,2-Cyclopentanedione",
    "2-Methylpentane-2,4-Diol", "C12", "C12-15 Alkyl Benzoate", "Bicyclo[2.2.1]Heptane"])
def test_chemical_locants_and_carbon_chain_numbers_are_not_ocr_noise(name):
    assert name_exclusion_reasons(name) == []


def test_current_catalogue_all_exclusions_aliases_and_cas_are_consistent():
    rows = json.loads(CATALOGUE.read_text())
    names, cas_index = screening._load_kma1360()
    excluded = []
    for row in rows:
        assert row.get("name_index", {}) == excluded_name_metadata(row)
        if row["name_en"] and "name_en" not in row.get("name_index", {}):
            assert build.translation_exclusion_reasons(row["name_en"]) == []
        for field, metadata in row.get("name_index", {}).items():
            excluded.append((row, field))
            assert metadata["indexable"] is False
            assert screening._match_kma1360(row[field]) == []
            assert row not in names.get(normalize_name(row[field]), [])
        if row["section"] in {"c.1", "c.2"}:
            for cas in row["cas"]:
                assert row in cas_index[cas]
    assert len(rows) == 4112
    assert len(excluded) == 437
    assert Counter(reason for row, field in excluded for reason in row["name_index"][field]["reasons"]) == {
        "unbalanced_brackets": 74, "ocr_intraword_closer": 3, "ocr_noise": 31,
        "repeated_special_characters": 131, "formula_like": 71, "ocr_exclamation": 12,
        "ocr_intraword_dot": 21, "ocr_intraword_digit": 24,
        "ocr_leading_dot": 5, "ocr_suffix_zero": 119}
    by_no = {row["no"]: row for row in rows if row["section"] == "c.2"}
    assert by_no[3420]["name_en"] is None
    assert by_no[3420]["name_id"].endswith("(Approx. 45%)")
    for no in (63, 757, 1020):
        assert by_no[no]["name_index"]["name_id"]["indexable"] is False


@pytest.mark.parametrize("name", OCR_NAMES + ["Approx. 45%"])
def test_new_counterexamples_remain_unconfirmed_and_cannot_autofill(name):
    result = screening.screen_merged(name)
    assert screening._match_kma1360(name) == []
    assert result["result"] == "NEEDS_EVIDENCE"
    assert result["status"] == "mushbooh"
    assert not intake._material_autofill_allowed(name)


@pytest.mark.parametrize("phase", ["entry", "decision", "issue", "checker"])
@pytest.mark.parametrize("name", OCR_NAMES + ["Approx. 45%"])
def test_new_counterexamples_block_all_four_workflow_gates(api, phase, name):
    cid, route, body, role = prepare(api, phase)
    pending(cid, name)
    assert_pending_blocked(api("POST", route, body, role), name)
    with SessionLocal() as db:
        assert db.query(models.HalalCertificate).filter_by(case_id=cid).count() == 0


@pytest.mark.parametrize("phase", ["entry", "decision", "issue", "checker"])
@pytest.mark.parametrize("canonical,peer,cas", CONFLICTS)
@pytest.mark.parametrize("use_cas", [False, True])
def test_cas_peer_and_unicode_conflicts_block_all_four_gates(api, monkeypatch, phase, canonical, peer, cas, use_cas):
    cid, route, body, role = prepare(api, phase)
    with SessionLocal() as db:
        item = models.IngredientOntology(ingredient_uid="ing.kma_round3_gate", canonical_name=canonical,
            aliases={}, default_status="mushbooh", severity="high", required_evidence=["source_declaration"])
        items = db.query(models.IngredientOntology).all() + [item]
        monkeypatch.setattr(screening, "_CACHE", {"by_e": {}, "by_alias": {}, "alias_tok": [], "negated": None})
        screening.load_ontology(SimpleNamespace(query=lambda _: SimpleNamespace(all=lambda: items)))
    query = cas if use_cas else peer
    pending(cid, query)
    assert_pending_blocked(api("POST", route, body, role), query)
    with SessionLocal() as db:
        assert db.query(models.HalalCertificate).filter_by(case_id=cid).count() == 0


@pytest.mark.parametrize("phase", ["entry", "decision", "issue", "checker"])
@pytest.mark.parametrize("note", ["contains ethanol", "glycerin carrier"])
def test_cas_notes_rescreen_and_persist_before_all_four_gates(api, monkeypatch, phase, note):
    real_screen = screening.screen_merged
    def noted_api(method, path, body=None, role="applicant"):
        if method == "POST" and path.endswith("/materials"):
            body = {**body, "name": "57-55-6", "note": note}
        return api(method, path, body, role)
    with monkeypatch.context() as setup:
        def prior_review(name, *args, **kwargs):
            return real_screen(name) if name == "57-55-6" else real_screen(name, *args, **kwargs)
        setup.setattr(screening, "screen_merged", prior_review)
        cid, route, body, role = prepare(noted_api, phase)
    with SessionLocal() as db:
        material = db.query(models.Material).filter_by(case_id=cid).one()
        result = screening.apply_screen(material)
        assert (result["result"], result["status"], result["severity"]) == (
            "NEEDS_EVIDENCE", "mushbooh", "medium")
        db.commit()
    response = api("POST", route, body, role)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    codes = {item["code"] for item in detail.get("blockers", [detail])}
    assert codes & {"MATERIAL_EVIDENCE_INCOMPLETE", "PRODUCT_MATERIAL_REVIEW_INCOMPLETE"}
    with SessionLocal() as db:
        assert db.query(models.HalalCertificate).filter_by(case_id=cid).count() == 0
