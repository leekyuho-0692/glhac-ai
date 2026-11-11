"""d2: 상태 전이뿐 아니라 심의·검토·신원·종결 쓰기의 업무 불변식."""
import re
import runpy
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth, models, state_machine as sm
from app import main as m
from app.db import SessionLocal

SIG = "data:image/png;base64,iVBORw0KGgo="
WRITE_ROUTES = [(method, r.path) for r in m.app.routes
                if "{case_id}" in getattr(r, "path", "")
                for method in sorted(getattr(r, "methods", set()) & {"POST", "PUT", "PATCH", "DELETE"})]


@pytest.fixture(scope="module")
def client():
    with TestClient(m.app) as c:
        yield c


@pytest.fixture(scope="module")
def tokens(client):
    out = {}
    for username in ("admin", "operator1", "auditor1", "consultant1", "applicant1", "fatwa1"):
        r = client.post("/auth/login", json={"username": username, "password": "admin" if username == "admin" else "pw"})
        assert r.status_code == 200, r.text
        out[username] = {"Authorization": "Bearer " + r.json()["token"]}
    return out


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("GLHAC_FORMAL_FLOW", "0")
    monkeypatch.setenv("GLHAC_RATE_LIMIT_PER_MIN", "0")


def case(status="onboarding", **kw):
    with SessionLocal() as db:
        c = models.CaseApplication(org_id="org_demo", company_name=kw.pop("company_name", "D2 " + uuid.uuid4().hex),
                                   status=status, **kw)
        db.add(c)
        db.commit()
        return c.case_id


def code(r):
    detail = r.json().get("detail", {})
    return detail.get("code") if isinstance(detail, dict) else None


def auditor(client):
    username = "d2aud" + uuid.uuid4().hex[:12]
    with SessionLocal() as db:
        u = models.User(username=username, password_hash=auth.hash_pw("D2Password!123"), role="auditor", org_id="org_demo")
        db.add(u)
        db.commit()
        uid = u.user_id
    r = client.post("/auth/login", json={"username": username, "password": "D2Password!123"})
    assert r.status_code == 200, r.text
    return uid, {"Authorization": "Bearer " + r.json()["token"]}


def test_proxy_requires_three_distinct_independent_auditors(client, tokens, monkeypatch):
    monkeypatch.setenv("GLHAC_FORMAL_FLOW", "1")
    cid = case("fatwa_review")
    reviewers = [auditor(client) for _ in range(3)]
    for i, (uid, header) in enumerate(reviewers):
        r = client.post(f"/cases/{cid}/fatwa/vote", headers=header, json={"member": "auditor", "vote": "approve"})
        assert r.status_code == 200, r.text
        assert r.json()["quorum_need"] == 3 and r.json()["votes_cast"] == i + 1
        assert r.json()["quorum_met"] is (i == 2)
        assert client.post(f"/cases/{cid}/fatwa/sign", headers=header, json={"member": "auditor", "image": SIG}).status_code == 200
        if i < 2:
            r = client.post(f"/cases/{cid}/fatwa/confirm", headers=header)
            assert r.status_code == 409 and code(r) == "VOTES_INCOMPLETE", r.text
    # 같은 계정의 별도 슬롯 사칭과 반복 투표는 사람 수를 늘리지 못한다.
    r = client.post(f"/cases/{cid}/fatwa/vote", headers=reviewers[0][1],
                    json={"member": "auditor:" + reviewers[1][0], "vote": "approve"})
    assert r.status_code == 422, r.text
    r = client.post(f"/cases/{cid}/fatwa/vote", headers=reviewers[0][1], json={"member": "auditor", "vote": "approve"})
    assert r.json()["votes_cast"] == 3
    r = client.post(f"/cases/{cid}/fatwa/confirm", headers=reviewers[0][1])
    assert r.status_code == 200 and r.json()["decision"] == "approved", r.text
    r = client.post(f"/cases/{cid}/fatwa/final-approve", headers=tokens["operator1"])
    assert r.status_code == 200, r.text
    assert client.post(f"/cases/{cid}/fatwa/final-approve", headers=tokens["operator1"]).json()["already_final"] is True


