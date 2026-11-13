"""P1 acceptance cases: complete identity, unchanged risks and ontology precedence."""
import json
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest

from app import intake, models, screening
from app.db import SessionLocal
from app.kma1360 import excluded_name_metadata, name_exclusion_reasons
from scripts import build_kma1360 as build
from test_axis_d1_freeze_issue import api, pending
from test_pending_material_guards import assert_pending_blocked, prepare


CATALOGUE = Path(__file__).resolve().parents[1] / "app/data/kma1360.json"
OCR_NAMES = ["NaaP04", "2-Ethylbutyra).dehyde", "Hydroxyphenyllf'ropionate"]
MIXTURES = [
    "1310-73-2 / 56-81-5", "1310-73-2/56-81-5", "1310-73-2 / 100-51-6",
    "1310-73-2 / 1310-73-2", "Squalene (CAS 1310-73-2)",
    "Sodium Hydroxide (100-51-6)", "Sodium Hydroxide (1310-73-2) + Squalene",
    "1310-73-2 / novel ingredient", "1310-73-2 + Water",
    "Sodium Hydroxide (1310-73-2, 56-81-5)",
]


def install_ontology(monkeypatch, name, status="mushbooh", severity="high", aliases=None):
    item = models.IngredientOntology(
        ingredient_uid="ing.kma_p1", canonical_name=name, aliases=aliases or {},
        default_status=status, severity=severity, category="test",
        required_evidence=[] if status == "halal" else ["source_declaration"], sources=["test ontology"])
    monkeypatch.setattr(screening, "_CACHE", {"by_e": {}, "by_alias": {}, "alias_tok": [], "negated": None})
    screening.load_ontology(SimpleNamespace(query=lambda _: SimpleNamespace(all=lambda: [item])))
    return item


@pytest.mark.parametrize("name", MIXTURES + OCR_NAMES + ["Na3PO4", "(Benzyl Alcohol", "Benzyl Alcohol)"])
def test_incomplete_identity_never_applies_kma_or_changes_baseline(monkeypatch, name):
    result = screening.screen_merged(name)
    assert screening._match_kma1360(name) == []
    assert result["decision_by"] != "kma1360"
    monkeypatch.setattr(screening, "_KMA1360_NAMES", {})
    monkeypatch.setattr(screening, "_KMA1360_CAS", {})
    assert result == screening.screen_merged(name)


@pytest.mark.parametrize("name", ["1310-73-2/56-81-5", "Squalene (CAS 1310-73-2)"] + OCR_NAMES)
def test_review_counterexamples_never_autofill(name):
    assert not intake._material_autofill_allowed(name)


@pytest.mark.parametrize("field", ["note", "source", "e_number", "cert_no"])
@pytest.mark.parametrize("text", ["contains ethanol", "glycerin carrier", "contains lard", "Squalene carrier",
                                  "Water", "unknown ingredient", "unspecified supplier"])
@pytest.mark.parametrize("name", ["1,2-Hexanediol", "Benzyl Alcohol"])
def test_additional_text_keeps_the_existing_decision(monkeypatch, field, text, name):
    kwargs = {field: text}
    actual = screening.screen_merged(name, **kwargs)
    explanation = screening.explain(name, **kwargs)
    if actual["decision_by"] == "ontology":
        assert explanation["matched_uid"] == actual["matched_uid"]
        assert explanation["status"] == actual["status"]
        assert explanation["required_evidence"] == actual["required_evidence"]
    monkeypatch.setattr(screening, "_KMA1360_NAMES", {})
    monkeypatch.setattr(screening, "_KMA1360_CAS", {})
    baseline = screening.screen_merged(name, **kwargs)
    assert actual == baseline
    assert actual["decision_by"] != "kma1360"
    assert not intake._material_screening_safe(actual)


@pytest.mark.parametrize("extra", [{"source": "plant"}, {"source": "synthetic"},
                                   {"docs": [SimpleNamespace()]}, {"source_known": False}])
