"""SJPH/HPAS Manual — 공식 템플릿(GLHAC HPAS SJPH Template) 정합 검증.
격리 임시DB(고유명), 서버 불필요. 실행: <venv>/bin/python -m pytest tests/test_sjph_manual.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_sjph_manual_test.db"

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.main as m  # noqa: E402
import app.models as models  # noqa: E402

USER = {"uid": "u1", "role": "applicant", "org_id": "orgSJPH"}
COMPANY = "Nusantara Halal Foods Co., Ltd."
SUP = "Ahmad Supervisor"


def _seed(tmp_path):
    eng = create_engine("sqlite:///%s" % (tmp_path / "sjph.db"))
    models.Base.metadata.create_all(bind=eng)
    db = sessionmaker(bind=eng)()
    db.add(models.Org(org_id="orgSJPH", name="Nusantara", address="Seoul, Korea"))
    db.add(models.CaseApplication(
        case_id="caseSJPH", org_id="orgSJPH", company_name=COMPANY,
        nib="99-88-77660", responsible_person="Kim Dae-hyun", halal_supervisor=SUP,
        address="Seoul, Republic of Korea", factory_address="Incheon, Korea",
        email="halal@nusantara.co", phone="010-9988-7766", pathway="reguler",
        profile_ext={"pic_name": "박PIC", "pic_title": "부장", "cp_name": "이CP", "cp_title": "대리",
                     "registration_type": "Manufacturing", "application_type": "Regular",
                     "registration_status": "New", "production_capacity": "10,000 units/day"}))
    db.add(models.PenyeliaHalal(penyelia_id="pen1", org_id="orgSJPH", name=SUP, status="active"))
    db.add(models.Product(product_id="prodS1", case_id="caseSJPH", org_id="orgSJPH",
                          name="Halal Chili Sauce", category="Sauce", registration_type="new"))
    db.add(models.Material(material_id="matS1", case_id="caseSJPH", name="Palm Oil",
                           mat_type="raw", supplier="ABC Oils", screen_status="halal"))
    db.add(models.Material(material_id="matS2", case_id="caseSJPH", name="Gelatin",
                           mat_type="additive", supplier="XYZ Gel", screen_status="mushbooh",
                           screen_result="NEEDS_EVIDENCE"))
    db.commit()
    return db


def test_sjph_manual_gendoc_saved_with_data(tmp_path):
    """POST 매뉴얼: gen-doc 저장 + 회사명·감독관명·재료·제품이 템플릿 구조 텍스트에 포함."""
    db = _seed(tmp_path)
    r = m.sjph_manual("caseSJPH", USER, db)
    assert r["version"] == 1 and r["gen_doc_id"] and r["blocks"] > 30
    doc = r["manual"]
    for expect in [COMPANY, SUP, "Kim Dae-hyun", "LEGAL BASIS", "PURPOSE AND SCOPE",
                   "Halal Commitment", "Raw Materials", "Palm Oil", "Gelatin",
                   "Halal Chili Sauce", "Pork-Free Statement", "Appendices"]:
        assert expect in doc, "누락: %s" % expect
    # gen-doc DB 저장 확인
    g = db.query(models.GeneratedDocument).filter_by(case_id="caseSJPH", doc_type="sjph_manual").first()
    assert g is not None and COMPANY in g.content


def test_sjph_manual_blocks_structure(tmp_path):
    """블록 구조: 표지 kv(회사명)·서명·법적근거 8건·재료표에 감독관/회사/재료 반영."""
    db = _seed(tmp_path)
    c = m._get_case(db, "caseSJPH", USER)
    blocks = m._sjph_manual_blocks(db, c)
    headings = [b["text"] for b in blocks if b.get("type") == "heading"]
    assert any("LEGAL BASIS" in h for h in headings)
    assert any("PURPOSE AND SCOPE" in h for h in headings)
    assert any(h.startswith("1. Halal Commitment") for h in headings)
    assert any(h.startswith("2. Raw Materials") for h in headings)
    assert any(h.startswith("3. Halal Production Process") for h in headings)
    assert any(h.startswith("4. Product Criteria") for h in headings)
    assert any(h.startswith("5. Monitoring") for h in headings)
    assert any("Appendices" in h for h in headings)
    # 회사명 표지 kv
    assert any(b.get("type") == "kv" and b.get("value") == COMPANY for b in blocks)
    # 재료표에 재료·생산자·판정 반영
    mat_tbl = [b for b in blocks if b.get("type") == "table"
               and b.get("headers", [None])[1] == "Material / 재료명"]
    assert mat_tbl, "재료 표 없음"
    flat = str(mat_tbl[0]["rows"])
    assert "Palm Oil" in flat and "ABC Oils" in flat and "Gelatin" in flat
    # 법적근거 8건
    assert len(m.SJPH_LEGAL_BASIS) == 8


def test_sjph_manual_pdf_renders(tmp_path):
    """GET PDF: 200 + %PDF 매직바이트."""
    db = _seed(tmp_path)
    resp = m.get_sjph_manual_pdf("caseSJPH", USER, db)
    assert resp.media_type == "application/pdf"
    assert resp.body[:4] == b"%PDF" and len(resp.body) > 3000


def test_sjph_manual_empty_data_no_fabrication(tmp_path):
    """데이터 없으면 빈칸('—') 표기 — 날조 금지."""
    eng = create_engine("sqlite:///%s" % (tmp_path / "sjph_empty.db"))
    models.Base.metadata.create_all(bind=eng)
    db = sessionmaker(bind=eng)()
    db.add(models.Org(org_id="orgSJPH", name="Empty"))
    db.add(models.CaseApplication(case_id="caseEmpty", org_id="orgSJPH", company_name="Empty Co"))
    db.commit()
    c = m._get_case(db, "caseEmpty", USER)
    blocks = m._sjph_manual_blocks(db, c)
    # 재료 없음 → placeholder 행
    mat_tbl = [b for b in blocks if b.get("type") == "table"
               and b.get("headers", [None])[1] == "Material / 재료명"][0]
    assert mat_tbl["rows"] == [["—", "—", "—", "—", "—"]]
    # PDF도 정상 렌더
    resp = m.get_sjph_manual_pdf("caseEmpty", USER, db)
    assert resp.body[:4] == b"%PDF"


if __name__ == "__main__":
    import tempfile
    from pathlib import Path
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            with tempfile.TemporaryDirectory() as d:
                try:
                    fn(Path(d))
                    print("PASS", name)
                except Exception as e:
                    fails += 1
                    print("FAIL", name, "->", repr(e))
    print("=" * 40)
    print("ALL PASS" if fails == 0 else "%d FAILED" % fails)
    sys.exit(1 if fails else 0)