def test_concurrent_proxy_votes_still_count_as_one_person(client, monkeypatch):
    monkeypatch.setenv("GLHAC_FORMAL_FLOW", "1")
    cid = case("fatwa_review")
    uid, header = auditor(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.post(f"/cases/{cid}/fatwa/vote", headers=header,
                            json={"member": "auditor", "vote": "approve"}), range(2)))
    assert all(r.status_code == 200 and r.json()["votes_cast"] == 1 for r in results), [r.text for r in results]
    with SessionLocal() as db:
        assert db.query(models.FatwaVote).filter_by(case_id=cid, member="auditor:" + uid).count() == 1
    r = client.post(f"/cases/{cid}/fatwa/confirm", headers=header)
    assert r.status_code == 409 and code(r) == "VOTES_INCOMPLETE", r.text


@pytest.mark.parametrize("participation", ["main", "co", "historical", "onsite", "lph"])
def test_proxy_excludes_onsite_auditors(client, tokens, monkeypatch, participation):
    monkeypatch.setenv("GLHAC_FORMAL_FLOW", "1")
    cid = case("fatwa_review")
    uid, header = auditor(client)
    with SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        if participation in {"main", "co"}:
            db.add(models.CaseAuditor(case_id=cid, user_id=uid, role=participation))
        elif participation == "lph":
            db.add(models.LphAssignment(case_id=cid, lph_name="LPH D2", auditor_ref=uid))
        else:
            action = "ops.auditor_assigned" if participation == "historical" else "onsite.sign"
            sm.record_event(db, c, c.status, c.status, action, "auditor", uid, {"auditor_id": uid})
        db.commit()
    for endpoint, body in [("vote", {"member": "auditor", "vote": "approve"}),
                           ("sign", {"member": "auditor", "image": SIG}), ("confirm", {})]:
        r = client.post(f"/cases/{cid}/fatwa/{endpoint}", headers=header, json=body)
        assert r.status_code == 403 and code(r) == "FATWA_AUDITOR_CONFLICT", r.text


@pytest.mark.parametrize("status", ["onboarding", "consultant_review", "fatwa_approved", "certificate_issued", "withdrawn"])
@pytest.mark.parametrize("method,path,body", [
    ("POST", "vote", {"member": "chairman", "vote": "reject", "note": "NC"}),
    ("POST", "sign", {"member": "chairman", "image": SIG}),
    ("POST", "confirm", {}),
    ("PATCH", "", {"decision": "rejected"}),
])
def test_sharia_requires_review_state(client, tokens, status, method, path, body):
    cid = case(status)
    r = client.request(method, f"/cases/{cid}/fatwa" + ("/" + path if path else ""),
                       headers=tokens["fatwa1"], json=body)
    expected = "CASE_CLOSED" if sm.is_terminal(status) else "FATWA_INVALID_STATE"
    assert r.status_code == 409 and code(r) == expected, r.text
    with SessionLocal() as db:
        assert not db.query(models.FatwaVote).filter_by(case_id=cid).count()
        assert not db.query(models.WorkflowEvent).filter_by(case_id=cid, action="fatwa.sign").count()


@pytest.mark.parametrize("status", ["onboarding", "withdrawn", "certificate_issued"])
def test_final_approval_checks_state_even_with_provisional_decision(client, tokens, status):
    cid = case(status, fatwa_status="provisional")
    with SessionLocal() as db:
        db.add(models.FatwaDecision(case_id=cid, decision="approved"))
        db.commit()
    r = client.post(f"/cases/{cid}/fatwa/final-approve", headers=tokens["operator1"])
    assert r.status_code == 409, r.text


def test_certificate_blocks_decision_even_if_case_status_is_rewound(client, tokens):
    cid = case("fatwa_review")
    with SessionLocal() as db:
        db.add(models.HalalCertificate(case_id=cid, certificate_no="D2-" + cid, status="suspended"))
        db.commit()
    r = client.patch(f"/cases/{cid}/fatwa", headers=tokens["fatwa1"], json={"decision": "rejected"})
    assert r.status_code == 409 and code(r) == "FATWA_DECISION_FROZEN", r.text


@pytest.mark.parametrize("target,source", [("document_pre_audit_approved", "document_pre_audit_in_review"),
                                          ("lph_assignment", "document_pre_audit_approved"),
                                          ("onsite_audit_scheduled", "lph_assignment")])
