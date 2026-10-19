"""R1 남은 항목 묶음(상태·흐름) 부정·정상 테스트.

R1-1 케이스 종결 상태(withdrawn·rejected·revoked·cancelled)
R1-2 자기선언 긍정목록 경고(막지 않고 경고만)
R1-3 2인 확인 strict 보강(GLHAC_TWO_PERSON=strict 일 때 operator/admin 우회 불가)

수정이 '막아야 할 것을 실제로 막고 정상 흐름은 열어 두는가'를 실제 엔드포인트·함수 호출로 본다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_remaining_r1_test.db")
os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_NOTIFY_WORKER", "0")
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app                    # noqa: E402
from app import models, auth, nav8, state_machine as sm  # noqa: E402
from app.db import SessionLocal             # noqa: E402

PW = "Passw0rd!23"


# ---------- 헬퍼 ----------
def _tok(c, u, p="pw"):
    r = c.post("/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _op(c):
    return _tok(c, "operator1")


def _adm(c):
    return _tok(c, "admin", "admin")


def _db_case(company="PT R1", status="onboarding", org_id="org_demo", **kw):
    db = SessionLocal()
    try:
        c = models.CaseApplication(org_id=org_id, company_name=company, status=status, **kw)
        db.add(c)
        db.commit()
        return c.case_id
    finally:
        db.close()


def _db_add(obj):
    db = SessionLocal()
    try:
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj
    finally:
        db.close()


def _db_get(model, pk):
    db = SessionLocal()
    try:
        o = db.get(model, pk)
        if o is not None:
            db.expunge(o)
        return o
    finally:
        db.close()


def _status(cid):
    return _db_get(models.CaseApplication, cid).status


def _code(r):
    d = r.json().get("detail")
    return d.get("code") if isinstance(d, dict) else None


def _events(cid, action):
    db = SessionLocal()
    try:
        return (db.query(models.WorkflowEvent)
                .filter_by(case_id=cid, action=action).order_by(models.WorkflowEvent.event_id).all())
    finally:
        db.close()


def _mkuser(username, role, org_id="org_demo", company_role="client_admin"):
    db = SessionLocal()
    try:
        u = models.User(username=username, password_hash=auth.hash_pw(PW), role=role,
                        org_id=org_id, company_role=company_role)
        db.add(u)
        db.commit()
        return u.user_id
    finally:
        db.close()


OPEN_STATES = [s for s in sm.TRANSITIONS if s not in sm.TERMINAL_STATES]


# ================= R1-1. 케이스 종결 상태 =================
def test_r11_terminal_transition_table():
    """종결 4종: 비종결 모든 상태에서 withdrawn·rejected·cancelled 진입, revoked 는 발급 이후 4개 상태에서만."""
    assert sm.TERMINAL_STATES == {"withdrawn", "rejected", "revoked", "cancelled"}
    assert OPEN_STATES, "비종결 상태가 있어야 한다"
    for s in OPEN_STATES:
        for t in ("withdrawn", "rejected", "cancelled"):
            assert sm.allowed(s, t), (s, t)
        assert sm.allowed(s, "revoked") == (s in {"certificate_issued", "post_certification_monitoring",
                                                  "change_impact", "renewal_preparation"}), s
    # 부정: 종결에서 나가는 전이는 없다(어떤 상태로도)
    for t in sm.TERMINAL_STATES:
        assert sm.TRANSITIONS[t] == set(), t
        for to in list(sm.TRANSITIONS) + ["onboarding"]:
            assert not sm.allowed(t, to), (t, to)
    # raw /transition 으로는 못 들어간다
    assert sm.TERMINAL_STATES <= sm.PROTECTED_STATES
    # 다음 단계 후보(forward)에는 종결이 섞이지 않는다
    assert sm.forward_states("pathway_determination") == {"self_declare_eligible",
                                                           "supplementation_required", "consultant_review"}


def test_r11_close_each_status_cancels_invoices_and_approvals():
    with TestClient(app) as c:
        h = _op(c)
        for to in ("withdrawn", "rejected", "cancelled"):
            cid = _db_case("PT close " + to, status="consultant_review")
            inv_ids = {}
            for st in ("draft", "invoice_created", "waiting_payment", "paid"):
                inv_ids[st] = _db_add(models.Invoice(case_id=cid, service_type="pre_audit", amount=1.0,
                                                     ppn=0.0, total=1.0, status=st)).invoice_id
            ar = _db_add(models.ApprovalRequest(action_type="consultant.change", case_id=cid,
                                                org_id="org_demo", status="pending", requested_by="u-x"))
            r = c.post("/cases/%s/close" % cid, headers=h, json={"status": to, "reason": "테스트 사유 " + to})
            assert r.status_code == 200, r.text
            assert r.json()["status"] == to and r.json()["case"]["closed"] is True
            assert _status(cid) == to
            evs = _events(cid, "case.close")
            assert len(evs) == 1 and evs[0].to_status == to and evs[0].from_status == "consultant_review"
            assert evs[0].payload["reason"] == "테스트 사유 " + to
            for st in ("draft", "invoice_created", "waiting_payment"):
                assert _db_get(models.Invoice, inv_ids[st]).status == "cancelled", st
            assert _db_get(models.Invoice, inv_ids["paid"]).status == "paid"      # 결제 끝난 건은 건드리지 않는다
            a = _db_get(models.ApprovalRequest, ar.id)
            assert a.status == "cancelled" and "CASE_CLOSED" in (a.decision_reason or "")
            db = SessionLocal()
            try:
                n = db.query(models.Notification).filter_by(case_id=cid, event_type="case.closed").count()
            finally:
                db.close()
            assert n >= 1


def test_r11_close_validation():
    with TestClient(app) as c:
        h = _op(c)
        cid = _db_case("PT close val", status="consultant_review")
        # 부정: 사유 없음 / 공백
        assert c.post("/cases/%s/close" % cid, headers=h, json={"status": "cancelled"}).status_code == 422
        r = c.post("/cases/%s/close" % cid, headers=h, json={"status": "cancelled", "reason": "   "})
        assert r.status_code == 422 and _code(r) == "REASON_REQUIRED"
        # 부정: revoked 는 이 엔드포인트로 못 간다
        r = c.post("/cases/%s/close" % cid, headers=h, json={"status": "revoked", "reason": "x"})
        assert r.status_code == 409 and _code(r) == "USE_CERT_REVOKE"
        # 부정: 임의 상태
        r = c.post("/cases/%s/close" % cid, headers=h, json={"status": "audit_closed", "reason": "x"})
        assert r.status_code == 422 and _code(r) == "INVALID_CLOSE_STATUS"
        assert _status(cid) == "consultant_review"
        # 부정: 존재하지 않는 케이스
        assert c.post("/cases/nope/close", headers=h, json={"status": "cancelled", "reason": "x"}).status_code == 404


def test_r11_raw_transition_cannot_enter_terminal():
    with TestClient(app) as c:
        h = _op(c)
        cid = _db_case("PT raw", status="consultant_review")
        for t in sorted(sm.TERMINAL_STATES):
            r = c.post("/cases/%s/transition" % cid, headers=h, json={"to_state": t})
            assert r.status_code == 403 and _code(r) == "USE_DEDICATED_ENDPOINT", (t, r.text)
        assert _status(cid) == "consultant_review"


def test_r11_closed_case_is_read_only_and_cannot_leave():
    with TestClient(app) as c:
        ho, ha, hc = _op(c), _tok(c, "applicant1"), _tok(c, "consultant1")
        cid = _db_case("PT closed rw", status="consultant_review")
        assert c.post("/cases/%s/close" % cid, headers=ho,
                      json={"status": "cancelled", "reason": "중복 등록"}).status_code == 200
        # 부정: 다시 종결 / 어디로도 전이 불가
        r = c.post("/cases/%s/close" % cid, headers=ho, json={"status": "withdrawn", "reason": "x"})
        assert r.status_code == 409 and _code(r) == "CASE_CLOSED"
        for to in ("onboarding", "application_draft", "consultant_review", "certificate_issued", "withdrawn"):
            r = c.post("/cases/%s/transition" % cid, headers=ho, json={"to_state": to})
            assert r.status_code == 409 and _code(r) == "CASE_CLOSED", (to, r.text)
        # 부정: 주요 쓰기 경로 전부 409 CASE_CLOSED
        mat = {"name": "설탕"}
        r = c.post("/cases/%s/materials" % cid, headers=ha, json=mat)
        assert r.status_code == 409 and _code(r) == "CASE_CLOSED", r.text
        r = c.post("/cases/%s/products" % cid, headers=ha, json={"name": "과자"})
        assert r.status_code == 409 and _code(r) == "CASE_CLOSED", r.text
        r = c.post("/cases/%s/documents" % cid, headers=ha, json={"filename": "a.pdf", "file_b64": "QUJD"})
        assert r.status_code == 409 and _code(r) == "CASE_CLOSED", r.text
        r = c.post("/cases/%s/contract/request" % cid, headers=ha)
        assert r.status_code == 409 and _code(r) == "CASE_CLOSED", r.text
        r = c.post("/cases/%s/contract/generate" % cid, headers=ho, json={})
        assert r.status_code == 409 and _code(r) == "CASE_CLOSED", r.text
        r = c.post("/cases/%s/quote" % cid, headers=ho, json={})
        assert r.status_code == 409 and _code(r) == "CASE_CLOSED", r.text
        r = c.post("/cases/%s/invoices" % cid, headers=hc,
                   json={"service_type": "pre_audit", "amount": 1000})
        assert r.status_code == 409 and _code(r) == "CASE_CLOSED", r.text
        # 기존 원재료의 수정·삭제·이름변경도 막는다
        m = _db_add(models.Material(case_id=cid, name="소금"))
        for method, url, body in (("patch", "/materials/%s" % m.material_id, {"cert_no": "X"}),
                                  ("patch", "/materials/%s/rename" % m.material_id, {"name": "굵은소금"}),
                                  ("delete", "/materials/%s" % m.material_id, None)):
            r = getattr(c, method)(url, headers=ha, **({"json": body} if body is not None else {}))
            assert r.status_code == 409 and _code(r) == "CASE_CLOSED", (url, r.text)
        assert _db_get(models.Material, m.material_id) is not None
        # 정상: 읽기는 그대로
        r = c.get("/cases/%s" % cid, headers=ha)
        assert r.status_code == 200 and r.json()["status"] == "cancelled" and r.json()["closed"] is True
        assert c.get("/cases/%s/timeline" % cid, headers=ha).status_code == 200
        # 정상: 열린 케이스는 같은 쓰기가 통과한다(차단이 종결 때문임을 대조)
        cid2 = _db_case("PT open rw", status="consultant_review")
        r = c.post("/cases/%s/products" % cid2, headers=ha, json={"name": "과자"})
        assert r.status_code == 200, r.text


def test_r11_permissions_applicant_withdraw_only_own_org():
    with TestClient(app) as c:
        ha = _tok(c, "applicant1")          # org_demo · client_admin
        own = _db_case("PT own", status="application_draft")
        other = _db_case("PT other org", status="application_draft", org_id="org_other_r1")
        # 정상: 자기 조직 케이스 철회
        r = c.post("/cases/%s/close" % own, headers=ha, json={"status": "withdrawn", "reason": "사업 중단"})
        assert r.status_code == 200 and _status(own) == "withdrawn", r.text
        # 부정: 신청자는 rejected·cancelled 불가
        own2 = _db_case("PT own2", status="application_draft")
        for to in ("rejected", "cancelled"):
            r = c.post("/cases/%s/close" % own2, headers=ha, json={"status": to, "reason": "x"})
            assert r.status_code == 403 and _code(r) == "NOT_AUTHORIZED_CLOSE", (to, r.text)
        # 부정: 타 조직 케이스는 철회 불가
        r = c.post("/cases/%s/close" % other, headers=ha, json={"status": "withdrawn", "reason": "x"})
        assert r.status_code == 403, r.text
        assert _status(other) == "application_draft" and _status(own2) == "application_draft"
        # 부정: 업무자(client_staff)는 철회 불가
        _mkuser("r1_staff", "applicant", "org_demo", company_role="client_staff")
        hs = _tok(c, "r1_staff", PW)
        r = c.post("/cases/%s/close" % own2, headers=hs, json={"status": "withdrawn", "reason": "x"})
        assert r.status_code == 403 and _code(r) == "NOT_COMPANY_ADMIN", r.text
        # 부정: 컨설턴트·오디터 불가 / 사유 없는 신청자 철회 422
        for u in ("consultant1", "auditor1", "fatwa1"):
            r = c.post("/cases/%s/close" % own2, headers=_tok(c, u), json={"status": "withdrawn", "reason": "x"})
            assert r.status_code == 403, (u, r.text)
        r = c.post("/cases/%s/close" % own2, headers=ha, json={"status": "withdrawn", "reason": ""})
        assert r.status_code == 422 and _code(r) == "REASON_REQUIRED"
        # 정상: 운영자는 타 조직 케이스도 종결
        r = c.post("/cases/%s/close" % other, headers=_op(c), json={"status": "cancelled", "reason": "오등록"})
        assert r.status_code == 200 and _status(other) == "cancelled"


def _issued_case(company, certs=("active",)):
    cid = _db_case(company, status="certificate_issued", pathway="reguler", fatwa_status="approved",
                   scope_frozen=True)
    for i, st in enumerate(certs):
        _db_add(models.HalalCertificate(case_id=cid, certificate_no="R1-%s-%d" % (cid[:6], i), status=st))
    return cid


def test_r11_cert_revoke_auto_closes_case_as_revoked():
    with TestClient(app) as c:
        cid = _issued_case("PT revoke auto")
        inv = _db_add(models.Invoice(case_id=cid, service_type="onsite", amount=1.0, ppn=0.0,
                                     total=1.0, status="waiting_payment")).invoice_id
        r = c.post("/cases/%s/certificate/revoke" % cid, headers=_op(c), json={"reason": "허위 서류 적발"})
        assert r.status_code == 200, r.text
        aid = r.json()["approval_id"]
        assert _status(cid) == "certificate_issued"          # 승인 전에는 그대로
        r = c.post("/approvals/%s/approve" % aid, headers=_adm(c), json={})
        assert r.status_code == 200, r.text
        assert _status(cid) == "revoked"
        ev = _events(cid, "case.close.revoked")
        assert len(ev) == 1 and ev[0].to_status == "revoked" and ev[0].payload["reason"] == "허위 서류 적발"
        assert _db_get(models.Invoice, inv).status == "cancelled"
        # 철회 승인 요청 자체는 approved 로 마무리(취소로 덮이지 않는다)
        assert _db_get(models.ApprovalRequest, aid).status == "approved"
        # 부정: revoked 가 된 뒤에는 어떤 쓰기도 불가
        r = c.post("/cases/%s/products" % cid, headers=_tok(c, "applicant1"), json={"name": "x"})
        assert r.status_code == 409 and _code(r) == "CASE_CLOSED"


def test_r11_cert_revoke_keeps_case_open_when_other_active_cert():
    with TestClient(app) as c:
        cid = _issued_case("PT revoke two", certs=("active", "suspended"))   # 활성은 케이스당 1장(uq_cert_active) — 나머지는 정지
        r = c.post("/cases/%s/certificate/revoke" % cid, headers=_op(c), json={"reason": "한 장만 철회"})
        aid = r.json()["approval_id"]
        r = c.post("/approvals/%s/approve" % aid, headers=_adm(c), json={})
        assert r.status_code == 200, r.text
        assert r.json()["result"]["case_closed"] is False
        assert _status(cid) == "certificate_issued"         # 다른 유효 인증서가 남아 있으면 종결 안 함
        assert _events(cid, "case.close.revoked") == []


def test_r11_aggregations_classify_terminal_as_closed():
    with TestClient(app) as c:
        h = _op(c)
        cid = _db_case("PT agg", status="consultant_review")
        db = SessionLocal()
        try:
            cur = db.get(models.CaseApplication, cid)
            before = nav8.compute_step8(db, cur)
        finally:
            db.close()
        assert before["closed"] is False and before["done"] is False
        k0 = c.get("/dashboard/summary", headers=h).json()["kpi"]
        assert c.post("/cases/%s/close" % cid, headers=h,
                      json={"status": "rejected", "reason": "불합격"}).status_code == 200
        db = SessionLocal()
        try:
            after = nav8.compute_step8(db, db.get(models.CaseApplication, cid))
        finally:
            db.close()
        assert after["closed"] is True and after["done"] is True and after["closed_status"] == "rejected"
        # 목록: 종결 플래그 / 할 일·진행 중 집계에서 빠진다
        items = c.get("/cases?limit=500", headers=h).json()["items"]
        me = next(i for i in items if i["case_id"] == cid)
        assert me["closed"] is True and me["done"] is True
        todo = c.get("/me/todo", headers=h).json()["items"]
        assert cid not in [t["case_id"] for t in todo]
        # 대시보드: 'rejected' 가 조치 필요로 잡히지 않고 종결로 센다
        k1 = c.get("/dashboard/summary", headers=h).json()["kpi"]
        assert k1["closed"] == k0["closed"] + 1
        assert k1["action"] == k0["action"]
        assert k1["total"] == k0["total"]
        assert k1["in_progress"] == k0["in_progress"] - 1
        # 워크플로 모니터: 종결 단계로, 차단 사유 없음
        wm = c.get("/admin/workflow-monitor", headers=h).json()
        row = next(r for r in wm["cases"] if r["case_id"] == cid)
        assert row["closed"] is True and row["phase_key"] == "closed" and row["blockers"] == []
        # 분석 요약
        an = c.get("/analytics/summary", headers=h).json()
        assert an["cases_closed"] >= 1 and an["cases_total"] == an["cases_closed"] + an["cases_open"]
        # 다음 단계 후보에 종결 상태가 끼지 않는다
        wf = c.get("/cases/%s/workflow" % _db_case("PT wf", status="pathway_determination"), headers=h).json()
        assert not ({n["to"] for n in wf["next_states"]} & sm.TERMINAL_STATES)