def test_alcohol_disambiguation_never_lowers_risk_with_extra_context(monkeypatch, extra):
    actual = screening.screen_merged("Benzyl Alcohol", **extra)
    monkeypatch.setattr(screening, "_KMA1360_NAMES", {})
    monkeypatch.setattr(screening, "_KMA1360_CAS", {})
    assert actual == screening.screen_merged("Benzyl Alcohol", **extra)
    assert actual["decision_by"] == "ontology"
    assert actual["status"] == "mushbooh"


@pytest.mark.parametrize("note", ["contains ethanol", "glycerin carrier"])
def test_reviewed_note_counterexamples_keep_medium_evidence_requirement(note):
    result = screening.screen_merged("1,2-Hexanediol", note=note)
    assert (result["result"], result["status"], result["severity"]) == (
        "NEEDS_EVIDENCE", "mushbooh", "medium")
    assert result["decision_by"] == "v1_rule"
    assert result["v1_risk"] == "medium"
    assert result["required_evidence"] == ["source_declaration"]


@pytest.mark.parametrize("extra", [{"source": "synthetic"}, {"source": "plant"},
                                   {"source_known": False}, {"docs": [SimpleNamespace()]},
                                   {"e_number": "unrecognized"}])
def test_existing_source_rules_and_non_name_context_are_not_kma(extra):
    assert screening.screen_merged("1,2-Hexanediol", **extra)["decision_by"] != "kma1360"


@pytest.mark.parametrize("name,cas", [("Benzyl-Alcohol", "100-51-6"), ("1,2 Hexanediol", "6920-22-5"),
                                     ("(ＢＥＮＺＹＬ—ＡＬＣＯＨＯＬ)", "100-51-6"),
                                     ("Benzil Alkohol", "100-51-6")])
@pytest.mark.parametrize("status,severity", [("mushbooh", "high"), ("haram", "high"), ("halal", "low")])
@pytest.mark.parametrize("as_alias", [False, True])
def test_all_ontology_statuses_and_aliases_precede_cas(monkeypatch, name, cas, status, severity, as_alias):
    item = install_ontology(monkeypatch, "Test canonical" if as_alias else name, status, severity,
                            {"en": [name]} if as_alias else None)
    for query in (cas, name):
        result = screening.screen_merged(query)
        assert result["decision_by"] == "ontology"
        assert (result["status"], result["severity"], result["matched_uid"]) == (
            status, severity, item.ingredient_uid)
    assert intake._material_autofill_allowed(cas) is (status == "halal")


@pytest.mark.parametrize("name", ["Propylene Glycol", "Sodium Hydroxide", "57-55-6", "1310-73-2"])
def test_real_safe_ontology_is_confirmed_without_being_relabelled_as_kma(name):
    result = screening.screen_merged(name)
    assert result["decision_by"] == "ontology"
    assert (result["result"], result["status"], result["severity"]) == ("PASS", "halal", "low")
    assert intake._material_autofill_allowed(name)


@pytest.mark.parametrize("name", ["1,2-Hexanediol", "Propylene Glycol", "100-51-6",
                                  "Benzyl Alcohol (100-51-6)"])
def test_acceptance_positive_cases_remain_confirmed(name):
    result = screening.screen_merged(name)
    assert (result["result"], result["status"], result["severity"]) == ("PASS", "halal", "low")
    assert result["decision_by"] in {"ontology", "kma1360"}
    assert intake._material_autofill_allowed(name)


@pytest.mark.parametrize("phase", ["entry", "decision", "issue", "checker"])
@pytest.mark.parametrize("name", ["1310-73-2/56-81-5", "Squalene (CAS 1310-73-2)"] + OCR_NAMES)
def test_review_counterexamples_keep_all_four_gates_at_409(api, phase, name):
    cid, route, body, role = prepare(api, phase)
    pending(cid, name)
    assert_pending_blocked(api("POST", route, body, role), name)
    with SessionLocal() as db:
        assert db.query(models.HalalCertificate).filter_by(case_id=cid).count() == 0


