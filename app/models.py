"""SQLAlchemy 모델 — 설계서 v2 24.9(마이그레이션)/24.13(ontology)/Part 9 매핑."""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Boolean, DateTime, Date, JSON, Text, Float, Integer
from .db import Base
from .crypto import EncryptedType


def uid() -> str:
    return uuid.uuid4().hex


class CaseApplication(Base):
    __tablename__ = "case_application"
    case_id = Column(String, primary_key=True, default=uid)
    org_id = Column(String, nullable=False)
    company_name = Column(String)
    # 사업자 프로필 (v1 상속 — 파일 파싱 자동채움 대상)
    nib = Column(EncryptedType)
    responsible_person = Column(EncryptedType)
    halal_supervisor = Column(EncryptedType)
    email = Column(EncryptedType)
    phone = Column(EncryptedType)
    address = Column(EncryptedType)
    factory_reg_no = Column(EncryptedType)
    factory_address = Column(EncryptedType)
    due_date = Column(String)  # 처리 목표 기한(ISO date) — 기한 경보용
    notify_consent = Column(Boolean, default=False)  # 알림 수신 동의(WhatsApp opt-in 등)
    status = Column(String, nullable=False, default="onboarding")
    draft_state = Column(String)  # 신청단계 오버레이: saved(임시저장)|in_progress(작성중)|completed(작성완료)|returned(반려)
    return_reason = Column(Text)  # 반려 사유(consultant→applicant)
    pathway = Column(String, nullable=False, default="undetermined")  # 24.9
    # 인증 종류 — 신청 맨 앞에서 신청자가 선언한다. pathway(SEHATI/Reguler)와 달리
    # 시스템이 원재료로 유도할 수 없다(물류사엔 판정 근거가 될 원재료가 없다) → 선언값이다.
    # product = 제품 할랄 인증 / logistics = 할랄 물류 서비스 인증(jasa logistik).
    # 기존 행은 전부 제품이었다 → 기본값 product 로 자동 백필(_migrate).
    scheme = Column(String, nullable=False, default="product")
    logistics_scope = Column(JSON)  # 물류만: ["penyimpanan","pengemasan","pendistribusian"] 복수 선택
    scheme_frozen = Column(Boolean, default=False)  # 서류 요구가 갈리는 순간 잠금 — 이후 변경은 새 케이스
    risk_category = Column(String)
    is_msme = Column(Boolean)
    annual_revenue = Column(Integer)   # 연매출(IDR) — BPJPH 자기선언 ≤Rp15B 판정(Decision 146/2025)
    outlet_count = Column(Integer)     # 매장(영업장) 수 — 자기선언 최대 1개
    sehati_eligible = Column(String)
    fatwa_status = Column(String, default="none")
    scope_frozen = Column(Boolean, default=False)
    profile_ext = Column(JSON)  # Company/Facility Info 확장 양식 필드(PIC·CP·등록유형·공장정보 등)
    facility_ids = Column(JSON)  # 이 신청 대상 공장 선택 — Phase 1
    product_ids = Column(JSON)   # 이 신청 대상 제품 선택·분류 — Phase 1
    created_at = Column(DateTime, default=datetime.utcnow)


