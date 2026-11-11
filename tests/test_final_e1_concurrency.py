"""e1: 실제 HTTP·독립 DB 세션·스레드로 인증서/승인/종결의 원자성을 검사한다.

준비 단계만 DB fixture로 구성한다. 각 경쟁은 20회 실행하며 LLM 응답만
이벤트로 지연한다. 인증·권한·업무 가드·트랜잭션은 실제 구현을 사용한다.
"""
import base64
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import ai_local, main as m, models, state_machine as sm
from app.db import SessionLocal

REPEATS = range(20)
TERMINAL = ("withdrawn", "rejected", "cancelled", "revoked")


@pytest.fixture(scope="module")
def api():
    with TestClient(m.app) as client:
        headers = {}
        for name in ("admin", "operator1", "fatwa1", "applicant1"):
            r = client.post("/auth/login", json={"username": name,
                            "password": "admin" if name == "admin" else "pw"})
            assert r.status_code == 200, r.text
            headers[name] = {"Authorization": "Bearer " + r.json()["token"]}

        def request(method, path, body=None, role="admin"):
            return client.request(method, path, json=body, headers=headers[role])

        yield request


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("GLHAC_RATE_LIMIT_PER_MIN", "0")
    monkeypatch.setenv("GLHAC_NOTIFY_WORKER", "0")
    monkeypatch.setattr(m, "_AUTOPARSE", False)
    monkeypatch.setattr(m, "_DOC_VERIFY", False)


def ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def blocked(r, code=None):
    assert r.status_code == 409, r.text
    if code:
        assert r.json()["detail"]["code"] == code, r.text


def race(*calls):
    barrier = threading.Barrier(len(calls))

    def run(call):
        barrier.wait(timeout=10)
        return call()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(run, calls))


def case(ready=False):
    with SessionLocal() as db:
        c = models.CaseApplication(org_id="org_demo", company_name="E1 " + uuid4().hex,
                                   status="fatwa_approved" if ready else "onboarding",
                                   pathway="reguler", fatwa_status="approved" if ready else "pending")
        db.add(c)
        db.flush()
        if ready:
            p = models.Product(case_id=c.case_id, name="Reviewed sugar product")
            mat = models.Material(case_id=c.case_id, name="sugar", screen_status="halal", screen_result="CLEARED")
            db.add_all([p, mat])
            db.flush()
            db.add(models.ProductMaterial(case_id=c.case_id, product_id=p.product_id, material_id=mat.material_id))
            inv = models.Invoice(case_id=c.case_id, total=100, amount=100, currency="KRW", status="paid")
            db.add(inv)
            db.flush()
            db.add(models.Payment(case_id=c.case_id, invoice_id=inv.invoice_id, amount=100,
                                  currency="KRW", status="confirmed", method="manual"))
            sm.freeze_scope(db, c)
        db.commit()
        return c.case_id


def request_issue(api, cid):
    return ok(api("POST", f"/cases/{cid}/certificate/issue", {"reason": "발급 검토 완료"}, "operator1"))["approval_id"]


def issued(api):
    cid = case(ready=True)
    aid = request_issue(api, cid)
    cert = ok(api("POST", f"/approvals/{aid}/approve", role="fatwa1"))["result"]
    return cid, cert


def suspend(api, cid):
    ok(api("POST", f"/cases/{cid}/certificate/suspend", {"reason": "사후관리 보완 필요"}, "operator1"))


def close(api, cid, state="cancelled"):
    if state == "revoked":
        # revoked는 HTTP 철회 승인으로만 진입한다. 다른 준비 상태는 DB fixture다.
        with SessionLocal() as db:
            db.get(models.CaseApplication, cid).status = "certificate_issued"
            db.add(models.HalalCertificate(case_id=cid, certificate_no="E1-" + cid, status="active"))
            db.commit()
        aid = ok(api("POST", f"/cases/{cid}/certificate/revoke", {"reason": "종결 경쟁 검증"}, "operator1"))["approval_id"]
        ok(api("POST", f"/approvals/{aid}/approve"))
    else:
        ok(api("POST", f"/cases/{cid}/close", {"status": state, "reason": "종결 경쟁 검증"}, "operator1"))


