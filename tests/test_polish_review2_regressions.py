"""Second review: merged OCR cells and long ingredient rows with metadata."""
from copy import deepcopy
import csv
import json
from pathlib import Path

import pytest

from app import intake, models, screening
from app.kma1360 import has_merged_chemical_names, name_exclusion_reasons, normalize_name
from scripts import build_kma1360 as build
from test_package_autofill import P1_ROUTES, _p1_ingest, case, offline, round4_http


ROOT = Path(__file__).resolve().parents[1]
MERGED = "2,6-Octadienoic Acid, 3,7- Dimethyl, Ethyl Ester 2-Hexylcyclopent-2-En-1-One"
SEMICOLON_MERGED = "Prop-2-Yn-1-Ol; 1-(2,6,6-Trimethyl-1-Cyclohex-2- Enyl)Pentan-3-One"
LONG_NAME = "2,4-Dimethyl-2-(5,5,8,8-Tetramethyl-6,7-Dihydronaphthalen-2-Yl) -1,3-Dioxolane"
MERGED_FIXES = {("b", 5), ("b", 9), *(('c.2', no) for no in
    (475, 3501, 3610, 3869, 3872, 3873, 3874, 3875, 3876))}


@pytest.mark.parametrize("suffix", ["", " (CAS 13058-12-3)", " (CAS 95-41-0)"])
def test_merged_name_and_either_cas_cannot_pass_or_autofill(monkeypatch, suffix):
    monkeypatch.setattr(screening, "_CACHE", {
        "by_e": {}, "by_alias": {}, "by_kma_name": {}, "alias_tok": [], "negated": None})
    query = MERGED + suffix
    assert "merged_chemical_names" in name_exclusion_reasons(query)
    assert screening._kma_query(query) is None
    assert screening._match_kma1360(query) == []
    actual = screening.screen_merged(query)
    assert actual["decision_by"] != "kma1360"
    assert actual["result"] == "NEEDS_EVIDENCE"
    assert not intake._material_autofill_allowed(query)


@pytest.mark.parametrize("name", [
    MERGED, MERGED.lower(), MERGED.replace("Ester 2-", "Ester\t2-"),
    SEMICOLON_MERGED, "2-Hepten-4-One 2-Hexylcyclopent-2-En-1-One",
])
@pytest.mark.parametrize("metadata", [{}, {"name_id": {"indexable": True}}])
def test_merged_names_fail_loader_and_stale_runtime_index(tmp_path, monkeypatch, name, metadata):
    row = {"no": 1, "section": "c.2", "page": 12, "name_id": name,
           "name_en": None, "cas": ["13058-12-3"], "name_index": metadata}
    path = tmp_path / "catalogue.json"
    path.write_text(json.dumps([row]))
    names, cas = screening._load_kma1360(path)
    assert names == {}
    assert cas == {"13058-12-3": [row]}
    monkeypatch.setattr(screening, "_KMA1360_NAMES", {normalize_name(name): [row]})
    monkeypatch.setattr(screening, "_KMA1360_CAS", cas)
    for query in (name, f"{name} (CAS 13058-12-3)"):
        assert screening._kma_query(query) is None
        assert screening._match_kma1360(query) == []
        assert not intake._material_autofill_allowed(query)


@pytest.mark.parametrize("name", [
    LONG_NAME,
    "(Z)-3-Hexen-1-Yl 3-Methylbutanoate", "Allyl 3,5,5-Trimethylhexanoate",
    "Carbonic Acid, Ethyl 2,3,6-Trimethylcyclohexyl Ester",
    "Limonene 1,2-Epoxide", "Copper(II) Sulfate 5-Hydrate",
    "1,2-Propanediol 1-Monomethyl Ether 2-Acetate (PGMAC)",
])
def test_multiple_locants_and_substituents_remain_indexable(tmp_path, name):
    assert not has_merged_chemical_names(name)
    assert name_exclusion_reasons(name) == []
    assert screening._kma_query(name) == (normalize_name(name), None)
    row = {"no": 1, "section": "c.2", "page": 12, "name_id": name,
           "name_en": None, "cas": []}
    path = tmp_path / "catalogue.json"
    path.write_text(json.dumps([row]))
    assert screening._load_kma1360(path)[0] == {normalize_name(name): [row]}


