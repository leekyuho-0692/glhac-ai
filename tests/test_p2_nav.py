"""P2(UI개편 기반) — step8·hold·/cases 필드확장·nav-counts(N2)·todo(N3)·동반오디터(N6).

실행: <venv>/bin/python -m pytest tests/test_p2_nav.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_p2nav_test.db")

from fastapi.testclient import TestClient   # noqa: E402

from app.main import app                    # noqa: E402


def _tok(c, u="applicant1", p="pw"):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _case(c, h, name="PT Nav Uji"):
    r = c.post("/cases", json={"company_name": name}, headers=h)
    assert r.status_code in (200, 201), r.text
    return r.json()["case_id"]


def test_cases_목록에_step8_필드가_추가된다():
    with TestClient(app) as c:
        h = _tok(c)
        cid = _case(c, h)
        it = next(x for x in c.get("/cases", headers=h).json()["items"] if x["case_id"] == cid)
        for k in ["step8", "step8_label", "done", "hold", "owner",
                  "consultant", "main_auditor", "co_auditors", "next_action"]:
            assert k in it, k
        # 신규 케이스 = 접수(step 0), 담당 client, 미완료
        assert it["step8"] == 0 and it["step8_label"] == "접수"
        assert it["owner"] == "client" and it["done"] is False
        assert it["co_auditors"] == []


def test_journey_에_step8_블록이_붙는다():
    with TestClient(app) as c:
        h = _tok(c)
        cid = _case(c, h)
        j = c.get(f"/cases/{cid}/journey", headers=h).json()
        assert "step8" in j and j["step8"]["step"] == 0
        assert j["step8"]["labels"][0] == "접수" and len(j["step8"]["labels"]) == 8
        # 기존 11단계도 유지(하위호환)
        assert len(j["stages"]) == 11


def test_me_nav_counts_역할별_집계():
    with TestClient(app) as c:
        h = _tok(c)                       # applicant
        _case(c, h)
        d = c.get("/me/nav-counts", headers=h).json()
        for k in ["action_required", "hold", "in_progress"]:
            assert k in d
        assert d["in_progress"] >= 1 and d["action_required"] >= 1   # 접수=client 담당
        # 관리자: 스태프 가입 대기 키
        ha = _tok(c, "admin", "admin")
        da = c.get("/me/nav-counts", headers=ha).json()
        assert "staff_signup_pending" in da
        # 오디터: 배정 대기 키
        hau = _tok(c, "auditor1", "pw")
        dau = c.get("/me/nav-counts", headers=hau).json()
        assert "assignment_pending" in dau


def test_me_todo_담당_케이스를_돌려준다():
    with TestClient(app) as c:
        h = _tok(c)
        cid = _case(c, h)
        d = c.get("/me/todo", headers=h).json()
        assert d["count"] >= 1
        row = next(x for x in d["items"] if x["case_id"] == cid)
        assert row["mine"] is True and row["owner"] == "client"


def test_동반_오디터_추가조회해제_N6():
    with TestClient(app) as c:
        h = _tok(c)
        cid = _case(c, h)
        ha = _tok(c, "admin", "admin")
        # 추가 (username)
        r = c.post(f"/cases/{cid}/auditors", json={"username": "auditor2"}, headers=ha)
        assert r.status_code == 200, r.text
        # 조회
        au = c.get(f"/cases/{cid}/auditors", headers=ha).json()
        assert any(x["name"] == "auditor2" for x in au["co"])
        co_uid = next(x["user_id"] for x in au["co"] if x["name"] == "auditor2")
        # /cases 항목에도 반영
        it = next(x for x in c.get("/cases", headers=ha).json()["items"] if x["case_id"] == cid)
        assert co_uid in it["co_auditors"]
        # 동반 오디터 본인도 케이스가 보인다(가시성)
        h2 = _tok(c, "auditor2", "pw")
        assert any(x["case_id"] == cid for x in c.get("/cases", headers=h2).json()["items"])
        # 비오디터 추가 거부
        r2 = c.post(f"/cases/{cid}/auditors", json={"username": "consultant1"}, headers=ha)
        assert r2.status_code == 422
        # 해제
        r3 = c.request("DELETE", f"/cases/{cid}/auditors/{co_uid}", headers=ha)
        assert r3.status_code == 200, r3.text
        au2 = c.get(f"/cases/{cid}/auditors", headers=ha).json()
        assert all(x["user_id"] != co_uid for x in au2["co"])


# ---------- v4 연결 4: nav8 재정의(상태머신 기준) ----------
_NAV8_TABLE = [
    # status, step, owner, done
    ("supplementation_submitted", 1, "auditor", False),
    ("consultant_review", 1, "consultant", False),
    ("document_pre_audit_requested", 3, "auditor", False),     # 모의심사(결제 정리)
    ("document_pre_audit_in_review", 3, "auditor", False),
    ("document_pre_audit_approved", 4, "ops", False),          # 다음 = lph_assignment
    ("lph_assignment", 4, "auditor", False),
    ("final_package_preparation", 4, "auditor", False),
    ("pendamping_verification", 1, "pendamping", False),
    ("self_declaration_submitted", 5, "ops", False),
    ("fatwa_review", 5, "sharia", False),
    ("fatwa_approved", 6, "ops", False),
    ("certificate_issued", 6, "ops", False),                    # SiHalal 미등록
    ("post_certification_monitoring", 7, "client", True),
    ("change_impact", 7, "client", False),                      # 완료 아님
    ("renewal_preparation", 7, "client", False),
]


def _nav8_of(cid, status):
    from app import models, nav8
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        c = db.get(models.CaseApplication, cid)
        c.status = status
        db.commit()
        return nav8.compute_step8(db, c)
    finally:
        db.close()


def test_nav8_상태머신_기준_매핑():
    with TestClient(app) as c:
        cid = _case(c, _tok(c), "PT Nav8 Map")
        for st, step, owner, done in _NAV8_TABLE:
            s8 = _nav8_of(cid, st)
            assert (s8["step"], s8["owner"], s8["done"]) == (step, owner, done), (st, s8)


def test_nav8_신호_보정_계약_결제_SiHalal_위원회():
    from app import models
    from app.db import SessionLocal
    with TestClient(app) as c:
        cid = _case(c, _tok(c), "PT Nav8 Sig")
        db = SessionLocal()
        try:
            db.add(models.Contract(case_id=cid))
            db.add(models.Invoice(case_id=cid, status="waiting_payment"))
            db.commit()
        finally:
            db.close()
        # 계약 있으면 consultant_review = 계약(2)
        assert _nav8_of(cid, "consultant_review")["step"] == 2
        # 미결제 인보이스 → 모의심사 요청돼도 계약·결제(2), 기업 차례
        s8 = _nav8_of(cid, "document_pre_audit_requested")
        assert (s8["step"], s8["owner"]) == (2, "client")
        db = SessionLocal()
        try:   # INVOICE_SETTLED(paid·refunded·expired·cancelled) 기준으로 정리
            db.query(models.Invoice).filter_by(case_id=cid).update({"status": "cancelled"})
            db.add(models.IntegrationEvent(case_id=cid, event_type="document_package_submitted",
                                           status="submitted"))
            c0 = db.get(models.CaseApplication, cid)
            c0.fatwa_status = "approved"
            db.commit()
        finally:
            db.close()
        s8 = _nav8_of(cid, "document_pre_audit_requested")
        assert (s8["step"], s8["owner"]) == (3, "auditor")
        # SiHalal 등록되면 7(진행 중), 미등록이면 6 — 위 표
        s8 = _nav8_of(cid, "certificate_issued")
        assert (s8["step"], s8["done"]) == (7, False)
        # 자기선언 위원회 승인 후 = 인증서 발급(6), 운영 차례
        s8 = _nav8_of(cid, "committee_verification")
        assert (s8["step"], s8["owner"]) == (6, "ops")


def test_nav8_pendamping_버킷():
    from app import nav8
    assert nav8.role_bucket("pendamping_pph") == "pendamping"
    assert nav8.role_bucket("penyelia_halal") == "client"
