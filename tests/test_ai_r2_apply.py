"""AI R2 반영·저장 회귀: 감사 재현 입력을 외부 호출 없이 실행한다."""
import base64
import copy
import io
import os
import random
import subprocess
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor

import fitz
import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.main as m
from app import intake, models


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(m, "_RL_WRITE_HITS", {})
    monkeypatch.setattr(m, "autoparse_enqueue", lambda *a, **k: False)
    monkeypatch.setattr(m, "verify_enqueue", lambda *a, **k: False)
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda *a, **k: {})
    monkeypatch.setattr(intake, "_ZIP_LOOKUP", False)


@pytest.fixture
def client():
    with TestClient(m.app) as client:
        yield client


@pytest.fixture
def case(client):
    login = client.post("/auth/login", json={"username": "applicant1", "password": "pw"})
    assert login.status_code == 200, login.text
    headers = {"Authorization": "Bearer " + login.json()["token"]}
    r = client.post("/cases", headers=headers, json={"company_name": "PT MAJU HALAL SEJAHTERA"})
    assert r.status_code == 200, r.text
    return r.json()["case_id"], headers


def pdf(content="QUALITY CERTIFICATE\nNumber Q12345\nA readable original document.", size=None):
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), content)
        raw = doc.tobytes()
    if size:
        raw += b"\n%" + b"x" * (size - len(raw) - 2)
    return raw


def zip_body(filename, raw):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(filename, raw)
    return {"zip_b64": base64.b64encode(buf.getvalue()).decode()}


@pytest.mark.parametrize("name", ["pt maju halal sejahtera tbk", "CV Maju-Halal Sejahtera",
                                  "Maju Halal Sejahtera Co., Ltd.", "MAJU HALAL SEJAHTERA Inc.",
                                  "(주)Maju Halal Sejahtera", "주식회사 Maju Halal Sejahtera",
                                  "㈜Maju Halal Sejahtera", "P.T. Maju Halal Sejahtera"])
def test_company_variants_apply_immediately(case, name):
    cid, _ = case
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        doc = models.DocumentAsset(case_id=cid, filename="NIB.pdf", doc_type="nib_business_license",
            fields={"company_name": name, "nib": "9120001234567", "address": "Jakarta"},
            text_full=f"NOMOR INDUK BERUSAHA\n{name}\nNIB 9120001234567\nJakarta")
        db.add(doc)
        db.flush()
        applied = m._apply_owned_autofill(db, c, doc)
        assert applied["nib"] == c.nib == "9120001234567"
        assert c.address == "Jakarta"
        assert not [x for x in m._pending_autofill(db, cid) if x["field"] == "nib"]


@pytest.mark.parametrize("route", ["parse-file", "intake-zip"])
def test_first_foreign_nib_is_pending_until_selected(client, case, monkeypatch, route):
    cid, headers = case
    company = "PT SUPPLIER LAIN"
    fields = {"company_name": company, "nib": "9120001234567", "address": "Jakarta"}
    content = f"NOMOR INDUK BERUSAHA (NIB)\n{company}\nNIB 9120001234567\nJakarta"
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda *a, **k: {
        "doc_type": "nib_business_license", "confidence": .9, "fields": fields, **fields})
    raw = pdf(content)
    body = (zip_body("NIB.pdf", raw) if route == "intake-zip" else {
        "doc_type": "nib_business_license", "filename": "NIB.pdf", "file_b64": base64.b64encode(raw).decode()})
    response = client.post(f"/cases/{cid}/{route}", headers=headers, json=body)
    assert response.status_code == 200, response.text
    saved = client.get(f"/cases/{cid}", headers=headers).json()
    assert saved["nib"] is None and not saved["address"]
    assert saved["company_name"] == "PT MAJU HALAL SEJAHTERA"
    pending = client.get(f"/cases/{cid}/pending-extractions", headers=headers).json()["conflicts"]
    nibs = [x for x in pending if x["field"] == "nib"]
    assert len(nibs) == 1 and nibs[0]["proposed"] == fields["nib"]
    assert {x["field"] for x in pending} >= {"nib", "company_name", "address"}
    assert len({x["document_id"] for x in pending}) == 1
    assert {x["filename"] for x in pending} == {"NIB.pdf"}
    problems = client.get(f"/cases/{cid}/problem-documents", headers=headers).json()
    assert "OWNERSHIP_MISMATCH" in {r["code"] for d in problems["documents"] for r in d["reasons"]}
    response = client.post(f"/cases/{cid}/pending-extractions/apply", headers=headers, json={
        "autofill_choices": [{"conflict_id": x["conflict_id"], "value": x["proposed"]} for x in pending]})
    assert response.status_code == 200, response.text
    saved = client.get(f"/cases/{cid}", headers=headers).json()
    assert (saved["nib"], saved["company_name"], saved["address"]) == (fields["nib"], company, "Jakarta")
    assert client.get(f"/cases/{cid}/pending-extractions", headers=headers).json()["conflicts"] == []


