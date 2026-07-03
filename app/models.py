"""SQLAlchemy 모델 — 설계서 v2 24.9(마이그레이션)/24.13(ontology)/Part 9 매핑."""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Boolean, DateTime, Date, JSON, Text, Float
from .db import Base


def uid() -> str:
    return uuid.uuid4().hex


class CaseApplication(Base):
    __tablename__ = "case_application"
    case_id = Column(String, primary_key=True, default=uid)
    org_id = Column(String, nullable=False)
    company_name = Column(String)
    # 사업자 프로필 (v1 상속 — 파일 파싱 자동채움 대상)
    nib = Column(String)
    responsible_person = Column(String)
    halal_supervisor = Column(String)
    email = Column(String)
    phone = Column(String)
    address = Column(String)
    factory_reg_no = Column(String)
    factory_address = Column(String)
    due_date = Column(String)  # 처리 목표 기한(ISO date) — 기한 경보용
    status = Column(String, nullable=False, default="onboarding")
    pathway = Column(String, nullable=False, default="undetermined")  # 24.9
    risk_category = Column(String)
    is_msme = Column(Boolean)
    sehati_eligible = Column(String)
    fatwa_status = Column(String, default="none")
    scope_frozen = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class ChangeImpact(Base):
    """인증서 변경영향 분석 이력 — 사후관리(설계 8.3)."""
    __tablename__ = "change_impact"
    change_impact_id = Column(String, primary_key=True, default=uid)
    case_id = Column(String, nullable=False, index=True)
    change_type = Column(String, nullable=False)
    impact_score = Column(Float)
    risk_level = Column(String)
    affected_products = Column(JSON)
    required_actions = Column(JSON)
    reason = Column(Text)
    actor = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class Product(Base):
    __tablename__ = "product"
    product_id = Column(String, primary_key=True, default=uid)
    case_id = Column(String, nullable=False)
    name = Column(String, nullable=False)
    category = Column(String)


class ProductMaterial(Base):
    """원재료-제품 매트릭스 (v1 상속) — 설계 9.1 product_material."""
    __tablename__ = "product_material"
    id = Column(String, primary_key=True, default=uid)
    case_id = Column(String, nullable=False)
    product_id = Column(String, nullable=False)
    material_id = Column(String, nullable=False)


class Material(Base):
    __tablename__ = "material"
    material_id = Column(String, primary_key=True, default=uid)
    case_id = Column(String, nullable=False)
    name = Column(String, nullable=False)
    e_number = Column(String)
    # v1 상속 필드
    mat_type = Column(String)   # raw|additive|processing_aid|packaging|lubricant|sanitizer
    source = Column(String)     # animal|plant|microbial|synthetic|mineral|unknown
    supplier = Column(String)
    cert = Column(String)       # certified|exempt|unknown
    cert_no = Column(String)
    note = Column(Text)
    v1_risk = Column(String)    # high|medium|low (v1 source-rule 결과)
    evidence_provided = Column(Boolean, default=False)
    source_known = Column(Boolean, default=True)
    # v2 ontology 판정
    screen_result = Column(String)
    screen_status = Column(String)
    screen_severity = Column(String)
    matched_uid = Column(String)


class WorkflowEvent(Base):
    """append-only + 해시 체인 (설계 B.5)."""
    __tablename__ = "workflow_event"
    event_id = Column(String, primary_key=True, default=uid)
    case_id = Column(String, nullable=False)
    from_status = Column(String)
    to_status = Column(String)
    action = Column(String, nullable=False)
    actor_type = Column(String, nullable=False, default="system")
    actor_id = Column(String)
    payload = Column(JSON)
    prev_hash = Column(String)
    row_hash = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class PenyeliaHalal(Base):
    __tablename__ = "penyelia_halal"
    penyelia_id = Column(String, primary_key=True, default=uid)
    org_id = Column(String, nullable=False)
    user_id = Column(String)
    name = Column(String, nullable=False)
    training_cert = Column(String)
    cert_expiry = Column(Date)
    status = Column(String, nullable=False, default="active")


class PendampingAssignment(Base):
    __tablename__ = "pendamping_assignment"
    assignment_id = Column(String, primary_key=True, default=uid)
    case_id = Column(String, nullable=False)
    pendamping_id = Column(String, nullable=False)
    decision = Column(String)
    note = Column(Text)
    signature_ref = Column(String)
    verified_at = Column(DateTime)


