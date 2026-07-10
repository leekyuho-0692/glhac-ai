"""P2 배치 B 격리 테스트 — P2-4 인증서 할랄마크번호·파트와참조번호·전체 ZIP ·
P2-5 evaluation 3-state·행별 판정버튼·증거카운트·시정조치 자동생성.

주의(테스트 격리): tests 일괄 실행 시 공유 DB 상호오염 방지를 위해 app import 전에
고유 DB(glhac_p2b_test.db)를 강제 대입한다(setdefault 금지).
실행: <venv>/bin/python -m pytest tests/test_p2_batch_b.py -q
"""
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_p2b_test.db"   # 격리 강제
os.environ["GLHAC_DEV"] = "1"                                   # 데모 계정 시드

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app, _halal_mark_no  # noqa: E402
from app import models  # noqa: E402
from app.db import SessionLocal  # noqa: E402


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(tok):
    return {"Authorization": "Bearer " + tok}


def _seed_issued_case(c, admin):
    """인증서 발급 완료 케이스를 DB 직접 심기(발급 게이트 우회 — P2-4/P2-5 표시·조회 검증 목적)."""
    cid = c.post("/cases", json={"org_id": "org_demo", "company_name": "P2BatchB Co"},
                 headers=_h(admin)).json()["case_id"]
    db = SessionLocal()
    try:
        today = date.today()
        cert = models.HalalCertificate(
            case_id=cid, certificate_no="HC-P2BTEST", scope=["Produk A", "Produk B"],
            issue_date=str(today), expiry_date=str(today + timedelta(days=1400)),
            status="active")
        db.add(cert)
        db.add(models.FatwaDecision(case_id=cid, decision="approved",
                                    decision_no="FATWA-2026-777", decided_at=datetime.utcnow()))
        db.add(models.GeneratedDocument(case_id=cid, org_id="org_demo", doc_type="sjph_manual",
                                        version=1, content="SJPH 매뉴얼 본문", status="approved",
                                        created_by="admin"))
        db.add(models.GeneratedDocument(case_id=cid, org_id="org_demo", doc_type="audit_report",
                                        version=1, content="현장심사 보고서 본문", status="approved",
                                        created_by="admin"))
        db.commit()
        mark = _halal_mark_no(cert)
    finally:
        db.close()
    return cid, mark


def test_a_certificate_response_and_pdf_carry_fatwa_and_mark():
    """(a) 인증서 응답/PDF에 파트와 결정번호·할랄마크번호 포함."""
    import fitz  # PyMuPDF — PDF 텍스트 추출
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid, mark = _seed_issued_case(c, adm)
        # 응답(JSON)
        r = c.get(f"/cases/{cid}/certificate", headers=_h(adm)).json()
        assert r["issued"] is True, r
        assert r["fatwa_decision_no"] == "FATWA-2026-777", r
        assert r["halal_mark_no"] == mark and mark.startswith("ID"), r
        # PDF
        p = c.get(f"/cases/{cid}/certificate/pdf", headers=_h(adm))
        assert p.status_code == 200 and p.content[:4] == b"%PDF", p.status_code
        text = "".join(pg.get_text() for pg in fitz.open(stream=p.content, filetype="pdf"))
        assert "FATWA-2026-777" in text, "PDF에 파트와 결정번호 없음"
        assert mark in text, "PDF에 할랄마크번호 없음"
        assert "Ketetapan Halal" in text, "PDF에 할랄마크 라벨 없음"


def test_b_bundle_zip_download():
    """(b) 전체 ZIP 다운로드 200 + zip 시그니처(PK) + 인증서 PDF·생성문서 포함."""
    import io
    import zipfile
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid, _ = _seed_issued_case(c, adm)
        z = c.get(f"/cases/{cid}/certificate/bundle.zip", headers=_h(adm))
        assert z.status_code == 200, z.text
        assert z.content[:2] == b"PK", "zip 시그니처(PK) 아님"
        names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
        assert any(n.endswith(".pdf") for n in names), names          # 인증서 PDF
        assert any("sjph_manual" in n for n in names), names          # 생성문서 포함
        assert any("audit_report" in n for n in names), names


