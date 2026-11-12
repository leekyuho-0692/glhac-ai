"""최종 e2: 시설·위원 실명·송부 근거·수납·자가선언·본문 참조 회귀.

단계 준비만 DB fixture로 구성한다. 공격 및 정상 대조 요청은 TestClient로 실행한다.
"""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import main as m, models as M, state_machine as sm
from app.db import SessionLocal


SIG = "data:image/png;base64,iVBORw0KGgo="


def seed_reviewed_issue_prerequisites(db, c):
    """기존 발급 fixture용 정상 심사 근거. 가드 자체를 모킹하거나 우회하지 않는다.

    각 기존 시험의 상태·범위·결제 실패 조건은 호출자가 유지한다. 송부 자체의 API
    검증은 이 파일의 report_ready/dispatch 대조군에서 별도로 수행한다.
    """
    db.flush()
    people = []
    for name in ("auditor1", "auditor2"):
        account = db.query(M.User).filter_by(username=name).first()
        if account is None:
            account = M.User(username=name, password_hash=m.auth.hash_pw("fixture-only"),
                             role="auditor", org_id=c.org_id)
            db.add(account)
            db.flush()
        people.append(account)
    auditor, checker = people
    for material in db.query(M.Material).filter_by(case_id=c.case_id):
        if material.screen_result is None:
            m.screening.apply_screen(material)
    for element, row in m._ensure_hpas(db, c.case_id).items():
        row.status = "ok"
    if not sm.has_active_penyelia(db, c.org_id):
        db.add(M.PenyeliaHalal(org_id=c.org_id, name="Reviewed fixture supervisor", status="active"))
    if c.pathway == "self_declare":
        seed_selfdeclare_prerequisites(db, c)
        sm.record_event(db, c, c.status, c.status, "committee.approve", "fatwa_liaison", None,
                        {"review_subject": sm.review_subject(db, c)})
        return
    g = M.GeneratedDocument(case_id=c.case_id, org_id=c.org_id, doc_type="audit_report",
                            version=1, status="approved", content="Reviewed fixture scope")
    db.add(g)
    db.flush()
    subject = sm.review_subject(db, c)
    sm.record_event(db, c, c.status, c.status, "audit_report.generated", "auditor", auditor.user_id,
                    {**m._report_version(g), "review_subject": subject})
    sm.record_event(db, c, c.status, c.status, "audit_report.sign", "auditor", auditor.user_id,
                    {"name": auditor.username, **m._report_version(g), "review_subject": subject})
    if m.nav8.formal_flow_on():
        applicant = db.query(M.User).filter_by(username="applicant1").one()
        sm.record_event(db, c, c.status, c.status, "audit_report.client_sign", "applicant", applicant.user_id,
                        {"name": applicant.username, **m._report_version(g), "review_subject": subject})
    _, context = m._fatwa_gate_check(db, c)
    ar = M.ApprovalRequest(case_id=c.case_id, org_id=c.org_id, action_type="audit_report.send_fatwa",
                           requested_by=auditor.user_id, decided_by=checker.user_id, status="approved",
                           payload={"review_basis": context["review_basis"]})
    db.add(ar)
    db.flush()
    sm.record_event(db, c, c.status, "fatwa_review", "audit_report.send_fatwa", "auditor", auditor.user_id,
                    {"approval_id": ar.id, "review_basis": context["review_basis"], "gate": "passed"})
    sm.record_event(db, c, c.status, c.status, "fatwa.provisional", "fatwa_liaison", None,
                    {"decision": "approved", "review_basis": context["review_basis"]})
    if c.fatwa_status == "approved":
        sm.record_event(db, c, c.status, c.status, "fatwa.final_approve", "operator", None,
                        {"review_basis": context["review_basis"]})


