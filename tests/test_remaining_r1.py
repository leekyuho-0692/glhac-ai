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


# ================= R1-2. 자기선언 긍정목록 경고(막지 않는다) =================
def _sd_case(company, mats):
    """경로 판정 단계 + 자기선언 가드(low·MSME·핵심원료 없음)를 통과하는 케이스. mats=[(이름, 긍정목록 여부)]"""
    cid = _db_case(company, status="pathway_determination", risk_category="low", is_msme=True)
    for name, listed in mats:
        _db_add(models.Material(case_id=cid, name=name,
                                matched_uid=("uid-" + name) if listed else None,
                                screen_status="halal" if listed else None))
    return cid


def test_r12_selfdeclare_entry_warns_not_blocks_off_list_material():
    with TestClient(app) as c:
        h = _tok(c, "consultant1")
        cid = _sd_case("PT sd off-list", [("양념소스X", False), ("정제수", True)])
        r = c.post("/cases/%s/transition" % cid, headers=h, json={"to_state": "self_declare_eligible"})
        assert r.status_code == 200, r.text            # 막지 않는다
        body = r.json()
        assert _status(cid) == "self_declare_eligible" and body["to"] == "self_declare_eligible"
        w = body["warnings"]
        assert w == [{"code": "NOT_ON_POSITIVE_LIST", "materials": ["양념소스X"]}]
        assert body["case"]["warnings"] == w            # 케이스 응답에도 싣는다
        evs = _events(cid, "selfdeclare.positive_list_warning")
        assert len(evs) == 1 and evs[0].payload["warnings"] == w      # 감사 추적 1건
        # 케이스 조회에도 경고가 남아 있다(자기선언 구간)
        g = c.get("/cases/%s" % cid, headers=h).json()
        assert g["warnings"] == w and g["status"] == "self_declare_eligible"


def test_r12_selfdeclare_entry_no_materials_warns():
    with TestClient(app) as c:
        h = _tok(c, "consultant1")
        cid = _sd_case("PT sd empty", [])
        r = c.post("/cases/%s/transition" % cid, headers=h, json={"to_state": "self_declare_eligible"})
        assert r.status_code == 200, r.text
        assert r.json()["warnings"] == [{"code": "NO_MATERIALS"}]
        assert len(_events(cid, "selfdeclare.positive_list_warning")) == 1


def test_r12_selfdeclare_all_on_list_has_no_warning():
    """대조: 전부 긍정목록이면 경고도 이벤트도 없다."""
    with TestClient(app) as c:
        h = _tok(c, "consultant1")
        cid = _sd_case("PT sd clean", [("정제수", True), ("설탕", True)])
        r = c.post("/cases/%s/transition" % cid, headers=h, json={"to_state": "self_declare_eligible"})
        assert r.status_code == 200 and r.json()["warnings"] == []
        assert _events(cid, "selfdeclare.positive_list_warning") == []
        assert c.get("/cases/%s" % cid, headers=h).json()["warnings"] == []
    # 열린 정규 경로 케이스는 경고를 싣지 않는다
    with TestClient(app) as c:
        cid2 = _db_case("PT reg", status="consultant_review")
        assert c.get("/cases/%s" % cid2, headers=_tok(c, "consultant1")).json()["warnings"] == []


def test_r12_pathway_confirm_also_warns_and_other_guards_still_block():
    with TestClient(app) as c:
        h = _tok(c, "consultant1")
        cid = _sd_case("PT sd confirm", [("미등록첨가물", False)])
        r = c.post("/cases/%s/pathway/confirm" % cid, headers=h, json={"pathway": "self_declare"})
        assert r.status_code == 200, r.text
        assert r.json()["warnings"][0]["code"] == "NOT_ON_POSITIVE_LIST"
        assert len(_events(cid, "selfdeclare.positive_list_warning")) == 1
        # 부정: 경고만 하는 것이지 다른 가드를 푼 게 아니다 — 금지 원재료는 여전히 409
        bad = _db_case("PT sd haram", status="pathway_determination", risk_category="low", is_msme=True)
        _db_add(models.Material(case_id=bad, name="돼지젤라틴", screen_status="haram"))
        r = c.post("/cases/%s/transition" % bad, headers=h, json={"to_state": "self_declare_eligible"})
        assert r.status_code == 409 and _code(r) == "TRANSITION_BLOCKED"
        assert _status(bad) == "pathway_determination"
        assert _events(bad, "selfdeclare.positive_list_warning") == []
    # 가드 함수 자체에는 긍정목록 코드가 없다(차단 목록에 섞이지 않는다)
    db = SessionLocal()
    try:
        cur = db.get(models.CaseApplication, cid)
        assert "NOT_ON_POSITIVE_LIST" not in [g["code"] for g in sm.guard_pathway_selfdeclare(db, cur)]
    finally:
        db.close()


# ================= R1-3. 2인 확인 strict 보강 =================
REVIEW_BODY = {"sections": {"documents": {"ok": True, "note": ""}}, "verdict": "supplement", "note": "보완 필요"}


def _co_case(company, co_username):
    """공동 오디터가 배정된 케이스 + 그 오디터 계정."""
    cid = _db_case(company, status="consultant_review")
    uid = _mkuser(co_username, "auditor")
    _db_add(models.CaseAuditor(case_id=cid, user_id=uid, role="co"))
    return cid, uid


