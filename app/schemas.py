"""요청 스키마 (Pydantic v2).

## 왜 여기서 막는가

종전에는 입력을 거의 그대로 받고 마지막 상태 전이 가드에서만 걸렀다. 그래서 잘못된 값이
DB 에 들어간 뒤 한참 지나 다른 화면에서 터지거나, 아예 안 터지고 인증서에 인쇄됐다.
대표적으로 날짜는 문자열로 그냥 저장돼, 나중에 `date.fromisoformat()` 으로 읽는 쪽이
죽었다(만료 경보·인증서 유효성 판정). 값이 들어오는 자리에서 막는다.

원재료 유형·출처 코드는 **사전(domain_dict)이 정본**이다. 여기서 목록을 다시 적지 않는다.
"""
import re
from datetime import date as _date
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator, model_validator

from . import domain_dict as _dd

# ── 공용 검증기 ────────────────────────────────────────────────────────────
_PW_MIN = 8


def _text_required(v, what):
    """필수 문자열 — 공백만 넣은 것은 안 넣은 것이다."""
    if v is None:
        return v
    t = str(v).strip()
    if not t:
        raise ValueError("%s은(는) 비워 둘 수 없습니다" % what)
    return t


def _iso_date(v, what):
    """ISO 날짜만 통과하고, **저장 형식을 YYYY-MM-DD 로 통일**한다. 빈 값은 미입력.

    파이썬 3.11 의 date.fromisoformat 은 '20260910' 이나 '2026-W37-1' 같은 축약형도
    받는다(실측). 그대로 저장하면 화면·보고서·엑셀마다 다른 모양이 찍히고, 문자열로
    비교하는 코드가 조용히 어긋난다. 파싱한 뒤 표준형으로 되돌려 저장한다."""
    if v is None:
        return None
    t = str(v).strip()
    if not t:
        return None
    try:
        return _date.fromisoformat(t).isoformat()
    except ValueError:
        raise ValueError("%s은(는) YYYY-MM-DD 형식이어야 합니다(받은 값: %s)" % (what, t))


def _password(v):
    """최소한의 비밀번호 정책.

    특수문자 강제 같은 규칙은 넣지 않았다 — 인니 중소업체가 쓰는 화면이라 못 외우는 규칙은
    포스트잇으로 끝난다. 대신 '너무 짧다·한 글자 반복'처럼 명백히 위험한 것만 막는다."""
    t = str(v or "")
    if len(t) < _PW_MIN:
        raise ValueError("비밀번호는 %d자 이상이어야 합니다" % _PW_MIN)
    if len(set(t)) == 1:
        raise ValueError("비밀번호가 같은 글자의 반복입니다")
    return t


def _future_date(v, what):
    """오늘 이후만 허용(오늘 포함). 형식 검사는 _iso_date 가 이미 했다."""
    t = _iso_date(v, what)
    if t and _date.fromisoformat(t) < _date.today():
        raise ValueError("%s은(는) 지난 날짜입니다(받은 값: %s)" % (what, t))
    return t


# 사업자 식별번호 — 이 서비스는 **한국 업체가 인도네시아 할랄 인증을 받는** 흐름이다.
# 실제 데이터가 그렇다(126-81-67748 같은 한국 사업자등록번호 10자리). 그래서
# '인니 NIB 13자리'만 받으면 실사용 값이 전부 거부된다.
#   · 한국 사업자등록번호 10자리 · 인도네시아 NIB 13자리 — 둘 다 받는다.
# 붙임표는 실측에서 en-dash(–)가 섞여 있었다(PDF 복사). 눈으로는 같아 보여서
# 대조·중복검사가 조용히 어긋난다 — 저장 전에 보통 붙임표로 통일한다.
_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\uff0d\u2212"
NIB_DIGIT_LENGTHS = (10, 13)


def normalize_nib(v):
    """붙임표·공백 정규화. 값 판단은 하지 않는다(조회·대조용 공용 함수)."""
    if v is None:
        return None
    t = str(v).strip()
    for d in _DASHES:
        t = t.replace(d, "-")
    return " ".join(t.split())


def _nib(v):
    if v is None or str(v).strip() == "":
        return None
    t = normalize_nib(v)
    digits = "".join(ch for ch in t if ch.isdigit())
    if len(digits) not in NIB_DIGIT_LENGTHS:
        raise ValueError(
            "사업자 식별번호 자릿수가 맞지 않습니다(받은 값: %s · 숫자 %d자리 · "
            "허용: 한국 사업자등록번호 10자리 또는 인도네시아 NIB 13자리)" % (t, len(digits)))
    return t