def seed_selfdeclare_prerequisites(db, c):
    """판정서/발급 fixture도 동반자·식별자·감독자 전제는 갖춘다."""
    db.add(M.ExternalIdentity(case_id=c.case_id, org_id=c.org_id,
                              external_email="fixture@example.invalid",
                              verification_status="verified", identifier_match=True))
    pendamping = db.query(M.User).filter_by(role="pendamping_pph").first()
    if not pendamping:
        pendamping = M.User(username="fixture_pendamping", password_hash=m.auth.hash_pw("fixture-only"),
                            role="pendamping_pph", org_id=c.org_id)
        db.add(pendamping)
        db.flush()
    db.add(M.PendampingAssignment(case_id=c.case_id, pendamping_id=pendamping.user_id, decision="verified"))
    if not sm.has_active_penyelia(db, c.org_id):
        db.add(M.PenyeliaHalal(org_id=c.org_id, name="Reviewed fixture supervisor", status="active"))
    db.flush()


def seed_paid_receipt(db, inv):
    """paid 상태 fixture에 대응하는 확정 수납 원장을 함께 준비한다."""
    inv.currency = inv.currency or "KRW"
    inv.total = inv.total if inv.total is not None else (inv.amount or 0) + (inv.ppn or 0)
    db.flush()
    db.add(M.Payment(case_id=inv.case_id, invoice_id=inv.invoice_id, amount=inv.total,
                     currency=inv.currency, status="confirmed", method="fixture"))


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("GLHAC_RATE_LIMIT_PER_MIN", "0")
    monkeypatch.setenv("GLHAC_FORMAL_FLOW", "0")
    monkeypatch.setenv("GLHAC_TWO_PERSON", "0")
    monkeypatch.setattr(m, "_AUTOPARSE", False)
    monkeypatch.setattr(m, "_DOC_VERIFY", False)
    with TestClient(m.app) as client:
        headers = {}
        for name in ("admin", "operator1", "fatwa1", "auditor1", "auditor2", "applicant1"):
            r = client.post("/auth/login", json={"username": name,
                                                 "password": "admin" if name == "admin" else "pw"})
            assert r.status_code == 200, r.text
            headers[name] = {"Authorization": "Bearer " + r.json()["token"]}

        def request(method, path, body=None, user="admin"):
            return client.request(method, path, json=body, headers=headers[user])

        request.headers = headers
        request.client = client
        yield request


def ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def blocked(r, code=None, status=409):
    assert r.status_code == status, r.text
    if code:
        assert r.json()["detail"]["code"] == code, r.text


def new_case(api, *, logistics=False):
    body = {"org_id": "org_demo", "company_name": "E2 " + uuid4().hex, "is_msme": True}
    if logistics:
        body.update(scheme="logistics", logistics_scope=["penyimpanan"])
    cid = ok(api("POST", "/cases", body))["case_id"]
    if not logistics:
        pid = ok(api("POST", f"/cases/{cid}/products", {"name": "Reviewed sugar"}))["product_id"]
        mid = ok(api("POST", f"/cases/{cid}/materials", {"name": "sugar"}))["material_id"]
        ok(api("POST", f"/cases/{cid}/matrix/link", {"product_id": pid, "material_id": mid, "linked": True}))
    return cid


def freeze(cid, *, pathway="reguler"):
    with SessionLocal() as db:
        c = db.get(M.CaseApplication, cid)
        c.status = "committee_verification" if pathway == "self_declare" else "fatwa_approved"
        c.pathway = pathway
        c.risk_category = "low"
        db.flush()
        c.fatwa_status = "approved"
        sm.freeze_scope(db, c)
        db.commit()


def paid(api, cid, amount=100):
    with SessionLocal() as db:
        if not db.query(M.Contract).filter_by(case_id=cid).first():
            db.add(M.Contract(case_id=cid, status="confirmed", currency="KRW"))
            db.commit()
    inv = ok(api("POST", f"/cases/{cid}/invoices", {"amount": amount, "service_type": "onsite"}))
    iid = inv["invoice_id"]
    ok(api("PATCH", f"/invoices/{iid}/status", {"status": "paid", "reason": "E2 confirmed receipt"}, "operator1"))
    return iid