def test_r13_non_strict_keeps_existing_behaviour(monkeypatch):
    monkeypatch.delenv("GLHAC_TWO_PERSON", raising=False)
    with TestClient(app) as c:
        # 공동 오디터 없음 → operator 즉시 실행
        cid = _db_case("PT 2p basic", status="consultant_review")
        r = c.post("/cases/%s/preassess/review" % cid, headers=_op(c), json=REVIEW_BODY)
        assert r.status_code == 200 and not r.json().get("pending"), r.text
        assert len(_events(cid, "preassess.review")) == 1
        # 공동 오디터 있어도 operator/admin 은 종전처럼 우회(비 strict)
        cid2, _ = _co_case("PT 2p basic co", "r1_co_basic")
        r = c.post("/cases/%s/preassess/review" % cid2, headers=_op(c), json=REVIEW_BODY)
        assert r.status_code == 200 and not r.json().get("pending"), r.text
        assert len(_events(cid2, "preassess.review")) == 1
        # 메인 오디터의 요청은 공동 오디터 확인 대기 → 비 strict 에서 admin 은 확인자로 통과(종전)
        cid3, _ = _co_case("PT 2p basic main", "r1_co_basic3")
        r = c.post("/cases/%s/preassess/review" % cid3, headers=_tok(c, "auditor1"), json=REVIEW_BODY)
        assert r.status_code == 200 and r.json()["pending"] is True, r.text
        r2 = c.post("/approvals/%s/approve" % r.json()["approval_id"], headers=_adm(c), json={})
        assert r2.status_code == 200, r2.text
        assert len(_events(cid3, "preassess.review")) == 1


def test_r13_strict_operator_alone_is_blocked_without_co_auditor(monkeypatch):
    monkeypatch.setenv("GLHAC_TWO_PERSON", "strict")
    with TestClient(app) as c:
        cid = _db_case("PT 2p strict none", status="consultant_review")
        for h in (_op(c), _adm(c)):
            r = c.post("/cases/%s/preassess/review" % cid, headers=h, json=REVIEW_BODY)
            assert r.status_code == 409 and _code(r) == "CO_AUDITOR_REQUIRED", r.text
        assert _events(cid, "preassess.review") == []        # 실행되지 않았다


def test_r13_strict_operator_goes_pending_and_only_co_auditor_confirms(monkeypatch):
    monkeypatch.setenv("GLHAC_TWO_PERSON", "strict")
    with TestClient(app) as c:
        cid, _ = _co_case("PT 2p strict co", "r1_co_strict")
        r = c.post("/cases/%s/preassess/review" % cid, headers=_op(c), json=REVIEW_BODY)
        assert r.status_code == 200 and r.json()["pending"] is True, r.text
        aid = r.json()["approval_id"]
        assert _events(cid, "preassess.review") == []                  # 대기 — 아직 실행 안 됨
        assert _db_get(models.ApprovalRequest, aid).requested_by
        # 부정: 요청자 본인 확인 금지, strict 에서는 admin 도 확인자가 못 된다
        r = c.post("/approvals/%s/approve" % aid, headers=_op(c), json={})
        assert r.status_code == 403 and _code(r) == "SELF_APPROVAL_FORBIDDEN", r.text
        r = c.post("/approvals/%s/approve" % aid, headers=_adm(c), json={})
        assert r.status_code == 403 and _code(r) == "NOT_A_CHECKER", r.text
        # 승인 목록에서도 admin 은 결정 불가로 보인다
        lst = c.get("/approvals", headers=_adm(c)).json()
        mine = next(x for x in (lst if isinstance(lst, list) else lst.get("items", [])) if x["approval_id"] == aid)
        assert mine["can_decide"] is False
        assert _events(cid, "preassess.review") == []
        # 정상: 그 케이스의 공동 오디터가 확인하면 실행된다
        r = c.post("/approvals/%s/approve" % aid, headers=_tok(c, "r1_co_strict", PW), json={})
        assert r.status_code == 200, r.text
        assert len(_events(cid, "preassess.review")) == 1


def test_r13_strict_cert_approval_requires_different_person(monkeypatch):
    monkeypatch.setenv("GLHAC_TWO_PERSON", "strict")
    with TestClient(app) as c:
        cid = _issued_case("PT 2p cert")
        r = c.post("/cases/%s/certificate/revoke" % cid, headers=_op(c), json={"reason": "strict 확인"})
        assert r.status_code == 200, r.text
        aid = r.json()["approval_id"]
        # 부정: 요청한 operator 본인은 승인 못 한다(철회 확인자 역할이 아니고, 본인이기도 하다)
        r = c.post("/approvals/%s/approve" % aid, headers=_op(c), json={})
        assert r.status_code == 403 and _code(r) == "SELF_APPROVAL_FORBIDDEN"
        assert _status(cid) == "certificate_issued"
        # 정상: 다른 사람(admin)이 승인하면 철회된다
        r = c.post("/approvals/%s/approve" % aid, headers=_adm(c), json={})
        assert r.status_code == 200, r.text
        assert _status(cid) == "revoked"