def snapshot(cid):
    """읽기 로그를 제외한 도메인 행/감사 이벤트/알림의 불변성을 비교한다."""
    classes = (models.CaseApplication, models.HalalCertificate, models.ApprovalRequest,
               models.MaterialMeasurement, models.AuditPlan, models.DocumentAsset,
               models.Material, models.Product, models.AuditFinding, models.CorrectiveAction,
               models.Consultation, models.Invoice, models.Refund, models.PaymentMatchCandidate,
               models.Vehicle, models.IntegrationEvent, models.WorkflowEvent, models.Notification)
    out = {}
    with SessionLocal() as db:
        for cls in classes:
            out[cls.__tablename__] = sorted(
                [json.dumps({col.name: getattr(row, col.name) for col in cls.__table__.columns},
                            sort_keys=True, default=str)
                 for row in db.query(cls).filter_by(case_id=cid)])
        c = db.get(models.CaseApplication, cid)
        for f in db.query(models.Facility).filter(models.Facility.facility_id.in_(c.facility_ids or [])):
            out[f.facility_id] = {col.name: getattr(f, col.name) for col in f.__table__.columns}
        vehicles = [v.vehicle_id for v in db.query(models.Vehicle).filter_by(case_id=cid)]
        out["cleanings"] = sorted(json.dumps({col.name: getattr(row, col.name)
            for col in models.VehicleCleaning.__table__.columns}, sort_keys=True, default=str)
            for row in db.query(models.VehicleCleaning).filter(models.VehicleCleaning.vehicle_id.in_(vehicles)))
    return out


@pytest.mark.parametrize("iteration", REPEATS)
@pytest.mark.parametrize("second", ["reject", "approve", "cancel"])
def test_approval_has_one_winner(api, iteration, second):
    cid = case(ready=True)
    aid = request_issue(api, cid)
    responses = race(
        lambda: api("POST", f"/approvals/{aid}/approve", role="fatwa1"),
        lambda: api("POST", f"/approvals/{aid}/{second}", {"reason": "독립 검토 반대 의견"}))
    assert sorted(r.status_code for r in responses) == [200, 409], [r.text for r in responses]
    with SessionLocal() as db:
        ar = db.get(models.ApprovalRequest, aid)
        certs = db.query(models.HalalCertificate).filter_by(case_id=cid).all()
        assert len(certs) == (1 if ar.status == "approved" else 0)
        if certs:
            assert ar.result["certificate_no"] == certs[0].certificate_no
            assert db.get(models.CaseApplication, cid).status == "certificate_issued"
        else:
            assert db.get(models.CaseApplication, cid).status == "fatwa_approved"


@pytest.mark.parametrize("iteration", REPEATS)
def test_revoke_and_reactivate_cannot_resurrect(api, iteration):
    cid, cert = issued(api)
    suspend(api, cid)
    aid = ok(api("POST", f"/cases/{cid}/certificate/revoke", {"reason": "중대한 사후관리 위반"}, "operator1"))["approval_id"]
    responses = race(
        lambda: api("POST", f"/approvals/{aid}/approve"),
        lambda: api("POST", f"/cases/{cid}/certificate/reactivate", {"reason": "보완 검증 완료 의견"}, "operator1"))
    assert [r.status_code for r in responses] == [200, 409], [r.text for r in responses]
    with SessionLocal() as db:
        assert db.get(models.CaseApplication, cid).status == "revoked"
        assert db.query(models.HalalCertificate).filter_by(case_id=cid, status="active").count() == 0
        assert db.get(models.ApprovalRequest, aid).status == "approved"
    public = ok(api("GET", cert["verify_url"]))
    assert public["status"] == "withdrawn" and public["valid"] is False


