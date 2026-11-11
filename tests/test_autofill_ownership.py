"""H1~H3: 원문·신청 업체 소유권을 확인한 뒤 두 업로드 경로가 같은 규칙으로 반영한다."""
import base64
import json
import os
import sys
import time
import uuid
import zipfile
from io import BytesIO

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import fitz
import pytest
from fastapi.testclient import TestClient

import app.main as m
from app import intake, models


OWNER = "PT MAJU HALAL SEJAHTERA"
SUPPLIER = "PT SUPPLIER GELATIN"
NIB = "9120001234567"
ADDRESS = "Jl. Sudirman No. 24, Jakarta Selatan"
FACTORY_ADDRESS = "Jl. Pabrik Kami 1, Bogor"
MSDS = ("MATERIAL SAFETY DATA SHEET\nProduct: Gelatin Powder\nManufacturer: " + SUPPLIER +
        "\nNIB 8120009998887\nAddress: Jl. Industri 5, Bekasi\n" + "Hazard information.\n" * 10)


def _pdf(text):
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((50, 60), text, fontsize=9)
        return base64.b64encode(doc.tobytes()).decode()


def _drain():
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if all(q is None or q.unfinished_tasks == 0 for q in (m._autoparse_q, m._verify_q)):
            return
        time.sleep(0.02)
    pytest.fail("자동 파싱·검증 큐가 비지 않았다")


@pytest.fixture(autouse=True)
def ai(monkeypatch):
    monkeypatch.setattr(m, "_RL_WRITE_HITS", {})
    monkeypatch.setattr(intake, "_ZIP_LOOKUP", False)
    state = {"fields": {}, "doc_type": "nib_business_license", "verdict": "ok"}

    def fake(system, user, **kwargs):
        if "접수 검수자" in system:
            return {"verdict": state["verdict"], "summary": "접수 검증", "doc_type_ok": True,
                    "detected_type": None, "field_issues": []}
        if "할랄 인증 서류 분류기" in system:
            return {"doc_type": state["doc_type"], "confidence": 0.95, "fields": dict(state["fields"])}
        return dict(state["fields"])

    monkeypatch.setattr(intake.ai_local, "llm_json", fake)
    yield state
    _drain()


@pytest.fixture
def client(ai):
    with TestClient(m.app) as client:
        yield client


@pytest.fixture
def case(client):
    # conftest의 격리 DB를 사용한다. 조직도 분리해 가입 기준번호·Facility가 사례 사이에 섞이지 않게 한다.
    response = client.post("/auth/register", json={"company_email": "owner@example.com", "email_consent": True, "username": "own" + uuid.uuid4().hex[:12],
                                                  "password": "Owner-test-123!", "company_name": OWNER})
    assert response.status_code == 200, response.text
    headers = {"Authorization": "Bearer " + response.json()["token"]}
    response = client.post("/cases", json={"company_name": OWNER}, headers=headers)
    assert response.status_code == 200, response.text
    cid = response.json()["case_id"]
    yield headers, cid
    _drain()
    # 사례들의 예시 번호는 같다. 다음 사례의 중복번호 검사에 잔존하지 않게 한다.
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        for row in db.query(models.CaseApplication).filter_by(org_id=c.org_id):
            row.nib = None
        db.commit()


def _upload(client, case, ai, fields, text, *, route="single", kind="nib_business_license", filename="scan001.pdf"):
    _drain()
    ai.update(fields=dict(fields), doc_type=kind)
    headers, cid = case
    if route == "single":
        response = client.post(f"/cases/{cid}/parse-file", headers=headers,
                               json={"doc_type": kind, "filename": filename, "file_b64": _pdf(text)})
    else:
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr(filename, base64.b64decode(_pdf(text)))
        endpoint = "intake-zip-stream" if route == "stream" else "intake-zip"
        response = client.post(f"/cases/{cid}/{endpoint}", headers=headers,
                               json={"zip_b64": base64.b64encode(buf.getvalue()).decode()})
    assert response.status_code == 200, response.text
    if route == "stream":
        return [json.loads(line[6:]) for line in response.text.splitlines()
                if line.startswith("data: ")][-1]
    return response.json()


def _nib_text(nib=NIB, company=OWNER):
    return f"NOMOR INDUK BERUSAHA (NIB)\nNama Perusahaan: {company}\nNIB: {nib}\nAlamat: {ADDRESS}\n"


def _factory_text(number="OWN-001", address=FACTORY_ADDRESS, company=OWNER):
    return f"FACTORY REGISTRATION\nCompany: {company}\nRegistration: {number}\nAddress: {address}\n"


