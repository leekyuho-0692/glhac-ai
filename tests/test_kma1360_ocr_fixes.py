"""Frozen OCR decisions, reproducible provenance, and unchanged runtime gates."""
from collections import Counter
from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest

from app import intake, screening
from app.kma1360 import excluded_name_metadata, name_exclusion_reasons, normalize_name
from scripts import build_kma1360 as build
from test_kma1360_p1 import install_ontology


ROOT = Path(__file__).resolve().parents[1]
CATALOGUE = ROOT / "app/data/kma1360.json"
TABLE = ROOT / "app/data/kma1360_ocr_fixes.tsv"
ROWS = json.loads(CATALOGUE.read_text(encoding="utf-8"))
BY_KEY = {(row["section"], row["no"], row["page"]): row for row in ROWS}
with TABLE.open(encoding="utf-8", newline="") as stream:
    FIXES = list(csv.DictReader(stream, delimiter="\t"))
POSITIVE = [
    (1829, "2-Hepten-4-One", "4643-25-8"),
    (184, "Klobetasol Propionat (Clobetasol Propionate)", "25122-46-7"),
    (3720, "CI 77002", "14762-49-3"),
    (1184, "Pure Myrrh Gum", "9000-45-7"),
]


def original_rows():
    rows = deepcopy(ROWS)
    for row in rows:
        for field in ("name_id", "name_en"):
            if f"{field}_ocr_raw" in row:
                row[field] = row.pop(f"{field}_ocr_raw")
        row.pop("name_index", None)
        metadata = excluded_name_metadata(row)
        if metadata:
            row["name_index"] = metadata
    return rows


def write_fixes(path, fixes):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(FIXES[0]), delimiter="\t")
        writer.writeheader()
        writer.writerows(fixes)
    return path


def empty_ontology(monkeypatch):
    monkeypatch.setattr(screening, "_CACHE", {
        "by_e": {}, "by_alias": {}, "by_kma_name": {}, "alias_tok": [], "negated": None})


def test_frozen_table_decisions_and_complete_original_provenance():
    assert len(FIXES) == 283
    assert Counter(fix["status"] for fix in FIXES) == {"applied": 262, "excluded": 21}
    assert Counter(reason for fix in FIXES for reason in fix["exclusion_reasons"].split("; ")
                   if reason) == {
        "multiline_fragment": 1, "different_cas_collision": 12, "unknown_cas_collision": 1,
        "merged_cells": 11}
    assert len({(f["section"], f["no"], f["page"], f["field"]) for f in FIXES}) == 283
    for fix in FIXES:
        row = BY_KEY[fix["section"], int(fix["no"]), int(fix["page"])]
        field = fix["field"]
        assert "; ".join(row["cas"]) == fix["cas"]
        assert "OCR 재판독" in fix["evidence"]
        assert fix["crop_path"].endswith(f"/crops/{fix['no']}_{field}.png")
        assert len(fix["crop_sha256"]) == 64
        assert fix["audit_path"].endswith(f"/audit/ocr/{fix['no']}_{field}.json")
        if fix["status"] == "applied":
            assert row[field] == fix["corrected"]
            assert row[f"{field}_ocr_raw"] == fix["original"]
            assert not fix["exclusion_reasons"]
        else:
            assert row[field] == fix["original"]
            assert f"{field}_ocr_raw" not in row
            assert fix["exclusion_reasons"] and fix["review_notes"]
    # This digest fixes every original name, CAS, location, and ordering, including
    # all 88 unclear / 66 same records. It is independent of the correction table.
    original = json.dumps(original_rows(), ensure_ascii=False, indent=2) + "\n"
    assert hashlib.sha256(original.encode()).hexdigest() == (
        "53cb8fe50153a932a18e2d23fc9c6da51cc3b83d5aee48081151ba213b6fb43e")
    # Frozen from d007771:app/data/kma1360.json, independently of name_index.
    # Preserve field/row order, Unicode, two-space indentation, and a final LF.
    source_fields = ("no", "section", "name_id", "name_en", "cas", "page")
    original_content = json.dumps(
        [{field: row[field] for field in source_fields} for row in original_rows()],
        ensure_ascii=False, indent=2) + "\n"
    assert hashlib.sha256(original_content.encode("utf-8")).hexdigest() == (
        "dc46e13950261e097e9673e5c9a8ff5119ade72639e455033126e01d117cb5c6")


