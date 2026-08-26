"""원재료 스크리닝 — 설계 24.13.5 결정 로직."""
import re
from .models import IngredientOntology

_CACHE = {"by_e": {}, "by_alias": {}, "alias_tok": []}


def load_ontology(db):
    _CACHE["by_e"], _CACHE["by_alias"], _CACHE["alias_tok"] = {}, {}, []
    for it in db.query(IngredientOntology).all():
        if it.e_number:
            _CACHE["by_e"][it.e_number.upper()] = it
        names = [it.canonical_name]
        for v in (it.aliases or {}).values():
            names += v if isinstance(v, list) else [v]
        for n in names:
            if n:
                key = n.strip().lower()
                _CACHE["by_alias"][key] = it
                tok = _tokens(key)
                if len(tok) >= 2:          # 단일 토큰 별칭은 과잉매칭이라 토큰매칭에서 제외
                    _CACHE["alias_tok"].append((tok, key, it))


# 표기 변형 흡수용 불용어 — 의미를 담지 않는 연결어만. 'powder/extract' 같은 형태어는
# 성분 구분에 쓰이므로 절대 넣지 마라(과잉매칭이 난다).
_STOPWORDS = {"of", "and", "the", "with", "for", "a", "an", "in", "on", "de", "dan"}


# 상품명에 붙는 계량 단위 — 인도네시아 실무 자료는 성분명이 아니라 상품명으로 적힌다
# ('Minyak Bimoli 5 Liter', 'DAGING POTONG 1 KG BRAVOO'). 수량·단위는 성분 구분에
# 기여하지 않으므로 토큰에서 뺀다. 브랜드는 열거할 수 없어 제거하지 않는다
# (부분문자열 매칭이 'minyak'을 잡아주므로 굳이 지울 필요도 없다).
_UNITS = {"kg", "g", "gr", "gram", "mg", "l", "liter", "litre", "ltr", "ml", "cc",
          "pcs", "pack", "pak", "box", "dus", "btl", "botol", "sachet", "renceng",
          "bungkus", "kaleng", "galon", "lusin", "ton"}


def _tokens(text):
    """어순·복수형·구두점 차이를 흡수한 토큰 집합.
    'Sucrose Fatty Acid Esters' 와 'sucrose esters of fatty acids' 가 같은 집합이 된다.
    숫자·계량단위는 제외한다(상품명의 용량 표기가 매칭을 방해하지 않게)."""
    out = set()
    for t in re.split(r"[^0-9a-z가-힣]+", (text or "").lower()):
        if not t or t in _STOPWORDS or t in _UNITS or t.isdigit():
            continue
        # 단순 복수형 정규화: acids→acid, esters→ester. 'ss'로 끝나거나 짧은 말은 건드리지 않는다.
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.add(t)
    return out


_STATUS_RANK = {"haram": 0, "mushbooh": 1, "halal": 2}


