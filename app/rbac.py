"""단일 소스 RBAC action_id 매트릭스 (보강안 §2.1/§3.1/§12.1).

API 권한 강제·UI 버튼 노출·계약 테스트가 모두 이 하나의 매트릭스에서 파생된다.
- ACTION_ROLES: action_id -> 허용 역할 집합 (admin은 항상 통과, 빈 집합=admin 전용)
- ACTION_ENDPOINTS: action_id -> (method, path, sample_body)  # 계약 테스트 자동생성용
"""
from fastapi import Depends, HTTPException
from . import auth

# 거버넌스 핵심 액션(민감/책임 큰 행위)부터 단일 매트릭스로 관리.
ACTION_ROLES = {
    "certificate.issue": {"operator"},
    "certificate.unlock": {"operator"},
    "certificate.suspend": {"operator"},               # 정지
    "certificate.revoke": {"operator"},                # 철회
    "certificate.reactivate": {"operator"},            # 정지 해제
    "certificate.renew": {"operator"},                 # 승인·실행
    "certificate.renew_request": {"applicant", "consultant"},  # 신청
    "fatwa.propose": {"fatwa_liaison"},                # 가승인(SoD)
    "fatwa.approve_final": {"operator"},               # 최종승인(SoD)
    "fatwa.document.read": {"fatwa_liaison", "operator"},
    "lph.assign": {"fatwa_liaison", "operator"},
    # 워크플로 액션 배치 이관
    "audit.mock_decide": {"auditor", "fatwa_liaison", "operator"},
    "material.add": {"applicant", "consultant", "penyelia_halal"},
    "material.delete": {"applicant", "consultant", "penyelia_halal"},
    "material.evidence": {"applicant", "consultant", "penyelia_halal"},
    "document.review": {"consultant"},
    "sjph.edit": {"applicant", "consultant", "penyelia_halal"},
    "finding.add": {"auditor", "consultant"},
    "finding.update": {"auditor", "consultant"},
    "onsite.checklist": {"auditor", "consultant"},
    "auditor_pool.add": {"operator"},
    "penyelia.update": {"applicant", "consultant", "penyelia_halal"},
    "pendamping.verify": {"pendamping_pph"},
    "admin.user.manage": set(),                        # admin 전용
}

# action_id -> 엔드포인트 계약. 계약 테스트가 이 표를 순회해 역할×엔드포인트 403/허용을 검증.
ACTION_ENDPOINTS = {
    "certificate.issue": ("POST", "/cases/{cid}/certificate/issue", None),
    "certificate.unlock": ("POST", "/cases/{cid}/certificate/unlock", {"reason": "계약테스트사유"}),
    "certificate.suspend": ("POST", "/cases/{cid}/certificate/suspend", {"reason": "계약테스트사유"}),
    "certificate.revoke": ("POST", "/cases/{cid}/certificate/revoke", {"reason": "계약테스트사유"}),
    "certificate.reactivate": ("POST", "/cases/{cid}/certificate/reactivate", {"reason": "계약테스트사유"}),
    "certificate.renew": ("POST", "/cases/{cid}/renew", None),
    "certificate.renew_request": ("POST", "/cases/{cid}/renew/request", None),
    "fatwa.propose": ("PATCH", "/cases/{cid}/fatwa", {"decision": "approved"}),
    "fatwa.approve_final": ("POST", "/cases/{cid}/fatwa/final-approve", None),
    "fatwa.document.read": ("POST", "/cases/{cid}/fatwa/document", None),
    "lph.assign": ("POST", "/cases/{cid}/lph-assignment", {"lph_name": "x"}),
    "audit.mock_decide": ("POST", "/cases/{cid}/mock-audit/decision", {"result": "pass"}),
    "material.add": ("POST", "/cases/{cid}/materials", {"name": "x"}),
    "material.delete": ("DELETE", "/materials/ctdummy", None),
    "document.review": ("PATCH", "/documents/ctdummy/review", {"review_status": "approved"}),
    "sjph.edit": ("PATCH", "/cases/{cid}/sjph", {"element": "commitment", "status": "ok"}),
    "finding.add": ("POST", "/cases/{cid}/findings", {"finding": "x", "severity": "minor"}),
    "finding.update": ("PATCH", "/findings/ctdummy", {"status": "closed"}),
    "onsite.checklist": ("POST", "/cases/{cid}/onsite-checklist", {"item_key": "x", "result": "comply"}),
    "auditor_pool.add": ("POST", "/cases/{cid}/auditor-pool", {"name": "x"}),
    "pendamping.verify": ("POST", "/cases/{cid}/pendamping/verify", {"decision": "verified"}),
    "admin.user.manage": ("POST", "/admin/users", {"username": "ct", "password": "pw", "role": "applicant"}),
}


def can(user, action_id) -> bool:
    """user(토큰 payload)가 action_id를 수행할 수 있는지. admin은 항상 True."""
    if user.get("role") == "admin":
        return True
    return user.get("role") in ACTION_ROLES.get(action_id, set())


def allowed_actions(user) -> dict:
    """UI 버튼 노출용 — 현재 사용자가 수행 가능한 전체 action_id 맵."""
    return {a: can(user, a) for a in ACTION_ROLES}


def require_action(action_id):
    """FastAPI 의존성 — 매트릭스 기반 권한 게이트(require_roles를 대체)."""
    def dep(user=Depends(auth.get_current_user)):
        if not can(user, action_id):
            raise HTTPException(403, {"code": "NOT_AUTHORIZED", "action": action_id,
                                      "need": sorted(ACTION_ROLES.get(action_id, set())),
                                      "have": user.get("role")})
        return user
    return dep