def test_regeneration_and_reapplication_preserve_all_frozen_decisions():
    rows, audit = original_rows(), {}
    assert build.apply_ocr_fixes(rows, audit=audit) == ROWS
    assert build.apply_ocr_fixes(rows) == ROWS
    assert audit["ocr_corrections"]["applied"] == 262
    assert audit["ocr_corrections"]["excluded"] == 21
    assert len(audit["excluded_names"]) == 184


@pytest.mark.parametrize("no", [1184, 1185])
def test_reviewed_alias_can_be_regenerated_after_leading_dot_split_rejection(tmp_path, no):
    fix = deepcopy(next(f for f in FIXES if int(f["no"]) == no))
    fix["page"] = "1"
    name = f"{fix['other_name']} ({fix['original']})"
    with fitz.open() as document:
        page = document.new_page(width=1000, height=927)
        for xy, text in [((80, 80), "2. Bahan Kimia Hasil Sintesis Anorganik dan Organik"),
                         ((80, 110), "No"), ((185, 125), "(CAS)"), ((380, 110), "Jenis Bahan"),
                         ((80, 150), str(no)), ((115, 150), fix["cas"]), ((290, 150), name)]:
            page.insert_text(xy, text)
        rows, audit = build.extract_document(document)
    assert rows[0]["name_id"] == name
    assert rows[0]["name_en"] is None
    build.apply_ocr_fixes(rows, write_fixes(tmp_path / "fixes.tsv", [fix]), audit=audit)
    assert rows[0]["name_id"] == fix["other_name"]
    assert rows[0]["name_en"] == fix["corrected"]
    assert rows[0]["name_en_ocr_raw"] == fix["original"]
    assert "name_index" not in rows[0]


@pytest.mark.parametrize("mutation", ["original", "cas", "page", "duplicate", "status"])
def test_stale_or_invalid_table_fails_before_mutating_any_row(tmp_path, mutation):
    fixes = deepcopy(FIXES)
    if mutation == "duplicate":
        fixes.append(deepcopy(fixes[-1]))
    elif mutation == "page":
        fixes[-1][mutation] = "999"
    else:
        fixes[-1][mutation] = "unexpected"
    rows = original_rows()
    before = deepcopy(rows)
    with pytest.raises(ValueError):
        build.apply_ocr_fixes(rows, write_fixes(tmp_path / "fixes.tsv", fixes))
    assert rows == before


def test_corrected_but_still_unsafe_name_remains_excluded(tmp_path):
    # A reviewed change cannot override the shared name-exclusion rules.
    fixes = deepcopy(FIXES[:1])
    fixes[0]["corrected"] = "Corrected|Name"
    rows = original_rows()
    build.apply_ocr_fixes(rows, write_fixes(tmp_path / "fixes.tsv", fixes))
    assert rows[0]["name_id"] == "Corrected|Name"
    assert rows[0]["name_id_ocr_raw"] == FIXES[0]["original"]
    assert rows[0]["name_index"]["name_id"] == {
        "indexable": False, "reasons": ["ocr_noise"]}


def test_all_new_names_and_remaining_exclusions_follow_existing_loader_rules(tmp_path):
    names, cas_index = screening._load_kma1360()
    assert len(names) == 4238
    assert sum(len(records) for records in names.values()) == 4354
    assert sum(len(row.get("name_index", {})) for row in ROWS) == 184
    original = tmp_path / "original.json"
    original.write_text(json.dumps(original_rows()), encoding="utf-8")
    before_names, before_cas = screening._load_kma1360(original)
    assert len(before_names) == 3981
    assert sum(len(records) for records in before_names.values()) == 4095
    assert {cas: [(row["section"], row["no"]) for row in records]
            for cas, records in cas_index.items()} == {
        cas: [(row["section"], row["no"]) for row in records]
        for cas, records in before_cas.items()}
    assert len(cas_index) == 3591
    assert sum(len(records) for records in cas_index.values()) == 3905
    for row in ROWS:
        assert row.get("name_index", {}) == excluded_name_metadata(row)
        if row["section"] not in {"c.1", "c.2"}:
            assert all(row not in records for records in names.values())
            continue
        for cas in row["cas"]:
            assert row in cas_index[cas]
        for field in ("name_id", "name_en"):
            if not row.get(field):
                continue
            assert (row in names.get(normalize_name(row[field]), [])) is (
                not name_exclusion_reasons(row[field]))
    # Collision review includes original names AND all proposed corrections,
    # including proposals excluded on both sides of a collision.
    identities = [(row, row[field]) for row in ROWS for field in ("name_id", "name_en")
                  if row.get(field)]
    identities += [(BY_KEY[f["section"], int(f["no"]), int(f["page"])], f["corrected"])
                   for f in FIXES]
    for fix in FIXES:
        if fix["status"] != "applied":
            continue
        row = BY_KEY[fix["section"], int(fix["no"]), int(fix["page"])]
        assert not any((other["section"], other["no"]) != (row["section"], row["no"])
                       and normalize_name(value) == normalize_name(fix["corrected"])
                       and set(other["cas"]) != set(row["cas"])
                       for other, value in identities)