class GeneratedDocument(Base):
    """생성 문서 버전관리 (Rizky #3·#4·#11) — SJPH Manual·Audit Report."""
    __tablename__ = "generated_document"
    gen_doc_id = Column(String, primary_key=True, default=uid)
    case_id = Column(String, index=True)
    org_id = Column(String)
    doc_type = Column(String)   # sjph_manual | audit_report
    version = Column(Integer, default=1)
    content = Column(Text)
    status = Column(String, default="draft")  # draft | approved
    created_by = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class Notification(Base):
    """자동 알림 (Rizky #5) — 이벤트 발생 시 생성, 채널 프로바이더로 발송."""
    __tablename__ = "notification"
    notification_id = Column(String, primary_key=True, default=uid)
    org_id = Column(String, index=True)
    case_id = Column(String)
    role = Column(String)          # 대상 역할(None=조직 전체)
    event_type = Column(String)    # document_requested|audit_scheduled|audit_closed|fatwa_approved|certificate_issued|expiry_soon
    channels = Column(JSON)        # ["inapp","sms","kakao","whatsapp"]
    title = Column(String)
    body = Column(Text)
    payload = Column(JSON)         # {"key": 문구키, "params": {...}} — 읽는 사람 언어로 다시 조립
    # title/body 는 한국어로 저장한 뒤 그대로 보여줬다. 인니어 화면에도 한국어 알림이 떴다
    # (실측: 인니어 화면에 남은 한글 245건 중 대부분). 발송 채널(SMS·WhatsApp)은 저장된
    # 문구를 그대로 쓰므로 건드리지 않고, 화면에 보일 때만 payload 로 다시 조립한다.
    status = Column(String, default="unsent")  # unsent|sent|failed
    attempts = Column(Integer, default=0)      # 발송 시도 횟수(비동기 워커)
    last_error = Column(String)
    read = Column(Boolean, default=False)
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
    org_id = Column(String, index=True)   # 회사 카탈로그화 준비 — Phase 1
    name = Column(String, nullable=False)
    category = Column(String)
    # 제품 설명 — 현장심사 보고서 제품표의 'Name' 칸에 제품명과 함께 인쇄된다
    # (템플릿 실측: "YUMTEA PEACHXSHOT / Vitamin Ion powder ..."처럼 이름 + 설명).
    description = Column(Text)
    registration_type = Column(String)   # new|renewal|material_change (Rizky: Product Detail)
    status = Column(String, default="draft")   # draft|under_review|certified|expired


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
    supplier = Column(String)   # 공급사(Pemasok) — 사오는 곳
    manufacturer = Column(String)   # 제조사(Produsen) — 만드는 곳. 공급사와 다를 수 있다
    origin = Column(String)     # 원산지(국가) — SJPH Appendix.4 'Negara/Country' 칸에 인쇄된다
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
    external_email = Column(EncryptedType)
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
    qr_token = Column(String, index=True)   # 공개 검증 토큰(§6.4) — /verify/{token}


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
    status = Column(String, default="waiting_payment")  # 9종(draft~refunded/expired), legacy unpaid=waiting
    due_date = Column(DateTime)
    payment_ref = Column(String, index=True)
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
    filename_en = Column(String)   # 한글 파일명의 영문 표시명(결정적 용어사전) — 인니·영문 심사자용
    doc_type = Column(String)
    confidence = Column(Float)
    fields = Column(JSON)
    text_excerpt = Column(Text)
    ocr_lines = Column(JSON)       # OCR 인식 라인+좌표 캐시 — 같은 사진을 두 번 읽지 않는다.
    # 기록물 사진의 수량·날짜·담당자는 그 사진 안에만 있어 매뉴얼·현장심사 보고서를 만들
    # 때마다 다시 OCR했다(사진 5장 ≈ 40초, 생성·미리보기마다 반복). 인테이크에서 이미
    # 읽은 결과를 두면 될 일이다. 좌표를 함께 남기는 이유는 열이 섞인 표의 행 복원 때문.
    review_status = Column(String, default="pending")  # pending|approved|rejected|rework
    translations = Column(JSON)   # {lang: 번역문} 온디맨드 캐시(예: {"id": "..."}) — 조회시점 번역
    content_b64 = Column(EncryptedType)    # 원본 파일 base64(암호화 저장) — 조회/다운로드용
    content_type = Column(String)  # MIME
    material_id = Column(String)   # 원재료별 증빙 연결(nullable) — 설계 G1/C1
    product_id = Column(String)    # 제품 사진 연결(nullable) — 설계 G2
    lat = Column(Float)            # 촬영 위치 위도 — 현장실사 사진 EXIF GPS/브라우저
    lng = Column(Float)            # 촬영 위치 경도
    geo_source = Column(String)    # exif|browser|manual — 위치 출처
    uploaded_by = Column(String)   # 업로더 uid — 증거 귀속(감사 A08 누가)
    uploader_role = Column(String) # 업로더 역할 — 증거 귀속(감사 A08 누가)
    captured_at = Column(String)   # EXIF DateTimeOriginal 원본 촬영시각 — 증거 귀속(A08 언제)
    file_hash = Column(String)     # sha256 파일 해시 — 증거 무결성(A08 무결성)
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
    address = Column(EncryptedType)      # 회사 주소(암호화) — Phase 1
    profile_ext = Column(JSON)    # Company Info 상세 — Phase 1
    # 담당 컨설턴트 — 영업으로 이 업체를 데려온 사람. 케이스별 배정과 성격이 다르다.
    # 오디터는 심사기관이 케이스마다 배정하지만, 컨설턴트는 업체가 들어올 때 이미
    # 관계가 있다. 그래서 케이스가 아니라 조직에 붙는다.
    consultant_id = Column(String, index=True)
    consultant_linked_at = Column(DateTime)
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
    company_role = Column(String, default="client_admin")  # client_admin(기업업무 관리자)|client_staff(업무자) — Phase 1
    token_version = Column(Integer, default=0)  # 토큰 무효화 버전(auth.make_token 참조)