# 이메일 — 사람이 손으로 적는 값이라 지나치게 엄격하면 멀쩡한 주소를 막는다.
# '@ 앞뒤가 있고 도메인에 점이 있다' 정도만 본다(RFC 전체 구현은 득보다 실이 크다).
_EMAIL_RE = __import__("re").compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def _email(v):
    if v is None or str(v).strip() == "":
        return None
    t = str(v).strip()
    if not _EMAIL_RE.match(t):
        raise ValueError("이메일 형식이 올바르지 않습니다(받은 값: %s)" % t)
    return t


def normalize_phone(p):
    """전화 정규화 — 인니(+62) 기본. 이미 +면 유지, 0 시작이면 +62로 치환.

    실데이터는 '+82-10-4928-1733'(붙임표 있음)과 '+821049281733'(없음)이 섞여 있었다.
    저장은 붙임표 없는 E.164 로 통일한다."""
    if not p:
        return p
    s = "".join(ch for ch in str(p) if ch.isdigit() or ch == "+")
    if s.startswith("+"):
        return s
    if s.startswith("0"):
        return "+62" + s[1:]
    if s.startswith("62"):
        return "+" + s
    return "+" + s if s else s


def _phone(v):
    if v is None or str(v).strip() == "":
        return None
    t = normalize_phone(v)
    digits = "".join(ch for ch in t if ch.isdigit())
    # E.164: 국가번호 포함 최대 15자리. 아래로는 대표번호(예: +82 2 xxx xxxx)까지 고려해 7자리.
    if not (7 <= len(digits) <= 15):
        raise ValueError("전화번호 자릿수가 올바르지 않습니다(받은 값: %s · 숫자 %d자리)"
                         % (v, len(digits)))
    return t


