"""A5 — KFPH(Komite Fatwa Produk Halal) 자기선언 할랄판정 트랙 격리 테스트.
TestClient 기반(서버 불필요). 실행: <venv>/bin/python -m pytest tests/test_kfph_track.py -q

격리(중요): tests 일괄 실행 시 여러 파일이 glhac_v3_test.db를 공유해 상호 오염되므로,
이 파일은 고유 DB(glhac_kfph_test.db)를 app import 전에 강제 대입한다(setdefault 금지).

검증:
 (a) committee 승인된 self_declare 케이스 ketetapan.pdf 200·%PDF / 미승인 409 NOT_APPROVED / reguler 409 NOT_SELF_DECLARE
 (b) ketetapan 미리보기에 Phase0 disclaimer(내부 서명·비공식·내부 참조) 포함
 (c) 비권한(무토큰) 접근 401
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_kfph_test.db"   # 격리 강제
os.environ["GLHAC_DEV"] = "1"   # 데모 계정 시드(테스트 전용)

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app import models  # noqa: E402
from app.db import SessionLocal  # noqa: E402


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(tok):
    return {"Authorization": "Bearer " + tok}


def _mkcase(c, admin, company="KfphCo"):
    return c.post("/cases", json={"org_id": "org_demo", "company_name": company},
                  headers=_h(admin)).json()["case_id"]


def _prep(case_id, pathway, status, add_product=False, **extra):
    """DB 직접 세팅 — pathway/status + (선택) 제품 1건 + 사업자 필드."""
    db = SessionLocal()
    try:
        c = db.get(models.CaseApplication, case_id)
        c.pathway = pathway
        c.status = status
        for k, v in extra.items():
            setattr(c, k, v)
        if add_product:
            db.add(models.Product(case_id=case_id, org_id=c.org_id,
                                  name="Kerupuk Halal", category="Makanan"))
        db.commit()
    finally:
        db.close()


def test_a_ketetapan_approved_selfdeclare_pdf():
    """(a): KFPH 승인된 self_declare → ketetapan.pdf 200·%PDF + GeneratedDocument 등록."""
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        cid = _mkcase(c, admin, "KfphUMK")
        _prep(cid, "self_declare", "committee_verification", add_product=True,
              nib="9988776655", responsible_person="Siti", address="Jl. UMK No.7")
        # KFPH 승인(committee.approve 이벤트 기록)
        r = c.post(f"/cases/{cid}/committee/decide",
                   json={"decision": "approve", "reason": "self-declare terverifikasi"},
                   headers=_h(admin))
        assert r.status_code == 200, r.text
        assert r.json()["fatwa_status"] == "approved"
        # ketetapan.pdf
        pdf = c.get(f"/cases/{cid}/committee/ketetapan.pdf", headers=_h(admin))
        assert pdf.status_code == 200, pdf.text
        assert pdf.content[:5] == b"%PDF-", pdf.content[:16]
        # GeneratedDocument(kfph_ketetapan) 등록 확인
        db = SessionLocal()
        try:
            g = db.query(models.GeneratedDocument).filter_by(
                case_id=cid, doc_type="kfph_ketetapan").first()
            assert g is not None
        finally:
            db.close()


def test_a_ketetapan_not_approved_409():
    """(a): 미승인 self_declare → 409 NOT_APPROVED."""
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        cid = _mkcase(c, admin, "KfphPending")
        _prep(cid, "self_declare", "committee_verification")
        r = c.get(f"/cases/{cid}/committee/ketetapan.pdf", headers=_h(admin))
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "NOT_APPROVED"


def test_a_ketetapan_reguler_409():
    """(a): reguler 경로 → 409 NOT_SELF_DECLARE."""
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        cid = _mkcase(c, admin, "KfphReguler")
        _prep(cid, "reguler", "fatwa_review")
        r = c.get(f"/cases/{cid}/committee/ketetapan.pdf", headers=_h(admin))
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "NOT_SELF_DECLARE"


def test_b_ketetapan_preview_phase0_disclaimer():
    """(b): 승인 후 미리보기에 Phase0 고지(내부 서명·비공식·내부 참조 번호) 포함."""
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        cid = _mkcase(c, admin, "KfphDisc")
        _prep(cid, "self_declare", "committee_verification", add_product=True,
              nib="1112223334", responsible_person="Budi")
        c.post(f"/cases/{cid}/committee/decide", json={"decision": "approve", "reason": "ok"},
               headers=_h(admin))
        pv = c.get(f"/cases/{cid}/committee/ketetapan/preview", headers=_h(admin))
        assert pv.status_code == 200, pv.text
        body = pv.text
        assert "내부 무결성 서명" in body          # _SIG_NATURE (공인 전자서명 아님)
        assert "비공식" in body                    # 내부 참조·비공식 결정번호
        assert "KFPH/INT/" in body                 # 내부 참조번호 파생값
        assert "Komite Fatwa Produk Halal (KFPH)" in body
        assert "BPJPH/SIHALAL" in body             # _LEGAL_DISCLAIMER_PDF


def test_c_ketetapan_unauthorized_401():
    """(c): 비권한(무토큰) 접근 → 401."""
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        cid = _mkcase(c, admin, "KfphAuth")
        _prep(cid, "self_declare", "committee_verification")
        c.post(f"/cases/{cid}/committee/decide", json={"decision": "approve", "reason": "ok"},
               headers=_h(admin))
        r = c.get(f"/cases/{cid}/committee/ketetapan.pdf")   # 토큰 없음
        assert r.status_code == 401, r.text
