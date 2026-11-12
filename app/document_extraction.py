"""문서 AI 입력 예산과 원문 표기 정규화. 한도 초과는 부분 분석으로 기록한다."""
import os
import re
import unicodedata
import base64
import hashlib
import hmac
import json
import time
from contextvars import ContextVar
from functools import wraps

DOCUMENT_AI_MAX_CALLS = max(2, int(os.getenv("GLHAC_DOCUMENT_AI_MAX_CALLS", "8")))
PUBLIC_AI_MAX_CALLS = max(1, min(DOCUMENT_AI_MAX_CALLS - 1,
                               int(os.getenv("GLHAC_PUBLIC_AI_MAX_CALLS", "2"))))
_ACTIVE_BUDGET = ContextVar("document_ai_budget", default=None)


class DocumentBudget:
    def __init__(self, state=None, *, public=False):
        cap = PUBLIC_AI_MAX_CALLS if public else DOCUMENT_AI_MAX_CALLS
        self.limit = min(cap, (state or {}).get("limit", cap))
        self.used = max(0, (state or {}).get("used", 0))
        self.partial = bool((state or {}).get("partial"))
        self.gpt_read_spans = []  # 같은 문서의 분류·유형별 추출 동안만 공유하는 원문 위치

    @property
    def remaining(self):
        return max(0, self.limit - self.used)

    def take(self):
        if not self.remaining:
            self.partial = True
            return False
        self.used += 1
        return True

    def snapshot(self):
        return {"limit": self.limit, "used": self.used, "partial": self.partial}


