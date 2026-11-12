"""R7 f1: final2-dom/new_probe.py N11–N15를 양 모드 TestClient 회귀로 고정.

보고서 준비 함수와 공격 순서는 원 재현의 case/review_ready/provisional/issue를
재사용했다. DB 쓰기는 단계 준비·손상된 과거 자료 대조군에만 사용한다.
외부 LLM만 대체하며 인증·가드·서명·승인·경쟁은 실제 API/독립 세션을 사용한다.
"""
from concurrent.futures import ThreadPoolExecutor
import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import ai_local, main as m, models as M, state_machine as sm
from app.db import SessionLocal

SIG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


def ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def blocked(r):
    assert r.status_code == 409, r.text


def seed_member_headers(client, *, chair="admin"):
    """기존 서명 테스트용 실계정 좌석. 반복 서명은 같은 소유자, 다른 좌석은 다른 사람."""
    admin = {"Authorization": "Bearer " + ok(client.post(
        "/auth/login", json={"username": "admin", "password": "admin"}))["token"]}
    third = "r7_member_" + uuid4().hex[:12]
    ok(client.post("/admin/users", headers=admin, json={
        "username": third, "password": "R7Member!234", "role": "fatwa_liaison", "org_id": "org_demo"}))
    accounts = {"chairman": chair, "member:member1": "fatwa1" if chair == "admin" else "admin", "member:member2": third}
    members = [{"role": "chair" if key == "chairman" else "member",
                "name": "Chair" if key == "chairman" else key.split(":", 1)[1], "user": username}
               for key, username in accounts.items()]
    operator = {"Authorization": "Bearer " + ok(client.post(
        "/auth/login", json={"username": "operator1", "password": "pw"}))["token"]}
    ok(client.post("/fatwa/committee", headers=operator, json={"members": members}))
    headers = {}
    for key, name in accounts.items():
        password = "admin" if name == "admin" else "pw" if name == "fatwa1" else "R7Member!234"
        token = ok(client.post("/auth/login", json={"username": name, "password": password}))["token"]
        headers[key] = {"Authorization": "Bearer " + token}
    return headers


@pytest.fixture(params=[0, 1], ids=["normal", "formal"])
def api(request, monkeypatch):
    monkeypatch.setenv("GLHAC_FORMAL_FLOW", str(request.param))
    monkeypatch.setenv("GLHAC_RATE_LIMIT_PER_MIN", "0")
    monkeypatch.setenv("GLHAC_NOTIFY_WORKER", "0")
    monkeypatch.setenv("GLHAC_TWO_PERSON", "0")
    monkeypatch.setattr(m, "_AUTOPARSE", False)
    monkeypatch.setattr(m, "_DOC_VERIFY", False)
    monkeypatch.setattr(ai_local, "llm_json", lambda *a, **k: {"error": "TEST_OFFLINE"})
    monkeypatch.setattr(ai_local, "llm_text", lambda *a, **k: "")
    monkeypatch.setattr(ai_local, "search_context", lambda *a, **k: [])
    with TestClient(m.app) as client:
        headers = {}
        names = ["admin", "applicant1", "consultant1", "auditor1", "auditor2", "operator1", "fatwa1", "pendamping1"]
        for name in names:
            token = ok(client.post("/auth/login", json={"username": name, "password": "admin" if name == "admin" else "pw"}))["token"]
            headers[name] = {"Authorization": "Bearer " + token}

        def call(method, path, body=None, actor="admin"):
            return client.request(method, path, json=body, headers=headers[actor])

        proxies = []
        for i in range(3):
            name = "r7_proxy_" + uuid4().hex[:12]
            ok(call("POST", "/admin/users", {"username": name, "password": "R7Proxy!234", "role": "auditor", "org_id": "org_demo"}))
            token = ok(client.post("/auth/login", json={"username": name, "password": "R7Proxy!234"}))["token"]
            headers[name] = {"Authorization": "Bearer " + token}
            proxies.append(name)
        call.formal, call.proxies, call.headers, call.client = request.param, proxies, headers, client
        yield call


def case(api, logistics=False):
    body = {"company_name": "R7 " + uuid4().hex[:12], "is_msme": False}
    if logistics:
        body.update(scheme="logistics", logistics_scope=["penyimpanan"])
    cid = ok(api("POST", "/cases", body, "applicant1"))["case_id"]
    if logistics:
        fid = ok(api("POST", f"/cases/{cid}/factories", {"name": "Reviewed warehouse", "address": "Original site"}, "applicant1"))["facility_id"]
        ok(api("PATCH", f"/facilities/{fid}", {"address": "Original site"}, "applicant1"))
        return cid, fid, None
    pid = ok(api("POST", f"/cases/{cid}/products", {"name": "Reviewed product"}, "applicant1"))["product_id"]
    mid = ok(api("POST", f"/cases/{cid}/materials", {"name": "sugar"}, "applicant1"))["material_id"]
    ok(api("POST", f"/cases/{cid}/matrix/link", {"product_id": pid, "material_id": mid, "linked": True}, "applicant1"))
    return cid, pid, mid


