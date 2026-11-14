"""H4, M6/O4, O5: 2인 확인·외부 알림 상태·정식신청 동시 제출 회귀."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import main, models, notify, state_machine as sm
from app.db import SessionLocal


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as api:
        yield api


@pytest.fixture(scope="module")
def tokens(client):
    headers = {}
    for username in ("admin", "operator1", "fatwa1", "applicant1"):
        response = client.post("/auth/login", json={
            "username": username, "password": "admin" if username == "admin" else "pw"})
        assert response.status_code == 200, response.text
        headers[username] = {"Authorization": "Bearer " + response.json()["token"]}
    return headers


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("GLHAC_FORMAL_FLOW", "0")
    monkeypatch.delenv("GLHAC_TWO_PERSON", raising=False)


def case(**fields):
    with SessionLocal() as db:
        row = models.CaseApplication(org_id="org_demo", company_name="Residual regression", **fields)
        db.add(row)
        db.commit()
        return row.case_id


def assign_co_auditor(cid):
    with SessionLocal() as db:
        auditor = db.query(models.User).filter_by(username="auditor2").one()
        db.add(models.CaseAuditor(case_id=cid, user_id=auditor.user_id, role="co"))
        db.commit()


@pytest.mark.parametrize("username", ["fatwa1", "operator1", "admin"])
@pytest.mark.parametrize("with_co", [False, True])
@pytest.mark.parametrize("strict", [False, True])
def test_mock_decision_non_auditors_follow_strict_gate(client, tokens, monkeypatch,
                                                    username, with_co, strict):
    if strict:
        monkeypatch.setenv("GLHAC_TWO_PERSON", "strict")
    cid = case(status="document_pre_audit_in_review")
    if with_co:
        assign_co_auditor(cid)
    response = client.post(f"/cases/{cid}/mock-audit/decision", headers=tokens[username],
                           json={"result": "pass"})
    if strict and not with_co:
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "CO_AUDITOR_REQUIRED"
    else:
        assert response.status_code == 200, response.text
        if strict:
            assert response.json()["pending"] is True
        else:
            assert not response.json().get("pending")
            assert response.json()["result"] == "pass"
    with SessionLocal() as db:
        assert db.query(models.ApprovalRequest).filter_by(
            case_id=cid, action_type="mock_audit.decision", status="pending").count() == int(strict and with_co)
        assert db.query(models.WorkflowEvent).filter_by(
            case_id=cid, action="mock_audit.decision").count() == int(not strict)
        assert db.query(models.GeneratedDocument).filter_by(
            case_id=cid, doc_type="mock_audit_notice").count() == int(not strict)
        if strict:
            assert db.get(models.CaseApplication, cid).status == "document_pre_audit_in_review"


@pytest.mark.parametrize("with_co", [False, True])
def test_send_fatwa_is_strict_for_liaison_without_env(client, with_co):
    cid = case(status="onsite_audit_completed")
    if with_co:
        assign_co_auditor(cid)
    with SessionLocal() as db:
        liaison = db.query(models.User).filter_by(username="fatwa1").one()
        user = {"uid": liaison.user_id, "role": liaison.role, "username": liaison.username}
        c = db.get(models.CaseApplication, cid)
        if with_co:
            result = main._two_person_gate(db, c, user, "audit_report.send_fatwa", {})
            assert result and result["pending"] is True
        else:
            with pytest.raises(HTTPException) as error:
                main._two_person_gate(db, c, user, "audit_report.send_fatwa", {})
            assert error.value.status_code == 409
            assert error.value.detail["code"] == "CO_AUDITOR_REQUIRED"


@pytest.mark.parametrize("channels,reason,expected", [
    (["inapp", "email"], "no_credentials", "partial"),
    (["inapp", "email"], "no_contact", "partial"),
    (["inapp", "email"], "consent_skipped", "sent"),
    (["inapp"], None, "sent"),
])
def test_notification_skips_preserve_status_and_stop_retrying(client, monkeypatch, channels, reason, expected):
    monkeypatch.delenv("GLHAC_SMTP_HOST", raising=False)
    if reason == "no_contact":
        monkeypatch.setenv("GLHAC_SMTP_HOST", "smtp.example.invalid")
        monkeypatch.setenv("GLHAC_SMTP_FROM", "sender@example.invalid")
    cid = case(email=None if reason == "no_contact" else "residual@example.invalid",
               notify_consent=reason != "consent_skipped")
    calls = []
    dispatch = notify.dispatch

    def track_dispatch(notification, contacts=None, channels=None):
        calls.extend(channels)
        return dispatch(notification, contacts=contacts, channels=channels)

    monkeypatch.setattr(notify, "dispatch", track_dispatch)
    with SessionLocal() as db:
        # 다른 회귀 사례에서 만든 알림을 제외하고 이 큐만 실제 프로바이더로 처리한다.
        db.query(models.Notification).delete(synchronize_session=False)
        n = models.Notification(case_id=cid, org_id="org_demo", event_type="residual",
                                channels=channels, status="unsent")
        db.add(n)
        db.commit()
        stats = main.drain_notifications(db)
        db.refresh(n)
        assert stats["processed"] == 1 and stats[expected] == 1
        assert stats["retried"] == 0
        assert n.status == expected
        assert n.channel_status["inapp"]["status"] == "sent"
        assert n.retry_pending is False and n.claimed_at is None
        if "email" in channels:
            assert n.channel_status["email"] == {
                "status": "skipped", "reason": reason, "attempts": 0, "retryable": False}
            assert n.last_error == "email:" + reason
        before = (n.status, n.attempts, dict(n.channel_status), n.last_error)
        for _ in range(3):
            assert main.drain_notifications(db)["processed"] == 0
            db.refresh(n)
            assert (n.status, n.attempts, n.channel_status, n.last_error) == before
            assert n.retry_pending is False
    assert calls == (["inapp"] if reason == "consent_skipped" else channels)


def test_threaded_formal_submissions_create_one_document_and_event(client, tokens, monkeypatch):
    cid = case(status="consultant_review")
    with SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        sm.record_event(db, c, c.status, c.status, "eligibility.verdict", payload={"verdict": "eligible"})
        db.commit()
    body = {"process_summary": "원료 입고→혼합→포장", "halal_management": "분리 보관·내부심사",
            "non_halal_handling": False, "preferred_audit_period": "2026-11", "sales_channels": "국내",
            "pledge": True, "signer_name": "홍길동", "signature": "data:image/png;base64,iVBORw0KGgo="}
    barrier = threading.Barrier(2)
    completed = threading.Event()
    lock_case, next_version = main._lock_case, main._next_version

    def synchronize(db):
        # 잠금 또는 버전 조회 직전에 한 요청을 멈추고 다른 요청을 완료시킨다.
        # 상태를 잠금 전에 확인하면 대기한 요청도 새 문서 버전으로 중복 제출한다.
        if not db.info.get("residual_formal_synchronized"):
            db.info["residual_formal_synchronized"] = True
            if barrier.wait(timeout=10) == 0:
                assert completed.wait(timeout=10), "다른 제출 요청이 완료되지 않았습니다"

    def synchronized_lock(db, case_id):
        if case_id == cid:
            synchronize(db)
        return lock_case(db, case_id)

    def synchronized_version(db, case_id, doc_type):
        if case_id == cid and doc_type == "formal_application":
            synchronize(db)
        return next_version(db, case_id, doc_type)

    def submit(_):
        try:
            return client.post(f"/cases/{cid}/formal-application", headers=tokens["applicant1"], json=body)
        finally:
            completed.set()

    monkeypatch.setattr(main, "_lock_case", synchronized_lock)
    monkeypatch.setattr(main, "_next_version", synchronized_version)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, range(2)))
    with SessionLocal() as db:
        documents = db.query(models.GeneratedDocument).filter_by(case_id=cid, doc_type="formal_application").all()
        events = db.query(models.WorkflowEvent).filter_by(case_id=cid, action="formal_application.submitted").all()
        assert len(documents) == len(events) == 1
        assert json.loads(documents[0].content)["doc"] == "D-06"
        assert documents[0].gen_doc_id == events[0].payload["gen_doc_id"]
    assert sorted(r.status_code for r in responses) == [200, 409], [r.text for r in responses]
    accepted = next(r.json() for r in responses if r.status_code == 200)
    rejected = next(r.json() for r in responses if r.status_code == 409)
    assert accepted["status"] == "submitted"
    assert rejected["detail"]["code"] == "FORMAL_ALREADY_SUBMITTED"
    assert documents[0].gen_doc_id == accepted["gen_doc_id"]
