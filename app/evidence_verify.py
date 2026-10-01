"""P29-lite: GPT 누락근거 탐색 (판정로직 재설계 리뷰 ⑤, 스코프 1차).

문제: 시스템이 라벨링한 근거만 GPT에 주면, 시스템이 추출하지 못한 근거는 GPT도 확인할 수
없다. 그래서 검증 입력(packet)에 '라벨된 근거'뿐 아니라 문서 전체 원문·OCR 라인·문서 목록을
담는다. GPT는 놓친 근거 후보를 document_id + 정확한 인용문으로 지목한다.

안티-환각(리뷰 ⑤ '좌표·원문 검증'): LLM이 인용한 문장이 실제로 그 문서 안에 존재하는지
대조한다. 존재하지 않으면 rejected(환각)로 남기고 채택하지 않는다.

이번 스코프 제외: 자동 재판정 루프. 확정 후보는 사람이 검토하도록 수동 플래그로 둔다
(재검증 횟수·종료조건이 필요한 루프는 후속 단계).
"""
import re

from . import ai_local
from . import screening
from .models import DocumentAsset, Material, EvidenceCandidate


def _norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _doc_text(doc):
    """문서에서 대조 가능한 텍스트 — 발췌 + OCR 라인. (전체 원문은 후속에서 content 재추출)"""
    parts = [doc.text_excerpt or ""]
    for ln in (doc.ocr_lines or []):
        if isinstance(ln, dict) and ln.get("text"):
            parts.append(ln["text"])
    return "\n".join(p for p in parts if p)


def build_packet(db, case_id):
    """검증 패킷 — 라벨된 근거만이 아니라 문서 전체 원문·목록·현재 판정을 담는다(리뷰 ⑤)."""
    docs = db.query(DocumentAsset).filter_by(case_id=case_id).all()
    mats = db.query(Material).filter_by(case_id=case_id).all()
    documents = [{
        "document_id": d.document_id,
        "doc_type": d.doc_type,
        "filename": d.filename,
        "text": _doc_text(d),
        "ocr_line_count": len(d.ocr_lines or []),
    } for d in docs]
    decisions = [{
        "material_id": m.material_id, "name": m.name,
        "screen_result": m.screen_result, "screen_status": m.screen_status,
        "cert_no": m.cert_no,
    } for m in mats]
    return {"case_id": case_id, "documents": documents, "decisions": decisions,
            "doc_texts": {d["document_id"]: d["text"] for d in documents}}


_SYS = (
    "당신은 할랄 인증 근거 검증자입니다. 아래에는 케이스의 '모든 문서 전체 원문'과 시스템의 "
    "'현재 판정'이 주어집니다. 시스템이 라벨링/추출하지 못했지만 판정에 영향을 줄 수 있는 "
    "'놓친 근거'를 찾으세요 — 원재료의 유래·원산지·제조공정·할랄 인증번호·도축 인증 등. "
    "반드시 실제 문서에 있는 문장만 인용하세요(지어내지 말 것). 각 근거는 그 문장이 있는 "
    "document_id 와 정확한 인용문(quote)을 함께 제시하세요. axis 는 다음 중 하나로 분류: "
    "ORIGIN(원산지)/DERIVATION(생물학적 유래)/DERIVATION_DETAIL(종·부위)/PROCESS(공정)/"
    "CARRIER(캐리어)/CERT(인증)/COMPOSITION(조성). "
    '반드시 JSON으로만: {"candidates":[{"document_id":"...","quote":"문서 속 정확한 문장",'
    '"supports":"무엇을 뒷받침하는지","axis":"ORIGIN|..."}]}'
)


def find_missing_evidence(db, case_id, timeout=90):
    """LLM에 전체 패킷을 주고 놓친 근거 후보를 받는다. 실패 시 {'error':...}."""
    pk = build_packet(db, case_id)
    if not pk["documents"]:
        return {"candidates": [], "note": "no_documents"}
    import json as _json
    user = _json.dumps({"documents": pk["documents"], "decisions": pk["decisions"]},
                       ensure_ascii=False)[:24000]   # 토큰 방어(과대 패킷 절단)
    r = ai_local.llm_json(_SYS, user, timeout=timeout)
    if not isinstance(r, dict) or "candidates" not in r:
        return {"error": (r or {}).get("error", "BAD_RESPONSE"), "candidates": []}
    r["_doc_texts"] = pk["doc_texts"]
    return r


def verify_quote(quote, doc_text, min_len=6):
    """안티-환각: 인용문이 실제로 문서 텍스트에 존재하는가(정규화 부분매칭).
    너무 짧은 인용은 우연매칭 위험이라 거부한다."""
    q = _norm(quote)
    if len(q) < min_len:
        return False, "quote_too_short"
    if q in _norm(doc_text):
        return True, "quote_found"
    return False, "quote_not_in_document"


def discover(db, case_id, persist=True, timeout=90):
    """오케스트레이션: 패킷 구성 → LLM 후보 → 원문 대조 → 확정 후보 등록(선택).

    반환: {"found": n, "confirmed": n, "rejected": n, "candidates": [...]}.
    자동 재판정은 하지 않는다(스코프 1차) — 확정 후보는 reviewer_status=pending 으로 남긴다.
    """
    res = find_missing_evidence(db, case_id, timeout=timeout)
    if res.get("error"):
        return {"error": res["error"], "found": 0, "confirmed": 0, "rejected": 0, "candidates": []}
    doc_texts = res.get("_doc_texts", {})
    out = []
    confirmed = rejected = 0
    for c in res.get("candidates", []):
        did = c.get("document_id")
        quote = c.get("quote") or ""
        ok, reason = verify_quote(quote, doc_texts.get(did, ""))
        axis = c.get("axis") or "OTHER"
        if axis not in {"ORIGIN", "DERIVATION", "DERIVATION_DETAIL", "PROCESS",
                        "CARRIER", "CERT", "COMPOSITION"}:
            axis = "OTHER"
        rec = {"document_id": did, "quoted_text": quote,
               "supports": c.get("supports"), "axis": axis,
               "verify_status": "confirmed" if ok else "rejected",
               "verify_reason": reason}
        if ok:
            confirmed += 1
        else:
            rejected += 1
        if persist:
            db.add(EvidenceCandidate(case_id=case_id, document_id=did, quoted_text=quote,
                                     supports=c.get("supports"), axis=axis,
                                     verify_status=rec["verify_status"],
                                     verify_reason=reason, source="llm"))
        out.append(rec)
    if persist:
        db.commit()
    return {"found": len(out), "confirmed": confirmed, "rejected": rejected, "candidates": out}
