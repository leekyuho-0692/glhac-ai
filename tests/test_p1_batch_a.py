"""P1 배치 A 격리 테스트 — #1 다항목 청구 · #2 견적서 PDF · #7 파트와→오디터 반려.

주의(테스트 격리): tests 일괄 실행 시 공유 DB 상호오염 방지를 위해 app import 전에
고유 DB(glhac_p1a_test.db)를 강제 대입한다(setdefault 금지).
실행: <venv>/bin/python -m pytest tests/test_p1_batch_a.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["GLHAC_DB_URL"] = "sqlite:///./glhac_p1a_test.db"   # 격리 강제
os.environ["GLHAC_DEV"] = "1"                                   # 데모 계정 시드

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


def _mkcase(c, admin):
    return c.post("/cases", json={"org_id": "org_demo", "company_name": "P1BatchACo"},
                  headers=_h(admin)).json()["case_id"]


def _set_status(case_id, status):
    db = SessionLocal()
    try:
        c = db.get(models.CaseApplication, case_id)
        c.status = status
        db.commit()
    finally:
        db.close()


def _confirm_contract(case_id):
    """청구서 생성 게이트(CONTRACT_NOT_CONFIRMED) 충족용 confirmed 계약 삽입(픽스처).
    add_invoice가 계약 최종확인 후에만 청구서를 허용하므로 인보이스 POST 전에 필요."""
    db = SessionLocal()
    try:
        db.add(models.Contract(case_id=case_id, status="confirmed"))
        db.commit()
    finally:
        db.close()


def _count(case_id, model, **filt):
    db = SessionLocal()
    try:
        return db.query(model).filter_by(case_id=case_id, **filt).count()
    finally:
        db.close()


def test_a_multi_line_invoice_saves_items_and_sums():
    """(a) 다항목 청구 생성 → 라인아이템 저장·합계(DPP=Σamount, PPN 11%) 반영."""
    with TestClient(app) as c:
        con = _tok(c, "consultant1", "pw")
        cid = _mkcase(c, _tok(c, "admin", "admin"))
        _confirm_contract(cid)   # 청구서 생성 선행조건(계약 게이트) 충족
        items = [{"name": "심사료", "qty": 1, "unit_price": 6500000, "amount": 6500000},
                 {"name": "특수조항 가산", "qty": 2, "unit_price": 500000, "amount": 1000000}]
        r = c.post(f"/cases/{cid}/invoices",
                   json={"service_type": "pre_audit", "amount": 0, "line_items": items},
                   headers=_h(con))
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["line_items"]) == 2, body
        assert body["ppn"] == round(7500000 * 0.11, 2), body      # DPP=7.5M → PPN
        assert body["total"] == round(7500000 * 1.11, 2), body
        # 조회 병합(list) 시에도 라인아이템 반환
        lst = c.get(f"/cases/{cid}/invoices", headers=_h(con)).json()
        assert lst[0]["line_items"] and len(lst[0]["line_items"]) == 2, lst
        # 라인아이템은 WorkflowEvent(invoice.line_items)로 저장(스키마 무변경)
        assert _count(cid, models.WorkflowEvent, action="invoice.line_items") == 1


def test_b_quotation_pdf_signature():
    """(b) 견적서 PDF 200 + PDF 시그니처(%PDF)."""
    with TestClient(app) as c:
        con = _tok(c, "consultant1", "pw")
        cid = _mkcase(c, _tok(c, "admin", "admin"))
        _confirm_contract(cid)   # 청구서 생성 선행조건(계약 게이트) 충족
        inv = c.post(f"/cases/{cid}/invoices",
                     json={"service_type": "pre_audit", "amount": 0,
                           "line_items": [{"name": "심사료", "qty": 1, "unit_price": 6500000}]},
                     headers=_h(con)).json()
        r = c.get(f"/invoices/{inv['invoice_id']}/quotation.pdf", headers=_h(con))
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "application/pdf", r.headers
        assert r.content[:5] == b"%PDF-", r.content[:16]


def test_c_fatwa_return_to_auditor_transition_event_notify():
    """(c) 파트와 반려 → 오디터 단계(audit_closed) 전이 + 이벤트 + 오디터 알림."""
    with TestClient(app) as c:
        cid = _mkcase(c, _tok(c, "admin", "admin"))
        _set_status(cid, "fatwa_review")   # 반려 대상이 되도록 파트와 심의 단계로
        fatwa = _tok(c, "fatwa1", "pw")
        before_n = _count(cid, models.Notification, role="auditor")
        r = c.post(f"/cases/{cid}/fatwa/return-to-auditor",
                   json={"reason": "현장 사진 위치정보 누락"}, headers=_h(fatwa))
        assert r.status_code == 200, r.text
        assert r.json()["to"] == "audit_closed" and r.json()["from"] == "fatwa_review", r.json()
        assert _count(cid, models.WorkflowEvent, action="fatwa.returned_to_auditor") == 1
        assert _count(cid, models.Notification, role="auditor") == before_n + 1
        # 사유 누락 → 422
        rb = c.post(f"/cases/{cid}/fatwa/return-to-auditor",
                    json={"reason": "  "}, headers=_h(fatwa))
        assert rb.status_code == 422 and rb.json()["detail"]["code"] == "REASON_REQUIRED", rb.text


def test_d_return_to_auditor_forbidden_for_applicant():
    """(d) 비권한(applicant) 반려 → 403."""
    with TestClient(app) as c:
        cid = _mkcase(c, _tok(c, "admin", "admin"))
        _set_status(cid, "fatwa_review")
        ap = _tok(c, "applicant1", "pw")
        r = c.post(f"/cases/{cid}/fatwa/return-to-auditor",
                   json={"reason": "test"}, headers=_h(ap))
        assert r.status_code == 403, r.text


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