def _norm_key(s):
    """매칭 전 표기 정규화 — 곱슬따옴표·비분리공백처럼 같은 이름을 갈라놓는 문자만 손본다
    ('Merica Bubuk “Ladaku”'). 내용을 바꾸는 치환은 하지 않는다."""
    if not s:
        return ""
    for a, b in (("\u201c", '"'), ("\u201d", '"'), ("\u2018", "'"), ("\u2019", "'"),
                 ("\u00a0", " "), ("\u200b", " ")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


def _match(name, e_number):
    if e_number:
        it = _CACHE["by_e"].get(e_number.strip().upper())
        if it:
            return it
    if name:
        key = _norm_key(name).lower()
        if key in _CACHE["by_alias"]:
            return _CACHE["by_alias"][key]
        # 토큰 매칭(어순·복수형 흡수)과 부분 문자열 매칭을 하나의 후보 풀로 합쳐
        # '안전측 우선'으로 고른다. 단계를 나눠 먼저 걸린 쪽을 반환하면,
        # 단일 토큰 별칭(flavor 등)이 경쟁에서 빠져 위험 성분이 할랄로 통과한다.
        # 정렬: (위험도, 토큰매칭 우선, 매칭 강도 큰 순)
        cands = []
        key_tok = _tokens(key)
        if key_tok:
            for tok, alias, it in _CACHE["alias_tok"]:
                if tok <= key_tok:
                    cands.append((_STATUS_RANK.get(it.default_status, 3), 0, -len(tok), it))
        # 짧은 이름(SAN·PP·LDPE 등 약어)은 부분문자열 매칭에서 제외한다.
        # 'san'이 인도네시아어 별칭 'pisang'(바나나)·'santan'(코코넛밀크) 안에 우연히 들어가
        # 플라스틱 SAN이 바나나로 판정됐다. 짧은 약어는 정확·토큰 매칭으로만 잡아야 한다.
        if len(key) >= 5:
            for alias, it in _CACHE["by_alias"].items():
                if len(alias) >= 4 and (alias in key or key in alias):
                    cands.append((_STATUS_RANK.get(it.default_status, 3), 1, -len(alias), it))
        if cands:
            cands.sort(key=lambda p: (p[0], p[1], p[2]))
            return cands[0][3]
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
    """DB에 저장되는 판정 — 조회·보고서가 쓰는 screen_merged와 같은 경로로 계산한다.
    원시 screen()만 쓰면 미등재 성분이 저장값 UNKNOWN / 조회값 NEEDS_EVIDENCE로 갈려
    화면과 DB가 어긋난다(재스크리닝 후 실측에서 확인)."""
    r = screen_merged(getattr(material, "name", None), getattr(material, "e_number", None),
                      getattr(material, "source", None), getattr(material, "cert_no", None),
                      bool(getattr(material, "evidence_provided", False)),
                      (getattr(material, "source_known", None)
                       if getattr(material, "source_known", None) is not None else True),
                      getattr(material, "note", "") or "")
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
            # 온톨로지에 없는 성분을 할랄로 통과시키지 않는다. 모르는 것은 '모른다'로 둬야 한다.
            # (화장품 원료 실측: 프로폴리스·유산균발효물·알란토인·정체불명 코드명이 전부 PASS로
            #  빠져나갔다. 벌·발효배지·요산 유래 가능성이 있어 최소 확인 대상이다.)
            # 면제 목록(물·소금 등 KMA1360_EXEMPT)은 아래에서 따로 PASS로 돌린다.
            res, sev, st = "NEEDS_EVIDENCE", "low", "mushbooh"
        if cert == "exempt":
            res, st, sev = "PASS", "halal", "low"
        out = {"result": res, "status": st, "severity": sev, "matched_uid": None,
               "carrier_check": None, "sources": [source] if source else [],
               "required_evidence": [] if res == "PASS" else ["source_declaration"],
               "alternatives": [],
               "decision_by": "v1_rule" if v1risk in ("high", "medium") or cert == "exempt"
               else "unmatched_default"}
    # 할랄 인증서 번호가 적혀 있으면 의심(mushbooh)을 할랄로 올린다.
    # 위험도(severity)는 건드리지 않는다 — 무엇을 근거로 올렸는지, 원래 얼마나 위험한
    # 성분인지는 그대로 남아야 오디터가 인증서 진위를 대조할 수 있다.
    # cert_promoted 플래그로 '증빙으로 해소된 것'과 '번호만 적힌 것'을 구분한다.
    out["cert_promoted"] = False
    if cert_no and str(cert_no).strip() and out.get("status") == "mushbooh":
        out["status"] = "halal"
        out["result"] = "CLEARED"
        out["cert_promoted"] = True
        out["decision_by"] = "cert_no"
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
# 코드값 → 화면 표기. 표를 코드에 세 벌 두지 않고 도메인 사전 한 곳에서 꺼낸다.
#
# 왜 옮겼나(실측): 같은 개념의 번역이 _CAT_KO/_CAT_EN/_CAT_ID 처럼 세 표에 흩어져 있어
# 한쪽만 고쳐졌다. 유래(_SOURCE)는 한국어만 7개가 비어 화면에 'dairy'·'unknown' 같은
# 코드값이 그대로 나왔고, 판정 상태는 사전과 코드가 서로 다른 값을 갖고 있었다.
# 사전에는 세 언어가 한 항목에 붙어 있어 한 언어만 빠지면 눈에 띈다.
from . import domain_dict as _dd   # noqa: E402

_CAT_KO = _dd.code_labels("MATERIAL_CAT", "screen_category", "ko")
_CAT_EN = _dd.code_labels("MATERIAL_CAT", "screen_category", "en")
_CAT_ID = _dd.code_labels("MATERIAL_CAT", "screen_category", "id")
_STATUS_KO = _dd.code_labels("STATUS", "screen_status", "ko")
_STATUS_EN = _dd.code_labels("STATUS", "screen_status", "en")
_STATUS_ID = _dd.code_labels("STATUS", "screen_status", "id")
_SOURCE_KO = _dd.code_labels("SOURCE", "screen_source", "ko")
_SOURCE_EN = _dd.code_labels("SOURCE", "screen_source", "en")
_SOURCE_ID = _dd.code_labels("SOURCE", "screen_source", "id")
_EVID_KO = _dd.code_labels("EVIDENCE", "evidence_code", "ko")
_EVID_EN = _dd.code_labels("EVIDENCE", "evidence_code", "en")
_EVID_ID = _dd.code_labels("EVIDENCE", "evidence_code", "id")
# 대체재는 키가 한국어 문구다(코드값이 아니다) — 문구→표기 표로 꺼낸다.
_ALT_EN = _dd.axis_text_map("ALTERNATIVE", "en")
_ALT_ID = _dd.axis_text_map("ALTERNATIVE", "id")

# --- 다국어 서술 ---------------------------------------------------------
# 판정 근거는 366항목마다 쓰인 글이 아니라 템플릿 문장 + 아래 조회표로 조립된다.
# 따라서 항목별 번역문이 아니라 이 표와 문장만 언어별로 갖추면 된다.
# 미등록 키는 원문(영문 코드 또는 한국어)을 그대로 내보낸다 — 빈칸이 생기지 않게.
# 필요 증빙 코드 — 심사자가 실제로 요구하는 서류명이라 언어별 표기가 필요하다.
# 대체재 — 한국어로 적힌 값만 옮긴다(영문·화학명은 그대로 두는 것이 정확하다).
# 템플릿 문장 — 한국어 원문을 키로 쓴다(프런트 T()·보고서 라벨과 같은 방식).
# 판정 설명 문장 — 한국어 문장이 키다. 사전 LABEL 축에서 꺼낸다.
_EXPLAIN_L10N = {lg: _dd.axis_text_map("LABEL", lg) for lg in ("en", "id")}

_L10N_TABLES = {
    "en": (_CAT_EN, _STATUS_EN, _SOURCE_EN, _EVID_EN, _ALT_EN),
    "id": (_CAT_ID, _STATUS_ID, _SOURCE_ID, _EVID_ID, _ALT_ID),
}


# 한국어 증빙 표기 — 영어·인니어는 있는데 한국어만 비어 있어서 화면에 코드가 그대로
# 노출됐다('halal_slaughter_cert'). 오디터가 읽을 말로 적는다.
def _explain_l10n(lang):
    """(문장번역기, 카테고리, 상태, 유래, 증빙, 대체재) — 미지원 언어는 한국어 표를 돌려준다."""
    lang = (lang or "ko").lower()
    tabs = _L10N_TABLES.get(lang)
    if not tabs:
        return (lambda s: s), _CAT_KO, _STATUS_KO, _SOURCE_KO, _EVID_KO, {}
    sent = _EXPLAIN_L10N.get(lang, {})
    return ((lambda s: sent.get(s, s)),) + tabs


def term_tables(lang):
    """(증빙코드표, 대체재표) — 보고서가 목록 값을 현지화할 때 쓴다.
    explain()의 반환 dict는 원본 코드를 유지한다(프런트 로직이 코드로 분기하므로 계약 불변)."""
    _S, _C, _T, _R, evid, alt = _explain_l10n(lang)
    return evid, alt


def explain(name, e_number=None, source=None, cert_no=None, note="", lang="ko"):
    """성분 분석 결과에 대한 설명 — 판정 근거(온톨로지) + 대체재·증빙.

    lang=ko|en|id. 서술은 항목별 원고가 아니라 템플릿 문장 + 조회표로 조립되므로,
    언어를 바꿔도 판정 자체는 동일한 온톨로지 값에서 나온다(번역이 판정을 바꾸지 않는다)."""
    sc = screen_merged(name, e_number, source, cert_no, False, True, note or "")
    it = _match(name, e_number)
    verdict = sc["result"]
    S, CAT, STAT, SRC, EVID, ALT = _explain_l10n(lang)
    lines = []
    if it:
        cat = CAT.get(it.category, it.category)
        st = STAT.get(it.default_status, it.default_status)
        srcs = ", ".join(SRC.get(s, s) for s in (it.sources or [])) or S("미상")
        lines.append(S("‘%s’은(는) %s 계열 성분이며, 기본 할랄 상태는 %s입니다. (주요 유래: %s)")
                     % (it.canonical_name, cat, st, srcs))
        if it.najis_risk:
            lines.append(S("• 부정물(najis) 위험 성분 — 설비·공정의 할랄 전용/세척(사무 khusus) 이슈가 동반됩니다."))
        if it.carrier_check:
            lines.append(S("• 캐리어/용매 점검 대상(%s) — 향료·색소 등의 용매로 알코올/젤라틴이 쓰였는지 확인 필요.")
                         % it.carrier_check)
        if verdict == "BLOCK":
            lines.append(S("→ 판정: 금지(haram). 사용 시 인증 불가 — 대체재로 재설계가 필요합니다."))
        elif verdict == "NEEDS_EVIDENCE":
            lines.append(S("→ 판정: 증빙 필요 — 유래(동물/식물)에 따라 할랄 여부가 갈립니다. 아래 증빙 제출 시 CLEARED로 전환됩니다."))
        else:
            lines.append(S("→ 판정: 할랄 허용(추가 증빙 불요 또는 확보됨)."))
        if it.required_evidence:
            lines.append(S("• 필요 증빙: ") + ", ".join(EVID.get(x, x) for x in it.required_evidence))
        if it.alternatives:
            lines.append(S("• 할랄 대체재: ") + ", ".join(ALT.get(x, x) for x in it.alternatives))
    else:
        lines.append(S("‘%s’은(는) 온톨로지 정식 등재 성분이 아니며, v1 규칙/키워드 기반으로 ‘%s’ 판정되었습니다.")
                     % (name, verdict))
        if sc.get("decision_by") == "unmatched_default":
            lines.append(S("• 미등재 성분은 할랄로 간주하지 않습니다 — 유래·조성 확인 후 판정합니다."))
        if sc.get("v1_risk"):
            lines.append(S("• v1 위험도: ") + str(sc["v1_risk"]))
    return {"name": name, "verdict": verdict, "severity": sc.get("severity"),
            "matched_uid": sc.get("matched_uid"),
            "category": it.category if it else None,
            "status": it.default_status if it else None,
            "sources": (it.sources if it else []) or [],
            "najis": bool(it and it.najis_risk),
            "required_evidence": (it.required_evidence if it else []) or [],
            "alternatives": (it.alternatives if it else []) or [],
            "explanation": "\n".join(lines)}


# ── 오디터 심사 참고 의견 ────────────────────────────────────────────────
# 오디터는 '걸린 목록'만으로는 판단할 수 없다. 왜 걸렸는지, 어떤 조건이면 예외로 볼 수
# 있는지, 이 건이 그 조건에 해당하는지를 함께 봐야 서명할 수 있다. 판정을 바꾸지는 않는다 —
# 판단은 사람이 하고, 여기서는 근거와 확인할 것만 정리해 준다.

_BPJPH_NO = re.compile(r"^ID\d{8,}$", re.I)          # BPJPH 할랄 인증번호 형식
_MUI_NO = re.compile(r"^\d{8,}$")                    # LPPOM MUI 계열 번호

# 오디터 근거 메모 라벨 — 사전에서(코드값 키).
_NOTE_L10N = {lg: _dd.code_labels("AUDITOR_NOTE", "note_key", lg)
              for lg in ("ko", "en", "id")}

def _by_uid(uid):
    """ingredient_uid 로 온톨로지 항목을 찾는다 — 판정 시점의 매칭을 그대로 재사용."""
    if not uid:
        return None
    for it in (_CACHE.get("items") or []):
        if getattr(it, "ingredient_uid", None) == uid:
            return it
    for it in (_CACHE.get("by_alias") or {}).values():
        if getattr(it, "ingredient_uid", None) == uid:
            return it
    return None


def _issuer_of(cert_no):
    """인증번호 형식으로 발급기관 추정 — 확실하지 않으면 None(지어내지 않는다)."""
    v = (cert_no or "").strip().replace(" ", "")
    if not v or v == "-":
        return None
    if _BPJPH_NO.match(v):
        return "BPJPH"
    if _MUI_NO.match(v):
        return "LPPOM MUI"
    return None


def auditor_note(material, lang="ko"):
    """오디터용 심사 참고 의견 — 왜 걸렸는지·언제 예외인지·이 건은 어떤지·무엇을 할지.

    판정을 바꾸지 않는다. 오디터가 근거를 보고 스스로 판단하도록 정리만 한다."""
    L = _NOTE_L10N.get((lang or "ko").lower(), _NOTE_L10N["ko"])
    _S, _CAT, _STAT, _SRC, EVID, ALT = _explain_l10n(lang)
    name = getattr(material, "name", "") or ""
    result = getattr(material, "screen_result", None)
    cert_no = (getattr(material, "cert_no", None) or "").strip()
    ev_done = bool(getattr(material, "evidence_provided", False))
    # 판정 당시 매칭된 항목을 그대로 쓴다 — 이름으로 다시 맞추면 그때와 달라질 수 있고,
    # 실제로 매칭된 재료를 '사전에 없다'고 잘못 적었다.
    uid = getattr(material, "matched_uid", None)
    it = _by_uid(uid) or _match(name, getattr(material, "e_number", None))

    # 설명은 판정 당시 매칭된 성분명으로 뽑는다. 제품 표기('DAGING POTONG 1 KG BRAVOO')로
    # 다시 맞추면 사전에 없다고 나와 '미등재'라는 틀린 이유가 붙는다(실측).
    ex = explain(it.canonical_name if it else name,
                 getattr(material, "e_number", None),
                 getattr(material, "source", None), cert_no or None,
                 getattr(material, "note", "") or "", lang)
    why = [x for x in (ex.get("explanation") or "").split("\n") if x.strip()]
    if not it and not why:
        why = [L["why_unmatched"]]
    elif not it:
        why.append(L["why_unmatched"])
    exempt = []
    if it and it.required_evidence:
        exempt.append(", ".join(EVID.get(x, x) for x in it.required_evidence))
    if it and it.alternatives:
        exempt += [ALT.get(a, a) for a in it.alternatives]

    this = []
    issuer = _issuer_of(cert_no)
    if cert_no and issuer:
        this.append(L["cert_ok"] % (cert_no, issuer))
    elif cert_no:
        this.append(L["cert_unknown"] % cert_no)
    else:
        this.append(L["no_cert"])
    this.append(L["evidence_yes"] if ev_done else L["evidence_no"])

    if result == "BLOCK":
        action = L["act_block"]
    elif result in ("PASS", "CLEARED"):
        action = L["act_none"]
    elif cert_no and issuer:
        action = L["act_verify"]
    else:
        action = L["act_collect"]

    return {"draft_notice": L["draft"],
            "why": {"label": L["why"], "text": [x for x in why if x]},
            "exemption": {"label": L["exempt"], "text": exempt},
            "this_case": {"label": L["this"], "text": this},
            "action": {"label": L["action"], "text": action},
            "cert_issuer": issuer, "cert_no": cert_no or None}
