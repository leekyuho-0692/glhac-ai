"""Layer A — SEHATI 자기선언 촉진 도구(A1 자기선언서 · A2 동반자 워크스페이스 · A3 온보딩) 격리 테스트.
TestClient 기반(서버 불필요). 실행: <venv>/bin/python -m pytest tests/test_sehati_layerA.py -q

격리(중요): tests 일괄 실행 시 여러 파일이 glhac_v3_test.db를 공유해 상호 오염되므로,
이 파일은 고유 DB(glhac_sehati_test.db)를 app import 전에 강제 대입한다(setdefault 금지).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_sehati_test.db"   # 격리 강제
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


def _mkcase(c, admin, company="SehatiCo"):
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


def test_a1_self_declaration_pdf_and_disclaimer():
    """(a)+(c): self_declare 케이스 → PDF 200·%PDF, 미리보기에 내부서명 disclaimer 포함, reguler는 400."""
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        # self_declare 케이스
        sd = _mkcase(c, admin, "SehatiUMK")
        _set_pathway(sd, "self_declare", nib="1234567890", responsible_person="Budi",
                     address="Jl. Halal No.1")
        r = c.get(f"/cases/{sd}/self-declaration.pdf", headers=_h(admin))
        assert r.status_code == 200, r.text
        assert r.content[:5] == b"%PDF-", r.content[:16]
        # (c) 내부서명 고지 — 미리보기 HTML에서 확인(공인 전자서명 아님)
        pv = c.get(f"/cases/{sd}/self-declaration/preview", headers=_h(admin))
        assert pv.status_code == 200, pv.text
        assert "내부 무결성 서명" in pv.text, pv.text[:400]
        assert "SURAT PERNYATAAN" in pv.text
        # GeneratedDocument 등록 확인
        db = SessionLocal()
        try:
            n = db.query(models.GeneratedDocument).filter_by(
                case_id=sd, doc_type="self_declaration").count()
            assert n == 1, n
            # 재다운로드해도 버전 폭증하지 않음(존재 시 재사용)
        finally:
            db.close()
        c.get(f"/cases/{sd}/self-declaration.pdf", headers=_h(admin))
        db = SessionLocal()
        try:
            n2 = db.query(models.GeneratedDocument).filter_by(
                case_id=sd, doc_type="self_declaration").count()
            assert n2 == 1, n2
        finally:
            db.close()

        # (a) reguler 경로는 400 NOT_SELF_DECLARE
        rg = _mkcase(c, admin, "RegulerCo")
        _set_pathway(rg, "reguler")
        r2 = c.get(f"/cases/{rg}/self-declaration.pdf", headers=_h(admin))
        assert r2.status_code == 400, r2.text
        assert r2.json()["detail"]["code"] == "NOT_SELF_DECLARE", r2.text


def test_a2_pendamping_assignments_and_rbac():
    """(b): pendamping 배정 → /pendamping/assignments 반영, 비권한(applicant) 403."""
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        consultant = _tok(c, "consultant1", "pw")
        pendamping = _tok(c, "pendamping1", "pw")
        applicant = _tok(c, "applicant1", "pw")

        # 로그인 동반자 uid
        pd_uid = c.get("/auth/me", headers=_h(pendamping)).json()["uid"]

        cid = _mkcase(c, admin, "PendampingCo")
        # 배정(컨설턴트) — pendamping_id = 동반자 uid
        ra = c.post(f"/cases/{cid}/pendamping/assign",
                    json={"pendamping_id": pd_uid}, headers=_h(consultant))
        assert ra.status_code == 200, ra.text

        # 동반자 인박스에 반영
        inbox = c.get("/pendamping/assignments", headers=_h(pendamping))
        assert inbox.status_code == 200, inbox.text
        ids = [i["case_id"] for i in inbox.json()["items"]]
        assert cid in ids, inbox.json()

        # 비권한(applicant) 403
        forb = c.get("/pendamping/assignments", headers=_h(applicant))
        assert forb.status_code == 403, forb.text

        # operator/admin은 전체 모니터링(반영 확인)
        ops = _tok(c, "operator1", "pw")
        allv = c.get("/pendamping/assignments", headers=_h(ops))
        assert allv.status_code == 200 and cid in [i["case_id"] for i in allv.json()["items"]]
