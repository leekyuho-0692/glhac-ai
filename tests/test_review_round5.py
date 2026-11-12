"""5차 검수: 머리말 셀 전체 일치·문맥 종료, 저장 및 심의 HTTP 회귀."""
import base64

import pytest
from fastapi.testclient import TestClient

from app import ai_local, intake, models, state_machine as sm
import app.main as m


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(m, "verify_enqueue", lambda *a: False)
    monkeypatch.setattr(m, "_AUTOPARSE", False)
    monkeypatch.setattr(intake, "_ZIP_LOOKUP", False)


@pytest.fixture(scope="module")
def api():
    with TestClient(m.app) as client:
        login = client.post("/auth/login", json={"username": "admin", "password": "admin"})
        assert login.status_code == 200, login.text
        yield client, {"Authorization": "Bearer " + login.json()["token"]}


COA = ("No. | Parameter | Result | Unit\n"
       "1 | Ethanol | 0.02 | %\n2 | Porcine DNA PCR | Negative | -")
WATER_THEN_COA = "Ingredients | %\nWater | 100\n\nCERTIFICATE OF ANALYSIS\n" + COA
RISK_TABLE = "Ingredient | %\nEthanol | 70\nWater | 30"

BLOCKED = [
    "No test result is available.\nEthanol | 70\nWater | 30",
    "Please see the test method in the attachment.\nEthanol | 70",
    "Ingredients: Water, Pork",
    "Composition: Water, gelatin (porcine)",
    "Ingredient | %\nPork | 5",
    "Ingredient | %\nLard | 5",
    "Ingredient | %\nPorcine gelatin | 5",
    "Ingredient | %\nEthanol 70% | 5",
    RISK_TABLE,
    "Ingredients\nIngredient | %\nPork | 5",
    "Ingredients\nWater\nPork",
    "Pork | 5 | %",
    "원료명 | 함량\n돈지 | 5",
    "Ingredients | %\nEthyl alcohol | 70",
    "Ingredients | %\nAlcohol denat. | 70",
    "Composition/information on ingredients\nEthanol 64-17-5 70%",
    "Ingredient | Test | Result\nEthanol | Content | 70%",
    "Ingredients\nCetyl alcohol\nEthanol Result: 70%",
    COA + "\n\n" + RISK_TABLE,
    RISK_TABLE + "\n\nCERTIFICATE OF ANALYSIS\n" + COA,
]
NORMAL = [
    (COA, {"ethanol_pct": .02, "pork_dna": 0.0}),
    ("1. Porcine DNA PCR: Negative", {"pork_dna": 0.0}),
    ("Cetyl alcohol is a fatty alcohol derived from coconut oil.", {}),
    ("Ethanol Result: 0.02%", {"ethanol_pct": .02}),
    (WATER_THEN_COA, {"ethanol_pct": .02, "pork_dna": 0.0}),
]


@pytest.mark.parametrize("provider", ["openai", "ollama", "none"])
@pytest.mark.parametrize("doc_type", ["material_list", "coa_msds"])
@pytest.mark.parametrize("offset", [0, 3100])
@pytest.mark.parametrize("source,blocked,measurements", [
    *[(text, True, {}) for text in BLOCKED],
    *[(text, False, measurements) for text, measurements in NORMAL],
])
def test_saved_context_controls_committee_transition(
        api, monkeypatch, provider, doc_type, offset, source, blocked, measurements):
    calls = []
    monkeypatch.setattr(ai_local, "provider", lambda: provider)

    def llm(system, user):
        if provider == "none":
            return {"error": "LLM_UNAVAILABLE"}
        calls.append(user)
        return {"doc_type": doc_type, "confidence": .99, "fields": {}, "names": [],
                "verdict": "ok", "doc_type_ok": True, "field_issues": []}

    monkeypatch.setattr(ai_local, "llm_json", llm)
    client, headers = api
    created = client.post("/cases", headers=headers, json={"company_name": "Round 5"})
    assert created.status_code == 200, created.text
    cid = created.json()["case_id"]
    text = ("x" * offset + "\n" if offset else "") + source
    with m.SessionLocal() as db:
        case = db.get(models.CaseApplication, cid)
        case.status, case.pathway = "self_declaration_submitted", "reguler"
        doc = models.DocumentAsset(case_id=cid, filename=doc_type + ".txt", doc_type=doc_type,
                                  content_b64=base64.b64encode(text.encode()).decode())
        db.add(doc)
        db.commit()
        did = doc.document_id
        m._reprocess_doc(db, doc, case, keep_doc_type=True)
        m._verify_doc(db, doc, case)
        db.commit()

    with m.SessionLocal() as db:
        doc = db.get(models.DocumentAsset, did)
        case = db.get(models.CaseApplication, cid)
        assert doc.text_full == text
        assert doc.doc_type == doc_type
        assert doc.fields.get("_verify")
        assert bool(calls) is (provider != "none")
        assert bool(sm.pending_extractions(db, cid)) is blocked
        assert any(b["code"] == "PENDING_CRITICAL_MATERIAL"
                   for b in sm.material_safety_blockers(db, case)) is blocked
        if doc_type == "coa_msds":
            for key, value in measurements.items():
                assert doc.fields["measurements"][key] == value
        if not blocked and provider == "ollama" and offset:
            assert doc.fields["_analysis"]["partial"]
            assert "partial_analysis" in {x["code"] for x in doc.fields["_verify"]["advisory"]}

    response = client.post(f"/cases/{cid}/transition", headers=headers,
                           json={"to_state": "committee_verification"})
    assert response.status_code == (409 if blocked else 200), response.text
    if blocked:
        assert any(b["code"] == "PENDING_CRITICAL_MATERIAL"
                   for b in response.json()["detail"]["blockers"])
    with m.SessionLocal() as db:
        assert db.get(models.CaseApplication, cid).status == (
            "self_declaration_submitted" if blocked else "committee_verification")


