"""2026-10 감사 치명 결함 K1~K7 부정 테스트.

각 테스트는 '막혀야 하는 행위가 실제로 막히는가'를 본다. 수정 전 코드에서는 실패하고,
수정 후에는 통과해야 한다. 상태 준비는 상태머신을 완주하지 않고 DB에 직접 세팅한다(픽스처).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_auditcrit_test.db")
os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_NOTIFY_WORKER", "0")
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app                    # noqa: E402
from app import models                      # noqa: E402
from app.db import SessionLocal             # noqa: E402


# ---------- 헬퍼 ----------
def _tok(c, u, p="pw"):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _db_case(company="PT Kritis", status="onboarding", org_id="org_demo", **kw):
    """케이스를 DB에 직접 만든다 — 테스트 전제 상태를 정확히 세팅하기 위해."""
    db = SessionLocal()
    try:
        c = models.CaseApplication(org_id=org_id, company_name=company, status=status, **kw)
        db.add(c)
        db.commit()
        return c.case_id
    finally:
        db.close()


def _db_add(obj):
    db = SessionLocal()
    try:
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj
    finally:
        db.close()


def _db_get(model, pk):
    db = SessionLocal()
    try:
        o = db.get(model, pk)
        if o is not None:
            db.expunge(o)
        return o
    finally:
        db.close()


def _invoice(case_id, status="waiting_payment", total=1_110_000.0):
    inv = _db_add(models.Invoice(case_id=case_id, service_type="pre_audit", amount=1_000_000.0,
                                 ppn=110_000.0, total=total, status=status))
    return inv.invoice_id


def _code(r):
    d = r.json().get("detail")
    return d.get("code") if isinstance(d, dict) else None


def _issue_ready_case(company):
    """발급 직전 상태(fatwa 승인·범위 동결)의 정규 케이스."""
    return _db_case(company, status="fatwa_approved", fatwa_status="approved",
                    scope_frozen=True, pathway="reguler")


# ---------- K1. 회사명만 맞추면 남의 조직에 편입되던 문제 ----------
def test_register_same_company_creates_new_org():
    with TestClient(app) as c:
        ra = c.post("/auth/register", json={"username": "k1victim", "password": "Passw0rd!23",
                                            "company_name": "ABC Corp K1"})
        assert ra.status_code == 200, ra.text
        rb = c.post("/auth/register", json={"username": "k1attacker", "password": "Passw0rd!23",
                                            "company_name": "ABC Corp K1"})
        assert rb.status_code == 200, rb.text
        assert ra.json()["org_id"] != rb.json()["org_id"], "같은 회사명 자가가입이 기존 조직에 편입됨"
        assert rb.json()["company_role"] == "client_admin"
        # 피해 업체의 가입 시 생성된 케이스를 공격자가 열람할 수 없어야 한다
        db = SessionLocal()
        try:
            victim_case = db.query(models.CaseApplication).filter_by(org_id=ra.json()["org_id"]).first()
            assert victim_case is not None
            vcid = victim_case.case_id
        finally:
            db.close()
        hb = {"Authorization": "Bearer " + rb.json()["token"]}
        r = c.get("/cases/%s" % vcid, headers=hb)
        assert r.status_code == 403, r.text


def test_company_org_for_reuse_only_my_consultant():
    """컨설턴트가 회사명으로 담당 없는 자가가입 조직을 가로채지 못한다."""
    with TestClient(app) as c:
        ra = c.post("/auth/register", json={"username": "k1selfreg", "password": "Passw0rd!23",
                                            "company_name": "Victim Self Co"})
        assert ra.status_code == 200, ra.text
        victim_org = ra.json()["org_id"]
        db = SessionLocal()
        try:
            vcid = db.query(models.CaseApplication).filter_by(org_id=victim_org).first().case_id
        finally:
            db.close()
        cons = _tok(c, "consultant1")
        r = c.post("/cases", headers=cons, json={"company_name": "Victim Self Co"})
        assert r.status_code == 200, r.text
        new_case = _db_get(models.CaseApplication, r.json()["case_id"])
        assert new_case.org_id != victim_org, "컨설턴트가 담당 없는 조직을 재사용함"
        assert _db_get(models.Org, victim_org).consultant_id is None, "담당 없는 조직에 컨설턴트가 귀속됨"
        r = c.get("/cases/%s" % vcid, headers=cons)
        assert r.status_code == 403, r.text


# ---------- K2. 신청자 자기결제 ----------
def test_pay_invoice_operator_only():
    with TestClient(app) as c:
        cid = _db_case("PT PayK2")
        iid = _invoice(cid)
        r = c.patch("/invoices/%s/pay" % iid, headers=_tok(c, "applicant1"))
        assert r.status_code == 403, r.text
        assert _db_get(models.Invoice, iid).status == "waiting_payment"
        r = c.patch("/invoices/%s/pay" % iid, headers=_tok(c, "operator1"))
        assert r.status_code == 200, r.text
        assert _db_get(models.Invoice, iid).status == "paid"


def test_record_payment_applicant_pending():
    with TestClient(app) as c:
        cid = _db_case("PT RecordK2", status="consultant_review")
        iid = _invoice(cid)
        r = c.post("/invoices/%s/payment" % iid, headers=_tok(c, "applicant1"),
                   json={"method": "card", "amount": 1_110_000.0})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "pending", r.json()
        assert r.json()["invoice_status"] == "need_verification", r.json()
        assert _db_get(models.Invoice, iid).status == "need_verification"
        assert _db_get(models.CaseApplication, cid).status == "consultant_review"


# ---------- K3. 계약 금액 위조·서명 ----------
def test_gen_contract_fee_override_forbidden():
    with TestClient(app) as c:
        cid = _db_case("PT FeeK3")
        r = c.post("/cases/%s/contract/generate" % cid, headers=_tok(c, "applicant1"),
                   json={"fee": 50_000_000})
        assert r.status_code == 403 and _code(r) == "FEE_OVERRIDE_FORBIDDEN", r.text
        r = c.post("/cases/%s/contract/generate" % cid, headers=_tok(c, "operator1"),
                   json={"fee": 50_000_000})
        assert r.status_code == 200, r.text


def test_gen_contract_locked_prevent_regenerate():
    with TestClient(app) as c:
        cid = _db_case("PT LockK3")
        _db_add(models.Contract(case_id=cid, org_id="org_demo", status="confirmed", fee=1000.0,
                                signatures=[{"party": "A", "name": "a"}, {"party": "B", "name": "b"}]))
        r = c.post("/cases/%s/contract/generate" % cid, headers=_tok(c, "applicant1"), json={})
        assert r.status_code == 409 and _code(r) == "CONTRACT_LOCKED", r.text


def test_sign_contract_wrong_state():
    with TestClient(app) as c:
        cid = _db_case("PT SignK3")
        ct = _db_add(models.Contract(case_id=cid, org_id="org_demo", status="draft"))
        app_h = _tok(c, "applicant1")
        r = c.post("/contracts/%s/sign" % ct.contract_id, headers=app_h, params={"party": "A", "name": "x"})
        assert r.status_code == 409 and _code(r) == "CONTRACT_NOT_SIGNABLE", r.text
        db = SessionLocal()
        try:
            db.get(models.Contract, ct.contract_id).status = "signing"
            db.commit()
        finally:
            db.close()
        r = c.post("/contracts/%s/sign" % ct.contract_id, headers=app_h, params={"party": "A", "name": "x"})
        assert r.status_code == 200, r.text


def test_sign_contract_other_org_forbidden():
    """다른 조직 신청자가 남의 계약에 갑(A) 서명을 남기지 못한다(IDOR)."""
    with TestClient(app) as c:
        cid = _db_case("PT IdorK3")
        ct = _db_add(models.Contract(case_id=cid, org_id="org_demo", status="sent"))
        rg = c.post("/auth/register", json={"username": "k3outsider", "password": "Passw0rd!23",
                                            "company_name": "Outsider K3"})
        assert rg.status_code == 200, rg.text
        h = {"Authorization": "Bearer " + rg.json()["token"]}
        r = c.post("/contracts/%s/sign" % ct.contract_id, headers=h, params={"party": "A", "name": "x"})
        assert r.status_code == 403, r.text
        assert not (_db_get(models.Contract, ct.contract_id).signatures or [])


# ---------- K4. raw 전이로 정식 흐름 건너뛰기·미결제 발급 ----------
def test_transition_formal_flow_guard(monkeypatch):
    monkeypatch.setenv("GLHAC_FORMAL_FLOW", "1")
    with TestClient(app) as c:
        cid = _db_case("PT FormalK4", status="consultant_review", pathway="reguler")
        body = {"to_state": "document_pre_audit_requested"}
        r = c.post("/cases/%s/transition" % cid, headers=_tok(c, "applicant1"), json=body)
        assert r.status_code == 403, r.text
        op = _tok(c, "operator1")
        r = c.post("/cases/%s/transition" % cid, headers=op, json=body)
        assert r.status_code == 409, r.text
        assert any(b.get("code") == "FORMAL_NOT_ACCEPTED" for b in r.json()["detail"]["blockers"]), r.text
        # 관리자 접수(accepted) 기록 후에는 진행된다
        from app import state_machine as sm
        db = SessionLocal()
        try:
            sm.record_event(db, db.get(models.CaseApplication, cid), "consultant_review",
                            "consultant_review", "formal_application.accepted", "operator", None)
            db.commit()
        finally:
            db.close()
        r = c.post("/cases/%s/transition" % cid, headers=op, json=body)
        assert r.status_code == 200, r.text


def test_issue_guards_no_payment():
    with TestClient(app) as c:
        cid = _issue_ready_case("PT NoPayK4")
        op = _tok(c, "operator1")
        r = c.post("/cases/%s/certificate/issue" % cid, headers=op, json={"reason": "발급 요청"})
        assert r.status_code == 409 and _code(r) == "NO_PAYMENT", r.text
        _invoice(cid, status="paid")
        r = c.post("/cases/%s/certificate/issue" % cid, headers=op, json={"reason": "발급 요청"})
        assert r.status_code == 200, r.text


# ---------- K5. 동결 후 원재료 변경·하람 원재료 발급 ----------
def test_scope_frozen_prevent_material_edit():
    with TestClient(app) as c:
        cid = _db_case("PT FrozenK5", status="final_package_preparation", scope_frozen=True)
        m = _db_add(models.Material(case_id=cid, name="Sugar", screen_status="halal", screen_result="PASS"))
        h = _tok(c, "applicant1")
        r = c.post("/cases/%s/materials" % cid, headers=h, json={"name": "Gelatin"})
        assert r.status_code == 409 and _code(r) == "SCOPE_FROZEN", r.text
        r = c.patch("/materials/%s" % m.material_id, headers=h, json={"supplier": "X"})
        assert r.status_code == 409 and _code(r) == "SCOPE_FROZEN", r.text
        r = c.delete("/materials/%s" % m.material_id, headers=h)
        assert r.status_code == 409 and _code(r) == "SCOPE_FROZEN", r.text
        assert _db_get(models.Material, m.material_id) is not None


def test_haram_material_prevent_issue():
    with TestClient(app) as c:
        cid = _issue_ready_case("PT HaramK5")
        _invoice(cid, status="paid")
        _db_add(models.Material(case_id=cid, name="Pork Lard", screen_result="BLOCK", screen_status="haram"))
        r = c.post("/cases/%s/certificate/issue" % cid, headers=_tok(c, "operator1"),
                   json={"reason": "발급 요청"})
        assert r.status_code == 409 and _code(r) == "HARAM_MATERIAL", r.text
        assert "Pork Lard" in r.json()["detail"]["materials"]


# ---------- K6. 중대 부적합 삭제 ----------
def test_delete_finding_consultant_forbidden():
    with TestClient(app) as c:
        cid = _db_case("PT FindK6")
        f = _db_add(models.AuditFinding(case_id=cid, finding="minor issue", severity="minor"))
        r = c.delete("/findings/%s" % f.finding_id, headers=_tok(c, "consultant1"))
        assert r.status_code == 403, r.text
        assert _db_get(models.AuditFinding, f.finding_id) is not None
        r = c.delete("/findings/%s" % f.finding_id, headers=_tok(c, "auditor1"))
        assert r.status_code == 200, r.text
        assert _db_get(models.AuditFinding, f.finding_id) is None


def test_delete_major_finding_forbidden():
    with TestClient(app) as c:
        cid = _db_case("PT MajorK6")
        f = _db_add(models.AuditFinding(case_id=cid, finding="major issue", severity="major"))
        r = c.delete("/findings/%s" % f.finding_id, headers=_tok(c, "auditor1"))
        assert r.status_code == 409 and _code(r) == "MAJOR_FINDING_DELETE_FORBIDDEN", r.text
        assert _db_get(models.AuditFinding, f.finding_id) is not None


# ---------- K7. 아무 문자열 인증번호로 할랄 승격 ----------
def test_cert_verify_strict_default(monkeypatch):
    monkeypatch.delenv("GLHAC_CERT_VERIFY", raising=False)
    from app import screening
    assert screening._cert_verify_mode() == "strict"
    with TestClient(app):     # 온톨로지 적재
        r = screening.screen_merged("Gelatin", cert_no="asdfghjk")
    assert r["cert_promoted"] is False, r
    assert r["status"] != "halal", r
    assert r["result"] != "CLEARED", r
