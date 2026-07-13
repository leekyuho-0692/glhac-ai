"""SEHATI Layer B — SIHALAL 제출 커넥터 격리 테스트(자격증명 미설정 상태).

정직성 검증: SIHALAL env 미설정 시 실제 제출을 '척'하지 않고 424 no_credentials로 정직 폴백하며
제출 의도(sihalal.submit_intent)만 감사 기록한다. import-number → certificate_number_imported →
_official_bpjph_no 반영(Phase0↔Layer B 브릿지)까지 검증한다.

격리(중요): tests 일괄 실행 시 여러 파일이 공용 DB를 나눠 써 상호 오염되므로, 이 파일은 고유
DB(glhac_sihsub_test.db)를 app import 전에 강제 대입한다(setdefault 금지). SIHALAL env는 미설정.
실행: <venv>/bin/python -m pytest tests/test_sihalal_submit.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_sihsub_test.db"   # 격리 강제
os.environ["GLHAC_DEV"] = "1"                                     # 데모 계정 시드(테스트 전용)
# SIHALAL 자격증명 '미설정' 상태 보장(정직 폴백 검증 목적)
for _k in ("GLHAC_SIHALAL_API_URL", "GLHAC_SIHALAL_TOKEN", "GLHAC_SIHALAL_API_KEY"):
    os.environ.pop(_k, None)

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app import models, state_machine as sm  # noqa: E402
from app.db import SessionLocal  # noqa: E402


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(tok):
    return {"Authorization": "Bearer " + tok}


def _mkcase(c, admin, company="SihSubCo"):
    return c.post("/cases", json={"org_id": "org_demo", "company_name": company},
                  headers=_h(admin)).json()["case_id"]


def _set_pathway(case_id, pathway, **extra):
    db = SessionLocal()
    try:
        c = db.get(models.CaseApplication, case_id)
        c.pathway = pathway
        for k, v in extra.items():
            setattr(c, k, v)
        db.commit()
    finally:
        db.close()


def _kfph_approve(case_id):
    """committee.approve WorkflowEvent 직접 기록(KFPH 승인 상태 구성)."""
    db = SessionLocal()
    try:
        c = db.get(models.CaseApplication, case_id)
        sm.record_event(db, c, c.status, c.status, "committee.approve",
                        "fatwa_liaison", "fatwa1", {"reason": "ok"})
        db.commit()
    finally:
        db.close()


def _add_active_cert(case_id, cert_no="HC-SIH-1"):
    db = SessionLocal()
    try:
        db.add(models.HalalCertificate(case_id=case_id, certificate_no=cert_no,
                                        status="active", scope=[]))
        db.commit()
    finally:
        db.close()


def test_a_submit_not_configured_424_and_intent_event():
    """(a) KFPH 승인 self_declare 케이스 submit → SIHALAL 미설정이라 424 no_credentials·의도이벤트 기록."""
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        pend = _tok(c, "pendamping1", "pw")
        cid = _mkcase(c, admin, "SehatiSubmitCo")
        _set_pathway(cid, "self_declare", nib="1234567890", address="Jl. Halal 1",
                     responsible_person="Budi")
        _kfph_approve(cid)

        r = c.post(f"/cases/{cid}/sihalal/submit", headers=_h(pend))
        assert r.status_code == 424, r.text
        j = r.json()
        assert j["ok"] is False and j["reason"] == "no_credentials", j
        assert j["code"] == "SIHALAL_NOT_CONFIGURED", j

        # 제출 의도(sihalal.submit_intent) 정직 기록 확인
        db = SessionLocal()
        try:
            n = (db.query(models.WorkflowEvent)
                 .filter_by(case_id=cid, action="sihalal.submit_intent").count())
            assert n == 1, n
            # 미설정이므로 실제 제출(document_package_submitted) 이벤트는 없어야 함(위조 금지)
            sub = (db.query(models.IntegrationEvent)
                   .filter_by(case_id=cid, event_type="document_package_submitted").count())
            assert sub == 0, sub
        finally:
            db.close()

        # status 엔드포인트도 정직 표기
        st = c.get(f"/cases/{cid}/sihalal/status", headers=_h(pend)).json()
        assert st["configured"] is False and st["submitted"] is False, st
        assert st["status"] == "not_configured", st


def test_b_guard_409_not_self_declare_and_not_approved():
    """(b) reguler → 409 NOT_SELF_DECLARE, self_declare 미승인 → 409 NOT_APPROVED."""
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        ops = _tok(c, "operator1", "pw")

        rg = _mkcase(c, admin, "RegulerSub")
        _set_pathway(rg, "reguler")
        r1 = c.post(f"/cases/{rg}/sihalal/submit", headers=_h(ops))
        assert r1.status_code == 409, r1.text
        assert r1.json()["detail"]["code"] == "NOT_SELF_DECLARE", r1.text

        sd = _mkcase(c, admin, "SdNoApprove")
        _set_pathway(sd, "self_declare")   # KFPH 승인 이벤트 없음
        r2 = c.post(f"/cases/{sd}/sihalal/submit", headers=_h(ops))
        assert r2.status_code == 409, r2.text
        assert r2.json()["detail"]["code"] == "NOT_APPROVED", r2.text


def test_c_import_number_phase0_bridge():
    """(c) import-number → certificate_number_imported 이벤트 → _official_bpjph_no 반영 →
    인증서 응답 halal_no_is_official=true 전환(Phase0↔Layer B 브릿지)."""
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        ops = _tok(c, "operator1", "pw")
        cid = _mkcase(c, admin, "ImportNoCo")
        _set_pathway(cid, "self_declare")
        _add_active_cert(cid, "HC-IMP-1")

        # import 전: 비공식
        before = c.get(f"/cases/{cid}/certificate", headers=_h(ops)).json()
        assert before["halal_no_is_official"] is False, before

        official = "ID12345678901234"
        r = c.post(f"/cases/{cid}/sihalal/import-number",
                   json={"official_no": official}, headers=_h(ops))
        assert r.status_code == 200, r.text
        assert r.json()["official_no"] == official, r.text
        assert r.json()["halal_no_is_official"] is True, r.text

        # certificate_number_imported IntegrationEvent 기록 확인
        db = SessionLocal()
        try:
            n = (db.query(models.IntegrationEvent)
                 .filter_by(case_id=cid, event_type="certificate_number_imported").count())
            assert n == 1, n
        finally:
            db.close()

        # import 후: 공식 라벨 전환
        after = c.get(f"/cases/{cid}/certificate", headers=_h(ops)).json()
        assert after["halal_no_is_official"] is True, after
        assert after["official_bpjph_no"] == official, after

        st = c.get(f"/cases/{cid}/sihalal/status", headers=_h(ops)).json()
        assert st["official_no"] == official and st["status"] == "official_received", st

        # 형식검증 최소: 너무 짧은 번호는 422
        bad = c.post(f"/cases/{cid}/sihalal/import-number",
                     json={"official_no": "x"}, headers=_h(ops))
        assert bad.status_code == 422, bad.text


def test_d_rbac_forbidden():
    """(d) 비권한 403 — submit(applicant)·import-number(consultant)."""
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        applicant = _tok(c, "applicant1", "pw")
        consultant = _tok(c, "consultant1", "pw")
        cid = _mkcase(c, admin, "RbacSubCo")
        _set_pathway(cid, "self_declare")
        _kfph_approve(cid)

        f1 = c.post(f"/cases/{cid}/sihalal/submit", headers=_h(applicant))
        assert f1.status_code == 403, f1.text

        f2 = c.post(f"/cases/{cid}/sihalal/import-number",
                    json={"official_no": "ID999999999999"}, headers=_h(consultant))
        assert f2.status_code == 403, f2.text
