"""Dual-Pathway 상태기계 + 가드 — 설계 24.2.6 / 24.11."""
import hashlib
import json
from datetime import datetime
from .models import (CaseApplication, Material, PenyeliaHalal,
                     PendampingAssignment, ExternalIdentity, WorkflowEvent)

# 24.2.6 전이표 (adjacency)
TRANSITIONS = {
    "onboarding": {"application_draft"},
    "application_draft": {"ai_pre_assessment_ready"},
    "ai_pre_assessment_ready": {"ai_pre_assessment_running"},
    "ai_pre_assessment_running": {"pathway_determination"},
    "pathway_determination": {"self_declare_eligible", "supplementation_required", "consultant_review"},
    # 자기선언(SEHATI)
    "self_declare_eligible": {"sjph_lite_prepared"},
    "sjph_lite_prepared": {"pendamping_verification"},
    "pendamping_verification": {"self_declaration_submitted", "consultant_review"},  # 후자=경로전환
    "self_declaration_submitted": {"committee_verification"},
    "committee_verification": {"certificate_issued"},
    # 정규(Reguler)
    "supplementation_required": {"supplementation_submitted"},
    "supplementation_submitted": {"consultant_review"},
    "consultant_review": {"document_pre_audit_requested"},
    "document_pre_audit_requested": {"document_pre_audit_in_review"},
    "document_pre_audit_in_review": {"document_pre_audit_approved"},
    "document_pre_audit_approved": {"lph_assignment"},
    "lph_assignment": {"onsite_audit_scheduled"},
    "onsite_audit_scheduled": {"onsite_audit_in_progress"},
    "onsite_audit_in_progress": {"corrective_action_required", "audit_closed"},
    "corrective_action_required": {"corrective_action_submitted"},
    "corrective_action_submitted": {"audit_closed", "corrective_action_required"},
    "audit_closed": {"hpas_evaluation_ready"},
    "hpas_evaluation_ready": {"final_package_preparation"},
    "final_package_preparation": {"fatwa_review"},
    "fatwa_review": {"fatwa_approved", "supplementation_required"},
    "fatwa_approved": {"certificate_issued"},
    # 재수렴
    "certificate_issued": {"post_certification_monitoring"},
    "post_certification_monitoring": {"renewal_preparation", "change_impact"},
    "change_impact": {"renewal_preparation"},
    "renewal_preparation": set(),
}


def allowed(frm, to):
    return to in TRANSITIONS.get(frm, set())


# ---- 전이 권한 (P0-1) — /transition 우회로 승인·발급 무력화 방지 ----
# raw /transition으로 진입 금지(전용 엔드포인트만): 최종승인·발급
PROTECTED_STATES = {"fatwa_approved", "certificate_issued"}
# 진입(to_state)에 필요한 역할. admin은 항상 허용. 미정의 상태는 DEFAULT.
TRANSITION_ROLES = {
    "committee_verification": {"operator"},                     # SEHATI ketetapan = 최종 결제자
    "final_package_preparation": {"fatwa_liaison", "operator"},
    "fatwa_review": {"fatwa_liaison", "operator"},
    "lph_assignment": {"fatwa_liaison", "operator"},
    "onsite_audit_scheduled": {"auditor", "fatwa_liaison", "operator"},
    "onsite_audit_in_progress": {"auditor", "operator"},
    "corrective_action_required": {"auditor", "operator"},
    "corrective_action_submitted": {"auditor", "consultant", "operator"},
    "audit_closed": {"auditor", "operator"},                    # 오디터 현장심사 완료
    "hpas_evaluation_ready": {"auditor", "fatwa_liaison", "operator"},
}
# 그 외 상태(초기 워크플로 등)는 신청·컨설턴트 등 스태프 진행 허용(applicant 포함)
DEFAULT_TRANSITION_ROLES = {"applicant", "consultant", "penyelia_halal",
                            "pendamping_pph", "auditor", "fatwa_liaison", "operator"}