def review(api, cid):
    with SessionLocal() as db:
        c = db.get(M.CaseApplication, cid)
        c.pathway, c.status = "reguler", "final_package_preparation"
        db.commit()
    gid = ok(api("POST", f"/cases/{cid}/audit-report", {}))["gen_doc_id"]
    ok(api("POST", f"/gen-docs/{gid}/approve", {}))
    ok(api("POST", f"/cases/{cid}/audit-report/sign", {"name": "Reviewed auditor"}, "auditor1"))
    ok(api("POST", f"/cases/{cid}/audit-report/client-sign", {"name": "Reviewed applicant", "image": SIG}, "applicant1"))
    for el in m.HPAS_ELEMENTS:
        ok(api("PATCH", f"/cases/{cid}/sjph", {"element": el, "status": "ok"}))
    ok(api("POST", "/orgs/org_demo/penyelia", {"name": "R7 supervisor"}))
    ok(api("POST", f"/cases/{cid}/auditors", {"username": "auditor2"}))
    aid = ok(api("POST", f"/cases/{cid}/audit-report/send-fatwa", {}, "auditor1"))["approval_id"]
    ok(api("POST", f"/approvals/{aid}/approve", {}, "auditor2"))
    with SessionLocal() as db:
        sm.freeze_scope(db, db.get(M.CaseApplication, cid))
        db.commit()
    return gid, aid


def provisional(api, cid):
    if api.formal:
        for actor in api.proxies:
            ok(api("POST", f"/cases/{cid}/fatwa/vote", {"member": "auditor", "vote": "approve"}, actor))
            ok(api("POST", f"/cases/{cid}/fatwa/sign", {"member": "auditor", "image": SIG}, actor))
        ok(api("POST", f"/cases/{cid}/fatwa/confirm", {}, api.proxies[0]))
    else:
        ok(api("PATCH", f"/cases/{cid}/fatwa", {"decision": "approved"}, "fatwa1"))


def ready(api, logistics=False, final=True):
    cid, pid, mid = case(api, logistics)
    with SessionLocal() as db:
        db.add(M.Contract(case_id=cid, status="confirmed", currency="KRW"))
        db.commit()
    inv = ok(api("POST", f"/cases/{cid}/invoices", {"service_type": "pre_audit", "amount": 100}))
    ok(api("PATCH", f"/invoices/{inv['invoice_id']}/pay", actor="operator1"))
    review(api, cid)
    provisional(api, cid)
    if final:
        ok(api("POST", f"/cases/{cid}/fatwa/final-approve", {}, "operator1"))
    return cid, pid, mid


def issue(api, cid):
    r = api("POST", f"/cases/{cid}/certificate/issue", {}, "operator1")
    if r.status_code == 200 and r.json().get("approval_id"):
        return api("POST", f"/approvals/{r.json()['approval_id']}/approve", {}, "fatwa1")
    return r


def unlock(api, cid):
    return api("POST", f"/cases/{cid}/certificate/unlock", {"reason": "Correct scope for fresh review"}, "operator1")


@pytest.mark.parametrize("kind", ["product", "facility"])
def test_n11_unlocked_scope_requires_fresh_review(api, kind):
    cid, pid, mid = ready(api, logistics=kind == "facility", final=False)
    ok(unlock(api, cid))
    if kind == "product":
        new = ok(api("POST", f"/cases/{cid}/products", {"name": "Unreviewed addition"}, "applicant1"))["product_id"]
        ok(api("POST", f"/cases/{cid}/matrix/link", {"product_id": new, "material_id": mid, "linked": True}, "applicant1"))
    else:
        ok(api("PATCH", f"/facilities/{pid}", {"address": "Unreviewed site"}, "applicant1"))
    blocked(api("POST", f"/cases/{cid}/fatwa/final-approve", {}, "operator1"))
    blocked(issue(api, cid))
    # 보완 후 보고서·서명·송부·의결을 새로 거치면 정상 발급되어야 한다.
    review(api, cid)
    provisional(api, cid)
    ok(api("POST", f"/cases/{cid}/fatwa/final-approve", {}, "operator1"))
    ok(issue(api, cid))


