"""d3: 실제 요청·독립 세션·스레드로 결제/운영 불변식을 검증한다."""
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

os.environ.setdefault("GLHAC_DEV", "1")
os.environ["GLHAC_RATE_LIMIT_PER_MIN"] = "0"
os.environ["GLHAC_NOTIFY_WORKER"] = "0"

from app import main, models, notify, state_machine as sm
from test_final_e2_rules import seed_reviewed_issue_prerequisites
from app.db import SessionLocal


@pytest.fixture(scope="module")
def api():
    with TestClient(main.app, raise_server_exceptions=False) as client:
        token = client.post("/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
        client.headers["Authorization"] = "Bearer " + token
        yield client


@pytest.fixture(autouse=True)
def local_mode(monkeypatch):
    monkeypatch.delenv("GLHAC_FORMAL_FLOW", raising=False)
    monkeypatch.delenv("GLHAC_PG_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("GLHAC_PG_SECRET_MANUAL", raising=False)


def invoice(status="waiting_payment", paid=0, case_status="consultant_review", currency="KRW"):
    with SessionLocal() as db:
        c = models.CaseApplication(org_id="org_demo", company_name="d3-" + uuid4().hex,
                                   status=case_status, pathway="reguler", notify_consent=True)
        db.add(c)
        db.flush()
        db.add(models.Contract(case_id=c.case_id, status="confirmed", currency=currency))
        inv = models.Invoice(case_id=c.case_id, total=100, amount=100, ppn=0, status=status,
                             payment_ref="d3-" + uuid4().hex, invoice_no="d3-" + uuid4().hex)
        db.add(inv)
        db.flush()
        if paid:
            db.add(models.Payment(case_id=c.case_id, invoice_id=inv.invoice_id, amount=paid,
                                  currency=currency, status="confirmed", method="manual"))
        db.commit()
        return c.case_id, inv.invoice_id, inv.payment_ref


def balance(iid):
    with SessionLocal() as db:
        return db.query(func.sum(models.Payment.amount)).filter_by(invoice_id=iid, status="confirmed").scalar() or 0


def hook(api, ref, status="settlement", amount=100, currency="KRW", **extra):
    return api.post("/pg/webhook/manual", json=dict(order_id=ref, transaction_status=status,
                    amount=amount, currency=currency, event_id=uuid4().hex, **extra))


def concurrent(call, n=4):
    barrier = threading.Barrier(n)
    def run(i):
        barrier.wait(timeout=10)
        return call(i)
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(run, range(n)))


@pytest.mark.parametrize("state", ["cancelled", "refunded", "expired", "draft", "rejected"])
def test_pay_rejects_illegal_state(api, state):
    _, iid, _ = invoice(state)
    r = api.patch(f"/invoices/{iid}/pay")
    assert r.status_code == 409, r.text
    assert balance(iid) == 0


def test_pay_checks_existing_partial_receipts(api):
    _, iid, _ = invoice(paid=1)
    r = api.patch(f"/invoices/{iid}/pay")
    assert r.status_code == 409, r.text
    assert balance(iid) == 1


def test_paid_cannot_be_downgraded_by_one_unit(api):
    _, iid, _ = invoice("paid", paid=100)
    r = api.post(f"/invoices/{iid}/payment", json={"method": "card", "amount": 1})
    assert r.status_code == 409, r.text
    with SessionLocal() as db:
        assert db.get(models.Invoice, iid).status == "paid"
    assert balance(iid) == 100


@pytest.mark.parametrize("state", ["waiting_payment", "cancelled", "expired", "draft"])
def test_pg_cannot_refund_uncollected_invoice(api, state):
    _, iid, ref = invoice(state)
    r = hook(api, ref, "refund")
    assert r.status_code == 409, r.text
    assert balance(iid) == 0


@pytest.mark.parametrize("status", ["settlement", "refund"])
def test_pg_currency_mismatch(api, status):
    _, iid, ref = invoice("paid" if status == "refund" else "waiting_payment", paid=100 if status == "refund" else 0)
    r = hook(api, ref, status, currency="USD")
    assert r.status_code == 409, r.text
    assert balance(iid) == (100 if status == "refund" else 0)


def test_pg_partial_refund_uses_net_receipts(api):
    cid, iid, ref = invoice("paid", paid=100)
    assert hook(api, ref, "partial_refund", 30).status_code == 200
    assert balance(iid) == 70
    with SessionLocal() as db:
        assert db.get(models.Invoice, iid).status == "partially_refunded"
        assert main._guard_invoice_receipts(db, db.get(models.CaseApplication, cid))
    assert hook(api, ref, "refund", 71).status_code == 409
    assert hook(api, ref, "refund", 70).status_code == 200
    assert balance(iid) == 0
    with SessionLocal() as db:
        assert db.get(models.Invoice, iid).status == "refunded"


@pytest.mark.parametrize("state", ["paid", "refunded", "expired"])
def test_payment_gate_requires_money_even_for_settled_labels(api, state):
    cid, _, _ = invoice(state, case_status="document_pre_audit_requested")
    with SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        ok, blockers = sm.can_transition(db, c, "document_pre_audit_in_review")
        assert not ok and any(b["code"] == "UNPAID_INVOICE" for b in blockers)


def test_quote_counts_actual_products(api):
    cid, _, _ = invoice()
    with SessionLocal() as db:
        for i in range(3):
            db.add(models.Product(case_id=cid, name=f"product-{i}"))
        db.commit()
    r = api.post(f"/cases/{cid}/quote", json={"product_count": 0})
    assert r.status_code == 200, r.text
    assert r.json()["product_count"] == 3
    assert next(x for x in r.json()["lines"] if x["code"] == "product_extra")["qty"] == 2


@pytest.mark.parametrize("operation", ["quote", "payment", "deposit", "approval", "pay", "pg"])
def test_threaded_retries_have_one_result(api, operation):
    cid, iid, ref = invoice()
    headers = {"Idempotency-Key": uuid4().hex}
    if operation == "quote":
        call = lambda _: api.post(f"/cases/{cid}/quote", json={}, headers=headers)
    elif operation == "payment":
        call = lambda _: api.post(f"/invoices/{iid}/payment", json={"method": "card", "amount": 100, "reference": ref}, headers=headers)
    elif operation == "deposit":
        call = lambda _: api.post("/admin/deposits", json={"amount": 100, "ref_memo": ref, "currency": "KRW"}, headers=headers)
    elif operation == "approval":
        with SessionLocal() as db:
            c = db.get(models.CaseApplication, cid)
            c.status, c.fatwa_status = "fatwa_approved", "approved"
            db.get(models.Invoice, iid).status = "paid"
            db.add(models.Payment(case_id=cid, invoice_id=iid, amount=100, currency="KRW", status="confirmed"))
            product = models.Product(case_id=cid, name="Reviewed product")
            material = models.Material(case_id=cid, name="gelatin")
            db.add_all([product, material])
            db.flush()
            mid = material.material_id
            db.add(models.ProductMaterial(case_id=cid, product_id=product.product_id, material_id=mid))
            # D1 발급 전제: 제품·원재료·연결을 포함한 실제 동결 스냅샷을 남긴다.
            seed_reviewed_issue_prerequisites(db, c)
            sm.freeze_scope(db, c)
            db.commit()
        r = api.post(f"/cases/{cid}/materials/{mid}/evidence", json={
            "filename": "source.png", "evidence_type": "source_declaration",
            "file_b64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="})
        assert r.status_code == 200, r.text
        r = api.get(f"/cases/{cid}/materials/{mid}/evidence")
        assert r.status_code == 200, r.text
        did = r.json()[0]["document_id"]
        # 증빙 제출만으로는 부족하므로 검토자 권한(admin)으로 승인한다.
        r = api.patch(f"/documents/{did}/review", json={"review_status": "approved"})
        assert r.status_code == 200, r.text
        call = lambda _: api.post(f"/cases/{cid}/certificate/issue", json={"reason": "d3"}, headers=headers)
    elif operation == "pay":
        call = lambda _: api.patch(f"/invoices/{iid}/pay", headers=headers)
    else:
        body = dict(order_id=ref, transaction_status="settlement", amount=100, currency="KRW", event_id=uuid4().hex)
        call = lambda _: api.post("/pg/webhook/manual", json=body)
    responses = concurrent(call)
    assert [r.status_code for r in responses] == [200] * 4, [r.text for r in responses]
    assert all(r.json() == responses[0].json() for r in responses)
    with SessionLocal() as db:
        if operation == "quote":
            assert db.query(models.GeneratedDocument).filter_by(case_id=cid, doc_type="quotation").count() == 1
            assert db.query(models.WorkflowEvent).filter_by(case_id=cid, action="quote.issued").count() == 1
        elif operation == "approval":
            assert db.query(models.ApprovalRequest).filter_by(case_id=cid, status="pending").count() == 1
        else:
            assert db.query(models.Payment).filter_by(invoice_id=iid).count() == 1
            assert balance(iid) == 100


def test_notification_partial_only_retries_failed_channel(api, monkeypatch):
    cid, _, _ = invoice()
    calls = []
    def dispatch(n, contacts=None, channels=None):
        if n.event_type != "d3":
            return [{"channel": ch, "ok": True} for ch in channels]
        calls.append(channels)
        if channels == ["sms"]:
            return [{"channel": "sms", "ok": False, "reason": "no_contact"}]
        return [{"channel": ch, "ok": ch == "inapp" or calls.count(["email"]) > 1, "reason": "timeout"} for ch in channels]
    monkeypatch.setattr(notify, "dispatch", dispatch)
    with SessionLocal() as db:
        n = models.Notification(case_id=cid, org_id="org_demo", event_type="d3", channels=["inapp", "email", "sms"], status="unsent")
        db.add(n)
        db.commit()
        main.drain_notifications(db, batch=10000)
        db.refresh(n)
        assert n.status == "partial"
        assert n.channel_status["inapp"]["status"] == "sent"
        assert n.channel_status["email"]["status"] == "failed"
        assert n.channel_status["sms"] == {"status": "skipped", "reason": "no_contact", "attempts": 0, "retryable": False}
        assert n.retry_pending is True
        main.drain_notifications(db, batch=10000)
        db.refresh(n)
        assert n.status == "sent"
        assert n.channel_status["email"]["attempts"] == 2
        assert n.retry_pending is False
        main.drain_notifications(db, batch=10000)
    assert calls == [["inapp"], ["email"], ["sms"], ["email"]]


@pytest.mark.parametrize("chain", ["workflow", "audit"])
def test_tail_deletion_is_detected(api, chain):
    cid, _, _ = invoice()
    with SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        for i in range(2):
            if chain == "workflow":
                sm.record_event(db, c, c.status, c.status, "d3.event", payload={"i": i})
            else:
                main._audit(db, {"uid": "d3", "role": "admin"}, "d3.audit", case_id=cid, commit=False)
        db.commit()
    url = f"/cases/{cid}/audit-verify" if chain == "workflow" else "/admin/audit-log-verify"
    assert api.get(url).json()["integrity_ok"]
    with SessionLocal() as db:
        q = db.query(models.WorkflowEvent).filter_by(case_id=cid) if chain == "workflow" else db.query(models.AuditLog)
        model = models.WorkflowEvent if chain == "workflow" else models.AuditLog
        row = q.order_by(model.created_at.desc()).first()
        db.delete(row)
        db.commit()
    result = api.get(url).json()
    assert result["integrity_ok"] is False, result
    assert result["anchor_ok"] is False


@pytest.mark.parametrize("operation", ["quote", "payment", "deposit", "pg"])
def test_reusing_key_with_different_input_is_conflict(api, operation):
    cid, iid, ref = invoice()
    headers = {"Idempotency-Key": uuid4().hex}
    if operation == "quote":
        url, first, second = f"/cases/{cid}/quote", {"note": "first"}, {"note": "second"}
    elif operation == "payment":
        url = f"/invoices/{iid}/payment"
        first, second = {"method": "manual", "amount": 40}, {"method": "manual", "amount": 60}
    elif operation == "deposit":
        url = "/admin/deposits"
        first, second = {"amount": 40, "ref_memo": ref}, {"amount": 60, "ref_memo": ref}
    else:
        url = "/pg/webhook/manual"
        first = dict(order_id=ref, transaction_status="settlement", amount=100, currency="KRW", event_id=uuid4().hex)
        second = dict(first, amount=1)
    assert api.post(url, json=first, headers=headers).status_code == 200
    r = api.post(url, json=second, headers=headers)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_distinct_threaded_quotes_have_distinct_versions(api):
    cid, _, _ = invoice()
    results = concurrent(lambda i: api.post(f"/cases/{cid}/quote", json={"note": str(i)}))
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    assert sorted(r.json()["version"] for r in results) == [1, 2, 3, 4]


def test_deposit_without_key_retries_deduplicated(api):
    _, iid, ref = invoice()
    body = {"amount": 100, "ref_memo": ref, "bank_name": "d3"}
    results = concurrent(lambda _: api.post("/admin/deposits", json=body))
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    assert len({r.json()["deposit_id"] for r in results}) == 1
    assert balance(iid) == 100


def test_deposit_currency_is_not_overwritten_by_contract(api):
    _, iid, ref = invoice(currency="USD")
    r = api.post("/admin/deposits", json={"amount": 100, "ref_memo": ref, "currency": "KRW"})
    assert r.status_code == 200 and r.json()["verdict"] == "DEFER", r.text
    with SessionLocal() as db:
        dep = db.get(models.Deposit, r.json()["deposit_id"])
        assert dep.currency == "KRW"
        candidate = db.query(models.PaymentMatchCandidate).filter_by(deposit_id=dep.id, invoice_id=iid).one()
        candidate_id = candidate.id
    assert api.post(f"/admin/match/{candidate_id}/decide", json={"decision": "approved"}).status_code == 409
    assert balance(iid) == 0


def test_partial_deposit_match_does_not_claim_full_payment(api):
    _, iid, ref = invoice()
    r = api.post("/admin/deposits", json={"amount": 40, "ref_memo": ref})
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        candidate_id = db.query(models.PaymentMatchCandidate).filter_by(deposit_id=r.json()["deposit_id"], invoice_id=iid).one().id
    results = concurrent(lambda _: api.post(f"/admin/match/{candidate_id}/decide", json={"decision": "approved"}))
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    assert balance(iid) == 40
    with SessionLocal() as db:
        assert db.get(models.Invoice, iid).status == "need_verification"
    r = api.post("/admin/deposits", json={"amount": 60, "ref_memo": ref, "transaction_id": uuid4().hex})
    assert r.status_code == 200 and r.json()["verdict"] == "COMMIT", r.text
    assert balance(iid) == 100


def test_pending_claim_and_pg_receipt_count_money_once(api):
    _, iid, ref = invoice()
    assert api.post(f"/invoices/{iid}/payment", json={"method": "bank_transfer", "amount": 100}).status_code == 200
    assert hook(api, ref).status_code == 200
    assert balance(iid) == 100
    with SessionLocal() as db:
        assert db.query(models.Payment).filter_by(invoice_id=iid, status="confirmed").count() == 1


@pytest.mark.parametrize("amount", [0, -1, "NaN", "Infinity", 1.001])
def test_invalid_record_amount_has_no_side_effects(api, amount):
    _, iid, _ = invoice()
    r = api.post(f"/invoices/{iid}/payment", json={"method": "manual", "amount": amount})
    assert r.status_code in (409, 422), r.text
    with SessionLocal() as db:
        assert db.get(models.Invoice, iid).status == "waiting_payment"
        assert db.query(models.Payment).filter_by(invoice_id=iid).count() == 0


def test_manual_payment_currency_and_partial_confirmation(api):
    _, iid, _ = invoice()
    assert api.post(f"/invoices/{iid}/payment", json={"method": "manual", "amount": 100, "currency": "USD"}).status_code == 409
    assert api.post(f"/invoices/{iid}/payment", json={"method": "manual", "amount": 40, "reference": "first"}).status_code == 200
    assert api.patch(f"/invoices/{iid}/pay").status_code == 409
    assert api.post(f"/invoices/{iid}/payment", json={"method": "manual", "amount": 60, "reference": "second"}).status_code == 200
    assert api.patch(f"/invoices/{iid}/pay").status_code == 200
    assert balance(iid) == 100


@pytest.mark.parametrize("kind", ["closed", "active", "declined"])
def test_auditor_limit_counts_active_assignments_only(api, kind):
    with SessionLocal() as db:
        auditor = models.User(username="d3_auditor_" + uuid4().hex, role="auditor", org_id="org_demo", password_hash="unused")
        db.add(auditor)
        db.flush()
        uid = auditor.user_id
        states = ["certificate_issued", "post_certification_monitoring", "withdrawn", "rejected", "cancelled", "revoked"]
        for i in range(6):
            c = models.CaseApplication(org_id="org_demo", status=states[i] if kind == "closed" else "consultant_review")
            db.add(c)
            db.flush()
            sm.record_event(db, c, c.status, c.status, "ops.auditor_assigned", payload={"auditor_id": uid})
            if kind == "declined":
                sm.record_event(db, c, c.status, c.status, main.ASSIGN_RESPONSE_ACTION,
                                payload={"auditor_id": uid, "decision": "rejected"})
        db.commit()
    cid, _, _ = invoice()
    r = api.post(f"/ops/cases/{cid}/assign-auditor", json={"auditor_id": uid})
    if kind == "active":
        assert r.status_code == 409 and r.json()["detail"]["code"] == "AUDITOR_OVERLOADED", r.text
    else:
        assert r.status_code == 200 and r.json()["load"] == 1, r.text


@pytest.mark.parametrize("reason,expected_status,attempts,dispatch_count", [
    ("no_contact", "skipped", 0, 1),
    ("no_credentials", "skipped", 0, 1),
    ("not_implemented", "skipped", 0, 1),
    ("consent_skipped", "skipped", 0, 1),
    ("timeout", "failed", 3, 3),
    ("no_result", "failed", 3, 3),
])
def test_notification_failure_or_skip_and_retry_budget(api, monkeypatch, reason, expected_status, attempts, dispatch_count):
    cid, _, _ = invoice()
    calls = []
    def dispatch(n, contacts=None, channels=None):
        if n.event_type == "d3.fail":
            calls.append(channels)
            if reason == "no_result":
                return []
            return [{"channel": ch, "ok": False, "reason": reason} for ch in channels]
        return [{"channel": ch, "ok": True} for ch in channels]
    monkeypatch.setattr(notify, "dispatch", dispatch)
    with SessionLocal() as db:
        db.get(models.CaseApplication, cid).email = "owner@example.test"
        n = models.Notification(case_id=cid, event_type="d3.fail", channels=["email"], status="unsent")
        db.add(n)
        db.commit()
        for _ in range(5):
            main.drain_notifications(db, batch=10000)
        db.refresh(n)
        assert n.status == expected_status
        assert n.retry_pending is False
        assert n.attempts == attempts
        assert n.channel_status["email"]["status"] == expected_status
        assert n.channel_status["email"]["attempts"] == attempts
        assert n.channel_status["email"]["retryable"] is False
        assert n.channel_status["email"]["reason"] == reason
        assert db.query(models.MailLog).filter_by(notification_id=n.notification_id).count() == attempts
    assert len(calls) == dispatch_count


@pytest.mark.parametrize("sms_reason,sms_status,sms_attempts", [
    ("no_contact", "skipped", 0),
    ("timeout", "failed", 3),
])
def test_notification_only_attempted_channels_fail(api, monkeypatch, sms_reason, sms_status, sms_attempts):
    cid, _, _ = invoice()
    calls = []
    def dispatch(n, contacts=None, channels=None):
        if n.event_type != "d3.failed":
            return [{"channel": ch, "ok": True} for ch in channels]
        calls.extend(channels)
        return [{"channel": ch, "ok": False, "reason": sms_reason if ch == "sms" else "timeout"} for ch in channels]
    monkeypatch.setattr(notify, "dispatch", dispatch)
    with SessionLocal() as db:
        n = models.Notification(case_id=cid, event_type="d3.failed", channels=["email", "sms"], status="unsent")
        db.add(n)
        db.commit()
        main.drain_notifications(db, batch=10000)
        db.refresh(n)
        assert n.status == "failed" and n.retry_pending is True
        for _ in range(4):
            main.drain_notifications(db, batch=10000)
        db.refresh(n)
        assert n.status == "failed" and n.retry_pending is False
        assert n.channel_status["sms"]["status"] == sms_status
        assert n.channel_status["sms"]["attempts"] == sms_attempts
        assert n.channel_status["email"]["attempts"] == 3
    assert calls.count("email") == 3
    assert calls.count("sms") == (1 if sms_status == "skipped" else 3)


@pytest.mark.parametrize("channels,expected_status,expected_calls", [
    (["email", "sms"], "skipped", []),
    (["inapp", "email", "sms"], "sent", ["inapp"]),
])
def test_notification_no_consent_skips_user_channels(api, monkeypatch, channels, expected_status, expected_calls):
    cid, _, _ = invoice()
    calls = []
    def dispatch(n, contacts=None, channels=None):
        if n.event_type == "d3.consent":
            calls.extend(channels)
        return [{"channel": ch, "ok": True} for ch in channels]
    monkeypatch.setattr(notify, "dispatch", dispatch)
    with SessionLocal() as db:
        db.get(models.CaseApplication, cid).notify_consent = False
        n = models.Notification(case_id=cid, event_type="d3.consent", channels=channels, status="unsent")
        db.add(n)
        db.commit()
        for _ in range(2):
            main.drain_notifications(db, batch=10000)
        db.refresh(n)
        assert n.status == expected_status and n.retry_pending is False
        assert n.attempts == len(expected_calls)
        for ch in ("email", "sms"):
            assert n.channel_status[ch] == {"status": "skipped", "reason": "consent_skipped", "attempts": 0, "retryable": False}
    assert calls == expected_calls


@pytest.mark.parametrize("channels,expected_status,attempts", [
    (["d3.unimplemented"], "skipped", 0),
    (["inapp", "d3.unimplemented"], "sent", 1),
])
def test_notification_unimplemented_channel_is_skipped(api, monkeypatch, channels, expected_status, attempts):
    cid, _, _ = invoice()
    calls = []
    original_dispatch = notify.dispatch
    def dispatch(n, contacts=None, channels=None):
        if n.event_type != "d3.unimplemented":
            return [{"channel": ch, "ok": True} for ch in channels]
        calls.extend(channels)
        return original_dispatch(n, contacts=contacts, channels=channels)
    monkeypatch.setattr(notify, "dispatch", dispatch)
    with SessionLocal() as db:
        n = models.Notification(case_id=cid, event_type="d3.unimplemented", channels=channels, status="unsent")
        db.add(n)
        db.commit()
        for _ in range(2):
            main.drain_notifications(db, batch=10000)
        db.refresh(n)
        assert n.status == expected_status and n.retry_pending is False
        assert n.attempts == attempts
        assert n.channel_status["d3.unimplemented"] == {"status": "skipped", "reason": "not_implemented", "attempts": 0, "retryable": False}
    assert calls == channels


def test_notification_concurrent_drains_claim_once(api, monkeypatch):
    cid, _, _ = invoice()
    calls = []
    def dispatch(n, contacts=None, channels=None):
        if n.event_type == "d3.concurrent":
            calls.append(channels)
        return [{"channel": ch, "ok": True} for ch in channels]
    monkeypatch.setattr(notify, "dispatch", dispatch)
    with SessionLocal() as db:
        db.add(models.Notification(case_id=cid, event_type="d3.concurrent", channels=["inapp"], status="unsent"))
        db.commit()
    def drain(_):
        with SessionLocal() as db:
            return main.drain_notifications(db, batch=10000)
    concurrent(drain)
    assert calls == [["inapp"]]


def test_workflow_anchor_survives_deletion_then_append_and_restart(api):
    cid, _, _ = invoice()
    with SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        sm.record_event(db, c, c.status, c.status, "first")
        last = sm.record_event(db, c, c.status, c.status, "second")
        db.commit()
        db.delete(last)
        db.commit()
        sm.record_event(db, c, c.status, c.status, "replacement")
        db.commit()
        main._bootstrap_chain_anchors(db)
        db.rollback()
    result = api.get(f"/cases/{cid}/audit-verify").json()
    assert not result["integrity_ok"] and not result["anchor_ok"]


def test_chain_anchor_rolls_back_with_event(api):
    cid, _, _ = invoice()
    with SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        sm.record_event(db, c, c.status, c.status, "rolled_back")
        db.rollback()
        assert db.get(models.ChainAnchor, "wf:" + cid) is None
        assert db.query(models.WorkflowEvent).filter_by(case_id=cid).count() == 0


def test_database_constraints_protect_duplicate_business_keys(api):
    cid, iid, _ = invoice()
    factories = [
        lambda: models.Payment(case_id=cid, invoice_id=iid, amount=100, source_key="d3.unique.payment"),
        lambda: models.Deposit(amount=100, source_key="d3.unique.deposit"),
        lambda: models.GeneratedDocument(case_id=cid, doc_type="quotation", version=1),
        lambda: models.ApprovalRequest(case_id=cid, action_type="certificate.issue", requested_by="d3", status="pending"),
        lambda: models.IntegrationEvent(case_id=cid, event_type="pg.webhook.paid", idempotency_key="d3.unique.pg"),
    ]
    for create in factories:
        with SessionLocal() as db:
            db.add(create())
            db.commit()
            db.add(create())
            with pytest.raises(IntegrityError):
                db.commit()
            db.rollback()


def test_existing_schema_migration_adds_columns_and_enforces_unique_indexes(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, inspect
    from app.db import Base
    engine = create_engine("sqlite:///" + str(tmp_path / "old.db"))
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE payment (id TEXT PRIMARY KEY, amount FLOAT)")
            conn.exec_driver_sql("INSERT INTO payment (id, amount) VALUES ('legacy', 100)")
            conn.exec_driver_sql("CREATE TABLE notification (notification_id TEXT PRIMARY KEY, status TEXT)")
        Base.metadata.create_all(engine)
        monkeypatch.setattr(main, "engine", engine)
        main._migrate()
        main._ensure_payment_indexes()
        main._ensure_payment_indexes()  # 재기동도 멱등
        assert {c["name"] for c in inspect(engine).get_columns("notification")} >= {"channel_status", "claimed_at", "retry_pending"}
        assert any(i["name"] == "uq_payment_source" and i["unique"] for i in inspect(engine).get_indexes("payment"))
        with engine.begin() as conn:
            assert conn.exec_driver_sql("SELECT amount FROM payment WHERE id = 'legacy'").scalar_one() == 100
            conn.exec_driver_sql("DROP INDEX uq_approval_pending")
            conn.exec_driver_sql("INSERT INTO approval_request (id, case_id, action_type, requested_by, status) VALUES ('a', 'case', 'certificate.issue', 'u', 'pending'), ('b', 'case', 'certificate.issue', 'u', 'pending')")
        with pytest.raises(IntegrityError):
            main._ensure_payment_indexes()  # 기존 중복을 숨기고 제약 없이 기동하지 않는다.
    finally:
        engine.dispose()


def test_existing_chain_baseline_is_created_once(tmp_path):
    from datetime import datetime, timedelta
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.db import Base
    engine = create_engine("sqlite:///" + str(tmp_path / "legacy_chain.db"))
    Base.metadata.create_all(engine)
    try:
        # 이전 버전에서 이미 기록한 이벤트: ORM insert hook을 거치지 않는 기존 행.
        with engine.begin() as conn:
            conn.execute(models.WorkflowEvent.__table__.insert(), [
                dict(event_id="a", case_id="legacy", action="legacy", row_hash="old-a", created_at=datetime(2020, 1, 1)),
                dict(event_id="b", case_id="legacy", action="legacy", row_hash="old-b", created_at=datetime(2020, 1, 1) + timedelta(seconds=1)),
            ])
        with Session(engine) as db:
            main._bootstrap_chain_anchors(db)
            assert db.get(models.ChainAnchor, "wf:legacy").event_count == 2
            db.delete(db.get(models.WorkflowEvent, "b"))
            db.commit()
            main._bootstrap_chain_anchors(db)
            result = main._verify_wf_chain(db.query(models.WorkflowEvent).filter_by(case_id="legacy").all(), db=db, case_id="legacy")
            assert not result["integrity_ok"] and not result["anchor_ok"]
    finally:
        engine.dispose()


def test_payment_reference_is_unique_across_operators_and_request_keys(api):
    _, iid, ref = invoice()
    body = {"method": "bank_transfer", "amount": 100, "reference": ref}
    first = api.post(f"/invoices/{iid}/payment", json=body, headers={"Idempotency-Key": "first"})
    token = api.post("/auth/login", json={"username": "operator1", "password": "pw"}).json()["token"]
    second = api.post(f"/invoices/{iid}/payment", json=body,
                      headers={"Authorization": "Bearer " + token, "Idempotency-Key": "second"})
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert api.patch(f"/invoices/{iid}/pay").status_code == 200
    assert balance(iid) == 100


def test_request_key_conflict_still_checked_when_reference_changes(api):
    _, iid, ref = invoice()
    headers = {"Idempotency-Key": uuid4().hex}
    body = {"method": "manual", "amount": 40, "reference": ref}
    assert api.post(f"/invoices/{iid}/payment", json=body, headers=headers).status_code == 200
    r = api.post(f"/invoices/{iid}/payment", json=dict(body, reference=ref + "changed"), headers=headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_manual_refund_uses_same_net_balance_as_pg(api):
    _, iid, _ = invoice("paid", paid=100)
    r = api.post(f"/invoices/{iid}/refund/request", json={"amount": 30, "reason": "partial"})
    assert r.status_code == 200, r.text
    checker = api.post("/auth/login", json={"username": "operator1", "password": "pw"}).json()["token"]
    r = api.post(f"/refunds/{r.json()['refund_id']}/decide", json={"decision": "approved"},
                 headers={"Authorization": "Bearer " + checker})
    assert r.status_code == 200 and r.json()["invoice_status"] == "partially_refunded", r.text
    assert balance(iid) == 70
    r = api.post(f"/invoices/{iid}/refund/request", json={"amount": 71, "reason": "excess"})
    assert r.status_code == 409, r.text


def test_receipt_gate_counts_only_invoice_currency(api):
    cid, iid, _ = invoice("paid")
    with SessionLocal() as db:
        db.add(models.Payment(case_id=cid, invoice_id=iid, amount=100, currency="USD", status="confirmed"))
        db.commit()
        assert main._guard_invoice_receipts(db, db.get(models.CaseApplication, cid))


def test_verification_does_not_report_concurrent_append_as_deletion(api):
    cid, _, _ = invoice()
    def work(i):
        if i % 2:
            with SessionLocal() as db:
                c = db.get(models.CaseApplication, cid)
                sm.record_event(db, c, c.status, c.status, "concurrent.append")
                db.commit()
            return True
        r = api.get(f"/cases/{cid}/audit-verify")
        assert r.status_code == 200, r.text
        return r.json()["integrity_ok"]
    assert all(concurrent(work, n=8))


def test_channel_exception_preserves_previous_channel_success(api, monkeypatch):
    cid, _, _ = invoice()
    calls = []
    def dispatch(n, contacts=None, channels=None):
        if n.event_type == "d3.exception":
            calls.extend(channels)
            if channels == ["email"]:
                raise RuntimeError("email unavailable")
        return [{"channel": ch, "ok": True} for ch in channels]
    monkeypatch.setattr(notify, "dispatch", dispatch)
    with SessionLocal() as db:
        n = models.Notification(case_id=cid, event_type="d3.exception", channels=["inapp", "email"], status="unsent")
        db.add(n)
        db.commit()
        for _ in range(2):
            main.drain_notifications(db, batch=10000)
        db.refresh(n)
        assert n.status == "partial" and n.channel_status["inapp"]["status"] == "sent"
    assert calls == ["inapp", "email", "email"]
