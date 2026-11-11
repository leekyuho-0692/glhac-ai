"""Dual-Pathway 상태기계 + 가드 — 설계 24.2.6 / 24.11."""
from datetime import date as _date
import hashlib
import os
import json
from datetime import datetime
from sqlalchemy.orm import object_session
from .db import lock_chain_tail
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
    # 모의심사(document_pre_audit_*) 반려 → 보완 루프(v4 연결 5a: reject 가 갈 곳이 없어 기록만 되던 문제)
    "document_pre_audit_requested": {"document_pre_audit_in_review", "supplementation_required"},
    "document_pre_audit_in_review": {"document_pre_audit_approved", "supplementation_required"},
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


# ---- 케이스 종결(terminal) 상태 — R1-1 ----
# 사유별로 나눈다: withdrawn=업체 철회·포기 / rejected=심사 불합격·인증 불가 판정 /
# revoked=발급 인증서 철회 / cancelled=운영 취소(중복·오등록 등).
# 종결 상태에서 나가는 전이는 없다 — 재개는 새 케이스(갱신 흐름이 새 케이스를 만드는 방식)로 한다.
TERMINAL_STATES = {"withdrawn", "rejected", "revoked", "cancelled"}
# 전용 종결 엔드포인트(POST /cases/{id}/close)로 갈 수 있는 사유(revoked 는 인증서 철회 처리가 자동으로 넣는다)
CLOSE_REASON_STATES = {"withdrawn", "rejected", "cancelled"}
# revoked 는 인증서가 발급된 이후 상태에서만
REVOKE_ENTRY_STATES = {"certificate_issued", "post_certification_monitoring",
                       "change_impact", "renewal_preparation"}
for _frm in list(TRANSITIONS):
    TRANSITIONS[_frm] = set(TRANSITIONS[_frm]) | CLOSE_REASON_STATES
    if _frm in REVOKE_ENTRY_STATES:
        TRANSITIONS[_frm].add("revoked")
for _t in TERMINAL_STATES:
    TRANSITIONS[_t] = set()      # 종결에서 나가는 전이 없음(명시)


def is_terminal(status):
    return status in TERMINAL_STATES


def forward_states(frm):
    """업무 진행상 '다음 단계' 후보 — 종결 상태로의 전이는 제외(종결은 전용 엔드포인트로만)."""
    return TRANSITIONS.get(frm, set()) - TERMINAL_STATES


def allowed(frm, to):
    return to in TRANSITIONS.get(frm, set())


# ---- 전이 권한 (P0-1) — /transition 우회로 승인·발급 무력화 방지 ----
# raw /transition으로 진입 금지(전용 엔드포인트만): 최종승인·발급
PROTECTED_STATES = {"fatwa_approved", "certificate_issued"} | TERMINAL_STATES   # 종결은 POST /cases/{id}/close 로만
# 모의심사 승인에는 준비서류 확인과 2인 확인이 수반된다. 자동 진행과 구별하여
# raw /transition 진입만 금지하고 전용 API가 기존 승인 절차를 수행하게 한다.
DEDICATED_REVIEW_STATES = {"document_pre_audit_approved"}
# 진입(to_state)에 필요한 역할. admin은 항상 허용. 미정의 상태는 DEFAULT.
TRANSITION_ROLES = {
    "committee_verification": {"operator"},                     # SEHATI ketetapan = 최종 결제자
    # auditor: HPAS 5요소 평가를 끝내는 쪽이 오디터라 evaluation/verdict 자동 전진이 조용히 실패하던 것 해소(v4 5d)
    "final_package_preparation": {"auditor", "fatwa_liaison", "operator"},
    "fatwa_review": {"fatwa_liaison", "operator"},
    "lph_assignment": {"fatwa_liaison", "operator"},
    "onsite_audit_scheduled": {"auditor", "fatwa_liaison", "operator"},
    "onsite_audit_in_progress": {"auditor", "operator"},
    "corrective_action_required": {"auditor", "operator"},
    # applicant·penyelia_halal: CAR 제출은 기업의 행위(POST /findings/{id}/car) — 자동 전진이 권한 부족으로 실패하던 것 해소(v4 5g)
    "corrective_action_submitted": {"auditor", "consultant", "operator", "applicant", "penyelia_halal"},
    # 보완 요구는 검토자 판단 — 신청자가 모의심사·심의 단계에서 스스로 되돌리지 못하게(위 반려 루프 추가에 따른 게이트)
    "supplementation_required": {"consultant", "auditor", "fatwa_liaison", "operator"},
    "audit_closed": {"auditor", "operator"},                    # 오디터 현장심사 완료
    "hpas_evaluation_ready": {"auditor", "fatwa_liaison", "operator"},
    # 모의심사 진입·진행·승인은 검토자 판단 — 신청자가 raw 전이로 컨설턴트 검토·정식 접수·
    # 문서심사를 건너뛰던 결함(감사 K4). 결제확정 게이트(_on_invoice_paid)는 역할 검사를 거치지 않는다.
    "document_pre_audit_requested": {"consultant", "operator"},
    "document_pre_audit_in_review": {"auditor", "consultant", "operator"},
    "document_pre_audit_approved": {"auditor", "operator"},
}
# 그 외 상태(초기 워크플로 등)는 신청·컨설턴트 등 스태프 진행 허용(applicant 포함)
DEFAULT_TRANSITION_ROLES = {"applicant", "consultant", "penyelia_halal",
                            "pendamping_pph", "auditor", "fatwa_liaison", "operator"}