def test_raw_transition_cannot_skip_reviews_or_lph(client, tokens, target, source):
    cid = case(source)
    with SessionLocal() as db:
        db.add(models.ExternalIdentity(case_id=cid, org_id="org_demo", verification_status="verified", identifier_match=True))
        db.commit()
    r = client.post(f"/cases/{cid}/transition", headers=tokens["admin"], json={"to_state": target})
    assert r.status_code in (403, 409), r.text
    if target != "document_pre_audit_approved":
        assert r.json()["detail"]["blockers"] == [{"code": "LPH_NOT_ASSIGNED"}], r.text
    with SessionLocal() as db:
        assert db.get(models.CaseApplication, cid).status == source


def test_mock_approval_requires_second_reviewer_then_advances(client, tokens, monkeypatch):
    monkeypatch.setenv("GLHAC_TWO_PERSON", "strict")
    cid = case("document_pre_audit_in_review")
    uid, header = auditor(client)
    with SessionLocal() as db:
        db.add(models.CaseAuditor(case_id=cid, user_id=uid, role="co"))
        db.commit()
    r = client.post(f"/cases/{cid}/mock-audit/decision", headers=tokens["auditor1"], json={"result": "pass"})
    assert r.status_code == 200 and r.json()["pending"] is True, r.text
    aid = r.json()["approval_id"]
    raw = client.post(f"/cases/{cid}/transition", headers=tokens["admin"], json={"to_state": "document_pre_audit_approved"})
    assert code(raw) == "USE_DEDICATED_ENDPOINT", raw.text
    r = client.post(f"/approvals/{aid}/approve", headers=header)
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        assert db.get(models.CaseApplication, cid).status == "document_pre_audit_approved"
    cid = case("lph_assignment")
    assert client.post(f"/cases/{cid}/lph-assignment", headers=tokens["operator1"], json={"lph_name": "LPH D2"}).status_code == 200
    r = client.post(f"/cases/{cid}/transition", headers=tokens["auditor1"], json={"to_state": "onsite_audit_scheduled"})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("role,status", [("consultant1", "closed"), ("auditor1", "garbage"), ("auditor1", "closed")])
def test_major_cannot_be_closed_without_auditor_review(client, tokens, role, status):
    cid = case("corrective_action_required")
    with SessionLocal() as db:
        f = models.AuditFinding(case_id=cid, finding="Major NC", severity="major", status="open")
        db.add(f)
        db.commit()
        fid = f.finding_id
    r = client.patch(f"/findings/{fid}", headers=tokens[role], json={"status": status})
    assert r.status_code in (403, 409, 422), r.text
    with SessionLocal() as db:
        assert db.get(models.AuditFinding, fid).status == "open"


def test_major_car_requires_independent_auditor_and_keeps_consultant_submission(client, tokens):
    cid = case("corrective_action_required")
    with SessionLocal() as db:
        f = models.AuditFinding(case_id=cid, finding="Major NC", severity="major", status="open")
        db.add(f)
        db.commit()
        fid = f.finding_id
    r = client.post(f"/findings/{fid}/car", headers=tokens["consultant1"],
                    json={"description": "위생 절차 수정", "evidence": "검증 사진 및 기록"})
    assert r.status_code == 200, r.text
    car_id = r.json()["id"]
    r = client.patch(f"/car/{car_id}/review", headers=tokens["operator1"], json={"status": "accepted"})
    assert r.status_code == 403, r.text
    r = client.patch(f"/car/{car_id}/review", headers=tokens["auditor1"], json={"status": "accepted", "note": "현장 검증 완료"})
    assert r.status_code == 200, r.text
    assert client.patch(f"/findings/{fid}", headers=tokens["auditor1"], json={"status": "open"}).status_code == 409


def test_identity_duplicate_check_runs_when_only_company_name_changes(client, tokens):
    name = "D2 Same " + uuid.uuid4().hex
    nib = "9" + str(uuid.uuid4().int)[:12]
    case(company_name=name, nib=nib)
    cid = case(company_name=name, nib=nib)
    r = client.patch(f"/cases/{cid}/profile", headers=tokens["applicant1"], json={"company_name": "Different Company"})
    assert r.status_code == 409 and code(r) == "NIB_ALREADY_USED", r.text


@pytest.mark.parametrize("body", [{"company_name": "Changed"}, {"nib": "9123400000000"}, {"nib": None},
                                  {"profile_ext": {"company_name_en": "Changed Ltd"}}])