class Facility(Base):
    """공장·시설 (회사1:공장N) — Phase 1 신규 엔티티."""
    __tablename__ = "facility"
    facility_id = Column(String, primary_key=True, default=uid)
    org_id = Column(String, index=True, nullable=False)   # 소속 회사(org)
    name = Column(String)               # 제조업체/공장명
    address = Column(EncryptedType)
    city = Column(String)
    country = Column(String)
    zip = Column(String)
    reg_no = Column(String)             # 공장등록번호
    profile_ext = Column(JSON)          # Facility Info 상세
    created_at = Column(DateTime, default=datetime.utcnow)
    token_version = Column(Integer, default=0)   # 토큰 취소 — 증가 시 기존 토큰 전부 무효(§9.1)


class AiExtraction(Base):
    """AI/OCR 결과 근거저장 (보강안 §7.2) — Human-in-the-loop 추적성."""
    __tablename__ = "ai_extraction"
    id = Column(String, primary_key=True, default=uid)
    case_id = Column(String, index=True)
    source = Column(String)          # ocr|label_judgment|biz_doc
    model_provider = Column(String)  # local|paddleocr 등
    model_name = Column(String)
    model_version = Column(String)
    extracted_json = Column(JSON)    # 추출 필드/판정
    confidence = Column(Float)
    evidence = Column(Text)          # 원문 근거 텍스트(요약)
    reviewer_status = Column(String, default="unreviewed")  # unreviewed|accepted|overridden
    reviewer_id = Column(String)
    reviewer_note = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class AuditPlan(Base):
    """LPH 현장심사 일정(§P2 LPH scheduling)."""
    __tablename__ = "audit_plan"
    id = Column(String, primary_key=True, default=uid)
    case_id = Column(String, nullable=False, index=True)
    lph_name = Column(String)
    scheduled_date = Column(String)   # ISO date
    scope = Column(Text)
    auditors = Column(JSON)           # 심사원 이름 목록
    status = Column(String, default="scheduled")  # scheduled|completed|cancelled
    note = Column(String)
    created_by = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class FatwaVote(Base):
    """파트와 위원 투표(§6.2 fatwa_votes) — 위원별 1표, quorum 산정."""
    __tablename__ = "fatwa_vote"
    id = Column(String, primary_key=True, default=uid)
    case_id = Column(String, nullable=False, index=True)
    member = Column(String, nullable=False)
    vote = Column(String)             # approve|reject|abstain
    note = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class Contract(Base):
    """할랄인증 계약서 (FORM 4.1-HCB-GL HAC) — 정적 법률조항 + 동적 병합필드."""
    __tablename__ = "contract"
    contract_id = Column(String, primary_key=True, default=uid)
    case_id = Column(String, index=True, nullable=False)
    org_id = Column(String)
    contract_no = Column(String)
    party_a = Column(String)            # 회사명
    effective_date = Column(String)     # ISO date str
    standard = Column(String, default="SJPH")
    scope = Column(JSON)                # ["Foods","Beverages",...]
    product_ids = Column(JSON)
    fee = Column(Float)
    currency = Column(String, default="KRW")
    status = Column(String, default="draft")   # draft|issued|signed
    signatures = Column(JSON)           # [{"party":"A"|"B","name","title","signed_at"}]
    created_at = Column(DateTime, default=datetime.utcnow)