def test_n11_identity_cannot_replace_final_review(api):
    cid, _, _ = ready(api)
    changed = api("PATCH", f"/cases/{cid}/profile", {"company_name": "Replacement " + uuid4().hex, "nib": "9" + str(uuid4().int)[:12]}, "applicant1")
    assert changed.status_code == 409 or issue(api, cid).status_code == 409


def test_n11_unlock_cancels_pending_issue_and_old_dispatch(api):
    cid, _, _ = ready(api)
    aid = ok(api("POST", f"/cases/{cid}/certificate/issue", {}, "operator1"))["approval_id"]
    ok(unlock(api, cid))
    with SessionLocal() as db:
        assert db.get(M.ApprovalRequest, aid).status == "cancelled"
        assert db.get(M.CaseApplication, cid).fatwa_status != "approved"
    blocked(api("POST", f"/approvals/{aid}/approve", {}, "fatwa1"))


def test_n11_old_provisional_cannot_approve_a_new_report(api):
    cid, _, _ = ready(api, final=False)
    review(api, cid)
    blocked(api("POST", f"/cases/{cid}/fatwa/final-approve", {}, "operator1"))


def test_n11_pending_issue_cannot_reuse_replaced_review(api):
    cid, _, _ = ready(api)
    aid = ok(api("POST", f"/cases/{cid}/certificate/issue", {}, "operator1"))["approval_id"]
    review(api, cid)
    provisional(api, cid)
    ok(api("POST", f"/cases/{cid}/fatwa/final-approve", {}, "operator1"))
    blocked(api("POST", f"/approvals/{aid}/approve", {}, "fatwa1"))


def test_n11_unsigned_scope_change_requires_regenerated_report(api):
    cid, _, _ = case(api)
    gid = ok(api("POST", f"/cases/{cid}/audit-report", {}))["gen_doc_id"]
    ok(api("POST", f"/gen-docs/{gid}/approve", {}))
    ok(api("POST", f"/cases/{cid}/products", {"name": "Added after report"}, "applicant1"))
    blocked(api("POST", f"/cases/{cid}/audit-report/sign", {"name": "Auditor"}, "auditor1"))


@pytest.mark.parametrize("material", ["lard", "gelatin", "salt"])
def test_n12_reactivation_checks_materials_and_review_scope(api, material):
    cid, pid, _ = ready(api)
    cert = ok(issue(api, cid))["result"]
    ok(api("POST", f"/cases/{cid}/certificate/suspend", {"reason": "Recipe needs review"}, "operator1"))
    ok(unlock(api, cid))
    mid = ok(api("POST", f"/cases/{cid}/materials", {"name": material}, "applicant1"))["material_id"]
    ok(api("POST", f"/cases/{cid}/matrix/link", {"product_id": pid, "material_id": mid, "linked": True}, "applicant1"))
    blocked(api("POST", f"/cases/{cid}/certificate/reactivate", {"reason": "Recipe correction reviewed"}, "operator1"))
    assert ok(api("GET", cert["verify_url"]))["valid"] is False


def test_n12_unchanged_review_can_reactivate(api):
    cid, _, _ = ready(api)
    cert = ok(issue(api, cid))["result"]
    ok(api("POST", f"/cases/{cid}/certificate/suspend", {"reason": "Temporary review"}, "operator1"))
    ok(api("POST", f"/cases/{cid}/certificate/reactivate", {"reason": "No scope change; checked"}, "operator1"))
    assert ok(api("GET", cert["verify_url"]))["valid"] is True


@pytest.mark.parametrize("change", ["lard", "gelatin", "scope", "pending"])
def test_n12_reactivation_rechecks_current_data_even_without_unlock(api, change):
    cid, pid, mid = ready(api)
    cert = ok(issue(api, cid))["result"]
    ok(api("POST", f"/cases/{cid}/certificate/suspend", {"reason": "Review latest evidence"}, "operator1"))
    with SessionLocal() as db:
        if change in {"lard", "gelatin"}:
            db.execute(text("UPDATE material SET name=:name WHERE material_id=:mid"), {"name": change, "mid": mid})
        elif change == "scope":
            db.execute(text("UPDATE product SET name='Changed recipe' WHERE product_id=:pid"), {"pid": pid})
        else:
            db.add(M.DocumentAsset(case_id=cid, filename="pending.txt", doc_type="material_list",
                                   fields={"material_names": ["lard"], "_sources": {"material_names": "llm"}}))
        db.commit()
    blocked(api("POST", f"/cases/{cid}/certificate/reactivate", {"reason": "Latest evidence reviewed"}, "operator1"))
    assert ok(api("GET", cert["verify_url"]))["valid"] is False


