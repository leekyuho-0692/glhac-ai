"""2026-10 감사 중간 결함 묶음 MA(결제·무결성) MA1~MA7 테스트.

모든 테스트는 실제 엔드포인트/함수를 호출한다(구조 확인만 하는 단언 금지).
각 테스트 주석에 '수정 전 코드에서 왜 실패하는지'를 적는다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_auditmid_a_test.db")
os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_NOTIFY_WORKER", "0")
os.environ.setdefault("GLHAC_RATE_LIMIT_PER_MIN", "0")
os.environ.pop("GLHAC_PG_WEBHOOK_SECRET", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app import models  # noqa: E402
from app.db import SessionLocal  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------- 헬퍼 ----------
def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _op(c):
    return _tok(c, "operator1", "pw")


def _db_case(company="MA-Co"):
    db = SessionLocal()
    try:
        c = models.CaseApplication(org_id="org_demo", company_name=company, status="onboarding")
        db.add(c)
        db.commit()
        return c.case_id
    finally:
        db.close()


def _add(obj):
    db = SessionLocal()
    try:
        db.add(obj)
        db.commit()
        db.refresh(obj)
        db.expunge(obj)
        return obj
    finally:
        db.close()


def _contract(cid, currency="KRW", status="confirmed"):
    return _add(models.Contract(case_id=cid, currency=currency, status=status)).contract_id


def _invoice(cid, status="waiting_payment", total=1_100_000.0, ref=None):
    return _add(models.Invoice(case_id=cid, service_type="pre_audit", amount=total / 1.1,
                               ppn=total - total / 1.1, total=total, status=status,
                               payment_ref=ref)).invoice_id


def _payments(iid):
    db = SessionLocal()
    try:
        return db.query(models.Payment).filter_by(invoice_id=iid).all()
    finally:
        db.close()


def _inv_status(iid):
    db = SessionLocal()
    try:
        return db.get(models.Invoice, iid).status
    finally:
        db.close()


def _code(r):
    d = r.json().get("detail")
    return d.get("code") if isinstance(d, dict) else None


# ======== MA1: 통화 KRW 통일 — 계약 통화에서 가져온다 ========

def test_ma1_모델_기본통화는_KRW():
    """Payment/Deposit 기본 통화 KRW (수정 전 IDR)."""
    cid = _db_case()
    p = _add(models.Payment(invoice_id="x", case_id=cid, amount=1.0, method="manual"))
    d = _add(models.Deposit(bank_name="B", amount=1.0))
    assert p.currency == "KRW" and d.currency == "KRW"


def test_ma1_결제기록은_계약통화를_따른다():
    """POST /invoices/{id}/payment — 계약 통화(USD)가 Payment 에 기록된다.
    수정 전: currency 미지정이라 모델 기본값 IDR 이 박혀 USD 가 되지 않는다."""
    cid = _db_case()
    _contract(cid, currency="USD")
    iid = _invoice(cid)
    with TestClient(app) as c:
        r = c.post(f"/invoices/{iid}/payment", headers=_op(c),
                   json={"method": "bank_transfer", "amount": 1_100_000.0})
        assert r.status_code == 200, r.text
    assert [p.currency for p in _payments(iid)] == ["USD"]


def test_ma1_계약없으면_KRW_수기확정_결제도_계약통화():
    """PATCH status=paid 의 수기 Payment — 계약 없으면 KRW, 있으면 계약 통화.
    수정 전: 하드코딩 "IDR"."""
    cid = _db_case()
    iid = _invoice(cid)
    cid2 = _db_case("MA-Co2")
    _contract(cid2, currency="USD")
    iid2 = _invoice(cid2)
    with TestClient(app) as c:
        h = _op(c)
        for i in (iid, iid2):
            r = c.patch(f"/invoices/{i}/status", headers=h, json={"status": "paid", "reason": "수기확인"})
            assert r.status_code == 200, r.text
    assert [p.currency for p in _payments(iid)] == ["KRW"]
    assert [p.currency for p in _payments(iid2)] == ["USD"]


def test_ma1_PG웹훅_결제는_계약통화():
    """POST /pg/webhook/manual settlement — 수정 전 하드코딩 "IDR"."""
    cid = _db_case()
    _contract(cid, currency="USD")
    iid = _invoice(cid, ref="ORD-MA1-WH", total=1_100_000.0)
    with TestClient(app) as c:
        r = c.post("/pg/webhook/manual", json={"order_id": "ORD-MA1-WH", "transaction_status": "settlement",
                                               "gross_amount": "1100000.00", "event_id": "evt-ma1-wh"})
        assert r.status_code == 200, r.text
    assert _inv_status(iid) == "paid"
    assert [p.currency for p in _payments(iid)] == ["USD"]


def test_ma1_입금자동확정은_Deposit_Payment_모두_계약통화():
    """POST /admin/deposits COMMIT — 입금(Deposit)·결제(Payment) 통화가 계약 통화.
    수정 전: Deposit 은 기본값 IDR, Payment 도 IDR."""
    cid = _db_case()
    _contract(cid, currency="USD")
    iid = _invoice(cid, ref="PAY-MA1-DEP", total=1_100_000.0)
    with TestClient(app) as c:
        r = c.post("/admin/deposits", headers=_op(c),
                   json={"bank_name": "B", "account_no": "1234567890", "depositor_name": "MA-Co",
                         "amount": 1_100_000.0, "ref_memo": "PAY-MA1-DEP"})
        assert r.status_code == 200, r.text
        assert r.json()["verdict"] == "COMMIT", r.text
        dep_id = r.json()["deposit_id"]
    db = SessionLocal()
    try:
        assert db.get(models.Deposit, dep_id).currency == "USD"
    finally:
        db.close()
    assert [p.currency for p in _payments(iid)] == ["USD"]


def test_ma1_기본_부가세율_0_10_저장값은_존중():
    """GET /billing/settings 기본 0.10, 저장한 값(0.11)은 그대로(수정 전 기본 0.11)."""
    with TestClient(app) as c:
        h = _op(c)
        s = c.get("/billing/settings", headers=h).json()
        assert s["ppn_rate"] == 0.10
        body = {k: s[k] for k in ("sector_base", "per_product_extra", "onsite_travel", "options",
                                  "payment_terms", "due_days", "auto_notice_contract",
                                  "auto_notice_paid", "bank_account")}
        r = c.post("/billing/settings", headers=h, json=dict(body, ppn_rate=0.11))
        assert r.status_code == 200, r.text
        assert c.get("/billing/settings", headers=h).json()["ppn_rate"] == 0.11
        r = c.post("/billing/settings", headers=h, json=dict(body, ppn_rate=0.10))
        assert r.status_code == 200, r.text


def test_ma1_청구서_생성_부가세는_설정_기본_10퍼센트():
    """POST /cases/{id}/invoices — PPN = 금액 x ppn_rate(기본 0.10). 수정 전 0.11 하드코딩."""
    cid = _db_case()
    _contract(cid)
    with TestClient(app) as c:
        r = c.post(f"/cases/{cid}/invoices", headers=_tok(c, "consultant1", "pw"),
                   json={"service_type": "pre_audit", "amount": 1_000_000})
        assert r.status_code == 200, r.text
        assert r.json()["ppn"] == 100_000.0 and r.json()["total"] == 1_100_000.0
