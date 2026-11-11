"""CoA 판독·출처 보존 — 뒤의 적합 성적서가 부적합/확인 대기를 지우지 않는다."""
import base64
from io import BytesIO
import os
import sys
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_coa_test.db")

import docx                                # noqa: E402
import fitz                                # noqa: E402
import pytest                              # noqa: E402
from fastapi.testclient import TestClient   # noqa: E402

import app.main as m                        # noqa: E402
from app import intake, models              # noqa: E402


@pytest.mark.parametrize("text,expected", [
    ("Porcine DNA (PCR): DETECTED\nSalmonella: Negative", 1.0),
    ("Pork DNA PCR result: 0.02% detected\nRemarks: none", 1.0),
    ("Porcine DNA: Not Detected", 0.0),
    ("Babi DNA: tidak terdeteksi", 0.0),
    ("Porcine DNA: see attached", None),
    ("Porcine DNA (PCR) | Result\nDetected", 1.0),
    ("PCR species test\nPorcine: Detected", 1.0),
    ("Salmonella: Detected\nPorcine DNA: Negative", 0.0),
    ("Porcine DNA: Negative\nSalmonella: Detected", 0.0),
    ("DNA babi (PCR): Positif\nE. coli: tidak terdeteksi", 1.0),
    ("Swine DNA: Positive", 1.0),
    ("Pig DNA: non-detected", 0.0),
    ("Porcine-specific PCR: detected", 1.0),
    ("돼지 DNA: 불검출", 0.0),
    ("돈 DNA: 음성", 0.0),
    ("돼지 DNA: 양성", 1.0),
    ("돼지 DNA: 검출", 1.0),
    ("Babi DNA: negatif", 0.0),
    ("Babi DNA: terdeteksi", 1.0),
    ("Porcine DNA: absent", 0.0),
    ("Porcine DNA: none", 0.0),
    ("돼지 DNA: 없음", 0.0),
    ("Porcine DNA: Detected / Not Detected", None),
    ("Porcine DNA: Detected\nPorcine DNA: Negative", None),
    ("Porcine DNA\nResult: Not detected", 0.0),
    ("Porcine DNA\n0.02% detected", 1.0),
    ("Porcine DNA: see attached\nSalmonella: Negative", None),
    ("Porcine DNA: see attached\nRemarks: none", None),
    ("Porcine DNA: undetected", None),  # 지원하지 않는 표기는 추측하지 않는다
    ("Porcine DNA Test Report\nSample: Gelatin\nPorcine DNA: Detected", 1.0),
    ("돈지 검사 DNA: 양성", 1.0),
])
def test_pork_dna_항목_범위와_양부정_판독(text, expected):
    result = intake.extract_measurements(text)
    if expected is None:
        assert result == {"pork_dna_unclear": True}
    else:
        assert result == {"pork_dna": expected}


@pytest.mark.parametrize("text", [
    "", "Salmonella: Negative", "Pigment: Positive",
    "Source: bovine, free from pork",
    "Gelatin (porcine)",
    "Bahan: lemak babi tidak digunakan",
])
def test_돼지_항목이_없으면_측정값을_만들지_않는다(text):
    assert intake.extract_measurements(text) == {}


def _drain():
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if all(q is None or q.unfinished_tasks == 0 for q in (m._autoparse_q, m._verify_q)):
            return
        time.sleep(0.02)
    pytest.fail("자동 파싱·검증 큐가 비지 않았다")


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch):
    monkeypatch.setattr(m, "_RL_WRITE_HITS", {})
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda *a, **kw: {"error": "LLM_UNAVAILABLE"})
    yield
    _drain()


@pytest.fixture
def client(fake_llm):
    with TestClient(m.app) as c:
        yield c