class Consultation(Base):
    """고객 상담/문의 (관리자 고객대응) — 회의 2026-07-09 반영."""
    __tablename__ = "consultation"
    id = Column(String, primary_key=True, default=uid)
    org_id = Column(String, index=True)
    case_id = Column(String)            # nullable
    channel = Column(String, default="inapp")   # inapp|phone|email|kakao|whatsapp
    subject = Column(String)
    message = Column(Text)
    status = Column(String, default="open")      # open|answered|closed
    response = Column(Text)
    responder = Column(String)
    created_by = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    answered_at = Column(DateTime)


class CorrectiveAction(Base):
    """시정조치 CAR(§P2 CAR advanced) — finding별 제출→검토→종결 라이프사이클."""
    __tablename__ = "corrective_action"
    id = Column(String, primary_key=True, default=uid)
    case_id = Column(String, nullable=False, index=True)
    finding_id = Column(String, nullable=False, index=True)
    description = Column(Text)
    evidence = Column(Text)
    status = Column(String, default="submitted")  # submitted|accepted|rejected|closed
    reviewer = Column(String)
    reviewer_note = Column(String)
    due_date = Column(String)
    submitted_by = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class IntegrationEvent(Base):
    """외부 연동 이벤트 로그(§10.1) — idempotency_key로 중복수신 방지."""
    __tablename__ = "integration_event"
    id = Column(String, primary_key=True, default=uid)
    provider = Column(String, default="sihalal")
    event_type = Column(String)
    external_id = Column(String)
    idempotency_key = Column(String, index=True)
    case_id = Column(String, index=True)
    request_hash = Column(String)
    response_hash = Column(String)
    payload = Column(JSON)
    status = Column(String, default="received")   # received|processed|failed
    retry_count = Column(Integer, default=0)
    last_error = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class Signature(Base):
    """전자서명(§6.4) — 인증서/문서 무결성+발급자 서명(내부 HMAC MVP)."""
    __tablename__ = "signature"
    id = Column(String, primary_key=True, default=uid)
    case_id = Column(String, index=True)
    subject_type = Column(String)     # certificate|document
    subject_id = Column(String)
    signer = Column(String)
    provider = Column(String, default="internal-hmac")
    payload_hash = Column(String)     # sha256 canonical content
    signature_value = Column(String)  # HMAC(SECRET, payload)
    signed_at = Column(DateTime, default=datetime.utcnow)


class Payment(Base):
    """결제 기록(§P2 billing/payment) — 인보이스 결제 확인."""
    __tablename__ = "payment"
    id = Column(String, primary_key=True, default=uid)
    invoice_id = Column(String, index=True)
    case_id = Column(String, index=True)
    amount = Column(Float)
    currency = Column(String, default="IDR")
    method = Column(String)           # bank_transfer|va|card|manual
    reference = Column(String)
    status = Column(String, default="confirmed")  # pending|confirmed
    paid_by = Column(String)
    paid_at = Column(DateTime, default=datetime.utcnow)


class AuditLog(Base):
    """접근/조회 감사로그(§2.4) — 워크플로 전이(workflow_event)와 별개.
    누가·언제·무엇을 조회/다운로드/변경했는지. 읽기까지 감사 대상."""
    __tablename__ = "audit_log"
    id = Column(String, primary_key=True, default=uid)
    actor_id = Column(String, index=True)
    actor_role = Column(String)
    org_id = Column(String, index=True)
    action = Column(String, index=True)   # case.read|document.download|fatwa.document.read|certificate.read|ai.extraction.read|role.change
    resource_type = Column(String)
    resource_id = Column(String, index=True)
    case_id = Column(String, index=True)
    meta = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)


