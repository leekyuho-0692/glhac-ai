"""요청 스키마 (Pydantic v2)."""
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class CaseCreate(BaseModel):
    org_id: Optional[str] = None   # 비-admin은 토큰의 org_id 사용
    company_name: Optional[str] = None
    is_msme: Optional[bool] = False
    actor_type: Optional[str] = "applicant"


class ProductCreate(BaseModel):
    name: str
    category: Optional[str] = None
    registration_type: Optional[str] = None


class ProductUpdate(BaseModel):
    category: Optional[str] = None
    registration_type: Optional[str] = None
    status: Optional[str] = None


class MaterialCreate(BaseModel):
    name: str
    e_number: Optional[str] = None
    mat_type: Optional[str] = None      # raw|additive|processing_aid|packaging|lubricant|sanitizer
    source: Optional[str] = None        # animal|plant|microbial|synthetic|mineral|unknown
    supplier: Optional[str] = None
    cert_no: Optional[str] = None
    note: Optional[str] = None
    evidence_provided: Optional[bool] = False
    source_known: Optional[bool] = True


class TextScreenReq(BaseModel):
    text: str


class ZipIntakeReq(BaseModel):
    zip_b64: str


class DocReviewReq(BaseModel):
    review_status: str   # approved|rejected|rework|pending


class SjphElementReq(BaseModel):
    element: str          # commitment|materials|process|product|monitoring
    status: str           # ok|gap|not_started
    note: Optional[str] = None


class FindingReq(BaseModel):
    area: Optional[str] = None
    finding: str
    severity: Optional[str] = "minor"   # major|minor|observation
    corrective_action: Optional[str] = None
    due_date: Optional[str] = None


class FindingStatusReq(BaseModel):
    status: str   # open|closed


class LphAssignReq(BaseModel):
    lph_name: str
    auditor_ref: Optional[str] = None
    source: Optional[str] = "manual"


class InvoiceReq(BaseModel):
    service_type: str            # pre_audit|onsite
    amount: float = Field(ge=0)  # 음수 청구 방지
    # P1-#1: 다항목 라인아이템(스키마 무변경 — WorkflowEvent payload에 저장). 각 항목
    # {name, qty, unit_price, amount}. 존재 시 amount(DPP)는 라인 합계로 산정.
    line_items: Optional[list] = None


class FatwaReturnReq(BaseModel):     # P1-#7 파트와→오디터 반려 루프
    reason: str                      # 반려 사유(필수)
    target: Optional[str] = None     # P2(G2): audit_closed(기본·오디터 재작업) | supplementation_required(클라이언트 보완)


class FatwaReq(BaseModel):
    decision: str                # approved|rejected|conditional|pending
    committee_note: Optional[str] = None
    committee_head: Optional[str] = None       # 위원장 — S3-2
    committee_secretary: Optional[str] = None  # 간사 — S3-2
    committee_members: Optional[list] = None   # 위원 목록 — S3-2
    product_scope: Optional[list] = None       # 파트와 대상 제품 ID 목록 — S3-2


class MockAuditDecisionReq(BaseModel):
    result: str                      # pass|reject
    reason: Optional[str] = None     # reject 시 필수


class MockAuditManualReq(BaseModel):
    decision: str                    # approve|reject
    comment: Optional[str] = None    # reject 시 필수(보완요청 사유)


class MockAuditEvidenceReq(BaseModel):
    section: str                     # MOCK_EVIDENCE_SECTIONS 키
    verdict: str                     # comply|nonconformity
    corrective_action: Optional[str] = None   # nonconformity 시 권장(빈값 허용)


class PreassessReviewReq(BaseModel):        # P0-3 사전심사 오디터 3섹션 검토
    sections: Dict[str, Any]                # {documents:{ok,note}, materials:{ok,note}, process:{ok,note}}
    verdict: str                            # ready|supplement
    note: Optional[str] = None


class PreassessDocRequestReq(BaseModel):    # P0-3 오디터 추가서류 요청·전송
    items: List[Dict[str, Any]]             # [{doc_type, note}]
    message: Optional[str] = None


class PreassessResubmitReq(BaseModel):      # P0-3 클라이언트 재제출
    note: Optional[str] = None


class AuditReportReturnReq(BaseModel):      # P0-4 오디터 보고서 보완 반려
    comment: str                            # 보완 요청 사유(필수)


