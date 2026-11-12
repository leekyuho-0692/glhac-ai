"""7차 검수: COA 항목명 변형·제목 접두·상한 뒤 표 문맥과 제외 기록."""
import base64
import io
import zipfile

import docx
import pytest
from fastapi.testclient import TestClient

from app import ai_local, intake, models, state_machine as sm
import app.main as m


NAMES = ["Ethyl alcohol", "Ethyl Alcohol (%)", "Ethanol (%)", "Ethanol content",
         "Alcohol (ethanol)", "Ethanol (v/v)", "Ethanol, %"]
PREFIXES = ["(2)", "[2]", "2-", "Section 2"]


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


def _saved_transition(api, data, source, doc_type, blocked, *, filename="round7.txt", limit=None):
    client, headers = api
    created = client.post("/cases", headers=headers, json={"company_name": "Round 7"})
    assert created.status_code == 200, created.text
    cid = created.json()["case_id"]
    with m.SessionLocal() as db:
        case = db.get(models.CaseApplication, cid)
        case.status, case.pathway = "self_declaration_submitted", "reguler"
        doc = models.DocumentAsset(case_id=cid, filename=filename, doc_type=doc_type,
                                  content_b64=base64.b64encode(data).decode())
        db.add(doc)
        db.commit()
        did = doc.document_id
        m._reprocess_doc(db, doc, case, keep_doc_type=True)
        m._verify_doc(db, doc, case)
        db.commit()

    with m.SessionLocal() as db:
        doc = db.get(models.DocumentAsset, did)
        case = db.get(models.CaseApplication, cid)
        assert doc.text_full == source[:limit]
        assert doc.doc_type == doc_type
        fields = doc.fields
        assert fields.get("_verify")
        assert bool(sm.pending_extractions(db, cid)) is blocked
        assert any(b["code"] == "PENDING_CRITICAL_MATERIAL"
                   for b in sm.material_safety_blockers(db, case)) is blocked
        excluded = fields.get("_measurement_excluded", [])
        assert bool(excluded) is not blocked
        positions = []
        for item in excluded:
            pos = item["location"]
            assert item["row"] == source.splitlines()[pos["line"] - 1]
            assert item["keyword"] == source[pos["start"]:pos["end"]]
            assert item["reason"] in {"test_table_result", "explicit_test_result"}
            positions.append((pos["start"], pos["end"]))
        assert len(positions) == len(set(positions))

    response = client.get(f"/cases/{cid}/intake-view", headers=headers)
    assert response.status_code == 200, response.text
    view = response.json()
    document = next(d for d in view["documents"] if d["document_id"] == did)
    assert document["measurement_excluded"] == excluded
    assert view["pending"]["measurement_excluded"] == [
        {**item, "document_id": did, "filename": filename} for item in excluded]
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
    return fields


SOURCES = [
    *[("CERTIFICATE OF ANALYSIS\nTest | Result\n" + name + " | 0.02%", False, .02)
      for name in NAMES],
    *[("Ingredient | Test | Result\nWater | |\n" + name + " | 70", True, None)
      for name in NAMES],
    *[("Ingredients | %\n" + name + " | 70", True, None)
      for name in ("Ethyl alcohol", "Ethanol (%)")],
    *[("Ingredients\nWater\n" + prefix + " CERTIFICATE OF ANALYSIS\nEthanol Result: 0.02%",
       False, .02) for prefix in PREFIXES],
    ("Ingredients\nWater\n\nEthanol Result: 70%", False, 70.0),
]


@pytest.mark.parametrize("provider", ["openai", "ollama", "none"])
@pytest.mark.parametrize("doc_type", ["material_list", "coa_msds"])
@pytest.mark.parametrize("offset", [0, 3100])
@pytest.mark.parametrize("source,blocked,value", SOURCES)
def test_name_and_section_boundaries_survive_save_and_transition(
        api, monkeypatch, provider, doc_type, offset, source, blocked, value):
    calls = _model(monkeypatch, provider, doc_type)
    source = ("x" * offset + "\n" if offset else "") + source
    fields = _saved_transition(api, source.encode(), source, doc_type, blocked)
    assert bool(calls) is (provider != "none")
    if not blocked:
        assert len(fields["_measurement_excluded"]) == 1
        if doc_type == "coa_msds":
            assert fields["measurements"]["ethanol_pct"] == value
    if not blocked and provider == "ollama" and offset:
        assert fields["_analysis"]["partial"]
        assert "partial_analysis" in {x["code"] for x in fields["_verify"]["advisory"]}