class AuditFinding(Base):
    """심사 지적 (v1 상속) — 설계 9.1 audit_finding."""
    __tablename__ = "audit_finding"
    finding_id = Column(String, primary_key=True, default=uid)
    case_id = Column(String, nullable=False)
    area = Column(String)
    finding = Column(Text)
    severity = Column(String)   # major|minor|observation
    corrective_action = Column(Text)
    due_date = Column(String)
    status = Column(String, default="open")  # open|closed
    auditor = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class LphAssignment(Base):
    __tablename__ = "lph_assignment"
    lph_assignment_id = Column(String, primary_key=True, default=uid)
    case_id = Column(String, nullable=False)
    lph_name = Column(String)
    auditor_ref = Column(String)
    source = Column(String, default="manual")
    assigned_at = Column(DateTime, default=datetime.utcnow)


class ExternalIdentity(Base):
    """SIHALAL 식별자 연동 — 설계 Part 23 / 24.5."""
    __tablename__ = "external_identity"
    external_identity_id = Column(String, primary_key=True, default=uid)
    case_id = Column(String)
    org_id = Column(String, nullable=False)
    provider = Column(String, default="SIHALAL")
    external_username = Column(String)
    external_email = Column(String)
    external_application_no = Column(String)
    verification_status = Column(String, default="submitted_by_applicant")
    identifier_match = Column(Boolean)
    pathway = Column(String)


class IngredientOntology(Base):
    """임계원재료 ontology — 설계 24.13."""
    __tablename__ = "ingredient_ontology"
    ingredient_uid = Column(String, primary_key=True)
    canonical_name = Column(String, nullable=False)
    category = Column(String, nullable=False)
    e_number = Column(String)
    default_status = Column(String, nullable=False)
    severity = Column(String, nullable=False)
    najis_risk = Column(Boolean, default=False)
    carrier_check = Column(String)   # 'alcohol' | 'gelatin' — 캐리어/용매 점검 (24.13.8)
    sources = Column(JSON)
    aliases = Column(JSON)
    required_evidence = Column(JSON)
    alternatives = Column(JSON)
    rule_version = Column(String)


class HalalCertificate(Base):
    """할랄 인증서 (v1 상속) — 발급/scope/갱신/변경영향."""
    __tablename__ = "halal_certificate"
    id = Column(String, primary_key=True, default=uid)
    case_id = Column(String, nullable=False)
    certificate_no = Column(String)
    scope = Column(JSON)         # 제품명 목록
    issue_date = Column(String)
    expiry_date = Column(String)
    status = Column(String, default="active")  # active|suspended|withdrawn
    frozen_product_ids = Column(JSON)    # 발급 시 동결 제품 ID 목록 — S3-3
    frozen_material_ids = Column(JSON)   # 발급 시 동결 원재료 ID 목록 — S3-3


class FatwaDecision(Base):
    """파트와/위원회 결정 (v1 상속) — 설계 9.1 fatwa_decision."""
    __tablename__ = "fatwa_decision"
    id = Column(String, primary_key=True, default=uid)
    case_id = Column(String, nullable=False)
    decision = Column(String, default="pending")   # pending|approved|rejected|conditional
    decision_no = Column(String)
    committee_note = Column(Text)
    committee_head = Column(String)       # 위원장 — S3-2
    committee_secretary = Column(String)  # 간사 — S3-2
    committee_members = Column(JSON)      # 위원 목록 — S3-2
    product_scope = Column(JSON)          # 파트와 대상 제품 ID 목록 — S3-2
    decided_at = Column(DateTime)         # 샤리아 가승인 시각
    final_approved_at = Column(DateTime)  # 최고운영자 최종승인 시각 — 2단계 승인
    final_approver = Column(String)       # 최종 승인자 uid


class Invoice(Base):
    """청구/인보이스 (v1 상속) — PPN 11% + 전이게이트."""
    __tablename__ = "invoice"
    invoice_id = Column(String, primary_key=True, default=uid)
    case_id = Column(String, nullable=False)
    invoice_no = Column(String)
    service_type = Column(String)   # pre_audit|onsite
    amount = Column(Float)
    ppn = Column(Float)
    total = Column(Float)
    status = Column(String, default="unpaid")  # unpaid|paid
    created_at = Column(DateTime, default=datetime.utcnow)


