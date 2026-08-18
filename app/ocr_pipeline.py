"""M3: 라벨 OCR → qwen 성분추출 → ontology 판정 파이프라인.
설계 원칙(C.1/C.7): LLM은 추출·설명만, 판정은 결정적 ontology(24.13)."""
import json
from . import ai_local, screening

_EXTRACT_SYS = (
    "당신은 식품/화장품 라벨 성분 추출기입니다. 아래 OCR 텍스트에서 '성분(ingredient)'만 "
    "추출해 JSON으로 반환하세요. 제품명·용량·제조사·주의사항·사용법은 제외합니다. "
    'E-number가 보이면 함께. 형식: {"ingredients":[{"name":"글리세린","e_number":"E422 또는 null"}]}'
)

# 결정적 설명 — ontology 사실에서 생성(LLM 환각 차단). 설계 원칙: 판정·설명=결정적.
#
# 유래·증빙·판정 표기는 여기 따로 두지 않고 도메인 사전에서 꺼낸다. 표를 두 벌 두었더니
# 같은 코드가 화면마다 다른 이름으로 나왔다(실측: animal 이 '동물'과 '동물성', unknown 이
# '미상'과 '출처불명', 증빙 12개 중 11개가 다른 표기). 심사 화면에서 같은 개념이 다르게
# 불리면 심사자가 다른 것으로 읽는다.
from . import domain_dict as _dd


def _srcs(lang="ko"):
    return _dd.code_labels("SOURCE", "screen_source", lang)


def _evs(lang="ko"):
    return _dd.code_labels("EVIDENCE", "evidence_code", lang)


def _sts(lang="ko"):
    return _dd.code_labels("STATUS", "screen_status", lang)


def _ko(items, mapping):
    return ", ".join(mapping.get(x, x) for x in (items or []))


def build_explanation(criticals, lang="ko"):
    """ontology 사실(출처·등급·증빙·대체재)로 결정적 설명 생성 — 환각 없음."""
    L = _dd.text_fn(lang)
    src_t, ev_t, st_t = _srcs(lang), _evs(lang), _sts(lang)
    lines = []
    for j in criticals:
        srcs = _ko(j.get("sources"), src_t) or L("미상")
        evs = _ko(j.get("required_evidence"), ev_t) or ev_t.get("composition_breakdown",
                                                                "composition_breakdown")
        alts = ", ".join(_dd.text(a, lang) for a in (j.get("alternatives") or [])) or "—"
        st = st_t.get(j.get("status")) or L("미판정")
        carrier = (" · " + L("알코올 캐리어(용매) 점검 필요")) if j.get("carrier_check") == "alcohol" else ""
        lines.append(f"· {j['name']}: {srcs} {L('유래 가능')} ({st}, {L('위험')} {j['severity']}){carrier}. "
                     f"{L('필요 증빙')}: {evs}. {L('대체재')}: {alts}.")
    return "\n".join(lines)


def judge_label(image_path, locale="ko-KR"):
    # 1) OCR (PaddleOCR)
    ocr = ai_local.ocr_image(image_path)
    if not ocr.get("ok"):
        return {"ok": False, "stage": "ocr", "error": ocr.get("error")}
    raw = "\n".join(line["text"] for line in ocr["lines"])

    # 2) (주) OCR 원문 직접 ontology 매칭 — 결정적. qwen이 깨뜨려도 잡힘.
    by_uid = {}
    for uid, it in screening.scan_text(raw):
        sc = screening.screen_raw(it.canonical_name, it.e_number)
        by_uid[uid] = {"name": it.canonical_name, "e_number": it.e_number, "source": "ocr", **sc}

    # 3) (보조) qwen 추출 — OCR 토큰화가 놓친 항목 보강. ontology 미매칭은 분리 표기.
    extracted = ai_local.llm_json(_EXTRACT_SYS, raw)
    cands = extracted.get("ingredients", []) if isinstance(extracted, dict) else []
    llm_unmatched = []
    for c in cands:
        name = (c.get("name") or "").strip()
        enum = c.get("e_number")
        if enum in ("null", "", None):
            enum = None
        if not name:
            continue
        sc = screening.screen_raw(name, enum)
        uid = sc["matched_uid"]
        if uid:
            if uid in by_uid:
                by_uid[uid]["source"] = "both"
            else:
                by_uid[uid] = {"name": name, "e_number": enum, "source": "llm", **sc}
        else:
            llm_unmatched.append(name)

    judged = list(by_uid.values())
    criticals = [j for j in judged if j["result"] in ("BLOCK", "NEEDS_EVIDENCE")]

    # 4) 설명 — ontology 사실에서 결정적 생성(LLM 환각 차단)
    explanation = build_explanation(criticals)

    return {
        "ok": True,
        "ocr_line_count": len(ocr["lines"]),
        "raw_text": raw,
        "ingredients": judged,
        "critical_count": len(criticals),
        "llm_unmatched": llm_unmatched,
        "pathway_implication": "reguler" if criticals else "self_declare_candidate",
        "explanation": explanation,
        "note": "판정·설명=ontology(결정적, OCR직매칭 우선·환각 없음). LLM=성분 추출 보조만. 최종 할랄성은 Komite Fatwa.",
    }