class AuditReportResubmitReq(BaseModel):    # P0-4 클라이언트 재제출
    note: Optional[str] = None


class AuditReportReconfirmReq(BaseModel):   # P0-4 오디터 수정확인
    decision: str                           # ok|hold
    note: Optional[str] = None


class AuditReportSignReq(BaseModel):        # P0-4 오디터 E-서명
    name: str                               # 서명자명(필수)


class OnsiteChecklistReq(BaseModel):
    item_key: str
    result: str          # not_checked|comply|nonconformity
    note: Optional[str] = None


class AuditorPoolReq(BaseModel):
    name: str
    cert_no: Optional[str] = None
    role_in_team: Optional[str] = "anggota"   # ketua|anggota


class LphReferenceReq(BaseModel):
    name: str
    accreditation_no: Optional[str] = None
    region: Optional[str] = None
    status: Optional[str] = "active"


class ChangeImpactReq(BaseModel):
    change_type: str             # supplier_changed|material_added|process_changed|material_source_changed


class AskReq(BaseModel):
    question: str


class ExplainReq(BaseModel):
    name: str
    e_number: Optional[str] = None
    source: Optional[str] = None
    note: Optional[str] = None
    llm: Optional[bool] = False


class MaterialEvidenceReq(BaseModel):
    evidence_type: str            # msds|coa|halal_certificate|supplier_declaration|process_flow|facility_photo
    file_b64: str
    filename: Optional[str] = "evidence"
    # 출처 기록과 증빙 충족은 다르다. 원산지증명서처럼 '어디서 온 자료인지'는 밝히지만
    # 요구 증빙(유래 선언·조성표 등)을 충족하지 않는 문서는 False로 붙여야 한다.
    # False면 파일은 원재료에 연결되되 재스크리닝·evidence_provided를 건드리지 않는다.
    counts_as_evidence: bool = True


class ProductPhotoReq(BaseModel):
    file_b64: str
    filename: Optional[str] = "photo"


class DocTypeReq(BaseModel):
    doc_type: str


class SjphEvidenceReq(BaseModel):
    item_key: str
    file_b64: str
    filename: Optional[str] = "evidence"


class DiscussionReq(BaseModel):
    kind: Optional[str] = "comment"   # add|repair|comment|resolved
    target: Optional[str] = None
    text: str


class ParseFileReq(BaseModel):
    doc_type: str           # nib_business_license|factory_registration|halal_certificate|quality_cert|consent|product_list|material_list|product_label
    file_b64: str
    filename: Optional[str] = "upload"


class MatrixLinkReq(BaseModel):
    product_id: str
    material_id: str
    linked: bool = True


class CaseProfileReq(BaseModel):
    company_name: Optional[str] = None
    nib: Optional[str] = None
    responsible_person: Optional[str] = None
    halal_supervisor: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    factory_reg_no: Optional[str] = None
    factory_address: Optional[str] = None
    due_date: Optional[str] = None   # 처리 목표 기한(ISO date) — 기한 경보
    notify_consent: Optional[bool] = None  # 알림 수신 동의
    profile_ext: Optional[dict] = None  # Company/Facility Info 확장 양식 필드(PIC·CP·등록유형·공장정보 등)


class PathwayConfirm(BaseModel):
    pathway: str  # self_declare | reguler
    override_reason: Optional[str] = None


class TransitionReq(BaseModel):
    to_state: str
    action: Optional[str] = "transition"
    actor_type: Optional[str] = "system"
    actor_id: Optional[str] = None


class PenyeliaCreate(BaseModel):
    name: str
    training_cert: Optional[str] = None
    cert_expiry: Optional[str] = None


class PenyeliaUpdate(BaseModel):
    status: str  # active | inactive


class PendampingAssign(BaseModel):
    pendamping_id: str


class PendampingVerify(BaseModel):
    decision: str  # verified | rejected | rework
    note: Optional[str] = None
    signature_ref: Optional[str] = None


class SihalalLink(BaseModel):
    external_username: Optional[str] = None
    external_email: Optional[str] = None
    external_application_no: Optional[str] = None


class SihalalVerify(BaseModel):
    expected_identifier: str  # GL-HAC 측 식별자(email/NIB) — 동일성 검증용