def transition_roles(to_state):
    return TRANSITION_ROLES.get(to_state, DEFAULT_TRANSITION_ROLES)


# ---- 헬퍼 질의 ----
def penyelia_state(db, org_id):
    """(있는가, 자격이 유효한가) — 자격 만료를 '없음'과 구분해 말한다.

    종전에는 active 이기만 하면 통과라, 교육 자격이 만료된 감독자로도 신청이 진행됐다.
    감독자 자격은 SJPH 의 전제라 만료된 채로 인증서까지 가면 그 인증이 흔들린다."""
    rows = db.query(PenyeliaHalal).filter_by(org_id=org_id, status="active").all()
    if not rows:
        return False, False
    today = _date.today()
    valid = any(p.cert_expiry is None or p.cert_expiry >= today for p in rows)
    return True, valid


def has_active_penyelia(db, org_id):
    have, valid = penyelia_state(db, org_id)
    return have and valid


def materials(db, case_id):
    return db.query(Material).filter_by(case_id=case_id).all()


REVIEWER_ROLES = {"consultant", "auditor", "fatwa_liaison", "operator", "admin"}


def lock_scope(db, case):
    """추출 대기 중 동결된 경우도 최신 잠금을 확인한다. 커밋까지 쓰기와 동결을 직렬화."""
    lock_chain_tail(db, "scope:" + case.case_id)
    db.refresh(case, ["scope_frozen"])


def material_identity(m):
    return {key: getattr(m, key) for key in (
        "material_id", "name", "e_number", "mat_type", "source", "source_known",
        "supplier", "manufacturer", "origin", "cert_no", "note")}


def scope_snapshot(db, case):
    from .models import Product, ProductMaterial
    db.flush()
    return {"version": 1, "scheme": case.scheme or "product",
            "logistics_scope": sorted(case.logistics_scope or []),
            "products": [{k: getattr(p, k) for k in (
                "product_id", "name", "category", "description", "registration_type")}
                for p in db.query(Product).filter_by(case_id=case.case_id).order_by(Product.product_id)],
            "materials": [material_identity(m) for m in
                          db.query(Material).filter_by(case_id=case.case_id).order_by(Material.material_id)],
            "links": sorted([[v.product_id, v.material_id] for v in
                             db.query(ProductMaterial).filter_by(case_id=case.case_id)])}


def freeze_scope(db, case, actor_type="system", actor_id=None):
    lock_scope(db, case)
    if not case.scope_frozen:
        snapshot = scope_snapshot(db, case)
        case.scope_frozen = True
        record_event(db, case, case.status, case.status, "scope.freeze", actor_type, actor_id,
                     {"snapshot": snapshot})


def frozen_scope(db, case):
    row = (db.query(WorkflowEvent).filter_by(case_id=case.case_id, action="scope.freeze")
           .order_by(WorkflowEvent.created_at.desc(), WorkflowEvent.event_id.desc()).first())
    return (row.payload or {}).get("snapshot") if row else None


