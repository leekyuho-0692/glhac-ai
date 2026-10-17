"""v4 연결 5 — 단계별 전용 전이(버튼)가 실제로 상태를 바꾸는지, 권한 밖 역할은 막히는지.

  a) 사전심사 ready → consultant_review / 모의심사 pass → approved(연쇄), reject → 보완, 미결제는 차단
  b) document_pre_audit_approved → lph_assignment (operator) — SiHalal 가드 사유 노출
  c) 현장심사 종료·시정조치 요구(auditor), CAR 수용 후 종료
  d) HPAS 5요소 good → final_package_preparation 자동 전진(auditor)
  e) final_package_preparation 이 오디터 큐(owner=auditor)에 남는다
  f) self_declaration_submitted → committee_verification (operator)
  g) 기업(applicant) CAR 제출 → corrective_action_submitted 자동 전진

실행: <venv>/bin/python -m pytest tests/test_v4_flow_actions.py -q
"""
import logging
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///" + tempfile.mktemp(suffix=".db"))
os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_SECRET", "test-" + "x" * 36)
logging.disable(logging.CRITICAL)

from fastapi.testclient import TestClient  # noqa: E402

from app import models  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402


def _h(t):
    return {"Authorization": "Bearer " + t}


def _tok(c, u, p="pw"):
    return c.post("/auth/login", json={"username": u, "password": p}).json()["token"]


def _new_company(c):
    """업체 가입 → 자기 조직의 케이스 1건. (case_id, org_id, token)"""
    u = "fa" + uuid.uuid4().hex[:8]
    j = c.post("/auth/register", json={"username": u, "password": "EntPass!123",
                                       "company_name": "업체" + u}).json()
    cases = c.get("/cases", headers=_h(j["token"])).json()
    items = cases if isinstance(cases, list) else cases.get("items") or cases.get("cases")
    return items[0]["case_id"], j["org_id"], j["token"]


def _db_do(fn):
    db = SessionLocal()
    try:
        out = fn(db)
        db.commit()
        return out
    finally:
        db.close()


def _set_status(cid, st):
    _db_do(lambda db: setattr(db.get(models.CaseApplication, cid), "status", st))


def _status(cid):
    return _db_do(lambda db: db.get(models.CaseApplication, cid).status)


def _uid(username):
    return _db_do(lambda db: db.query(models.User).filter_by(username=username).first().user_id)


def _assigned_case(c, status):
    """업체 케이스 + auditor1 배정 + 상태 지정. (cid, org, applicant_token)"""
    cid, org, ent = _new_company(c)
    _set_status(cid, status)
    op = _tok(c, "operator1")
    # Capacity enforcement is tested separately; these scenarios share one auditor.
    assert c.post('/admin/audit-settings', headers=_h(op), json={'max_cases_per_auditor': 100}).status_code == 200
    r = c.post("/ops/cases/%s/assign-auditor" % cid, json={"auditor_id": _uid("auditor1")}, headers=_h(op))
    assert r.status_code == 200, r.text
    return cid, org, ent


def _tr(c, tok, cid, to):
    return c.post(f"/cases/{cid}/transition", json={"to_state": to, "action": "test"}, headers=_h(tok))


def _item(c, tok, cid):
    return next(x for x in c.get("/cases", headers=_h(tok)).json()["items"] if x["case_id"] == cid)


def _checked(c, response, cid):
    """A maker submits; the assigned co-auditor must approve before execution."""
    assert response.status_code == 200, response.text
    pending = response.json()
    assert pending['pending'] is True
    approval_id = pending['approval_id']
    maker = _tok(c, 'auditor1')
    assert c.post('/approvals/' + approval_id + '/approve', headers=_h(maker), json={}).status_code == 403
    def co_name(db):
        co = db.query(models.CaseAuditor).filter_by(case_id=cid, role='co').first()
        assert co is not None
        return db.get(models.User, co.user_id).username
    checker = _tok(c, _db_do(co_name))
    approved = c.post('/approvals/' + approval_id + '/approve', headers=_h(checker), json={})
    assert approved.status_code == 200, approved.text
    return approved.json()['result']


# ---------- a) 사전심사 ready · 모의심사 판정 ----------
def test_a_사전심사_ready_가_컨설턴트검토로_전진():
    with TestClient(app) as c:
        cid, _, _ = _assigned_case(c, "supplementation_submitted")
        au = _tok(c, "auditor1")
        it = _item(c, au, cid)
        assert (it["step8"], it["owner"]) == (1, "auditor")          # aud-pre 큐 대상
        r = c.post(f"/cases/{cid}/preassess/review", headers=_h(au),
                   json={"verdict": "ready", "sections": {}, "note": ""})
        assert r.status_code == 200, r.text
        assert _checked(c, r, cid)["transitioned_to"] == "consultant_review"
        assert _status(cid) == "consultant_review"


