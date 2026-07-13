"""법적효력 Phase 0(법적 포지셔닝·고지) 격리 테스트.

검증 항목:
  (a) 공식 BPJPH 번호 미import 시 인증서 응답 halal_no_is_official=False + 내부참조 라벨(자체값은 유지).
  (b) certificate_number_imported 이벤트 주입 시 official_bpjph_no 반영 + is_official=True.
  (c) /verify 응답에 disclaimer·서명 성격(signature_nature) 필드 존재.

격리: app import 전에 고유 DB를 강제 대입(공유 DB 오염 방지).
실행: <venv>/bin/python -m pytest tests/test_legal_phase0.py -q
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_lp0_test.db"   # 격리 강제
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
    cid = c.post("/cases", json={"org_id": "org_demo", "company_name": "LegalP0 Co"},
                 headers=_h(admin)).json()["case_id"]
    db = SessionLocal()
    try:
        today = date.today()
        token = "lp0_" + cid[:12]   # 케이스별 고유 토큰(공개검증 조회 충돌 방지)
        cert = models.HalalCertificate(
            case_id=cid, certificate_no="HC-LP0", scope=["Produk X"],
            issue_date=str(today), expiry_date=str(today + timedelta(days=1400)),
            status="active", qr_token=token)
        db.add(cert)
        db.commit()
        mark = _halal_mark_no(cert)
    finally:
        db.close()
    return cid, mark, token


def test_a_unofficial_when_no_import():
    """(a) 공식번호 미import → is_official=False, 자체값(내부참조)은 halal_mark_no로 유지."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid, mark, _tok_ = _seed_issued_case(c, adm)
        r = c.get(f"/cases/{cid}/certificate", headers=_h(adm)).json()
        assert r["issued"] is True, r
        assert r["halal_no_is_official"] is False, r
        assert r["official_bpjph_no"] is None, r
        assert r["halal_mark_no"] == mark and mark.startswith("ID"), r
        # PDF에 '내부 참조번호(비공식)' 라벨 + 고지 문구
        import fitz
        p = c.get(f"/cases/{cid}/certificate/pdf", headers=_h(adm))
        assert p.status_code == 200 and p.content[:4] == b"%PDF"
        text = "".join(pg.get_text() for pg in fitz.open(stream=p.content, filetype="pdf"))
        assert "Internal Ref" in text, "PDF에 내부참조(비공식) 라벨 없음"
        assert "BPJPH/SIHALAL" in text, "PDF에 고지 문구 없음"


def test_b_official_when_imported():
    """(b) certificate_number_imported 이벤트 주입 → official_bpjph_no 반영 + is_official=True."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        oper = _tok(c, "operator1", "pw")
        cid, _mk, token = _seed_issued_case(c, adm)
        ev = c.post("/integration/sihalal/event", headers=_h(oper), json={
            "event_type": "certificate_number_imported", "external_id": "SIH-1",
            "idempotency_key": "lp0-import-1", "case_id": cid,
            "payload": {"certificate_number": "ID00110012026000123"}})
        assert ev.status_code == 200, ev.text
        r = c.get(f"/cases/{cid}/certificate", headers=_h(adm)).json()
        assert r["halal_no_is_official"] is True, r
        assert r["official_bpjph_no"] == "ID00110012026000123", r
        # PDF에 공식 라벨 반영
        import fitz
        p = c.get(f"/cases/{cid}/certificate/pdf", headers=_h(adm))
        text = "".join(pg.get_text() for pg in fitz.open(stream=p.content, filetype="pdf"))
        assert "No. Ketetapan Halal (BPJPH)" in text, text[:400]
        assert "ID00110012026000123" in text, "PDF에 공식번호 미반영"


def test_c_verify_has_disclaimer_and_sig_nature():
    """(c) /verify 응답에 disclaimer·signature 성격 필드 존재."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid, _mk, token = _seed_issued_case(c, adm)
        # 서명 부착(내부 HMAC)
        c.post(f"/cases/{cid}/certificate/sign", headers=_h(adm))
        r = c.get("/verify/" + token)
        assert r.status_code == 200, r.text
        d = r.json()
        assert "disclaimer" in d and "BPJPH/SIHALAL" in d["disclaimer"], d
        assert "signature_nature" in d and d["signature_nature"], d
        assert "PSrE" in d["signature_nature"], d
        assert "signature_valid_meaning" in d, d
        assert d["halal_no_is_official"] is False, d


if __name__ == "__main__":
    tests = [test_a_unofficial_when_no_import, test_b_official_when_imported,
             test_c_verify_has_disclaimer_and_sig_nature]
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
