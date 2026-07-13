"""법적효력 Phase 1 — PSrE 공인 전자서명 커넥터 격리 테스트.

정직성 원칙: PSrE 자격증명 미설정(기본값)이면 기존 내부 무결성 서명(HMAC)으로 폴백하며,
서명/PDF/응답이 100% 그대로여야 한다(회귀 0). 자격증명 설정 시에만 signature.certified 이벤트로
공인 서명 성격이 표기된다.

검증 항목:
  (a) PSrE 미설정 + 서명 후 _sig_nature_for가 내부 _SIG_NATURE 반환(PDF/응답 불변) · admin/psre-status configured=false
  (b) signature.certified 이벤트 없을 때 내부 표기(_SIG_NATURE) 반환
  (c) signature.certified 이벤트를 직접 주입하면 _sig_nature_for가 PSrE 공인 표기 반환(설정 시 동작 검증)
  (d) admin/psre-status 비권한(applicant) 403

격리: app import 전에 고유 DB 강제 대입 + PSrE env 미설정 보장(공유 오염·과장 방지).
실행: <venv>/bin/python -m pytest tests/test_psre_sign.py -q
"""
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_psre_test.db"   # 격리 강제
os.environ["GLHAC_DEV"] = "1"                                    # 데모 계정 시드
# PSrE 미설정 보장(테스트 환경에 우연히 남은 자격증명 제거) — 기본=내부 서명 폴백 검증
for _k in ("GLHAC_PSRE_PROVIDER", "GLHAC_PSRE_API_URL", "GLHAC_PSRE_TOKEN", "GLHAC_PSRE_API_KEY"):
    os.environ.pop(_k, None)

from fastapi.testclient import TestClient  # noqa: E402
from app.main import (app, _halal_mark_no, _sig_nature_for, _SIG_NATURE,  # noqa: E402
                      _psre_config, _PSRE_CERTIFIED_EVENT)
from app import models  # noqa: E402
from app.db import SessionLocal  # noqa: E402


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(tok):
    return {"Authorization": "Bearer " + tok}


def _seed_issued_case(c, admin):
    cid = c.post("/cases", json={"org_id": "org_demo", "company_name": "PSrE Co"},
                 headers=_h(admin)).json()["case_id"]
    db = SessionLocal()
    try:
        today = date.today()
        token = "psre_" + cid[:12]
        cert = models.HalalCertificate(
            case_id=cid, certificate_no="HC-PSRE", scope=["Produk Y"],
            issue_date=str(today), expiry_date=str(today + timedelta(days=1400)),
            status="active", qr_token=token)
        db.add(cert)
        db.commit()
        mark = _halal_mark_no(cert)
    finally:
        db.close()
    return cid, mark, token


def _inject_certified(case_id, subject="certificate", provider="PrivyID"):
    """signature.certified 이벤트 직접 주입(설정 시 동작 시뮬레이션)."""
    db = SessionLocal()
    try:
        ev = models.WorkflowEvent(
            case_id=case_id, from_status="", to_status="", action=_PSRE_CERTIFIED_EVENT,
            actor_type="system", actor_id="tester",
            payload={"subject": subject, "provider": provider,
                     "signer": "PIC Halal", "tsa_timestamp": datetime.utcnow().isoformat(),
                     "signature_ref": "PSRE-REF-1"})
        db.add(ev)
        db.commit()
    finally:
        db.close()


def test_a_unset_keeps_internal_signature():
    """(a) PSrE 미설정 + 서명 후 _sig_nature_for=내부표기, PDF/응답 불변, admin/psre-status configured=false."""
    assert _psre_config()["configured"] is False, "테스트 전제: PSrE 미설정"
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        oper = _tok(c, "operator1", "pw")
        cid, _mk, token = _seed_issued_case(c, adm)
        # 내부 HMAC 서명(기존 동작)
        r = c.post(f"/cases/{cid}/certificate/sign", headers=_h(adm))
        assert r.status_code == 200, r.text
        # _sig_nature_for는 certified 이벤트 부재 → 기존 _SIG_NATURE 그대로(회귀 0)
        db = SessionLocal()
        try:
            assert _sig_nature_for(db, cid, "certificate") == _SIG_NATURE
        finally:
            db.close()
        # verify 응답: 기존 내부 표기 유지("PSrE ... a닌" 문구 안에 PSrE 포함), 공식성 false
        d = c.get("/verify/" + token).json()
        assert d["signature_nature"] == _SIG_NATURE, d
        # PDF: 내부 무결성 서명 문구 존재(공인 표기 없음)
        import fitz
        p = c.get(f"/cases/{cid}/certificate/pdf", headers=_h(adm))
        assert p.status_code == 200 and p.content[:4] == b"%PDF"
        text = "".join(pg.get_text() for pg in fitz.open(stream=p.content, filetype="pdf"))
        assert "Internal integrity signature" in text, text[:400]
        assert "PSrE 공인 전자서명" not in text, "미설정인데 공인 표기가 노출됨(과장 금지)"
        # admin/psre-status
        s = c.get("/admin/psre-status", headers=_h(oper))
        assert s.status_code == 200, s.text
        js = s.json()["psre"]
        assert js["configured"] is False and js["status"] == "unset", js
        assert js.get("provider") in (None, ""), js


def test_b_no_event_internal_notation():
    """(b) signature.certified 이벤트 없을 때 _sig_nature_for는 내부 표기 반환."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid, _mk, _t = _seed_issued_case(c, adm)
        db = SessionLocal()
        try:
            assert _sig_nature_for(db, cid) == _SIG_NATURE
            assert _sig_nature_for(db, cid, "self_declaration") == _SIG_NATURE
        finally:
            db.close()


def test_c_injected_event_certified_notation():
    """(c) signature.certified 이벤트 주입 시 _sig_nature_for가 PSrE 공인 표기 반환(설정 시 동작 검증)."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid, _mk, _t = _seed_issued_case(c, adm)
        _inject_certified(cid, "certificate", provider="PrivyID")
        db = SessionLocal()
        try:
            nat = _sig_nature_for(db, cid, "certificate")
        finally:
            db.close()
        assert "PSrE 공인 전자서명" in nat, nat
        assert "PrivyID" in nat, nat
        assert "UU ITE" in nat, nat
        assert nat != _SIG_NATURE, nat


def test_d_psre_status_forbidden_for_non_operator():
    """(d) admin/psre-status 비권한(applicant) 403."""
    with TestClient(app) as c:
        app_tok = _tok(c, "applicant1", "pw")
        r = c.get("/admin/psre-status", headers=_h(app_tok))
        assert r.status_code == 403, r.text


if __name__ == "__main__":
    tests = [test_a_unset_keeps_internal_signature, test_b_no_event_internal_notation,
             test_c_injected_event_certified_notation, test_d_psre_status_forbidden_for_non_operator]
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
