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


def _tokens(text):
    """어순·복수형·구두점 차이를 흡수한 토큰 집합.
    'Sucrose Fatty Acid Esters' 와 'sucrose esters of fatty acids' 가 같은 집합이 된다."""
    out = set()
    for t in re.split(r"[^0-9a-z가-힣]+", (text or "").lower()):
        if not t or t in _STOPWORDS:
            continue
        # 단순 복수형 정규화: acids→acid, esters→ester. 'ss'로 끝나거나 짧은 말은 건드리지 않는다.
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.add(t)
    return out


_STATUS_RANK = {"haram": 0, "mushbooh": 1, "halal": 2}


def _match(name, e_number):
    if e_number:
        it = _CACHE["by_e"].get(e_number.strip().upper())
        if it:
            return it
    if name:
        key = name.strip().lower()
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
    # 아래 6종은 DB에 실제로 쓰이는데 표에 없어 영문 코드가 그대로 인쇄되고 있었다
    # (보고서에 "‘…’은(는) additive 계열 성분이며" 형태로 노출).
    "additive": "식품첨가물", "base": "기초 원료", "ferment": "발효산물",
    "flavor_enhancer": "향미증진제", "glazing": "피막제", "processing_aid": "가공보조제",
}
_STATUS_KO = {"halal": "할랄(허용)", "haram": "금지(하람)", "mushbooh": "의심(mushbooh/샤부하)"}
_SOURCE_KO = {"animal": "동물", "plant": "식물", "ferment": "발효", "synthetic": "합성",
              "mineral": "광물", "microbial": "미생물"}

# --- 다국어 서술 ---------------------------------------------------------
# 판정 근거는 366항목마다 쓰인 글이 아니라 템플릿 문장 + 아래 조회표로 조립된다.
# 따라서 항목별 번역문이 아니라 이 표와 문장만 언어별로 갖추면 된다.
# 미등록 키는 원문(영문 코드 또는 한국어)을 그대로 내보낸다 — 빈칸이 생기지 않게.
_CAT_EN = {
    "animal_protein": "animal protein", "animal_fat": "animal fat", "alcohol": "alcohol",
    "emulsifier": "emulsifier", "gelatin": "gelatin", "enzyme": "enzyme", "flavor": "flavor",
    "colorant": "colorant", "sweetener": "sweetener", "acid": "acidity regulator",
    "preservative": "preservative", "carbohydrate": "carbohydrate/sugar",
    "mineral": "mineral", "vitamin": "vitamin", "other": "other", "additive": "additive",
    "base": "base material", "ferment": "fermentation product",
    "flavor_enhancer": "flavor enhancer", "glazing": "glazing agent",
    "processing_aid": "processing aid",
}
_CAT_ID = {
    "animal_protein": "protein hewani", "animal_fat": "lemak hewani", "alcohol": "alkohol",
    "emulsifier": "pengemulsi", "gelatin": "gelatin", "enzyme": "enzim", "flavor": "perisa",
    "colorant": "pewarna", "sweetener": "pemanis", "acid": "pengatur keasaman",
    "preservative": "pengawet", "carbohydrate": "karbohidrat/gula", "mineral": "mineral",
    "vitamin": "vitamin", "other": "lainnya", "additive": "bahan tambahan pangan",
    "base": "bahan dasar", "ferment": "produk fermentasi",
    "flavor_enhancer": "penguat rasa", "glazing": "bahan pelapis",
    "processing_aid": "bahan penolong",
}
_STATUS_EN = {"halal": "halal (permitted)", "haram": "haram (prohibited)",
              "mushbooh": "doubtful (mushbooh)"}
_STATUS_ID = {"halal": "halal (diperbolehkan)", "haram": "haram (dilarang)",
              "mushbooh": "syubhat (mushbooh)"}
_SOURCE_EN = {"animal": "animal", "plant": "plant", "ferment": "fermentation",
              "synthetic": "synthetic", "mineral": "mineral", "microbial": "microbial",
              "dairy": "dairy", "marine": "marine", "insect": "insect", "human": "human",
              "petrochemical": "petrochemical", "unknown": "unknown", "microbe": "microbial"}
_SOURCE_ID = {"animal": "hewani", "plant": "nabati", "ferment": "fermentasi",
              "synthetic": "sintetis", "mineral": "mineral", "microbial": "mikroba",
              "dairy": "susu", "marine": "laut", "insect": "serangga", "human": "manusia",
              "petrochemical": "petrokimia", "unknown": "tidak diketahui", "microbe": "mikroba"}