@pytest.mark.parametrize("phase", ["entry", "decision", "issue", "checker"])
@pytest.mark.parametrize("name,cas", [("Benzyl-Alcohol", "100-51-6"), ("1,2 Hexanediol", "6920-22-5")])
def test_normalized_ontology_conflicts_keep_all_four_gates_at_409(api, monkeypatch, phase, name, cas):
    cid, route, body, role = prepare(api, phase)
    # Add to the existing dictionary so unrelated safe prerequisite materials stay safe.
    with SessionLocal() as db:
        item = models.IngredientOntology(ingredient_uid="ing.kma_gate_conflict", canonical_name=name,
            aliases={}, default_status="mushbooh", severity="high", required_evidence=["source_declaration"])
        items = db.query(models.IngredientOntology).all() + [item]
        monkeypatch.setattr(screening, "_CACHE", {"by_e": {}, "by_alias": {}, "alias_tok": [], "negated": None})
        screening.load_ontology(SimpleNamespace(query=lambda _: SimpleNamespace(all=lambda: items)))
    pending(cid, cas)
    assert_pending_blocked(api("POST", route, body, role), cas)


@pytest.mark.parametrize("phase", ["entry", "decision", "issue", "checker"])
@pytest.mark.parametrize("note", ["contains ethanol", "glycerin carrier"])
def test_note_counterexamples_keep_all_four_gates_at_409(api, monkeypatch, phase, note):
    # Persist the note before taking the scope snapshot. Prepare later workflow
    # stages under the old decision, then re-screen through the real persistence
    # path: changing the screening result must preserve all evidence gates.
    real_screen = screening.screen_merged
    def noted_api(method, path, body=None, role="applicant"):
        if method == "POST" and path.endswith("/materials"):
            body = {**body, "name": "1,2-Hexanediol", "note": note}
        return api(method, path, body, role)

    with monkeypatch.context() as setup:
        def prior_review(name, *args, **kwargs):
            if name == "1,2-Hexanediol":
                return real_screen(name)
            return real_screen(name, *args, **kwargs)
        setup.setattr(screening, "screen_merged", prior_review)
        cid, route, body, role = prepare(noted_api, phase)
    with SessionLocal() as db:
        material = db.query(models.Material).filter_by(case_id=cid).one()
        risk = screening.apply_screen(material)
        assert (risk["status"], risk["severity"]) == ("mushbooh", "medium")
        db.commit()
    response = api("POST", route, body, role)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    codes = {v["code"] for v in detail.get("blockers", [detail])}
    assert codes & {"MATERIAL_EVIDENCE_INCOMPLETE", "PRODUCT_MATERIAL_REVIEW_INCOMPLETE"}, response.text
    with SessionLocal() as db:
        assert db.query(models.HalalCertificate).filter_by(case_id=cid).count() == 0


def test_catalogue_exclusions_are_complete_and_cas_indexes_are_preserved(monkeypatch, tmp_path):
    # Preserve the 282-name v2 regression corpus and its original assertions.
    # Run the current loader/matcher over those old records, including removed
    # aliases. The current full catalogue is checked separately in round3.
    path = Path(__file__).with_name("fixtures") / "kma1360_v2_excluded.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    for row in rows:
        row["name_index"] = excluded_name_metadata(row)
    current = tmp_path / "legacy-exclusions.json"
    current.write_text(json.dumps(rows))
    names, cas_index = screening._load_kma1360(current)
    monkeypatch.setattr(screening, "_KMA1360_NAMES", names)
    monkeypatch.setattr(screening, "_KMA1360_CAS", cas_index)
    excluded = []
    for row in rows:
        metadata = row.get("name_index", {})
        assert set(row) <= {"no", "section", "name_id", "name_en", "cas", "page", "name_index"}
        assert metadata == excluded_name_metadata(row)
        for field, value in metadata.items():
            assert value["indexable"] is False and value["reasons"]
            assert screening._match_kma1360(row[field]) == []
            assert not any((r["section"], r["no"]) == (row["section"], row["no"])
                           for r in names.get(screening._kma_key(row[field]), []))
            excluded.append(row[field])
        if row["section"].startswith("c."):
            for cas in row["cas"]:
                assert screening._valid_cas(cas)
                assert row in cas_index[cas]
                assert row in screening._match_kma1360(cas)
    assert set(OCR_NAMES) <= set(excluded)
    assert len(excluded) == 282
    assert sum(row["name_id"].count("(") != row["name_id"].count(")") for row in rows) == 63
    assert all("unbalanced_brackets" in row["name_index"]["name_id"]["reasons"] for row in rows
               if row["name_id"].count("(") != row["name_id"].count(")"))