def approved_material_evidence(db, material, today=None):
    """발급 요청/승인 공통: 현 원료·최신 문서·현재 유효기간으로 승인 증빙을 재검증."""
    from .models import DocumentAsset, MaterialEvidenceLink, CertificateScopeItem, normalize_material_name
    from .cert_verify import _expiry_of, normalize
    from .intake import certificate_invalid_marker
    import re
    import unicodedata
    today = today or _date.today()
    linked = [v.document_id for v in db.query(MaterialEvidenceLink).filter_by(
        case_id=material.case_id, material_id=material.material_id)]
    all_docs = db.query(DocumentAsset).filter_by(case_id=material.case_id).all()
    docs = [d for d in all_docs if d.material_id == material.material_id or d.document_id in linked]
    if not docs:
        return False
    events = db.query(WorkflowEvent).filter(
        WorkflowEvent.case_id == material.case_id,
        WorkflowEvent.action.in_(("documents.review", "documents.reprocess"))).order_by(
            WorkflowEvent.created_at.desc(), WorkflowEvent.event_id.desc()).all()
    latest_reviews, reprocessed = {}, {}
    for ev in events:
        did = (ev.payload or {}).get("document_id")
        target = latest_reviews if ev.action == "documents.review" else reprocessed
        target.setdefault(did, ev)
    def filename(d):
        return unicodedata.normalize("NFC", d.filename or "").casefold()
    def same_version_family(left, right):
        return filename(left) == filename(right) or (left.doc_type == right.doc_type and
            (left.fields or {}).get("cert_no") and
            normalize(left.fields["cert_no"]) == normalize((right.fields or {}).get("cert_no")))
    identity = material_identity(material)
    for d in docs:
        fields = d.fields or {}
        if (d.review_status != "approved" or not d.content_b64 or fields.get("_counts_as_evidence") is False
                or fields.get("_ocr_error") or fields.get("_cert_not_valid")
                or certificate_invalid_marker(d.filename, d.text_full or d.text_excerpt)):
            continue
        expiry = _expiry_of({"fields": fields})
        # 인증서는 만료일이 읽혀야 한다. 날짜를 갖는 다른 증빙도 같은 현재시점 검사를 적용한다.
        if ((d.doc_type == "halal_certificate" or fields.get("expiry_date")) and expiry is None
                or expiry is not None and expiry < today):
            continue
        if any(other.document_id != d.document_id and same_version_family(d, other)
               and other.created_at >= d.created_at for other in all_docs):
            continue
        scope_matches = True
        for names in (fields.get("material_names"), fields.get("scope")):
            if isinstance(names, str):
                names = re.split(r"[,;|\n]", names)
            if names and (not isinstance(names, list) or normalize_material_name(material.name) not in
                          {normalize_material_name(n) for n in names if isinstance(n, str)}):
                scope_matches = False
        if not scope_matches:
            continue
        scopes = db.query(CertificateScopeItem).filter_by(case_id=material.case_id, document_id=d.document_id).all()
        if scopes and not any(s.scope_type == "INGREDIENT" and
                              normalize_material_name(s.scope_value) == normalize_material_name(material.name)
                              for s in scopes):
            continue
        ev = latest_reviews.get(d.document_id)
        if ev is None:
            continue
        changed = reprocessed.get(d.document_id)
        if changed and changed.created_at >= ev.created_at:
            continue
        payload = ev.payload or {}
        if (ev.actor_type in REVIEWER_ROLES and payload.get("review_status") == "approved"
                and identity in payload.get("material_scope", [])):
            return True
    return False


def _has_evidence(m):
    db = object_session(m)
    return approved_material_evidence(db, m) if db is not None else False


def material_risk(m):
    from . import screening
    # 신고 인증번호나 과거 CLEARED 저장값으로 고위험 원료가 숨지 않도록 원래 위험을 재계산.
    return screening.screen_merged(m.name, m.e_number, m.source, None, False,
                                   m.source_known is not False, m.note or "")