def request_issue(api, cid, checker=False):
    if not checker:
        return api("POST", f"/cases/{cid}/certificate/issue", user="operator1")
    with SessionLocal() as db:
        maker = db.query(M.User).filter_by(username="operator1").one()
        ar = M.ApprovalRequest(case_id=cid, action_type="certificate.issue", requested_by=maker.user_id,
                               requester_role="operator", org_id="org_demo", status="pending", payload={})
        db.add(ar)
        db.commit()
        aid = ar.id
    return api("POST", f"/approvals/{aid}/approve", {})


@pytest.mark.parametrize("operation", ["patch", "case_profile", "add", "select", "unlink", "delete", "classify"])
def test_frozen_facility_routes(api, operation):
    cid = new_case(api, logistics=True)
    fid = ok(api("POST", f"/cases/{cid}/factories", {"name": "Reviewed warehouse"}))["facility_id"]
    freeze(cid)
    routes = {
        "patch": ("PATCH", f"/facilities/{fid}", {"name": "Unreviewed", "address": "Elsewhere"}),
        "case_profile": ("PATCH", f"/cases/{cid}/profile", {"factory_address": "Elsewhere"}),
        "add": ("POST", f"/cases/{cid}/factories", {"name": "Unreviewed"}),
        "select": ("PATCH", f"/cases/{cid}/facilities-select", {"facility_ids": []}),
        "unlink": ("DELETE", f"/cases/{cid}/factories/{fid}", None),
        "delete": ("DELETE", f"/facilities/{fid}", None),
        "classify": ("POST", f"/cases/{cid}/factories/classify", None),
    }
    blocked(api(*routes[operation]), "SCOPE_FROZEN")
    with SessionLocal() as db:
        assert db.get(M.Facility, fid).name == "Reviewed warehouse"
        assert fid in db.get(M.CaseApplication, cid).facility_ids


@pytest.mark.parametrize("status", sorted(sm.TERMINAL_STATES))
def test_shared_terminal_facility_cannot_change(api, status):
    cid = new_case(api, logistics=True)
    fid = ok(api("POST", f"/cases/{cid}/factories", {"name": "Closed warehouse"}))["facility_id"]
    other = new_case(api, logistics=True)
    with SessionLocal() as db:
        db.get(M.CaseApplication, other).facility_ids = [fid]
        db.commit()
        db.get(M.CaseApplication, cid).status = status
        db.commit()
    blocked(api("PATCH", f"/facilities/{fid}", {"address": "Moved"}), "CASE_CLOSED")


@pytest.mark.parametrize("terminal", [False, True])
def test_selecting_immutable_facility_preserves_locked_source(api, terminal):
    cid = new_case(api, logistics=True)
    fid = ok(api("POST", f"/cases/{cid}/factories", {"name": "Reviewed warehouse"}))["facility_id"]
    other = new_case(api, logistics=True)
    with SessionLocal() as db:
        source, target = db.get(M.CaseApplication, cid), db.get(M.CaseApplication, other)
        target.company_name, target.nib = source.company_name, source.nib
        db.commit()
    freeze(cid)
    with SessionLocal() as db:
        snapshot = sm.frozen_scope(db, db.get(M.CaseApplication, cid))
        if terminal:
            db.get(M.CaseApplication, cid).status = "cancelled"
            db.commit()
    ok(api("PATCH", f"/cases/{other}/facilities-select", {"facility_ids": [fid]}))
    blocked(api("PATCH", f"/facilities/{fid}", {"address": "Unreviewed edit"}),
            "CASE_CLOSED" if terminal else "SCOPE_FROZEN")
    with SessionLocal() as db:
        assert fid in (db.get(M.CaseApplication, other).facility_ids or [])
        assert sm.frozen_scope(db, db.get(M.CaseApplication, cid)) == snapshot


