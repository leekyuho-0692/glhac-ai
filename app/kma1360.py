"""Shared, conservative validation of KMA text-layer names.

These are exclusion heuristics, not spelling repairs. Keep the extracted text
for review; a checksum-valid CAS remains independently usable.
"""
import re
import unicodedata


def normalize_name(value):
    """Shared ontology/KMA identity key; preserve words, order and digits."""
    text = "".join(char for char in unicodedata.normalize("NFKD", value or "")
                   if not unicodedata.combining(char)).lower()
    text = re.sub(r"[()\[\]{}\-‐‑‒–—−]", " ", text)
    return " ".join(text.split())


def name_exclusion_reasons(value):
    text = unicodedata.normalize("NFKC", value or "").strip()
    reasons = []
    if not text:
        return ["empty_name"]
    stack = []
    pairs = {")": "(", "]": "[", "}": "{"}
    for char in text:
        if char in pairs.values():
            stack.append(char)
        elif char in pairs:
            if not stack or stack.pop() != pairs[char]:
                reasons.append("unbalanced_brackets")
                break
    if stack and "unbalanced_brackets" not in reasons:
        reasons.append("unbalanced_brackets")
    # A lower-case continuation or dot after ')' inside a word is suspect.
    # Balanced systematic notation such as (Methylthio)Methyl is not this pattern.
    if re.search(r"[A-Za-z]\)(?:[a-z]|\.[A-Za-z])", text):
        reasons.append("ocr_intraword_closer")
    if re.search(r"lf['’]|[A-Za-z]['’][A-Za-z]|[|~<>`\\\ufffd]", text):
        reasons.append("ocr_noise")
    if "!" in text:
        reasons.append("ocr_exclamation")
    if text.startswith("."):
        reasons.append("ocr_leading_dot")
    if re.search(r"[A-Za-z]\.[A-Za-z]", text):
        reasons.append("ocr_intraword_dot")
    # A zero in -one/-ol is not a chemical locant; retain the source spelling.
    if re.search(r"[-‐‑‒–—−]\s*0(?:ne|l)\b", text, re.I):
        reasons.append("ocr_suffix_zero")
    # Do not confuse di9ne/Ethy1hexyl with locants (1,2-, -2-) or C12.
    # Carbon-chain ranges such as C12-15 also have no letter after the digits.
    if re.search(r"[A-Za-z][0-9]+[A-Za-z]", text):
        reasons.append("ocr_intraword_digit")
    # Bracket nesting and bracket/hyphen combinations are chemical notation.
    if re.search(r"[^\w\s()\[\]{}]{2,}", text):
        reasons.append("repeated_special_characters")
    # Deliberately also catch OCR-like symbols (Naa, digit zero for O).
    # Formula/code-shaped tokens cannot stand alone as verified chemical names.
    if (re.search(r"\d", text) and not re.fullmatch(r"C[1-9][0-9]*", text)
            and re.fullmatch(r"(?:[A-Z][a-z]{0,2}[0-9]*)+", text)):
        reasons.append("formula_like")
    return reasons


def excluded_name_metadata(row):
    """Only excluded aliases need metadata; names and CAS text stay untouched."""
    return {field: {"indexable": False, "reasons": reasons}
            for field in ("name_id", "name_en") if row.get(field)
            for reasons in [name_exclusion_reasons(row[field])] if reasons}