def pending_extractions(db, case_id):
    from . import intake
    from .models import DocumentAsset, Product, normalize_material_name as norm
    c = db.get(CaseApplication, case_id)
    have = {"product_names": {norm(p.name) for p in db.query(Product).filter_by(case_id=case_id)},
            "material_names": {norm(m.name) for m in materials(db, case_id)}}
    handled = set()
    for ev in db.query(WorkflowEvent).filter_by(case_id=case_id, action="extraction.review"):
        if ev.actor_type in REVIEWER_ROLES:
            for item in (ev.payload or {}).get("items", []):
                handled.add((item["kind"], norm(item["value"])))
    out, seen = [], set()
    for doc in db.query(DocumentAsset).filter_by(case_id=case_id):
        fields = doc.fields or {}
        scope_fields = {k: fields[k] for k in ("product_names", "material_names", "_sources") if k in fields}
        agg = intake.aggregate_fields([{"doc_type": doc.doc_type, "fields": scope_fields}])
        candidates = list(agg.get("pending") or []) + list(fields.get("_scope_pending") or [])
        # 동결 중 들어온 구조 추출도 확인대기다. 저장 이전의 구 문서 역시 빠뜨리지 않는다.
        if c and c.scope_frozen:
            for bucket, kind in (("products", "product_names"), ("materials", "material_names")):
                candidates += [{"kind": kind, "value": name, "reason": "scope_frozen"}
                               for name in agg.get(bucket, [])]
        for item in candidates:
            key = (item["kind"], norm(item["value"]))
            if key in seen or key in handled or key[1] in have.get(item["kind"], set()):
                continue
            seen.add(key)
            out.append({**item, "document_id": doc.document_id, "filename": doc.filename,
                        "requires_scope_unlock": bool(c and c.scope_frozen)})
    return out


def material_safety_blockers(db, case):
    from . import screening
    haram, unresolved = [], []
    for m in materials(db, case.case_id):
        risk = material_risk(m)
        if risk["status"] == "haram" or m.screen_status == "haram" or m.screen_result == "BLOCK":
            haram.append(m.name)
        elif (risk["severity"] == "high" or m.screen_severity == "high" or m.v1_risk == "high") and not _has_evidence(m):
            unresolved.append(m.name)
    pending = []
    for item in pending_extractions(db, case.case_id):
        if item["kind"] == "material_names":
            risk = screening.screen_merged(item["value"])
            if risk["status"] == "haram" or risk["severity"] == "high":
                pending.append(item["value"])
    return ([{"code": "HARAM_MATERIAL", "materials": haram}] if haram else []) + (
        [{"code": "CRITICAL_MATERIAL_NO_EVIDENCE", "materials": unresolved}] if unresolved else []) + (
        [{"code": "PENDING_CRITICAL_MATERIAL", "materials": pending}] if pending else [])


def critical_materials(db, case_id):
    out = []
    for m in materials(db, case_id):
        risk = material_risk(m)
        if risk["status"] == "haram" or m.screen_status == "haram":
            out.append(m)
        elif risk["severity"] == "high" and not _has_evidence(m):
            out.append(m)
    return out


def evidence_complete(db, case_id):
    return all(_has_evidence(m) for m in materials(db, case_id)
               if m.screen_status == "mushbooh" or material_risk(m)["severity"] == "high")


def sihalal_verified(db, case_id):
    return db.query(ExternalIdentity).filter_by(
        case_id=case_id, verification_status="verified", identifier_match=True).count() > 0


def pendamping_decision(db, case_id):
    pa = (db.query(PendampingAssignment).filter_by(case_id=case_id)
          .order_by(PendampingAssignment.assignment_id.desc()).first())
    return pa.decision if pa else None


# ---- 24.11 가드 ----
# BPJPH 자기선언 정량 기준 (Decision 146/2025)
SELF_DECLARE_REVENUE_LIMIT = 15_000_000_000   # 연매출 Rp 15 billion
SELF_DECLARE_MAX_FACILITIES = 1               # 공장 최대 1개
SELF_DECLARE_MAX_OUTLETS = 1                  # 매장(영업장) 최대 1개