@pytest.mark.parametrize("name,reason", [("NaaP04", "formula_like"), ("Na3PO4", "formula_like"),
    ("Na₃PO₄", "formula_like"), ("2-Ethylbutyra).dehyde", "unbalanced_brackets"),
    ("Hydroxyphenyllf'ropionate", "ocr_noise"), ("Benzyl|Alcohol", "ocr_noise"),
    ("Example..name", "repeated_special_characters"), ("([Example)]", "unbalanced_brackets")])
def test_new_or_missing_metadata_cannot_admit_ocr_aliases(tmp_path, name, reason):
    assert reason in name_exclusion_reasons(name)
    row = {"no": 1, "section": "c.2", "name_id": name, "name_en": None, "cas": ["100-51-6"], "page": 12}
    for metadata in ({}, {"name_index": {"name_id": {"indexable": True}}}):
        path = tmp_path / "catalogue.json"
        path.write_text(json.dumps([{**row, **metadata}]))
        names, cas = screening._load_kma1360(path)
        assert names == {}
        assert len(cas["100-51-6"]) == 1


def test_extractor_records_excluded_alias_without_repairing_text_or_dropping_cas(tmp_path):
    # The current extractor must retain a formula in the original name. Keep
    # the old extraction assertions below as a migration fixture contract.
    # Also verify that the current loader still rejects that old stored alias.
    with fitz.open() as doc:
        page = doc.new_page(width=606, height=927)
        for xy, text in [((80, 80), "2. Bahan Kimia Hasil Sintesis Anorganik dan Organik"),
                         ((80, 110), "No"), ((185, 125), "(CAS)"), ((380, 110), "Jenis Bahan"),
                         ((80, 150), "1"), ((115, 150), "7601-54-9"),
                         ((290, 150), "Natrium Fosfat (NaaP04)")]:
            page.insert_text(xy, text)
        rows, audit = build.extract_document(doc)
    assert rows[0]["name_id"] == "Natrium Fosfat (NaaP04)"
    assert rows[0]["name_en"] is None
    assert audit["translation_candidates"][0]["accepted"] is False
    fixture = Path(__file__).with_name("fixtures") / "kma1360_v2_formula_extraction.json"
    legacy = json.loads(fixture.read_text(encoding="utf-8"))
    rows, audit = legacy["rows"], legacy["audit"]
    assert excluded_name_metadata(rows[0]) == rows[0]["name_index"]
    assert name_exclusion_reasons(rows[0]["name_en"]) == ["formula_like"]
    previous = tmp_path / "previous-extraction.json"
    previous.write_text(json.dumps(rows))
    names, cas = screening._load_kma1360(previous)
    assert screening._kma_key("NaaP04") not in names
    assert cas["7601-54-9"] == rows
    assert rows[0]["name_en"] == "NaaP04"
    assert rows[0]["name_index"]["name_en"] == {"indexable": False, "reasons": ["formula_like"]}
    assert rows[0]["cas"] == ["7601-54-9"]
    assert audit["excluded_names"][0]["name"] == "NaaP04"
    assert audit["text_layer_pages"] == [1]
