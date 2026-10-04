"""P2 — UI 개편 백엔드 기반(N1).

8단계 진행(step8) + hold 플래그 + 단계 담당(owner)을 case.status 에서 계산한다.
현행 상태머신(state_machine.TRANSITIONS)은 그대로 두고 '표시용 8단계'만 도출한다.

v4 연결 4번(재정의) — 상태머신 실제 흐름 기준. owner = '지금 다음 전이를 일으킬 수 있는 쪽'.
(전이 권한은 state_machine.TRANSITION_ROLES·전용 엔드포인트 기준. v4 업무 큐가 이 값으로 거른다.)

| status                         | step              | owner      | 근거(다음 동작)                                  |
|--------------------------------|-------------------|------------|-------------------------------------------------|
| onboarding·application_draft   | 0 접수             | client     | 신청서 작성                                      |
| ai_pre_assessment_ready·running| 1 사전심사          | client     | AI 사전심사 실행                                  |
| pathway_determination          | 1                 | consultant | 경로 확정(pathway/confirm)                        |
| supplementation_required       | 1 (hold)          | client     | 보완 재제출(preassess/resubmit)                   |
| supplementation_submitted      | 1                 | auditor    | 사전심사 검토 ready → consultant_review           |
| consultant_review              | 1 / 계약 있으면 2   | consultant | 계약·모의심사 요청(→document_pre_audit_requested)  |
| document_pre_audit_requested   | 미결제 인보이스 2   | client     | 결제(guard_payment)                              |
|                                | 결제 정리 3 모의심사 | auditor    | 모의심사 판정(mock-audit/decision)                |
| document_pre_audit_in_review   | 3                 | auditor    | 모의심사 판정 pass → approved                     |
| document_pre_audit_approved    | 4 현장심사          | ops        | LPH 배정 단계 진행(operator·fatwa_liaison)        |
| lph_assignment                 | 4                 | auditor    | 현장심사 일정 확정 → onsite_audit_scheduled        |
| onsite_audit_scheduled·in_prog.| 4                 | auditor    | 체크리스트·현장심사 종료/시정요구                   |
| corrective_action_required     | 4 (hold)          | client     | CAR 제출                                         |
| corrective_action_submitted    | 4                 | auditor    | CAR 검토 후 종료/재요구                            |
| audit_closed·hpas_eval_ready   | 4                 | auditor    | 보고서 서명·HPAS 평가                              |
| final_package_preparation      | 4                 | auditor    | 오디터 최종 화면에서 파트와 상정(send-fatwa)         |
| self_declare_eligible·sjph_lite| 1                 | client     | SJPH lite 작성                                   |
| pendamping_verification        | 1                 | pendamping | 동반자 검증(pendamping/verify)                    |
| self_declaration_submitted     | 5 샤리아 심의        | ops        | 위원회 회부(operator 전용 전이)                    |
| committee_verification         | 5 / 승인 후 6       | sharia/ops | KFPH 판정 → (승인 시) 인증서 발급                  |
| fatwa_review                   | 5 (부적합 hold)     | sharia     | 파트와 심의                                       |
| fatwa_approved                 | 6 인증서 발급        | ops        | 인증서 발급                                       |
| certificate_issued             | SiHalal 미등록 6    | ops        | SiHalal 제출·공식번호 import                      |
|                                | SiHalal 등록 7      | ops        | 사후관리 전환                                     |
| post_certification_monitoring  | 7 (done)          | client     | 완료                                             |
| change_impact·renewal_prep.    | 7 (진행 중)         | client     | 변경영향·갱신 — 완료 아님                          |

결제 판정은 state_machine.INVOICE_SETTLED(has_unpaid_invoice)와 같은 기준을 쓴다.

주의: 이 모듈은 읽기 전용 계산만 한다(쓰기·스키마 변경 없음). main.py 가 import 해서 쓴다.
main._STAGE_OWNER(워크플로 모니터)는 이 _OWNER 를 그대로 쓴다(단일 출처).
"""
from . import models
from . import state_machine as sm

# 표시용 8단계 라벨 (설계서 4장)
STEP8_LABELS = ["접수", "사전심사", "계약", "모의심사", "현장심사",
                "샤리아 심의", "인증서 발급", "SiHalal 등록"]

# case.status → 8단계 기본 매핑. 계약(2)·인증서(6/7)는 신호로 보정한다(compute_step8).
_BASE_STEP = {
    "onboarding": 0, "application_draft": 0,
    "ai_pre_assessment_ready": 1, "ai_pre_assessment_running": 1,
    "pathway_determination": 1, "supplementation_required": 1,
    "supplementation_submitted": 1, "consultant_review": 1,   # 계약 있으면 2(보정)
    # 모의심사 = document_pre_audit_* (main._MOCK_NEXT·mock-audit 엔드포인트)
    "document_pre_audit_requested": 3,                          # 미결제면 2(보정)
    "document_pre_audit_in_review": 3,
    "document_pre_audit_approved": 4,                            # 다음 = lph_assignment
    # 자기선언 경로 — 제출 전까지 사전심사, 제출·위원회는 심의(5)
    "self_declare_eligible": 1, "sjph_lite_prepared": 1,
    "pendamping_verification": 1, "self_declaration_submitted": 5,
    "lph_assignment": 4, "onsite_audit_scheduled": 4, "onsite_audit_in_progress": 4,
    "corrective_action_required": 4, "corrective_action_submitted": 4,
    "audit_closed": 4, "hpas_evaluation_ready": 4, "final_package_preparation": 4,
    "fatwa_review": 5, "committee_verification": 5,             # 위원회 승인 후 6(보정)
    "fatwa_approved": 6, "certificate_issued": 6,               # SiHalal 등록되면 7(보정)
    "post_certification_monitoring": 7, "change_impact": 7, "renewal_preparation": 7,
}