def _conflicts(client, case):
    headers, cid = case
    response = client.get(f"/cases/{cid}/pending-extractions", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["conflicts"]


@pytest.mark.parametrize("route", ["single", "zip"])
def test_h1_환각_번호는_빈칸에도_쓰지_않고_확인대기로_남긴다(client, case, ai, route):
    result = _upload(client, case, ai, {"company_name": OWNER, "nib": "5555555555555", "address": ADDRESS},
                     _nib_text(), route=route)
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        assert c.nib is None and c.address is None
    pending = [x for x in _conflicts(client, case) if x["field"] == "nib"]
    assert len(pending) == 1
    assert pending[0]["current"] is None and pending[0]["proposed"] == "5555555555555"
    assert pending[0]["reason"] == "value_not_in_text"
    assert result.get("conflicts") or result["applied"]["conflicts"]


@pytest.mark.parametrize("route", ["single", "zip"])
def test_h1_원문_번호의_공백과_붙임표를_정규화해_정상_서류는_채운다(client, case, ai, route):
    _upload(client, case, ai, {"company_name": OWNER, "nib": NIB, "address": ADDRESS},
            _nib_text("9120-0012-34567"), route=route)
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        assert c.nib == NIB and c.address == ADDRESS
    assert not _conflicts(client, case)


@pytest.mark.parametrize("reference", ["org", "signup"])
@pytest.mark.parametrize("route", ["single", "zip"])
def test_h1_가입과_조직_번호가_다르면_빈_케이스도_충돌이다(client, case, ai, reference, route):
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        if reference == "org":
            db.get(models.Org, c.org_id).profile_ext = {"nib": NIB}
        else:
            registered = db.query(models.CaseApplication).filter(
                models.CaseApplication.org_id == c.org_id, models.CaseApplication.case_id != c.case_id).one()
            registered.nib = NIB
            event = db.query(models.WorkflowEvent).filter_by(case_id=registered.case_id, action="case.create.register").one()
            event.payload = {}   # 스냅샷 도입 전, 가입 케이스에만 저장되던 번호
        db.commit()
    _upload(client, case, ai, {"company_name": OWNER, "nib": "8120009998887", "address": ADDRESS},
            _nib_text("8120009998887"), route=route)
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        assert c.nib is None and c.address is None
    conflict = next(x for x in _conflicts(client, case) if x["field"] == "nib")
    assert conflict["reason"] == "nib_conflict" and conflict["reference"]["value"] == NIB


@pytest.mark.parametrize("route", ["single", "zip"])
def test_h1_먼저_반영된_AI_번호와_진짜_번호를_함께_제시하고_명시적으로_선택한다(client, case, ai, route):
    first = "8120009998887"
    _upload(client, case, ai, {"company_name": OWNER, "nib": first}, _nib_text(first), route=route)
    _upload(client, case, ai, {"company_name": OWNER, "nib": NIB}, _nib_text(), route=route, filename="real.pdf")
    conflict = next(x for x in _conflicts(client, case) if x["field"] == "nib")
    assert (conflict["current"], conflict["proposed"]) == (first, NIB)
    assert conflict["current_source"]["source"] == "ai_unconfirmed"
    assert conflict["current_source"]["filename"] == "scan001.pdf"
    h, cid = case
    # 제품·원재료 전체 반영으로 업체 번호까지 승인되지 않는다.
    assert client.post(f"/cases/{cid}/pending-extractions/apply", headers=h, json={}).status_code == 200
    with m.SessionLocal() as db:
        assert db.get(models.CaseApplication, cid).nib == first
    response = client.post(f"/cases/{cid}/pending-extractions/apply", headers=h,
                           json={"autofill_choices": [{"conflict_id": conflict["conflict_id"], "value": NIB}]})
    assert response.status_code == 200, response.text
    with m.SessionLocal() as db:
        assert db.get(models.CaseApplication, cid).nib == NIB
        assert db.query(models.AuditLog).filter_by(case_id=cid, action="extraction.apply").count() == 2
    assert not _conflicts(client, case)


@pytest.mark.parametrize("route", ["single", "zip"])
@pytest.mark.parametrize("filename", ["scan001.pdf", "NIB.pdf"])
def test_h1_h3_MSDS의_본문_NIB와_공급사_값은_업체와_시설에_들어가지_않는다(client, case, ai, route, filename):
    fields = {"company_name": SUPPLIER, "company_name_en": SUPPLIER, "responsible_person_en": "Budi Supplier",
              "nib": "8120009998887", "address": "Jl. Industri 5, Bekasi",
              "factory_address": "Jl. Industri 5, Bekasi", "factory_reg_no": "SUP-999"}
    _upload(client, case, ai, fields, MSDS, route=route, filename=filename)
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        assert (c.company_name, c.nib, c.address, c.factory_address, c.factory_reg_no) == (OWNER, None, None, None, None)
        assert not (c.profile_ext or {}).get("company_name_en")
        assert not (c.profile_ext or {}).get("responsible_person_en")
        assert not c.facility_ids
        assert db.query(models.Facility).filter_by(org_id=c.org_id).count() == 0


@pytest.mark.parametrize("route", ["single", "zip"])
def test_h2_기존_공장값은_유지하고_새_값은_충돌에_남긴다(client, case, ai, route):
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        c.factory_reg_no, c.factory_address = "OLD-001", "Old factory address"
        db.commit()
    _upload(client, case, ai, {"company_name": OWNER, "factory_reg_no": "OWN-001", "factory_address": FACTORY_ADDRESS},
            _factory_text(), kind="factory_registration", route=route)
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        assert (c.factory_reg_no, c.factory_address) == ("OLD-001", "Old factory address")
    conflicts = {x["field"]: x for x in _conflicts(client, case)}
    assert conflicts["factory_reg_no"]["proposed"] == "OWN-001"
    assert conflicts["factory_address"]["proposed"] == FACTORY_ADDRESS


@pytest.mark.parametrize("route", ["single", "zip"])
@pytest.mark.parametrize("bad", ["verify_fail", "number_absent", "address_absent", "other_company"])
def test_h2_검증실패나_원문근거_소유권이_없는_공장은_반영하지_않는다(client, case, ai, route, bad):
    fields = {"company_name": OWNER, "factory_reg_no": "OWN-001", "factory_address": FACTORY_ADDRESS}
    text = _factory_text()
    if bad == "verify_fail":
        ai["verdict"] = "fail"
    elif bad == "number_absent":
        fields["factory_reg_no"] = "GHOST-999"
    elif bad == "address_absent":
        fields["factory_address"] = "Made up address"
    else:
        fields["company_name"] = SUPPLIER
        text = _factory_text(company=SUPPLIER)
    _upload(client, case, ai, fields, text, route=route, kind="factory_registration")
    _drain()   # LLM 판정은 업로드 이후 검증 큐에서 반영한다.
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        if bad in ("verify_fail", "other_company"):
            assert c.factory_reg_no is None and c.factory_address is None
            assert not c.facility_ids
            facilities = db.query(models.Facility).filter_by(org_id=c.org_id).all()
            if bad == "other_company":
                assert not facilities
            else:
                # 회수된 시설 후보는 확인 대기 항목의 대상으로 남으며 잘못된 값은 비운다.
                assert all(f.reg_no is None and f.address is None for f in facilities)
        elif bad == "number_absent":
            assert c.factory_reg_no is None
        else:
            assert c.factory_address is None
    assert _conflicts(client, case)


def test_h3_기존_Facility의_다른_필드는_덮지_않고_충돌로_남긴다(client, case, ai):
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        fac = models.Facility(org_id=c.org_id, name=OWNER, address="Existing address", reg_no="OLD-001")
        db.add(fac)
        db.commit()
        fid = fac.facility_id
    _upload(client, case, ai, {"company_name": OWNER, "factory_reg_no": "OWN-001", "factory_address": FACTORY_ADDRESS},
            _factory_text(), route="zip", kind="factory_registration")
    with m.SessionLocal() as db:
        fac = db.get(models.Facility, fid)
        assert (fac.address, fac.reg_no) == ("Existing address", "OLD-001")
        assert db.query(models.Facility).filter_by(org_id=fac.org_id).count() == 1
    conflicts = [x for x in _conflicts(client, case) if x["scope"] == "facility"]
    assert {x["field"] for x in conflicts} == {"address", "reg_no"}
    assert all(x["facility_id"] == fid for x in conflicts)


@pytest.mark.parametrize("route", ["zip", "stream"])
def test_h3_ZIP_전문을_저장한_커밋_뒤에_검증큐에_한번_넣는다(client, case, ai, monkeypatch, route):
    queued = []
    text = MSDS + "Long source text\n" * 800
    monkeypatch.setattr(intake, "parse_file", lambda *args, **kwargs: text)

    def enqueue(did):
        with m.SessionLocal() as db:
            d = db.get(models.DocumentAsset, did)
            assert d.text_full == text
            assert d.text_excerpt == text[:300]
        queued.append(did)
        return True

    monkeypatch.setattr(m, "verify_enqueue", enqueue)
    _upload(client, case, ai, {"company_name": SUPPLIER}, MSDS, route=route)
    assert len(queued) == 1


@pytest.mark.parametrize("line", ["NIB 8120009998887", "NIB: 8120-0099-98887"])
def test_h3_번호_라벨은_제목이_아니고_빈줄_제외_첫_세줄만_읽는다(line):
    assert intake._title_lines("\nFirst\n\nSecond\nThird\nNIB\n") == ["First", "Second", "Third"]
    assert line not in intake._title_lines("\n" + line)
    assert "사업자등록증" not in intake._title_lines("First\nSecond\nThird\n[시트/제품명: 사업자등록증]")
    assert intake.refine_doctype_reason("scan.pdf", "factory_registration", MSDS)[0] == "coa_msds"


@pytest.mark.parametrize("route", ["single", "zip", "stream"])
@pytest.mark.parametrize("reference", ["case", "org_profile", "registration"])
def test_기존_번호와_일치하면_등록증_상호_주소_번호표기가_정본이다(client, case, ai, route, reference):
    number = "9120-0012-34567"
    registered_company = OWNER + " TBK"
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        c.address = "Manual address"
        if reference == "case":
            c.nib = number
        elif reference == "org_profile":
            db.get(models.Org, c.org_id).profile_ext = {"nib": number}
        else:
            event = (db.query(models.WorkflowEvent)
                     .join(models.CaseApplication, models.CaseApplication.case_id == models.WorkflowEvent.case_id)
                     .filter(models.CaseApplication.org_id == c.org_id,
                             models.WorkflowEvent.action == "case.create.register").one())
            event.payload = {**(event.payload or {}), "nib": number}
        db.commit()
    fields = {"company_name": registered_company, "nib": NIB, "address": ADDRESS}
    result = _upload(client, case, ai, fields, _nib_text(company=registered_company), route=route)
    if route == "single":
        assert result["applied"] == fields
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        assert (c.company_name, c.nib, c.address) == (registered_company, NIB, ADDRESS)
        doc = db.query(models.DocumentAsset).filter_by(case_id=c.case_id).one()
        did, changes = doc.document_id, doc.fields["_autofill"]["changes"]
        assert changes["nib"]["before"] == (number if reference == "case" else None)
        assert changes["company_name"]["before"] == OWNER
        assert changes["address"]["before"] == "Manual address"
    assert not _conflicts(client, case)
    _drain()
    ai["verdict"] = "fail"
    assert m.verify_enqueue(did)
    _drain()
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        assert (c.company_name, c.nib, c.address) == (OWNER, number if reference == "case" else None, "Manual address")


def test_같은_업체_Facility의_빈칸만_채우고_선택을_연결한다(client, case, ai):
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        fac = models.Facility(org_id=c.org_id, name=OWNER, reg_no="OWN-001")
        db.add(fac)
        db.commit()
        fid = fac.facility_id
    _upload(client, case, ai, {"company_name": OWNER, "factory_reg_no": "OWN-001", "factory_address": FACTORY_ADDRESS,
                              "factory_phone": "021-5551234"},
            _factory_text() + "Tel: 021-5551234\n", kind="factory_registration", route="zip")
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        fac = db.get(models.Facility, fid)
        assert (fac.reg_no, fac.address) == ("OWN-001", FACTORY_ADDRESS)
        assert fac.profile_ext["phone"] == "021-5551234"
        assert c.facility_ids == [fid]
    assert not _conflicts(client, case)


def test_충돌_조회_이후_사람이_바꾼_값을_낡은_선택으로_덮지_않는다(client, case, ai):
    _upload(client, case, ai, {"company_name": OWNER, "nib": "5555555555555"}, _nib_text())
    conflict = next(x for x in _conflicts(client, case) if x["field"] == "nib")
    with m.SessionLocal() as db:
        db.get(models.CaseApplication, case[1]).nib = NIB
        db.commit()
    response = client.post(f"/cases/{case[1]}/pending-extractions/apply", headers=case[0],
                           json={"autofill_choices": [{"conflict_id": conflict["conflict_id"], "value": "5555555555555"}]})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "AUTOFILL_CONFLICT_STALE"
    with m.SessionLocal() as db:
        assert db.get(models.CaseApplication, case[1]).nib == NIB


@pytest.mark.parametrize("route", ["reprocess", "profile"])
def test_재처리와_프로필_자동채움도_환각_번호를_우회_반영하지_않는다(client, case, ai, route):
    h, cid = case
    ai["fields"] = {"company_name": OWNER, "nib": "5555555555555", "address": ADDRESS}
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        d = models.DocumentAsset(case_id=cid, filename="NIB.pdf", doc_type="nib_business_license", content_b64=_pdf(_nib_text()))
        db.add(d)
        db.commit()
        did = d.document_id
        if route == "reprocess":
            m._reprocess_doc(db, d, c)
    if route == "profile":
        response = client.post(f"/cases/{cid}/profile/autofill?force=true", headers=h)
        assert response.status_code == 200, response.text
    _drain()
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        assert c.nib is None and c.address is None
        assert db.get(models.DocumentAsset, did).text_full
    assert any(x["field"] == "nib" for x in _conflicts(client, case))


@pytest.mark.parametrize("number", ["55", "1234", "912000123456"])
def test_원문_번호의_일부분은_실재_번호로_인정하지_않는다(number):
    assert not intake.autofill_value_in_text("nib", number, _nib_text())


def test_무출처_집계는_업체_공장_값을_반영하지_않는다(case):
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        m._apply_agg_to_case(db, c, {"nib": NIB, "company_name": SUPPLIER, "factory_address": "Supplier address"})
        assert c.company_name == OWNER and c.nib is None and c.factory_address is None


def test_가입_당시_번호는_나중의_케이스_수정과_독립적으로_대조한다(client, ai):
    response = client.post("/auth/register", json={"company_email": "owner@example.com", "email_consent": True, "username": "own" + uuid.uuid4().hex[:12],
                                                  "password": "Owner-test-123!", "company_name": OWNER, "nib": NIB})
    assert response.status_code == 200, response.text
    h = {"Authorization": "Bearer " + response.json()["token"]}
    org_id = response.json()["org_id"]
    with m.SessionLocal() as db:
        c = db.query(models.CaseApplication).filter_by(org_id=org_id).one()
        cid = c.case_id
        c.nib = None
        db.commit()
    _upload(client, (h, cid), ai, {"company_name": OWNER, "nib": "8120009998887"}, _nib_text("8120009998887"))
    conflict = next(x for x in _conflicts(client, (h, cid)) if x["field"] == "nib")
    assert conflict["reference"] == {"source": "registration", "value": NIB, "case_id": cid}
    with m.SessionLocal() as db:
        assert db.get(models.CaseApplication, cid).nib is None


@pytest.mark.parametrize("bad_id", [[], {}, None])
def test_잘못된_충돌_선택_입력은_서버오류가_아닌_검증오류다(client, case, bad_id):
    response = client.post(f"/cases/{case[1]}/pending-extractions/apply", headers=case[0],
                           json={"autofill_choices": [{"conflict_id": bad_id, "value": NIB}]})
    assert response.status_code == 422


@pytest.mark.parametrize("route", ["single", "zip", "stream"])
@pytest.mark.parametrize("company,address", [(OWNER, "Seoul"), (OWNER, None), ("My Company", None), ("", None)])
def test_번호가_비면_같은_업체의_빈칸_반영값만_검증실패로_되돌린다(
        client, case, ai, monkeypatch, route, company, address):
    queued, enqueue = [], m.verify_enqueue
    monkeypatch.setattr(m, "verify_enqueue", lambda did: queued.append(did) or True)
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        c.company_name, c.address = company, address
        db.commit()
    # 빈 업체명·기본 업체명은 가입 조직명 OWNER와 대조한다.
    result = _upload(client, case, ai, {"company_name": OWNER, "nib": NIB, "address": ADDRESS},
                     _nib_text(), route=route)
    expected = {"nib": NIB}
    if company in ("", "My Company"):
        expected["company_name"] = OWNER
    if not address:
        expected["address"] = ADDRESS
    if route == "single":
        assert result["applied"] == expected
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        assert (c.company_name, c.nib, c.address) == (expected.get("company_name", company), NIB, address or ADDRESS)
        fields = db.get(models.DocumentAsset, queued[0]).fields
        assert "_verify" not in fields
        changes = fields["_autofill"]["changes"]
        assert all(changes[k]["status"] == "applied" and changes[k]["source"] == "ai_unconfirmed" for k in expected)
        assert m._autofill_current_source(db, c, "nib", c.nib)["source"] == "ai_unconfirmed"
    assert not any(x["reason"] == "company_conflict" for x in _conflicts(client, case))
    # 정상 검증 뒤의 후속 fail도 이 문서가 빈칸에 쓴 값만 복원한다.
    assert enqueue(queued[0])
    _drain()
    with m.SessionLocal() as db:
        verify = db.get(models.DocumentAsset, queued[0]).fields["_verify"]
        assert verify["verdict"] == "ok"
        assert not any(i.get("code") == "company_name_mismatch" for i in verify["field_issues"])
    ai["verdict"] = "fail"
    assert enqueue(queued[0])
    _drain()
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        assert (c.company_name, c.nib, c.address) == (company, None, address)
        assert db.get(models.DocumentAsset, queued[0]).fields["_verify"]["verdict"] == "fail"
    assert {x["field"] for x in _conflicts(client, case)
            if x["scope"] == "case" and x["reason"] == "verify_failed"} == set(expected)


@pytest.mark.parametrize("route", ["single", "zip", "stream"])
@pytest.mark.parametrize("company,address", [(OWNER, "Seoul"), (OWNER, None), ("My Company", None), ("", None)])
def test_번호가_비면_다른_업체_등록증은_검증결과와_무관하게_후보로_보관한다(
        client, case, ai, monkeypatch, route, company, address):
    queued, enqueue = [], m.verify_enqueue
    monkeypatch.setattr(m, "verify_enqueue", lambda did: queued.append(did) or True)
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        c.company_name, c.address = company, address
        db.commit()
    result = _upload(client, case, ai, {"company_name": SUPPLIER, "nib": NIB, "address": ADDRESS},
                     _nib_text(company=SUPPLIER), route=route)
    if route == "single":
        assert result["applied"] == {"skipped": "company_conflict"}
    assert len(queued) == 1
    pending = _conflicts(client, case)
    nibs = [x for x in pending if x["scope"] == "case" and x["field"] == "nib"]
    assert len(nibs) == 1
    assert (nibs[0]["current"], nibs[0]["proposed"], nibs[0]["reason"]) == (None, NIB, "company_conflict")
    assert {x["document_id"] for x in pending} == {queued[0]}
    # 검증 ok도 소유권 충돌을 승인하지 않으며, fail도 미반영 후보를 중복 생성하지 않는다.
    for verdict in (None, "ok", "fail"):
        if verdict is not None:
            ai["verdict"] = verdict
            assert enqueue(queued[0])
            _drain()
        with m.SessionLocal() as db:
            c = db.get(models.CaseApplication, case[1])
            assert (c.company_name, c.nib, c.address) == (company, None, address)
            fields = db.get(models.DocumentAsset, queued[0]).fields
            assert not fields["_autofill"]["changes"]
            if verdict is not None:
                assert fields["_verify"]["verdict"] == verdict
        assert _conflicts(client, case) == pending
        problems = client.get(f"/cases/{case[1]}/problem-documents", headers=case[0]).json()
        assert "OWNERSHIP_MISMATCH" in {r["code"] for d in problems["documents"] for r in d["reasons"]}


@pytest.mark.parametrize("route", ["single", "zip"])
def test_선택한_칸과_LLM_추측만으로_서류_유형을_확정하지_않는다(client, case, ai, route):
    text = f"Scanned document\nCompany: {OWNER}\nNIB {NIB}\nAddress: {ADDRESS}\n"
    _upload(client, case, ai, {"company_name": OWNER, "nib": NIB, "address": ADDRESS}, text, route=route)
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        assert c.nib is None and c.address is None
    assert any(x["reason"] == "unconfirmed_document_type" for x in _conflicts(client, case))


def test_유형_근거가_파일명뿐이고_AI_검증도_없으면_확인대기로_둔다(client, case, ai):
    ai["verdict"] = "unknown"
    text = f"Scanned document\nCompany: {OWNER}\nNIB {NIB}\nAddress: {ADDRESS}\n"
    _upload(client, case, ai, {"company_name": OWNER, "nib": NIB}, text, filename="NIB.pdf")
    with m.SessionLocal() as db:
        assert db.get(models.CaseApplication, case[1]).nib is None
    assert any(x["reason"] == "unconfirmed_document_type" for x in _conflicts(client, case))


@pytest.mark.parametrize("route", ["single", "zip", "stream"])
def test_업로드_반영_경로는_LLM_검증을_호출하지_않고_커밋_뒤_큐에만_넣는다(client, case, ai, monkeypatch, route):
    fields = {"company_name": OWNER, "nib": NIB, "address": ADDRESS}
    # 추출 단계의 결과를 고정하고, 그 이후 업로드 반영에서 발생하는 LLM 호출을 잡는다.
    monkeypatch.setattr(intake, "parse_typed", lambda *a, **kw: {"fields": dict(fields), "text": _nib_text()})
    monkeypatch.setattr(intake, "classify", lambda *a, **kw: {
        "fields": dict(fields), "doc_type": "nib_business_license", "confidence": 0.95})
    calls, queued = [], []

    def unexpected(*args, **kwargs):
        calls.append(True)
        pytest.fail("업로드 응답 전에 반영 경로가 llm_json을 호출했다")

    def enqueue(did):
        with m.SessionLocal() as db:
            d = db.get(models.DocumentAsset, did)
            assert d is not None and "_verify" not in d.fields
            assert db.get(models.CaseApplication, case[1]).nib == NIB
        queued.append(did)
        return True

    monkeypatch.setattr(intake.ai_local, "llm_json", unexpected)
    monkeypatch.setattr(m, "verify_enqueue", enqueue)
    _upload(client, case, ai, fields, _nib_text(), route=route)
    assert calls == [] and len(queued) == 1


@pytest.mark.parametrize("route", ["single", "zip"])
def test_비동기_fail은_같은_문서의_자동반영만_되돌리고_중복없이_확인대기에_올린다(client, case, ai, monkeypatch, route):
    queued, enqueue = [], m.verify_enqueue
    monkeypatch.setattr(m, "verify_enqueue", lambda did: queued.append(did) or True)
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        c.profile_ext = {"office_phone": "사람이 입력한 전화"}
        db.commit()
    _upload(client, case, ai, {"company_name": OWNER, "nib": NIB, "address": ADDRESS}, _nib_text(), route=route)
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        assert c.nib == NIB and c.address == ADDRESS and c.profile_ext["company_name_en"] == OWNER
    ai["verdict"] = "fail"
    assert enqueue(queued[0])
    _drain()
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        assert c.company_name == OWNER and c.nib is None and c.address is None
        assert c.profile_ext == {"office_phone": "사람이 입력한 전화"}
        assert db.get(models.DocumentAsset, queued[0]).fields["_verify"]["verdict"] == "fail"
    pending = _conflicts(client, case)
    assert {(x["scope"], x["field"]) for x in pending} == {
        ("case", "nib"), ("case", "address"), ("profile", "company_name_en"), ("profile", "country")}
    assert all(x["reason"] == "verify_failed" and x["document_id"] == queued[0] for x in pending)
    assert enqueue(queued[0])
    _drain()
    assert _conflicts(client, case) == pending
    conflict = next(x for x in pending if x["field"] == "nib")
    response = client.post(f"/cases/{case[1]}/pending-extractions/apply", headers=case[0],
                           json={"autofill_choices": [{"conflict_id": conflict["conflict_id"], "value": NIB}]})
    assert response.status_code == 200, response.text
    assert enqueue(queued[0])
    _drain()
    with m.SessionLocal() as db:
        assert db.get(models.CaseApplication, case[1]).nib == NIB
    assert not any(x["field"] == "nib" for x in _conflicts(client, case))


@pytest.mark.parametrize("value", [NIB, "8120009998887", None])
def test_검증_대기_중_사람이_저장한_값은_같은값_확인과_삭제까지_보존한다(client, case, ai, monkeypatch, value):
    queued = []
    monkeypatch.setattr(m, "verify_enqueue", lambda did: queued.append(did) or True)
    _upload(client, case, ai, {"company_name": OWNER, "nib": NIB, "address": ADDRESS}, _nib_text())
    with m.SessionLocal() as db:
        d, c = db.get(models.DocumentAsset, queued[0]), db.get(models.CaseApplication, case[1])

        def verify(*args):
            assert not db.in_transaction()
            response = client.patch(f"/cases/{case[1]}/profile", headers=case[0], json={"nib": value})
            assert response.status_code == 200, response.text
            return {"verdict": "fail", "summary": "검증 실패"}

        monkeypatch.setattr(intake, "verify_extraction", verify)
        m._verify_doc(db, d, c)
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        assert c.nib == value and c.address is None
    assert not any(x["field"] == "nib" for x in _conflicts(client, case))


@pytest.mark.parametrize("reprocess", ["none", "document", "profile"])
def test_다른_문서의_충돌에서_기존값을_확인하면_원본_검증실패에도_유지한다(client, case, ai, monkeypatch, reprocess):
    queued = []
    monkeypatch.setattr(m, "verify_enqueue", lambda did: queued.append(did) or True)
    fields = {"company_name": OWNER, "nib": NIB}
    _upload(client, case, ai, fields, _nib_text())
    _upload(client, case, ai, {"company_name": OWNER, "nib": "8120009998887"},
            _nib_text("8120009998887"), filename="other.pdf")
    conflict = next(x for x in _conflicts(client, case) if x["field"] == "nib")
    response = client.post(f"/cases/{case[1]}/pending-extractions/apply", headers=case[0],
                           json={"autofill_choices": [{"conflict_id": conflict["conflict_id"], "value": NIB}]})
    assert response.status_code == 200, response.text
    ai.update(fields=fields, verdict="fail")
    if reprocess == "profile":
        response = client.post(f"/cases/{case[1]}/profile/autofill", headers=case[0])
        assert response.status_code == 200, response.text
    with m.SessionLocal() as db:
        d, c = db.get(models.DocumentAsset, queued[0]), db.get(models.CaseApplication, case[1])
        if reprocess == "document":
            m._reprocess_doc(db, d, c)
        m._verify_doc(db, d, c)
    with m.SessionLocal() as db:
        assert db.get(models.CaseApplication, case[1]).nib == NIB
    assert not any(x["field"] == "nib" and x["document_id"] == queued[0] for x in _conflicts(client, case))


def test_시설과_확장프로필도_사람이_확인한_필드만_검증실패에서_보호한다(client, case, ai, monkeypatch):
    queued = []
    monkeypatch.setattr(m, "verify_enqueue", lambda did: queued.append(did) or True)
    _upload(client, case, ai, {"company_name": OWNER, "factory_reg_no": "OWN-001",
                              "factory_address": FACTORY_ADDRESS, "factory_phone": "021-5551234"},
            _factory_text() + "Tel: 021-5551234\n", kind="factory_registration")
    with m.SessionLocal() as db:
        fid = db.get(models.CaseApplication, case[1]).facility_ids[0]
    response = client.patch(f"/facilities/{fid}", headers=case[0],
                            json={"address": FACTORY_ADDRESS, "phone": "021-5551234"})
    assert response.status_code == 200, response.text
    ai["verdict"] = "fail"
    with m.SessionLocal() as db:
        m._verify_doc(db, db.get(models.DocumentAsset, queued[0]), db.get(models.CaseApplication, case[1]))
    with m.SessionLocal() as db:
        fac, c = db.get(models.Facility, fid), db.get(models.CaseApplication, case[1])
        assert fac.address == FACTORY_ADDRESS and fac.profile_ext["phone"] == "021-5551234"
        assert fac.reg_no is None and c.factory_reg_no is None and c.factory_address is None
        assert c.facility_ids == [fid]
    assert not any(x["scope"] in ("facility", "facility_profile") and x["field"] in ("address", "phone")
                   for x in _conflicts(client, case))


def test_자동반영_출처가_아닌_같은값_문서의_fail은_기존값을_지우지_않는다(client, case, ai, monkeypatch):
    queued = []
    monkeypatch.setattr(m, "verify_enqueue", lambda did: queued.append(did) or True)
    fields = {"company_name": OWNER, "nib": NIB, "address": ADDRESS}
    _upload(client, case, ai, fields, _nib_text())
    _upload(client, case, ai, fields, _nib_text(), filename="same.pdf")
    ai["verdict"] = "fail"
    with m.SessionLocal() as db:
        m._verify_doc(db, db.get(models.DocumentAsset, queued[1]), db.get(models.CaseApplication, case[1]))
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        assert c.nib == NIB and c.address == ADDRESS
    assert not _conflicts(client, case)


def test_출처기록이_남아도_이후_다른값으로_수정됐으면_되돌리지_않는다(client, case, ai, monkeypatch):
    queued = []
    monkeypatch.setattr(m, "verify_enqueue", lambda did: queued.append(did) or True)
    _upload(client, case, ai, {"company_name": OWNER, "nib": NIB, "address": ADDRESS}, _nib_text())
    with m.SessionLocal() as db:
        db.get(models.CaseApplication, case[1]).address = "사람이 바꾼 주소"
        db.commit()
    ai["verdict"] = "fail"
    with m.SessionLocal() as db:
        m._verify_doc(db, db.get(models.DocumentAsset, queued[0]), db.get(models.CaseApplication, case[1]))
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        assert c.nib is None and c.address == "사람이 바꾼 주소"
    assert not any(x["field"] == "address" for x in _conflicts(client, case))


def test_검증_대기_중_재추출된_문서에_이전_실패로_롤백하지_않는다(client, case, ai, monkeypatch):
    queued = []
    monkeypatch.setattr(m, "verify_enqueue", lambda did: queued.append(did) or True)
    _upload(client, case, ai, {"company_name": OWNER, "nib": NIB, "address": ADDRESS}, _nib_text())

    def verify(*args):
        with m.SessionLocal() as db:
            d = db.get(models.DocumentAsset, queued[0])
            d.text_full += "재추출된 본문\n"
            db.commit()
        return {"verdict": "fail"}

    monkeypatch.setattr(intake, "verify_extraction", verify)
    with m.SessionLocal() as db:
        m._verify_doc(db, db.get(models.DocumentAsset, queued[0]), db.get(models.CaseApplication, case[1]))
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        assert c.nib == NIB and c.address == ADDRESS
    assert not _conflicts(client, case)


def test_문서_승인은_당시_반영값만_보호하고_이후_재추출_값까지_승인하지_않는다(client, case, ai, monkeypatch):
    queued = []
    monkeypatch.setattr(m, "verify_enqueue", lambda did: queued.append(did) or True)
    _upload(client, case, ai, {"company_name": OWNER, "nib": NIB}, _nib_text())
    login = client.post("/auth/login", json={"username": "admin", "password": "admin"})
    assert login.status_code == 200, login.text
    headers = {"Authorization": "Bearer " + login.json()["token"]}
    response = client.patch(f"/documents/{queued[0]}/review", headers=headers, json={"review_status": "approved"})
    assert response.status_code == 200, response.text
    ai.update(fields={"company_name": OWNER, "nib": NIB, "address": ADDRESS}, verdict="fail")
    with m.SessionLocal() as db:
        d, c = db.get(models.DocumentAsset, queued[0]), db.get(models.CaseApplication, case[1])
        m._reprocess_doc(db, d, c)
        assert c.address == ADDRESS
        m._verify_doc(db, d, c)
    with m.SessionLocal() as db:
        c = db.get(models.CaseApplication, case[1])
        assert c.nib == NIB and c.address is None
    assert any(x["field"] == "address" for x in _conflicts(client, case))
    assert not any(x["field"] == "nib" for x in _conflicts(client, case))
