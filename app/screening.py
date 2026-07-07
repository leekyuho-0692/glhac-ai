"""원재료 스크리닝 — 설계 24.13.5 결정 로직."""
import re
from .models import IngredientOntology

_CACHE = {"by_e": {}, "by_alias": {}}


def load_ontology(db):
    _CACHE["by_e"], _CACHE["by_alias"] = {}, {}
    for it in db.query(IngredientOntology).all():
        if it.e_number:
            _CACHE["by_e"][it.e_number.upper()] = it
        names = [it.canonical_name]
        for v in (it.aliases or {}).values():
            names += v if isinstance(v, list) else [v]
        for n in names:
            if n:
                _CACHE["by_alias"][n.strip().lower()] = it


def _match(name, e_number):
    if e_number:
        it = _CACHE["by_e"].get(e_number.strip().upper())
        if it:
            return it
    if name:
        key = name.strip().lower()
        if key in _CACHE["by_alias"]:
            return _CACHE["by_alias"][key]
        for alias, it in _CACHE["by_alias"].items():
            if len(alias) >= 4 and (alias in key or key in alias):
                return it
    return None


def screen(material):
    it = _match(material.name, material.e_number)
    if not it:
        return {"result": "UNKNOWN", "status": None, "severity": "medium",
                "matched_uid": None, "carrier_check": None, "sources": [],
                "required_evidence": ["composition_breakdown"], "alternatives": []}

    status = it.default_status
    carrier = getattr(it, "carrier_check", None)
    evidence = list(it.required_evidence or [])
    if carrier == "alcohol" and "alcohol_carrier_check" not in evidence:
        evidence.append("alcohol_carrier_check")

    if status == "haram":
        result = "BLOCK"
    elif status == "halal":
        # halal 추정이라도 캐리어/용매(예: 카로틴의 젤라틴 캐리어)는 점검 필요 (24.13.8)
        result = "NEEDS_EVIDENCE" if (carrier and not material.evidence_provided) else "PASS"
    else:  # mushbooh
        result = "CLEARED" if (material.evidence_provided and material.source_known) else "NEEDS_EVIDENCE"

    severity = it.severity
    if status == "mushbooh" and not material.source_known:
        severity = "high"
    return {"result": result, "status": status, "severity": severity,
            "matched_uid": it.ingredient_uid, "carrier_check": carrier,
            "sources": it.sources or [],
            "required_evidence": evidence, "alternatives": it.alternatives or []}


def apply_screen(material):
    r = screen(material)
    material.screen_result = r["result"]
    material.screen_status = r["status"]
    material.screen_severity = r["severity"]
    material.matched_uid = r["matched_uid"]
    return r


class _Shim:
    def __init__(self, name, e_number=None, evidence_provided=False, source_known=True):
        self.name = name
        self.e_number = e_number
        self.evidence_provided = evidence_provided
        self.source_known = source_known


def screen_raw(name, e_number=None, evidence_provided=False, source_known=True):
    """이름/E-number 직접 스크리닝 (OCR 파이프라인용)."""
    return screen(_Shim(name, e_number, evidence_provided, source_known))


def scan_text(text):
    """OCR 원문에서 ontology 별칭/E-number를 직접 탐지(결정적, 하이브리드 매칭의 '주').
    다중어 별칭(citric acid)은 substring, 짧은 별칭(향료/물)은 토큰 경계로 매칭.
    반환: [(ingredient_uid, item)]."""
    low = text.lower()
    tokens = set(t for t in re.split(r"[\s,()/:·\[\]{}*]+", low) if t)
    hits = {}
    for alias, it in _CACHE["by_alias"].items():
        match = (alias in low) if (len(alias) >= 4 or " " in alias) else (alias in tokens)
        if match:
            hits.setdefault(it.ingredient_uid, it)
    for enum, it in _CACHE["by_e"].items():
        if enum.lower() in low:
            hits.setdefault(it.ingredient_uid, it)
    return list(hits.items())