def guard_pathway_selfdeclare(db, case):
    g = []
    if case.risk_category != "low":
        g.append({"code": "RISK_NOT_LOW"})
    if not case.is_msme:
        g.append({"code": "NOT_MSME"})
    # BPJPH: 연매출 ≤ Rp15B. **연매출·매장 수는 선택 입력이라 미입력을 차단하지 않는다.**
    # 입력했는데 상한을 넘으면 그때는 판정 가능한 사실이므로 막는다.
    # 미입력이라 확인하지 못했다는 사실 자체는 assess_pathway 의 unverified 로 남긴다 —
    # 통과시키되 '무엇을 근거 없이 통과시켰는지'는 보이게 한다.
    if case.annual_revenue is not None and case.annual_revenue > SELF_DECLARE_REVENUE_LIMIT:
        g.append({"code": "REVENUE_EXCEEDS_LIMIT",
                  "limit": SELF_DECLARE_REVENUE_LIMIT, "have": case.annual_revenue})
    # BPJPH: 공장 최대 1개 (이 신청 대상 공장 수)
    fac = len(case.facility_ids or [])
    if fac > SELF_DECLARE_MAX_FACILITIES:
        g.append({"code": "TOO_MANY_FACILITIES", "max": SELF_DECLARE_MAX_FACILITIES, "have": fac})
    # BPJPH: 매장 최대 1개. **매장 수는 선택 입력이라 미입력을 차단하지 않는다** —
    # 매장이 없는 제조업체가 대부분이고, 없는 것을 0으로 적으라고 요구하면 그게 더 이상하다.
    # 연매출은 자기선언 자격의 법적 상한(Rp15B)이라 그대로 미입력을 차단한다.
    if case.outlet_count is not None and case.outlet_count > SELF_DECLARE_MAX_OUTLETS:
        g.append({"code": "TOO_MANY_OUTLETS", "max": SELF_DECLARE_MAX_OUTLETS, "have": case.outlet_count})
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


def guard_committee_materials(db, case):
    g = material_safety_blockers(db, case)
    if not evidence_complete(db, case.case_id):
        g.append({"code": "MATERIAL_EVIDENCE_INCOMPLETE"})
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
    return db.query(AuditFinding).filter(
        AuditFinding.case_id == case_id, AuditFinding.severity == "major",
        AuditFinding.status.is_distinct_from("closed")).count()


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
    # draft는 아직 청구 전 상태이므로 미결로 세지 않음 (A1)
    return db.query(Invoice).filter(Invoice.case_id == case_id,
                                    Invoice.status.notin_(INVOICE_SETTLED | {"draft"})).count() > 0


def guard_payment(db, case):
    g = []
    if has_unpaid_invoice(db, case.case_id):
        g.append({"code": "UNPAID_INVOICE"})
    return g


# ---- §5.2 증빙기반 전이 가드 (전면화) ----
def guard_intake_complete(db, case):
    """신청 완비(§5.2) — 회사명·NIB 공통. 인증 대상은 제품/물류로 다르다:
    제품은 제품 최소 1개, 물류(jasa logistik)는 취급 서비스(logistics_scope) 최소 1개."""
    from .models import Product
    g = []
    if not (case.company_name and str(case.company_name).strip()):
        g.append({"code": "COMPANY_NAME_MISSING"})
    if not (case.nib and str(case.nib).strip()):
        g.append({"code": "NIB_MISSING"})
    if (getattr(case, "scheme", "product") or "product") == "logistics":
        # 물류는 제품이 아니라 서비스(보관·포장·유통)가 인증 대상이다.
        if not (case.logistics_scope):
            g.append({"code": "NO_JASA"})
    else:
        if db.query(Product).filter_by(case_id=case.case_id).count() == 0:
            g.append({"code": "NO_PRODUCT"})
    return g


def guard_documents_approved(db, case):
    """필수문서 승인(§5.2 document_review→document_approved) — 반려/재작업 문서 없음."""
    from .models import DocumentAsset
    g = []
    bad = db.query(DocumentAsset).filter(
        DocumentAsset.case_id == case.case_id,
        DocumentAsset.review_status.in_(("rejected", "rework"))).count()
    if bad > 0:
        g.append({"code": "DOCUMENTS_NOT_APPROVED", "unresolved": bad})
    return g


