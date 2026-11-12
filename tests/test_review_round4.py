"""4차 검수: 전체 원문 위험 검사·성분표 우선, 저장 후 실제 HTTP 전이까지."""
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


def _model(monkeypatch, provider, doc_type):
    calls = []
    monkeypatch.setattr(ai_local, "provider", lambda: provider)

    def llm(system, user):
        if provider == "none":
            return {"error": "LLM_UNAVAILABLE"}
        calls.append(user)
        return {"doc_type": doc_type, "confidence": .99, "fields": {}, "names": [],
                "verdict": "ok", "doc_type_ok": True, "field_issues": []}

    monkeypatch.setattr(ai_local, "llm_json", llm)
    return calls


def _reprocess_and_transition(api, text, doc_type, blocked, measurements=None):
    client, headers = api
    response = client.post("/cases", headers=headers, json={"company_name": "Round 4"})
    assert response.status_code == 200, response.text
    cid = response.json()["case_id"]
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

    # 새 세션으로 직렬화·저장 결과를 확인한다. 검증 후 위험 후보가 유실되면 실패한다.
    with m.SessionLocal() as db:
        doc = db.get(models.DocumentAsset, did)
        case = db.get(models.CaseApplication, cid)
        assert doc.text_full == text
        assert doc.doc_type == doc_type
        assert doc.fields.get("_verify")
        assert bool(sm.pending_extractions(db, cid)) is blocked
        blockers = sm.material_safety_blockers(db, case)
        assert any(b["code"] == "PENDING_CRITICAL_MATERIAL" for b in blockers) is blocked
        for key, value in (measurements or {}).items():
            assert doc.fields["measurements"][key] == value
        fields = doc.fields

    response = client.post(f"/cases/{cid}/transition", headers=headers,
                           json={"to_state": "committee_verification"})
    assert response.status_code == (409 if blocked else 200), response.text
    if blocked:
        assert any(b["code"] == "PENDING_CRITICAL_MATERIAL"
                   for b in response.json()["detail"]["blockers"])
    with m.SessionLocal() as db:
        assert db.get(models.CaseApplication, cid).status == (
            "self_declaration_submitted" if blocked else "committee_verification")
    return fields


BLOCKED = [
    "Ingredient | %\nPork | 5\nWater | 95",
    "Ingredient | %\nLard | 5\nWater | 95",
    "Ingredient | %\nPorcine gelatin | 5\nWater | 95",
    "Ingredient | %\nEthanol 70% | 5\nWater | 95",
    "Ingredient | %\nEthanol | 70\nWater | 30",
    "Ingredients\nIngredient | %\nPork | 5\nWater | 95",
    "Ethanol | 70",
    "Ingredients\nWater\nPork",
    "Ingredients: Water, Pork",
    "Composition: Water, gelatin (porcine)",
]


@pytest.mark.parametrize("provider", ["openai", "ollama", "none"])
@pytest.mark.parametrize("offset", [0, 3100])
@pytest.mark.parametrize("source", BLOCKED)
def test_ingredients_block_after_empty_extraction_and_verification(api, monkeypatch, provider, offset, source):
    _model(monkeypatch, provider, "material_list")
    _reprocess_and_transition(api, ("x" * offset + "\n" if offset else "") + source,
                             "material_list", blocked=True)


NORMAL = [
    ("No. | Parameter | Result | Unit\n1 | Ethanol | 0.02 | %", {"ethanol_pct": .02}),
    ("1. Porcine DNA PCR: Negative", {"pork_dna": 0.0}),
    ("Cetyl alcohol is a fatty alcohol derived from coconut oil.", {}),
    ("Ethanol Result: 0.02%", {"ethanol_pct": .02}),
]


@pytest.mark.parametrize("provider", ["openai", "ollama", "none"])
@pytest.mark.parametrize("offset", [0, 3100])
@pytest.mark.parametrize("source,measurements", NORMAL)
def test_measurements_and_fatty_alcohol_enter_committee(api, monkeypatch, provider, offset, source, measurements):
    _model(monkeypatch, provider, "coa_msds")
    text = "CERTIFICATE OF ANALYSIS\n" + "x" * offset + "\n" + source
    fields = _reprocess_and_transition(api, text, "coa_msds", blocked=False, measurements=measurements)
    if provider == "ollama" and offset:
        assert fields["_analysis"]["partial"]
        assert "partial_analysis" in {item["code"] for item in fields["_verify"]["advisory"]}