def test_empty_case_nib_still_requires_company_match_with_org_reference(case):
    cid, _ = case
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        org = db.get(models.Org, c.org_id)
        if org is None:
            org = models.Org(org_id=c.org_id, name=c.company_name)
            db.add(org)
        org.profile_ext = {"nib": "9120001234567"}
        db.flush()
        doc = models.DocumentAsset(case_id=cid, filename="foreign.pdf", doc_type="nib_business_license",
            fields={"company_name": "PT SUPPLIER LAIN", "nib": "9120001234567", "address": "Jakarta"},
            text_full="NOMOR INDUK BERUSAHA\nPT SUPPLIER LAIN\nNIB 9120001234567\nJakarta")
        db.add(doc)
        db.flush()
        assert m._apply_owned_autofill(db, c, doc)["skipped"] == "company_conflict"
        assert c.nib is None and not c.address


# 감사 파일 Daftar Bahan.xlsx의 50개 표기 = 기존 정규화 기준 47종.
MATERIAL_NAMES = ["Tepung terigu", "Gula pasir", "Lemak babi", "Gelatin sapi", "Minyak babi", "Angciu",
    "Arak masak", "Rhum", "Mirin", "Kuas bulu babi", "Etanol", "Alkohol", "Lard", "L a r d", "Lardd",
    "Pork fat", "Porc", "Gelatine", "Gélatine", "E120", "E-120", "E 120", "E471", "Karmin", "Kochineal",
    "Darah", "Babi", "B4bi", "Wine vinegar", "Cuka anggur", "Shortening", "Emulsifier (E471)", "Rum flavour",
    "Bacon flavour", "Ham", "Hamburger", "Champagne", "Sake", "Tuak", "Brem", "Khamr", "Collagen", "Kolagen",
    "Plasma", "L-cysteine", "E920", "Whey", "Rennet", "Pepsin", "Lipase"]


def test_six_concurrent_material_tables_have_47_rows(client, case, monkeypatch):
    cid, headers = case
    wb = Workbook()
    ws = wb.active
    ws.title = "Daftar Bahan"
    ws.append(["Daftar Bahan Halal PT MAJU"])
    ws.append(["No", "Nama Bahan", "Jenis Bahan", "Produsen", "Negara", "No. Sertifikat Halal"])
    for i, name in enumerate(MATERIAL_NAMES, 1):
        ws.append([i, name, "Bahan", "PT X", "ID", ""])
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    encoded = base64.b64encode(buf.getvalue()).decode()
    # 여섯 요청 모두 기존 행이 없다고 읽은 다음 삽입 경합을 일으킨다.
    barrier = threading.Barrier(6, timeout=20)
    upsert = m._upsert_material
    def racing_upsert(db, material):
        if material.name == MATERIAL_NAMES[0]:
            barrier.wait()
        return upsert(db, material)
    monkeypatch.setattr(m, "_upsert_material", racing_upsert)
    def upload(i):
        return client.post(f"/cases/{cid}/parse-file", headers=headers, json={
            "filename": f"Daftar Bahan {i}.xlsx", "doc_type": "material_list", "file_b64": encoded})
    with ThreadPoolExecutor(6) as pool:
        responses = list(pool.map(upload, range(6)))
    assert [r.status_code for r in responses] == [200] * 6, [r.text for r in responses]
    assert sum(r.json()["applied"]["materials_added"] for r in responses) == 47
    with m.SessionLocal() as db:
        rows = db.query(models.Material).filter_by(case_id=cid).all()
        assert len(rows) == len({r.normalized_name for r in rows}) == 47
        assert {r.normalized_name for r in rows} == {m._norm_material(n) for n in MATERIAL_NAMES}