def document_budget(fn):
    """분류·유형 재추출이 같은 호출 예산을 사용한다. 검증 큐에는 snapshot을 저장한다."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        token = _ACTIVE_BUDGET.set(DocumentBudget())
        try:
            return fn(*args, **kwargs)
        finally:
            _ACTIVE_BUDGET.reset(token)
    return wrapped


def current_budget(*, public=False):
    return _ACTIVE_BUDGET.get() or DocumentBudget(public=public)


class ChunkPlan(list):
    partial = False

# 단계별 기존 로컬 한도도 이 표에서만 관리한다.
INPUT_LIMITS = {
    "openai": {"classify": 40000, "parse": 40000, "signup": 40000, "verify": 40000, "verify_fields": 40000},
    "ollama": {"classify": 2000, "parse": 2500, "signup": 2500, "verify": 8000, "verify_fields": 1500},
}


def input_limit(provider, stage):
    return INPUT_LIMITS.get(provider, INPUT_LIMITS["ollama"])[stage]


def document_chunks(text, provider, stage, max_chunks=None):
    limit = input_limit(provider, stage)
    cap = DOCUMENT_AI_MAX_CALLS if max_chunks is None else max(0, max_chunks)
    if provider != "openai":
        plan = ChunkPlan([text[:limit]] if cap else [])
        plan.spans = [[(0, min(limit, len(text)))]] if cap else []
        plan.partial = len(text) > limit or not cap
        return plan
    spans, start = [], 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            boundary = max(text.rfind("\f", start + limit // 2, end),
                           text.rfind("\n", start + limit // 2, end))
            if boundary >= 0:
                end = boundary + 1
        spans.append((start, end))
        if end == len(text):
            break
        start = end - min(200, (end - start) // 4)  # 경계의 라벨/값도 함께 읽는다.
    spans = spans or [(0, 0)]
    partial = len(spans) > cap
    if partial:
        if cap == 1:
            plan = ChunkPlan([text[:limit // 2] + "\n[중간 원문 생략]\n" + text[-(limit // 2 - 30):]])
            plan.spans = [[(0, limit // 2), (len(text) - (limit // 2 - 30), len(text))]]
            plan.partial = True
            return plan
        spans = spans[:(cap + 1) // 2] + (spans[-(cap // 2):] if cap // 2 else [])
    plan = ChunkPlan(text[start:end] for start, end in spans)
    plan.spans = [[span] for span in spans]
    plan.partial = partial
    return plan


def company_key(value):
    name = unicodedata.normalize("NFKC", str(value or "")).casefold()
    name = re.sub(r"주\s*식\s*회\s*사|유\s*한\s*(?:책\s*임\s*)?회\s*사|\(\s*[주유]\s*\)", " ", name)
    name = re.sub(r"\bco(?:mpany)?[.,\s]+(?:ltd|limited)\b\.?", " ", name)
    name = re.sub(r"\b(?:p[.\s]*t|c[.\s]*v|tbk|inc|incorporated|limited|ltd)\b\.?", " ", name)
    name = re.sub(r"\bco[.,]?\s*$", " ", name)
    return re.sub(r"[\W_]+", "", name)


def normalized_text(value):
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    # 한국어·영문 병기된 날짜와 숫자 구분자 흔들림을 흡수한다.
    value = re.sub(r"(\d{4})\s*년(?:\s*\(year\))?\s*(\d{1,2})\s*월(?:\s*\(month\))?\s*(\d{1,2})\s*일(?:\s*\(day\))?",
                   lambda m: f"{m[1]}{int(m[2]):02d}{int(m[3]):02d}", value)
    value = re.sub(r"(?<!\d)(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})(?!\d)",
                   lambda m: f"{m[1]}{int(m[2]):02d}{int(m[3]):02d}", value)
    value = re.sub(r"(?<!\d)(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{4})(?!\d)",
                   lambda m: f"{m[3]}{int(m[2]):02d}{int(m[1]):02d}", value)
    value = re.sub(r"\bjl\.?\s*", "jalan ", value)
    return re.sub(r"[\W_]+", "", value)


def suspect_value(field, value):
    if not isinstance(value, str):
        return False
    compact = re.sub(r"\s+", "", value)
    if re.fullmatch(r"[():：\s]*(?:단체명|법인명|상호|본점)[():：\s]*", compact):
        return True
    return bool(field in ("company_name", "address", "factory_address") and re.search(
        r"(?:개업(?:연월일|년월일|일)|법인등록번호|담당부서|전자우편주소)|본점$", compact))


def issuer_phone(value, text):
    """번호가 나오는 각 위치의 발급기관 문맥을 검사한다(한 줄 OCR도 포함)."""
    digits = re.sub(r"\D", "", value or "")
    local = "0" + digits[2:] if digits.startswith(("82", "62")) else digits
    found = False
    for hit in re.finditer(r"\+?\d[\d() .\-–]{6,}\d", text):
        number = re.sub(r"\D", "", hit[0])
        if number.startswith(("82", "62")):
            number = "0" + number[2:]
        if number != local:
            continue
        found = True
        context = text[max(0, hit.start() - 220):hit.start()]
        agency = list(re.finditer(r"세\s*무\s*서|국\s*세\s*청|담\s*당\s*(?:부\s*서|자)|"
                                 r"tax\s*office|issuing\s*(?:office|authority)|담당\s*공무원", context, re.I))
        business = list(re.finditer(r"회사\s*(?:전화|연락처)|사업장\s*(?:전화|연락처)|"
                                   r"company\s*(?:phone|tel)|business\s*(?:phone|tel)", context, re.I))
        if not agency or (business and business[-1].start() > agency[-1].start()):
            return False
    return found


def extraction_receipt(fields, metadata, secret):
    """가입까지 출처를 전달하는 서명 영수증. 인증 토큰과 서명 영역을 분리한다."""
    keys = ("company_name", "company_name_en", "nib", "address", "responsible_person")
    selected = {k: fields[k] for k in keys if k in fields}
    meta = {k: metadata[k] for k in selected if k in metadata}
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + 1800,
        "fields": selected, "metadata": meta}, ensure_ascii=False).encode()).decode()
    signature = hmac.new(secret, ("ocr-extract-v1:" + payload).encode(), hashlib.sha256).hexdigest()
    return payload + "." + signature if len(payload) < 60000 else None


def receipt_metadata(receipt, submitted, secret):
    if not receipt:
        return {}
    try:
        payload, signature = receipt.rsplit(".", 1)
        expected = hmac.new(secret, ("ocr-extract-v1:" + payload).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return {}
        data = json.loads(base64.urlsafe_b64decode(payload))
        if data["exp"] < time.time():
            return {}
        # 사용자가 수정한 값을 AI가 낸 값으로 표시하지 않는다.
        return {k: v for k, v in data["metadata"].items()
                if data["fields"].get(k) == submitted.get(k)}
    except (ValueError, TypeError, KeyError):
        return {}
