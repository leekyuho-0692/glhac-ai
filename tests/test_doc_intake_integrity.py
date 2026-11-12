"""서류 업로드 무결성 — 잘못 고른 칸·공급사 정보로 신청 업체를 덮지 않는다."""
import base64
import logging
import os
import queue
import sys
import time
from datetime import datetime
from io import BytesIO

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_doc_intake_integrity_test.db")

import fitz                                # noqa: E402
import pytest                              # noqa: E402
from openpyxl import Workbook              # noqa: E402
from fastapi.testclient import TestClient   # noqa: E402

import app.main as m                        # noqa: E402
from app import intake, models              # noqa: E402
from app.main import app                    # noqa: E402


PLAIN_TEXT = ("Scanned document\nManufacturer BIO-FD&C Co., Ltd.\nAddress Hwasun\n"
              + "Technical details for the supplied ingredient.\n" * 8)
MSDS_TEXT = "MATERIAL SAFETY DATA SHEET\nOligopeptide-1\n" + PLAIN_TEXT


def _pdf(text=PLAIN_TEXT):
    """OCR·외부 파일 없이 실제 PDF 텍스트 추출 경로를 지난다."""
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), text)
        return base64.b64encode(doc.tobytes()).decode()


def _drain():
    """파싱 뒤 검증까지 끝내야 다음 테스트에서 LLM 대역을 안전하게 바꿀 수 있다."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if all(q is None or q.unfinished_tasks == 0 for q in (m._autoparse_q, m._verify_q)):
            return
        time.sleep(0.02)
    pytest.fail("자동 파싱·검증 큐가 비지 않았다")


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch):
    # 각 테스트의 쓰기 예산을 격리한다 — 많은 업로드 회귀 사례가 서로 429를 유발하지 않게 한다.
    monkeypatch.setattr(m, "_RL_WRITE_HITS", {})
    fields = {"company_name": "BIO-FD&C Co., Ltd.", "address": "Hwasun"}

    def fake(system, user, **kw):
        if "접수 검수자" in system:
            return {"verdict": "warn", "summary": "x", "doc_type_ok": False,
                    "detected_type": "coa_msds", "field_issues": []}
        if "사업자등록번호" in system:
            return dict(fields)
        if "원료 성적서(CoA)" in system:
            return {"material_names": ["Oligopeptide-1"], "product_names": []}
        return {"doc_type": "nib_business_license", "confidence": 0.85, "fields": dict(fields)}

    monkeypatch.setattr(intake.ai_local, "llm_json", fake)
    yield fields
    _drain()


def _tok(client, username="applicant1"):
    r = client.post("/auth/login", json={"username": username, "password": "pw"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


@pytest.fixture
def client(fake_llm):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def case(client):
    h = _tok(client)
    r = client.post("/cases", json={"company_name": "PT Integrity"}, headers=h)
    assert r.status_code == 200, r.text
    cid = r.json()["case_id"]
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        c.address = "Seoul"
        db.commit()
    yield h, cid
    _drain()
    # 같은 예시 사업자번호를 다음 업체 픽스처가 재사용하지 않게 격리한다.
    # 종결 상태도 포함하는 테스트 데이터 정리이므로 API를 거치지 않는다.
    with m.SessionLocal() as db:
        db.query(models.CaseApplication).filter_by(case_id=cid).update({"nib": None})
        db.commit()


def _upload(client, case, filename="scan001.pdf", text=PLAIN_TEXT):
    h, cid = case
    r = client.post(f"/cases/{cid}/parse-file", headers=h,
                    json={"doc_type": "nib_business_license", "filename": filename,
                          "file_b64": _pdf(text)})
    assert r.status_code == 200, r.text
    return r.json()


def test_MSDS는_올바른_칸으로_저장하고_업체를_보존한다(client, case):
    h, cid = case
    res = _upload(client, case, "[MSDS] Oligopeptide-1.pdf", MSDS_TEXT)
    assert res["mismatch"]["selected"] == "nib_business_license"
    assert res["mismatch"]["detected"] == res["doc_type"] == "coa_msds"
    assert res["mismatch"]["reason"]
    assert res["extracted"]["material_names"] == ["Oligopeptide-1"]
    assert res["applied"]["materials_added"] == 0
    assert res["applied"]["materials_pending"] == 1
    _drain()
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        assert (c.company_name, c.address, c.nib) == ("PT Integrity", "Seoul", None)
        d = db.query(models.DocumentAsset).filter_by(case_id=cid).one()
        assert d.doc_type == "coa_msds"
        assert "MATERIAL SAFETY DATA SHEET" in d.text_full
        assert len(d.text_full) > len(d.text_excerpt) == 300
        assert d.fields["_verify"]["verdict"] == "warn"   # 저장 뒤 검증 큐도 실행된다
        ext = db.query(models.AiExtraction).filter_by(case_id=cid).one()
        assert ext.source == "parse_file:coa_msds"
        event = db.query(models.WorkflowEvent).filter_by(case_id=cid, action="documents.parse_file").one()
        assert event.payload["doc_type"] == "coa_msds"
        assert db.query(models.Material).filter_by(case_id=cid, name="Oligopeptide-1").count() == 0


@pytest.mark.parametrize("doc_type,field,value,model,bucket", [
    ("material_list", "material_names", "Water", models.Material, "materials"),
    ("coa_msds", "material_names", "Oligopeptide-1", models.Material, "materials"),
    ("product_label", "ingredients", "Sugar", models.Material, "materials"),
    ("coa_msds", "product_names", "Face Cream", models.Product, "products"),
    ("product_label", "product_names", "Face Cream", models.Product, "products"),
])
def test_LLM_목록은_확인_대기에_나타나고_선택_반영_후에만_등록된다(
        client, case, monkeypatch, doc_type, field, value, model, bucket):
    h, cid = case
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda *a, **kw: {field: [value]})
    r = client.post(f"/cases/{cid}/parse-file", headers=h,
                    json={"doc_type": doc_type, "filename": "scan001.pdf", "file_b64": _pdf()})
    assert r.status_code == 200, r.text
    assert r.json()["applied"][bucket + "_added"] == 0
    assert r.json()["applied"][bucket + "_pending"] == 1
    _drain()
    kind = "material_names" if bucket == "materials" else "product_names"
    with m.SessionLocal() as db:
        assert db.query(model).filter_by(case_id=cid).count() == 0
        d = db.query(models.DocumentAsset).filter_by(case_id=cid).one()
        assert d.fields["_sources"][kind] == "llm"
    pending = client.get(f"/cases/{cid}/pending-extractions", headers=h).json()["items"]
    assert [(it["kind"], it["value"], it["filename"]) for it in pending] == [(kind, value, "scan001.pdf")]
    r = client.post(f"/cases/{cid}/pending-extractions/apply", headers=h, json={"values": [value]})
    assert r.status_code == 200, r.text
    assert r.json()["applied"][bucket] == 1
    with m.SessionLocal() as db:
        assert db.query(model).filter_by(case_id=cid).one().name == value
    assert client.get(f"/cases/{cid}/pending-extractions", headers=h).json()["items"] == []
    # 같은 선택을 재시도해도 중복 원료·제품을 만들지 않는다.
    r = client.post(f"/cases/{cid}/pending-extractions/apply", headers=h, json={"values": [value]})
    assert r.json()["applied"][bucket] == 0


def test_전성분표_xlsx_구조_파서_값은_즉시_등록한다(client, case, monkeypatch):
    h, cid = case
    wb = Workbook()
    ws = wb.active
    ws.append(["구조 파서 제품"])
    ws.append(["원료명", "함량"])
    ws.append(["Water", "80%"])
    ws.append(["Sugar", "20%"])
    buf = BytesIO()
    wb.save(buf)
    wb.close()
    # 구조 파서는 이 LLM 목록을 읽거나 섞지 않는다.
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda *a, **kw: {"material_names": ["없는 원료"]})
    body = {"doc_type": "material_list", "filename": "전성분표.xlsx",
            "file_b64": base64.b64encode(buf.getvalue()).decode()}
    r = client.post(f"/cases/{cid}/parse-file", headers=h, json=body)
    assert r.status_code == 200, r.text
    applied = r.json()["applied"]
    assert applied["materials_added"] == 2 and applied["materials_pending"] == 0
    assert applied["products_added"] == 1 and applied["products_pending"] == 0
    _drain()
    with m.SessionLocal() as db:
        assert {v.name for v in db.query(models.Material).filter_by(case_id=cid)} == {"Water", "Sugar"}
        assert db.query(models.Product).filter_by(case_id=cid).one().name == "구조 파서 제품"
        assert db.query(models.DocumentAsset).filter_by(case_id=cid).one().fields["_sources"] == {
            "material_names": "structural", "product_names": "structural"}
    assert client.get(f"/cases/{cid}/pending-extractions", headers=h).json()["items"] == []
    r = client.post(f"/cases/{cid}/parse-file", headers=h, json=body)
    assert r.json()["applied"]["materials_added"] == 0
    assert r.json()["applied"]["products_added"] == 0


def test_parse_file_AI실패는_실패목록과_재처리에_포함한다(client, case, monkeypatch):
    h, cid = case
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda *a, **kw: {"error": "TIMEOUT"})
    r = client.post(f"/cases/{cid}/parse-file", headers=h,
                    json={"doc_type": "material_list", "filename": "scan001.pdf", "file_b64": _pdf()})
    assert r.status_code == 200, r.text
    _drain()
    failed = client.get(f"/cases/{cid}/pending-extractions", headers=h).json()["failed"]
    assert len(failed) == 1 and failed[0]["error"] == "TIMEOUT"
    queued = []
    monkeypatch.setattr(m, "autoparse_enqueue", lambda did, keep_doc_type=False: queued.append((did, keep_doc_type)) or True)
    r = client.post(f"/cases/{cid}/documents/reprocess-pending", headers=_tok(client, "operator1"))
    assert r.status_code == 200, r.text
    assert r.json()["queued"] == 1 and queued == [(failed[0]["document_id"], True)]


def test_수정한_원재료_직접_등록도_감사_로그에_남는다(client, case):
    h, cid = case
    r = client.post(f"/cases/{cid}/materials", headers=h,
                    json={"name": "수정한 원재료", "supplier": "공급사", "origin": "대한민국"})
    assert r.status_code == 200, r.text
    with m.SessionLocal() as db:
        row = db.query(models.AuditLog).filter_by(case_id=cid, action="material.add").one()
        assert row.resource_id == r.json()["material_id"]
        assert row.actor_role == "applicant"
        assert row.meta["name"] == "수정한 원재료"
        assert row.meta["supplier"] == "공급사" and row.meta["origin"] == "대한민국"


@pytest.mark.parametrize("nib", [None, "---"])
def test_모르는_문서의_사업자번호가_없으면_업체를_보존한다(client, case, fake_llm, nib):
    h, cid = case
    if nib is not None:
        fake_llm["nib"] = nib
    assert intake.refine_doctype_reason("scan001.pdf", None, PLAIN_TEXT)[0] is None
    res = _upload(client, case)
    assert res["mismatch"] is None
    assert res["applied"] == {"skipped": "no_nib"}
    saved = client.get(f"/cases/{cid}", headers=h).json()
    assert (saved["company_name"], saved["address"], saved["nib"]) == ("PT Integrity", "Seoul", None)


@pytest.mark.parametrize("current,extracted,skip", [
    ("126-81-67748", "207-81-34847", "nib_conflict"),
    ("126-81-67748", "1268167748", None),
    ("1268167748", "126 81 67748", None),
    (None, "126-81-67748", None),
])
def test_사업자번호를_숫자로_대조하고_일치하거나_비었을_때만_반영한다(client, case, fake_llm,
                                                        current, extracted, skip):
    h, cid = case
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        c.nib = current
        db.commit()
    fake_llm["nib"] = extracted
    if current is None:
        # 첫 번호의 빈칸 반영은 같은 업체의 등록증으로 검증한다.
        fake_llm["company_name"] = "PT Integrity"
    text = PLAIN_TEXT.replace("BIO-FD&C Co., Ltd.", fake_llm["company_name"])
    res = _upload(client, case, "사업자등록증.pdf",
                  f"NOMOR INDUK BERUSAHA (NIB)\nNIB: {extracted}\n{text}")
    assert res["doc_type"] == "nib_business_license" and res["mismatch"] is None
    saved = client.get(f"/cases/{cid}", headers=h).json()
    if skip:
        assert res["applied"] == {"skipped": skip}
        assert (saved["company_name"], saved["address"], saved["nib"]) == ("PT Integrity", "Seoul", current)
    elif current is None:
        assert res["applied"] == {"nib": extracted}
        assert (saved["company_name"], saved["address"], saved["nib"]) == ("PT Integrity", "Seoul", extracted)
    else:
        assert res["applied"] == fake_llm
        assert (saved["company_name"], saved["address"], saved["nib"]) == ("BIO-FD&C Co., Ltd.", "Hwasun", extracted)


@pytest.mark.parametrize("full,excerpt,verdict", [(MSDS_TEXT, "", "warn"), (None, PLAIN_TEXT, "unknown"), (None, None, "unknown")])
def test_검증_결과가_JSON에_저장되고_서류함에_노출된다(client, case, full, excerpt, verdict):
    h, cid = case
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        d = models.DocumentAsset(case_id=cid, filename="[MSDS] Oligopeptide-1.pdf", doc_type="coa_msds",
                                  fields={"material_names": ["Oligopeptide-1"], "_sources": {"material_names": "llm"}},
                                  text_full=full, text_excerpt=excerpt)
        db.add(d)
        db.commit()
        did = d.document_id
        m._verify_doc(db, d, c)
    with m.SessionLocal() as db:
        saved = db.get(models.DocumentAsset, did)
        assert saved.fields["_verify"]["verdict"] == verdict
        assert saved.fields["material_names"] == ["Oligopeptide-1"]
        assert saved.fields["_sources"] == {"material_names": "llm"}
        assert datetime.fromisoformat(saved.fields["_verify"]["at"])
    rows = client.get(f"/cases/{cid}/documents", headers=h).json()
    assert rows[0]["verify"]["verdict"] == verdict


def test_SJPH_증빙은_저장_뒤_유형을_유지하며_자동파싱한다(client, case, monkeypatch):
    h, cid = case
    queued = []

    def enqueue(document_id, keep_doc_type=False):
        with m.SessionLocal() as db:
            d = db.get(models.DocumentAsset, document_id)
            assert d is not None and d.content_b64   # 큐 투입 시점에 이미 커밋됐다
        queued.append((document_id, keep_doc_type))
        return True

    monkeypatch.setattr(m, "autoparse_enqueue", enqueue)
    r = client.post(f"/cases/{cid}/sjph-evidence", headers=h,
                    json={"item_key": "training", "filename": "Halal_Policy.pdf", "file_b64": _pdf()})
    assert r.status_code == 200, r.text
    assert len(queued) == 1 and queued[0][1] is True
    with m.SessionLocal() as db:
        d = db.get(models.DocumentAsset, queued[0][0])
        c = db.get(models.CaseApplication, cid)
        m._reprocess_doc(db, d, c, keep_doc_type=True)
        assert d.doc_type == "sjph_evidence"
        assert len(d.text_full) > len(d.text_excerpt) == 300


def test_신청기업_재추출도_전문을_저장한다(client, case):
    h, cid = case
    with m.SessionLocal() as db:
        d = models.DocumentAsset(case_id=cid, filename="사업자등록증.pdf", doc_type="nib_business_license",
                                  fields={}, content_b64=_pdf())
        db.add(d)
        db.commit()
        did = d.document_id
    r = client.post(f"/cases/{cid}/profile/autofill", headers=h)
    assert r.status_code == 200, r.text
    with m.SessionLocal() as db:
        d = db.get(models.DocumentAsset, did)
        assert len(d.text_full) > len(d.text_excerpt) == 300


@pytest.mark.parametrize("filename,raw,code", [
    ("거절파일.exe", b"executable", "UNSUPPORTED_TYPE"),
    ("위장파일.png", b"%PDF-1.4\n", "CONTENT_MISMATCH"),
])
def test_415_거절_로그에_파일명이_남는다(client, case, caplog, filename, raw, code):
    h, cid = case
    with caplog.at_level(logging.WARNING, logger="glhac"):
        r = client.post(f"/cases/{cid}/parse-file", headers=h,
                        json={"doc_type": "nib_business_license", "filename": filename,
                              "file_b64": base64.b64encode(raw).decode()})
    assert r.status_code == 415, r.text
    assert r.json()["detail"]["code"] == code
    assert any(filename in rec.getMessage() and code in rec.getMessage() for rec in caplog.records)


def test_검증_상태는_인증이_필요하다(client, case):
    h, _ = case
    assert client.get("/system/doc-verify").status_code == 401
    r = client.get("/system/doc-verify", headers=h)
    assert r.status_code == 200, r.text
    assert set(r.json()) >= {"enabled", "pending", "queued", "done", "failed"}
    assert r.json()["enabled"] is True


def test_재검증은_권한을_확인하고_원본_있는_문서만_투입한다(client, case, monkeypatch):
    h, cid = case
    with m.SessionLocal() as db:
        docs = [models.DocumentAsset(case_id=cid, filename="scan.pdf", content_b64=raw)
                for raw in (_pdf(), _pdf(), None)]
        db.add_all(docs)
        db.commit()
        ids = [d.document_id for d in docs[:2]]
    queued = []

    def enqueue(did):
        queued.append(did)
        return did == ids[0]   # 이미 대기 중인 문서는 투입 건수에서 제외

    monkeypatch.setattr(m, "verify_enqueue", enqueue)
    assert client.post(f"/cases/{cid}/documents/verify", headers=h).status_code == 403
    assert queued == []
    r = client.post(f"/cases/{cid}/documents/verify", headers=_tok(client, "operator1"))
    assert r.status_code == 200, r.text
    assert r.json() == {"found": 2, "queued": 1}
    assert set(queued) == set(ids)
    with m.SessionLocal() as db:
        audit = db.query(models.AuditLog).filter_by(case_id=cid, action="documents.verify").one()
        assert audit.meta["found"] == 2 and audit.meta["queued"] == 1


def test_검증_큐는_중복과_포화를_막고_비활성을_존중한다(monkeypatch, caplog):
    _drain()
    with monkeypatch.context() as patch:
        patch.setattr(m, "_verify_q", queue.Queue(maxsize=1))
        patch.setattr(m, "_verify_inflight", set())
        patch.setattr(m, "_verify_state", {"queued": 0, "done": 0, "failed": 0})
        patch.setattr(m, "_DOC_VERIFY", True)
        assert m.verify_enqueue("doc-a") is True
        assert m.verify_enqueue("doc-a") is False
        with caplog.at_level(logging.WARNING, logger="glhac"):
            assert m.verify_enqueue("doc-b") is False
        assert "doc-b" in caplog.text
        assert m._verify_inflight == {"doc-a"}
        assert m._verify_state["queued"] == 1
        patch.setattr(m, "_DOC_VERIFY", False)
        assert m.verify_enqueue("doc-c") is False


def test_검증_실패_후에도_다음_문서를_처리한다(client, case, monkeypatch):
    _, cid = case
    with m.SessionLocal() as db:
        docs = [models.DocumentAsset(case_id=cid, filename="scan.pdf", doc_type="other", text_full=PLAIN_TEXT)
                for _ in range(2)]
        db.add_all(docs)
        db.commit()
        ids = [d.document_id for d in docs]
    original = m._verify_doc
    sessions = []

    def flaky(db, d, c):
        sessions.append(db)
        if d.document_id == ids[0]:
            raise RuntimeError("의도된 검증 실패")
        return original(db, d, c)

    monkeypatch.setattr(m, "_verify_doc", flaky)
    failed, done = m._verify_state["failed"], m._verify_state["done"]
    assert all(m.verify_enqueue(did) for did in ids)
    _drain()
    assert m._verify_state["failed"] == failed + 1
    assert m._verify_state["done"] == done + 1
    assert not m._verify_inflight
    assert len(sessions) == 2 and sessions[0] is not sessions[1]


def test_자동파싱_성공_후_검증까지_이어진다(client, case):
    h, cid = case
    r = client.post(f"/cases/{cid}/documents", headers=h,
                    json={"filename": "Halal_Policy.pdf", "file_b64": _pdf()})
    assert r.status_code == 200, r.text
    assert r.json()["autoparse_queued"] is True
    _drain()
    with m.SessionLocal() as db:
        d = db.get(models.DocumentAsset, r.json()["document_id"])
        assert len(d.text_full) > len(d.text_excerpt) == 300
        assert d.fields["_verify"]["verdict"] == "warn"


def test_교차검증_재추출은_파일을_한번만_읽는다(client, case, monkeypatch):
    original = intake.parse_file
    calls = []

    def counted(filename, data, **kw):
        calls.append(filename)
        return original(filename, data, **kw)

    monkeypatch.setattr(intake, "parse_file", counted)
    res = _upload(client, case, "[MSDS] Oligopeptide-1.pdf", MSDS_TEXT)
    assert res["mismatch"]["detected"] == "coa_msds"
    assert res["extracted"]["material_names"] == ["Oligopeptide-1"]
    assert calls == ["[MSDS] Oligopeptide-1.pdf"]


def test_검증_중_갱신된_필드를_보존하고_LLM_대기에는_트랜잭션이_없다(client, case, monkeypatch):
    _, cid = case
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        d = models.DocumentAsset(case_id=cid, filename="scan.pdf", doc_type="other",
                                  text_full=PLAIN_TEXT, fields={"company_name": "이전값", "removed": "삭제 전"})
        db.add(d)
        db.commit()
        did = d.document_id
        latest = {"company_name": "사람이 수정한 값", "added": ["새 항목"],
                  "_sources": {"company_name": "manual"}}

        def verify(doc_type, filename, text, fields, company):
            assert not db.in_transaction()   # 대기 중 쓰기 요청이 같은 트랜잭션에 묶이지 않는다
            assert (doc_type, filename, text, company) == ("other", "scan.pdf", PLAIN_TEXT, "PT Integrity")
            assert fields["company_name"] == "이전값"
            with m.SessionLocal() as other:
                doc = other.get(models.DocumentAsset, did)
                doc.fields = dict(latest)
                other.commit()
            return {"verdict": "ok", "summary": "대조 완료"}

        monkeypatch.setattr(intake, "verify_extraction", verify)
        m._verify_doc(db, d, c)
    with m.SessionLocal() as db:
        fields = db.get(models.DocumentAsset, did).fields
        assert fields["_verify"]["verdict"] == "ok"
        assert {k: v for k, v in fields.items() if k != "_verify"} == latest


@pytest.mark.parametrize("route", ["manual", "pending", "autoparse", "profile"])
def test_재추출_커밋_후_검증을_정확히_한번_투입한다(client, case, monkeypatch, route):
    h, cid = case
    queued = []

    def enqueue(did):
        with m.SessionLocal() as db:
            d = db.get(models.DocumentAsset, did)
            assert d.text_full and len(d.text_full) > 300   # 다른 세션에서 커밋을 확인한다
            assert d.fields["company_name"] == "BIO-FD&C Co., Ltd."
        queued.append(did)
        return True

    monkeypatch.setattr(m, "verify_enqueue", enqueue)
    if route == "autoparse":
        r = client.post(f"/cases/{cid}/documents", headers=h,
                        json={"filename": "사업자등록증.pdf", "file_b64": _pdf()})
        assert r.status_code == 200, r.text
        did = r.json()["document_id"]
    else:
        with m.SessionLocal() as db:
            d = models.DocumentAsset(case_id=cid, filename="사업자등록증.pdf", doc_type="nib_business_license",
                                      content_b64=_pdf(), fields={}, confidence=None)
            db.add(d)
            db.commit()
            did = d.document_id
        if route == "profile":
            r = client.post(f"/cases/{cid}/profile/autofill", headers=h)
        else:
            path = (f"/documents/{did}/reprocess" if route == "manual"
                    else f"/cases/{cid}/documents/reprocess-pending")
            r = client.post(path, headers=_tok(client, "operator1"))
        assert r.status_code == 200, r.text
    _drain()
    assert queued == [did]


@pytest.mark.parametrize("full", [None, "", "   "])
def test_전문이_없으면_발췌로_GPT를_부르지_않는다(client, case, monkeypatch, full):
    _, cid = case

    def unexpected(*args, **kw):
        pytest.fail("전문 없는 문서를 LLM으로 검증했다")

    monkeypatch.setattr(intake.ai_local, "llm_json", unexpected)
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        d = models.DocumentAsset(case_id=cid, filename="scan.pdf", doc_type="other",
                                  fields={"company_name": "기업명"}, text_full=full, text_excerpt=PLAIN_TEXT[:300])
        db.add(d)
        db.commit()
        m._verify_doc(db, d, c)
        assert d.fields["_verify"]["verdict"] == "unknown"
        assert d.fields["_verify"]["summary"] == "전문 없음 — 재처리 필요"


@pytest.mark.parametrize("llm_verdict,expected", [("ok", "warn"), ("fail", "fail"), (None, "warn")])
def test_원문에_없는_값은_규칙과_GPT_지적을_합쳐_최소_warn이다(monkeypatch, llm_verdict, expected):
    text = "Ac me\nCO WATER " + "본문 " * 3000 + "Tail Material"
    fields = {"company_name": "ACME CO", "material_names": ["Water", "가짜원료", "없는성분"],
              "address": "가짜주소", "short": "ab", "count": 20, "tail": "Tail Material",
              "_internal": "검증에서 제외"}
    calls = []

    def verify(system, user):
        calls.append(user)
        if llm_verdict is None:
            return {"error": "LLM_UNAVAILABLE"}
        return {"verdict": llm_verdict, "summary": "모델 판정",
                "field_issues": [{"field": "address", "issue": "원문에 없는 값"},
                                 {"field": "company_name", "issue": "업체 대조 필요"}]}

    monkeypatch.setattr(intake.ai_local, "llm_json", verify)
    res = intake.verify_extraction("other", "scan.pdf", text, fields)
    assert res["verdict"] == expected
    issues = [{"field": "material_names", "issue": "원문에 없는 값"},
              {"field": "address", "issue": "원문에 없는 값"},
              {"field": "short", "issue": "원문에 없는 값"},
              {"field": "count", "issue": "원문에 없는 값"},
              {"field": "document", "code": "partial_verification",
               "issue": "검증 입력 한도 초과 — 전체 원문·추출값의 담당자 검토 필요"}]
    if llm_verdict is not None:
        issues.append({"field": "company_name", "issue": "업체 대조 필요"})
    assert res["field_issues"] == issues
    assert len(calls) == 1 and calls[0].split("원문:\n", 1)[1] == text[:8000]


@pytest.mark.parametrize("company,address", [("직접 입력한 회사", "직접 입력한 주소"),
                                            ("직접 입력한 회사", ""), ("My Company", ""),
                                            ("PT bio fd&c Tbk", "")])
def test_케이스_사업자번호가_비어도_사용자_입력은_보존한다(client, case, fake_llm, company, address):
    h, cid = case
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        c.company_name, c.address, c.nib = company, address, None
        db.commit()
    fake_llm["nib"] = "597-19-01669"
    res = _upload(client, case, "사업자등록증.pdf",
                  f"NOMOR INDUK BERUSAHA (NIB)\nNIB: {fake_llm['nib']}\n{PLAIN_TEXT}")
    saved = client.get(f"/cases/{cid}", headers=h).json()
    assert saved["company_name"] == company
    if company == "PT bio fd&c Tbk":
        assert saved["nib"] == res["applied"]["nib"] == "597-19-01669"
        assert saved["address"] == fake_llm["address"]
    else:
        assert saved["nib"] is None and "nib" not in res["applied"]
        assert saved["address"] == address
        pending = client.get(f"/cases/{cid}/pending-extractions", headers=h).json()["conflicts"]
        # 기존 선택 API는 필드별 후보를 반환한다. 이 등록증의 NIB 후보는 한 건이다.
        candidates = [x for x in pending if x["field"] == "nib"]
        assert len(candidates) == 1 and candidates[0]["proposed"] == "597-19-01669"
        assert candidates[0]["filename"] == "사업자등록증.pdf"
        problems = client.get(f"/cases/{cid}/problem-documents", headers=h).json()
        assert "OWNERSHIP_MISMATCH" in {r["code"] for d in problems["documents"] for r in d["reasons"]}


@pytest.mark.parametrize("status", sorted(m.sm.TERMINAL_STATES))
def test_종결_케이스는_재검증을_거부한다(client, case, monkeypatch, status):
    _, cid = case
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        c.status = status
        db.add(models.DocumentAsset(case_id=cid, filename="scan.pdf", content_b64=_pdf()))
        db.commit()

    def unexpected(*args):
        pytest.fail("종결 케이스를 검증 큐에 넣었다")

    monkeypatch.setattr(m, "verify_enqueue", unexpected)
    r = client.post(f"/cases/{cid}/documents/verify", headers=_tok(client, "operator1"))
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "CASE_CLOSED"


def test_재처리_집계는_기존_사업자번호를_다른_번호로_덮지_않는다():
    """다른 회사(제조사) 등록증을 재처리해도 신청 업체 번호는 그대로다(2026-11-11 복구 중 발견)."""
    db = m.SessionLocal()
    try:
        c = models.CaseApplication(case_id="nibguard01", org_id="org_demo", company_name="신청업체", nib="597-19-01669")
        db.add(c); db.flush()
        doc = models.DocumentAsset(case_id=c.case_id, filename="사업자등록증.pdf", doc_type="nib_business_license",
                                   fields={"company_name": "신청업체", "nib": "123-45-67890"},
                                   text_full="사업자등록증\n업체: 신청업체\n사업자번호: 123 45 67890")
        db.add(doc); db.flush()
        ap = m._apply_agg_to_case(db, c, {"nib": "123-45-67890", "products": [], "materials": [], "pending": []},
                                documents=[doc])
        assert c.nib == "597-19-01669" and ap.get("nib_conflict") is True and not ap.get("nib_set")
        c.nib = None
        ap = m._apply_agg_to_case(db, c, {"nib": "123-45-67890", "products": [], "materials": [], "pending": []},
                                documents=[doc])
        assert c.nib == "123-45-67890" and ap.get("nib_set") is True
    finally:
        db.rollback(); db.close()


def test_검증_프롬프트는_공급사_서류의_회사명_차이를_문제로_보지_않는다():
    assert "coa_msds" in intake._VERIFY_SYS and "정상" in intake._VERIFY_SYS


def test_재검증_대상_조회는_본문_없이_ID만_읽는다(client, case, monkeypatch):
    from sqlalchemy import event
    _, cid = case
    with m.SessionLocal() as db:
        db.add(models.DocumentAsset(case_id=cid, filename="scan.pdf", content_b64=_pdf()))
        db.commit()
    monkeypatch.setattr(m, "verify_enqueue", lambda did: True)
    queries = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        sql = " ".join(statement.lower().split())
        if sql.startswith("select") and "from document_asset" in sql:
            queries.append(sql)

    event.listen(m.engine, "before_cursor_execute", capture)
    try:
        r = client.post(f"/cases/{cid}/documents/verify", headers=_tok(client, "operator1"))
    finally:
        event.remove(m.engine, "before_cursor_execute", capture)
    assert r.status_code == 200, r.text
    assert queries
    for sql in queries:
        columns = sql.split(" from ", 1)[0]
        assert "document_id" in columns
        assert "content_b64" not in columns and "text_full" not in columns
        assert "content_b64 is not null" in sql


@pytest.mark.parametrize("kind,model,bucket", [
    ("material_names", models.Material, "materials"), ("product_names", models.Product, "products"),
])
def test_이름수정_제외는_재조회와_같은값_재추출에도_대기로_돌아오지_않는다(client, case, kind, model, bucket):
    h, cid = case
    reviewer = _tok(client, "auditor1")
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        c.profile_ext = {"sector": "food", "pending_handled": ["old%d" % i for i in range(2000)]}
        db.add(models.DocumentAsset(case_id=cid, filename="확인.pdf", doc_type="coa_msds",
                                   fields={kind: ["Raw Water", "Discard Me", "Keep Me", "Not Selected"],
                                           "_sources": {kind: "llm"}}))
        db.commit()
    body = {"values": ["Keep Me"], "renames": {"Raw Water": "확인한 이름"}, "dismiss": ["Discard Me"]}
    r = client.post(f"/cases/{cid}/pending-extractions/apply", headers=reviewer, json={**body, "reason": "원문 대조 후 OCR 교정"})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 3 and r.json()["applied"][bucket] == 2
    with m.SessionLocal() as db:
        assert {x.name for x in db.query(model).filter_by(case_id=cid)} == {"확인한 이름", "Keep Me"}
        pe = db.get(models.CaseApplication, cid).profile_ext
        assert pe["sector"] == "food" and len(pe["pending_handled"]) == 2000
        assert set(pe["pending_handled"]) >= {"rawwater", "discardme", "keepme"}
        audit = db.query(models.AuditLog).filter_by(case_id=cid, action="extraction.apply").one()
        assert audit.meta["renames"] == body["renames"] and audit.meta["dismiss"] == body["dismiss"]
        db.add(models.DocumentAsset(case_id=cid, filename="재추출.pdf", doc_type="coa_msds",
                                   fields={kind: ["RAW   WATER", "discardme"], "_sources": {kind: "llm"}}))
        db.commit()
    for _ in range(2):
        pending = client.get(f"/cases/{cid}/pending-extractions", headers=h).json()["items"]
        assert [x["value"] for x in pending] == ["Not Selected"]
    r = client.post(f"/cases/{cid}/pending-extractions/apply", headers=reviewer, json={**body, "reason": "원문 대조 후 OCR 교정"})
    assert r.json()["count"] == r.json()["applied"][bucket] == 0


@pytest.mark.parametrize("body,expected", [
    ({"values": [], "renames": {"Old Name": "정 제 수"}, "dismiss": []}, {"Not Selected"}),
    ({"dismiss": ["Old Name"]}, {"Not Selected"}),
    ({"values": [], "renames": {}, "dismiss": []}, {"Old Name", "Not Selected"}),
])
def test_수정과_제외는_미선택값을_등록하지_않고_수정이름의_중복도_막는다(client, case, body, expected):
    h, cid = case
    reviewer = _tok(client, "auditor1")
    with m.SessionLocal() as db:
        db.add(models.Material(case_id=cid, name="정제수"))
        db.add(models.DocumentAsset(case_id=cid, filename="확인.pdf", doc_type="material_list",
                                   fields={"material_names": ["Old Name", "Not Selected"]}))
        db.commit()
    r = client.post(f"/cases/{cid}/pending-extractions/apply", headers=reviewer, json={**body, "reason": "원문 대조 후 OCR 교정"})
    assert r.status_code == 200, r.text
    assert r.json()["applied"]["materials"] == 0
    with m.SessionLocal() as db:
        assert [x.name for x in db.query(models.Material).filter_by(case_id=cid)] == ["정제수"]
    pending = {x["value"] for x in client.get(f"/cases/{cid}/pending-extractions", headers=h).json()["items"]}
    assert pending == expected


@pytest.mark.parametrize("body", [None, {}, {"values": []}])
def test_기존_values_생략과_빈목록의_전량반영을_유지한다(client, case, body):
    h, cid = case
    with m.SessionLocal() as db:
        db.add(models.DocumentAsset(case_id=cid, filename="확인.pdf", doc_type="material_list",
                                   fields={"material_names": ["Water", "Salt"]}))
        db.commit()
    r = client.post(f"/cases/{cid}/pending-extractions/apply", headers=h, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["applied"]["materials"] == 2


def test_원재료는_공백_대소문자_정규화_중복시_기존ID와_409(client, case):
    h, cid = case
    r = client.post(f"/cases/{cid}/materials", headers=h, json={"name": "Raw Water"})
    assert r.status_code == 200, r.text
    mid = r.json()["material_id"]
    r = client.post(f"/cases/{cid}/materials", headers=h, json={"name": "rawWATER"})
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == {"code": "DUPLICATE_MATERIAL", "material_id": mid}
    with m.SessionLocal() as db:
        assert db.query(models.Material).filter_by(case_id=cid).count() == 1
    # 중복 제한은 같은 케이스 안에서만 적용한다.
    other = client.post("/cases", headers=h, json={"company_name": "다른 신청서"}).json()["case_id"]
    assert client.post(f"/cases/{other}/materials", headers=h, json={"name": "Raw Water"}).status_code == 200


@pytest.mark.parametrize("status,frozen,code", [
    ("withdrawn", False, "CASE_CLOSED"), ("rejected", False, "CASE_CLOSED"),
    ("revoked", False, "CASE_CLOSED"), ("cancelled", False, "CASE_CLOSED"),
    ("onboarding", True, "SCOPE_FROZEN"),
])
def test_잠긴_케이스는_확인대기_반영과_파일파싱을_거부한다(client, case, monkeypatch, status, frozen, code):
    h, cid = case
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        c.status, c.scope_frozen = status, frozen
        db.add(models.DocumentAsset(case_id=cid, filename="확인.pdf", doc_type="material_list",
                                   fields={"material_names": ["Water"]}))
        db.commit()
    monkeypatch.setattr(intake, "parse_typed", lambda *a: pytest.fail("잠긴 케이스의 파일을 파싱했다"))
    for path, body in (("pending-extractions/apply", {"values": ["Water"]}),
                       ("parse-file", {"doc_type": "material_list", "filename": "확인.pdf", "file_b64": _pdf()})):
        r = client.post(f"/cases/{cid}/{path}", headers=h, json=body)
        assert r.status_code == 409 and r.json()["detail"]["code"] == code, r.text
    with m.SessionLocal() as db:
        assert db.query(models.Material).filter_by(case_id=cid).count() == 0
        assert db.query(models.DocumentAsset).filter_by(case_id=cid).count() == 1
        assert not (db.get(models.CaseApplication, cid).profile_ext or {}).get("pending_handled")


@pytest.mark.parametrize("route", ["parse-file", "documents"])
def test_제품목록은_구조파서가_없어도_업로드로_제품을_즉시_등록한다(client, case, monkeypatch, route):
    h, cid = case
    with m.SessionLocal() as db:
        assert "NO_PRODUCT" in {g["code"] for g in m.sm.guard_intake_complete(db, db.get(models.CaseApplication, cid))}
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda *a, **kw: {"product_names": ["Face Cream"]})
    r = client.post(f"/cases/{cid}/{route}", headers=h,
                    json={"doc_type": "product_list", "filename": "제품목록.pdf", "file_b64": _pdf()})
    assert r.status_code == 200, r.text
    _drain()
    if route == "parse-file":
        assert r.json()["applied"]["products_added"] == 1 and r.json()["applied"]["products_pending"] == 0
    with m.SessionLocal() as db:
        assert db.query(models.Product).filter_by(case_id=cid).one().name == "Face Cream"
        d = db.query(models.DocumentAsset).filter_by(case_id=cid).one()
        assert d.doc_type == "product_list" and d.fields["_sources"]["product_names"] == "llm"
        assert "NO_PRODUCT" not in {g["code"] for g in m.sm.guard_intake_complete(db, db.get(models.CaseApplication, cid))}
    assert client.get(f"/cases/{cid}/pending-extractions", headers=h).json()["items"] == []
    assert client.get(f"/cases/{cid}/package-check", headers=h).json()["items"]["product"]["ok"] is True


def test_재처리_대상은_SQL로_ID만_조회하고_지정유형을_보존한다(client, case, monkeypatch):
    from sqlalchemy import event
    _, cid = case
    with m.SessionLocal() as db:
        docs = [models.DocumentAsset(case_id=cid, filename="scan.pdf", doc_type="product_label",
                                    content_b64=raw, confidence=conf, fields=fields, text_full=PLAIN_TEXT)
                for raw, conf, fields in [("raw", None, {}), ("raw", 0, {"_llm_error": "TIMEOUT"}),
                                          ("raw", 0.8, {}), ("", None, {}), (None, None, {}),
                                          ("raw", 0.8, {"_llm_error": ""}), ("raw", 0.8, {"_llm_error": None})]]
        db.add_all(docs)
        db.commit()
        expected = {d.document_id for d in docs[:2]}
    queued, queries = [], []
    monkeypatch.setattr(m, "autoparse_enqueue", lambda did, keep_doc_type=False: queued.append((did, keep_doc_type)) or True)

    def capture(conn, cursor, statement, parameters, context, executemany):
        sql = " ".join(statement.lower().split())
        if sql.startswith("select") and "from document_asset" in sql:
            queries.append(sql)

    event.listen(m.engine, "before_cursor_execute", capture)
    try:
        r = client.post(f"/cases/{cid}/documents/reprocess-pending", headers=_tok(client, "operator1"))
    finally:
        event.remove(m.engine, "before_cursor_execute", capture)
    assert r.status_code == 200, r.text
    assert r.json()["found"] == r.json()["queued"] == 2
    assert {did for did, keep in queued} == expected and all(keep for did, keep in queued)
    assert queries
    for sql in queries:
        columns = sql.split(" from ", 1)[0]
        assert "document_id" in columns
        assert all(key not in columns for key in ("content_b64", "text_full", "text_excerpt", "fields"))
        assert "confidence is null" in sql and "json_extract" in sql and "content_b64 is not null" in sql
