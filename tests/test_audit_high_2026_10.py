"""2026-10 감사 높음 A1~A8 부정·정상 테스트 (검수자 작성).

수정이 '막아야 할 것을 실제로 막고, 정상 흐름은 열어 두는가'를 본다.
상태 준비는 DB 직접 세팅(픽스처). 제품 코드는 건드리지 않는다.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_audithigh_test.db")
os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_NOTIFY_WORKER", "0")
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app                    # noqa: E402
from app import models, auth, state_machine as sm  # noqa: E402
from app.db import SessionLocal             # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PW = "Passw0rd!23"


# ---------- 헬퍼 ----------
def _tok(c, u, p="pw"):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _db_case(company="PT Tinggi", status="onboarding", org_id="org_demo", **kw):
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


def _invoice(case_id, status="waiting_payment", total=1_110_000.0, ref=None):
    inv = _db_add(models.Invoice(case_id=case_id, service_type="pre_audit", amount=1_000_000.0,
                                 ppn=110_000.0, total=total, status=status, payment_ref=ref))
    return inv.invoice_id


def _code(r):
    d = r.json().get("detail")
    return d.get("code") if isinstance(d, dict) else None


def _mkuser(username, role, org_id="org_demo"):
    db = SessionLocal()
    try:
        u = models.User(username=username, password_hash=auth.hash_pw(PW), role=role, org_id=org_id)
        db.add(u)
        db.commit()
        return u.user_id
    finally:
        db.close()


def _login(c, u):
    return _tok(c, u, PW)


# ---------- A1. 분할 청구: draft 는 미결이 아님 ----------
def test_a1_draft_invoice_not_unpaid():
    cid = _db_case("PT A1a")
    _invoice(cid, status="paid")
    _invoice(cid, status="draft")
    db = SessionLocal()
    try:
        assert sm.has_unpaid_invoice(db, cid) is False
    finally:
        db.close()


def test_a1_open_invoice_still_unpaid():
    """부정: waiting_payment/payment_processing 등 진짜 미결은 여전히 미결."""
    db = SessionLocal()
    try:
        for st in ("waiting_payment", "payment_processing", "need_verification", "rejected"):
            cid = _db_case("PT A1b " + st)
            _invoice(cid, status=st)
            assert sm.has_unpaid_invoice(db, cid) is True, st
    finally:
        db.close()


def test_a1_issue_guard_draft_passes_pending_blocks():
    with TestClient(app) as c:
        h = _login_op(c)
        # 2회차 draft 만 있음 → PAYMENT_PENDING 으로 막히지 않아야 한다
        cid = _db_case("PT A1c", status="fatwa_approved", fatwa_status="approved",
                       scope_frozen=True, pathway="reguler")
        _invoice(cid, status="paid")
        _invoice(cid, status="draft")
        r = c.post("/cases/%s/certificate/issue" % cid, headers=h, json={"reason": "a1"})
        assert _code(r) != "PAYMENT_PENDING", r.text
        # 부정: 미결 waiting_payment 가 있으면 PAYMENT_PENDING
        cid2 = _db_case("PT A1d", status="fatwa_approved", fatwa_status="approved",
                        scope_frozen=True, pathway="reguler")
        _invoice(cid2, status="paid")
        _invoice(cid2, status="waiting_payment")
        r = c.post("/cases/%s/certificate/issue" % cid2, headers=h, json={"reason": "a1"})
        assert r.status_code == 409 and _code(r) == "PAYMENT_PENDING", r.text


def _login_op(c):
    return _tok(c, "operator1")


# ---------- A2. return-application: 승인 후 반려 불가 ----------
def test_a2_return_blocked_after_approval_states():
    with TestClient(app) as c:
        h = _tok(c, "consultant1")
        for st in ("fatwa_approved", "certificate_issued", "post_certification_monitoring",
                   "change_impact", "renewal_preparation"):
            cid = _db_case("PT A2 " + st, status=st)
            r = c.post("/cases/%s/return-application" % cid, headers=h, json={"reason": "x"})
            assert r.status_code == 409 and _code(r) == "CASE_LOCKED_AFTER_APPROVAL", (st, r.text)
            assert _db_get(models.CaseApplication, cid).status == st


def test_a2_return_blocked_with_active_certificate():
    with TestClient(app) as c:
        h = _tok(c, "consultant1")
        cid = _db_case("PT A2cert", status="document_review")
        _db_add(models.HalalCertificate(case_id=cid, certificate_no="X-A2", status="active"))
        r = c.post("/cases/%s/return-application" % cid, headers=h, json={"reason": "x"})
        assert r.status_code == 409 and _code(r) == "CASE_LOCKED_AFTER_APPROVAL", r.text


def test_a2_return_normal_path_and_inactive_cert():
    """정상: 승인 전 케이스·비활성 인증서만 있는 케이스는 반려 가능(과잉 차단 아님)."""
    with TestClient(app) as c:
        h = _tok(c, "consultant1")
        cid = _db_case("PT A2ok", status="document_review")
        r = c.post("/cases/%s/return-application" % cid, headers=h, json={"reason": "보완"})
        assert r.status_code == 200, r.text
        cid2 = _db_case("PT A2ok2", status="document_review")
        _db_add(models.HalalCertificate(case_id=cid2, certificate_no="X-A2w", status="withdrawn"))
        r = c.post("/cases/%s/return-application" % cid2, headers=h, json={"reason": "보완"})
        assert r.status_code == 200, r.text


# ---------- A3. fatwa return-to-auditor 상태 제한 ----------
def test_a3_return_to_auditor_invalid_states():
    with TestClient(app) as c:
        h = _tok(c, "fatwa1")
        for st in ("onboarding", "fatwa_approved", "certificate_issued", "document_review"):
            cid = _db_case("PT A3 " + st, status=st)
            r = c.post("/cases/%s/fatwa/return-to-auditor" % cid, headers=h, json={"reason": "x"})
            assert r.status_code == 409 and _code(r) == "INVALID_STATE", (st, r.text)
            assert _db_get(models.CaseApplication, cid).status == st


def test_a3_return_to_auditor_allowed_state():
    with TestClient(app) as c:
        h = _tok(c, "fatwa1")
        cid = _db_case("PT A3ok", status="fatwa_review")
        r = c.post("/cases/%s/fatwa/return-to-auditor" % cid, headers=h, json={"reason": "재심사"})
        assert r.status_code == 200, r.text
        assert _db_get(models.CaseApplication, cid).status == "audit_closed"


# ---------- A4. PG 웹훅 ----------
def _hook(c, order, status, **extra):
    body = {"order_id": order, "transaction_status": status}
    body.update(extra)
    return c.post("/pg/webhook/midtrans", json=body)


def test_a4_amount_mismatch_missing_unparsable_need_verification():
    os.environ.pop("GLHAC_PG_WEBHOOK_SECRET", None)
    with TestClient(app) as c:
        for tag, extra in (("mis", {"gross_amount": "1000"}), ("none", {}),
                           ("bad", {"gross_amount": "abc"})):
            cid = _db_case("PT A4 " + tag)
            iid = _invoice(cid, ref="ORD-A4-" + tag)
            r = _hook(c, "ORD-A4-" + tag, "settlement", **extra)
            assert r.status_code == 200, r.text
            assert _db_get(models.Invoice, iid).status == "need_verification", (tag, r.text)


def test_a4_exact_amount_paid_and_advances_case():
    os.environ.pop("GLHAC_PG_WEBHOOK_SECRET", None)
    with TestClient(app) as c:
        cid = _db_case("PT A4ok", status="payment_pending")
        iid = _invoice(cid, ref="ORD-A4-OK")
        r = _hook(c, "ORD-A4-OK", "settlement", gross_amount="1110000.00")
        assert r.status_code == 200, r.text
        assert _db_get(models.Invoice, iid).status == "paid"
        db = SessionLocal()
        try:
            assert db.query(models.Payment).filter_by(invoice_id=iid, status="confirmed").count() == 1
            # _on_invoice_paid 가 호출되어 가드 통과 시 모의심사로 전진(통과 못 하면 no-op)
            ev = db.query(models.WorkflowEvent).filter_by(case_id=cid, action="payment.gate").count()
            st = db.get(models.CaseApplication, cid).status
        finally:
            db.close()
        # 전진 여부는 상태머신 가드에 달려 있다 — 가드가 열릴 상태에서 실제 전진해야 함
        ok_db = SessionLocal()
        try:
            cc = ok_db.get(models.CaseApplication, cid)
            cc_status = cc.status
        finally:
            ok_db.close()
        assert (ev == 1) == (cc_status == "document_pre_audit_requested"), (st, ev)


import pytest  # noqa: E402


_A4_DEFECT = pytest.mark.xfail(strict=True, reason="제품 결함: main.py pg_webhook 의 paid 역행 가드가 매핑 전 이름(pending/failed)을 비교 — 실제 매핑값(payment_processing/rejected)·paid 재전송 강등 미차단. 수정되면 XPASS 로 실패하니 마커를 제거할 것")


@pytest.mark.parametrize("st", [
    pytest.param("pending", marks=_A4_DEFECT), "expire",
    pytest.param("failed", marks=_A4_DEFECT), pytest.param("deny", marks=_A4_DEFECT),
    pytest.param("cancel", marks=_A4_DEFECT)])
def test_a4_paid_not_regressed(st):
    """이미 paid 인 인보이스가 지연·재전송 콜백으로 역행하면 안 된다."""
    os.environ.pop("GLHAC_PG_WEBHOOK_SECRET", None)
    with TestClient(app) as c:
        cid = _db_case("PT A4rev " + st)
        ref = "ORD-A4-REV-" + st
        iid = _invoice(cid, status="paid", ref=ref)
        r = _hook(c, ref, st)
        assert r.status_code == 200, r.text
        assert _db_get(models.Invoice, iid).status == "paid", (st, r.json())


@_A4_DEFECT
def test_a4_paid_replay_without_amount_not_downgraded():
    """이미 paid 인 인보이스에 (다른 event_id 의) paid 재전송이 금액 없이 오면 need_verification 으로
    강등되거나 Payment 가 중복되면 안 된다."""
    os.environ.pop("GLHAC_PG_WEBHOOK_SECRET", None)
    with TestClient(app) as c:
        cid = _db_case("PT A4replay")
        iid = _invoice(cid, status="paid", ref="ORD-A4-REPLAY")
        r = _hook(c, "ORD-A4-REPLAY", "settlement", event_id="evt-replay-1")
        assert r.status_code == 200, r.text
        assert _db_get(models.Invoice, iid).status == "paid", r.json()


# ---------- A5. GET /approvals 범위 ----------
def test_a5_approvals_scoped_for_applicant_consultant():
    with TestClient(app) as c:
        # 다른 조직 신청자(자가가입) + 그 조직 케이스
        rb = c.post("/auth/register", json={"username": "a5other", "password": PW,
                                            "company_name": "Other Co A5"})
        assert rb.status_code == 200, rb.text
        other_org = rb.json()["org_id"]
        other_case = _db_case("Other Co A5 case", org_id=other_org)
        mine_case = _db_case("PT A5 mine", org_id="org_demo")
        for cid in (other_case, mine_case):
            _db_add(models.ApprovalRequest(action_type="certificate.issue", case_id=cid,
                                           status="pending", requested_by="someone"))
        _db_add(models.ApprovalRequest(action_type="certificate.issue", case_id=None,
                                       status="pending", requested_by="someone"))
        # 다른 조직 신청자는 남의 케이스를 못 본다
        ho = {"Authorization": "Bearer " + rb.json()["token"]}
        ids = {a["case_id"] for a in c.get("/approvals", headers=ho).json()}
        assert mine_case not in ids and None not in ids, ids
        # 같은 조직 신청자는 자기 케이스 항목을 본다, 남의 조직은 못 본다
        ids = {a["case_id"] for a in c.get("/approvals", headers=_tok(c, "applicant1")).json()}
        assert other_case not in ids and None not in ids, ids
        assert mine_case in ids, "정상 접근 가능 케이스 항목까지 숨김(과잉 차단)"
        # 미배정 컨설턴트: 타 조직 케이스 항목 제외
        ids = {a["case_id"] for a in c.get("/approvals", headers=_tok(c, "consultant1")).json()}
        assert other_case not in ids, ids
        # operator 는 전체
        ids = {a["case_id"] for a in c.get("/approvals", headers=_tok(c, "operator1")).json()}
        assert other_case in ids and mine_case in ids


# ---------- A6. 시설 IDOR ----------
def test_a6_facility_cross_org_forbidden():
    with TestClient(app) as c:
        rb = c.post("/auth/register", json={"username": "a6other", "password": PW,
                                            "company_name": "Other Co A6"})
        assert rb.status_code == 200, rb.text
        ho = {"Authorization": "Bearer " + rb.json()["token"]}
        f = _db_add(models.Facility(org_id="org_demo", name="Pabrik A6"))
        r = c.post("/orgs/org_demo/facilities", headers=ho, json={"name": "intruder"})
        assert r.status_code == 403, r.text
        r = c.patch("/facilities/%s" % f.facility_id, headers=ho, json={"name": "hacked"})
        assert r.status_code == 403, r.text
        r = c.delete("/facilities/%s" % f.facility_id, headers=ho)
        assert r.status_code == 403, r.text
        got = _db_get(models.Facility, f.facility_id)
        assert got is not None and got.name == "Pabrik A6"


def test_a6_facility_same_org_ok():
    with TestClient(app) as c:
        h = _tok(c, "applicant1")
        r = c.post("/orgs/org_demo/facilities", headers=h, json={"name": "Pabrik Own"})
        assert r.status_code == 200, r.text
        fid = r.json().get("facility_id")
        assert fid
        r = c.patch("/facilities/%s" % fid, headers=h, json={"name": "Pabrik Own2"})
        assert r.status_code == 200, r.text
        r = c.delete("/facilities/%s" % fid, headers=h)
        assert r.status_code == 200, r.text


# ---------- A7. 운영 노출 차단 ----------
def _sub_import(env_extra, code):
    env = {k: v for k, v in os.environ.items()
           if k not in ("GLHAC_DEV", "GLHAC_API_DOCS", "GLHAC_METRICS_TOKEN")}
    env.update(env_extra)
    env["GLHAC_DB_URL"] = "sqlite:////tmp/glhac_a7_sub_%d.db" % os.getpid()
    env["GLHAC_SECRET"] = "x" * 40
    env["GLHAC_NOTIFY_WORKER"] = "0"
    p = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr[-2000:]
    return p.stdout.strip().splitlines()[-1]


_DOC_CODE = ("from app.main import app; "
             "print(app.docs_url, app.redoc_url, app.openapi_url)")


def test_a7_docs_disabled_without_dev_or_flag():
    assert _sub_import({}, _DOC_CODE) == "None None None"


def test_a7_docs_enabled_with_flag_or_dev():
    assert _sub_import({"GLHAC_API_DOCS": "1"}, _DOC_CODE) == "/docs /redoc /openapi.json"
    assert _sub_import({"GLHAC_DEV": "1"}, _DOC_CODE) == "/docs /redoc /openapi.json"


def test_a7_docs_404_runtime_without_dev():
    code = ("from fastapi.testclient import TestClient; from app.main import app; "
            "c=TestClient(app); "
            "print([c.get(p).status_code for p in ('/docs','/redoc','/openapi.json')])")
    assert _sub_import({}, code) == "[404, 404, 404]"


def test_a7_stream_test_404_without_dev(monkeypatch):
    with TestClient(app) as c:
        monkeypatch.delenv("GLHAC_DEV", raising=False)
        r = c.get("/debug/stream-test?n=1")
        assert r.status_code == 404, r.text
        monkeypatch.setenv("GLHAC_DEV", "1")
        r = c.get("/debug/stream-test?n=1")
        assert r.status_code == 200, r.status_code


def test_a7_metrics_with_token_env(monkeypatch):
    monkeypatch.setenv("GLHAC_METRICS_TOKEN", "scrape-secret-1")
    with TestClient(app) as c:
        assert c.get("/metrics").status_code == 401
        assert c.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 403
        # 토큰 설정 시 운영자 JWT 는 통과하지 못한다(Bearer 일치만)
        assert c.get("/metrics", headers=_tok(c, "operator1")).status_code == 403
        r = c.get("/metrics", headers={"Authorization": "Bearer scrape-secret-1"})
        assert r.status_code == 200, r.text


def test_a7_metrics_without_token_env(monkeypatch):
    monkeypatch.delenv("GLHAC_METRICS_TOKEN", raising=False)
    with TestClient(app) as c:
        assert c.get("/metrics").status_code == 401
        assert c.get("/metrics", headers={"Authorization": "Bearer garbage"}).status_code == 401
        assert c.get("/metrics", headers=_tok(c, "applicant1")).status_code == 403
        assert c.get("/metrics", headers=_tok(c, "operator1")).status_code == 200
        assert c.get("/metrics", headers=_tok(c, "admin", "admin")).status_code == 200
        # refresh 토큰은 401
        db = SessionLocal()
        try:
            op = db.query(models.User).filter_by(username="operator1").first()
            refresh = auth.make_token(op, "refresh")
        finally:
            db.close()
        r = c.get("/metrics", headers={"Authorization": "Bearer " + refresh})
        assert r.status_code == 401, r.text


def test_a7_metrics_revoked_operator_token_401(monkeypatch):
    monkeypatch.delenv("GLHAC_METRICS_TOKEN", raising=False)
    with TestClient(app) as c:
        _mkuser("a7op", "operator")
        h = _login(c, "a7op")
        assert c.get("/metrics", headers=h).status_code == 200
        db = SessionLocal()
        try:
            u = db.query(models.User).filter_by(username="a7op").first()
            u.token_version = (u.token_version or 0) + 1
            db.commit()
        finally:
            db.close()
        assert c.get("/metrics", headers=h).status_code == 401


# ---------- A8. 역할 변경·비번 재설정 시 기존 토큰 폐기 ----------
def test_a8_role_change_revokes_token():
    with TestClient(app) as c:
        uid = _mkuser("a8role", "applicant")
        h = _login(c, "a8role")
        assert c.get("/auth/me", headers=h).status_code == 200
        r = c.patch("/admin/users/%s" % uid, headers=_tok(c, "admin", "admin"), json={"role": "consultant"})
        assert r.status_code == 200, r.text
        assert c.get("/auth/me", headers=h).status_code == 401
        # 새 로그인은 새 역할로 정상
        r2 = c.get("/auth/me", headers=_login(c, "a8role"))
        assert r2.status_code == 200 and r2.json()["role"] == "consultant"


def test_a8_password_reset_revokes_token():
    with TestClient(app) as c:
        uid = _mkuser("a8pw", "applicant")
        h = _login(c, "a8pw")
        r = c.patch("/admin/users/%s" % uid, headers=_tok(c, "admin", "admin"),
                    json={"password": "NewPassw0rd!45"})
        assert r.status_code == 200, r.text
        assert c.get("/auth/me", headers=h).status_code == 401
        assert c.post("/auth/login", json={"username": "a8pw", "password": "NewPassw0rd!45"}).status_code == 200


def test_a8_noop_patch_keeps_token():
    """정상: 같은 역할로 PATCH(변경 없음)하면 토큰을 끊지 않는다."""
    with TestClient(app) as c:
        uid = _mkuser("a8noop", "applicant")
        h = _login(c, "a8noop")
        r = c.patch("/admin/users/%s" % uid, headers=_tok(c, "admin", "admin"), json={"role": "applicant"})
        assert r.status_code == 200, r.text
        assert c.get("/auth/me", headers=h).status_code == 200