@pytest.fixture(scope="module")
def office_document():
    cache = {}

    def build(name, delimiter, blocked, header_before_limit):
        key = name, delimiter, blocked, header_before_limit
        if key not in cache:
            header = delimiter.join(["Ingredient" if blocked else "Test", "Result"])
            if header_before_limit:
                parts = ["x" * (intake.OFFICE_MAX_TEXT_CHARS - len(header) - 2), header]
            else:
                parts = ["x" * intake.OFFICE_MAX_TEXT_CHARS, header]
            parts += [delimiter.join([name, "70" if blocked else "0.02%"])]
            if not blocked:
                parts += [delimiter.join(["Porcine DNA PCR", "Negative"])]
            document = docx.Document()
            for part in parts:
                document.add_paragraph(part)
            compressed = io.BytesIO()
            document.save(compressed)
            # 반복 채움 문자가 압축비 방어를 건드리지 않도록 저장 방식만 바꾼다.
            output = io.BytesIO()
            with zipfile.ZipFile(compressed) as original, zipfile.ZipFile(output, "w") as stored:
                for path in original.namelist():
                    stored.writestr(path, original.read(path))
            cache[key] = output.getvalue(), "\n".join(parts)
        return cache[key]

    return build


@pytest.mark.parametrize("provider", ["openai", "ollama", "none"])
@pytest.mark.parametrize("doc_type", ["material_list", "coa_msds"])
@pytest.mark.parametrize("name", ["Ethanol", "Ethyl alcohol", "Ethanol (%)"])
@pytest.mark.parametrize("delimiter", [" | ", "｜"])
@pytest.mark.parametrize("header_before_limit", [False, True])
@pytest.mark.parametrize("blocked", [False, True])
def test_docx_tail_keeps_table_context_and_all_records(
        api, monkeypatch, office_document, provider, doc_type, name, delimiter,
        header_before_limit, blocked):
    _model(monkeypatch, provider, doc_type)
    data, source = office_document(name, delimiter, blocked, header_before_limit)
    fields = _saved_transition(api, data, source, doc_type, blocked,
                               filename="round7.docx", limit=intake.OFFICE_MAX_TEXT_CHARS)
    if not blocked:
        excluded = fields["_measurement_excluded"]
        assert len(excluded) == 2
        assert excluded[0]["keyword"] == ("Ethanol" if name == "Ethanol (%)" else name)
        assert excluded[1]["keyword"] == "Porcine DNA PCR"
        assert all(item["reason"] == "test_table_result" for item in excluded)
        assert all(item["location"]["start"] >= intake.OFFICE_MAX_TEXT_CHARS for item in excluded)


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("delimiter", [" | ", "｜", "\t", "  "])
def test_analyte_labels_require_test_context_and_keep_source_positions(name, delimiter):
    row = delimiter.join([name, "0.02%"])
    text = delimiter.join(["Test", "Result"]) + "\r\n" + row
    excluded = []
    scanned = intake._material_scan_text(text, excluded)
    assert "alcohol" not in scanned.lower() and "ethanol" not in scanned.lower()
    assert len(scanned) == len(text)
    item, = excluded
    pos = item["location"]
    assert item["row"] == row
    assert pos["line"] == 2
    assert text[pos["start"]:pos["end"]] == item["keyword"]
    # content는 기존의 명시적 시험 라벨이다. 나머지는 단위/구분자만으로 제외하지 않는다.
    if "content" not in name:
        assert intake._material_scan_text(row) == row
    assert intake._material_scan_text("Ingredients\nWater\n" + row).endswith(row)


@pytest.mark.parametrize("row", [
    "Ethanol (unknown) | 0.02%", "Ethanol (70%) | 0.02%",
    "Ethyl alcohol | pending", "Alcohol (ethanol) | Negative Positive",
    "Ethanol (%) | not tested", "Ethanol (v/v) | see attached",
])
def test_unrecognized_labels_and_unclear_results_stay_candidates(row):
    text = "Test | Result\n" + row
    excluded = []
    assert intake._material_scan_text(text, excluded) == text
    assert not excluded