@pytest.mark.parametrize("checker", [False, True])
def test_issue_rejects_facility_snapshot_drift(api, checker):
    cid = new_case(api, logistics=True)
    fid = ok(api("POST", f"/cases/{cid}/factories", {"name": "Reviewed"}))["facility_id"]
    paid(api, cid)
    freeze(cid)
    # 저장 가드 바깥의 과거 데이터/직접 SQL 변경도 발급 대조에서 탐지해야 한다.
    with SessionLocal() as db:
        db.execute(text("UPDATE facility SET name='Changed outside API' WHERE facility_id=:fid"), {"fid": fid})
        db.commit()
    blocked(request_issue(api, cid, checker), "SCOPE_CHANGED")


@pytest.mark.parametrize("checker", [False, True])
@pytest.mark.parametrize("state", ["refunded", "expired", "partially_refunded"])
def test_issue_checks_net_receipts(api, checker, state):
    cid = new_case(api)
    paid(api, cid)
    second = paid(api, cid, 200)
    with SessionLocal() as db:
        inv = db.get(M.Invoice, second)
        inv.status = state
        db.add(M.Payment(case_id=cid, invoice_id=second, amount=-100 if state == "partially_refunded" else -inv.total,
                         currency=inv.currency, status="confirmed", method="refund"))
        db.commit()
    freeze(cid)
    blocked(request_issue(api, cid, checker))


def test_direct_sharia_transition_is_forbidden(api):
    cid = new_case(api)
    with SessionLocal() as db:
        db.get(M.CaseApplication, cid).status = "final_package_preparation"
        db.commit()
    blocked(api("POST", f"/cases/{cid}/transition", {"to_state": "fatwa_review"}),
            "USE_DEDICATED_ENDPOINT", 403)


@pytest.mark.parametrize("checker", [False, True])
def test_issue_requires_review_and_dispatch(api, checker):
    cid = new_case(api)
    paid(api, cid)
    freeze(cid)
    blocked(request_issue(api, cid, checker), "FATWA_GATE_BLOCKED")


@pytest.mark.parametrize("phase", ["entry", "decision", "issue", "checker"])
@pytest.mark.parametrize("grounds", [{"annual_revenue": 16_000_000_000}, {"outlet_count": 2},
                                     {"business_scale": "Besar"}])
def test_self_declare_rechecks_quantitative_eligibility(api, phase, grounds):
    cid = new_case(api)
    with SessionLocal() as db:
        c = db.get(M.CaseApplication, cid)
        c.pathway, c.status, c.risk_category = "self_declare", "self_declaration_submitted", "low"
        c.annual_revenue, c.outlet_count = 1000, 1
        db.commit()
        c.sehati_eligible, c.fatwa_status = "eligible", "approved"
        db.commit()
    ok(api("PATCH", f"/cases/{cid}/profile", {"profile_ext": grounds}))
    with SessionLocal() as db:
        c = db.get(M.CaseApplication, cid)
        assert c.sehati_eligible == "needs_review" and c.fatwa_status == "none"
    if phase == "entry":
        r = api("POST", f"/cases/{cid}/transition", {"to_state": "committee_verification"})
    else:
        freeze(cid, pathway="self_declare")
        r = (api("POST", f"/cases/{cid}/committee/decide", {"decision": "approve"})
             if phase == "decision" else request_issue(api, cid, phase == "checker"))
    blocked(r)
    with SessionLocal() as db:
        assert db.get(M.CaseApplication, cid).sehati_eligible != "eligible"


def test_each_product_requires_material_links(api):
    cid = new_case(api)
    ok(api("POST", f"/cases/{cid}/products", {"name": "Unmapped product"}))
    paid(api, cid)
    freeze(cid)
    blocked(request_issue(api, cid), "PRODUCT_MATERIAL_LINKS_MISSING")


@pytest.mark.parametrize("checker", [False, True])
def test_each_linked_material_requires_completed_review(api, checker):
    cid = new_case(api)
    paid(api, cid)
    freeze(cid)
    with SessionLocal() as db:
        db.query(M.Material).filter_by(case_id=cid).one().screen_result = None
        db.commit()
    blocked(request_issue(api, cid, checker), "PRODUCT_MATERIAL_REVIEW_INCOMPLETE")


