"""할랄 인증번호 검증(REQ-CERT-001).

번호가 적혀 있다는 사실만으로 의심(mushbooh)을 할랄로 올리면 안 된다.
발급기관 식별·형식·뒷받침 문서·유효기간을 단계로 확인하고 판정만 돌려준다.
승격 여부는 호출자가 정한다.
"""

import re
from datetime import date, datetime

VERDICTS = (
    "VERIFIED",
    "EXPIRED",
    "MISMATCH",
    "UNVERIFIED_NO_DOCUMENT",
    "INVALID_FORMAT",
    "ABSENT",
)

# 실사용 데이터와 BPJPH/MUI 스킴에서 도출한 발급기관 패턴.
_PATTERNS = [
    (re.compile(r"^ID\d{17}$", re.IGNORECASE), "BPJPH",
     "인도네시아 국가 할랄 인증번호(ID + 17자리)"),
    (re.compile(r"^LPPOM-\d{8,20}$", re.IGNORECASE), "MUI LPPOM",
     "MUI LPPOM 인증번호"),
    (re.compile(r"^[A-Z]{2,5}[-.][A-Z0-9.\-]{6,}$", re.IGNORECASE), "issuer-prefixed",
     "발급기관 약어 + 구분자 + 번호"),
]

_NUM_IN_TEXT = re.compile(r"[A-Z]{2,6}[-.]?\d{6,20}|ID\d{17}", re.IGNORECASE)
_DIGITS_ONLY = re.compile(r"\d{1,20}")


def normalize(cert_no):
    """인증번호를 비교 가능한 형태로 정규화한다."""
    if cert_no is None:
        return ""
    text = str(cert_no).strip().upper()
    return re.sub(r"\s+", "", text)


def classify_format(cert_no):
    """발급기관 식별과 형식 판정을 돌려준다."""
    n = normalize(cert_no)
    if not n:
        return {"ok": False, "issuer": None, "reason": "인증번호가 없다"}
    for pattern, issuer, note in _PATTERNS:
        if pattern.match(n):
            return {"ok": True, "issuer": issuer, "reason": note}
    if _DIGITS_ONLY.fullmatch(n):
        return {"ok": False, "issuer": None,
                "reason": "숫자만으로는 발급기관을 식별할 수 없다(REQ-CERT-002)"}
    if len(n) < 8:
        return {"ok": False, "issuer": None, "reason": "번호가 너무 짧다"}
    return {"ok": True, "issuer": None, "reason": "형식 미등록 — 발급기관 확인 필요"}


def _is_halal_cert(doc):
    dt = doc.get("doc_type")
    return dt is not None and str(dt).strip().lower() == "halal_certificate"


def _doc_numbers(docs):
    """할랄 인증서 문서에서 후보 번호를 모아 (정규화 번호, 문서) 쌍으로 돌려준다."""
    pairs, seen = [], set()
    for doc in docs or []:
        if not isinstance(doc, dict) or not _is_halal_cert(doc):
            continue
        candidates = []
        fields = doc.get("fields") or {}
        if isinstance(fields, dict):
            value = fields.get("cert_no")
            if value is not None and str(value).strip():
                candidates.append(value)
        text = doc.get("text")
        if text:
            candidates.extend(_NUM_IN_TEXT.findall(str(text)))
        for candidate in candidates:
            n = normalize(candidate)
            if n and n not in seen:
                seen.add(n)
                pairs.append((n, doc))
    return pairs


def _expiry_of(doc):
    """문서의 유효기간을 date 로 읽는다. 실패하면 None."""
    if not isinstance(doc, dict):
        return None
    fields = doc.get("fields") or {}
    if not isinstance(fields, dict):
        return None
    raw = fields.get("expiry_date")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def verify(cert_no, docs=None, today=None):
    """인증번호를 형식·문서·유효기간 순으로 확인해 판정만 돌려준다."""
    if today is None:
        today = date.today()
    n = normalize(cert_no)

    result = {"verdict": "ABSENT", "issuer": None, "normalized": n,
              "reason": "", "matched_document": None, "expiry": None}

    if not n:
        result["reason"] = "인증번호가 적혀 있지 않다"
        return result

    fmt = classify_format(n)
    result["issuer"] = fmt["issuer"]
    if not fmt["ok"]:
        result["verdict"] = "INVALID_FORMAT"
        result["reason"] = fmt["reason"]
        return result

    pairs = _doc_numbers(docs)
    if not pairs:
        result["verdict"] = "UNVERIFIED_NO_DOCUMENT"
        result["reason"] = "번호는 적혀 있으나 할랄 인증서 문서가 제출되지 않았다"
        return result

    matched = None
    for number, doc in pairs:
        if number == n:
            matched = doc
            break
    if matched is None:
        result["verdict"] = "MISMATCH"
        result["reason"] = "제출된 할랄 인증서의 번호와 일치하지 않는다"
        return result

    result["matched_document"] = matched
    expiry = _expiry_of(matched)
    if expiry is not None:
        result["expiry"] = expiry.isoformat()
        if expiry < today:
            result["verdict"] = "EXPIRED"
            result["reason"] = "인증서 유효기간이 지났다"
            return result

    result["verdict"] = "VERIFIED"
    result["reason"] = "할랄 인증서 문서로 번호가 확인됐다"
    return result


def promotes(result):
    """VERIFIED 판정일 때만 mushbooh 원재료를 halal 로 올릴 수 있다."""
    return result.get("verdict") == "VERIFIED"


MODES = ("off", "format", "strict")


def may_promote(result, mode="off"):
    """인증 판정 결과를 승격 여부로 변환한다.

    off     기존 동작 — 번호가 있으면 승격한다(판정은 기록만).
    format  형식 불량(INVALID_FORMAT)만 차단한다. 발급기관을 식별할 수 없는 번호로는 올리지 않는다.
    strict  VERIFIED 만 승격한다(REQ-CERT-001 완전 적용).

    알 수 없는 mode 값은 의도적으로 "off" 로 떨어진다. 환경변수 오타가 인증 판정을
    조용히 바꾸면 안 되기 때문이다.
    """
    verdict = result.get("verdict")
    if mode == "strict":
        return verdict == "VERIFIED"
    if mode == "format":
        return verdict != "INVALID_FORMAT"
    return True