def _non_negative_int(v, what, limit=None):
    """수량·금액 — 음수는 값이 아니라 오타다. 빈 값은 미입력."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        n = int(str(v).strip().replace(",", ""))
    except (TypeError, ValueError):
        raise ValueError("%s은(는) 숫자여야 합니다(받은 값: %s)" % (what, v))
    if n < 0:
        raise ValueError("%s은(는) 음수일 수 없습니다(받은 값: %s)" % (what, n))
    if limit is not None and n > limit:
        raise ValueError("%s이(가) 너무 큽니다(받은 값: %s)" % (what, n))
    return n


# 계정 아이디 — 로그인 키이자 감사로그·알림에 찍히는 이름이다. 공백이나 눈에 안 보이는
# 문자가 섞이면 '분명히 만들었는데 로그인이 안 되는' 상태가 된다.
# 실데이터 14개 계정은 전부 영숫자·밑줄이라 이 규칙에 걸리는 기존 계정은 없다.
_USERNAME_RE = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")


def _username(v):
    t = str(v or "").strip()
    if not _USERNAME_RE.match(t):
        raise ValueError("아이디는 영문·숫자로 시작하는 3~32자여야 합니다"
                         "(영문·숫자와 . _ - 만 사용, 받은 값: %s)" % (v or ""))
    return t


def _address(v, what="주소"):
    """주소는 인증서·보고서에 인쇄된다. 형식은 나라마다 달라 길이만 최소한으로 본다.

    실데이터는 28~137자(한국·인도네시아). 너무 엄격하면 멀쩡한 주소를 막는다."""
    if v is None or str(v).strip() == "":
        return None
    t = " ".join(str(v).split())
    if len(t) < 8:
        raise ValueError("%s가 너무 짧습니다 — 인증서에 인쇄되므로 전체 주소를 적어 주세요"
                         "(받은 값: %s)" % (what, t))
    return t


def _material_type(v):
    if v is None or str(v).strip() == "":
        return None
    code = _dd.material_type_code(v)
    if not code:
        raise ValueError("원재료 유형이 올바르지 않습니다(받은 값: %s · 허용: %s)"
                         % (v, ", ".join(_dd.MATERIAL_TYPE_CODES)))
    return code


def _material_source(v):
    if v is None or str(v).strip() == "":
        return None
    code = _dd.material_source_code(v)
    if not code:
        raise ValueError("원재료 출처가 올바르지 않습니다(받은 값: %s · 허용: %s)"
                         % (v, ", ".join(_dd.MATERIAL_SOURCE_CODES)))
    return code


class CaseCreate(BaseModel):
    org_id: Optional[str] = None   # 비-admin은 토큰의 org_id 사용
    company_name: Optional[str] = None
    is_msme: Optional[bool] = False
    actor_type: Optional[str] = "applicant"
    # 인증 종류 — 신청 맨 앞에서 신청자가 고른다. product=제품 / logistics=물류 서비스.
    scheme: Optional[str] = "product"
    logistics_scope: Optional[List[str]] = None  # 물류일 때 jasa 복수 선택

    @field_validator("scheme")
    @classmethod
    def _scheme_ok(cls, v):
        v = (v or "product").lower()
        if v not in ("product", "logistics"):
            raise ValueError("scheme은 product 또는 logistics여야 합니다")
        return v

    @model_validator(mode="after")
    def _logistics_scope_ok(self):
        if self.scheme == "logistics":
            allowed = {"penyimpanan", "pengemasan", "pendistribusian"}
            sel = [x for x in (self.logistics_scope or []) if x in allowed]
            if not sel:
                raise ValueError("물류 인증은 jasa(penyimpanan/pengemasan/pendistribusian) 최소 1개를 골라야 합니다")
            self.logistics_scope = sel
        else:
            self.logistics_scope = None   # 제품이면 물류 scope 무시
        return self


class ProductCreate(BaseModel):
    name: str
    category: Optional[str] = None
    description: Optional[str] = None    # 현장심사 보고서 제품표에 인쇄
    registration_type: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _v_name(cls, v):
        return _text_required(v, "제품명")


class ProductUpdate(BaseModel):
    category: Optional[str] = None
    description: Optional[str] = None
    registration_type: Optional[str] = None
    status: Optional[str] = None


class MaterialCreate(BaseModel):
    name: str
    e_number: Optional[str] = None
    mat_type: Optional[str] = None      # raw|additive|processing_aid|preservative|cleaning|lubricant|packaging
    source: Optional[str] = None        # animal|plant|microbial|synthetic|mineral|unknown
    supplier: Optional[str] = None
    manufacturer: Optional[str] = None  # 제조사(Produsen)
    origin: Optional[str] = None
    cert_no: Optional[str] = None
    note: Optional[str] = None
    evidence_provided: Optional[bool] = False
    source_known: Optional[bool] = True

    @field_validator("name")
    @classmethod
    def _v_name(cls, v):
        return _text_required(v, "원재료명")

    @field_validator("mat_type")
    @classmethod
    def _v_type(cls, v):
        return _material_type(v)

    @field_validator("source")
    @classmethod
    def _v_source(cls, v):
        return _material_source(v)


class MaterialPatch(BaseModel):
    """원재료 속성 수정 — 심사 중 제조사·인증번호가 뒤늦게 확인되는 일이 흔하다."""
    mat_type: Optional[str] = None
    source: Optional[str] = None
    supplier: Optional[str] = None
    manufacturer: Optional[str] = None
    origin: Optional[str] = None
    cert: Optional[str] = None
    cert_no: Optional[str] = None
    note: Optional[str] = None

    @field_validator("mat_type")
    @classmethod
    def _v_type(cls, v):
        return _material_type(v)

    @field_validator("source")
    @classmethod
    def _v_source(cls, v):
        return _material_source(v)


class VehicleReq(BaseModel):
    plate_no: Optional[str] = None
    vehicle_type: Optional[str] = None      # truck|van|container|tanker|reefer
    transport_type: Optional[str] = None    # ambient|chilled|frozen|insulated
    capacity: Optional[str] = None
    reg_no: Optional[str] = None
    previous_cargo: Optional[str] = None
    previous_cargo_halal: Optional[bool] = None
    last_cleaned: Optional[str] = None
    sertu: Optional[bool] = None
    note: Optional[str] = None
    case_id: Optional[str] = None


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

    @field_validator("due_date")
    @classmethod
    def _v_due(cls, v):
        return _iso_date(v, "조치 기한")


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
    lang: Optional[str] = "ko"   # ko|en|id — 안 보내면 인니어 화면에 한국어 판정문이 뜬다


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
    scheme: Optional[str] = None            # 인증 종류 변경(신청 단계) — product|logistics
    logistics_scope: Optional[List[str]] = None  # 물류 jasa 선택

    @field_validator("scheme")
    @classmethod
    def _v_scheme(cls, v):
        if v is None:
            return v
        v = v.lower()
        if v not in ("product", "logistics"):
            raise ValueError("scheme은 product 또는 logistics여야 합니다")
        return v

    @field_validator("company_name")
    @classmethod
    def _v_company(cls, v):
        return _text_required(v, "기업명")

    @field_validator("nib")
    @classmethod
    def _v_nib(cls, v):
        return _nib(v)

    @field_validator("email")
    @classmethod
    def _v_email(cls, v):
        return _email(v)

    @field_validator("phone")
    @classmethod
    def _v_phone(cls, v):
        return _phone(v)

    @field_validator("address")
    @classmethod
    def _v_addr(cls, v):
        return _address(v, "회사 주소")

    @field_validator("factory_address")
    @classmethod
    def _v_faddr(cls, v):
        return _address(v, "공장 주소")

    @field_validator("profile_ext")
    @classmethod
    def _v_ext(cls, v):
        """확장 양식의 정량 필드 — 종전에는 int() 실패를 조용히 넘겨 값이 사라졌다.
        자기선언 자격이 이 숫자로 갈리므로, 못 읽는 값은 말해 준다."""
        if not v:
            return v
        out = dict(v)
        for k, what, lim in (("annual_revenue", "연매출", None),
                             ("outlet_count", "매장 수", 100000),
                             ("employee_count", "직원 수", 1000000),
                             ("total_employee", "총 직원 수", 1000000)):
            if k in out:
                out[k] = _non_negative_int(out[k], what, lim)
        return out

    @field_validator("due_date")
    @classmethod
    def _v_due(cls, v):
        return _iso_date(v, "처리 기한")


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

    @field_validator("name")
    @classmethod
    def _v_name(cls, v):
        return _text_required(v, "할랄감독자 이름")

    @field_validator("cert_expiry")
    @classmethod
    def _v_expiry(cls, v):
        return _iso_date(v, "자격 만료일")


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
    _pw = field_validator("password")(classmethod(lambda cls, v: _password(v)))
    _nibv = field_validator("nib")(classmethod(lambda cls, v: _nib(v)))
    _un = field_validator("username")(classmethod(lambda cls, v: _username(v)))
    company_name: Optional[str] = None
    invite_code: Optional[str] = None   # 컨설턴트 초대 코드 — 유치 관계·수수료 근거
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

    @field_validator("username")
    @classmethod
    def _v_un(cls, v):
        return _username(v)

    @field_validator("password")
    @classmethod
    def _v_pw(cls, v):
        return _password(v)


class AdminUserPatchReq(BaseModel):
    role: Optional[str] = None
    password: Optional[str] = None

    @field_validator("password")
    @classmethod
    def _v_pw(cls, v):
        return None if v is None or str(v).strip() == "" else _password(v)


class AdminOrgReq(BaseModel):
    org_id: str
    name: Optional[str] = None


class OrgRenameReq(BaseModel):
    name: str      # 경로에 org_id 가 있으므로 이름만 받는다


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

    @field_validator("scheduled_date")
    @classmethod
    def _v_date(cls, v):
        # 새 일정은 지난 날짜일 수 없다. 수정(Patch)은 완료 기록 보정에도 쓰이므로 형식만 본다.
        return _future_date(v, "심사 예정일")


class AuditPlanPatchReq(BaseModel):
    status: Optional[str] = None              # scheduled|completed|cancelled
    scheduled_date: Optional[str] = None
    note: Optional[str] = None

    @field_validator("scheduled_date")
    @classmethod
    def _v_date(cls, v):
        return _iso_date(v, "심사 예정일")


class FatwaVoteReq(BaseModel):
    member: str
    vote: str                                 # approve|reject|abstain
    note: Optional[str] = None


class CarSubmitReq(BaseModel):
    description: str
    evidence: Optional[str] = None
    due_date: Optional[str] = None

    @field_validator("due_date")
    @classmethod
    def _v_due(cls, v):
        return _iso_date(v, "조치 기한")


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

    @field_validator("effective_date")
    @classmethod
    def _v_eff(cls, v):
        return _iso_date(v, "시행일")


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


# ── 컨설턴트(영업) ────────────────────────────────────────────────────────
class ConsultantCreate(BaseModel):
    username: str
    password: str
    display_name: Optional[str] = None
    company_name: Optional[str] = None
    biz_reg_no: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    bank_name: Optional[str] = None
    bank_account: Optional[str] = None
    account_holder: Optional[str] = None
    commission_rate: Optional[float] = None
    contract_note: Optional[str] = None


class ConsultantProfileReq(BaseModel):
    display_name: Optional[str] = None
    company_name: Optional[str] = None
    biz_reg_no: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    bank_name: Optional[str] = None
    bank_account: Optional[str] = None
    account_holder: Optional[str] = None
    commission_rate: Optional[float] = None   # 운영자만 변경 가능
    contract_note: Optional[str] = None
    status: Optional[str] = None


class InviteCreate(BaseModel):
    company_name: Optional[str] = None    # 영업 대상 업체명(참고)
    note: Optional[str] = None
    max_uses: Optional[int] = 1
    expires_days: Optional[int] = 30


class OrgConsultantReq(BaseModel):
    consultant_id: str        # user_id 또는 username
    reason: Optional[str] = None


class PayoutCreate(BaseModel):
    consultant_id: str
    period_from: str
    period_to: str
    note: Optional[str] = None


# ── 홈페이지 상담 게시판 ──────────────────────────────────────────────────
class _BoardPwRule:
    """글 비밀번호는 연락처와 같으면 안 된다.

    연락처는 목록에 일부가 보이고(마스킹) 명함·홈페이지로도 알 수 있다. 비밀번호를
    전화번호로 쓰면 '연락처로 찾기'가 곧 '내용 열기'가 된다 — 가림막이 사라진다.

    통째로 같은 것만 막으면 '12345678'(가운데·뒷자리 조합) 같은 게 빠져나간다.
    그래서 **4자리 이상 이어서 겹치면** 막는다 — 앞 4자리든 뒷 4자리든 가운데든.
    전부 막으면 쓸 수 있는 비번이 지나치게 줄어 그 선에서 끊는다.
    하이픈은 무시하고 숫자만 본다.
    """

    @model_validator(mode="after")
    def _pw_not_contact(self):
        pw = (getattr(self, "password", None) or "").strip()
        ct = (getattr(self, "contact", None) or "").strip()
        if not pw or not ct:
            return self
        if pw.lower() == ct.lower():
            raise ValueError("비밀번호를 연락처와 다르게 정해 주세요")
        d_ct = re.sub(r"[^0-9]", "", ct)
        if not d_ct:
            return self
        # 연락처 숫자와 **4자리 이상 이어서 겹치면** 거부한다.
        # 전부 막으면 쓸 수 있는 비번이 너무 줄고, 통째로만 막으면 '12345678' 같은
        # 조합이 빠져나간다. 섞인 비번('5678abcd')도 숫자 부분만 떼어 본다.
        def _hits(num):
            for i in range(len(num) - 3):
                if num[i:i + 4] in d_ct:
                    return True
            return False

        for run in re.findall(r"\d+", pw):
            if _hits(run):
                raise ValueError("비밀번호에 연락처 숫자를 4자리 이상 쓰지 말아 주세요")
        # '49-28-77' 처럼 구분기호로 끊어 쓰면 위 검사를 빠져나간다(실측).
        # 구분기호만 걷어내고 숫자만 남는 경우 한 번 더 본다.
        sep_free = re.sub(r"[\s\-./_]", "", pw)
        if sep_free.isdigit() and _hits(sep_free):
            raise ValueError("비밀번호에 연락처 숫자를 4자리 이상 쓰지 말아 주세요")
        return self


class BoardPostCreate(_BoardPwRule, BaseModel):
    """무가입 문의 — 연락처는 필수다(답변할 방법이 없으면 글이 무의미하고, 봇 차단도 된다)."""
    title: str
    body: str
    author_name: str
    contact: str
    password: str
    ref: Optional[str] = None       # QR 로 들어온 경우의 영업자 코드

    @field_validator("title")
    @classmethod
    def _v_title(cls, v):
        return _text_required(v, "제목")

    @field_validator("body")
    @classmethod
    def _v_body(cls, v):
        t = _text_required(v, "내용")
        if len(t) < 10:
            raise ValueError("내용을 10자 이상 적어 주세요")
        return t

    @field_validator("author_name")
    @classmethod
    def _v_name(cls, v):
        return _text_required(v, "이름")

    @field_validator("contact")
    @classmethod
    def _v_contact(cls, v):
        t = _text_required(v, "연락처")
        # 이메일이거나 전화번호여야 한다 — 답변을 보낼 수 있어야 하고, 봇 글도 걸러진다
        if _EMAIL_RE.match(t):
            return t
        digits = "".join(ch for ch in t if ch.isdigit())
        if 7 <= len(digits) <= 15:
            # 국가코드를 붙이지 않는다 — 이 게시판은 한국 업체가 쓰는데 normalize_phone 은
            # 인니(+62) 기준이라 '010-1234-5678' 이 '+621012345678' 이 된다(실측).
            # 사람이 보고 연락하는 값이라 적은 그대로 두는 편이 안전하다.
            return t
        raise ValueError("연락처는 이메일 또는 전화번호여야 합니다(받은 값: %s)" % t)

    @field_validator("password")
    @classmethod
    def _v_pw(cls, v):
        t = str(v or "")
        # 글 비밀번호는 계정 비번만큼 길 필요는 없지만, 4자면 1만 번이면 뚫린다
        if len(t) < 6:
            raise ValueError("글 비밀번호는 6자 이상이어야 합니다")
        return t


class BoardPostOpen(BaseModel):
    """글쓴이가 자기 글을 다시 열 때."""
    password: str


class BoardReplyCreate(BaseModel):
    """답글 — 직원은 토큰으로, 글쓴이는 글 비밀번호로 쓴다.

    password 가 오면 글쓴이의 되묻기로 본다(무가입 게시판이라 계정이 없다).
    parent_reply_id 가 오면 그 답글에 달리는 대댓글이다."""
    body: str
    password: Optional[str] = None
    parent_reply_id: Optional[str] = None

    @field_validator("body")
    @classmethod
    def _v_body(cls, v):
        return _text_required(v, "답변 내용")


class BoardPostEdit(BaseModel):
    """글 수정 — 글쓴이는 비밀번호로, 직원은 토큰으로. 빈 칸은 그대로 둔다."""
    password: Optional[str] = None
    title: Optional[str] = None
    body: Optional[str] = None
    author_name: Optional[str] = None
    contact: Optional[str] = None

    @field_validator("title")
    @classmethod
    def _v_title(cls, v):
        return v if v is None else _text_required(v, "제목")

    @field_validator("body")
    @classmethod
    def _v_body(cls, v):
        if v is None:
            return v
        t = _text_required(v, "내용")
        if len(t) < 10:
            raise ValueError("내용을 10자 이상 적어 주세요")
        return t

    @field_validator("author_name")
    @classmethod
    def _v_name(cls, v):
        return v if v is None else _text_required(v, "이름")

    @field_validator("contact")
    @classmethod
    def _v_contact(cls, v):
        if v is None:
            return v
        t = _text_required(v, "연락처")
        digits = re.sub(r"[^0-9]", "", t)
        if "@" in t:
            return _email(t)
        if not (7 <= len(digits) <= 15):
            raise ValueError("연락처는 이메일이거나 숫자 7~15자리여야 합니다")
        return t


class BoardFindReq(BaseModel):
    """연락처 + 글 비밀번호로 내 글 찾기.

    32자리 글번호를 받아적게 하는 건 무리다. 다른 기기에서도 자기 글을 찾을 수
    있어야 한다 — 폰으로 남기고 사무실 PC 에서 확인하는 게 보통이다.

    연락처만으로는 **목록만** 나온다 — 날짜와 답변 여부뿐이고 제목·내용은 없다.
    내용을 보려면 글 비밀번호가 있어야 한다. 연락처는 알아내기 쉽지만(명함·홈페이지)
    비밀번호는 글쓴이만 안다."""
    contact: str
    password: Optional[str] = None      # 없으면 목록만, 있으면 그 글을 바로 연다

    @field_validator("contact")
    @classmethod
    def _v_contact(cls, v):
        return _text_required(v, "연락처")


class BoardReplyEdit(BaseModel):
    """답글 수정 — 잘못 나간 답변은 고칠 수 있어야 한다."""
    body: str
    password: Optional[str] = None      # 글쓴이가 자기 답글을 고칠 때

    @field_validator("body")
    @classmethod
    def _v_body(cls, v):
        return _text_required(v, "답변 내용")


class BoardStatusReq(BaseModel):
    status: str      # open | answered | closed