def test_a_모의심사_pass_는_requested_에서_approved_까지_연쇄():
    with TestClient(app) as c:
        cid, _, ent = _assigned_case(c, "document_pre_audit_requested")
        au = _tok(c, "auditor1")
        it = _item(c, au, cid)
        assert (it["step8"], it["owner"]) == (3, "auditor")          # aud-mock 큐 대상
        # 기업은 판정 불가
        assert c.post(f"/cases/{cid}/mock-audit/decision", json={"result": "pass"},
                      headers=_h(ent)).status_code == 403
        r = c.post(f"/cases/{cid}/mock-audit/decision", json={"result": "pass"}, headers=_h(au))
        assert r.status_code == 200, r.text
        assert _checked(c, r, cid)["status"] == "document_pre_audit_approved"
        assert _status(cid) == "document_pre_audit_approved"
        it = _item(c, au, cid)
        assert (it["step8"], it["owner"]) == (4, "ops")


def test_a_모의심사_reject_는_보완으로_돌아간다():
    with TestClient(app) as c:
        cid, _, _ = _assigned_case(c, "document_pre_audit_in_review")
        au = _tok(c, "auditor1")
        r = c.post(f"/cases/{cid}/mock-audit/decision", json={"result": "reject", "reason": "증빙 부족"},
                   headers=_h(au))
        assert r.status_code == 200, r.text
        assert _checked(c, r, cid)["transitioned_to"] == "supplementation_required"
        assert _status(cid) == "supplementation_required"


def test_a_미결제_인보이스면_모의심사_착수_차단_사유_반환():
    with TestClient(app) as c:
        cid, _, ent = _assigned_case(c, "document_pre_audit_requested")
        _db_do(lambda db: db.add(models.Invoice(case_id=cid, status="waiting_payment", total=1)))
        au = _tok(c, "auditor1")
        it = _item(c, au, cid)
        assert (it["step8"], it["owner"]) == (2, "client")           # 계약·결제 단계, 기업 차례
        response = c.post(f"/cases/{cid}/mock-audit/decision", json={"result": "pass"}, headers=_h(au))
        r = _checked(c, response, cid)
        assert r["transitioned_to"] is None
        assert any(b["code"] == "UNPAID_INVOICE" for b in r["blockers"]), r
        assert _status(cid) == "document_pre_audit_requested"


def test_a_신청자는_보완단계로_스스로_되돌릴_수_없다():
    with TestClient(app) as c:
        cid, _, ent = _assigned_case(c, "document_pre_audit_in_review")
        assert _tr(c, ent, cid, "supplementation_required").status_code == 403


# ---------- b) 모의심사 승인 → LPH 배정 ----------
def test_b_LPH_배정_진행은_운영자만_가드_사유_노출():
    with TestClient(app) as c:
        cid, org, ent = _assigned_case(c, "document_pre_audit_approved")
        au, cons, op = _tok(c, "auditor1"), _tok(c, "consultant1"), _tok(c, "operator1")
        assert _tr(c, au, cid, "lph_assignment").status_code == 403
        assert _tr(c, ent, cid, "lph_assignment").status_code == 403
        assert _tr(c, cons, cid, "lph_assignment").status_code == 403
        r = _tr(c, op, cid, "lph_assignment")
        assert r.status_code == 409, r.text
        assert any(b["code"] == "SIHALAL_IDENTITY_UNVERIFIED" for b in r.json()["detail"]["blockers"])
        _db_do(lambda db: db.add(models.ExternalIdentity(case_id=cid, org_id=org,
                                                         verification_status="verified",
                                                         identifier_match=True)))
        r = _tr(c, op, cid, "lph_assignment")
        assert r.status_code == 200, r.text
        assert _status(cid) == "lph_assignment"
        it = _item(c, au, cid)
        assert (it["step8"], it["owner"]) == (4, "auditor")          # 오디터 일정 큐로


# ---------- c) 현장심사 종료 · 시정조치 ----------
def test_c_현장심사_종료는_오디터만():
    with TestClient(app) as c:
        cid, _, ent = _assigned_case(c, "onsite_audit_in_progress")
        au = _tok(c, "auditor1")
        assert _tr(c, ent, cid, "audit_closed").status_code == 403
        assert _tr(c, au, cid, "audit_closed").status_code == 200
        assert _status(cid) == "audit_closed"