class Deposit(Base):
    """은행 입금내역 (Payment P2 §8.4) — 인보이스 자동매칭 대상."""
    __tablename__ = "deposit"
    id = Column(String, primary_key=True, default=uid)
    bank_name = Column(String)
    account_no_masked = Column(String)
    depositor_name = Column(String)
    amount = Column(Float)
    currency = Column(String, default="IDR")
    ref_memo = Column(String)                 # 이체 메모(invoice_no/payment_ref 포함 가능)
    deposit_at = Column(DateTime, default=datetime.utcnow)
    matched_invoice_id = Column(String, index=True)
    match_status = Column(String, default="unmatched")   # unmatched|candidate|matched|rejected
    created_at = Column(DateTime, default=datetime.utcnow)


class PaymentMatchCandidate(Base):
    """입금↔인보이스 매칭 후보 (Payment P2 §8.5) — 규칙 스코어 + 관리자 승인."""
    __tablename__ = "payment_match_candidate"
    id = Column(String, primary_key=True, default=uid)
    deposit_id = Column(String, index=True)
    invoice_id = Column(String, index=True)
    case_id = Column(String)
    score = Column(Float)
    reason = Column(JSON)
    risk_flags = Column(JSON)
    decision_status = Column(String, default="pending")  # pending|approved|held|rejected
    reviewer_id = Column(String)
    reviewed_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)


class Refund(Base):
    """환불 요청·2단계 승인 (Payment P5) — maker(요청)≠checker(승인)."""
    __tablename__ = "refund"
    id = Column(String, primary_key=True, default=uid)
    invoice_id = Column(String, index=True)
    case_id = Column(String, index=True)
    amount = Column(Float)
    reason = Column(Text)                 # 환불 사유
    status = Column(String, default="requested")   # requested|approved|rejected
    requested_by = Column(String)
    decided_by = Column(String)
    decide_note = Column(Text)            # 승인/거절 메모
    decided_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)


