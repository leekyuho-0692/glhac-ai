"""P0: 결제 confirmed → 모의심사 진입 게이트 검증.
결제확정 시 _on_invoice_paid가 케이스를 document_pre_audit_requested로 전이(가드 통과 시)하고,
오디터 배정 태스크 이벤트 + 알림을 남기는지. TestClient 기반(서버 불필요).
실행: <venv>/bin/python -m pytest tests/test_payment_gate.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_v3_test.db")
os.environ.setdefault("GLHAC_DEV", "1")   # 데모 계정 시드(테스트 전용)

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402


def _tok(c, u, p):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(tok):
    return {"Authorization": "Bearer " + tok}


def _set_status(case_id, status):
    """상태머신을 완주하지 않고 시작상태를 직접 세팅(테스트 픽스처)."""
    from app import models
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        c = db.get(models.CaseApplication, case_id)
        c.status = status
        db.commit()
    finally:
        db.close()


def _events(case_id, action):
    from app import models
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        return db.query(models.WorkflowEvent).filter_by(case_id=case_id, action=action).all()
    finally:
        db.close()


def _notifs(case_id, event_type):
    from app import models
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        return db.query(models.Notification).filter_by(case_id=case_id, event_type=event_type).all()
    finally:
        db.close()


def _case_status(case_id):
    from app import models
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        return db.get(models.CaseApplication, case_id).status
    finally:
        db.close()


def _confirm_contract(case_id):
    """청구서 생성 게이트(CONTRACT_NOT_CONFIRMED) 충족용 — confirmed 계약 삽입(픽스처).
    add_invoice가 계약 최종확인 후에만 청구서를 허용하므로, 인보이스 POST 전에 필요."""
    from app import models
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        db.add(models.Contract(case_id=case_id, status="confirmed"))
        db.commit()
    finally:
        db.close()


def _mk_case_with_invoice(c, tok, status="consultant_review"):
    cid = c.post("/cases", json={"company_name": "PG게이트테스트", "is_msme": True},
                 headers=_h(tok)).json()["case_id"]
    _confirm_contract(cid)   # 청구서 생성 선행조건(계약 게이트) 충족
    _set_status(cid, status)
    iid = c.post("/cases/%s/invoices" % cid, json={"service_type": "pre_audit", "amount": 1000000},
                 headers=_h(tok)).json()["invoice_id"]
    return cid, iid


def test_pay_gate_transitions_to_mock_audit():
    """consultant_review 케이스 인보이스 결제확정 → document_pre_audit_requested 전이 + 이벤트/알림."""
    with TestClient(app) as c:
        tok = _tok(c, "consultant1", "pw")
        cid, iid = _mk_case_with_invoice(c, tok, status="consultant_review")
        r = c.patch("/invoices/%s/pay" % iid, headers=_h(tok))
        assert r.status_code == 200, r.text
        assert _case_status(cid) == "document_pre_audit_requested", _case_status(cid)
        # 게이트 전이 이벤트 + 오디터 배정 태스크 + 알림
        assert len(_events(cid, "payment.gate")) == 1
        assert len(_events(cid, "mock_audit.task_created")) == 1
        notifs = _notifs(cid, "mock_audit.assigned")
        assert len(notifs) == 1 and notifs[0].role == "auditor", notifs


def test_pay_gate_noop_when_transition_invalid():
    """전이 불가 상태(onboarding)에서는 게이트가 no-op — 상태 유지·이벤트 없음."""
    with TestClient(app) as c:
        tok = _tok(c, "consultant1", "pw")
        cid, iid = _mk_case_with_invoice(c, tok, status="onboarding")
        r = c.patch("/invoices/%s/pay" % iid, headers=_h(tok))
        assert r.status_code == 200, r.text
        assert _case_status(cid) == "onboarding"
        assert _events(cid, "payment.gate") == []
        assert _notifs(cid, "mock_audit.assigned") == []


def test_pay_gate_idempotent_when_already_in_mock_audit():
    """이미 모의심사 단계면 재전이·중복알림 금지(멱등)."""
    with TestClient(app) as c:
        tok = _tok(c, "consultant1", "pw")
        cid, iid = _mk_case_with_invoice(c, tok, status="document_pre_audit_in_review")
        r = c.patch("/invoices/%s/pay" % iid, headers=_h(tok))
        assert r.status_code == 200, r.text
        assert _case_status(cid) == "document_pre_audit_in_review"
        assert _events(cid, "payment.gate") == []
        assert _notifs(cid, "mock_audit.assigned") == []
