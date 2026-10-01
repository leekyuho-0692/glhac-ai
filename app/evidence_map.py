"""증빙코드 ↔ 문서유형 매핑 적용기. 판정이 제출 문서를 실제로 보게 하는 연결부다(Phase 1-A)."""

import json
import os
import re

_STATE = {"map": None}

_JSON_PATH = os.path.join(os.path.dirname(__file__), "evidence_map.json")

VERDICTS = (
    "SATISFIED",
    "SATISFIED_WEAK",
    "SUPPORTING_ONLY",
    "NOT_SATISFIED",
    "NOT_SCOPED",
    "NOT_APPLICABLE",
)

# NOT_SCOPED 가 NOT_SATISFIED 보다 높다 — 종류는 맞는데 이 원료 것이 아닌 문서가
# 존재한다는 사실이 더 유용한 진단이다(바인딩하면 해소된다).
_RANK = {
    "SATISFIED": 5,
    "SATISFIED_WEAK": 4,
    "SUPPORTING_ONLY": 3,
    "NOT_SCOPED": 2,
    "NOT_SATISFIED": 1,
    "NOT_APPLICABLE": 0,
}

_TIERS = ("primary", "acceptable", "supporting")


def _load():
    if _STATE["map"] is None:
        with open(_JSON_PATH, encoding="utf-8") as fh:
            _STATE["map"] = json.load(fh)
    return _STATE["map"]


def canonical_doc_type(doc_type):
    if not doc_type or not isinstance(doc_type, str):
        return None
    cleaned = doc_type.strip().lower()
    if not cleaned:
        return None
    aliases = _load().get("doc_type_aliases") or {}
    return aliases.get(cleaned, cleaned)


def resolve_code(code):
    if not code or not isinstance(code, str):
        return None
    cleaned = code.strip()
    if not cleaned:
        return None
    evidence = _load().get("evidence") or {}
    current = cleaned
    for _ in range(5):
        entry = evidence.get(current)
        if not isinstance(entry, dict):
            return current
        target = entry.get("alias_of")
        if not target:
            return current
        current = target
    return current


def is_action_only(evidence_code):
    """증빙이 아니라 조치 항목인가(예: reformulation). 충족 판정 대상에서 빼야 한다."""
    code = resolve_code(evidence_code)
    entry = (_load().get("evidence") or {}).get(code) or {}
    return bool(entry.get("is_action_not_evidence"))


def validate(doc_types=None):
    result = {
        "unknown_doc_types_in_map": [],
        "codes_missing_from_domain_dict": [],
        "domain_dict_codes_missing_from_map": [],
        "alias_targets_missing": [],
        "doc_types_not_in_caller_list": [],
    }
    data = _load()
    evidence = data.get("evidence") or {}
    canonical = set(data.get("doc_type_canonical") or [])

    referenced = []
    for entry in evidence.values():
        if not isinstance(entry, dict):
            continue
        for tier in _TIERS:
            for dt in entry.get(tier) or []:
                referenced.append(dt)
    unknown = []
    for dt in referenced:
        if dt not in canonical and dt not in unknown:
            unknown.append(dt)
    result["unknown_doc_types_in_map"] = unknown

    alias_targets_missing = []
    for key, entry in evidence.items():
        if isinstance(entry, dict) and entry.get("alias_of"):
            target = entry["alias_of"]
            if target not in evidence and target not in alias_targets_missing:
                alias_targets_missing.append(target)
    result["alias_targets_missing"] = alias_targets_missing

    try:
        from .domain_dict import by_axis, actions

        dd_codes = []
        for k in by_axis("EVIDENCE"):
            code = (actions(k) or {}).get("evidence_code")
            if code:
                dd_codes.append(code)

        map_codes = list(evidence.keys())
        result["codes_missing_from_domain_dict"] = [
            c for c in map_codes if c not in dd_codes
        ]
        result["domain_dict_codes_missing_from_map"] = [
            c for c in dd_codes if c not in map_codes
        ]
    except Exception:
        result["codes_missing_from_domain_dict"] = []
        result["domain_dict_codes_missing_from_map"] = []

    if doc_types is not None:
        caller = set()
        for dt in doc_types:
            c = canonical_doc_type(dt)
            if c:
                caller.add(c)
        result["doc_types_not_in_caller_list"] = [
            dt for dt in canonical if dt not in caller
        ]

    return result