class Feedback(Base):
    """솔루션 개선 피드백(전역, 케이스 비종속) — 제출=전원, 열람=admin/ops 전체·그외 본인."""
    __tablename__ = "feedback"
    feedback_id = Column(String, primary_key=True, default=uid)
    author = Column(String, nullable=False)         # username
    author_role = Column(String, nullable=False)
    org_id = Column(String)
    category = Column(String, default="improvement")  # improvement|bug|question|other
    title = Column(String, nullable=False)
    body = Column(Text)
    status = Column(String, default="open")          # open|reviewing|resolved|wontfix
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class FeedbackImage(Base):
    __tablename__ = "feedback_image"
    image_id = Column(String, primary_key=True, default=uid)
    feedback_id = Column(String, nullable=False, index=True)
    filename = Column(String)
    content_b64 = Column(EncryptedType)          # <4MB만 저장(암호화)
    content_type = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class FeedbackComment(Base):
    __tablename__ = "feedback_comment"
    comment_id = Column(String, primary_key=True, default=uid)
    feedback_id = Column(String, nullable=False, index=True)
    author = Column(String, nullable=False)
    author_role = Column(String, nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class MaterialMeasurement(Base):
    """원재료 정량 측정값 (P3-데이터 선행) — 에탄올%·중금속 ppm 등 실측치.

    정성 온톨로지(IngredientOntology)가 다루지 못하는 정량 임계 판정의 근거.
    param_key는 QUANT_CRITERIA 키, verdict는 서버가 임계와 대조해 계산·저장한다.
    """
    __tablename__ = "material_measurement"
    measurement_id = Column(String, primary_key=True, default=uid)
    case_id = Column(String, nullable=False, index=True)
    material_id = Column(String, index=True)      # None = 케이스(제품) 단위 측정
    param_key = Column(String, nullable=False)    # ethanol_pct | lead_ppm | ...
    value = Column(Float)
    unit = Column(String)
    method = Column(String)                       # 시험법(예: GC-MS, ICP-MS)
    lab_name = Column(String)
    tested_at = Column(String)                    # ISO date (성적서 시험일)
    document_id = Column(String)                  # 근거 문서(CoA/성적서)
    verdict = Column(String)                      # pass | fail | unknown
    note = Column(Text)
    recorded_by = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


# ===== 동적 메뉴 시스템 (설계서 §3) — DB 기반 메뉴/배정 =====
class SysMenu(Base):
    """메뉴 마스터 — parent_menu_id 재귀 트리(N뎁스 DB, 2뎁스 운영). 설계서 §3.1."""
    __tablename__ = "sys_menu"
    menu_id = Column(String, primary_key=True, default=uid)
    parent_menu_id = Column(String, index=True)        # NULL = 1뎁스
    menu_code = Column(String, unique=True, index=True)  # AUDIT·MOCK_AUDIT (route 연결 키)
    menu_depth = Column(Integer, default=1)            # 파생 캐시값
    menu_type = Column(String, default="screen")       # screen | folder | link
    route_path = Column(String)                        # 화면 경로(folder는 NULL)
    icon_name = Column(String)
    permission_code = Column(String)
    default_sort_order = Column(Integer, default=0)
    use_yn = Column(Boolean, default=True)             # 사용 중지 = 신규 배정 금지
    required_yn = Column(Boolean, default=False)       # 필수 메뉴 = 제거 금지
    system_admin_yn = Column(Boolean, default=False)   # 관리자 전용 = 일반 역할 배정 제한
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class SysMenuI18n(Base):
    """메뉴명 다국어 — ko/id/en. 설계서 §3.2."""
    __tablename__ = "sys_menu_i18n"
    id = Column(String, primary_key=True, default=uid)
    menu_id = Column(String, index=True, nullable=False)
    language_code = Column(String, nullable=False)     # ko | id | en
    menu_name = Column(String)
    description = Column(String)


class SysRoleMenu(Base):
    """역할 기본 메뉴 배정 — 설계서 §3.3. sort_order=같은 부모 내 순서."""
    __tablename__ = "sys_role_menu"
    id = Column(String, primary_key=True, default=uid)
    role_id = Column(String, index=True, nullable=False)  # ROLE_AUDITOR 등
    menu_id = Column(String, nullable=False)
    sort_order = Column(Integer, default=0)
    visible_yn = Column(Boolean, default=True)


class SysUserMenu(Base):
    """사용자별 메뉴 배정/예외 — 설계서 §3.4. override_type=ADD|REMOVE|ORDER."""
    __tablename__ = "sys_user_menu"
    id = Column(String, primary_key=True, default=uid)
    user_id = Column(String, index=True, nullable=False)
    menu_id = Column(String, nullable=False)
    sort_order = Column(Integer, default=0)
    visible_yn = Column(Boolean, default=True)
    override_type = Column(String)                     # ADD | REMOVE | ORDER
    created_at = Column(DateTime, default=datetime.utcnow)


class SysOrgMenu(Base):
    """기관별 메뉴 배정 — 설계서 §3.5. 병합 순서: 역할 < 기관 < 사용자."""
    __tablename__ = "sys_org_menu"
    id = Column(String, primary_key=True, default=uid)
    org_id = Column(String, index=True, nullable=False)
    menu_id = Column(String, nullable=False)
    sort_order = Column(Integer, default=0)
    visible_yn = Column(Boolean, default=True)


class SysMenuPermission(Base):
    """메뉴 기능 권한(메뉴 노출과 분리) — 설계서 §3.6·§9."""
    __tablename__ = "sys_menu_permission"
    id = Column(String, primary_key=True, default=uid)
    assignment_type = Column(String, nullable=False)   # ROLE | ORG | USER
    target_id = Column(String, index=True, nullable=False)
    menu_id = Column(String, nullable=False)
    can_view = Column(Boolean, default=True)
    can_create = Column(Boolean, default=False)
    can_update = Column(Boolean, default=False)
    can_delete = Column(Boolean, default=False)
    can_submit = Column(Boolean, default=False)
    can_approve = Column(Boolean, default=False)
    can_sign = Column(Boolean, default=False)
    can_download = Column(Boolean, default=False)


class ApprovalRequest(Base):
    """2인 승인(maker-checker) — 인증서 발급/철회 등 민감 행위. 설계서 보강안 §4.3.
    maker가 요청(pending) → checker(다른 사람)가 승인 시 실제 실행. self-approval 금지."""
    __tablename__ = "approval_request"
    id = Column(String, primary_key=True, default=uid)
    action_type = Column(String, nullable=False, index=True)   # certificate.issue | certificate.revoke ...
    case_id = Column(String, index=True)
    org_id = Column(String, index=True)
    payload = Column(JSON, default=dict)          # 실행 파라미터(reason 등)
    reason = Column(String)                        # maker 요청 사유
    status = Column(String, default="pending", index=True)   # pending | approved | rejected
    requested_by = Column(String, nullable=False)  # maker uid
    requester_role = Column(String)
    requester_name = Column(String)
    decided_by = Column(String)                    # checker uid
    decider_role = Column(String)
    decider_name = Column(String)
    decision_reason = Column(String)
    result = Column(JSON)                          # 실행 결과(certificate_no 등)
    created_at = Column(DateTime, default=datetime.utcnow)
    decided_at = Column(DateTime)


class ConsultantInvite(Base):
    """컨설턴트 초대 코드 — 영업한 업체를 자기 담당으로 들이는 통로.

    컨설턴트가 클라이언트 계정을 대신 만들면 남의 자격증명을 다루게 된다. 코드를 주고
    업체가 직접 가입하면 그 문제가 없고, '누가 누구를 데려왔는지'가 기록으로 남는다."""
    __tablename__ = "consultant_invite"
    invite_id = Column(String, primary_key=True, default=uid)
    code = Column(String, unique=True, index=True, nullable=False)
    consultant_id = Column(String, index=True, nullable=False)
    company_name = Column(String)      # 영업 대상 업체명(참고용) — 가입 시 업체가 정정 가능
    note = Column(Text)
    max_uses = Column(Integer, default=1)
    used_count = Column(Integer, default=0)
    # 명함·QR 에 박는 대표 코드인가. 가입 시 1회 자동 발급되며 만료·횟수 제한이 없다.
    # 기간 한정 코드(기존 /consultant/invites)와 구분하려고 둔다.
    is_primary = Column(Boolean, default=False)
    expires_at = Column(DateTime)
    revoked_at = Column(DateTime)      # 회수된 코드는 다시 쓸 수 없다
    created_at = Column(DateTime, default=datetime.utcnow)


class ConsultantProfile(Base):
    """컨설턴트 정보 — 영업 인센티브 정산에 필요한 신원·연락처·계좌.

    수수료율은 기본값을 두지 않는다. 계약마다 다를 수 있고, 시스템이 임의로 정한 숫자로
    돈이 나가면 안 된다. 운영자가 명시적으로 넣어야 실적이 금액으로 환산된다."""
    __tablename__ = "consultant_profile"
    consultant_id = Column(String, primary_key=True)     # app_user.user_id
    display_name = Column(String)          # 대외 표기명(개인 또는 법인)
    company_name = Column(String)          # 소속 컨설팅사(개인이면 비움)
    biz_reg_no = Column(EncryptedType)     # 사업자등록번호 — 세금계산서·정산
    phone = Column(EncryptedType)
    email = Column(EncryptedType)
    address = Column(EncryptedType)
    bank_name = Column(String)
    bank_account = Column(EncryptedType)   # 계좌번호 — 지급 대상
    account_holder = Column(String)
    commission_rate = Column(Float)        # % — 운영자가 설정. 비면 금액 환산 안 함
    contract_note = Column(Text)           # 정산 조건 메모(지급주기·예외 등)
    status = Column(String, default="active")   # active|suspended
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime)