def _tok(client, username="applicant1"):
    r = client.post("/auth/login", json={"username": username, "password": "pw"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


@pytest.fixture
def case(client):
    headers = _tok(client)
    r = client.post("/cases", json={"company_name": "PT CoA"}, headers=headers)
    assert r.status_code == 200, r.text
    return headers, r.json()["case_id"]


def _pdf(text):
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((50, 60), "Certificate of Analysis\n" + text, fontsize=9)
        return base64.b64encode(doc.tobytes()).decode()


def _zip(entries):
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for filename, content in entries:
            archive.writestr(filename, base64.b64decode(content))
    return base64.b64encode(buf.getvalue()).decode()


def _upload(client, case, filename, content, route="intake-zip"):
    headers, cid = case
    body = ({"zip_b64": _zip([(filename, content)])} if route == "intake-zip" else
            {"doc_type": "coa_msds", "filename": filename, "file_b64": content})
    r = client.post(f"/cases/{cid}/{route}", json=body, headers=headers)
    assert r.status_code == 200, r.text
    _drain()
    return r.json()


def _rows(cid):
    with m.SessionLocal() as db:
        return [{"id": r.measurement_id, "document_id": r.document_id, "material_id": r.material_id,
                 "param_key": r.param_key, "value": r.value, "verdict": r.verdict}
                for r in db.query(models.MaterialMeasurement).filter_by(case_id=cid).all()]


def _quant(client, case):
    headers, cid = case
    r = client.get(f"/cases/{cid}/material-report", headers=headers)
    assert r.status_code == 200, r.text
    return r.json(), next(x for x in r.json()["quantitative"] if x["param_key"] == "pork_dna")


@pytest.mark.parametrize("route", ["intake-zip", "parse-file", "documents"])
@pytest.mark.parametrize("reverse", [False, True])
def test_다른_성적서의_fail은_업로드_순서와_무관하게_보존된다(client, case, route, reverse):
    headers, cid = case
    material_ids = {}
    for name in ("Gelatin", "Sugar"):
        r = client.post(f"/cases/{cid}/materials", headers=headers, json={"name": name})
        assert r.status_code == 200, r.text
        material_ids[name] = r.json()["material_id"]
    entries = [("CoA_Gelatin.pdf", _pdf("Material: Gelatin\nPorcine DNA: Detected\nSalmonella: Negative")),
               ("CoA_Sugar.pdf", _pdf("Material: Sugar\nPorcine DNA: Not Detected"))]
    for filename, content in reversed(entries) if reverse else entries:
        _upload(client, case, filename, content, route)
    rows = _rows(cid)
    assert len(rows) == 2
    assert len({r["document_id"] for r in rows}) == 2
    assert {r["material_id"]: (r["value"], r["verdict"]) for r in rows} == {
        material_ids["Gelatin"]: (1.0, "fail"), material_ids["Sugar"]: (0.0, "pass")}
    with m.SessionLocal() as db:
        for row in rows:
            doc = db.get(models.DocumentAsset, row["document_id"])
            name = "Gelatin" if row["verdict"] == "fail" else "Sugar"
            assert name in doc.filename
    listed = client.get(f"/cases/{cid}/measurements", headers=headers).json()
    assert (listed["count"], listed["fail_count"], listed["verdict"]) == (2, 1, "fail")
    assert client.get(f"/cases/{cid}", headers=headers).json()["quant_verdict"] == "fail"
    report, quant = _quant(client, case)
    assert (report["quant_fail"], report["quant_verdict"], quant["value"], quant["verdict"]) == (1, "fail", 1.0, "fail")
    assert quant["material_id"] == material_ids["Gelatin"]


def test_같은_ZIP의_다른_서류는_누적하고_동일_원본은_한_번만_반영한다(client, case):
    headers, cid = case
    fail = _pdf("Material: Gelatin\nPorcine DNA: Positive")
    passed = _pdf("Material: Sugar\nPorcine DNA: Negative")
    body = {"zip_b64": _zip([("CoA_Gelatin.pdf", fail), ("CoA_Sugar.pdf", passed),
                             ("CoA_copy.pdf", fail)])}
    ids = None
    for _ in range(2):
        r = client.post(f"/cases/{cid}/intake-zip", json=body, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["applied"]["measurements"] == 2
        rows = _rows(cid)
        assert len(rows) == 2 and {x["verdict"] for x in rows} == {"fail", "pass"}
        now = {x["id"] for x in rows}
        assert ids is None or now == ids
        ids = now
        with m.SessionLocal() as db:
            assert db.query(models.DocumentAsset).filter_by(case_id=cid).count() == 2
    assert _quant(client, case)[0]["quant_verdict"] == "fail"


def test_재처리는_같은_문서_행만_갱신하고_모호해진_값도_반영한다(client, case):
    _, cid = case
    _upload(client, case, "CoA_Gelatin.pdf", _pdf("Porcine DNA: Detected"))
    _upload(client, case, "CoA_Sugar.pdf", _pdf("Porcine DNA: Negative"))
    rows = _rows(cid)
    target = next(r for r in rows if r["verdict"] == "pass")
    operator = _tok(client, "operator1")
    for _ in range(2):
        r = client.post(f"/documents/{target['document_id']}/reprocess", headers=operator)
        assert r.status_code == 200, r.text
        _drain()
        assert _rows(cid) == rows
    with m.SessionLocal() as db:
        doc = db.get(models.DocumentAsset, target["document_id"])
        doc.content_b64 = _pdf("Porcine DNA: see attached")
        db.commit()
    r = client.post(f"/documents/{target['document_id']}/reprocess", headers=operator)
    assert r.status_code == 200, r.text
    _drain()
    changed = _rows(cid)
    assert {x["id"] for x in changed} == {x["id"] for x in rows}
    assert next(x for x in changed if x["id"] == target["id"])["value"] is None
    assert {x["verdict"] for x in changed} == {"review", "fail"}
    assert _quant(client, case)[0]["quant_verdict"] == "fail"


@pytest.mark.parametrize("reverse", [False, True])
def test_review는_pass보다_우선하며_보고서에서_사라지지_않는다(client, case, reverse, monkeypatch):
    headers, cid = case
    entries = [("CoA_unknown.pdf", _pdf("Porcine DNA: see attached")),
               ("CoA_pass.pdf", _pdf("Porcine DNA: Negative"))]
    for filename, content in reversed(entries) if reverse else entries:
        _upload(client, case, filename, content)
    report, quant = _quant(client, case)
    assert (report["quant_fail"], report["quant_review"], report["quant_verdict"]) == (0, 1, "review")
    assert quant["value"] is None and quant["verdict"] == "review"
    listed = client.get(f"/cases/{cid}/measurements", headers=headers).json()
    assert listed["review_count"] == 1 and listed["verdict"] == "review"
    staff = _tok(client, "operator1")
    dossier = client.get(f"/cases/{cid}/preassess/dossier", headers=staff).json()
    assert dossier["quant_review"] == 1 and dossier["quant_verdict"] == "review"
    r = client.get(f"/cases/{cid}/preassess-report.docx", headers=headers)
    assert r.status_code == 200, r.text
    tables = docx.Document(BytesIO(r.content)).tables
    table_rows = [row for table in tables for row in table.rows if row.cells[0].text == "돈지·돼지 DNA"]
    assert len(table_rows) == 1
    assert [c.text for c in table_rows[0].cells][1:] == ["-", "≤ 0.0", "보완 필요"]
    rendered = []
    monkeypatch.setattr(m, "_render_pdf", lambda title, text, **kw: rendered.append(text) or b"%PDF-1.4")
    r = client.get(f"/cases/{cid}/material-report.pdf", headers=headers)
    assert r.status_code == 200
    assert "돈지·돼지 DNA: 미확정 detect — 사람 확인 필요" in rendered[0]


@pytest.mark.parametrize("result,verdict,code", [
    ("Detected", "fail", "MEASUREMENT_FAILED"),
    ("see attached", "review", "MEASUREMENT_REVIEW_REQUIRED"),
])
def test_부적합과_확인대기는_판정과_발급을_차단한다(client, case, result, verdict, code):
    headers, cid = case
    _upload(client, case, "CoA_risk.pdf", _pdf("Porcine DNA: " + result))
    _upload(client, case, "CoA_good.pdf", _pdf("Porcine DNA: Negative"))
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        c.status, c.fatwa_status, c.scope_frozen, c.pathway = "fatwa_approved", "approved", True, "self_declare"
        analysis = m._ai_second_analysis_compute(db, c)
        assert analysis["quant_verdict"] == verdict and code in analysis["blockers"]
        assert analysis["probability"] < 80
        missing, ctx = m._fatwa_gate_check(db, c)
        assert code in missing and ctx["quant_verdict"] == verdict
        m.sm.record_event(db, c, c.status, c.status, "ai_second_analysis", "system", "test", analysis)
        db.commit()
    staff = _tok(client, "operator1")
    timeline = client.get(f"/cases/{cid}/timeline", headers=headers).json()
    assert code in {b["code"] for b in timeline["blockers"]}
    report = client.post(f"/cases/{cid}/report", headers=headers)
    assert report.status_code == 200 and code in report.json()["report"]
    for route, body in [("certificate/issue", {}), ("eligibility/verdict", {"verdict": "eligible"})]:
        r = client.post(f"/cases/{cid}/{route}", headers=staff, json=body)
        assert r.status_code == 409 and r.json()["detail"]["code"] == code, r.text
    hpas = client.get(f"/cases/{cid}/hpas-auto", headers=headers).json()
    mat = next(x for x in hpas["elements"] if x["element"] == "materials")
    assert mat["state3"] == ("fail" if verdict == "fail" else "gap")
    with m.SessionLocal() as db:
        assert db.query(models.HalalCertificate).filter_by(case_id=cid).count() == 0


def test_수동_POST는_출처가_같아도_누적되며_자동_재처리가_덮지_않는다(client, case):
    headers, cid = case
    _upload(client, case, "CoA_manual.pdf", _pdf("Porcine DNA: Negative"))
    did = _rows(cid)[0]["document_id"]
    manual = []
    for value in (1.0, 0.0, None):
        r = client.post(f"/cases/{cid}/measurements", headers=headers,
                        json={"param_key": "pork_dna", "value": value, "document_id": did})
        assert r.status_code == 200, r.text
        assert r.json()["verdict"] == ("unknown" if value is None else "fail" if value else "pass")
        manual.append(r.json()["measurement_id"])
    before = _rows(cid)
    r = client.post(f"/documents/{did}/reprocess", headers=_tok(client, "operator1"))
    assert r.status_code == 200, r.text
    _drain()
    assert _rows(cid) == before and len(before) == 4
    assert set(manual) <= {row["id"] for row in before}
    assert _quant(client, case)[0]["quant_verdict"] == "fail"


def test_기존_출처없는_fail을_보존하고_본문_뒤쪽_원재료도_연결한다(client, case):
    headers, cid = case
    with m.SessionLocal() as db:
        mat = models.Material(case_id=cid, name="Sugar", screen_result="CLEARED")
        db.add(mat)
        db.add(models.MaterialMeasurement(case_id=cid, param_key="pork_dna", value=1.0,
                                          verdict="fail", recorded_by="ai"))
        db.commit()
        mid = mat.material_id
    text = "Laboratory analysis details.\n" * 15 + "Material: Sugar\nPorcine DNA: Negative"
    _upload(client, case, "CoA_scan.pdf", _pdf(text))
    rows = _rows(cid)
    assert len(rows) == 2
    assert next(x for x in rows if x["document_id"] is None)["verdict"] == "fail"
    assert next(x for x in rows if x["document_id"] is not None)["material_id"] == mid
    assert _quant(client, case)[0]["quant_verdict"] == "fail"


@pytest.mark.parametrize("values,expected,verdict", [
    ([4.0, 0.5], 4.0, "fail"), ([0.5, 4.0], 4.0, "fail"), ([0.8, 0.3], 0.8, "pass"),
])
def test_다른_정량_항목도_최악_판정과_최대_수치를_사용한다(client, case, values, expected, verdict):
    headers, cid = case
    for value in values:
        r = client.post(f"/cases/{cid}/measurements", headers=headers,
                        json={"param_key": "lead_ppm", "value": value})
        assert r.status_code == 200, r.text
    report = client.get(f"/cases/{cid}/material-report", headers=headers).json()
    quant = next(x for x in report["quantitative"] if x["param_key"] == "lead_ppm")
    assert (quant["value"], quant["verdict"], report["quant_verdict"]) == (expected, verdict, verdict)


def test_동일_문서_동시_자동반영도_중복_행을_만들지_않는다(client, case):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    _, cid = case
    with m.SessionLocal() as db:
        doc = models.DocumentAsset(case_id=cid, filename="CoA_concurrent.pdf", doc_type="coa_msds",
                                   fields={"measurements": {"pork_dna": 1.0}})
        db.add(doc)
        db.commit()
        did = doc.document_id
    barrier = Barrier(2)

    def apply():
        with m.SessionLocal() as db:
            c = db.get(models.CaseApplication, cid)
            doc = db.get(models.DocumentAsset, did)
            barrier.wait(timeout=5)
            count = m._apply_document_measurements(db, c, doc)
            db.commit()
            return count

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(apply) for _ in range(2)]
        assert [job.result(timeout=10) for job in jobs] == [1, 1]
    rows = _rows(cid)
    assert len(rows) == 1 and rows[0]["document_id"] == did and rows[0]["verdict"] == "fail"