@pytest.mark.parametrize("legacy_has_key", [False, True])
def test_existing_material_duplicates_merge_before_index(tmp_path, monkeypatch, legacy_has_key):
    engine = create_engine("sqlite:///" + str(tmp_path / "legacy.db"))
    models.Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        if not legacy_has_key:
            conn.exec_driver_sql("DROP INDEX uq_material_case_name")
            conn.exec_driver_sql("ALTER TABLE material DROP COLUMN normalized_name")
        conn.execute(models.CaseApplication.__table__.insert().values(case_id="legacy", org_id="legacy"))
        conn.execute(text("INSERT INTO material (material_id,case_id,name,screen_result,note) VALUES "
                          "('a','legacy','Water','CLEARED','original note'),"
                          "('b','legacy','WATER','BLOCK','unsafe note')"))
    with Session(engine) as db:
        db.add(models.Product(product_id="p", case_id="legacy", name="Product"))
        db.add(models.DocumentAsset(document_id="d", case_id="legacy", filename="coa.pdf", material_id="a"))
        db.flush()
        db.add_all([models.ProductMaterial(case_id="legacy", product_id="p", material_id=mid) for mid in ("a", "b")])
        db.add(models.MaterialMeasurement(case_id="legacy", material_id="a", document_id="d", param_key="pork_dna", verdict="fail"))
        db.add(models.MaterialEvidenceLink(case_id="legacy", material_id="a", document_id="d", link_type="DIRECT"))
        db.add(models.HalalCertificate(case_id="legacy", frozen_material_ids=["a", "b"]))
        db.commit()
    monkeypatch.setattr(m, "engine", engine)
    m._migrate()
    m._ensure_indexes()
    m._ensure_indexes()  # 재기동 멱등성
    with Session(engine) as db:
        material = db.query(models.Material).one()
        assert (material.material_id, material.normalized_name, material.screen_result) == ("b", "water", "BLOCK")
        assert "original note" in material.note and "unsafe note" in material.note
        assert db.query(models.ProductMaterial).one().material_id == "b"
        assert db.get(models.DocumentAsset, "d").material_id == "b"
        assert db.query(models.MaterialMeasurement).one().material_id == "b"
        assert db.query(models.MaterialEvidenceLink).one().material_id == "b"
        assert db.query(models.HalalCertificate).one().frozen_material_ids == ["b"]
        db.add(models.Material(case_id="legacy", name="Wa ter"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        assert db.execute(text("PRAGMA foreign_key_check")).all() == []
    assert any(i["name"] == "uq_material_case_name" and i["unique"] for i in inspect(engine).get_indexes("material"))
    engine.dispose()


def test_material_key_changes_with_rename_and_upsert_keeps_evidence(case):
    cid, _ = case
    with m.SessionLocal() as db:
        first, created = m._upsert_material(db, models.Material(case_id=cid, name="Sugar", note="human input", evidence_provided=True))
        assert created
        first.name = "White Sugar"
        db.commit()
        assert first.normalized_name == "whitesugar"
        same, created = m._upsert_material(db, models.Material(case_id=cid, name="WHITE-SUGAR"))
        assert not created and same.material_id == first.material_id
        assert same.note == "human input" and same.evidence_provided
        db.commit()


def test_material_rename_collision_returns_conflict(client, case):
    cid, headers = case
    ids = [client.post(f"/cases/{cid}/materials", headers=headers, json={"name": name}).json()["material_id"]
           for name in ("Water", "Sugar")]
    response = client.patch(f"/materials/{ids[1]}/rename", headers=headers, json={"name": "WA TER"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["material_id"] == ids[0]
    with m.SessionLocal() as db:
        assert db.get(models.Material, ids[1]).name == "Sugar"


@pytest.mark.parametrize("route", ["evidence", "photo", "sjph", "documents"])
def test_large_original_is_kept_by_other_document_uploads(client, case, route):
    cid, headers = case
    raw = pdf(size=3_603_965)
    body = {"filename": "large.pdf", "file_b64": base64.b64encode(raw).decode()}
    path = f"/cases/{cid}/documents"
    if route == "evidence":
        mid = client.post(f"/cases/{cid}/materials", headers=headers, json={"name": "Water"}).json()["material_id"]
        path = f"/cases/{cid}/materials/{mid}/evidence"
        body.update(evidence_type="coa", counts_as_evidence=False)
    elif route == "photo":
        pid = client.post(f"/cases/{cid}/products", headers=headers, json={"name": "Product"}).json()["product_id"]
        path = f"/cases/{cid}/products/{pid}/photo"
    elif route == "sjph":
        path = f"/cases/{cid}/sjph-evidence"
        body["item_key"] = "training"
    response = client.post(path, headers=headers, json=body)
    assert response.status_code == 200, response.text
    with m.SessionLocal() as db:
        doc = db.query(models.DocumentAsset).filter_by(case_id=cid).one()
        assert base64.b64decode(doc.content_b64) == raw


@pytest.mark.parametrize("size", [3_603_965, intake.UPLOAD_MAX_BYTES])
def test_large_allowed_original_is_downloadable(client, case, size):
    cid, headers = case
    raw = pdf(size=size)
    response = client.post(f"/cases/{cid}/parse-file", headers=headers, json={
        "filename": "valid_large_coa.pdf", "doc_type": "quality_cert", "file_b64": base64.b64encode(raw).decode()})
    assert response.status_code == 200, response.text
    with m.SessionLocal() as db:
        doc = db.query(models.DocumentAsset).filter_by(case_id=cid).one()
        assert base64.b64decode(doc.content_b64) == raw
        did = doc.document_id
    response = client.get(f"/documents/{did}/file", headers=headers)
    assert response.status_code == 200 and response.content == raw


def test_oversize_single_upload_is_rejected(client, case):
    cid, headers = case
    response = client.post(f"/cases/{cid}/parse-file", headers=headers, json={
        "filename": "large.txt", "doc_type": "other", "file_b64": base64.b64encode(b"x" * (intake.UPLOAD_MAX_BYTES + 1)).decode()})
    assert response.status_code == 413
    with m.SessionLocal() as db:
        assert db.query(models.DocumentAsset).filter_by(case_id=cid).count() == 0


def test_missing_zip_original_is_reported(client, case, monkeypatch):
    cid, headers = case
    # 압축은 작지만 압축 해제한 항목이 원본 저장 상한을 넘는 경로.
    monkeypatch.setattr(intake, "parse_file", lambda *a, **k: "Readable document")
    monkeypatch.setattr(intake, "classify", lambda *a, **k: {"doc_type": "other", "confidence": .9, "fields": {}})
    # 압축비 약 2:1, 해제 크기 20MB 초과: 압축폭탄 가드에 걸리지 않는 입력.
    half = intake.UPLOAD_MAX_BYTES // 2
    raw = random.Random(42).randbytes(half) + b"x" * (half + 1)
    response = client.post(f"/cases/{cid}/intake-zip", headers=headers, json=zip_body("large.txt", raw))
    assert response.status_code == 200, response.text
    problems = client.get(f"/cases/{cid}/problem-documents", headers=headers).json()
    assert "ORIGINAL_NOT_STORED" in {r["code"] for d in problems["documents"] for r in d["reasons"]}
    for lang in ("ko", "en", "id"):
        assert m._problem_document_explanation({"code": "ORIGINAL_NOT_STORED"}, lang)


@pytest.mark.parametrize("route", ["intake-zip", "reprocess"])
def test_ocr_recovery_replaces_all_old_diagnostics(client, case, monkeypatch, route):
    cid, headers = case
    raw = pdf("SERTIFIKAT HALAL\nCertificate No: ID00110012345678\nValid until: 2099-01-01")
    filename = "Sertifikat Halal.pdf"
    old = {"_ocr_error": "TIMEOUT", "_llm_error": "TIMEOUT", "_empty": True,
           "_truncated": {"kind": "pages", "limit": 40}, "_truncations": [{"kind": "pages", "limit": 40}],
           "_verify": {"llm_error": "TIMEOUT"}, "old_field": "obsolete",
           "_autofill": {"changes": {}, "conflicts": []}}
    with m.SessionLocal() as db:
        doc = models.DocumentAsset(case_id=cid, filename=filename, doc_type="halal_certificate", fields=old,
            confidence=0, text_full="", text_excerpt="", content_b64=base64.b64encode(raw).decode(),
            file_hash=m._sha256_b64(base64.b64encode(raw).decode()), translations={"ko": "old translation"})
        db.add(doc)
        db.commit()
        did = doc.document_id
    fresh = {"doc_type": "halal_certificate", "confidence": .9,
             "fields": {"certificate_no": "ID00110012345678", "expiry_date": "2099-01-01"}}
    monkeypatch.setattr(intake, "classify", lambda *a, **k: copy.deepcopy(fresh))
    monkeypatch.setattr(intake, "parse_typed_text", lambda *a, **k: copy.deepcopy(fresh))
    if route == "intake-zip":
        response = client.post(f"/cases/{cid}/intake-zip", headers=headers, json=zip_body(filename, raw))
        assert response.status_code == 200, response.text
        result = response.json()["classified"][0]
    else:
        with m.SessionLocal() as db:
            result = m._reprocess_doc(db, db.get(models.DocumentAsset, did), db.get(models.CaseApplication, cid))
    with m.SessionLocal() as db:
        doc = db.query(models.DocumentAsset).filter_by(case_id=cid).one()
        assert doc.document_id == did
        assert doc.confidence == result["confidence"] == .9
        assert doc.fields == result["fields"]
        assert doc.fields["certificate_no"] == "ID00110012345678"
        assert not set(doc.fields).intersection({"_ocr_error", "_llm_error", "_truncated", "_truncations", "_empty", "_verify", "old_field"})
        assert doc.text_full and doc.text_excerpt and doc.translations is None
        assert "_autofill" in doc.fields
        assert m._problem_document_reasons(doc) == []


@pytest.mark.parametrize("lang,expected", [("ko", "업체 조치 불필요"), ("en", "no company action"),
                                           ("id", "perusahaan tidak perlu")])
def test_verification_error_message(lang, expected):
    doc = models.DocumentAsset(confidence=.9, text_full="Readable", fields={"_verify": {"verdict": "unknown", "llm_error": "TIMEOUT"}})
    reasons = m._problem_document_reasons(doc)
    assert [r["code"] for r in reasons] == ["VERIFY_ERROR"]
    assert expected in m._problem_document_explanation(reasons[0], lang)


@pytest.mark.parametrize("company_action", [False, True])
def test_verify_error_routes_mail_to_staff_only_when_no_company_action(case, monkeypatch, company_action):
    cid, _ = case
    monkeypatch.setenv("GLHAC_PROBLEM_DOC_NOTIFY_DELAY_S", "0")
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        c.email, c.notify_consent = "company@example.test", True
        auditor = db.query(models.User).filter_by(username="auditor1").one()
        auditor.contact_email = "auditor@example.test"
        m.sm.record_event(db, c, c.status, c.status, "ops.auditor_assigned", "operator", "op", {"auditor_id": auditor.user_id})
        fields = {"_verify": {"llm_error": "TIMEOUT"}}
        if company_action:
            fields["_ocr_error"] = "ENCRYPTED"
        db.add(models.DocumentAsset(case_id=cid, filename="certificate.pdf", confidence=.9, text_full="Readable", fields=fields))
        db.commit()
        assert m._notify_problem_documents(db, c)["notified"]
        notice = db.query(models.Notification).filter_by(case_id=cid, event_type="problem.documents").one()
        assert notice.role == ("applicant" if company_action else "auditor")
        contacts, _ = m._notification_contacts(db, notice)
        assert contacts["email"] == ("company@example.test" if company_action else "auditor@example.test")
        assert m._problem_documents_result(db, c)["recipient_type"] == notice.role
        if not company_action:
            assert "업체에 보완을 요청" not in notice.body
            assert "다시 올려" not in notice.body
            assert db.query(models.Notification).filter_by(case_id=cid, role="applicant").count() == 0


@pytest.mark.parametrize("setting,expected", [(None, 0), ("0", 0), ("1", 1)])
def test_geocoding_default_and_explicit_opt_in(setting, expected, tmp_path):
    # 모듈 최초 import 기본값을 별도 프로세스에서 확인하며 네트워크는 대역으로 막는다.
    env = dict(os.environ, GLHAC_DB_URL="sqlite:///" + str(tmp_path / "geo.db"))
    if setting is None:
        env.pop("GLHAC_ZIP_LOOKUP", None)
    else:
        env["GLHAC_ZIP_LOOKUP"] = setting
    script = """
import httpx
from app import intake
import app.main
calls = []
class Result:
    def json(self): return [{'address': {'postcode': '12345'}}]
def get(*args, **kwargs):
    calls.append(args)
    return Result()
httpx.get = get
intake._lookup_zip('Jakarta fixture address')
print(len(calls))
"""
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(expected)