def test_c_evaluation_verdict_saved_and_car_created():
    """(c) evaluation 행별 판정 → verdict 저장(hpas-auto state3) + 시정조치(CAR) 자동생성."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        aud = _tok(c, "auditor1", "pw")
        cid, _ = _seed_issued_case(c, adm)
        # 부적합 판정 → CAR 생성
        rv = c.post(f"/cases/{cid}/evaluation/verdict",
                    json={"element": "materials", "verdict": "fail"}, headers=_h(aud))
        assert rv.status_code == 200, rv.text
        assert rv.json()["car_id"], rv.json()
        # 적합 판정(다른 항목)
        assert c.post(f"/cases/{cid}/evaluation/verdict",
                      json={"element": "product", "verdict": "good"},
                      headers=_h(aud)).status_code == 200
        # hpas-auto 3-state 반영
        h = c.get(f"/cases/{cid}/hpas-auto", headers=_h(aud)).json()
        st = {e["element"]: e for e in h["elements"]}
        assert st["materials"]["state3"] == "fail", st["materials"]
        assert st["product"]["state3"] == "good", st["product"]
        assert h["fail"] == 1, h
        assert "evidence_docs" in st["materials"] and "evidence_media" in st["materials"]
        # 시정조치 기록 조회
        cars = c.get(f"/cases/{cid}/corrective-actions", headers=_h(adm)).json()
        assert any(x["finding_id"] == "eval:materials" for x in cars), cars
        # 멱등: 동일 항목 재판정해도 CAR 중복 생성 안 됨
        rv2 = c.post(f"/cases/{cid}/evaluation/verdict",
                     json={"element": "materials", "verdict": "fail"}, headers=_h(aud))
        cars2 = c.get(f"/cases/{cid}/corrective-actions", headers=_h(adm)).json()
        assert sum(1 for x in cars2 if x["finding_id"] == "eval:materials") == 1, cars2
        assert rv2.json()["car_id"] == rv.json()["car_id"], (rv.json(), rv2.json())


def test_d_verdict_rbac_and_validation():
    """(d) 비권한 403 + 잘못된 element/verdict 422."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        ap = _tok(c, "applicant1", "pw")
        cons = _tok(c, "consultant1", "pw")
        aud = _tok(c, "auditor1", "pw")
        cid, _ = _seed_issued_case(c, adm)
        # 클라이언트/컨설턴트는 판정 불가(403) — 역할 게이트가 케이스 조회보다 먼저
        assert c.post(f"/cases/{cid}/evaluation/verdict",
                      json={"element": "materials", "verdict": "fail"},
                      headers=_h(ap)).status_code == 403
        assert c.post(f"/cases/{cid}/evaluation/verdict",
                      json={"element": "materials", "verdict": "fail"},
                      headers=_h(cons)).status_code == 403
        # 오디터: 잘못된 element/verdict → 422
        assert c.post(f"/cases/{cid}/evaluation/verdict",
                      json={"element": "nope", "verdict": "fail"},
                      headers=_h(aud)).status_code == 422
        assert c.post(f"/cases/{cid}/evaluation/verdict",
                      json={"element": "materials", "verdict": "maybe"},
                      headers=_h(aud)).status_code == 422


if __name__ == "__main__":
    tests = [test_a_certificate_response_and_pdf_carry_fatwa_and_mark,
             test_b_bundle_zip_download,
             test_c_evaluation_verdict_saved_and_car_created,
             test_d_verdict_rbac_and_validation]
    ok = 0
    for fn in tests:
        try:
            fn(); ok += 1; print("[PASS]", fn.__name__)
        except Exception as e:  # noqa
            import traceback
            traceback.print_exc()
            print("[FAIL]", fn.__name__, "—", e)
    print(f"\n{ok}/{len(tests)} passed")
    sys.exit(0 if ok == len(tests) else 1)