@pytest.mark.parametrize("iteration", REPEATS)
def test_two_suspensions_have_one_winner(api, iteration):
    cid, cert = issued(api)
    responses = race(*(lambda: api("POST", f"/cases/{cid}/certificate/suspend",
                       {"reason": "중복 정지 동시 요청"}, "operator1") for _ in range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409], [r.text for r in responses]
    public = ok(api("GET", cert["verify_url"]))
    assert public["status"] == "suspended" and public["valid"] is False


@pytest.mark.parametrize("iteration", REPEATS)
@pytest.mark.parametrize("status", ["active", "suspended"])
def test_existing_certificate_blocks_new_request_and_pending_approval(api, iteration, status):
    cid = case(ready=True)
    aid = request_issue(api, cid)
    # 대기 승인 이후 인증서가 생긴 경우도 승인 단계에서 반드시 재조회한다.
    with SessionLocal() as db:
        db.get(models.CaseApplication, cid).status = "certificate_issued"
        db.add(models.HalalCertificate(case_id=cid, certificate_no="E1-existing-" + cid,
                                      status=status, qr_token=cid, expiry_date="2099-01-01"))
        db.commit()
    before = snapshot(cid)
    responses = race(
        lambda: api("POST", f"/cases/{cid}/certificate/issue", role="operator1"),
        lambda: api("POST", f"/approvals/{aid}/approve", role="fatwa1"))
    for r in responses:
        blocked(r, "CERT_ALREADY_EXISTS")
    assert snapshot(cid) == before


@pytest.mark.parametrize("state", TERMINAL)
def test_public_verification_rejects_inconsistent_terminal_certificate(api, state):
    cid = case()
    with SessionLocal() as db:
        db.get(models.CaseApplication, cid).status = state
        db.add(models.HalalCertificate(case_id=cid, certificate_no="legacy-" + cid,
                                      status="active", qr_token=cid, expiry_date="2099-01-01"))
        db.commit()
    assert ok(api("GET", f"/verify/{cid}"))["valid"] is False


@pytest.mark.parametrize("problem,code", [
    ("major", "UNRESOLVED_MAJOR_NC"), ("car", "UNRESOLVED_CAR"),
    ("rejected_car", "UNRESOLVED_CAR"), ("unknown_car", "UNRESOLVED_CAR"),
    ("document", "DOCUMENTS_NOT_APPROVED"), ("measurement", None),
    ("same_reason", "SUSPENSION_REVIEW_REQUIRED"), ("no_history", "SUSPENSION_REVIEW_REQUIRED")])
def test_reactivation_requires_resolved_suspension(api, problem, code):
    cid, cert = issued(api)
    suspend(api, cid)
    reason = "보완 검증 완료 의견"
    with SessionLocal() as db:
        if problem == "major":
            db.add(models.AuditFinding(case_id=cid, severity="major", status="open", finding="Unresolved"))
        elif problem.endswith("car"):
            row = models.CorrectiveAction(case_id=cid, finding_id="eval:review",
                                         status="rejected" if problem == "rejected_car" else "submitted")
            db.add(row)
            db.flush()
            if problem == "unknown_car":
                row.status = None
        elif problem == "document":
            db.add(models.DocumentAsset(case_id=cid, filename="unresolved.txt", review_status="rework"))
        elif problem == "measurement":
            db.add(models.MaterialMeasurement(case_id=cid, param_key="ethanol_pct", value=2, verdict="fail"))
        elif problem == "same_reason":
            reason = "사후관리 보완 필요"
        elif problem == "no_history":
            db.query(models.WorkflowEvent).filter_by(case_id=cid, action="certificate.suspend").delete()
        db.commit()
    before = snapshot(cid)
    blocked(api("POST", f"/cases/{cid}/certificate/reactivate", {"reason": reason}, "operator1"), code)
    assert snapshot(cid) == before
    assert ok(api("GET", cert["verify_url"]))["valid"] is False


def test_reviewed_car_and_resolution_can_reactivate(api):
    cid, cert = issued(api)
    suspend(api, cid)
    with SessionLocal() as db:
        finding = models.AuditFinding(case_id=cid, severity="major", status="closed", finding="Resolved")
        db.add(finding)
        db.flush()
        db.add(models.CorrectiveAction(case_id=cid, finding_id=finding.finding_id,
                                      status="accepted", reviewer="independent-reviewer"))
        db.commit()
    ok(api("POST", f"/cases/{cid}/certificate/reactivate", {"reason": "시정조치 검토 및 정지 사유 해소 확인"}, "operator1"))
    assert ok(api("GET", cert["verify_url"]))["valid"] is True
    with SessionLocal() as db:
        ev = db.query(models.WorkflowEvent).filter_by(case_id=cid, action="certificate.reactivate").one()
        suspension = db.query(models.WorkflowEvent).filter_by(case_id=cid, action="certificate.suspend").one()
        assert ev.payload["resolved_suspension"] == suspension.event_id
        assert ev.payload["resolution_reviewed_by"] == ev.actor_id


def test_failed_issuance_rolls_back_decision_and_certificate(api, monkeypatch):
    cid = case(ready=True)
    aid = request_issue(api, cid)
    original = m._notify

    def fail_after_certificate_insert(db, c, event, *args, **kwargs):
        if c.case_id == cid and event == "certificate_issued":
            raise m.HTTPException(409, {"code": "E1_INJECTED_FAILURE"})
        return original(db, c, event, *args, **kwargs)

    before = snapshot(cid)
    with monkeypatch.context() as patch:
        patch.setattr(m, "_notify", fail_after_certificate_insert)
        blocked(api("POST", f"/approvals/{aid}/approve", role="fatwa1"), "E1_INJECTED_FAILURE")
    assert snapshot(cid) == before
    ok(api("POST", f"/approvals/{aid}/approve", role="fatwa1"))


INDIRECT = ("measurement", "plan_patch", "plan_propose", "consultation_patch", "consultation_respond",
            "facility_patch", "facility_delete", "vehicle_patch", "vehicle_delete", "cleaning_add",
            "cleaning_sign", "cleaning_delete", "refund", "match", "sync", "consultation_body",
            "vehicle_body", "integration_body")


def indirect(cid, operation):
    with SessionLocal() as db:
        if operation == "measurement":
            row = models.MaterialMeasurement(case_id=cid, param_key="ethanol_pct", value=2, verdict="fail")
            db.add(row); db.flush()
            route = ("DELETE", f"/measurements/{row.measurement_id}", {})
        elif operation.startswith("plan_"):
            row = models.AuditPlan(case_id=cid, note="Original", status="scheduled")
            db.add(row); db.flush()
            route = (("PATCH", f"/audit-plans/{row.id}", {"note": "Changed"}) if operation == "plan_patch"
                     else ("POST", f"/audit-plans/{row.id}/propose-dates", {"dates": ["2027-01-01"]}))
        elif operation in ("consultation_patch", "consultation_respond"):
            row = models.Consultation(case_id=cid, org_id="org_demo", message="Original", status="open")
            db.add(row); db.flush()
            route = (("PATCH", f"/consultations/{row.id}", {"status": "closed"}) if operation.endswith("patch")
                     else ("POST", f"/consultations/{row.id}/respond", {"response": "Changed"}))
        elif operation.startswith("facility_"):
            row = models.Facility(org_id="org_demo", name="Original", address="Original")
            db.add(row); db.flush()
            db.get(models.CaseApplication, cid).facility_ids = [row.facility_id]
            route = ("DELETE" if operation.endswith("delete") else "PATCH",
                     f"/facilities/{row.facility_id}", {"name": "Changed"})
        elif operation in ("vehicle_patch", "vehicle_delete", "cleaning_add", "cleaning_sign", "cleaning_delete"):
            v = models.Vehicle(case_id=cid, org_id="org_demo", plate_no="E1-" + cid)
            db.add(v); db.flush()
            if operation.startswith("vehicle_"):
                route = ("PATCH" if operation.endswith("patch") else "DELETE",
                         f"/vehicles/{v.vehicle_id}", {"plate_no": "Changed"})
            elif operation == "cleaning_add":
                route = ("POST", f"/vehicles/{v.vehicle_id}/cleanings", {"method": "normal", "cleaned_at": "2026-11-12"})
            else:
                row = models.VehicleCleaning(vehicle_id=v.vehicle_id, org_id="org_demo", method="normal")
                db.add(row); db.flush()
                route = (("POST", f"/cleanings/{row.cleaning_id}/sign", {}) if operation.endswith("sign")
                         else ("DELETE", f"/cleanings/{row.cleaning_id}", {}))
        elif operation in ("refund", "match"):
            inv = models.Invoice(case_id=cid, total=100, status="paid")
            db.add(inv); db.flush()
            if operation == "refund":
                row = models.Refund(case_id=cid, invoice_id=inv.invoice_id, amount=100, status="requested")
                db.add(row); db.flush()
                route = ("POST", f"/refunds/{row.id}/decide", {"decision": "rejected", "note": "Changed"})
            else:
                row = models.PaymentMatchCandidate(case_id=cid, invoice_id=inv.invoice_id, decision_status="pending")
                db.add(row); db.flush()
                route = ("POST", f"/admin/match/{row.id}/decide", {"decision": "held", "note": "Changed"})
        elif operation == "sync":
            row = models.IntegrationEvent(case_id=cid, provider="logistics_audit", status="pending", payload={})
            db.add(row); db.flush()
            route = ("POST", f"/admin/logistics-sync/{row.id}/resend", {})
        elif operation == "consultation_body":
            route = ("POST", "/consultations", {"case_id": cid, "subject": "Changed", "message": "Changed"})
        elif operation == "vehicle_body":
            route = ("POST", "/orgs/org_demo/vehicles", {"case_id": cid, "plate_no": "Changed"})
        else:
            route = ("POST", "/integration/sihalal/event", {"case_id": cid, "event_type": "external_status_synced",
                                                            "idempotency_key": uuid4().hex, "payload": {}})
        db.commit()
        return route


@pytest.mark.parametrize("state", TERMINAL)
@pytest.mark.parametrize("operation", INDIRECT)
def test_indirect_writes_cannot_modify_terminal_case(api, state, operation):
    cid = case()
    route = indirect(cid, operation)
    close(api, cid, state)
    before = snapshot(cid)
    blocked(api(*route), "CASE_CLOSED")
    assert snapshot(cid) == before


@pytest.mark.parametrize("operation", ("measurement", "plan_patch", "plan_propose"))
def test_open_indirect_write_controls(api, operation):
    cid = case()
    route = indirect(cid, operation)
    before = snapshot(cid)
    ok(api(*route))
    assert snapshot(cid) != before


@pytest.mark.parametrize("state", TERMINAL)
def test_expiry_scan_skips_terminal_certificate(api, state):
    cid = case()
    with SessionLocal() as db:
        db.get(models.CaseApplication, cid).status = state
        db.add(models.HalalCertificate(case_id=cid, certificate_no="E1-expiring-" + cid,
                                      status="active", expiry_date=str(date.today() + timedelta(days=20))))
        db.commit()
    before = snapshot(cid)
    ok(api("POST", "/admin/scan-expiry"))
    assert snapshot(cid) == before


@pytest.mark.parametrize("state", TERMINAL)
def test_deposit_matching_does_not_add_candidates_to_closed_case(api, state):
    cid = case()
    with SessionLocal() as db:
        db.add(models.Invoice(case_id=cid, status="need_verification", total=100,
                              currency="KRW", payment_ref="E1-deposit-" + cid))
        db.commit()
    close(api, cid, state)
    before = snapshot(cid)
    result = ok(api("POST", "/admin/deposits", {"bank_name": "E1", "amount": 33,
        "currency": "KRW", "depositor_name": "Unrelated", "ref_memo": "E1-deposit-" + cid,
        "transaction_id": uuid4().hex}, "operator1"))
    assert result["candidates"] == 0
    assert snapshot(cid) == before


@pytest.mark.parametrize("iteration", REPEATS)
@pytest.mark.parametrize("operation", ("measurement", "plan_patch", "plan_propose"))
def test_close_after_request_guard_is_rechecked_before_save(api, monkeypatch, iteration, operation):
    cid = case()
    route = indirect(cid, operation)
    # 엔드포인트의 첫 자원 조회에서 대기한다. 종결 가드가 이미 통과한 요청이다.
    loaded, resume = threading.Event(), threading.Event()
    get = SessionLocal.class_.get
    target = models.MaterialMeasurement if operation == "measurement" else models.AuditPlan
    target_id = route[1].split("/")[2]
    hits = 0

    def delayed_get(db, cls, ident, *args, **kwargs):
        nonlocal hits
        result = get(db, cls, ident, *args, **kwargs)
        if cls is target and ident == target_id:
            hits += 1
            if hits == 2:  # 전역 가드의 부모 조회 다음, 실제 핸들러의 조회
                db.rollback()  # 중간 커밋/롤백으로 최초 트랜잭션이 끝난 경우도 검증
                loaded.set()
                assert resume.wait(10)
        return result

    monkeypatch.setattr(SessionLocal.class_, "get", delayed_get)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(api, *route)
        try:
            assert loaded.wait(10)
            close(api, cid)
            before = snapshot(cid)
        finally:
            resume.set()
        blocked(future.result(timeout=10), "CASE_CLOSED")
    assert snapshot(cid) == before


@pytest.mark.parametrize("iteration", REPEATS)
@pytest.mark.parametrize("state", ("withdrawn", "rejected", "cancelled"))
@pytest.mark.parametrize("target", ("profile_city", "facility_address"))
def test_inflight_verification_preserves_closed_records(api, monkeypatch, iteration, state, target):
    _verification_race(api, monkeypatch, target, state)


@pytest.mark.parametrize("target", ("profile_city", "facility_address", "case_address"))
def test_verification_still_reverts_open_case(api, monkeypatch, target):
    _verification_race(api, monkeypatch, target, None)


def _verification_race(api, monkeypatch, target, state):
    cid = case()
    field, scope = ("city", "profile") if target == "profile_city" else ("address", "facility" if target == "facility_address" else "case")
    with SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        change = {"field": field, "scope": scope, "before": "Original", "value": "AI filled",
                  "status": "applied", "present": True}
        fid = None
        if scope == "facility":
            f = models.Facility(org_id="org_demo", name="Factory", address="AI filled")
            db.add(f); db.flush()
            fid = f.facility_id
            c.facility_ids = [fid]
            change["facility_id"] = fid
        elif scope == "profile":
            c.profile_ext = {"city": "AI filled"}
        else:
            c.address = "AI filled"
        d = models.DocumentAsset(case_id=cid, filename="registration.txt",
            doc_type="factory_registration" if scope == "facility" else "nib_business_license",
            confidence=0.9, text_full="AI filled", content_b64=base64.b64encode(b"AI filled").decode(),
            fields={field: "AI filled", "_autofill": {"changes": {field: change}, "conflicts": []}})
        db.add(d); db.commit()
        did = d.document_id
    started, release = threading.Event(), threading.Event()

    def llm(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return {"verdict": "fail", "doc_type_ok": True,
                "field_issues": [{"field": field, "issue": "Controlled verification failure"}], "summary": "E1 verification"}

    monkeypatch.setattr(ai_local, "llm_json", llm)
    monkeypatch.setattr(m, "_DOC_VERIFY", True)
    m._verify_start()
    failed = m._verify_state["failed"]
    try:
        assert ok(api("POST", f"/cases/{cid}/documents/verify", role="operator1"))["queued"] == 1
        assert started.wait(10)
        if state:
            close(api, cid, state)
        before = snapshot(cid)
    finally:
        release.set()
    deadline = time.monotonic() + 10
    while m._verify_q.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not m._verify_q.unfinished_tasks
    assert m._verify_state["failed"] == failed
    if state:
        assert snapshot(cid) == before
    else:
        with SessionLocal() as db:
            c = db.get(models.CaseApplication, cid)
            value = (db.get(models.Facility, fid).address if fid else
                     (c.profile_ext or {}).get("city") if scope == "profile" else c.address)
            assert value == "Original"
            assert db.get(models.DocumentAsset, did).fields["_verify"]["verdict"] == "fail"