def guard_car_closed(db, case):
    """시정조치 종결(§5.2 finding_open→car_closed) — 미해결 finding·미종결 CAR 없음."""
    from .models import AuditFinding, CorrectiveAction
    g = []
    open_find = db.query(AuditFinding).filter(
        AuditFinding.case_id == case.case_id, AuditFinding.status.is_distinct_from("closed")).count()
    if open_find > 0:
        g.append({"code": "OPEN_FINDINGS", "count": open_find})
    open_car = db.query(CorrectiveAction).filter(
        CorrectiveAction.case_id == case.case_id,
        ~CorrectiveAction.status.in_(("closed", "accepted"))).count()
    if open_car > 0:
        g.append({"code": "UNRESOLVED_CAR", "count": open_car})
    return g


def guard_formal_accepted(db, case):
    """정식 흐름(GLHAC_FORMAL_FLOW) 이 켜져 있으면 관리자 접수(accepted) 전에는 모의심사로 못 간다.
    raw 전이로 인증 가능 판정·정식 신청·접수를 건너뛰던 결함(감사 K4). 플래그 OFF 면 기존 동작."""
    from . import nav8          # nav8 이 state_machine 을 임포트하므로 지연 임포트(순환 회피)
    if not nav8.formal_flow_on():
        return []
    if nav8.formal_state(db, case.case_id) != "accepted":
        return [{"code": "FORMAL_NOT_ACCEPTED", "formal_state": nav8.formal_state(db, case.case_id)}]
    return []


def guard_lph_assigned(db, case):
    from .models import LphAssignment
    assigned = db.query(LphAssignment).filter_by(case_id=case.case_id).all()
    if not any((row.lph_name or "").strip() for row in assigned):
        return [{"code": "LPH_NOT_ASSIGNED"}]
    return []


def guard_lph_entry(db, case):
    return guard_reguler_submit(db, case) + guard_lph_assigned(db, case)


GUARDS = {
    "ai_pre_assessment_ready": guard_intake_complete,        # §5.2 신청 완비
    "document_pre_audit_requested": guard_formal_accepted,   # 정식 흐름 접수 확인(K4)
    "final_package_preparation": guard_final_package,
    "document_pre_audit_in_review": guard_payment,
    "document_pre_audit_approved": guard_documents_approved,  # §5.2 문서 all-approved
    "audit_closed": guard_car_closed,                        # §5.2 CAR 종결
    "self_declare_eligible": guard_pathway_selfdeclare,
    "self_declaration_submitted": guard_selfdeclare_submit,
    "committee_verification": guard_committee_materials,
    "lph_assignment": guard_lph_entry,               # guard_reguler_submit + LPH 배정 확인
    "onsite_audit_scheduled": guard_lph_assigned,
    "certificate_issued": guard_certificate_issue,
}


def evaluate_blocking(db, case):
    b = []
    have, valid = penyelia_state(db, case.org_id)
    if not have:
        b.append({"code": "PENYELIA_HALAL_MISSING"})
    elif not valid:
        # '없음'과 '자격 만료'는 할 일이 다르다 — 지정 vs 재교육
        b.append({"code": "PENYELIA_CERT_EXPIRED"})
    for m in critical_materials(db, case.case_id):
        code = "HARAM_INGREDIENT" if m.screen_status == "haram" else "CRITICAL_MATERIAL_NO_EVIDENCE"
        b.append({"code": code, "target": m.name})
    if not sihalal_verified(db, case.case_id):
        b.append({"code": "SIHALAL_IDENTITY_UNVERIFIED"})
    return b


def can_transition(db, case, to_state):
    if not allowed(case.status, to_state):
        return False, [{"code": "INVALID_TRANSITION", "from": case.status, "to": to_state}]
    if to_state == "committee_verification":
        lock_scope(db, case)
    guard = GUARDS.get(to_state)
    blockers = guard(db, case) if guard else []
    return (len(blockers) == 0), blockers


def apply_side_effects(case, to_state):
    if to_state == "self_declare_eligible":
        case.pathway = "self_declare"
    # P1: reguler 전용 상태로의 전이는 pathway를 reguler로 확정(self_declare→reguler 전환 포함)
    if to_state in ("supplementation_required", "consultant_review") and case.pathway in ("undetermined", "self_declare"):
        case.pathway = "reguler"
    if to_state in ("committee_verification", "final_package_preparation"):
        freeze_scope(object_session(case), case)
    # NOTE(P0-1): fatwa_approved 자동 approved 제거 — 최종승인은 /fatwa/final-approve(operator)만 수행