def test_n13_car_conflicting_decisions_one_winner(api, monkeypatch):
    cid, _, _ = ready(api)
    fid = ok(api("POST", f"/cases/{cid}/findings", {"finding": "Major separation gap", "severity": "major"}, "auditor1"))["finding_id"]
    car = ok(api("POST", f"/findings/{fid}/car", {"description": "Fixed separation", "evidence": "Inspection record"}, "consultant1"))
    barrier = threading.Barrier(2)
    original = m._get_case

    def synchronize(db, case_id, user=None):
        # 양 요청이 CAR의 이전 상태를 읽은 뒤 경쟁시킨다(가드/DB는 대체하지 않음).
        if case_id == cid and user and user.get("username") in {"auditor1", "auditor2"}:
            barrier.wait(timeout=15)
        return original(db, case_id, user)

    with monkeypatch.context() as race_patch:
        race_patch.setattr(m, "_get_case", synchronize)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(api, "PATCH", f"/car/{car['id']}/review", {"status": state, "note": "Independent review"}, actor)
                       for state, actor in [("accepted", "auditor1"), ("rejected", "auditor2")]]
            replies = [f.result(timeout=25) for f in futures]
    assert sorted(r.status_code for r in replies) == [200, 409], [r.text for r in replies]
    with SessionLocal() as db:
        result, finding = db.get(M.CorrectiveAction, car["id"]), db.get(M.AuditFinding, fid)
        assert (result.status, finding.status) in {("accepted", "closed"), ("rejected", "open")}


def test_n13_legacy_closed_nc_requires_valid_car_at_issue(api):
    cid, _, _ = ready(api)
    with SessionLocal() as db:
        f = M.AuditFinding(case_id=cid, finding="Old inconsistent decision", severity="major", status="closed")
        db.add(f)
        db.flush()
        db.add(M.CorrectiveAction(case_id=cid, finding_id=f.finding_id, status="rejected", description="Invalid", evidence="Rejected"))
        db.commit()
    blocked(issue(api, cid))


def test_n14_renewal_references_immutable_facility(api):
    cid, fid, _ = ready(api, logistics=True)
    ok(issue(api, cid))
    child = ok(api("POST", f"/cases/{cid}/renew", {}, "operator1"))["new_case_id"]
    ok(api("PATCH", f"/cases/{child}/facilities-select", {"facility_ids": [fid]}, "applicant1"))
    assert ok(api("GET", f"/cases/{child}"))["facility_ids"] == [fid]
    blocked(api("PATCH", f"/facilities/{fid}", {"address": "Must stay immutable"}, "applicant1"))
    blocked(api("DELETE", f"/facilities/{fid}", actor="applicant1"))
    with SessionLocal() as db:
        assert db.get(M.Facility, fid).address == "Original site"
        assert sm.frozen_scope(db, db.get(M.CaseApplication, cid))["facilities"][0]["address"] == "Original site"


def selfdeclare(api):
    cid = ok(api("POST", "/cases", {"company_name": "Self declare " + uuid4().hex[:12], "is_msme": True}, "applicant1"))["case_id"]
    ok(api("PATCH", f"/cases/{cid}/profile", {"nib": "9" + str(uuid4().int)[:12]}, "applicant1"))
    pid = ok(api("POST", f"/cases/{cid}/products", {"name": "Citric acid"}, "applicant1"))["product_id"]
    mid = ok(api("POST", f"/cases/{cid}/materials", {"name": "citric acid"}, "applicant1"))["material_id"]
    ok(api("POST", f"/cases/{cid}/matrix/link", {"product_id": pid, "material_id": mid, "linked": True}, "applicant1"))
    ok(api("POST", "/orgs/org_demo/penyelia", {"name": "Self declare supervisor"}))
    eid = ok(api("POST", f"/cases/{cid}/sihalal/identity/link", {"external_email": "r7@example.invalid"}))["external_identity_id"]
    ok(api("POST", f"/sihalal/identity/{eid}/verify", {"expected_identifier": "r7@example.invalid"}))
    ok(api("POST", f"/cases/{cid}/submit-application", {}, "applicant1"))
    ok(api("POST", f"/cases/{cid}/pathway/assess", {}))
    ok(api("POST", f"/cases/{cid}/pathway/confirm", {"pathway": "self_declare"}))
    ok(api("POST", f"/cases/{cid}/transition", {"to_state": "sjph_lite_prepared"}))
    pp = next(u["user_id"] for u in ok(api("GET", "/admin/users")) if u["username"] == "pendamping1")
    ok(api("POST", f"/cases/{cid}/pendamping/assign", {"pendamping_id": pp}))
    ok(api("POST", f"/cases/{cid}/transition", {"to_state": "pendamping_verification"}))
    ok(api("POST", f"/cases/{cid}/pendamping/verify", {"decision": "verified"}, "pendamping1"))
    ok(api("POST", f"/cases/{cid}/transition", {"to_state": "committee_verification"}, "operator1"))
    ok(api("POST", f"/cases/{cid}/committee/decide", {"decision": "approve"}, "fatwa1"))
    return cid, eid


