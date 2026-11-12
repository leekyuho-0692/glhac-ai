"""6차 검수: 성분 문맥 경계 및 측정 제외 기록의 저장·표시·심의 영향."""
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


BLOCKED = [
    "Ingredient | Test | Result\nWater | |\nEthanol | Content | 70%",
    "Ingredient | Result\nWater |\nEthanol | 70%",
    "Ingredients\nWater\nD&C Red No. 6\nEthanol Result: 70%",
    "Ingredients\nWater\n\nPork",
    "Ingredient  Content\nPork  5%",
]
TITLES = [prefix + title for prefix in ("", "2. ", "2) ", "II. ", "# ")
          for title in ("CERTIFICATE OF ANALYSIS", "ANALYTICAL RESULTS", "TEST REPORT",
                        "시험성적서", "분석결과")]
NORMAL = ["Ingredients\nWater\n" + title + "\nEthanol Result: 0.02%" for title in TITLES] + [
    "Ingredients | %\nWater | 100\nTest | Result\nEthanol | 0.02%",
    "Test\tResult\nEthanol\t0.02%",
    "Ingredients\nWater\nII. TEST REPORT\n1. Porcine DNA PCR: Negative",
]


@pytest.mark.parametrize("provider", ["openai", "ollama", "none"])
@pytest.mark.parametrize("doc_type", ["material_list", "coa_msds"])
@pytest.mark.parametrize("offset", [0, 3100])
@pytest.mark.parametrize("source,blocked", [(text, True) for text in BLOCKED] +
                         [(text, False) for text in NORMAL])
def test_saved_context_and_measurement_reference(
        api, monkeypatch, provider, doc_type, offset, source, blocked):
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
    created = client.post("/cases", headers=headers, json={"company_name": "Round 6"})
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
        excluded = doc.fields.get("_measurement_excluded", [])
        assert bool(excluded) is not blocked
        for item in excluded:
            pos = item["location"]
            assert item["row"] == text.splitlines()[pos["line"] - 1]
            assert item["keyword"] == text[pos["start"]:pos["end"]]
            assert item["reason"] in {"test_table_result", "explicit_test_result"}
            assert item["keyword"] in {"Ethanol", "Porcine DNA PCR"}
        if not blocked and doc_type == "coa_msds":
            key, value = ("pork_dna", 0.0) if "Porcine" in source else ("ethanol_pct", .02)
            assert doc.fields["measurements"][key] == value
        if not blocked and provider == "ollama" and offset:
            assert doc.fields["_analysis"]["partial"]
            assert "partial_analysis" in {x["code"] for x in doc.fields["_verify"]["advisory"]}

    # 실제 저장된 문서 상세와 확인대기 참고 항목에 모두 같은 기록을 투영한다.
    response = client.get(f"/cases/{cid}/intake-view", headers=headers)
    assert response.status_code == 200, response.text
    view = response.json()
    document = next(d for d in view["documents"] if d["document_id"] == did)
    assert document["measurement_excluded"] == excluded
    assert view["pending"]["measurement_excluded"] == [
        {**item, "document_id": did, "filename": doc_type + ".txt"} for item in excluded]
    if not blocked:
        assert view["summary"]["pending_count"] == 0

    response = client.post(f"/cases/{cid}/transition", headers=headers,
                           json={"to_state": "committee_verification"})
    assert response.status_code == (409 if blocked else 200), response.text
    if blocked:
        assert any(b["code"] == "PENDING_CRITICAL_MATERIAL"
                   for b in response.json()["detail"]["blockers"])
    with m.SessionLocal() as db:
        assert db.get(models.CaseApplication, cid).status == (
            "self_declaration_submitted" if blocked else "committee_verification")
        assert db.get(models.DocumentAsset, did).fields.get("_measurement_excluded", []) == excluded


@pytest.mark.parametrize("row,cells", [("Water | |", 3), ("Water |", 2),
                                      ("| Water | | |", 3), ("Water\t\t", 3)])
def test_empty_cells_keep_table_width(row, cells):
    assert len(intake._table_cells(row)) == cells
    text = "Ingredient | Test | Result\n" + row + "\nEthanol | Content | 70%"
    assert intake._material_scan_text(text) == text