class HpasEvaluation(Base):
    """SJPH/HPAS 5대 요소 평가 (v1 상속) — 설계 9.1 sjph_evidence/hpas."""
    __tablename__ = "hpas_evaluation"
    id = Column(String, primary_key=True, default=uid)
    case_id = Column(String, nullable=False)
    element = Column(String, nullable=False)   # commitment|materials|process|product|monitoring
    status = Column(String, default="not_started")  # ok|gap|not_started
    note = Column(Text)


class RuleVersion(Base):
    __tablename__ = "rule_version"
    code = Column(String, primary_key=True)
    jurisdiction = Column(String)
    effective_from = Column(String)
    status = Column(String, default="active")


class DocumentAsset(Base):
    """Document Intake AI 결과 — 설계 Part 5/9.2."""
    __tablename__ = "document_asset"
    document_id = Column(String, primary_key=True, default=uid)
    case_id = Column(String, nullable=False)
    filename = Column(String, nullable=False)
    doc_type = Column(String)
    confidence = Column(Float)
    fields = Column(JSON)
    text_excerpt = Column(Text)
    review_status = Column(String, default="pending")  # pending|approved|rejected|rework
    content_b64 = Column(Text)    # 원본 파일 base64 (조회/다운로드용, <3MB만)
    content_type = Column(String)  # MIME
    material_id = Column(String)   # 원재료별 증빙 연결(nullable) — 설계 G1/C1
    product_id = Column(String)    # 제품 사진 연결(nullable) — 설계 G2
    created_at = Column(DateTime, default=datetime.utcnow)


class SjphEvidence(Base):
    """SJPH Integration 10증빙 (v1 상속) — 설계 P1-#7."""
    __tablename__ = "sjph_evidence"
    id = Column(String, primary_key=True, default=uid)
    case_id = Column(String, nullable=False)
    item_key = Column(String, nullable=False)
    filename = Column(String)
    document_id = Column(String)
    uploaded_at = Column(DateTime, default=datetime.utcnow)


class OnsiteChecklist(Base):
    """현장 체크리스트 16항목 — S3-1 설계 §9.2."""
    __tablename__ = "onsite_checklist"
    id = Column(String, primary_key=True, default=uid)
    case_id = Column(String, nullable=False)
    item_key = Column(String, nullable=False)   # 16개 항목 키
    result = Column(String, default="not_checked")  # not_checked|comply|nonconformity
    note = Column(Text)
    updated_at = Column(DateTime, default=datetime.utcnow)


class AuditorPool(Base):
    """심사원 풀 배정 (3명) — S3-5 설계 §13."""
    __tablename__ = "auditor_pool"
    id = Column(String, primary_key=True, default=uid)
    case_id = Column(String, nullable=False)
    name = Column(String, nullable=False)
    cert_no = Column(String)
    role_in_team = Column(String, default="anggota")  # ketua|anggota
    assigned_at = Column(DateTime, default=datetime.utcnow)


class LphReference(Base):
    """인정 LPH 레퍼런스 테이블 — S3-6 드롭다운 소스."""
    __tablename__ = "lph_reference"
    lph_id = Column(String, primary_key=True, default=uid)
    name = Column(String, nullable=False)
    accreditation_no = Column(String)
    region = Column(String)
    status = Column(String, default="active")  # active|inactive


class Org(Base):
    """조직 (admin 관리용, 경량) — org_id 문자열 정본."""
    __tablename__ = "org"
    org_id = Column(String, primary_key=True)
    name = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class Discussion(Base):
    """역할별 토론/수정노트 (v1 상속) — 설계 11.x."""
    __tablename__ = "discussion"
    id = Column(String, primary_key=True, default=uid)
    case_id = Column(String, nullable=False)
    kind = Column(String, default="comment")   # add|repair|comment|resolved
    target = Column(String)
    text = Column(Text)
    author_role = Column(String)
    author = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class User(Base):
    """설계 24.9 app_user — RBAC/ABAC 주체 (B.4)."""
    __tablename__ = "app_user"
    user_id = Column(String, primary_key=True, default=uid)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(String, nullable=False)   # applicant|penyelia_halal|consultant|pendamping_pph|auditor|fatwa_liaison|admin
    org_id = Column(String, nullable=False)