def test_issued_identity_is_frozen_and_public_verify_uses_snapshot(client, tokens, body):
    cid = case("fatwa_approved", nib="9" + str(uuid.uuid4().int)[:12])
    with SessionLocal() as db:
        c = db.get(models.CaseApplication, cid)
        original = c.company_name
        result = m._do_issue_certificate(db, c, {"role": "admin", "uid": "d2", "username": "d2"})
    r = client.patch(f"/cases/{cid}/profile", headers=tokens["applicant1"], json=body)
    assert r.status_code == 409 and code(r) == "IDENTITY_CHANGE_REQUIRES_RENEWAL", r.text
    # DB 직접 수정은 발급 시점과 현재 값이 다름을 재현한다. API 가드는 우회하지 않는다.
    with SessionLocal() as db:
        db.query(models.CaseApplication).filter_by(case_id=cid).update({"company_name": "Tampered current value"})
        db.commit()
    r = client.get(result["verify_url"])
    assert r.status_code == 200 and r.json()["company_name"] == original, r.text


def test_legacy_certificate_never_uses_current_company_as_issuance_identity(client):
    cid = case("certificate_issued", company_name="Current unverified name")
    with SessionLocal() as db:
        db.add(models.HalalCertificate(case_id=cid, certificate_no="D2-" + cid, status="active", qr_token=cid))
        db.commit()
    r = client.get(f"/verify/{cid}")
    assert r.status_code == 200, r.text
    assert r.json()["company_name"] is None and r.json()["identity_snapshot_available"] is False


@pytest.mark.parametrize("issued", [False, True])
def test_identity_guard_also_covers_extraction_confirmation(client, tokens, issued):
    name = "D2 confirm " + uuid.uuid4().hex
    nib = "9" + str(uuid.uuid4().int)[:12]
    case(company_name=name, nib=nib)
    cid = case("certificate_issued" if issued else "onboarding", company_name=name, nib=nib)
    with SessionLocal() as db:
        if issued:
            db.add(models.HalalCertificate(case_id=cid, status="active"))
        d = models.DocumentAsset(case_id=cid, filename="company.pdf", doc_type="nib_business_license",
            fields={"_autofill": {"conflicts": [{"status": "pending", "scope": "case", "field": "company_name",
                                               "current": name, "proposed": "Renamed"}]}})
        db.add(d)
        db.commit()
        did = d.document_id
    r = client.post(f"/cases/{cid}/pending-extractions/apply", headers=tokens["applicant1"],
                    json={"autofill_choices": [{"conflict_id": did + ":0", "value": "Renamed"}]})
    expected = "IDENTITY_CHANGE_REQUIRES_RENEWAL" if issued else "NIB_ALREADY_USED"
    assert r.status_code == 409 and code(r) == expected, r.text
    with SessionLocal() as db:
        assert db.get(models.CaseApplication, cid).company_name == name


@pytest.mark.parametrize("status", sorted(sm.TERMINAL_STATES))
def test_indirect_writes_and_explicit_closed_case_exceptions(client, tokens, status):
    cid = case(status)
    with SessionLocal() as db:
        f = models.AuditFinding(case_id=cid, severity="minor", status="open")
        db.add(f)
        db.flush()
        car = models.CorrectiveAction(case_id=cid, finding_id=f.finding_id, description="fix")
        ar = models.ApprovalRequest(case_id=cid, action_type="certificate.issue", requested_by="d2")
        db.add_all([car, ar, models.HalalCertificate(case_id=cid, certificate_no="D2-" + cid, status="active")])
        db.commit()
        fid, carid, aid = f.finding_id, car.id, ar.id
    for method, path, body in [("PATCH", f"/findings/{fid}", {"status": "closed"}),
                                ("DELETE", f"/findings/{fid}", None),
                                ("POST", f"/findings/{fid}/car", {"description": "fix"}),
                                ("PATCH", f"/car/{carid}/review", {"status": "accepted"}),
                                ("POST", f"/approvals/{aid}/approve", {})]:
        r = client.request(method, path, headers=tokens["admin"], json=body)
        assert r.status_code == 409 and code(r) == "CASE_CLOSED", r.text
    assert client.get(f"/cases/{cid}", headers=tokens["admin"]).status_code == 200
    assert client.post(f"/cases/{cid}/fatwa/document", headers=tokens["fatwa1"]).status_code == 200
    r = client.post(f"/cases/{cid}/certificate/revoke", headers=tokens["operator1"], json={"reason": "종결 후 잔존 인증서 철회"})
    assert r.status_code == 200, r.text
    r = client.post(f"/approvals/{r.json()['approval_id']}/approve", headers=tokens["admin"])
    assert r.status_code == 200, r.text
    r = client.post(f"/cases/{cid}/certificate/reactivate", headers=tokens["operator1"], json={"reason": "철회 후 재개 시도"})
    assert r.status_code == 409 and code(r) == "CASE_CLOSED", r.text