# 단계 담당. 값: client|consultant|auditor|sharia|ops|pendamping
_OWNER = {
    "onboarding": "client", "application_draft": "client",
    "ai_pre_assessment_ready": "client", "ai_pre_assessment_running": "client",
    "pathway_determination": "consultant",
    "self_declare_eligible": "client", "sjph_lite_prepared": "client",
    "pendamping_verification": "pendamping", "self_declaration_submitted": "ops",
    "committee_verification": "sharia",
    "supplementation_required": "client", "supplementation_submitted": "auditor",
    "consultant_review": "consultant",
    "document_pre_audit_requested": "auditor", "document_pre_audit_in_review": "auditor",
    "document_pre_audit_approved": "ops", "lph_assignment": "auditor",
    "onsite_audit_scheduled": "auditor", "onsite_audit_in_progress": "auditor",
    "corrective_action_required": "client", "corrective_action_submitted": "auditor",
    "audit_closed": "auditor", "hpas_evaluation_ready": "auditor",
    "final_package_preparation": "auditor", "fatwa_review": "sharia",
    "fatwa_approved": "ops", "certificate_issued": "ops",
    "post_certification_monitoring": "client", "change_impact": "client",
    "renewal_preparation": "client",
}

# 로그인 역할(백엔드 8종) → 업무 담당 버킷. 설계서 2.1 + 동반자(pendamping) 분리.
_ROLE_BUCKET = {
    "applicant": "client", "penyelia_halal": "client", "pendamping_pph": "pendamping",
    "client": "client", "consultant": "consultant", "auditor": "auditor",
    "fatwa_liaison": "sharia", "admin": "ops", "operator": "ops",
}

# 완료로 보는 상태 — 사후 모니터링만. 변경영향·갱신은 진행 중 업무라 목록에 남긴다.
_DONE_STATES = {"post_certification_monitoring"}


def role_bucket(role):
    return _ROLE_BUCKET.get(role, "ops")


def _payment_cleared(db, case_id):
    """상태머신 guard_payment 와 같은 기준 — INVOICE_SETTLED 밖 인보이스가 없으면 정리됨."""
    return not sm.has_unpaid_invoice(db, case_id)


def _has_contract(db, case_id):
    return db.query(models.Contract).filter_by(case_id=case_id).first() is not None


def _sihalal_registered(db, case_id):
    """SiHalal 등록 = 제출 패키지 접수(document_package_submitted/submitted) 또는 공식번호 import."""
    ie = models.IntegrationEvent
    if (db.query(ie).filter(ie.case_id == case_id,
                            ((ie.event_type == "document_package_submitted") & (ie.status == "submitted"))
                            | (ie.event_type == "certificate_number_imported")).first()):
        return True
    return db.query(models.WorkflowEvent).filter_by(
        case_id=case_id, action="certificate_number_imported").first() is not None


def compute_step8(db, c):
    """case → {step(0-7), step_label, done, hold, owner}. 읽기 전용."""
    st = c.status or "onboarding"
    step = _BASE_STEP.get(st, 0)
    owner = _OWNER.get(st, "ops")
    done = st in _DONE_STATES

    # 계약(2): 전용 상태가 없어 계약·인보이스 신호로 가른다.
    if st == "consultant_review" and _has_contract(db, c.case_id):
        step = 2
    if st == "document_pre_audit_requested" and not _payment_cleared(db, c.case_id):
        step, owner = 2, "client"          # 결제 전엔 모의심사 착수 불가(guard_payment)

    # 자기선언 위원회 승인(fatwa_status=approved) 후엔 발급 대기(6)
    if st == "committee_verification" and c.fatwa_status == "approved":
        step, owner = 6, "ops"

    # 인증서 발급 후: SiHalal 등록되면 7, 아니면 발급 단계(6)에 머문다(등록 업무).
    if st == "certificate_issued" and _sihalal_registered(db, c.case_id):
        step = 7

    # hold(보완 대기) — 설계서 4장: 보완요청·시정요청·샤리아 부적합 재심의
    hold = st in ("supplementation_required", "corrective_action_required")
    if st == "fatwa_review":
        fd = db.query(models.FatwaDecision).filter_by(case_id=c.case_id).first()
        if fd and fd.decision and fd.decision not in ("approved", "conditional"):
            hold = True

    return {"step": step, "step_label": STEP8_LABELS[step],
            "done": done, "hold": hold, "owner": owner}