def transition_roles(to_state):
    return TRANSITION_ROLES.get(to_state, DEFAULT_TRANSITION_ROLES)


# ---- 헬퍼 질의 ----
def has_active_penyelia(db, org_id):
    return db.query(PenyeliaHalal).filter_by(org_id=org_id, status="active").count() > 0


def materials(db, case_id):
    return db.query(Material).filter_by(case_id=case_id).all()


def critical_materials(db, case_id):
    out = []
    for m in materials(db, case_id):
        if m.screen_status == "haram":
            out.append(m)
        elif m.screen_status == "mushbooh" and m.screen_severity == "high" and not m.evidence_provided:
            out.append(m)
    return out


def evidence_complete(db, case_id):
    return all(m.evidence_provided for m in materials(db, case_id)
               if m.screen_status == "mushbooh")


def sihalal_verified(db, case_id):
    return db.query(ExternalIdentity).filter_by(
        case_id=case_id, verification_status="verified", identifier_match=True).count() > 0


def pendamping_decision(db, case_id):
    pa = (db.query(PendampingAssignment).filter_by(case_id=case_id)
          .order_by(PendampingAssignment.assignment_id.desc()).first())
    return pa.decision if pa else None


# ---- 24.11 가드 ----
def guard_pathway_selfdeclare(db, case):
    g = []
    if case.risk_category != "low":
        g.append({"code": "RISK_NOT_LOW"})
    if not case.is_msme:
        g.append({"code": "NOT_MSME"})
    if len(critical_materials(db, case.case_id)) > 0:
        g.append({"code": "HAS_CRITICAL_MATERIAL"})
    if not evidence_complete(db, case.case_id):
        g.append({"code": "MATERIAL_EVIDENCE_INCOMPLETE"})
    return g


def guard_selfdeclare_submit(db, case):
    g = []
    if pendamping_decision(db, case.case_id) != "verified":
        g.append({"code": "PENDAMPING_NOT_VERIFIED"})
    if not sihalal_verified(db, case.case_id):
        g.append({"code": "SIHALAL_IDENTITY_UNVERIFIED"})
    if not evidence_complete(db, case.case_id):
        g.append({"code": "MATERIAL_EVIDENCE_INCOMPLETE"})
    if not has_active_penyelia(db, case.org_id):
        g.append({"code": "PENYELIA_HALAL_MISSING"})
    return g


def guard_reguler_submit(db, case):
    g = []
    if not sihalal_verified(db, case.case_id):
        g.append({"code": "SIHALAL_IDENTITY_UNVERIFIED"})
    return g


def guard_certificate_issue(db, case):
    g = []
    if case.fatwa_status != "approved":
        g.append({"code": "FATWA_NOT_APPROVED"})
    if not case.scope_frozen:
        g.append({"code": "SCOPE_NOT_FROZEN"})
    return g


def open_major_nc(db, case_id):
    from .models import AuditFinding
    return db.query(AuditFinding).filter_by(case_id=case_id, status="open", severity="major").count()


def guard_final_package(db, case):
    g = []
    if open_major_nc(db, case.case_id) > 0:
        g.append({"code": "UNRESOLVED_MAJOR_NC"})
    return g


# 인보이스 종결 상태 — 이 외(waiting_payment·need_verification·unpaid[legacy] 등)는 전부 미결제로 간주.
# 과거 status=="unpaid"만 검사해 waiting_payment 기본값이 게이트를 통과하던 결함(2026-07-10 전수검사) 수정.
INVOICE_SETTLED = {"paid", "refunded", "expired", "cancelled"}


def has_unpaid_invoice(db, case_id):
    from .models import Invoice
    return db.query(Invoice).filter(Invoice.case_id == case_id,
                                    ~Invoice.status.in_(INVOICE_SETTLED)).count() > 0


def guard_payment(db, case):
    g = []
    if has_unpaid_invoice(db, case.case_id):
        g.append({"code": "UNPAID_INVOICE"})
    return g