class OCRReq(BaseModel):
    image_path: str
    lang: Optional[str] = "korean"


class LabelJudgmentReq(BaseModel):
    image_path: str
    locale: Optional[str] = "ko-KR"


class LabelB64Req(BaseModel):
    image_b64: str
    filename: Optional[str] = "label.png"
    locale: Optional[str] = "ko-KR"


class LoginReq(BaseModel):
    username: str
    password: str


class RefreshReq(BaseModel):
    refresh_token: str


class RegisterReq(BaseModel):
    username: str
    password: str
    company_name: Optional[str] = None
    # 회원가입 AI OCR 자동추출 프로필(Rizky #1) — 초기 케이스에 프리필
    nib: Optional[str] = None
    responsible_person: Optional[str] = None
    address: Optional[str] = None
    factory_address: Optional[str] = None
    business_type: Optional[str] = None


class OCRExtractReq(BaseModel):
    image_b64: str
    doc_type: Optional[str] = "business_registration"  # business_registration|factory_registration


class AdminUserReq(BaseModel):
    username: str
    password: str
    role: str
    org_id: Optional[str] = "org_demo"


class AdminUserPatchReq(BaseModel):
    role: Optional[str] = None
    password: Optional[str] = None


class AdminOrgReq(BaseModel):
    org_id: str
    name: Optional[str] = None


class SeedResetReq(BaseModel):
    confirm: str   # "RESET" 필요


class CasePurgeReq(BaseModel):   # 심사데이터 초기화 — 감사 흔적을 남기는 API 경로
    keep: List[str] = []                    # 보존 화이트리스트(case_id). 나머지가 삭제 대상
    reason: Optional[str] = None            # 초기화 사유 — 감사로그에 남는다(실삭제 시 필수)
    dry_run: bool = True                    # 기본은 미리보기. 실삭제는 명시적으로 꺼야 한다
    confirm: Optional[str] = None           # 실삭제 시 "PURGE" 필요
    expect_delete: Optional[int] = None     # 삭제 예정 건수 — 미리보기와 다르면 중단(오조작 방지)
    vacuum: bool = True                     # 삭제 후 VACUUM — 빈 페이지에 남는 원본 잔상까지 회수
    sweep_uploads: bool = False             # GLHAC_UPLOAD_DIR 스테이징 파일 정리(케이스 귀속 불가라 기본 off)


class FacilityReq(BaseModel):
    name: str
    address: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    zip: Optional[str] = None
    reg_no: Optional[str] = None


class FacilitySelectReq(BaseModel):
    facility_ids: list = []   # 이 신청 대상 공장 ID 목록


class FacilityUpdateReq(BaseModel):
    name: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    zip: Optional[str] = None
    reg_no: Optional[str] = None
    profile_ext: Optional[dict] = None   # 제조업체명·전화·이메일·PIC 등 상세


class UnlockReq(BaseModel):
    reason: str = Field(min_length=5)   # 언락 사유 필수(감사 추적)


class CertStatusReq(BaseModel):
    reason: str = Field(min_length=5)   # 정지/철회/재개 사유 필수(감사·통지)


class RenewRequestReq(BaseModel):
    reason: Optional[str] = None


class IssueReq(BaseModel):
    reason: Optional[str] = None   # operator 발급 사유(감사 추적)


class AiReviewReq(BaseModel):
    reviewer_status: str            # accepted | overridden
    note: Optional[str] = None


class AuditPlanReq(BaseModel):
    lph_name: Optional[str] = None
    scheduled_date: str                       # ISO date
    scope: Optional[str] = None
    auditors: Optional[list] = None


class AuditPlanPatchReq(BaseModel):
    status: Optional[str] = None              # scheduled|completed|cancelled
    scheduled_date: Optional[str] = None
    note: Optional[str] = None


class FatwaVoteReq(BaseModel):
    member: str
    vote: str                                 # approve|reject|abstain
    note: Optional[str] = None


class CarSubmitReq(BaseModel):
    description: str
    evidence: Optional[str] = None
    due_date: Optional[str] = None


class CarReviewReq(BaseModel):
    status: str                               # accepted|rejected|closed
    note: Optional[str] = None


class IntegrationEventReq(BaseModel):
    event_type: str
    idempotency_key: str
    external_id: Optional[str] = None
    case_id: Optional[str] = None
    payload: Optional[dict] = None