# ===== v1 상속: KMA1360 면제목록 + 키워드 위험 + source-rule (applyMaterialRules 이식) =====
KMA1360_EXEMPT = {
    "water", "aqua", "air", "salt", "sodium chloride", "calcium carbonate", "sodium carbonate",
    "sodium bicarbonate", "potassium chloride", "calcium sulfate", "magnesium sulfate",
    "sodium sulfate", "sodium hydroxide", "potassium hydroxide", "hydrochloric acid",
    "sulfuric acid", "nitric acid", "phosphoric acid", "citric acid", "acetic acid", "malic acid",
    "tartaric acid", "ascorbic acid", "benzoic acid", "sorbic acid", "silicon dioxide",
    "titanium dioxide", "iron oxide", "carbon black", "kaolin", "talc", "mica", "zinc oxide",
    "magnesium carbonate", "calcium phosphate",
}
_RE_HIGH1 = re.compile(r"pork|pig|babi|porcine|lard|bacon|ham\b|gammon|prosciutto|pepperoni|pancetta|chorizo|blood|darah|carrion|bangkai|human|placenta|khamr|wine|beer|rum|vodka|whisky|whiskey|brandy|liquor|carmine|cochineal")
_RE_HIGH2 = re.compile(r"animal|hewani|beef|bovine|cow|chicken|poultry|meat|fat|gelatin|collagen|rennet|tallow|bone|skin")
_RE_MED = re.compile(r"enzyme|microbial|fermentation|flavo[u]?r|glycerin|glycerol|emulsifier|mono|diglyceride|e471|ethanol|alcohol|solvent|oleic|stearic|polysorbate|lecithin|colou?r")


def analyze_text_risk(text):
    """v1 analyzeMaterialTextRisk 이식 — 키워드 기반 high/medium/low."""
    t = (text or "").lower()
    if _RE_HIGH1.search(t) or _RE_HIGH2.search(t):
        return "high"
    if _RE_MED.search(t):
        return "medium"
    return "low"


def is_kma1360(text):
    """v1 isKMA1360Listed 이식 — 단어경계 매칭(부분문자열 오탐 방지: mica∈chemical 등)."""
    t = (text or "").lower()
    return any(re.search(r"\b" + re.escape(term) + r"\b", t) for term in KMA1360_EXEMPT)


def v1_rule(name, source, cert_no, note=""):
    """v1 applyMaterialRules 이식 → (cert, risk)."""
    blob = " ".join([name or "", note or ""]).strip()
    if source in ("plant", "mineral"):
        return ("exempt", "low")
    if source == "animal":
        return ("certified", "low") if cert_no else ("unknown", "high")
    if source in ("synthetic", "chemical"):
        if is_kma1360(blob):
            return ("exempt", "low")
        r = analyze_text_risk(blob)
        return ("unknown", "high" if r == "high" else "medium")
    r = analyze_text_risk(blob)
    return (("certified" if (cert_no and r == "low") else "unknown"), r)


def screen_merged(name, e_number=None, source=None, cert_no=None,
                  evidence_provided=False, source_known=True, note=""):
    """병합 스크리닝: ontology 매칭 시 ontology 우선(결정적), 미매칭 시 v1 source-rule."""
    onto = screen(_Shim(name, e_number, evidence_provided, source_known))
    cert, v1risk = v1_rule(name, source, cert_no, note)
    if onto.get("matched_uid"):
        out = dict(onto)
        out["decision_by"] = "ontology"
    else:
        if v1risk == "high":
            res, sev, st = "NEEDS_EVIDENCE", "high", "mushbooh"
        elif v1risk == "medium":
            res, sev, st = "NEEDS_EVIDENCE", "medium", "mushbooh"
        else:
            res, sev, st = "PASS", "low", "halal"
        if cert == "exempt":
            res, st = "PASS", "halal"
        out = {"result": res, "status": st, "severity": sev, "matched_uid": None,
               "carrier_check": None, "sources": [source] if source else [],
               "required_evidence": [] if res == "PASS" else ["source_declaration"],
               "alternatives": [], "decision_by": "v1_rule"}
    out["v1_risk"] = v1risk
    out["v1_cert"] = cert
    out["source"] = source
    return out