def _flatten_values(value, out):
    if value is None:
        return
    if isinstance(value, list):
        for item in value:
            if item is not None:
                out.append(str(item))
    else:
        out.append(str(value))


def haystack_of(doc):
    """지목 판정에 쓰는 검색 대상 문자열. 본문·추출필드를 한 덩어리로 소문자화한다.
    text_full 이 있으면 함께 본다 — text_excerpt 는 300자로 잘려 있다."""
    flat = []
    _flatten_values(doc.get("text") or "", flat)
    _flatten_values(doc.get("text_full") or "", flat)
    for v in (doc.get("fields") or {}).values():
        _flatten_values(v, flat)
    return " ".join(flat).lower()


def name_hit(haystack, names):
    """이름 후보 중 검색대상에 나타나는 첫 이름. (맞은 이름, 판정근거) 또는 (None, None).

    바인더(문서↔원료 링크 적재)와 스코프 게이트가 **같은 규칙**을 써야 한다.
    규칙이 두 곳에서 갈리면 링크는 걸렸는데 스코프는 아니라고 하는 모순이 생긴다."""
    for name in names or []:
        if not isinstance(name, str):
            continue
        trimmed = name.strip()
        if len(trimmed) < 3:
            continue
        low = trimmed.lower()
        if len(trimmed) < 5 and trimmed.isascii():
            # 짧은 ASCII 약어(SAN·PP·LDPE)는 부분문자열로 우연히 걸린다 —
            # 실측: 'san' 이 인니어 'pisang'(바나나) 안에 들어가 플라스틱이 과일로 잡혔다.
            # 앞뒤가 영숫자가 아닌 경우에만 인정한다. 한글·비ASCII 이름은 그대로 부분일치.
            if re.search(r"(?<![0-9a-z])%s(?![0-9a-z])" % re.escape(low), haystack):
                return (trimmed, "word")
        elif low in haystack:
            return (trimmed, "substring")
    return (None, None)


def _subject_mentioned(doc, subject):
    """문서가 이 대상(원료)을 지목하는가. 명시 바인딩이 최우선이고, 없으면 파일명·본문·
    추출된 material_names 에서 이름/별칭을 찾는다."""
    doc_mat = doc.get("material_id")
    subj_id = subject.get("id")
    if (isinstance(doc_mat, str) and doc_mat.strip()
            and isinstance(subj_id, str) and subj_id.strip()
            and doc_mat.strip() == subj_id.strip()):
        return (True, "명시 바인딩")

    # 링크 테이블(1:N)로 이어진 문서도 명시 바인딩으로 본다 — 문서 한 장이 원료 여러 건을 덮는다.
    # 다만 INFERRED 링크는 별칭 매칭에서 온 추정이라 바인딩으로 인정하지 않는다(REQ-EVD-010).
    inferred = False
    if isinstance(subj_id, str) and subj_id.strip():
        linked_map = doc.get("linked")
        if isinstance(linked_map, dict):
            link_type = linked_map.get(subj_id.strip())
            if link_type is not None:
                if str(link_type).upper() == "DIRECT":
                    return (True, "링크 바인딩(DIRECT)")
                if str(link_type).upper() == "INFERRED":
                    inferred = True
        linked = doc.get("linked_material_ids")
        if linked and subj_id.strip() in set(linked):
            return (True, "링크 바인딩(DIRECT)")

    hit, _basis = name_hit(haystack_of(doc), subject.get("names") or [])
    if hit:
        return (True, "이름 일치: %s" % hit)

    if inferred:
        return (False, "추정 링크만 있다(INFERRED) — 확인 필요")

    return (False, "문서가 이 원료를 지목하지 않음")


