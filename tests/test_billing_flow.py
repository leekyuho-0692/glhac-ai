"""BILLING FLOW — 견적·계약·전자서명·회차·입금 고지·확인 흐름 (GLHAC_FORMAL_FLOW off)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_billing_test.db")
os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_NOTIFY_WORKER", "0")
os.environ.pop("GLHAC_FORMAL_FLOW", None)
from datetime import datetime, timedelta  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app                    # noqa: E402
from app import models                      # noqa: E402
from app.db import SessionLocal             # noqa: E402


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _mkcase(c, admin):
    r = c.post("/cases", headers=admin, json={"org_id": "org_demo", "company_name": "BillingCo"})
    assert r.status_code == 200, r.text
    return r.json()["case_id"]


def _quote(c, operator, cid, body=None):
    r = c.post("/cases/%s/quote" % cid, headers=operator, json=(body or {}))
    assert r.status_code == 200, r.text
    return r.json()


def _contract(c, cid, applicant, operator):
    r = c.post("/cases/%s/contract/request" % cid, headers=applicant)
    assert r.status_code == 200, r.text
    r = c.post("/cases/%s/contract/approve" % cid, headers=operator, json={})
    assert r.status_code == 200, r.text
    return r.json()["contract_id"]


def _sign(c, cid, applicant, operator):
    r = c.post("/contracts/%s/sign" % cid, headers=applicant, params={"party": "A", "name": "대표"})
    assert r.status_code == 200, r.text
    r = c.post("/contracts/%s/sign" % cid, headers=operator, params={"party": "B", "name": "GLHAC"})
    assert r.status_code == 200, r.text
    return r.json()


def _settings(c, operator, **kw):
    r = c.get("/billing/settings", headers=operator)
    assert r.status_code == 200, r.text
    body = {"sector_base": r.json()["sector_base"], "per_product_extra": r.json()["per_product_extra"],
            "onsite_travel": r.json()["onsite_travel"], "options": r.json()["options"],
            "ppn_rate": r.json()["ppn_rate"], "payment_terms": r.json()["payment_terms"],
            "due_days": r.json()["due_days"], "auto_notice_contract": r.json()["auto_notice_contract"],
            "auto_notice_paid": r.json()["auto_notice_paid"], "bank_account": r.json()["bank_account"]}
    body.update(kw)
    r = c.post("/billing/settings", headers=operator, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _reset_settings(c, operator):
    _settings(c, operator, payment_terms="split50", due_days=7)


def _invoices(c, operator, cid):
    r = c.get("/cases/%s/invoices" % cid, headers=operator)
    assert r.status_code == 200, r.text
    return r.json()


def _board(c, operator, cid=None, filt="all"):
    r = c.get("/admin/billing/board", headers=operator, params={"filter": filt})
    assert r.status_code == 200, r.text
    data = r.json()
    if cid is None:
        return data
    for it in (data["items"] if isinstance(data, dict) else data):
        if it["case_id"] == cid:
            return it
    return None


def test_설정_저장과_검증():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        operator = _tok(c, "operator1", "pw")
        applicant = _tok(c, "applicant1", "pw")
        got = _settings(c, operator, payment_terms="lump", due_days=10,
                        bank_account={"bank": "국민", "number": "123", "holder": "GL HAC"})
        assert got["payment_terms"] == "lump" and got["due_days"] == 10
        r = c.get("/billing/settings", headers=operator)
        assert r.status_code == 200, r.text
        assert r.json()["payment_terms"] == "lump"
        assert r.json()["bank_account"]["bank"] == "국민"
        r = c.post("/billing/settings", headers=operator, json={**_settings_payload(c, operator), "payment_terms": "x"})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "BAD_PAYMENT_TERMS", r.text
        r = c.post("/billing/settings", headers=operator, json={**_settings_payload(c, operator), "due_days": 0})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "BAD_DUE_DAYS", r.text
        r = c.get("/billing/settings", headers=applicant)
        assert r.status_code == 200, r.text
        assert "sector_base" not in r.json()
        _reset_settings(c, operator)


def _settings_payload(c, operator):
    r = c.get("/billing/settings", headers=operator)
    assert r.status_code == 200, r.text
    j = r.json()
    return {"sector_base": j["sector_base"], "per_product_extra": j["per_product_extra"],
            "onsite_travel": j["onsite_travel"], "options": j["options"], "ppn_rate": j["ppn_rate"],
            "payment_terms": j["payment_terms"], "due_days": j["due_days"],
            "auto_notice_contract": j["auto_notice_contract"], "auto_notice_paid": j["auto_notice_paid"],
            "bank_account": j["bank_account"]}


def test_견적_산출_근거와_버전():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        operator = _tok(c, "operator1", "pw")
        cid = _mkcase(c, admin)
        q1 = _quote(c, operator, cid, {"product_count": 3, "onsite": True, "options": ["sihalal_registration"]})
        assert q1["subtotal"] == 9100000
        assert q1["ppn"] == 1001000
        assert q1["total"] == 10101000
        codes = {l["code"] for l in q1["lines"]}
        assert {"base", "product_extra", "onsite_travel", "opt:sihalal_registration"} <= codes
        q2 = _quote(c, operator, cid, {"product_count": 1, "onsite": False})
        assert q2["version"] == 2
        assert q2["total"] == round(6500000 * 1.11)
        r = c.get("/cases/%s/quote" % cid, headers=operator)
        assert r.status_code == 200, r.text
        assert len(r.json()["history"]) == 2


def test_견적_변경요청_반려_재발행():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        operator = _tok(c, "operator1", "pw")
        consultant = _tok(c, "consultant1", "pw")
        cid = _mkcase(c, admin)
        r = c.post("/cases/%s/quote/change-request" % cid, headers=consultant, json={"reason": "금액"})
        assert r.status_code == 409, r.text
        _quote(c, operator, cid)
        r = c.post("/cases/%s/quote/change-request" % cid, headers=consultant, json={})
        assert r.status_code == 422, r.text
        r = c.post("/cases/%s/quote/change-request" % cid, headers=consultant, json={"reason": "금액 조정"})
        assert r.status_code == 200, r.text
        r = c.get("/cases/%s/quote" % cid, headers=operator)
        assert r.json()["pending_change"] is True
        r = c.post("/cases/%s/quote/change-reject" % cid, headers=operator, json={"reason": "반려"})
        assert r.status_code == 200, r.text
        r = c.get("/cases/%s/quote" % cid, headers=operator)
        assert r.json()["pending_change"] is False
        r = c.post("/cases/%s/quote/change-request" % cid, headers=consultant, json={"reason": "재요청"})
        assert r.status_code == 200, r.text
        _quote(c, operator, cid)
        r = c.get("/cases/%s/quote" % cid, headers=operator)
        assert r.json()["pending_change"] is False
        assert r.json()["quote"]["version"] == 2


def test_계약_당사자_가드():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        operator = _tok(c, "operator1", "pw")
        consultant = _tok(c, "consultant1", "pw")
        applicant = _tok(c, "applicant1", "pw")
        cid = _mkcase(c, admin)
        q = _quote(c, operator, cid)
        cid2 = _contract(c, cid, applicant, operator)
        r = c.post("/contracts/%s/sign" % cid2, headers=consultant, params={"party": "A", "name": "x"})
        assert r.status_code == 403 and r.json()["detail"]["code"] == "CONSULTANT_CANNOT_SIGN", r.text
        r = c.post("/contracts/%s/sign" % cid2, headers=applicant, params={"party": "B", "name": "x"})
        assert r.status_code == 403 and r.json()["detail"]["code"] == "NOT_PARTY_B", r.text
        r = c.post("/contracts/%s/sign" % cid2, headers=operator, params={"party": "A", "name": "x"})
        assert r.status_code == 403 and r.json()["detail"]["code"] == "NOT_PARTY_A", r.text
        r = c.post("/contracts/%s/sign" % cid2, headers=applicant, params={"party": "A", "name": "대표"})
        assert r.status_code == 200, r.text
        r = c.post("/contracts/%s/sign" % cid2, headers=applicant, params={"party": "A", "name": "대표"})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "ALREADY_SIGNED", r.text
        r = c.get("/cases/%s/contract" % cid, headers=operator)
        assert r.status_code == 200, r.text
        assert r.json()["fee"] == q["total"]


def test_체결_D09_회차_자동고지():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        operator = _tok(c, "operator1", "pw")
        applicant = _tok(c, "applicant1", "pw")
        _reset_settings(c, operator)
        cid = _mkcase(c, admin)
        q = _quote(c, operator, cid)
        cid2 = _contract(c, cid, applicant, operator)
        r = _sign(c, cid2, applicant, operator)
        assert r["status"] == "confirmed", r
        r = c.get("/cases/%s/gen-docs" % cid, headers=operator)
        assert r.status_code == 200, r.text
        assert any(d.get("doc_type") == "esign_certificate" for d in r.json())
        invs = _invoices(c, operator, cid)
        assert len(invs) == 2
        assert all(i["service_type"] == "installment" for i in invs)
        waiting = [i for i in invs if i["status"] == "waiting_payment"]
        drafts = [i for i in invs if i["status"] == "draft"]
        assert len(waiting) == 1 and len(drafts) == 1
        assert waiting[0]["due_date"]
        assert abs(sum(i["total"] for i in invs) - q["total"]) <= 1
        db = SessionLocal()
        try:
            n = db.query(models.Notification).filter_by(case_id=cid, event_type="billing.contract_signed").count()
        finally:
            db.close()
        assert n >= 1
        it = _board(c, operator, cid)
        assert it["signed_a"] is True and it["signed_b"] is True
        assert it["signed_notice_sent"] is True
        assert it["next_action"] == "notice_due"


def test_lump_회차_1개():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        operator = _tok(c, "operator1", "pw")
        applicant = _tok(c, "applicant1", "pw")
        _settings(c, operator, payment_terms="lump")
        try:
            cid = _mkcase(c, admin)
            q = _quote(c, operator, cid)
            cid2 = _contract(c, cid, applicant, operator)
            _sign(c, cid2, applicant, operator)
            invs = _invoices(c, operator, cid)
            assert len(invs) == 1
            assert abs(invs[0]["total"] - q["total"]) <= 1
        finally:
            _reset_settings(c, operator)


def test_입금고지_재고지_입금완료_확인_자동알림():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        operator = _tok(c, "operator1", "pw")
        applicant = _tok(c, "applicant1", "pw")
        _reset_settings(c, operator)
        cid = _mkcase(c, admin)
        _quote(c, operator, cid)
        cid2 = _contract(c, cid, applicant, operator)
        _sign(c, cid2, applicant, operator)
        invs = _invoices(c, operator, cid)
        iid = [i for i in invs if i["status"] == "waiting_payment"][0]["invoice_id"]
        r = c.post("/cases/%s/billing/notice" % cid, headers=operator,
                   json={"kind": "payment_due", "invoice_id": iid, "due_days": 7})
        assert r.status_code == 200, r.text
        invs = _invoices(c, operator, cid)
        target = [i for i in invs if i["invoice_id"] == iid][0]
        assert target["due_date"]
        it = _board(c, operator, cid)
        assert it["next_action"] == "wait_payment"
        db = SessionLocal()
        try:
            inv = db.query(models.Invoice).filter_by(invoice_id=iid).first()
            inv.due_date = datetime.utcnow() - timedelta(days=1)
            db.commit()
        finally:
            db.close()
        it = _board(c, operator, cid)
        rnd = [x for x in it["rounds"] if x["invoice_id"] == iid][0]
        assert rnd["overdue"] is True
        assert it["next_action"] == "notice_reminder"
        r = c.post("/cases/%s/billing/notice" % cid, headers=operator,
                   json={"kind": "payment_reminder", "invoice_id": iid})
        assert r.status_code == 200, r.text
        r = c.get("/cases/%s/billing/notices" % cid, headers=operator)
        assert r.status_code == 200, r.text
        assert any(n["kind"] == "payment_reminder" for n in r.json()["items"])
        r = c.post("/invoices/%s/paid-notice" % iid, headers=applicant, json={})
        assert r.status_code == 200, r.text
        it = _board(c, operator, cid)
        assert it["next_action"] == "confirm_paid"
        r = c.patch("/invoices/%s/status" % iid, headers=operator, json={"status": "paid", "reason": ""})
        assert r.status_code == 200, r.text
        db = SessionLocal()
        try:
            n = db.query(models.Notification).filter_by(case_id=cid, event_type="billing.payment_confirmed").count()
        finally:
            db.close()
        assert n >= 1
        r = c.get("/cases/%s/billing/notices" % cid, headers=operator)
        assert r.status_code == 200, r.text
        pc = [x for x in r.json()["items"] if x["kind"] == "payment_confirmed"]
        assert pc
        it = _board(c, operator, cid)
        assert it["next_action"] == "next_round"


def test_미리청구와_완납():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        operator = _tok(c, "operator1", "pw")
        applicant = _tok(c, "applicant1", "pw")
        _reset_settings(c, operator)
        cid = _mkcase(c, admin)
        _quote(c, operator, cid)
        cid2 = _contract(c, cid, applicant, operator)
        _sign(c, cid2, applicant, operator)
        invs = _invoices(c, operator, cid)
        draft = [i for i in invs if i["status"] == "draft"][0]["invoice_id"]
        r = c.post("/invoices/%s/bill-now" % draft, headers=operator)
        assert r.status_code == 200, r.text
        invs = _invoices(c, operator, cid)
        target = [i for i in invs if i["invoice_id"] == draft][0]
        assert target["status"] == "waiting_payment"
        assert target["due_date"]
        r = c.get("/cases/%s/billing/notices" % cid, headers=operator)
        assert r.status_code == 200, r.text
        assert any(n["kind"] == "payment_due" and n["invoice_id"] == draft for n in r.json()["items"])
        for i in invs:
            r = c.patch("/invoices/%s/status" % i["invoice_id"], headers=operator,
                        json={"status": "paid", "reason": ""})
            assert r.status_code == 200, r.text
        it = _board(c, operator, cid)
        assert it["settled"] is True
        assert it["next_action"] == "settled"
        b = _board(c, operator, None, filt="settled")
        assert any(x["case_id"] == cid for x in b["items"])
        b = _board(c, operator, None, filt="overdue")
        assert not any(x["case_id"] == cid for x in b["items"])


def test_고지_검증():
    with TestClient(app) as c:
        admin = _tok(c, "admin", "admin")
        operator = _tok(c, "operator1", "pw")
        cid = _mkcase(c, admin)
        r = c.post("/cases/%s/billing/notice" % cid, headers=operator, json={"kind": "x"})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "BAD_NOTICE_KIND", r.text
        r = c.post("/cases/%s/billing/notice" % cid, headers=operator, json={"kind": "payment_due"})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "INVOICE_REQUIRED", r.text
        r = c.post("/cases/%s/billing/notice" % cid, headers=operator,
                   json={"kind": "payment_due", "invoice_id": "inv_nope"})
        assert r.status_code == 404, r.text