class SihalalImportNumberReq(BaseModel):
    official_no: str                          # 공식 BPJPH 할랄번호(No. Ketetapan Halal)
    source: Optional[str] = None              # manual|callback (기본 manual)


class InvoiceStatusReq(BaseModel):
    status: str
    reason: Optional[str] = None


class PaymentReq(BaseModel):
    method: str                               # bank_transfer|va|card|manual
    amount: Optional[float] = None
    depositor_name: Optional[str] = None
    reference: Optional[str] = None


class DepositReq(BaseModel):
    bank_name: Optional[str] = None
    account_no: Optional[str] = None
    depositor_name: Optional[str] = None
    amount: float = Field(ge=0)
    ref_memo: Optional[str] = None


class MatchDecisionReq(BaseModel):
    decision: str          # approved|held|rejected


class ReturnReq(BaseModel):
    reason: str            # 신청서 반려 사유


class GeoReq(BaseModel):
    lat: float             # 위도
    lng: float             # 경도
    source: Optional[str] = "browser"   # browser|manual


class RefundReq(BaseModel):
    amount: Optional[float] = None    # 미지정 시 인보이스 전액
    reason: str                       # 환불 사유


class RefundDecideReq(BaseModel):
    decision: str                     # approved|rejected
    note: Optional[str] = None


class FeedbackReq(BaseModel):
    title: str
    body: Optional[str] = ""
    category: Optional[str] = "improvement"   # improvement|bug|question|other


class FeedbackImageReq(BaseModel):
    file_b64: str
    filename: Optional[str] = "feedback.png"


class FeedbackStatusReq(BaseModel):
    status: str   # open|reviewing|resolved|wontfix


class FeedbackCommentReq(BaseModel):
    body: str


class MaterialRenameReq(BaseModel):
    name: str          # OCR 오독 교정용 원재료명


# ── M01 최고운영자 운영현황(P0-3차) — 신규 업체 승인/거절·오디터 배정 ──
class OpsRejectReq(BaseModel):
    reason: str                       # 신규 업체 거절 사유(필수)


class OpsAssignAuditorReq(BaseModel):
    auditor_id: str                   # 배정 대상 오디터(app_user.user_id, role=auditor)


# ── P0-4차: M06 규정·법령 관리 — 스키마 무변경(WorkflowEvent latest-wins) ──
class RegulationUpsertReq(BaseModel):
    title: str                              # 법령/규정 제목(필수)
    reg_number: Optional[str] = None        # 법령번호
    effective_date: Optional[str] = None    # 시행일(YYYY-MM-DD 문자열)
    category: Optional[str] = None          # 분류(law|regulation|fatwa|standard|guideline)
    summary: Optional[str] = None           # 본문요약
    impact_stages: List[str] = Field(default_factory=list)     # 영향 심사단계 키 다중선택
    impact_sections: List[str] = Field(default_factory=list)   # 영향 증거섹션 키 다중선택


class RegulationTransitionReq(BaseModel):
    to_state: str                           # draft|review|effective|retired
    reason: Optional[str] = None            # 상태전이 사유(발효/폐지 시 권장)


class AuditorProfileReq(BaseModel):
    specialty: Optional[str] = None                            # 전문분야(예: 식품 전문)
    languages: List[str] = Field(default_factory=list)         # 지원언어 키 다중(id|en|ar 등)


class MenuAssignReq(BaseModel):     # 메뉴 배정 저장 — 설계서 §5·§8
    menus: List[Dict[str, Any]] = Field(default_factory=list)  # [{menuId, sortOrder, children:[{menuId, sortOrder}]}]


class MenuPermissionReq(BaseModel):   # 메뉴 기능 권한 — 설계서 §3.6·§9
    permissions: Dict[str, Dict[str, bool]] = Field(default_factory=dict)  # {menu_id: {view,create,update,delete,submit,approve,sign,download}}


class ApprovalDecisionReq(BaseModel):   # 2인 승인 결정 — 설계서 보강안 §4.3
    reason: Optional[str] = None            # 승인/거절 사유(거절 시 권장)


class CommitteeDecisionReq(BaseModel):   # 자기선언 위원회 검증 결정(SEHATI)
    decision: str                           # approve | reject
    reason: Optional[str] = None            # 결정 근거(반려 시 필수)
