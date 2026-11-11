"""서류 업로드 무결성 — 잘못 고른 칸·공급사 정보로 신청 업체를 덮지 않는다."""
import base64
import logging
import os
import queue
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_doc_intake_integrity_test.db")

import fitz                                # noqa: E402
import pytest                              # noqa: E402
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
    return h, cid


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
    assert res["applied"]["materials_added"] == 1
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
        assert db.query(models.Material).filter_by(case_id=cid, name="Oligopeptide-1").count() == 1


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
    res = _upload(client, case, "사업자등록증.pdf")
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
              {"field": "address", "issue": "원문에 없는 값"}]
    if llm_verdict is not None:
        issues.append({"field": "company_name", "issue": "업체 대조 필요"})
    assert res["field_issues"] == issues
    assert len(calls) == 1 and calls[0].split("원문:\n", 1)[1] == text[:8000]


@pytest.mark.parametrize("company,address", [("직접 입력한 회사", "직접 입력한 주소"),
                                            ("직접 입력한 회사", ""), ("My Company", "")])
def test_케이스_사업자번호가_비어도_사용자_입력은_보존한다(client, case, fake_llm, company, address):
    h, cid = case
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        c.company_name, c.address, c.nib = company, address, None
        db.commit()
    fake_llm["nib"] = "597-19-01669"
    res = _upload(client, case, "사업자등록증.pdf")
    saved = client.get(f"/cases/{cid}", headers=h).json()
    assert saved["nib"] == res["applied"]["nib"] == "597-19-01669"
    assert saved["company_name"] == (fake_llm["company_name"] if company == "My Company" else company)
    assert saved["address"] == (address or fake_llm["address"])


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