@pytest.mark.parametrize("prefix", PREFIXES)
def test_prefixed_title_must_still_match_the_entire_line(prefix):
    text = "Ingredients\nWater\n" + prefix + " CERTIFICATE OF ANALYSIS | Pork\nEthanol Result: 70%"
    assert intake._material_scan_text(text) == text


@pytest.mark.parametrize("parts,keywords", [
    (["Test | Result", "Ethyl alcohol | 0.02%", "Porcine DNA PCR | Negative"],
     ["Ethyl alcohol", "Porcine DNA PCR"]),
    (["Ingredients", "Water", "Ethanol (%) Result: 70%"], []),
    (["Ingredients", "Water", "", "Ethanol (%) Result: 70%"], ["Ethanol"]),
    (["Ingredients\nWater\n", "Ethanol (%) Result: 70%"], ["Ethanol"]),
    (["Test | Result", "", "Ethanol (%) | 70%"], []),
    (["Test | Result", "Ingredients | Result", "Ethyl alcohol | 70%"], []),
])
@pytest.mark.parametrize("limit", [1, 14, 22, 35])
def test_office_cap_preserves_context_across_parts_and_blank_lines(parts, keywords, limit):
    source = "\n".join(parts)
    text = intake._bounded_office_text(parts, limit)
    assert text == source[:limit]
    fields = intake._parsed_result({"doc_type": "material_list", "fields": {}}, text)["fields"]
    excluded = fields.get("_measurement_excluded", [])
    assert [item["keyword"] for item in excluded] == keywords
    for item in excluded:
        pos = item["location"]
        assert item["row"] == source.splitlines()[pos["line"] - 1]
        assert item["keyword"] == source[pos["start"]:pos["end"]]
    if keywords:
        assert not text.parse_fields.get("_tail_screening", {}).get("risk_hits")
        # 결과 단어 중간에서 잘린 본문은 기존대로 후보를 남긴다. 완전한 꼬리 행의
        # 제외 기록을 근거로 불완전한 본문의 별도 판정을 바꾸지는 않는다.
        incomplete_result = str(text).endswith("Ethanol (%) Resu")
        assert bool(fields.get("_tail_screening", {}).get("risk_hits")) is incomplete_result
    else:
        assert fields["_tail_screening"]["risk_hits"]


@pytest.mark.parametrize("materials_first", [False, True])
def test_repeated_names_preserve_each_measurement_record_and_the_ingredient_risk(materials_first):
    coa = "Test | Result\n" + "\n".join(name + " | 0.02%" for name in NAMES)
    materials = "Ingredients | %\n" + "\n".join(name + " | 70" for name in NAMES)
    source = "\n\n".join([materials, coa] if materials_first else [coa, materials])
    result = intake._parsed_result({"doc_type": "material_list", "fields": {}}, source)
    excluded = result["fields"]["_measurement_excluded"]
    assert len(excluded) == len(NAMES)
    assert result["fields"]["_tail_screening"]["risk_hits"]
    for item in excluded:
        pos = item["location"]
        assert item["row"] == source.splitlines()[pos["line"] - 1]
        assert item["row"].endswith(" | 0.02%")
        assert item["keyword"] == source[pos["start"]:pos["end"]]
    assert intake._parsed_result(result, source)["fields"]["_measurement_excluded"] == excluded


@pytest.mark.parametrize("name", NAMES)
def test_pdf_tail_table_keeps_both_exclusions_and_page_positions(monkeypatch, name):
    import fitz

    with fitz.open() as pdf:
        pdf.new_page().insert_text((40, 60), "CERTIFICATE OF ANALYSIS")
        pdf.new_page().insert_text((40, 60), "Test | Result\n" + name + " | 0.02%\nPorcine DNA PCR | Negative")
        original = "".join(page.get_text() + "\n" for page in pdf)
        data = pdf.tobytes()
    monkeypatch.setattr(intake, "PDF_MAX_PAGES", 1)
    text = intake.parse_file("round7.pdf", data)
    fields = intake._parsed_result({"doc_type": "coa_msds", "fields": {}}, text)["fields"]
    assert not fields.get("_tail_screening", {}).get("risk_hits")
    excluded = fields["_measurement_excluded"]
    assert len(excluded) == 2
    for item in excluded:
        pos = item["location"]
        assert pos["page"] == 2
        assert item["row"] == original.splitlines()[pos["line"] - 1]
        assert item["keyword"] == original[pos["start"]:pos["end"]]
