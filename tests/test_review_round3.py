"""3차 검수: 측정항목·성분 설명을 원료 확인대기로 승격하지 않는다."""
import base64

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import ai_local, intake, models, state_machine as sm
from app import document_extraction as extraction
import app.main as m


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(m, "verify_enqueue", lambda *a: False)
    monkeypatch.setattr(m, "_AUTOPARSE", False)
    monkeypatch.setattr(intake, "_ZIP_LOOKUP", False)
    monkeypatch.setattr(ai_local, "llm_json", lambda *a, **k: {
        "doc_type": "coa_msds", "confidence": .99, "fields": {},
        "verdict": "ok", "doc_type_ok": True, "field_issues": []})


@pytest.fixture
def memory_case():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        case = models.CaseApplication(org_id="round3-org", company_name="Sample",
                                      status="self_declaration_submitted", pathway="reguler")
        db.add_all([models.Org(org_id="round3-org", name="Sample"), case])
        db.commit()
        yield db, case
    engine.dispose()


def _reprocess(db, case, text):
    doc = models.DocumentAsset(case_id=case.case_id, filename="CoA.txt", doc_type="coa_msds",
                              content_b64=base64.b64encode(text.encode()).decode())
    db.add(doc)
    db.commit()
    m._reprocess_doc(db, doc, case, keep_doc_type=True)
    m._verify_doc(db, doc, case)
    db.commit()
    db.expire_all()
    return db.get(models.DocumentAsset, doc.document_id)


NORMAL = [
    ("No. | Parameter | Result | Unit\n1 | Ethanol | 0.02 | %", {"ethanol_pct": .02}),
    ("1. Porcine DNA PCR: Negative", {"pork_dna": 0.0}),
    ("Cetyl alcohol is a fatty alcohol derived from coconut oil.", {}),
    ("Ethanol Result: 0.02%", {"ethanol_pct": .02}),
]


@pytest.mark.parametrize("provider", ["openai", "ollama", "none"])
@pytest.mark.parametrize("offset", [0, 3100])
@pytest.mark.parametrize("source,measurements", NORMAL)
def test_normal_document_can_enter_committee(memory_case, monkeypatch, provider, offset, source, measurements):
    db, case = memory_case
    monkeypatch.setattr(ai_local, "provider", lambda: provider)
    text = "CERTIFICATE OF ANALYSIS\n" + "x" * offset + "\n" + source
    doc = _reprocess(db, case, text)
    for key, value in measurements.items():
        assert doc.fields["measurements"][key] == value
    verification = doc.fields["_verify"]
    if provider == "ollama" and offset:
        # 기존 로컬 입력 상한의 참고 경고는 유지하며 원료 차단과 구분한다.
        assert verification["verdict"] == "warn"
        assert {item["code"] for item in verification["advisory"]} == {"partial_analysis"}
    else:
        assert verification["verdict"] == "ok"
    assert verification["field_issues"] == []
    assert sm.pending_extractions(db, case.case_id) == []
    assert sm.material_safety_blockers(db, case) == []
    assert sm.can_transition(db, case, "committee_verification") == (True, [])


@pytest.mark.parametrize("source", [
    "1) Porcine DNA PCR: Negative",
    "1)Porcine DNA PCR: Negative",
    "No.\tTest\tResult\n1\tPorcine DNA PCR\tNot detected",
    "No.  Parameter  Result  Unit\n1  Ethanol  0.02  %",
    "| No. | Parameter | Specification | Result | Unit |\n| 1 | Ethanol | < 0.5 | 0.02 | % |",
    "Test | Result\nPorcine DNA PCR | ND",
    "Test | Result\nPorcine DNA PCR | 불검출",
    "Parameter | Result\nEthanol | Negative",
    "Parameter | Result\nEthanol | ND",
    "Parameter | Result\nEthanol | Not detected",
    "Parameter | Result\nEthanol | 불검출",
    "1) Porcine DNA PCR: 0.0%",
    "Stearyl alcohol is used in cosmetics.",
    "Benzyl alcohol is listed in the technical description.",
    "This product contains a fatty alcohol derived from coconut oil.",
])
def test_measurement_layouts_and_chemical_compounds_do_not_become_materials(monkeypatch, source):
    monkeypatch.setattr(ai_local, "provider", lambda: "none")
    result = intake.parse_typed_text("coa_msds", "CoA.txt", "CERTIFICATE OF ANALYSIS\n" + source)
    assert not result["fields"].get("_tail_screening", {}).get("risk_hits")