GUARDS = {
    "final_package_preparation": guard_final_package,
    "document_pre_audit_in_review": guard_payment,
    "self_declare_eligible": guard_pathway_selfdeclare,
    "self_declaration_submitted": guard_selfdeclare_submit,
    "lph_assignment": guard_reguler_submit,
    "certificate_issued": guard_certificate_issue,
}


def evaluate_blocking(db, case):
    b = []
    if not has_active_penyelia(db, case.org_id):
        b.append({"code": "PENYELIA_HALAL_MISSING"})
    for m in critical_materials(db, case.case_id):
        code = "HARAM_INGREDIENT" if m.screen_status == "haram" else "CRITICAL_MATERIAL_NO_EVIDENCE"
        b.append({"code": code, "target": m.name})
    if not sihalal_verified(db, case.case_id):
        b.append({"code": "SIHALAL_IDENTITY_UNVERIFIED"})
    return b


def can_transition(db, case, to_state):
    if not allowed(case.status, to_state):
        return False, [{"code": "INVALID_TRANSITION", "from": case.status, "to": to_state}]
    guard = GUARDS.get(to_state)
    blockers = guard(db, case) if guard else []
    return (len(blockers) == 0), blockers


def apply_side_effects(case, to_state):
    if to_state == "self_declare_eligible":
        case.pathway = "self_declare"
    # P1: reguler 전용 상태로의 전이는 pathway를 reguler로 확정(self_declare→reguler 전환 포함)
    if to_state in ("supplementation_required", "consultant_review") and case.pathway in ("undetermined", "self_declare"):
        case.pathway = "reguler"
    if to_state == "committee_verification":            # 자기선언 ketetapan (operator 전용 전이)
        case.fatwa_status = "approved"
        case.scope_frozen = True
    if to_state == "final_package_preparation":
        case.scope_frozen = True
    # NOTE(P0-1): fatwa_approved 자동 approved 제거 — 최종승인은 /fatwa/final-approve(operator)만 수행


def assess_pathway(db, case):
    mats = materials(db, case.case_id)
    crit = critical_materials(db, case.case_id)
    has_haram = any(m.screen_status == "haram" for m in mats)
    has_mush_high = any(m.screen_status == "mushbooh" and m.screen_severity == "high"
                        and not m.evidence_provided for m in mats)
    has_mush_med = any(m.screen_status == "mushbooh" and m.screen_severity == "medium" for m in mats)
    risk = "high" if (has_haram or has_mush_high) else ("medium" if has_mush_med else "low")
    ev = evidence_complete(db, case.case_id)
    suggested = "self_declare" if (risk == "low" and case.is_msme and not crit and ev) else "reguler"
    return {"suggested_pathway": suggested, "risk_category": risk, "is_msme": bool(case.is_msme),
            "critical_ingredient_count": len(crit), "evidence_complete": ev,
            "blockers": [{"code": "HAS_CRITICAL_MATERIAL", "target": m.name} for m in crit]}


# ---- 감사 이벤트 (해시 체인, B.5) ----
def _last_hash(db, case_id):
    ev = (db.query(WorkflowEvent).filter_by(case_id=case_id)
          .order_by(WorkflowEvent.created_at.desc(), WorkflowEvent.event_id.desc()).first())
    return ev.row_hash if ev else ""


def record_event(db, case, frm, to, action, actor_type="system", actor_id=None, payload=None):
    prev = _last_hash(db, case.case_id)
    body = json.dumps({"case": case.case_id, "from": frm, "to": to, "action": action,
                       "payload": payload or {}}, sort_keys=True, ensure_ascii=False)
    row_hash = hashlib.sha256((prev + body).encode("utf-8")).hexdigest()
    ev = WorkflowEvent(case_id=case.case_id, from_status=frm, to_status=to, action=action,
                       actor_type=actor_type, actor_id=actor_id, payload=payload,
                       prev_hash=prev, row_hash=row_hash, created_at=datetime.utcnow())
    db.add(ev)
    return ev