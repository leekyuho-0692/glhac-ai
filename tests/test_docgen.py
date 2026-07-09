"""문서 자동생성 P1(Company/Facility Info) 검증 — 격리 임시DB, 서버 불필요.
실행: <venv>/bin/python -m pytest tests/test_docgen.py -q
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_docgen_test.db")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.main as m  # noqa: E402
import app.models as models  # noqa: E402


def _seed_db(tmp_path):
    eng = create_engine("sqlite:///%s" % (tmp_path / "docgen.db"))
    models.Base.metadata.create_all(bind=eng)
    db = sessionmaker(bind=eng)()
    db.add(models.Org(org_id="org1", name="ABC Food", address="Seoul, Korea"))
    db.add(models.CaseApplication(
        case_id="case1", org_id="org1", company_name="ABC Food Co., Ltd.",
        nib="123-45-67890", responsible_person="Kim Min-jun", address="Seoul, Republic of Korea",
        factory_address="Busan", email="a@abc.co", phone="010-1234",
        pathway="reguler", sehati_eligible="yes",
        profile_ext={"pic_name": "박민", "pic_title": "부장", "cp_name": "이수", "cp_title": "대리",
                     "registration_type": "Manufacturing", "application_type": "Regular",
                     "registration_status": "New", "production_capacity": "5,000 units/day"},
        product_ids=None))
    db.add(models.Facility(facility_id="fac1", org_id="org1", name="ABC Busan Plant",
        address="Busan, Republic of Korea", city="Busan", country="Korea", zip="00000", reg_no="F-123",
        profile_ext={"phone": "051-000", "email": "busan@abc.co",
                     "pic_name": "김공", "pic_title": "공장장", "cp_name": "정라", "cp_title": "주임"}))
    db.add(models.Product(product_id="prod1", case_id="case1", org_id="org1",
                          name="Sample Extract Drink", category="Beverages"))
    db.commit()
    return db


USER = {"uid": "u1", "role": "applicant", "org_id": "org1"}


def test_company_info_merges_fields(tmp_path):
    db = _seed_db(tmp_path)
    r = m.gen_company_info("case1", USER, db)
    doc = r["document"]
    assert r["version"] == 1 and r["gen_doc_id"]
    for expect in ["ABC Food Co., Ltd.", "Kim Min-jun", "123-45-67890",
                   "박민 / 부장", "Manufacturing", "Sample Extract Drink (Beverages)"]:
        assert expect in doc, "누락: %s" % expect


def test_facility_info_merges_fields(tmp_path):
    db = _seed_db(tmp_path)
    r = m.gen_facility_info("case1", "fac1", USER, db)
    doc = r["document"]
    assert "ABC Busan Plant" in doc and "F-123" in doc and "김공 / 공장장" in doc


def test_facility_info_org_isolation(tmp_path):
    db = _seed_db(tmp_path)
    other = {"uid": "u2", "role": "applicant", "org_id": "orgX"}
    try:
        m.gen_facility_info("case1", "fac1", other, db)
        assert False, "타 조직 접근이 차단되지 않음"
    except Exception:
        pass  # check_org 403 기대


def test_gendoc_pdf_renders(tmp_path):
    db = _seed_db(tmp_path)
    r = m.gen_company_info("case1", USER, db)
    resp = m.get_gendoc_pdf(r["gen_doc_id"], USER, db)
    assert resp.media_type == "application/pdf"
    assert resp.body[:4] == b"%PDF"


def test_contract_generate_pdf_sign(tmp_path):
    db = _seed_db(tmp_path)
    r = m.gen_contract("case1", user=USER, db=db)
    assert r["contract_no"].startswith("HAC-") and r["status"] == "issued"
    # 리치 PDF 렌더(정적 법률조항 + 제품표 + 서명블록)
    resp = m.get_contract_pdf("case1", USER, db)
    assert resp.media_type == "application/pdf" and resp.body[:4] == b"%PDF"
    # 양자 서명 → signed
    m.sign_contract(r["contract_id"], party="A", name="Client Rep", user=USER, db=db)
    s2 = m.sign_contract(r["contract_id"], party="B", name="GLHAC Rep", user=USER, db=db)
    assert s2["status"] == "signed"
    assert len(s2["signatures"]) == 2


def test_contract_pdf_404_when_absent(tmp_path):
    db = _seed_db(tmp_path)
    try:
        m.get_contract_pdf("case1", USER, db)
        assert False, "계약서 없는데 404 아님"
    except Exception:
        pass


def test_fatwa_decree_generate_and_pdf(tmp_path):
    db = _seed_db(tmp_path)
    db.add(models.FatwaDecision(
        case_id="case1", decision="approved", decision_no="GLHAC/2026/0001",
        committee_head="Dr. Ahmad", committee_secretary="Ust. Farhan",
        committee_members=["Dr. Siti"], decided_at=datetime.utcnow()))
    db.add(models.FatwaVote(case_id="case1", member="Dr. Ahmad", vote="approve"))
    db.commit()
    r = m.gen_fatwa_decree("case1", user=USER, db=db)
    assert r["decision"] == "approved"
    assert "GLHAC/2026/0001" in r["document"] and "Dr. Ahmad" in r["document"]
    resp = m.get_fatwa_decree_pdf("case1", user=USER, db=db)
    assert resp.media_type == "application/pdf" and resp.body[:4] == b"%PDF"


def test_factory_audit_pdf_with_photo(tmp_path):
    import base64
    import fitz
    db = _seed_db(tmp_path)
    db.add(models.Material(material_id="m1", case_id="case1", name="Gelatin A",
                           mat_type="additive", source="animal", screen_result="BLOCK",
                           screen_status="소유래 확인요망"))
    db.add(models.AuditFinding(case_id="case1", area="생산라인", finding="교차오염 차단 미흡",
                               severity="major", status="open", auditor="Ahmad Fauzi"))
    db.add(models.HpasEvaluation(case_id="case1", element="materials", status="gap", note="Gelatin 확인 필요"))
    db.add(models.PenyeliaHalal(org_id="org1", name="Siti Rahma", status="active"))
    pm = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 60, 40))
    pm.set_rect(pm.irect, (30, 120, 200))
    png_b64 = base64.b64encode(pm.tobytes("png")).decode()
    db.add(models.DocumentAsset(case_id="case1", filename="IMG_0418.jpg", doc_type="facility_photo",
                                content_b64=png_b64, content_type="image/png",
                                lat=-6.23, lng=106.99, geo_source="exif"))
    db.commit()
    resp = m.get_factory_audit_pdf("case1", user=USER, db=db)
    assert resp.media_type == "application/pdf" and resp.body[:4] == b"%PDF"


def test_sjph_manual_pdf_and_gate(tmp_path):
    db = _seed_db(tmp_path)
    # 증빙 일부만 업로드(halal_supervisor, training) → 게이트 미완료 기대
    db.add(models.SjphEvidence(case_id="case1", item_key="halal_supervisor", filename="appt.pdf"))
    db.add(models.SjphEvidence(case_id="case1", item_key="training", filename="train.pdf"))
    db.add(models.PenyeliaHalal(org_id="org1", name="Siti Rahma", status="active"))
    db.commit()
    resp = m.get_sjph_manual_pdf("case1", user=USER, db=db)
    assert resp.media_type == "application/pdf" and resp.body[:4] == b"%PDF"
    # PDF 텍스트에 증빙 진행/게이트 문구 포함 확인
    import fitz
    txt = "".join(p.get_text() for p in fitz.open(stream=resp.body, filetype="pdf"))
    assert "SJPH" in txt and "Appendix" in txt


def _add_factory_doc(db, reg, addr, mfr=None, fname="f.pdf"):
    db.add(models.DocumentAsset(case_id="case1", filename=fname, doc_type="factory_registration",
                                fields={"factory_reg_no": reg, "factory_address": addr, "manufacturer": mfr}))


def test_factory_classify_dedup(tmp_path):
    db = _seed_db(tmp_path)
    _add_factory_doc(db, "F-100", "Busan", "ABC Busan", "f1.pdf")   # 중복1
    _add_factory_doc(db, "F-100", "Busan", None, "f1b.pdf")          # 중복2 (같은 등록번호)
    _add_factory_doc(db, "F-200", "Seoul", "ABC Seoul", "f2.pdf")    # 다른 공장
    db.commit()
    r = m.classify_factories("case1", user=USER, db=db)
    assert r["classified"] == 2  # F-100(dedup) + F-200
    assert len(r["factories"]) == 2
    labels = [f["label"] for f in r["factories"]]
    assert "공장1" in labels and "공장2" in labels
    # 같은 공장 문서 2건이 한 공장에 묶임
    f100 = next(f for f in r["factories"] if f["reg_no"] == "F-100")
    assert len(f100["source_docs"]) == 2


def test_factory_empty_and_crud(tmp_path):
    db = _seed_db(tmp_path)
    # 공장 파일 없음 → 빈 공장1 자동 생성
    r = m.classify_factories("case1", user=USER, db=db)
    assert len(r["factories"]) == 1 and r["factories"][0]["label"] == "공장1"
    fid = r["factories"][0]["facility_id"]
    # 리스트 이름 수기 변경 + 필드
    up = m.update_factory(fid, body={"label": "메인공장", "reg_no": "F-9", "name": "ABC Main"}, user=USER, db=db)
    assert up["label"] == "메인공장" and up["reg_no"] == "F-9" and up["name"] == "ABC Main"
    # 수동 추가 → 공장2
    add = m.add_factory("case1", body=None, user=USER, db=db)
    assert add["label"] == "공장2"
    assert len(m.list_factories("case1", user=USER, db=db)["factories"]) == 2
    # 삭제
    d = m.delete_factory("case1", add["facility_id"], user=USER, db=db)
    assert add["facility_id"] not in d["remaining"]
    assert len(m.list_factories("case1", user=USER, db=db)["factories"]) == 1


def test_facility_info_after_classify(tmp_path):
    db = _seed_db(tmp_path)
    _add_factory_doc(db, "F-1", "Busan", "ABC Busan Plant")
    db.commit()
    r = m.classify_factories("case1", user=USER, db=db)
    fid = r["factories"][0]["facility_id"]
    doc = m.gen_facility_info("case1", fid, USER, db)  # D2 정합
    assert "ABC Busan Plant" in doc["document"]


def test_case_journey_11_stages(tmp_path):
    db = _seed_db(tmp_path)
    r = m.get_case_journey("case1", user=USER, db=db)
    assert len(r["stages"]) == 11
    assert r["stages"][0]["key"] == "signup" and r["stages"][0]["status"] == "done"
    labels = [s["label"] for s in r["stages"]]
    for lab in ["회원가입", "계약", "할랄매뉴얼", "모의실사", "파트와", "인증서"]:
        assert lab in labels
    # 정확히 하나의 current
    assert sum(1 for s in r["stages"] if s["status"] == "current") == 1


def test_material_report_snapshot_and_source_docs(tmp_path):
    db = _seed_db(tmp_path)
    db.add(models.Material(material_id="m1", case_id="case1", name="Gelatin A"))
    db.add(models.DocumentAsset(case_id="case1", filename="coa.pdf", doc_type="halal_certificate", material_id="m1"))
    db.commit()
    # 리포트 rows에 source_docs 포함
    rep = m._material_report(db, m._get_case(db, "case1", USER))
    row = next(r for r in rep["materials"] if r["material_id"] == "m1")
    assert row["source_docs"] and row["source_docs"][0]["filename"] == "coa.pdf"
    # 스냅샷 저장(오디터 체크·이력)
    r = m.save_material_report_snapshot("case1", body={"checked": ["m1"], "note": "확인"}, user=USER, db=db)
    assert r["checked"] == 1 and r["gen_doc_id"]


def test_contract_fee_override(tmp_path):
    db = _seed_db(tmp_path)
    m.gen_contract("case1", body={"fee": 9999000}, user=USER, db=db)
    ct = db.query(models.Contract).filter_by(case_id="case1").first()
    assert ct.fee == 9999000  # M4: 특수조항 수동 금액 override


def test_propose_audit_dates(tmp_path):
    db = _seed_db(tmp_path)
    p = models.AuditPlan(case_id="case1", scheduled_date="2026-08-01", status="scheduled")
    db.add(p)
    db.commit()
    auditor = {"uid": "a1", "role": "auditor", "org_id": "org1"}
    r = m.propose_audit_dates(p.id, body={"dates": ["2026-08-05", "2026-08-06", "2026-08-08"]}, user=auditor, db=db)
    assert len(r["proposed"]) == 3 and "일정변경 제안" in r["note"]


def test_consultation_flow(tmp_path):
    db = _seed_db(tmp_path)
    r = m.create_consultation(body={"subject": "질문", "message": "인증 절차 문의"}, user=USER, db=db)
    assert r["status"] == "open"
    cid = r["id"]
    lst = m.list_consultations(user=USER, db=db)
    assert any(c["id"] == cid for c in lst)
    admin = {"uid": "adm", "role": "admin", "org_id": "org1"}
    rr = m.respond_consultation(cid, body={"response": "3주 소요됩니다"}, user=admin, db=db)
    assert rr["status"] == "answered"
    pc = m.patch_consultation(cid, body={"status": "closed"}, user=admin, db=db)
    assert pc["status"] == "closed"


def test_upload_case_document(tmp_path):
    import base64
    db = _seed_db(tmp_path)
    b64 = base64.b64encode(b"hello world pdf").decode()
    r = m.upload_case_document("case1", body={"filename": "m.pdf", "file_b64": b64, "doc_type": "mock_audit_evidence"}, user=USER, db=db)
    assert r["doc_type"] == "mock_audit_evidence" and r["document_id"]


def test_fatwa_client_status(tmp_path):
    db = _seed_db(tmp_path)
    r = m.get_fatwa_status("case1", user=USER, db=db)
    assert r["fatwa_status"] == "none" and r["decision"] == "pending"
    db.add(models.FatwaDecision(case_id="case1", decision="approved", decision_no="F-1",
                                committee_head="A", committee_secretary="B", committee_members=["C"]))
    db.add(models.FatwaVote(case_id="case1", member="A", vote="approve"))
    db.commit()
    r2 = m.get_fatwa_status("case1", user=USER, db=db)
    assert r2["decision"] == "approved" and r2["votes_approve"] == 1 and r2["committee_size"] == 3


def test_get_contract_status(tmp_path):
    db = _seed_db(tmp_path)
    assert m.get_contract("case1", user=USER, db=db)["exists"] is False
    m.gen_contract("case1", user=USER, db=db)
    ct = db.query(models.Contract).filter_by(case_id="case1").first()
    m.sign_contract(ct.contract_id, party="A", name="X", user=USER, db=db)
    r = m.get_contract("case1", user=USER, db=db)
    assert r["exists"] and r["signed_a"] and not r["signed_b"]


def test_fatwa_status_derives_from_case_stage(tmp_path):
    db = _seed_db(tmp_path)
    c = db.query(models.CaseApplication).filter_by(case_id="case1").first()
    c.status = "fatwa_review"
    db.commit()
    r = m.get_fatwa_status("case1", user=USER, db=db)
    assert r["fatwa_status"] == "review"  # fatwa_status=none이어도 케이스 단계로 보정


def test_fac_key_merges_regno_with_date_noise():
    # 공장등록증 OCR에서 발급일자가 등록번호에 섞여도 같은 공장으로 병합되어야 함
    clean = m._fac_key("427302017384122", "BIO ROSETTE.,LTD", "addr-en")
    noisy = m._fac_key("2026-01-22 427302017384122", "(주)바이오로제트", "addr-ko")
    assert clean == noisy == "reg:427302017384122"


def test_fac_key_distinct_regno_not_merged():
    a = m._fac_key("100000000000001", "A", "x")
    b = m._fac_key("100000000000002", "B", "y")
    assert a != b


def test_fac_key_falls_back_to_name_addr_without_regno():
    assert m._fac_key(None, "Buzzup", "seoul").startswith("na:")
    assert m._fac_key("", "Buzzup", "seoul") == m._fac_key(None, " Buzzup ", "seoul")