def test_admin_cannot_approve_own_refund(api):
    cid = new_case(api)
    iid = paid(api, cid)
    rid = ok(api("POST", f"/invoices/{iid}/refund/request", {"reason": "E2 refund"}))["refund_id"]
    blocked(api("POST", f"/refunds/{rid}/decide", {"decision": "approved"}), "SELF_APPROVAL_FORBIDDEN", 403)
    ok(api("POST", f"/refunds/{rid}/decide", {"decision": "approved"}, "operator1"))


@pytest.mark.parametrize("route,org_id", [
    (route, "foreign-e2") for route in ("consultation", "vehicle", "measurement", "layout", "stamp", "reconcile", "cleaning")
] + [(route, "org_demo") for route in ("measurement", "layout", "stamp", "reconcile", "cleaning")])
def test_body_resource_ids_cannot_cross_organizations_or_cases(api, route, org_id):
    cid = new_case(api)
    with SessionLocal() as db:
        foreign = M.CaseApplication(org_id=org_id, company_name="Foreign company")
        db.add(foreign)
        db.flush()
        doc = M.DocumentAsset(case_id=foreign.case_id, filename="secret.png", content_b64="aGVsbG8=")
        db.add(doc)
        db.commit()
        foreign_id, did = foreign.case_id, doc.document_id
    if route == "consultation":
        path, body = "/consultations", {"message": "E2", "case_id": foreign_id}
    elif route == "vehicle":
        path, body = "/orgs/org_demo/vehicles", {"plate_no": "E2", "case_id": foreign_id}
    elif route == "measurement":
        path, body = f"/cases/{cid}/measurements", {"param_key": "ethanol_pct", "value": 0, "document_id": did}
    elif route == "layout":
        path, body = f"/cases/{cid}/sjph-manual/layout", {"inserts": {"org_chart": {"document_id": did}}}
    elif route == "stamp":
        path, body = f"/cases/{cid}/sjph-manual/layout", {"signers": {"ceo": {"stamp_document_id": did}}}
    elif route == "reconcile":
        path, body = f"/cases/{cid}/halal-org/reconcile", {"document_id": did}
    else:
        vid = ok(api("POST", "/orgs/org_demo/vehicles", {"plate_no": "E2", "case_id": cid}))["vehicle_id"]
        path, body = f"/vehicles/{vid}/cleanings", {"cleaned_at": "2026-11-12", "photo_doc_id": did}
    blocked(api("POST", path, body, "applicant1"), status=403)


def test_authorized_body_references_and_missing_integration_case(api):
    cid = new_case(api)
    ok(api("POST", "/consultations", {"message": "Authorized", "case_id": cid}, "applicant1"))
    with SessionLocal() as db:
        doc = M.DocumentAsset(case_id=cid, filename="approved-image.png")
        db.add(doc)
        db.commit()
        did = doc.document_id
    ok(api("POST", f"/cases/{cid}/sjph-manual/layout",
           {"inserts": {"org_chart": {"document_id": did}},
            "signers": {"ceo": {"stamp_document_id": did}}}, "applicant1"))
    body = {"event_type": "external_application_created", "idempotency_key": uuid4().hex,
            "case_id": "missing-e2"}
    blocked(api("POST", "/integration/sihalal/event", body, "operator1"), "CASE_NOT_FOUND", 404)
    ok(api("POST", "/integration/sihalal/event", {**body, "case_id": cid}, "operator1"))


def report_ready(api, cid):
    """보고서·서명·SJPH와 독립 공동 심사자를 정상 API로 준비한다."""
    with SessionLocal() as db:
        c = db.get(M.CaseApplication, cid)
        c.status, c.pathway = "final_package_preparation", "reguler"
        db.commit()
    gid = ok(api("POST", f"/cases/{cid}/audit-report"))["gen_doc_id"]
    ok(api("POST", f"/gen-docs/{gid}/approve"))
    ok(api("POST", f"/cases/{cid}/audit-report/sign", {"name": "Auditor"}, "auditor1"))
    ok(api("POST", f"/cases/{cid}/audit-report/client-sign", {"name": "Applicant", "image": SIG}, "applicant1"))
    for element in m.HPAS_ELEMENTS:
        ok(api("PATCH", f"/cases/{cid}/sjph", {"element": element, "status": "ok"}))
    ok(api("POST", "/orgs/org_demo/penyelia", {"name": "E2 supervisor"}))
    ok(api("POST", f"/cases/{cid}/auditors", {"username": "auditor2"}))
    return gid