def positive_listed(m):
    """SEHATI self-declare 긍정목록(known-halal) 판정.
    온톨로지에 매칭(matched_uid)되고 default_status가 halal인 원재료만 '긍정목록 확인'으로 본다.
    NOTE(정직): 여기서 '긍정목록'은 온톨로지 halal 매칭을 대체 기준으로 사용한 것이며,
    공식 BPJPH의 전체 bahan(할랄 긍정목록) 데이터셋 그 자체는 아니다.
    미매칭(matched_uid=None)·비-halal(mushbooh/unknown/haram)은 긍정목록 미확인으로 간주."""
    return (m.matched_uid is not None) and (m.screen_status == "halal")


# 자기선언 경로에 들어와 있는 상태들 — 이 구간에서는 케이스 응답에 긍정목록 경고를 싣는다
SELFDECLARE_STATES = {"self_declare_eligible", "sjph_lite_prepared", "pendamping_verification",
                      "self_declaration_submitted", "committee_verification"}


def selfdeclare_warnings(db, case):
    """자기선언 긍정목록 경고(R1-2) — 막지 않고 알리기만 한다(사용자 결정: 경고만).
    원재료가 0건이면 NO_MATERIALS, 긍정목록(positive_listed — assess_pathway 와 같은 판정) 밖 원재료가
    있으면 NOT_ON_POSITIVE_LIST(+원재료명). 둘 다 아니면 빈 목록."""
    mats = materials(db, case.case_id)
    if not mats:
        return [{"code": "NO_MATERIALS"}]
    non = [m.name for m in mats if not positive_listed(m)]
    return [{"code": "NOT_ON_POSITIVE_LIST", "materials": non}] if non else []


def _non_positive_reason(m):
    return "unmatched" if m.matched_uid is None else "not_halal"


def assess_pathway(db, case):
    mats = materials(db, case.case_id)
    crit = critical_materials(db, case.case_id)
    has_haram = any(m.screen_status == "haram" for m in mats)
    has_mush_high = any(m.screen_status == "mushbooh" and m.screen_severity == "high"
                        and not _has_evidence(m) for m in mats)
    has_mush_med = any(m.screen_status == "mushbooh" and m.screen_severity == "medium" for m in mats)
    risk = "high" if (has_haram or has_mush_high) else ("medium" if has_mush_med else "low")
    ev = evidence_complete(db, case.case_id)
    # SEHATI 긍정목록 게이트: self-declare는 전 원재료가 긍정목록(known-halal) 확인돼야 한다.
    # 미매칭/불명 원재료는 screen_status=None이라 haram이 아니어서 low risk로 통과하므로,
    # 기존 risk/msme/crit/evidence 로직은 보존하고 positive-list를 '추가 게이트'로 얹는다.
    non_positive = [m for m in mats if not positive_listed(m)]
    base_eligible = (risk == "low" and case.is_msme and not crit and ev)
    suggested = "self_declare" if (base_eligible and not non_positive) else "reguler"
    blockers = [{"code": "HAS_CRITICAL_MATERIAL", "target": m.name} for m in crit]
    blockers += [{"code": "NOT_ON_POSITIVE_LIST", "target": m.name, "reason": _non_positive_reason(m)}
                 for m in non_positive]
    # 선택 입력이라 막지는 않지만, 확인하지 못한 자격 요건은 드러내 둔다.
    unverified = []
    if case.annual_revenue is None:
        unverified.append({"field": "annual_revenue", "label": "연매출",
                           "limit": SELF_DECLARE_REVENUE_LIMIT})
    if case.outlet_count is None:
        unverified.append({"field": "outlet_count", "label": "매장 수",
                           "limit": SELF_DECLARE_MAX_OUTLETS})
    return {"suggested_pathway": suggested, "risk_category": risk, "is_msme": bool(case.is_msme),
            "unverified": unverified,
            "critical_ingredient_count": len(crit), "evidence_complete": ev,
            "positive_listed_count": sum(1 for m in mats if positive_listed(m)),
            "non_positive_count": len(non_positive),
            "materials": [{"name": m.name, "positive_listed": positive_listed(m),
                           "reason": "ok" if positive_listed(m) else _non_positive_reason(m)}
                          for m in mats],
            "blockers": blockers}


