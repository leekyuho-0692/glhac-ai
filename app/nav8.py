"""P2 — UI 개편 백엔드 기반(N1).

8단계 진행(step8) + hold 플래그 + 단계 담당(owner)을 case.status 에서 계산한다.
설계서 4장 매핑. 현행 상태머신(30여 상태)은 그대로 두고 '표시용 8단계'만 도출한다.

주의: 이 모듈은 읽기 전용 계산만 한다(쓰기·스키마 변경 없음). main.py 가 import 해서 쓴다.
순환 import 를 피하려고 _STAGE_OWNER 를 여기 복제한다(안정적인 매핑).
"""
from . import models

# 표시용 8단계 라벨 (설계서 4장)
STEP8_LABELS = ["접수", "사전심사", "계약", "모의심사", "현장심사",
                "샤리아 심의", "인증서 발급", "SiHalal 등록"]

# case.status → 8단계 기본 매핑. 계약(2)·모의심사(3)는 전용 상태가 없어 신호로 보정한다(아래).
_BASE_STEP = {
    "onboarding": 0, "application_draft": 0,
    "ai_pre_assessment_ready": 1, "ai_pre_assessment_running": 1,
    "pathway_determination": 1, "supplementation_required": 1,
    "supplementation_submitted": 1, "consultant_review": 1,
    "document_pre_audit_requested": 1, "document_pre_audit_in_review": 1,
    "document_pre_audit_approved": 1,   # 계약/모의 보정 대상(아래)
    # 자기선언 경로(#3 유지) — 사전심사 영역으로 표시, 심의 전까지 1
    "self_declare_eligible": 1, "sjph_lite_prepared": 1,
    "pendamping_verification": 1, "self_declaration_submitted": 1,
    "lph_assignment": 4, "onsite_audit_scheduled": 4, "onsite_audit_in_progress": 4,
    "corrective_action_required": 4, "corrective_action_submitted": 4,
    "audit_closed": 4, "hpas_evaluation_ready": 4, "final_package_preparation": 4,
    "fatwa_review": 5, "fatwa_approved": 5, "committee_verification": 5,
    "certificate_issued": 6,   # SiHalal 보정(아래)
    "post_certification_monitoring": 7, "change_impact": 7, "renewal_preparation": 7,
}

# 단계 담당(설계서 4장·_STAGE_OWNER 복제). 값: client|consultant|auditor|sharia|ops
_OWNER = {
    "onboarding": "client", "application_draft": "client",
    "ai_pre_assessment_ready": "client", "ai_pre_assessment_running": "client",
    "pathway_determination": "consultant",
    "self_declare_eligible": "client", "sjph_lite_prepared": "client",
    "pendamping_verification": "consultant", "self_declaration_submitted": "client",
    "committee_verification": "ops",
    "supplementation_required": "client", "supplementation_submitted": "consultant",
    "consultant_review": "consultant",
    "document_pre_audit_requested": "ops", "document_pre_audit_in_review": "auditor",
    "document_pre_audit_approved": "auditor", "lph_assignment": "ops",
    "onsite_audit_scheduled": "auditor", "onsite_audit_in_progress": "auditor",
    "corrective_action_required": "client", "corrective_action_submitted": "auditor",
    "audit_closed": "auditor", "hpas_evaluation_ready": "auditor",
    "final_package_preparation": "ops", "fatwa_review": "sharia",
    "fatwa_approved": "ops", "certificate_issued": "sharia",
    "post_certification_monitoring": "client", "change_impact": "client",
    "renewal_preparation": "client",
}

# 로그인 역할(백엔드 8종) → 업무 담당 버킷(5역할). 설계서 2.1.
_ROLE_BUCKET = {
    "applicant": "client", "penyelia_halal": "client", "pendamping_pph": "client",
    "client": "client", "consultant": "consultant", "auditor": "auditor",
    "fatwa_liaison": "sharia", "admin": "ops", "operator": "ops",
}

# 완료(SiHalal 등록까지) 로 보는 상태
_DONE_STATES = {"post_certification_monitoring", "change_impact", "renewal_preparation"}


def role_bucket(role):
    return _ROLE_BUCKET.get(role, "ops")


def _paid(db, case_id):
    return (db.query(models.Invoice)
            .filter(models.Invoice.case_id == case_id,
                    models.Invoice.status.in_(("paid", "settlement", "settled")))
            .first() is not None)


def compute_step8(db, c):
    """case → {step(0-7), step_label, done, hold, owner}. 읽기 전용."""
    st = c.status or "onboarding"
    step = _BASE_STEP.get(st, 0)
    done = st in _DONE_STATES

    # 계약(2)·모의심사(3) 보정: 사전심사 승인 후 전용 상태가 없어 결제 신호로 가른다
    # (#5 확정: 결제 게이트는 계약 후). 미결제=계약, 결제완료=모의심사 진입.
    if st == "document_pre_audit_approved":
        step = 3 if _paid(db, c.case_id) else 2

    # 인증서 발급 후: SiHalal 등록 단계(7). 사후 모니터링부터 완료로 본다(위 _DONE_STATES).
    if st == "certificate_issued":
        step = 7
        done = False

    # hold(보완 대기) — 설계서 4장: 보완요청·시정요청·샤리아 부적합 재심의
    hold = st in ("supplementation_required", "corrective_action_required")
    if st == "fatwa_review":
        fd = db.query(models.FatwaDecision).filter_by(case_id=c.case_id).first()
        if fd and fd.decision and fd.decision not in ("approved", "conditional"):
            hold = True

    return {"step": step, "step_label": STEP8_LABELS[step],
            "done": done, "hold": hold, "owner": _OWNER.get(st, "ops")}