def check_document(evidence_code, doc, content_check="strict", subject=None):
    doc = doc or {}
    code = resolve_code(evidence_code)
    evidence = (_load().get("evidence") or {})

    if not code or code not in evidence:
        return {
            "verdict": "NOT_SATISFIED",
            "grade": None,
            "reason": "매핑표에 없는 증빙코드",
            "matched_keywords": [],
            "missing_fields": [],
            "tier": None,
        }

    entry = evidence[code] or {}

    if entry.get("is_action_not_evidence"):
        return {
            "verdict": "NOT_APPLICABLE",
            "grade": None,
            "reason": "증빙이 아니라 조치 항목 — 충족 판정 대상이 아니다",
            "matched_keywords": [],
            "missing_fields": [],
            "tier": None,
        }

    cdt = canonical_doc_type(doc.get("doc_type"))
    tier = None
    for t in _TIERS:
        tiers = {canonical_doc_type(x) for x in (entry.get(t) or [])}
        if cdt and cdt in tiers:
            tier = t
            break

    if tier is None:
        return {
            "verdict": "NOT_SATISFIED",
            "grade": None,
            "reason": "문서유형 %r 이(가) 증빙코드 %s 의 어느 tier에도 해당하지 않음" % (doc.get("doc_type"), code),
            "matched_keywords": [],
            "missing_fields": [],
            "tier": None,
        }

    if entry.get("subject_scope") == "material" and subject is not None:
        mentioned, mention_reason = _subject_mentioned(doc, subject)
        if not mentioned:
            return {
                "verdict": "NOT_SCOPED",
                "grade": None,
                "reason": "문서유형 %s (tier=%s) — %s" % (cdt, tier, mention_reason),
                "matched_keywords": [],
                "missing_fields": [],
                "tier": tier,
            }

    reqs = entry.get("content_requirements") or {}
    keywords = reqs.get("keywords") or []
    fields_all = reqs.get("fields_all") or []
    fields_any = reqs.get("fields_any") or []

    fields = doc.get("fields") or {}
    flat = []
    _flatten_values(doc.get("text") or "", flat)
    for v in fields.values():
        _flatten_values(v, flat)
    haystack = " ".join(flat).lower()

    matched_keywords = []
    for kw in keywords:
        if kw and kw.lower() in haystack:
            matched_keywords.append(kw)

    missing_fields = []
    for key in fields_all:
        val = fields.get(key)
        if val is None or (isinstance(val, str) and not val.strip()):
            missing_fields.append(key)

    fields_any_present = False
    for key in fields_any:
        val = fields.get(key)
        if val is not None and not (isinstance(val, str) and not val.strip()):
            fields_any_present = True
            break

    reason = "문서유형 %s (tier=%s)" % (cdt, tier)

    if content_check == "doctype_only":
        content_ok = True
        reason = reason + " (내용조건 미검사)"
    elif content_check == "strict" and reqs:
        kw_ok = (not keywords) or bool(matched_keywords)
        all_ok = (not fields_all) or (not missing_fields)
        any_ok = (not fields_any) or fields_any_present
        content_ok = kw_ok and all_ok and any_ok
        if not content_ok:
            failed = []
            if not kw_ok:
                failed.append("키워드 미충족")
            if not all_ok:
                failed.append("필수필드 누락: %s" % ", ".join(missing_fields))
            if not any_ok:
                failed.append("선택필드 미충족")
            reason = reason + " — " + "; ".join(failed)
    else:
        content_ok = True

    if not content_ok:
        return {
            "verdict": "NOT_SATISFIED",
            "grade": None,
            "reason": reason,
            "matched_keywords": matched_keywords,
            "missing_fields": missing_fields,
            "tier": tier,
        }

    if tier == "primary":
        verdict, grade = "SATISFIED", "B"
    elif tier == "acceptable":
        verdict, grade = "SATISFIED_WEAK", "C"
    else:
        verdict, grade = "SUPPORTING_ONLY", "D"

    return {
        "verdict": verdict,
        "grade": grade,
        "reason": reason,
        "matched_keywords": matched_keywords,
        "missing_fields": missing_fields,
        "tier": tier,
    }


_MET = ("SATISFIED", "SATISFIED_WEAK", "SUPPORTING_ONLY")
_TIER_WEIGHT = {"primary": 3, "acceptable": 2, "supporting": 1}