@pytest.mark.parametrize("no,name,cas", POSITIVE)
def test_corrected_names_and_matching_cas_use_kma_fallback(monkeypatch, no, name, cas):
    empty_ontology(monkeypatch)
    for query in (name, f"{name} (CAS {cas})"):
        assert no in {row["no"] for row in screening._match_kma1360(query)}
        result = screening.screen_merged(query)
        assert (result["decision_by"], result["result"], result["status"], result["severity"]) == (
            "kma1360", "PASS", "halal", "low")
        assert intake._material_autofill_allowed(query)
    assert screening._match_kma1360(f"{name} (CAS 1310-73-2)") == []
    assert screening.screen_merged(f"{name} (CAS 1310-73-2)")["decision_by"] != "kma1360"
    assert screening._match_kma1360(f"{name} derivative") == []


@pytest.mark.parametrize("fix", [f for f in FIXES if f["status"] == "applied"],
                         ids=lambda f: f"{f['section']}-{f['no']}-{f['field']}")
def test_original_ocr_strings_never_become_runtime_aliases(fix):
    assert name_exclusion_reasons(fix["original"])
    assert screening._match_kma1360(fix["original"]) == []
    assert screening.screen_merged(fix["original"])["decision_by"] != "kma1360"


@pytest.mark.parametrize("name", ["NaaP04", "Na3PO4", "2-Hepten-4-0ne", "CI77002",
    "2-Ethylbutyra).dehyde", "Hydroxyphenyllf'ropionate", "Lilia!", ".Pure Myrrh Gum"])
def test_old_ocr_counterexamples_still_cannot_autofill(name):
    assert screening._match_kma1360(name) == []
    assert screening.screen_merged(name)["decision_by"] != "kma1360"
    assert not intake._material_autofill_allowed(name)


@pytest.mark.parametrize("no,name,cas", POSITIVE)
@pytest.mark.parametrize("status,severity", [("halal", "low"), ("mushbooh", "medium"), ("haram", "high")])
def test_dictionary_statuses_still_precede_corrected_names(monkeypatch, no, name, cas, status, severity):
    item = install_ontology(monkeypatch, cas, status, severity)
    result = screening.screen_merged(name)
    assert (result["decision_by"], result["status"], result["matched_uid"]) == (
        "ontology", status, item.ingredient_uid)


@pytest.mark.parametrize("extra", [
    {"note": "contains lard"}, {"note": "contains ethanol"}, {"note": "glycerin carrier"},
    {"source": "synthetic"}, {"e_number": "E422"}, {"cert_no": "unverified"},
    {"docs": [SimpleNamespace()]}, {"source_known": False},
])
def test_context_and_risky_notes_keep_baseline_decisions(monkeypatch, extra):
    empty_ontology(monkeypatch)
    actual = screening.screen_merged("2-Hepten-4-One", **extra)
    monkeypatch.setattr(screening, "_KMA1360_NAMES", {})
    monkeypatch.setattr(screening, "_KMA1360_CAS", {})
    assert actual == screening.screen_merged("2-Hepten-4-One", **extra)
    assert actual["decision_by"] != "kma1360"


def test_existing_v1_decision_is_not_promoted_by_corrected_name(monkeypatch):
    empty_ontology(monkeypatch)
    monkeypatch.setattr(screening, "v1_rule", lambda *args, **kwargs: ("unknown", "medium"))
    result = screening.screen_merged("2-Hepten-4-One")
    assert result["decision_by"] == "v1_rule"
    assert result["result"] == "NEEDS_EVIDENCE"