@pytest.mark.parametrize("provider", ["openai", "ollama", "none"])
@pytest.mark.parametrize("source", [
    "Ingredients: Water, Pork",
    "Composition: Water, gelatin (porcine)",
    "Ingredients: Ethanol",
    "Ingredients: Water | Ethanol | 96%",
    "Ingredients\nEthanol 96%",
    "Ethanol Result: 0.02%\nIngredients: Ethanol",
    "1. Porcine DNA PCR: Negative; Ingredients: Pork",
    "No. | Parameter | Result | Unit\n1 | Ethanol | 0.02 | % | Ingredients: Ethanol",
    "Cetyl alcohol is a fatty alcohol derived from coconut oil. Ingredients: Pork",
])
def test_explicit_materials_still_block_committee(memory_case, monkeypatch, provider, source):
    db, case = memory_case
    monkeypatch.setattr(ai_local, "provider", lambda: provider)
    _reprocess(db, case, "CERTIFICATE OF ANALYSIS\n" + source)
    assert sm.pending_extractions(db, case.case_id)
    assert any(b["code"] == "PENDING_CRITICAL_MATERIAL" for b in sm.material_safety_blockers(db, case))
    allowed, blockers = sm.can_transition(db, case, "committee_verification")
    assert not allowed
    assert any(b["code"] == "PENDING_CRITICAL_MATERIAL" for b in blockers)


@pytest.mark.parametrize("cap", [1, 2, 7])
@pytest.mark.parametrize("position", ["head", "middle", "tail"])
def test_all_gpt_ranges_promote_unlabelled_risks(monkeypatch, cap, position):
    monkeypatch.setattr(ai_local, "provider", lambda: "openai")
    budget = extraction.DocumentBudget()
    budget.used = budget.limit - cap - 1
    monkeypatch.setattr(intake, "current_budget", lambda **k: budget)
    seen = []

    def llm(system, user):
        seen.append(user)
        return {"doc_type": "coa_msds", "fields": {}}

    monkeypatch.setattr(ai_local, "llm_json", llm)
    filler = ("x" * 99 + "\n") * 2000
    sections = ["", filler, "", filler, ""]
    sections[{"head": 0, "middle": 2, "tail": 4}[position]] = "Pork\n"
    result = intake.parse_typed_text("coa_msds", "CoA.txt", "".join(sections))
    assert len(seen) == cap
    promoted = result["fields"].get("_tail_screening", {}).get("risk_hits", [])
    # R1: 읽기 범위와 호출 예산은 유지하지만 위험 후보 검사에서는 제외하지 않는다.
    assert promoted
    assert budget.gpt_read_spans


def test_classification_coverage_does_not_bypass_typed_reextraction_risks(memory_case, monkeypatch):
    db, case = memory_case
    monkeypatch.setattr(ai_local, "provider", lambda: "openai")
    calls = []

    def llm(system, user):
        calls.append(user)
        return {"doc_type": "coa_msds", "fields": {}, "verdict": "ok", "field_issues": []}

    monkeypatch.setattr(ai_local, "llm_json", llm)
    filler = "x" * 99 + "\n"
    _reprocess(db, case, filler * 3800 + "Pork\n" + filler * 200)
    assert len(calls) == 8
    assert any("Pork" in call for call in calls[:4])
    assert all("Pork" not in call for call in calls[4:])
    assert sm.pending_extractions(db, case.case_id)
    allowed, blockers = sm.can_transition(db, case, "committee_verification")
    assert not allowed
    assert any(b["code"] == "PENDING_CRITICAL_MATERIAL" for b in blockers)


def test_repeated_text_does_not_hide_an_unseen_occurrence(monkeypatch):
    monkeypatch.setattr(ai_local, "provider", lambda: "openai")
    filler = ("x" * 99 + "\n") * 2000
    text = "Pork\n" + filler + "Pork\n" + filler + "Pork\n"
    result = intake.parse_typed_text("coa_msds", "CoA.txt", text)
    assert result["fields"]["_tail_screening"]["risk_hits"]