@pytest.mark.parametrize("provider", ["openai", "ollama", "none"])
def test_middle_pork_blocks_outside_all_model_calls(api, monkeypatch, provider):
    calls = _model(monkeypatch, provider, "material_list")
    filler = ("x" * 99 + "\n") * 2000
    text = "Ingredients\nWater\n" + filler + "Pork\n" + filler
    assert len(text) == 400023
    fields = _reprocess_and_transition(api, text, "material_list", blocked=True)
    assert all("Pork" not in call for call in calls)
    if provider == "openai":
        assert len(calls) == fields["_ai_budget"]["used"] == 8


@pytest.mark.parametrize("header", [
    "Ingredient", "Ingredients", "Composition", "Formula", "INCI", "원료", "성분", "전성분", "배합",
])
@pytest.mark.parametrize("layout", [
    "{header} | Test | Result\nEthanol | Content | 70%",
    "{header}\nWater\nEthanol Result: 70%",
    "Parameter | Result\n{header} | Ethanol | 70%",
])
def test_material_headers_take_precedence_over_test_context(monkeypatch, header, layout):
    _model(monkeypatch, "none", "material_list")
    result = intake.parse_typed_text("material_list", "ingredients.txt", layout.format(header=header))
    assert result["fields"]["_tail_screening"]["risk_hits"]


@pytest.mark.parametrize("header", [
    "Parameter", "Test", "Test item", "Item", "Analysis", "Specification", "Spec", "Method", "Limit",
    "시험항목", "검사항목", "기준",
])
def test_two_test_header_terms_define_measurement_rows(monkeypatch, header):
    _model(monkeypatch, "none", "coa_msds")
    text = header + " | Result\nEthanol | 0.02%"
    result = intake.parse_typed_text("coa_msds", "CoA.txt", text)
    assert not result["fields"].get("_tail_screening", {}).get("risk_hits")


@pytest.mark.parametrize("context", ["Result", "Test", "PCR", "Assay", "Content", "Limit"])
@pytest.mark.parametrize("value", ["0.02%", "Negative", "Not detected", "ND", "불검출", "Positive"])
def test_same_line_test_context_and_result_exempt_the_analyte(monkeypatch, context, value):
    _model(monkeypatch, "none", "coa_msds")
    result = intake.parse_typed_text("coa_msds", "CoA.txt", f"1. Porcine DNA {context}: {value}")
    assert not result["fields"].get("_tail_screening", {}).get("risk_hits")


@pytest.mark.parametrize("source", [
    "Parameter\nEthanol | 70", "Test item\nEthanol | 70", "Result | Result\nEthanol | 70",
    "Parameter | Result\n\nEthanol | 70", "Parameter | Result\nEnd of table\nEthanol | 70",
    "Ethanol 70%", "Ethanol\t70", "Ethanol  70", "Porcine DNA: Negative",
    "Porcine DNA\nSample: Negative\nPositive control: Positive",
])
def test_units_columns_or_other_lines_alone_do_not_exempt_materials(monkeypatch, source):
    _model(monkeypatch, "none", "coa_msds")
    result = intake.parse_typed_text("coa_msds", "CoA.txt", source)
    assert result["fields"]["_tail_screening"]["risk_hits"]


@pytest.mark.parametrize("source", [
    "Ingredients\nEthanol", "Ingredients\nEthyl alcohol", "Ingredients\nAlcohol denat.",
    "Ingredients | Result\nEthyl alcohol | 70%", "INCI | Test | Result\nAlcohol denat. | Content | 70%",
])
def test_ethanol_ingredients_always_remain_candidates(monkeypatch, source):
    _model(monkeypatch, "none", "material_list")
    result = intake.parse_typed_text("material_list", "ingredients.txt", source)
    assert result["fields"]["_tail_screening"]["risk_hits"]


@pytest.mark.parametrize("provider", ["openai", "ollama", "none"])
@pytest.mark.parametrize("name", ["Cetyl", "Stearyl", "Cetearyl", "Benzyl", "fatty"])
def test_non_ethanol_alcohol_names_remain_exempt(monkeypatch, provider, name):
    _model(monkeypatch, provider, "material_list")
    result = intake.parse_typed_text("material_list", "ingredients.txt", "Ingredients\n" + name + " alcohol")
    assert not result["fields"].get("_tail_screening", {}).get("risk_hits")