@pytest.mark.parametrize("header", [
    "No test result is available.", "Please see the test method in the attachment.",
    "Test result | Notes", "No test | Result", "Test | no result", "Test; Result",
    "Test Result", "Test Item", "Result | Result", "| Test |", "Test | |",
])
def test_prose_and_partial_cells_do_not_start_test_tables(header):
    text = header + "\nEthanol | 70\nWater | 30"
    assert "Ethanol" in intake._material_scan_text(text)


@pytest.mark.parametrize("delimiter", [" | ", "\t", "  "])
@pytest.mark.parametrize("header", [
    "Parameter", "Test", "Test item", "Item", "Analysis", "Specification", "Spec",
    "Method", "Limit", "Unit", "시험항목", "검사항목", "기준", "단위", " tEsT iTeM ", "시험 항목",
])
def test_whole_header_cells_exempt_only_contiguous_table_rows(delimiter, header):
    text = delimiter.join([header, "Result"]) + "\n" + delimiter.join(["Ethanol", "0.02%"])
    text += "\n" + delimiter.join(["Porcine DNA PCR", "Negative"])
    scanned = intake._material_scan_text(text)
    assert "Ethanol" not in scanned
    assert "Porcine" not in scanned
    assert len(scanned) == len(text)
    for boundary in ("\n", "End of table", "No test result is available."):
        separated = text + "\n" + boundary + "\nEthanol | 70"
        assert "Ethanol" in intake._material_scan_text(separated)


@pytest.mark.parametrize("boundary", [
    "", "CERTIFICATE OF ANALYSIS", "Certificate of Analysis", "COA", "TEST REPORT",
    "시험성적서", "분석성적서", "The following section reports measurements.",
])
@pytest.mark.parametrize("materials", ["Ingredients | %\nWater | 100", "Ingredients\n- Water", "Ingredients\nWater"])
def test_material_context_ends_before_a_new_section(materials, boundary):
    text = materials + "\n" + boundary + "\nEthanol Result: 0.02%"
    assert "Ethanol" not in intake._material_scan_text(text)


def test_new_test_table_ends_material_context_without_a_blank_line():
    text = "Ingredients | %\nWater | 100\n" + COA
    scanned = intake._material_scan_text(text)
    assert "Ethanol" not in scanned
    assert "Porcine" not in scanned


@pytest.mark.parametrize("material", [
    "Water", "- Water", "1. Water", "Water | 100", "Alcohol denat.",
    "- Water.", "1. Water.", "Cetyl alcohol", "fatty alcohol",
])
def test_contiguous_material_rows_keep_precedence_over_same_line_results(material):
    text = "Ingredients\n" + material + "\nEthanol Result: 70%"
    assert "Ethanol" in intake._material_scan_text(text)


def test_test_header_does_not_exempt_its_own_material_cell():
    assert "Ethanol" in intake._material_scan_text("Test | Result | Ethanol | 70")