def test_inverted_ester_name_is_not_a_merge():
    name = "Propanoic Acid, 2-Methyl-, 2,2-Dimethyl-1-(1-Methylethyl)-1,3- Propanediyl Ester"
    assert not has_merged_chemical_names(name)
    # The pre-existing punctuation rule rejects '-,' independently of merges.
    assert name_exclusion_reasons(name) == ["repeated_special_characters"]


def test_full_table_merge_audit_keeps_originals_excluded():
    with (ROOT / "app/data/kma1360_ocr_fixes.tsv").open(newline="") as stream:
        fixes = list(csv.DictReader(stream, delimiter="\t"))
    rows = json.loads((ROOT / "app/data/kma1360.json").read_text())
    by_key = {(row["section"], row["no"], row["page"]): row for row in rows}
    excluded = set()
    for fix in fixes:
        row = by_key[fix["section"], int(fix["no"]), int(fix["page"])]
        if len(row["cas"]) > 1:
            assert fix["status"] == "excluded"
            assert "merged_cells" in fix["exclusion_reasons"].split("; ")
        if "merged_cells" not in fix["exclusion_reasons"].split("; "):
            continue
        excluded.add((row["section"], row["no"]))
        assert fix["status"] == "excluded"
        assert row[fix["field"]] == fix["original"]
        assert f"{fix['field']}_ocr_raw" not in row
        assert row["name_index"][fix["field"]]["indexable"] is False
        assert screening._match_kma1360(row[fix["field"]]) == []
    assert excluded == MERGED_FIXES


@pytest.mark.parametrize("name,cas", [(MERGED, ["13058-12-3"]),
    (SEMICOLON_MERGED, ["13058-12-3"]), ("A Valid Name", ["13058-12-3", "95-41-0"])])
def test_builder_rejects_new_merged_corrections_before_mutation(tmp_path, name, cas):
    rows = [{"no": 1, "section": "c.2", "page": 12, "name_id": "Raw!",
             "name_en": None, "cas": cas}]
    before = deepcopy(rows)
    fix = {"section": "c.2", "no": "1", "page": "12", "field": "name_id",
           "cas": "; ".join(cas), "original": "Raw!", "corrected": name,
           "status": "applied", "exclusion_reasons": "", "evidence": "OCR 재판독",
           "crop_path": "reviewed.png"}
    path = tmp_path / "fixes.tsv"
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(fix), delimiter="\t")
        writer.writeheader()
        writer.writerow(fix)
    with pytest.raises(ValueError, match="merged"):
        build.apply_ocr_fixes(rows, path)
    assert rows == before


@pytest.mark.parametrize("separator", [" | ", "\t", " ｜ "])
def test_long_table_name_does_not_close_material_section(separator):
    row = separator.join([LONG_NAME, "CAS 131812-67-4", "10 % w/w"])
    assert len(row) > 100
    assert intake._autofill_material_row_evidence(row) == ([LONG_NAME], False)
    text = f"Ingredients\nSalt 10%\n{row}\nWater | 90%"
    assert intake._material_scan_text(text, material_only=True).splitlines() == ["Salt", LONG_NAME, "Water"]
    fields = intake.aggregate_fields([{"doc_type": "coa_msds", "text_full": text,
        "fields": {"material_names": ["Salt", LONG_NAME, "Water"]}}])
    assert set(fields["materials"]) == {"Salt", LONG_NAME, "Water"}
    assert fields["pending"] == []


@pytest.mark.parametrize("route", P1_ROUTES[:-1])
def test_long_table_name_registers_three_materials_on_automatic_routes(
        case, monkeypatch, round4_http, route):
    db, application = case
    product = models.Product(case_id=application.case_id, name="Face Serum")
    db.add(product)
    db.commit()
    text = (f"Product name: Face Serum\nIngredients\nSalt 10%\n{LONG_NAME}"
            " | CAS 131812-67-4 | 10 % w/w\nWater | 90%")
    _p1_ingest(case, monkeypatch, round4_http, route, text,
               {"material_names": ["Salt", LONG_NAME, "Water"], "product_names": ["Face Serum"]})
    materials = db.query(models.Material).filter_by(case_id=application.case_id).all()
    assert {row.name for row in materials} == {"Salt", LONG_NAME, "Water"}
    assert {(link.product_id, link.material_id) for link in
            db.query(models.ProductMaterial).filter_by(case_id=application.case_id)} == {
                (product.product_id, row.material_id) for row in materials}
    from app.main import _pending_extractions
    assert not [item for item in _pending_extractions(db, application.case_id)
                if item["kind"] == "material_names"]