@pytest.mark.parametrize("row", ["#" * 200, "2. CERTIFICATE OF ANALYSIS | Pork", "II. TEST REPORT ingredient"])
def test_symbols_or_title_fragments_do_not_end_a_material_list(row):
    text = "Ingredients\n" + row + "\nEthanol Result: 70%"
    assert intake._material_scan_text(text) == text


def test_exclusion_is_occurrence_based_and_does_not_hide_other_risks():
    text = ("앞부분\r\nTest\tResult\r\n1\t에탄올\t0.02%\r\n"
            "Ethanol Result: 0.02%; Porcine DNA PCR: Negative\r\n"
            "\r\nIngredients\nD&C Red No. 6\nLard\nGelatin\nEthanol Result: 70%")
    result = intake._parsed_result({"doc_type": "material_list", "fields": {}}, text)
    excluded = result["fields"]["_measurement_excluded"]
    assert [item["keyword"] for item in excluded] == ["에탄올", "Ethanol", "Porcine DNA PCR"]
    for item in excluded:
        pos = item["location"]
        assert text[pos["start"]:pos["end"]] == item["keyword"]
        assert text.splitlines()[pos["line"] - 1] == item["row"]
    scanned = intake._material_scan_text(text)
    for keyword in ("Lard", "Gelatin", "Ethanol Result: 70%"):
        assert keyword in scanned
    assert result["fields"]["_tail_screening"]["risk_hits"]
    # 검증/재추출 반복으로 기록을 중복시키지 않으며 원문이 바뀌면 낡은 기록을 제거한다.
    assert intake._parsed_result(result, text)["fields"]["_measurement_excluded"] == excluded
    assert "_measurement_excluded" not in intake._parsed_result(result, "Ingredients\nLard")["fields"]


@pytest.mark.parametrize("row", ["Lard Result: 5%", "Gelatin Result: 5%", "돼지 | 5%",
                                "Ethanol Result: pending", "Porcine DNA PCR: Negative Positive"])
def test_unexcluded_or_ambiguous_risks_stay_candidates(row):
    excluded = []
    assert intake._material_scan_text(row, excluded) == row
    assert excluded == []


@pytest.mark.parametrize("limit", [10, 45])
def test_office_text_limit_preserves_excluded_rows_and_full_source_positions(limit):
    parts = ["CERTIFICATE OF ANALYSIS", "Ethanol Result: 0.02% (tested)", "Porcine DNA PCR: Negative"]
    original = "\n".join(parts)
    text = intake._bounded_office_text(parts, limit)
    assert text == original[:limit]
    fields = intake._parsed_result({"doc_type": "coa_msds", "fields": {}}, text)["fields"]
    excluded = fields["_measurement_excluded"]
    assert [item["keyword"] for item in excluded] == ["Ethanol", "Porcine DNA PCR"]
    for item in excluded:
        pos = item["location"]
        assert item["row"] == original.splitlines()[pos["line"] - 1]
        assert item["keyword"] == original[pos["start"]:pos["end"]]


def test_pdf_page_limit_preserves_measurement_exclusion(monkeypatch):
    import fitz

    with fitz.open() as pdf:
        pdf.new_page().insert_text((40, 60), "CERTIFICATE OF ANALYSIS")
        pdf.new_page().insert_text((40, 60), "Ethanol Result: 0.02%")
        original = "".join(page.get_text() + "\n" for page in pdf)
        data = pdf.tobytes()
    monkeypatch.setattr(intake, "PDF_MAX_PAGES", 1)
    text = intake.parse_file("CoA.pdf", data)
    assert "Ethanol" not in text
    fields = intake._parsed_result({"doc_type": "coa_msds", "fields": {}}, text)["fields"]
    item, = fields["_measurement_excluded"]
    pos = item["location"]
    assert pos["page"] == 2
    assert item["row"] == original.splitlines()[pos["line"] - 1]
    assert item["keyword"] == original[pos["start"]:pos["end"]] == "Ethanol"