def screen_text(text):
    """v1 AI 성분 스캐너(텍스트 붙여넣기) 상속 + ontology 매칭."""
    risk = analyze_text_risk(text)
    hits = [{"name": it.canonical_name, "status": it.default_status, "severity": it.severity,
             "uid": it.ingredient_uid} for _, it in scan_text(text)]
    haram = [h for h in hits if h["status"] == "haram"]
    return {"keyword_risk": risk, "ontology_hits": hits, "haram_hits": haram,
            "verdict": "high" if (haram or risk == "high") else risk}

# ===================== 성분 설명 (판정 근거 + 해설) =====================
_CAT_KO = {
    "animal_protein": "동물성 단백질", "animal_fat": "동물성 지방", "alcohol": "알코올",
    "emulsifier": "유화제", "gelatin": "젤라틴", "enzyme": "효소", "flavor": "향료",
    "colorant": "색소", "sweetener": "감미료", "acid": "산도조절제", "preservative": "보존료",
    "carbohydrate": "탄수화물/당류", "mineral": "광물/무기질", "vitamin": "비타민", "other": "기타",
}
_STATUS_KO = {"halal": "할랄(허용)", "haram": "금지(하람)", "mushbooh": "의심(mushbooh/샤부하)"}
_SOURCE_KO = {"animal": "동물", "plant": "식물", "ferment": "발효", "synthetic": "합성",
              "mineral": "광물", "microbial": "미생물"}


def explain(name, e_number=None, source=None, cert_no=None, note=""):
    """성분 분석 결과에 대한 설명 — 판정 근거(온톨로지) + 대체재·증빙."""
    sc = screen_merged(name, e_number, source, cert_no, False, True, note or "")
    it = _match(name, e_number)
    verdict = sc["result"]
    lines = []
    if it:
        cat = _CAT_KO.get(it.category, it.category)
        st = _STATUS_KO.get(it.default_status, it.default_status)
        srcs = ", ".join(_SOURCE_KO.get(s, s) for s in (it.sources or [])) or "미상"
        lines.append("‘%s’은(는) %s 계열 성분이며, 기본 할랄 상태는 %s입니다. (주요 유래: %s)"
                     % (it.canonical_name, cat, st, srcs))
        if it.najis_risk:
            lines.append("• 부정물(najis) 위험 성분 — 설비·공정의 할랄 전용/세척(사무 khusus) 이슈가 동반됩니다.")
        if it.carrier_check:
            lines.append("• 캐리어/용매 점검 대상(%s) — 향료·색소 등의 용매로 알코올/젤라틴이 쓰였는지 확인 필요."
                         % it.carrier_check)
        if verdict == "BLOCK":
            lines.append("→ 판정: 금지(haram). 사용 시 인증 불가 — 대체재로 재설계가 필요합니다.")
        elif verdict == "NEEDS_EVIDENCE":
            lines.append("→ 판정: 증빙 필요 — 유래(동물/식물)에 따라 할랄 여부가 갈립니다. 아래 증빙 제출 시 CLEARED로 전환됩니다.")
        else:
            lines.append("→ 판정: 할랄 허용(추가 증빙 불요 또는 확보됨).")
        if it.required_evidence:
            lines.append("• 필요 증빙: " + ", ".join(it.required_evidence))
        if it.alternatives:
            lines.append("• 할랄 대체재: " + ", ".join(it.alternatives))
    else:
        lines.append("‘%s’은(는) 온톨로지 정식 등재 성분이 아니며, v1 규칙/키워드 기반으로 ‘%s’ 판정되었습니다."
                     % (name, verdict))
        if sc.get("v1_risk"):
            lines.append("• v1 위험도: " + str(sc["v1_risk"]))
    return {"name": name, "verdict": verdict, "severity": sc.get("severity"),
            "matched_uid": sc.get("matched_uid"),
            "category": it.category if it else None,
            "status": it.default_status if it else None,
            "sources": (it.sources if it else []) or [],
            "najis": bool(it and it.najis_risk),
            "required_evidence": (it.required_evidence if it else []) or [],
            "alternatives": (it.alternatives if it else []) or [],
            "explanation": "\n".join(lines)}