class ConsultantPayout(Base):
    """수수료 지급 기록 — 언제 얼마를 무엇에 대해 지급했는가.

    실적은 결제 완료된 인보이스에서 그때그때 계산한다(규칙이 바뀌어도 과거가 흔들리지
    않게). 반면 '지급했다'는 사실은 계산이 아니라 기록이라 남긴다."""
    __tablename__ = "consultant_payout"
    payout_id = Column(String, primary_key=True, default=uid)
    consultant_id = Column(String, index=True, nullable=False)
    period_from = Column(String)      # ISO date — 정산 대상 기간
    period_to = Column(String)
    base_amount = Column(Float)       # 정산 근거 매출 합계
    rate = Column(Float)              # 적용 수수료율(%) — 지급 시점 값을 박아둔다
    amount = Column(Float)            # 실지급액
    currency = Column(String, default="IDR")
    invoice_ids = Column(JSON)        # 어느 인보이스가 근거인지 — 되짚을 수 있어야 한다
    status = Column(String, default="pending")   # pending|paid|cancelled
    paid_at = Column(DateTime)
    note = Column(Text)
    created_by = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


class BoardPost(Base):
    """홈페이지 상담 게시판 — 가입 없이 남기고, 비밀번호로 다시 본다.

    가입을 요구하면 문의 자체가 줄어든다. 그렇다고 공개로 두면 어느 업체가 무슨 원료로
    고민 중인지 경쟁사에 그대로 보인다 — 그래서 **전부 비공개**다.
    볼 수 있는 사람은 (1) 비밀번호를 아는 글쓴이 (2) 로그인한 컨설턴트·오디터뿐이다.

    비밀번호는 계정 비밀번호와 같은 방식(PBKDF2)으로 저장한다. 짧은 글 비번이라도
    평문으로 두면 유출 시 다른 서비스 비번까지 유추당한다."""
    __tablename__ = "board_post"
    post_id = Column(String, primary_key=True, default=uid)
    # 사람이 부르는 글번호 — 20260912-00001 꼴. 하루 단위로 1부터 센다.
    # 내부 키(post_id)는 32자리 그대로 둔다: 이미 답글·감사로그가 그걸 가리키고 있고,
    # 기본키를 갈아엎으면 그 연결이 전부 끊긴다. 화면과 조회에는 이 번호를 쓴다.
    post_no = Column(String, unique=True, index=True)
    title = Column(String, nullable=False)
    body = Column(Text, nullable=False)
    author_name = Column(String, nullable=False)
    contact = Column(String, nullable=False)      # 답변 받을 연락처(전화/이메일)
    password_hash = Column(String, nullable=False)
    # QR 로 들어온 문의는 그 영업자 건으로 남긴다 — 수수료·성과 근거
    ref_code = Column(String, index=True)
    edited_at = Column(DateTime)          # 수정 시각(수정 이력은 감사로그)
    consultant_id = Column(String, index=True)
    status = Column(String, default="open")       # open | answered | closed
    ip_hash = Column(String)                      # 도배 차단용. 원문 IP 는 남기지 않는다
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BoardReply(Base):
    """게시글 답변 — 로그인한 직원만 쓴다."""
    __tablename__ = "board_reply"
    reply_id = Column(String, primary_key=True, default=uid)
    post_id = Column(String, index=True, nullable=False)
    # 글쓴이(익명)가 되물을 수도 있다 — 그때는 author_id 가 없다
    # 글쓴이 답글이면 빈 문자열. NULL 이 아니다 — 기존 테이블에 NOT NULL 이 걸려 있고
    # SQLite 는 컬럼 제약을 나중에 풀 수 없다. 판별은 author_role=='client' 로 한다.
    author_id = Column(String, nullable=False, default="")
    author_role = Column(String)                 # 직원 역할 또는 'client'
    author_name = Column(String)                 # 글쓴이 답글의 표시 이름
    parent_reply_id = Column(String, index=True) # 대댓글 — 어느 답글에 달았는지
    body = Column(Text, nullable=False)
    edited_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