# 필요 증빙 코드 — 심사자가 실제로 요구하는 서류명이라 언어별 표기가 필요하다.
_EVID_EN = {
    "alcohol_carrier_check": "alcohol carrier check",
    "animal_source_declaration": "animal source declaration",
    "bone_char_free_declaration": "bone-char-free declaration",
    "carrier_check": "carrier check", "carrier_declaration": "carrier declaration",
    "composition_breakdown": "composition breakdown",
    "enzyme_source_declaration": "enzyme source declaration",
    "halal_cert": "halal certificate",
    "halal_slaughter_cert": "halal slaughter certificate",
    "halal_slaughter_certificate": "halal slaughter certificate",
    "process_declaration": "process declaration", "reformulation": "reformulation",
    "rennet_source_declaration": "rennet source declaration",
    "residual_alcohol_test": "residual alcohol test",
    "source_declaration": "source declaration",
    "supplier_halal_cert": "supplier halal certificate",
}
_EVID_ID = {
    "alcohol_carrier_check": "pemeriksaan pembawa alkohol",
    "animal_source_declaration": "deklarasi sumber hewani",
    "bone_char_free_declaration": "deklarasi bebas arang tulang",
    "carrier_check": "pemeriksaan bahan pembawa",
    "carrier_declaration": "deklarasi bahan pembawa",
    "composition_breakdown": "rincian komposisi",
    "enzyme_source_declaration": "deklarasi sumber enzim",
    "halal_cert": "sertifikat halal",
    "halal_slaughter_cert": "sertifikat penyembelihan halal",
    "halal_slaughter_certificate": "sertifikat penyembelihan halal",
    "process_declaration": "deklarasi proses", "reformulation": "reformulasi",
    "rennet_source_declaration": "deklarasi sumber renet",
    "residual_alcohol_test": "uji residu alkohol",
    "source_declaration": "deklarasi sumber",
    "supplier_halal_cert": "sertifikat halal pemasok",
}
# 대체재 — 한국어로 적힌 값만 옮긴다(영문·화학명은 그대로 두는 것이 정확하다).
_ALT_EN = {
    "HPMC 식물성 캡슐": "HPMC plant capsule", "PG 캐리어 향료": "PG-carrier flavor",
    "광물 인산염": "mineral phosphate", "미생물 rennet whey": "microbial-rennet whey",
    "식물성": "plant-based", "식물성 E471": "plant-based E471",
    "식물성 carbon": "plant-based carbon", "식물성 색소": "plant-based colorant",
    "식물성 쇼트닝": "plant-based shortening", "식물성 스테아르산": "plant-based stearic acid",
    "식물성 왁스": "plant wax", "식물성 지방산": "plant fatty acid",
    "식물성 캐리어 카로틴": "plant-carrier carotene", "식물성 코팅": "plant-based coating",
    "식물성/합성 글리세린": "plant/synthetic glycerin", "식물성유": "vegetable oil",
    "알코올프리 바닐라": "alcohol-free vanilla", "알코올프리 향료": "alcohol-free flavor",
    "카나우바 왁스": "carnauba wax", "합성 cysteine": "synthetic cysteine",
    "합성 glycine": "synthetic glycine", "활성탄": "activated carbon", "효모 유래": "yeast-derived",
}
_ALT_ID = {
    "HPMC 식물성 캡슐": "kapsul nabati HPMC", "PG 캐리어 향료": "perisa berpembawa PG",
    "광물 인산염": "fosfat mineral", "미생물 rennet whey": "whey renet mikroba",
    "식물성": "nabati", "식물성 E471": "E471 nabati", "식물성 carbon": "karbon nabati",
    "식물성 색소": "pewarna nabati", "식물성 쇼트닝": "shortening nabati",
    "식물성 스테아르산": "asam stearat nabati", "식물성 왁스": "lilin nabati",
    "식물성 지방산": "asam lemak nabati", "식물성 캐리어 카로틴": "karoten berpembawa nabati",
    "식물성 코팅": "pelapis nabati", "식물성/합성 글리세린": "gliserin nabati/sintetis",
    "식물성유": "minyak nabati", "알코올프리 바닐라": "vanila bebas alkohol",
    "알코올프리 향료": "perisa bebas alkohol", "카나우바 왁스": "lilin karnauba",
    "합성 cysteine": "sistein sintetis", "합성 glycine": "glisin sintetis",
    "활성탄": "karbon aktif", "효모 유래": "berasal dari ragi",
}
# 템플릿 문장 — 한국어 원문을 키로 쓴다(프런트 T()·보고서 라벨과 같은 방식).
_EXPLAIN_L10N = {
    "en": {
        "‘%s’은(는) %s 계열 성분이며, 기본 할랄 상태는 %s입니다. (주요 유래: %s)":
            "'%s' belongs to the %s category; its baseline halal status is %s. (Main sources: %s)",
        "• 부정물(najis) 위험 성분 — 설비·공정의 할랄 전용/세척(사무 khusus) 이슈가 동반됩니다.":
            "• Najis-risk ingredient — requires dedicated halal lines or ritual cleansing (samak) "
            "of equipment and process.",
        "• 캐리어/용매 점검 대상(%s) — 향료·색소 등의 용매로 알코올/젤라틴이 쓰였는지 확인 필요.":
            "• Carrier/solvent check required (%s) — verify that alcohol or gelatin is not used "
            "as a solvent for flavors or colorants.",
        "→ 판정: 금지(haram). 사용 시 인증 불가 — 대체재로 재설계가 필요합니다.":
            "→ Verdict: haram. Cannot be certified while in use — reformulation with an "
            "alternative is required.",
        "→ 판정: 증빙 필요 — 유래(동물/식물)에 따라 할랄 여부가 갈립니다. 아래 증빙 제출 시 CLEARED로 전환됩니다.":
            "→ Verdict: evidence required — halal status depends on the source (animal/plant). "
            "It becomes CLEARED once the evidence below is submitted.",
        "→ 판정: 할랄 허용(추가 증빙 불요 또는 확보됨).":
            "→ Verdict: halal (no further evidence needed, or already secured).",
        "• 필요 증빙: ": "• Required evidence: ",
        "• 할랄 대체재: ": "• Halal alternatives: ",
        "‘%s’은(는) 온톨로지 정식 등재 성분이 아니며, v1 규칙/키워드 기반으로 ‘%s’ 판정되었습니다.":
            "'%s' is not formally registered in the ontology; the verdict '%s' comes from the "
            "v1 rule/keyword screen.",
        "• v1 위험도: ": "• v1 risk level: ",
        "미상": "unknown",
    },
    "id": {
        "‘%s’은(는) %s 계열 성분이며, 기본 할랄 상태는 %s입니다. (주요 유래: %s)":
            "'%s' termasuk kategori %s; status halal dasarnya %s. (Sumber utama: %s)",
        "• 부정물(najis) 위험 성분 — 설비·공정의 할랄 전용/세척(사무 khusus) 이슈가 동반됩니다.":
            "• Bahan berisiko najis — memerlukan lini khusus halal atau pencucian (samak) pada "
            "peralatan dan proses.",
        "• 캐리어/용매 점검 대상(%s) — 향료·색소 등의 용매로 알코올/젤라틴이 쓰였는지 확인 필요.":
            "• Perlu pemeriksaan pembawa/pelarut (%s) — pastikan alkohol atau gelatin tidak "
            "dipakai sebagai pelarut perisa atau pewarna.",
        "→ 판정: 금지(haram). 사용 시 인증 불가 — 대체재로 재설계가 필요합니다.":
            "→ Putusan: haram. Tidak dapat disertifikasi selama digunakan — perlu reformulasi "
            "dengan bahan alternatif.",
        "→ 판정: 증빙 필요 — 유래(동물/식물)에 따라 할랄 여부가 갈립니다. 아래 증빙 제출 시 CLEARED로 전환됩니다.":
            "→ Putusan: perlu bukti — status halal bergantung pada sumber (hewani/nabati). "
            "Akan menjadi CLEARED setelah bukti di bawah diserahkan.",
        "→ 판정: 할랄 허용(추가 증빙 불요 또는 확보됨).":
            "→ Putusan: halal (tanpa bukti tambahan, atau bukti sudah tersedia).",
        "• 필요 증빙: ": "• Bukti yang diperlukan: ",
        "• 할랄 대체재: ": "• Alternatif halal: ",
        "‘%s’은(는) 온톨로지 정식 등재 성분이 아니며, v1 규칙/키워드 기반으로 ‘%s’ 판정되었습니다.":
            "'%s' belum terdaftar resmi dalam ontologi; putusan '%s' berasal dari aturan/kata "
            "kunci v1.",
        "• v1 위험도: ": "• Tingkat risiko v1: ",
        "미상": "tidak diketahui",
    },
}
_L10N_TABLES = {
    "en": (_CAT_EN, _STATUS_EN, _SOURCE_EN, _EVID_EN, _ALT_EN),
    "id": (_CAT_ID, _STATUS_ID, _SOURCE_ID, _EVID_ID, _ALT_ID),
}


def _explain_l10n(lang):
    """(문장번역기, 카테고리, 상태, 유래, 증빙, 대체재) — 미지원 언어는 한국어 표를 돌려준다."""
    lang = (lang or "ko").lower()
    tabs = _L10N_TABLES.get(lang)
    if not tabs:
        return (lambda s: s), _CAT_KO, _STATUS_KO, _SOURCE_KO, {}, {}
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
