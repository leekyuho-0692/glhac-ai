#!/usr/bin/env python3
"""Build the checked-in KMA 1360/2021 annex from its searchable, 182-page PDF.

Usage: .venv/bin/python scripts/build_kma1360.py /path/KMA_1360_2021.pdf
The PDF is read only. PyMuPDF is already an application dependency. Table columns
are read by position: CAS continuations must never become ingredient names.
Original numbering (including gaps) is preserved; page is the 1-based PDF page.
cas is a list because one printed row can contain several registry numbers.
Invalid CAS values are omitted, never repaired by guessing a digit. --audit
optionally writes their original text and locations for review.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.kma1360 import excluded_name_metadata, name_exclusion_reasons


DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "app/data/kma1360.json"
CAS_FORMAT = re.compile(r"[1-9][0-9]{1,6}-[0-9]{2}-[0-9]")
CAS_IN_CELL = re.compile(r"(?<![\w.~:'\"-])([1-9](?:[ \t]*[0-9]){1,6})\s*-\s*([0-9]{2})\s*-\s*([0-9])(?![\w~:'\"-])")
SECTIONS = ("a.1", "a.2", "a.3", "a.4", "b", "c.1", "c.2")
# Visually checked against page 41: the searchable layer reads printed 713 as 7t3.
NUMBER_REPAIRS = {(41, "7t3"): 713}
# Page 98 has one vertically merged name cell spanning printed Nos. 2039–2040.
SHARED_NAMES = {(98, "c.2", 2039): "Methyl 3-Hexenoate"}


def valid_cas(value):
    if not CAS_FORMAT.fullmatch(value):
        return False
    digits = value.replace("-", "")
    return sum(i * int(d) for i, d in enumerate(reversed(digits[:-1]), 1)) % 10 == int(digits[-1])


def cell_lines(words):
    """Group OCR words into physical lines without merging neighbouring columns."""
    lines = []
    for word in sorted(words, key=lambda w: (w[1], w[0])):
        if not lines or abs(word[1] - lines[-1][0][1]) > 3:
            lines.append([word])
        else:
            lines[-1].append(word)
    return [" ".join(w[4] for w in sorted(line, key=lambda w: w[0])).strip(" ·")
            for line in lines]


def join_name(lines):
    # A chemical name broken immediately after a hyphen continues without a space.
    return re.sub(r"-\n\s*", "-", "\n".join(lines)).replace("\n", " ").strip()


_ANNOTATION = re.compile(
    r"[%‰]|\b(?:approx(?:imately)?\.?|ca\.|circa|about|percent(?:age)?|purity|"
    r"concentration|content|ratio|solution|aqueous|diluted|balance|remainder|"
    r"contains?|containing|note|see|refer|grade|technical|anhydrous\s+basis|"
    r"dry\s+basis|active\s+(?:matter|substance)|as\s+supplied|minimum|maximum|"
    r"min\.|max\.)|\b[wv]\s*/\s*[wv]\b|\d\s*[:/]\s*\d|"
    r"\d\s*(?:ppm|ppb|mg|kg|ml|g/l|g|wt|vol)\b", re.I)


def translation_exclusion_reasons(translated):
    """English substance names only; never turn a quantity/note into an alias.

    Locants and named dye identifiers are nomenclature, not concentrations.
    All other bare numbers are ambiguous and stay with the original name.
    """
    reasons = []
    compact = re.sub(r"\s", "", translated)
    letters = len(re.findall(r"[A-Za-z]", translated))
    if not re.search(r"[A-Za-z]{3}", translated) or letters / max(len(compact), 1) < 0.65:
        reasons.append("not_english_substance_name")
    if _ANNOTATION.search(translated):
        reasons.append("concentration_ratio_or_note")
    # A digit belongs to a chemical locant only with its explicit separator.
    # Standard dye numbers (e.g. Basic Yellow 57) are also part of the name.
    numeric_context = re.sub(r"(?<!\w)\d+(?:\s*,\s*\d+)*\s*-", "", translated)
    numeric_context = re.sub(r"-\d+(?![\d.])", "", numeric_context)
    numeric_context = re.sub(
        r"\b(?:Red|Yellow|Blue|Orange|Green|Violet|Black|Brown)\s*(?:No\.?\s*)?\d+\b",
        "", numeric_context, flags=re.I)
    if re.search(r"\d", numeric_context):
        reasons.append("unqualified_number")
    if (re.fullmatch(r"(?:[A-Z][a-z]?[0-9]*|\([^)]*\)[0-9]*|~)+", translated)
            or re.search(r"\b(?:dan|atau|termasuk|garamnya|lain|minyak|ketan|singkong|"
                         r"isomer|isomers|mixture|mixed|resin|dimer|inci)\b", translated, re.I)
            or re.fullmatch(r"free acid|sodium salt|endo-?|\S*(?:alpha|beta)\S*", translated, re.I)):
        reasons.append("formula_abbreviation_or_qualification")
    reasons.extend(name_exclusion_reasons(translated))
    return list(dict.fromkeys(reasons))


def split_translation(name, *, audit=None, location=None):
    """Only a complete, terminal parenthesis can be a translated name/synonym.

    Poly(Propylene Glycol) Macromolecule, Tin(II) Oxide and (R)-... are
    chemical notation, not independent ingredient aliases. Keep them intact.
    Unbalanced parentheses from the source are also kept intact.
    """
    depth, start, groups = 0, None, []
    for i, char in enumerate(name):
        if char == "(":
            if depth == 0:
                start = i
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return name, None
            if depth == 0:
                groups.append((start, i))
    if depth or not groups:
        return name, None
    start, end = groups[-1]
    if not start or not name[start - 1].isspace() or end != len(name) - 1:
        return name, None
    translated = name[start + 1:end].strip()
    # Remove a duplicated wrapper, not the opening parenthesis of (S)-... .
    if re.fullmatch(r"\([^()]+\)", translated):
        translated = translated[1:-1].strip()
    reasons = translation_exclusion_reasons(translated)
    if audit is not None:
        audit["translation_candidates"].append({**(location or {}), "name": name,
            "candidate": translated, "accepted": not reasons, "reasons": reasons})
    if reasons:
        return name, None
    return name[:start].strip(), translated


def parse_cas(lines, location, audit):
    valid = []
    for raw in lines:
        value = unicodedata.normalize("NFKC", raw).strip(" .·,;")
        for match in CAS_IN_CELL.finditer(value):
            part = re.sub(r"\s+", "", "-".join(match.groups()))
            if not valid_cas(part):
                audit["cas_checksum_failures"].append({**location, "value": part})
            else:
                valid.append(part)
        remainder = re.sub(r"\([1-4]\)", "", CAS_IN_CELL.sub("", value)).strip(" .·,;/")
        if re.search(r"\d", remainder):
            audit["cas_format_failures"].append({**location, "value": remainder})
    return sorted(set(valid))


def section_before(words, header):
    text = " ".join(cell_lines([w for w in words if w[1] < header[1]]))
    headings = [
        ("a.1", r"Bahan\s+Berasal\s+dari\s+Tumbuhan"),
        ("a.2", r"Bahan\s+Berasal\s+dari\s+Hewan"),
        ("a.3", r"Bahan\s+Berasal\s+dari\s+Proses\s+Ferm"),
        ("a.4", r"Bahan\s+Berasal\s+dari\s+Air"),
        ("b", r"Bahan\s+Selain\s+Bahan"),
        ("c.1", r"Bahan\s+Kimia\s*Hasil\s+Penambangan"),
        ("c.2", r"Bahan\s+Kimia\s*(?:Hasil|Basil)\s+Sintesis"),
    ]
    # Some OCR layers split Bah an. This does not change the stored item names.
    text = re.sub(r"[·:.]", "", re.sub(r"\bBah\s+an\b", "Bahan", text))
    found = [(m.start(), section) for section, pattern in headings
             for m in re.finditer(pattern, text, re.I)]
    if not found:
        raise ValueError(f"Unrecognised table heading: {text[-250:]}")
    return max(found)[1]


def extract_document(document):
    rows = []
    audit = {"cas_format_failures": [], "cas_checksum_failures": [],
             "number_repairs": [], "page_continuations": [], "unbalanced_names": [],
             "excluded_names": [], "text_layer_pages": [],
             "translation_candidates": [], "parenthetical_annotations": []}
    for page_no, page in enumerate(document, 1):
        # Read the existing text layer; never OCR rendered page images or repair
        # source words speculatively. The embedded layer can itself contain OCR errors.
        words = page.get_text("words")
        if not words:
            raise ValueError(f"Missing searchable text layer on page {page_no}")
        audit["text_layer_pages"].append(page_no)
        headers = sorted((w for w in words if w[4] == "No" and w[0] < 140), key=lambda w: w[1])
        # Section headings delimit tables, even when several tables share a page.
        ends = [w[1] for w in words if w[0] < 130 and re.fullmatch(r"[abc1-4]\.", w[4])
                and any(abs(v[1] - w[1]) < 4 and v[0] > w[0] and v[4] in {"Bahan", "Bah"}
                        for v in words)]
        ends += [w[1] for w in words if w[4].startswith("MENTER")]
        for header in headers:
            section = section_before(words, header)
            bottom = min((y for y in ends if y > header[3]), default=page.rect.height - 60)
            name_left = header[0] + ({"c.1": 180, "c.2": 200}.get(section, 23))
            top = header[3]
            if section.startswith("c."):
                top = max([top + 12] + [w[3] for w in words if "CAS" in w[4]
                                  and header[1] <= w[1] < header[3] + 25])
            body = [w for w in words if top < w[1] < bottom - 4]
            anchors = []
            for w in body:
                if w[0] >= header[0] + 29 or w[2] >= header[0] + 55:
                    continue
                token = w[4].strip("·.\"'- ")
                repaired = NUMBER_REPAIRS.get((page_no, token))
                if repaired:
                    audit["number_repairs"].append({"page": page_no, "original": token, "no": repaired})
                if token.isdigit() or repaired:
                    anchors.append((w, repaired or int(token)))
            anchors.sort(key=lambda pair: pair[0][1])

            def append_cells(row, selected):
                row["_names"].extend(cell_lines([w for w in selected if w[0] >= name_left]))
                if section.startswith("c."):
                    row["_cas"].extend(cell_lines([w for w in selected
                                                  if header[0] + 29 <= w[0] < name_left]))

            first_y = anchors[0][0][1] - 10 if anchors else bottom
            continuation = [w for w in body if w[1] < first_y and w[0] >= header[0] + 29]
            if continuation:
                if not rows or rows[-1]["section"] != section:
                    raise ValueError(f"Orphan table continuation on page {page_no}")
                append_cells(rows[-1], continuation)
                audit["page_continuations"].append({"page": page_no, "section": section, "no": rows[-1]["no"]})
            for i, (anchor, no) in enumerate(anchors):
                row = {"no": no, "section": section, "page": page_no, "_names": [], "_cas": []}
                end = anchors[i + 1][0][1] - 10 if i + 1 < len(anchors) else bottom
                append_cells(row, [w for w in body if anchor[1] - 10 <= w[1] < end])
                rows.append(row)

    entries = []
    for row in rows:
        location = {key: row[key] for key in ("section", "no", "page")}
        name = join_name(row["_names"]) or SHARED_NAMES.get((row["page"], row["section"], row["no"]), "")
        if not name:
            raise ValueError(f"Empty ingredient: {location}")
        if name.count("(") != name.count(")"):
            audit["unbalanced_names"].append({**location, "name": name})
        for match in re.finditer(r"\(([^()]*)\)", name):
            if _ANNOTATION.search(match.group(1)):
                audit["parenthetical_annotations"].append(
                    {**location, "name": name, "annotation": match.group(1)})
        name_id, name_en = split_translation(name, audit=audit, location=location)
        cas_lines = re.sub(r"-\n\s*", "-", "\n".join(row["_cas"])).splitlines()
        entry = {"no": row["no"], "section": row["section"], "name_id": name_id,
                 "name_en": name_en, "cas": parse_cas(cas_lines, location, audit), "page": row["page"]}
        excluded = excluded_name_metadata(entry)
        if excluded:
            entry["name_index"] = excluded
            audit["excluded_names"].extend({**location, "field": field, "name": entry[field], **metadata}
                                           for field, metadata in excluded.items())
        entries.append(entry)
    entries.sort(key=lambda row: (SECTIONS.index(row["section"]), row["no"], row["page"]))
    keys = [(row["section"], row["no"]) for row in entries]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate section/row numbers; inspect the source before publishing")
    return entries, audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--audit", type=Path, help="Optional extraction diagnostics JSON")
    args = parser.parse_args()
    if any(path and path.resolve() == args.pdf.resolve() for path in (args.output, args.audit)):
        parser.error("The source PDF must remain read-only")
    with fitz.open(args.pdf) as document:
        if len(document) != 182:
            parser.error("Expected the 182-page KMA 1360/2021 source PDF")
        entries, audit = extract_document(document)
    counts = Counter(row["section"] for row in entries)
    if set(counts) != set(SECTIONS) or counts["c.2"] < 4000:
        raise ValueError(f"Incomplete annex: {dict(counts)}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit["source_sha256"] = hashlib.sha256(args.pdf.read_bytes()).hexdigest()
    if args.audit:
        args.audit.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Entries: {len(entries)}")
    for section in SECTIONS:
        print(f"  {section}: {counts[section]}")
    print(f"CAS checksum failures (excluded): {len(audit['cas_checksum_failures'])}")
    print(f"CAS format failures (excluded): {len(audit['cas_format_failures'])}")
    print(f"Page continuations: {len(audit['page_continuations'])}")
    print(f"Unbalanced names (preserved): {len(audit['unbalanced_names'])}")
    print(f"Names excluded from index (preserved): {len(audit['excluded_names'])}")
    print(f"Parenthetical aliases accepted: {sum(v['accepted'] for v in audit['translation_candidates'])}")
    print(f"Parenthetical aliases rejected: {sum(not v['accepted'] for v in audit['translation_candidates'])}")
    print(f"Concentration/ratio/note parentheses: {len(audit['parenthetical_annotations'])}")
    print(f"Text-layer pages (no image OCR): {len(audit['text_layer_pages'])}")
    print(f"Source SHA256: {audit['source_sha256']}")


if __name__ == "__main__":
    main()