def test_failed_gpt_response_keeps_rule_fallback(monkeypatch):
    monkeypatch.setattr(ai_local, "provider", lambda: "openai")
    monkeypatch.setattr(ai_local, "llm_json", lambda *a, **k: {"error": "TIMEOUT"})
    result = intake.parse_typed_text("coa_msds", "CoA.txt", "Pork")
    assert result["fields"]["_tail_screening"]["risk_hits"]


@pytest.mark.parametrize("source", [case[0] for case in NORMAL[:3]])
def test_discarded_office_tail_uses_same_measurement_exclusion(monkeypatch, source):
    monkeypatch.setattr(ai_local, "provider", lambda: "none")
    text = intake._bounded_office_text(["CERTIFICATE OF ANALYSIS", source], 23)
    result = intake.parse_typed_text("coa_msds", "CoA.docx", text)
    assert not result["fields"].get("_tail_screening", {}).get("risk_hits")


@pytest.mark.parametrize("source", [
    "Porcine DNA PCR: Negative / Positive",
    "Porcine DNA PCR: Negative\nIngredients: Pork",
    "Cetyl alcohol and Ethanol are listed separately.",
    "Benzyl alcohol; Ingredients: alcohol",
])
def test_exclusions_do_not_hide_other_risks(monkeypatch, source):
    monkeypatch.setattr(ai_local, "provider", lambda: "none")
    result = intake.parse_typed_text("coa_msds", "CoA.txt", source)
    assert result["fields"]["_tail_screening"]["risk_hits"]


@pytest.mark.parametrize("source", [
    "1. Porcine DNA PCR: Positive",
    "Porcine DNA PCR result: 0.02% detected",
])
def test_positive_assay_keeps_its_measurement_result(monkeypatch, source):
    monkeypatch.setattr(ai_local, "provider", lambda: "none")
    result = intake.parse_typed_text("coa_msds", "CoA.txt", source)
    # R2는 Positive도 시험 결과로 구분한다. 양성 측정값 자체는 그대로 보존한다.
    assert not result["fields"].get("_tail_screening", {}).get("risk_hits")
    assert result["fields"]["measurements"]["pork_dna"] == 1.0


@pytest.mark.parametrize("offset", [0, 3100])
@pytest.mark.parametrize("source,measurements", NORMAL)
def test_disabled_ai_without_model_response(memory_case, monkeypatch, offset, source, measurements):
    db, case = memory_case
    monkeypatch.setattr(ai_local, "provider", lambda: "none")
    monkeypatch.setattr(ai_local, "llm_json", lambda *a, **k: {"error": "LLM_UNAVAILABLE"})
    doc = _reprocess(db, case, "CERTIFICATE OF ANALYSIS\n" + "x" * offset + "\n" + source)
    for key, value in measurements.items():
        assert doc.fields["measurements"][key] == value
    assert doc.fields["_verify"]["verdict"] == "unknown"
    assert sm.pending_extractions(db, case.case_id) == []
    assert sm.can_transition(db, case, "committee_verification") == (True, [])


@pytest.mark.parametrize("source,expected_with_ingredient,same_line", [
    ("Porcine DNA (PCR): Negative", {"pork_dna_unclear": True}, True),
    ("Porcine DNA\nSample: Negative\nPositive control: Positive", {"pork_dna": 0.0}, False),
    ("Porcine DNA Test Report\nPorcine DNA: Negative", {"pork_dna_unclear": True}, False),
])
@pytest.mark.parametrize("ingredient", ["", "\nIngredients: Pork"])
def test_negative_assay_requires_same_line_context_for_exclusion(
        monkeypatch, source, expected_with_ingredient, same_line, ingredient):
    monkeypatch.setattr(ai_local, "provider", lambda: "none")
    text = "CERTIFICATE OF ANALYSIS\n" + "x" * 3100 + "\n" + source + ingredient
    result = intake.parse_typed_text("coa_msds", "CoA.txt", text)
    # 직전 DNA 행에 이어진 원료 행을 미확정으로 남기는 기존 측정 판독도 유지한다.
    assert result["fields"]["measurements"] == (expected_with_ingredient if ingredient else {"pork_dna": 0.0})
    # R2: 시험표가 아닌 여러 줄의 시험 맥락을 전파하지 않는다.
    assert bool(result["fields"].get("_tail_screening", {}).get("risk_hits")) is (bool(ingredient) or not same_line)
