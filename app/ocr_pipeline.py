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
_SRC_KO = {"animal": "동물성", "plant": "식물성", "microbial": "미생물", "synthetic": "합성",
           "insect": "곤충", "ferment": "발효", "mineral": "광물", "soy": "대두", "egg": "난류",
           "fish": "어류", "yeast": "효모", "unknown": "출처불명"}
_EV_KO = {"halal_cert": "할랄 인증서", "animal_source_declaration": "동물 출처 선언",
          "source_declaration": "출처 선언", "alcohol_carrier_check": "알코올 캐리어(용매) 점검",
          "composition_breakdown": "성분 조성 명세", "process_declaration": "공정 선언",
          "halal_slaughter_cert": "할랄 도축 증명", "reformulation": "재배합(대체) 필요",
          "carrier_check": "캐리어 점검", "rennet_source_declaration": "레닛 출처 선언",
          "enzyme_source_declaration": "효소 출처 선언", "bone_char_free_declaration": "골탄 비사용 선언"}
_STATUS_KO = {"haram": "금지(haram)", "mushbooh": "의심(mushbooh)", "halal": "허용(halal)"}


def _ko(items, mapping):
    return ", ".join(mapping.get(x, x) for x in (items or []))


def build_explanation(criticals):
    """ontology 사실(출처·등급·증빙·대체재)로 결정적 설명 생성 — 환각 없음."""
    lines = []
    for j in criticals:
        srcs = _ko(j.get("sources"), _SRC_KO) or "출처 불명"
        evs = _ko(j.get("required_evidence"), _EV_KO) or "성분 조성 명세"
        alts = ", ".join(j.get("alternatives") or []) or "—"
        st = _STATUS_KO.get(j.get("status"), "판정필요")
        carrier = " · 알코올 캐리어(용매) 점검 필요" if j.get("carrier_check") == "alcohol" else ""
        lines.append(f"· {j['name']}: {srcs} 유래 가능 ({st}, 위험 {j['severity']}){carrier}. "
                     f"필요 증빙: {evs}. 대체재: {alts}.")
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