def test_c_시정조치_요구_후_CAR_수용하면_종료_가능():
    with TestClient(app) as c:
        cid, _, ent = _assigned_case(c, "onsite_audit_in_progress")
        au = _tok(c, "auditor1")
        f = c.post(f"/cases/{cid}/findings", json={"finding": "라벨 미흡", "severity": "minor"},
                   headers=_h(au))
        assert f.status_code == 200, f.text
        fid = f.json().get("finding_id") or f.json().get("id")
        assert _tr(c, au, cid, "corrective_action_required").status_code == 200
        # g) 기업 CAR 제출 → corrective_action_submitted 자동 전진(종전엔 applicant 권한 부족으로 실패)
        r = c.post(f"/findings/{fid}/car", json={"description": "라벨 교체"}, headers=_h(ent))
        assert r.status_code == 200, r.text
        assert _status(cid) == "corrective_action_submitted"
        car_id = r.json()["id"]
        # 미수용 CAR 이 있으면 종료 차단(가드)
        r = _tr(c, au, cid, "audit_closed")
        assert r.status_code == 409
        assert any(b["code"] in ("UNRESOLVED_CAR", "OPEN_FINDINGS") for b in r.json()["detail"]["blockers"])
        assert c.patch(f"/car/{car_id}/review", json={"status": "accepted", "note": ""},
                       headers=_h(au)).status_code == 200
        assert _tr(c, au, cid, "audit_closed").status_code == 200
        assert _status(cid) == "audit_closed"


def test_g_penyelia_도_CAR_제출_전이_권한():
    from app import state_machine as sm
    assert {"applicant", "penyelia_halal"} <= sm.transition_roles("corrective_action_submitted")


# ---------- d·e) HPAS → 최종 패키지 → 오디터 최종 화면 ----------
def test_d_HPAS_5요소_good_이면_오디터_판정으로_최종패키지_자동전진():
    with TestClient(app) as c:
        cid, _, ent = _assigned_case(c, "hpas_evaluation_ready")
        au = _tok(c, "auditor1")
        for el in ("commitment", "materials", "process", "product", "monitoring"):
            r = c.post(f"/cases/{cid}/evaluation/verdict", json={"element": el, "verdict": "good"},
                       headers=_h(au))
            assert r.status_code == 200, r.text
            if el != "monitoring":
                assert _status(cid) == "hpas_evaluation_ready"
        assert _status(cid) == "final_package_preparation"
        it = _item(c, au, cid)
        assert (it["step8"], it["owner"]) == (4, "auditor")          # e) 오디터 최종 화면에 남는다


def test_d_최종패키지_전이_권한_신청자_차단_오디터_허용():
    with TestClient(app) as c:
        cid, _, ent = _assigned_case(c, "hpas_evaluation_ready")
        assert _tr(c, ent, cid, "final_package_preparation").status_code == 403
        assert _tr(c, _tok(c, "auditor1"), cid, "final_package_preparation").status_code == 200


def test_e_최종패키지에서_파트와_상정은_게이트_사유를_돌려준다():
    with TestClient(app) as c:
        cid, _, _ = _assigned_case(c, "final_package_preparation")
        r = c.post(f"/cases/{cid}/audit-report/send-fatwa", headers=_h(_tok(c, "auditor1")))
        assert r.status_code == 409 and r.json()["detail"]["code"] == "FATWA_GATE_BLOCKED"


# ---------- f) 자기선언 위원회 회부 ----------
def test_f_자기선언_위원회_회부는_운영자만():
    with TestClient(app) as c:
        cid, _, ent = _assigned_case(c, "self_declaration_submitted")
        op, cons = _tok(c, "operator1"), _tok(c, "consultant1")
        it = _item(c, op, cid)
        assert (it["step8"], it["owner"]) == (5, "ops")
        assert _tr(c, ent, cid, "committee_verification").status_code == 403
        assert _tr(c, cons, cid, "committee_verification").status_code == 403
        assert _tr(c, op, cid, "committee_verification").status_code == 200
        assert _status(cid) == "committee_verification"


# ---------- 프런트: 자유입력 전이 UI 제거 · 전용 버튼 모듈 로드 ----------
def test_자유입력_전이_UI_제거_전용버튼_모듈_로드():
    base = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "static", "v4")
    cca = open(os.path.join(base, "js", "cons-contract-actions.js"), encoding="utf-8").read()
    assert "cca-to" not in cca and "ccaTransition" not in cca
    html = open(os.path.join(base, "index.html"), encoding="utf-8").read()
    assert '<script src="js/flow-actions.js"></script>' in html
    fa = open(os.path.join(base, "js", "flow-actions.js"), encoding="utf-8").read()
    for to in ("lph_assignment", "committee_verification", "audit_closed",
               "corrective_action_required", "final_package_preparation"):
        assert to in fa, to