def dispatch(api, cid):
    ar = ok(api("POST", f"/cases/{cid}/audit-report/send-fatwa", user="auditor1"))
    assert ar.get("pending"), ar
    result = ok(api("POST", f"/approvals/{ar['approval_id']}/approve", {}, "auditor2"))
    assert result["result"]["transitioned_to"] == "fatwa_review"
    return ar["approval_id"]


@pytest.mark.parametrize("change", ["new_report", "content", "sjph", "signature"])
def test_dispatch_approval_binds_current_review(api, change):
    cid = new_case(api)
    gid = report_ready(api, cid)
    req = ok(api("POST", f"/cases/{cid}/audit-report/send-fatwa", user="auditor1"))
    if change == "new_report":
        gid = ok(api("POST", f"/cases/{cid}/audit-report"))["gen_doc_id"]
        ok(api("POST", f"/gen-docs/{gid}/approve"))
    elif change == "content":
        with SessionLocal() as db:
            db.get(M.GeneratedDocument, gid).content = "Changed after request"
            db.commit()
    elif change == "sjph":
        ok(api("PATCH", f"/cases/{cid}/sjph", {"element": "process", "status": "gap"}))
    else:
        ok(api("POST", f"/cases/{cid}/audit-report/sign", {"name": "New signature"}, "auditor1"))
    blocked(api("POST", f"/approvals/{req['approval_id']}/approve", {}, "auditor2"))
    with SessionLocal() as db:
        assert db.get(M.ApprovalRequest, req["approval_id"]).status == "pending"
        assert db.get(M.CaseApplication, cid).status != "fatwa_review"


@pytest.mark.parametrize("checker", [False, True])
@pytest.mark.parametrize("change", ["report", "sjph", "dispatch"])
def test_issue_rechecks_latest_dispatch(api, checker, change):
    cid = new_case(api)
    report_ready(api, cid)
    aid = dispatch(api, cid)
    paid(api, cid)
    freeze(cid)
    if change == "report":
        ok(api("POST", f"/cases/{cid}/audit-report"))
    elif change == "sjph":
        ok(api("PATCH", f"/cases/{cid}/sjph", {"element": "process", "status": "gap"}))
    else:
        with SessionLocal() as db:
            db.get(M.ApprovalRequest, aid).status = "cancelled"
            db.commit()
    blocked(request_issue(api, cid, checker), "FATWA_GATE_BLOCKED")


@pytest.mark.parametrize("logistics", [False, True])
def test_normal_review_and_split_billing_can_issue(api, logistics):
    cid = new_case(api, logistics=logistics)
    report_ready(api, cid)
    dispatch(api, cid)
    paid(api, cid)
    paid(api, cid, 200)
    with SessionLocal() as db:
        db.add(M.Invoice(case_id=cid, total=500, amount=500, status="draft", currency="KRW"))
        db.add(M.Invoice(case_id=cid, total=500, amount=500, status="cancelled", currency="KRW"))
        db.commit()
    ok(api("PATCH", f"/cases/{cid}/fatwa", {"decision": "approved"}, "fatwa1"))
    ok(api("POST", f"/cases/{cid}/fatwa/final-approve", {}, "operator1"))
    req = ok(request_issue(api, cid))
    ok(api("POST", f"/approvals/{req['approval_id']}/approve", {}))
    with SessionLocal() as db:
        assert db.query(M.HalalCertificate).filter_by(case_id=cid, status="active").count() == 1