def test_identity_change_uses_renewal_without_changing_parent(client, tokens):
    cid = case("certificate_issued", nib="9" + str(uuid.uuid4().int)[:12])
    with SessionLocal() as db:
        db.add(models.HalalCertificate(case_id=cid, status="active"))
        db.commit()
        original = db.get(models.CaseApplication, cid).company_name
    r = client.post(f"/cases/{cid}/renew", headers=tokens["operator1"])
    assert r.status_code == 200, r.text
    child = r.json()["new_case_id"]
    r = client.patch(f"/cases/{child}/profile", headers=tokens["applicant1"], json={"company_name": "새 상호"})
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        assert db.get(models.CaseApplication, cid).company_name == original
        assert db.get(models.CaseApplication, child).status == "onboarding"


@pytest.mark.parametrize("status", sorted(sm.TERMINAL_STATES))
@pytest.mark.parametrize("method,path", WRITE_ROUTES, ids=[f"{method} {path}" for method, path in WRITE_ROUTES])
def test_all_case_write_routes_reject_closed_cases(client, tokens, status, method, path):
    if (method, path) in getattr(m, "CLOSED_CASE_WRITE_EXCEPTIONS", {}):
        return  # 아래 예외 계약 테스트에서 별도 검증한다.
    cid = case(status)
    url = re.sub(r"\{[^}]+\}", "d2-placeholder", path.replace("{case_id}", cid))
    r = client.request(method, url, headers=tokens["admin"], json={})
    assert r.status_code == 409 and code(r) == "CASE_CLOSED", f"{method} {url}: {r.status_code} {r.text}"


def test_renewal_is_idempotent_and_cancelled_with_parent(client, tokens):
    cid = case("certificate_issued")
    with SessionLocal() as db:
        db.add(models.HalalCertificate(case_id=cid, certificate_no="D2-" + cid, status="active"))
        db.commit()
    first = client.post(f"/cases/{cid}/renew", headers=tokens["operator1"])
    second = client.post(f"/cases/{cid}/renew", headers=tokens["operator1"])
    assert first.status_code == second.status_code == 200, (first.text, second.text)
    child = first.json()["new_case_id"]
    assert second.json()["new_case_id"] == child
    r = client.post(f"/cases/{cid}/certificate/revoke", headers=tokens["operator1"], json={"reason": "인증 철회 요청"})
    assert r.status_code == 200, r.text
    r = client.post(f"/approvals/{r.json()['approval_id']}/approve", headers=tokens["admin"])
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        assert db.get(models.CaseApplication, child).status == "cancelled"


def test_concurrent_renewal_returns_one_child(client, tokens):
    cid = case("certificate_issued")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.post(f"/cases/{cid}/renew", headers=tokens["operator1"]), range(2)))
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    assert len({r.json()["new_case_id"] for r in results}) == 1


def test_formal_srs_cycle_over_testclient(client, monkeypatch, capsys):
    """CI의 같은 SRS 스크립트를 소켓 없이 실행한다. 앱의 가드는 대체하지 않는다."""
    monkeypatch.setenv("GLHAC_FORMAL_FLOW", "1")
    monkeypatch.setenv("GLHAC_E2E_FORMAL", "1")
    monkeypatch.setenv("GLHAC_E2E_BASE", "http://127.0.0.1:8897")
    monkeypatch.setenv("GLHAC_E2E_SKIP_OLLAMA", "1")
    monkeypatch.delenv("GLHAC_CYCLE_OUT", raising=False)
    monkeypatch.syspath_prepend(str(Path(__file__).parent))

    def transport(method, original):
        def request(url, **kwargs):
            if url.startswith("http://127.0.0.1:8897/"):
                kwargs.pop("timeout", None)
                return client.request(method, url, **kwargs)
            return original(url, **kwargs)
        return request

    for method in ("get", "post", "patch", "put", "delete"):
        monkeypatch.setattr(httpx, method, transport(method.upper(), getattr(httpx, method)))
    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(Path(__file__).with_name("e2e_srs_cycle.py")), run_name="__main__")
    output = capsys.readouterr().out
    assert result.value.code == 0, output
    assert re.search(r"PASS \d+ / FAIL 0", output), output