@pytest.mark.parametrize("pending", [False, True])
def test_n15_identity_withdrawal_invalidates_committee_and_issue(api, pending):
    cid, eid = selfdeclare(api)
    aid = ok(api("POST", f"/cases/{cid}/certificate/issue", {}, "operator1"))["approval_id"] if pending else None
    ok(api("POST", f"/sihalal/identity/{eid}/verify", {"expected_identifier": "different@example.invalid"}))
    blocked(api("POST", f"/approvals/{aid}/approve", {}, "fatwa1") if aid else issue(api, cid))
    with SessionLocal() as db:
        assert db.get(M.CaseApplication, cid).fatwa_status != "approved"
        if aid:
            assert db.get(M.ApprovalRequest, aid).status == "cancelled"
    # 식별자 재확인만으로 옛 의결이 살아나면 안 된다.
    ok(api("POST", f"/sihalal/identity/{eid}/verify", {"expected_identifier": "r7@example.invalid"}))
    blocked(issue(api, cid))
    ok(api("POST", f"/cases/{cid}/committee/decide", {"decision": "approve"}, "fatwa1"))
    ok(issue(api, cid))


def test_n15_issue_rechecks_current_selfdeclare_prerequisites(api):
    cid, eid = selfdeclare(api)
    # 저장 가드를 통하지 않은 과거 상태도 최종 발급 검사에서 거절한다.
    with SessionLocal() as db:
        db.execute(text("UPDATE external_identity SET verification_status='mismatch_found', identifier_match=0 WHERE external_identity_id=:eid"), {"eid": eid})
        db.commit()
    blocked(issue(api, cid))


def test_n15_pendamping_withdrawal_requires_new_committee_decision(api):
    cid, _ = selfdeclare(api)
    ok(api("POST", f"/cases/{cid}/pendamping/verify", {"decision": "rework"}, "pendamping1"))
    blocked(issue(api, cid))
    ok(api("POST", f"/cases/{cid}/pendamping/verify", {"decision": "verified"}, "pendamping1"))
    blocked(issue(api, cid))
    ok(api("POST", f"/cases/{cid}/committee/decide", {"decision": "approve"}, "fatwa1"))
    ok(issue(api, cid))


def test_remaining_registered_members_must_sign_and_vote_for_themselves(api):
    cid, _, _ = case(api)
    with SessionLocal() as db:
        db.get(M.CaseApplication, cid).status = "fatwa_review"
        db.commit()
    members = []
    for i in range(3):
        name = "r7_sharia_" + uuid4().hex[:12]
        ok(api("POST", "/admin/users", {"username": name, "password": "R7Sharia!234", "role": "fatwa_liaison", "org_id": "org_demo"}))
        token = ok(api.client.post("/auth/login", json={"username": name, "password": "R7Sharia!234"}))["token"]
        api.headers[name] = {"Authorization": "Bearer " + token}
        members.append({"role": "chair" if i == 0 else "member", "name": name, "user": name})
    ok(api("POST", "/fatwa/committee", {"members": members}, "operator1"))
    try:
        for member in members:
            key = "chairman" if member["role"] == "chair" else "member:" + member["name"]
            for action, payload in [("vote", {"member": key, "vote": "approve"}), ("sign", {"member": key, "image": SIG})]:
                r = api("POST", f"/cases/{cid}/fatwa/{action}", payload, "fatwa1")
                assert r.status_code == 403, r.text
                ok(api("POST", f"/cases/{cid}/fatwa/{action}", payload, member["user"]))
        tally = ok(api("GET", f"/cases/{cid}/fatwa/votes", actor="operator1"))
        assert tally["quorum_met"] is True
    finally:
        # 다음 테스트의 기본 독립 오디터 대리 설정을 복원(감사 기록은 보존).
        with SessionLocal() as db:
            shim = m._committee_shim("org_demo")
            sm.record_event(db, shim, None, None, m.FATWA_COMMITTEE_ACTION, "system", None, {"members": []})
            db.commit()