def committee_accounts(api):
    members = []
    with SessionLocal() as db:
        password = db.query(M.User).filter_by(username="fatwa1").one().password_hash
        for role, label in (("chair", "Chair"), ("member", "B"), ("member", "C")):
            u = M.User(username="e2_" + uuid4().hex, password_hash=password, org_id="org_demo", role="fatwa_liaison")
            db.add(u)
            db.flush()
            members.append({"role": role, "name": label, "user_id": u.user_id, "user": u.username})
        db.commit()
    for member in members:
        login = ok(api.client.post("/auth/login", json={"username": member["user"], "password": "pw"}))
        api.headers[member["user"]] = {"Authorization": "Bearer " + login["token"]}
    return members


def test_formal_seats_require_distinct_real_accounts(api, monkeypatch):
    monkeypatch.setenv("GLHAC_FORMAL_FLOW", "1")
    members = committee_accounts(api)
    missing = [{k: v for k, v in member.items() if k not in ("user", "user_id")} for member in members]
    blocked(api("POST", "/fatwa/committee", {"members": missing}, "operator1"), "COMMITTEE_ACCOUNT_REQUIRED", 422)
    repeated = [{**member, "user_id": members[0]["user_id"]} for member in members]
    blocked(api("POST", "/fatwa/committee", {"members": repeated}, "operator1"), "DUPLICATE_COMMITTEE_USER", 422)
    ok(api("POST", "/fatwa/committee", {"members": members}, "operator1"))
    cid = new_case(api)
    report_ready(api, cid)
    dispatch(api, cid)
    paid(api, cid)
    for key in ("chairman", "member:B", "member:C"):
        blocked(api("POST", f"/cases/{cid}/fatwa/vote", {"member": key, "vote": "approve"}, "fatwa1"), "NOT_SEAT_OWNER", 403)
        blocked(api("POST", f"/cases/{cid}/fatwa/sign", {"member": key, "image": SIG}, "fatwa1"), "NOT_SEAT_OWNER", 403)
    for index, member in enumerate(members):
        key = "chairman" if index == 0 else "member:" + member["name"]
        ok(api("POST", f"/cases/{cid}/fatwa/vote", {"member": key, "vote": "approve"}, member["user"]))
        ok(api("POST", f"/cases/{cid}/fatwa/sign", {"member": key, "image": SIG}, member["user"]))
    blocked(api("POST", f"/cases/{cid}/fatwa/confirm", {}, members[1]["user"]), "NOT_CHAIR", 403)
    result = ok(api("POST", f"/cases/{cid}/fatwa/confirm", {}, members[0]["user"]))
    assert result["decision"] == "approved"
    ok(api("POST", f"/cases/{cid}/fatwa/final-approve", {}, "operator1"))
    issue = ok(request_issue(api, cid))
    ok(api("POST", f"/approvals/{issue['approval_id']}/approve", {}, members[0]["user"]))


def test_formal_tally_rejects_legacy_impersonated_actors(api, monkeypatch):
    monkeypatch.setenv("GLHAC_FORMAL_FLOW", "1")
    members = committee_accounts(api)
    ok(api("POST", "/fatwa/committee", {"members": members}, "operator1"))
    cid = new_case(api)
    with SessionLocal() as db:
        c = db.get(M.CaseApplication, cid)
        c.status = "fatwa_review"
        actor = members[0]["user_id"]
        for key in ("chairman", "member:B", "member:C"):
            db.add(M.FatwaVote(case_id=cid, member=key, vote="approve"))
            sm.record_event(db, c, c.status, c.status, "fatwa.vote", "fatwa_liaison", actor,
                            {"member": key, "vote": "approve", "round": 1})
            sm.record_event(db, c, c.status, c.status, "fatwa.sign", "fatwa_liaison", actor,
                            {"member": key, "image": SIG, "round": 1})
        db.commit()
    tally = ok(api("GET", f"/cases/{cid}/fatwa/votes"))
    assert tally["votes_cast"] == 1 and tally["signatures"] == 1
    assert not tally["quorum_met"] and not tally["signatures_ok"]
    blocked(api("POST", f"/cases/{cid}/fatwa/confirm", {}, members[0]["user"]), "VOTES_INCOMPLETE")