# ---- 감사 이벤트 (해시 체인, B.5) ----
def _last_hash(db, case_id):
    ev = (db.query(WorkflowEvent).filter_by(case_id=case_id)
          .order_by(WorkflowEvent.created_at.desc(), WorkflowEvent.event_id.desc()).first())
    return ev.row_hash if ev else ""


AUDIT_KEY_PREFIX = "k2:"   # 감사 전용 키(GLHAC_AUDIT_KEY)로 서명한 행의 구분자. 접두 없음 = 기존 GLHAC_SECRET


def audit_key():
    """감사체인 전용 HMAC 키(GLHAC_AUDIT_KEY) — 호출 시점에 읽는다. 미설정이면 None(기존 동작)."""
    raw = (os.environ.get("GLHAC_AUDIT_KEY") or "").strip()
    return raw.encode("utf-8") if raw else None


def _hmac_hex(key, prev, body):
    import hmac
    return hmac.new(key, (prev + body).encode("utf-8"), hashlib.sha256).hexdigest()


def chain_row_hash(prev, body):
    """감사체인 행 해시 — HMAC 키잉.
    키 없는 SHA-256은 DB 쓰기 권한만 있으면 전체 체인 재계산 위조 가능(tamper-evident).
    HMAC 키잉으로 키 없이는 유효 해시를 만들 수 없게 함(tamper-resistant).
    GLHAC_AUDIT_KEY 가 설정돼 있으면 그 키로 서명하고 결과 앞에 'k2:' 를 붙인다(로그인 키 GLHAC_SECRET 과
    분리 — SECRET 을 회전해도 체인 검증 유지). 미설정이면 기존과 같이 HMAC(SECRET) 순수 hex.
    auth는 순환 임포트 회피 위해 지연 임포트."""
    from . import auth
    k = audit_key()
    if k:
        return AUDIT_KEY_PREFIX + _hmac_hex(k, prev, body)
    return _hmac_hex(auth.SECRET, prev, body)


def chain_row_matches(prev, body, stored):
    """저장된 row 해시 검증 — 행별로 구분자가 가리키는 키로 재계산한다(접두 없음=SECRET, k2:=AUDIT_KEY).
    k2: 행인데 AUDIT_KEY 가 없으면 검증할 수 없으므로 불일치(False)."""
    import hmac
    from . import auth
    stored = stored or ""
    if stored.startswith(AUDIT_KEY_PREFIX):
        k = audit_key()
        if not k:
            return False
        expect = AUDIT_KEY_PREFIX + _hmac_hex(k, prev, body)
    else:
        expect = _hmac_hex(auth.SECRET, prev, body)
    return hmac.compare_digest(expect, stored)


def record_event(db, case, frm, to, action, actor_type="system", actor_id=None, payload=None):
    # MA4: 꼬리 조회 전에 DB 쓰기 락(커밋까지 유지)을 잡아 다른 세션과 직렬화 — 근거는 db.lock_chain_tail.
    lock_chain_tail(db, "wf:" + case.case_id)
    prev = _last_hash(db, case.case_id)
    body = json.dumps({"case": case.case_id, "from": frm, "to": to, "action": action,
                       "payload": payload or {}}, sort_keys=True, ensure_ascii=False)
    row_hash = chain_row_hash(prev, body)
    ev = WorkflowEvent(case_id=case.case_id, from_status=frm, to_status=to, action=action,
                       actor_type=actor_type, actor_id=actor_id, payload=payload,
                       prev_hash=prev, row_hash=row_hash, created_at=datetime.utcnow())
    db.add(ev)
    # 해시체인 단절 수정(전수검사 후속): 세션이 autoflush=False라 flush 없이는 같은 트랜잭션의
    # 다음 record_event가 이 이벤트를 _last_hash에서 못 보고 동일 prev_hash로 체인됨
    # (한 요청 다중 이벤트 → audit-verify integrity_ok=false). flush로 체인 연속성 보장.
    db.flush()
    return ev
