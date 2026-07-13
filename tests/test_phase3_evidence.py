"""법적효력 Phase 3 — 증거능력(evidentiary) 하드닝 격리 테스트.

정직성 원칙: TSA 신뢰 타임스탬프 자격증명 미설정(기본값)이면 서명/발급 이벤트는 내부 시각만
사용하며 기존 동작이 100% 그대로여야 한다(회귀 0). 자격증명 설정 시에만 meta에 TSA 스탬프가 병기된다.

검증 항목:
  (a) 접근로그 여러 건 후 /admin/audit-log-verify integrity_ok=true · AuditLog meta._chain 존재
  (b) AuditLog meta._chain.row 변조 시 integrity_ok=false · break_at 반환(후 원복)
  (c) /cases/{id}/evidence-bundle.zip 200 · PK 시그니처 · integrity_report(양 체인) 포함
  (d) fatwa_final_approve 감사 이벤트 from_status=fatwa_review(정확화 확인)
  (e) TSA 미설정 시 /admin/tsa-status configured=false · 비권한 403

격리: app import 전에 고유 DB 강제 대입 + TSA env 미설정 보장(공유 오염·과장 방지).
실행: <venv>/bin/python -m pytest tests/test_phase3_evidence.py -q
"""
import io
import json
import os
import sys
import zipfile
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_p3ev_test.db"   # 격리 강제
os.environ["GLHAC_DEV"] = "1"                                    # 데모 계정 시드
# TSA 미설정 보장(우연히 남은 자격증명 제거) — 기본=신뢰 타임스탬프 미연동(내부 시각) 검증
for _k in ("GLHAC_TSA_URL", "GLHAC_TSA_TOKEN"):
    os.environ.pop(_k, None)

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app, _tsa_config  # noqa: E402
from app import models  # noqa: E402
from app.db import SessionLocal  # noqa: E402


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(tok):
    return {"Authorization": "Bearer " + tok}


def _new_case(c, admin, name="Evidence Co"):
    return c.post("/cases", json={"org_id": "org_demo", "company_name": name},
                  headers=_h(admin)).json()["case_id"]


def test_a_audit_chain_ok_and_meta_chain_present():
    """(a) 접근로그 여러 건 후 audit-log-verify integrity_ok=true · meta._chain 존재."""
    assert _tsa_config()["configured"] is False, "테스트 전제: TSA 미설정"
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid = _new_case(c, adm)
        for _ in range(4):
            assert c.get(f"/cases/{cid}", headers=_h(adm)).status_code == 200
        v = c.get("/admin/audit-log-verify", headers=_h(adm))
        assert v.status_code == 200, v.text
        js = v.json()
        assert js["integrity_ok"] is True, js
        assert js["checked"] >= 4, js
        assert js["break_at"] is None, js
        # meta._chain 존재 확인(DB 직접)
        db = SessionLocal()
        try:
            row = (db.query(models.AuditLog).filter_by(case_id=cid, action="case.read")
                   .order_by(models.AuditLog.created_at.desc()).first())
            assert row is not None and isinstance(row.meta, dict), row
            assert "_chain" in row.meta and "row" in row.meta["_chain"], row.meta
        finally:
            db.close()