def _pick_key(res):
    """최선 문서 고르기 기준. 두 층으로 나눈다.

      충족된 것(_MET)이 미충족보다 항상 먼저다 — 안 그러면 이미 충족된 요구가
      미충족으로 보고된다. 충족 안에서는 SATISFIED > WEAK > SUPPORTING.

      미충족 안에서는 **사유의 품질**로 고른다. 순서는 tier → scoped → 판정값이다.
      primary(3) > acceptable(2) > supporting(1) > 유형무관(0), 같은 tier 면
      이 원료를 지목한 문서가 먼저다(자기 문서의 구체적 실패가 남의 문서보다 유용하다).

    판정값만으로 고르면 세 방향으로 다 틀린다(전부 실측):
      · 남의 NOT_SCOPED 가 자기 문서의 NOT_SATISFIED 를 가림 → TAURINE 이 자기
        원산지증명서를 갖고도 '이 원재료를 지목하지 않습니다' 로 안내됐다
      · NOT_SCOPED 를 최하로 내림 → 무관한 사업자등록증이 사유가 됐다
      · tier 동순위 타이 → 전성분표(supporting)가 자기 인증서(primary)를 이겼다
      · 같은 tier 에서 판정값 우선 → 남의 인증서 NOT_SCOPED 가 자기 인증서를 또 가렸다
    """
    v = res.get("verdict")
    if v in _MET:
        return (1, _RANK.get(v, -1), 0, 0, 0)
    tier_w = _TIER_WEIGHT.get(res.get("tier"), 0)
    scoped = 0 if v == "NOT_SCOPED" else 1   # 같은 tier 면 이 원료 것이 먼저다
    return (0, 0, tier_w, scoped, _RANK.get(v, -1))


def coverage(required_codes, docs, content_check="strict", subject=None):
    docs = docs or []
    resolved = []
    seen = set()
    for rc in required_codes or []:
        r = resolve_code(rc)
        if not r or r in seen:
            continue
        seen.add(r)
        resolved.append((rc, r))

    evidence = (_load().get("evidence") or {})
    items = []
    counts = {
        "satisfied": 0,
        "weak": 0,
        "supporting_only": 0,
        "not_satisfied": 0,
        "not_scoped": 0,
        "not_applicable": 0,
    }

    for original, r in resolved:
        entry = evidence.get(r) or {}
        label = entry.get("label_ko") or r

        # 조치 항목(reformulation)은 제출 문서 유무와 무관하게 판정 대상에서 뺀다.
        # docs 가 비었을 때 NOT_SATISFIED 로 떨어지면 영구 미충족으로 남는다.
        if entry.get("is_action_not_evidence"):
            best = check_document(r, {}, content_check=content_check, subject=subject)
            best_idx = None
        else:
            best = None
            best_idx = None
            for idx, doc in enumerate(docs):
                res = check_document(r, doc, content_check=content_check, subject=subject)
                if best is None or _pick_key(res) > _pick_key(best):
                    best = res
                    best_idx = idx

        if best is None:
            best = {
                "verdict": "NOT_SATISFIED",
                "grade": None,
                "reason": "제출 문서 없음",
                "matched_keywords": [],
                "missing_fields": [],
                "tier": None,
            }
            best_idx = None

        verdict = best["verdict"]

        if verdict == "NOT_APPLICABLE":
            counts["not_applicable"] += 1
        elif verdict == "SATISFIED":
            counts["satisfied"] += 1
        elif verdict == "SATISFIED_WEAK":
            counts["weak"] += 1
        elif verdict == "SUPPORTING_ONLY":
            counts["supporting_only"] += 1
        elif verdict == "NOT_SCOPED":
            counts["not_scoped"] += 1
        else:
            counts["not_satisfied"] += 1

        items.append({
            "code": original,
            "resolved_code": r,
            "label": label,
            "verdict": verdict,
            "grade": best["grade"],
            "reason": best["reason"],
            "doc_index": best_idx,
        })

    required_effective = (
        counts["satisfied"] + counts["weak"] + counts["supporting_only"]
        + counts["not_scoped"] + counts["not_satisfied"]
    )

    decision_ready = True
    if required_effective > 0:
        for item in items:
            if item["verdict"] == "NOT_APPLICABLE":
                continue
            if item["verdict"] not in ("SATISFIED", "SATISFIED_WEAK"):
                decision_ready = False
                break

    return {
        "items": items,
        "satisfied": counts["satisfied"],
        "weak": counts["weak"],
        "supporting_only": counts["supporting_only"],
        "not_satisfied": counts["not_satisfied"],
        "not_scoped": counts["not_scoped"],
        "not_applicable": counts["not_applicable"],
        "required_effective": required_effective,
        "decision_ready": decision_ready,
    }
