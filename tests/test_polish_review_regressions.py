"""Review reproductions: OCR confusion and prose after a numeric token."""
import json
from pathlib import Path

import pytest

from app import intake, models, screening
from app.kma1360 import name_exclusion_reasons, normalize_name
from test_package_autofill import (
    P1_ROUTES, _p1_ingest, case, offline, round4_http,
)


REVIEWED_NAMES = [
    (1238, "1-(3,3-Dimethylbicyclo[2.2.1]Hept-2-Yl) Ethane-1-One",
     "1-(3,3-Dimethylbicyclo[2.2.1]Hept-2-YI) Ethane-1-One"),
    (1578, "(2E,4E)-Octa-2,4-Dien-1-Ol", "(2E,4E)-Octa-2,4-Dien-1-O1"),
    (1579, "2,4-Nonadien-1-Ol", "2,4-Nonadien-1-O1"),
    (1580, "(2E,4E)-Deca-2,4-Dien-1-Ol", "(2E,4E)-Deca-2,4-Dien-1-O1"),
    (3463, "4-Methyl-8-Methylene Tricyclo(3.3.1.1 3.7)Decan-2-Ol",
     "4-Methyl-8-Methylene Tricyclo(3.3.1.1 3.7)Decan-2-O1"),
]


def empty_ontology(monkeypatch):
    monkeypatch.setattr(screening, "_CACHE", {
        "by_e": {}, "by_alias": {}, "by_kma_name": {}, "alias_tok": [], "negated": None})


@pytest.mark.parametrize("no,printed,misread", REVIEWED_NAMES)
def test_reviewed_printed_name_passes_but_second_ocr_misread_cannot_autofill(
        monkeypatch, no, printed, misread):
    empty_ontology(monkeypatch)
    assert no in {row["no"] for row in screening._match_kma1360(printed)}
    actual = screening.screen_merged(printed)
    assert (actual["decision_by"], actual["result"]) == ("kma1360", "PASS")
    assert intake._material_autofill_allowed(printed)
    assert screening._match_kma1360(misread) == []
    actual = screening.screen_merged(misread)
    assert actual["decision_by"] != "kma1360"
    assert actual["result"] == "NEEDS_EVIDENCE"
    assert not intake._material_autofill_allowed(misread)


@pytest.mark.parametrize("name", [
    *(name for _, _, name in REVIEWED_NAMES),
    "2,4-Nonadien-1-o1", "2,4-Nonadien-1-OI", "Hex-1-Y1 Acetate",
    "EthyIhexyl", "MethyIene", "EthyI Hexanoate",
    "２，４－Ｎｏｎａｄｉｅｎ－１－Ｏ１",
])
@pytest.mark.parametrize("metadata", [{}, {"name_id": {"indexable": True}}],
                         ids=["missing-metadata", "stale-metadata"])
def test_loader_and_input_parser_both_reject_confusions_even_with_stale_index(
        tmp_path, monkeypatch, name, metadata):
    empty_ontology(monkeypatch)
    assert "ocr_letter_confusion" in name_exclusion_reasons(name)
    row = {"no": 1, "section": "c.2", "page": 12, "name_id": name,
           "name_en": None, "cas": ["100-51-6"], "name_index": metadata}
    path = tmp_path / "catalogue.json"
    path.write_text(json.dumps([row]))
    names, cas = screening._load_kma1360(path)
    assert names == {}
    assert cas["100-51-6"] == [row]
    # An index built by an older process still cannot bypass input validation.
    monkeypatch.setattr(screening, "_KMA1360_NAMES", {normalize_name(name): [row]})
    monkeypatch.setattr(screening, "_KMA1360_CAS", cas)
    for query in (name, f"{name} (CAS 100-51-6)"):
        assert screening._kma_query(query) is None
        assert screening._match_kma1360(query) == []
        assert screening.screen_merged(query)["decision_by"] != "kma1360"
        assert not intake._material_autofill_allowed(query)


@pytest.mark.parametrize("name", [
    "C12", "C12-15 Alkyl Benzoate", "1,2-Hexanediol", "Hex-1-yl",
    "CI 77002", "Vitamin B1", "2,4-Nonadien-1-Ol", "2,4-NONADIEN-1-OL",
    "2-Hepten-4-One", "But-1-ene", "Bicyclo[2.2.1]Heptane", "Isoamyl Isobutyrate",
])
def test_chemical_locants_codes_and_printed_suffixes_are_not_false_positives(tmp_path, name):
    assert name_exclusion_reasons(name) == []
    assert screening._kma_query(name) == (normalize_name(name), None)
    row = {"no": 1, "section": "c.2", "page": 12, "name_id": name,
           "name_en": None, "cas": []}
    path = tmp_path / "catalogue.json"
    path.write_text(json.dumps([row]))
    assert screening._load_kma1360(path)[0] == {normalize_name(name): [row]}


def test_previously_unreviewed_confusable_names_are_excluded_without_repair():
    rows = json.loads((Path(__file__).resolve().parents[1] / "app/data/kma1360.json").read_text())
    names, cas = screening._load_kma1360()
    for no in (575, 882, 888, 889, 1062, 1087, 1149, 1620, 2227):
        row = next(row for row in rows if row["section"] == "c.2" and row["no"] == no)
        assert "name_id_ocr_raw" not in row
        assert row["name_index"]["name_id"]["indexable"] is False
        assert "ocr_letter_confusion" in row["name_index"]["name_id"]["reasons"]
        assert row not in names.get(normalize_name(row["name_id"]), [])
        assert screening._match_kma1360(row["name_id"]) == []
        for number in row["cas"]:
            assert row in cas[number]


@pytest.mark.parametrize("route", P1_ROUTES[:-1])
@pytest.mark.parametrize("repeats", [1, 4], ids=["short-numeric-prose", "460-char-numeric-prose"])
def test_numeric_indonesian_prose_keeps_water_pending_on_all_automatic_routes(
        case, monkeypatch, round4_http, route, repeats):
    db, application = case
    product = models.Product(case_id=application.case_id, name="Face Serum")
    db.add(product)
    db.commit()
    sentence = ("Bilas peralatan produksi setiap 2 jam lalu keringkan seluruh permukaan "
                "sebelum digunakan kembali dengan air bersih ")
    instructions = sentence * repeats
    if repeats == 4:
        assert len(instructions) == 460
    text = ("Product name: Face Serum\nIngredients\nSalt 100%\n"
            + instructions + "\nWater | 100%")
    assert intake._autofill_material_row_evidence(instructions) == ([], False)
    assert intake._material_scan_text(text, material_only=True) == "Salt"
    _p1_ingest(case, monkeypatch, round4_http, route, text,
               {"material_names": ["Salt", "Water"], "product_names": ["Face Serum"]})
    salt = db.query(models.Material).filter_by(case_id=application.case_id).one()
    assert salt.name == "Salt"
    assert {(link.product_id, link.material_id) for link in
            db.query(models.ProductMaterial).filter_by(case_id=application.case_id)} == {
                (product.product_id, salt.material_id)}
    from app.main import _pending_extractions
    assert {item["value"] for item in _pending_extractions(db, application.case_id)
            if item["kind"] == "material_names"} == {"Water"}