def test_b_tamper_breaks_integrity():
    """(b) AuditLog meta._chain.row 변조 시 integrity_ok=false·break_at (검증 후 원복)."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        cid = _new_case(c, adm)
        for _ in range(3):
            c.get(f"/cases/{cid}", headers=_h(adm))
        db = SessionLocal()
        try:
            row = (db.query(models.AuditLog).filter_by(case_id=cid, action="case.read")
                   .order_by(models.AuditLog.created_at).first())
            orig = dict(row.meta)
            tampered = dict(row.meta)
            tampered["_chain"] = dict(tampered["_chain"], row="DEADBEEF" + tampered["_chain"]["row"])
            row.meta = tampered
            db.add(row)
            db.commit()
            rid = row.id
        finally:
            db.close()
        # case 필터로 해당 케이스 체인만 검증 → 변조 감지
        v = c.get("/admin/audit-log-verify", params={"case": cid}, headers=_h(adm))
        assert v.status_code == 200, v.text
        js = v.json()
        assert js["integrity_ok"] is False, js
        assert js["break_at"] == rid, js
        # 원복(전역 체인 오염 방지)
        db = SessionLocal()
        try:
            r2 = db.get(models.AuditLog, rid)
            r2.meta = orig
            db.add(r2)
            db.commit()
        finally:
            db.close()


def test_c_evidence_bundle_zip():
    """(c) evidence-bundle.zip 200 · PK 시그니처 · integrity_report(양 체인) 포함."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        oper = _tok(c, "operator1", "pw")
        cid = _new_case(c, adm)
        c.get(f"/cases/{cid}", headers=_h(adm))
        r = c.get(f"/cases/{cid}/evidence-bundle.zip", headers=_h(oper))
        assert r.status_code == 200, r.text
        assert r.content[:2] == b"PK", "zip PK 시그니처 아님"
        z = zipfile.ZipFile(io.BytesIO(r.content))
        names = z.namelist()
        assert "evidence.json" in names and "integrity_report.json" in names, names
        assert "README.txt" in names, names
        rep = json.loads(z.read("integrity_report.json"))
        assert "workflow_event_chain" in rep and "audit_log_chain" in rep, rep
        assert rep["workflow_event_chain"]["integrity_ok"] is True, rep
        assert rep["audit_log_chain"]["integrity_ok"] is True, rep
        assert "disclaimer" in rep and rep["disclaimer"], rep
        ev = json.loads(z.read("evidence.json"))
        assert ev["case"]["case_id"] == cid, ev


def test_d_final_approve_from_status_accurate():
    """(d) fatwa_final_approve 감사 이벤트 from_status=fatwa_review(선세팅 전 캡처 확인)."""
    with TestClient(app) as c:
        adm = _tok(c, "admin", "admin")
        oper = _tok(c, "operator1", "pw")
        cid = _new_case(c, adm, "Fatwa From Co")
        # 케이스를 fatwa_review + 가승인(provisional) 상태로 직접 진입
        db = SessionLocal()
        try:
            case = db.get(models.CaseApplication, cid)
            case.status = "fatwa_review"
            case.fatwa_status = "provisional"
            db.add(models.FatwaDecision(case_id=cid, decision="approved"))
            db.commit()
        finally:
            db.close()
        r = c.post(f"/cases/{cid}/fatwa/final-approve", headers=_h(oper))
        assert r.status_code == 200, r.text
        # 감사(WorkflowEvent)에 from=fatwa_review로 정확히 기록됐는지
        db = SessionLocal()
        try:
            ev = (db.query(models.WorkflowEvent)
                  .filter_by(case_id=cid, action="fatwa.final_approve").first())
            assert ev is not None, "final_approve 이벤트 없음"
            assert ev.from_status == "fatwa_review", ev.from_status
        finally:
            db.close()


def test_e_tsa_status_and_forbidden():
    """(e) TSA 미설정 시 tsa-status configured=false · 비권한 403."""
    with TestClient(app) as c:
        oper = _tok(c, "operator1", "pw")
        appl = _tok(c, "applicant1", "pw")
        s = c.get("/admin/tsa-status", headers=_h(oper))
        assert s.status_code == 200, s.text
        tj = s.json()["tsa"]
        assert tj["configured"] is False and tj["status"] == "unset", tj
        # 비권한: audit-log-verify(admin 전용) + evidence-bundle(operator/admin) → applicant 403
        assert c.get("/admin/audit-log-verify", headers=_h(appl)).status_code == 403
        adm = _tok(c, "admin", "admin")
        cid = _new_case(c, adm, "Perm Co")
        assert c.get(f"/cases/{cid}/evidence-bundle.zip", headers=_h(appl)).status_code == 403


if __name__ == "__main__":
    tests = [test_a_audit_chain_ok_and_meta_chain_present, test_b_tamper_breaks_integrity,
             test_c_evidence_bundle_zip, test_d_final_approve_from_status_accurate,
             test_e_tsa_status_and_forbidden]
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
