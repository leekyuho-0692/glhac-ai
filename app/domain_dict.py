"""인도네시아 할랄 인증 도메인 사전 — 원문 표현을 표준 키로 모으고, 그 키에 붙은
업무 Context와 시스템 Action을 돌려준다.

번역 사전과 다른 점: 'penyelia halal'을 '할랄 감독자'로 옮기는 데서 끝나지 않고,
그 역할이 전자서명 대상(requires_signature)이라는 것까지 시스템이 알게 한다.

5계층 (설계서 §2)
  ① surface  원문·현장 표현(동의어·약어·표기변형)
  ② id       표준 키 — 코드가 참조하는 유일값
  ③ labels   언어별 표기
  ④ context  업무 맥락(HPAS·AUDIT·SIGNATURE…)
  ⑤ actions  시스템 동작

원칙: 사전에 없으면 지어내지 않는다. lookup은 None을 돌려주고 호출부가 원문을 유지한다.
"""
import json
import os
import re
import threading

_PATH = os.path.join(os.path.dirname(__file__), "domain_dict.json")
_LOCK = threading.Lock()
_STATE = {"terms": {}, "surface": {}, "axis": {}, "loaded": False}

# 표기 흔들림 흡수 — 곱슬따옴표·비분리공백은 같은 말을 갈라놓는다(실제 파일명에서 발생).
_NORM_MAP = {"“": '"', "”": '"', "‘": "'", "’": "'",
             " ": " ", "​": " "}


def normalize(s):
    """조회용 정규화 — 대소문자·구두점·공백 차이를 없앤다. 내용은 바꾸지 않는다."""
    if not s:
        return ""
    for a, b in _NORM_MAP.items():
        s = s.replace(a, b)
    s = re.sub(r"[^0-9a-z가-힣]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


# 표기 전용 축 — 화면 문구를 담을 뿐 이름 매칭에 쓰지 않는다.
# 여기에 축을 추가할 때는 "이 말로 문서를 찾을 일이 있는가"를 먼저 답해야 한다.
# 답이 아니오면 표기 전용이다.
_DISPLAY_ONLY_AXES = {
    "LABEL",            # 보고서·화면 문장
    "MATERIAL_CAT",     # 성분 분류 표기
    "SOURCE",           # 유래 표기
    "EVIDENCE",         # 증빙 코드 표기
    "ALTERNATIVE",      # 대체재 문구
    "ENUM",             # 상태·판정 등 enum 표기
    "SJPH_EVIDENCE",    # SJPH 증빙 항목 이름
    "HPAS_ELEMENT",     # HPAS 5요소 이름
    "HPAS_REASON",      # HPAS 판정 근거
    "DOC_REQUIREMENT",  # 서류 요건 설명
    "ORG_ROLE",         # 할랄팀 역할 표기
    "BILLING_SERVICE",  # 청구 서비스 유형
    "VERDICT",          # 스크리닝 판정
    "HPAS_CHAPTER",     # SJPH 매뉴얼 장 제목
    "AUDITOR_NOTE",     # 오디터 근거 메모 라벨
    "BLOCKER",          # 자가진단 차단 사유
    "WS_STAGE",         # 워크스페이스 단계
    "VAULT_DOC",        # 자료함 문서 종류
    "ORG_DIV",          # 조직 부서
    "REG_STATE",        # 규정 상태
    "SEVERITY",         # 위험등급
    "INTAKE_ERROR",     # 인테이크 오류
    "GATE",             # 게이트 차단 사유
    "GEN_DOC",          # 생성 문서 종류
    "EXTRACT_FIELD",    # AI 추출 필드 라벨
    "EXTRACT_SRC",      # 그 필드가 나오는 서류
    "EXTRACT_FMT",      # 기대 형식
    "SIGNER",           # 서명·도장 슬롯 이름
    "ONSITE_ITEM",      # 현장 체크리스트 항목
    "WF_PHASE",         # 워크플로 파이프라인 단계
}


def load(path=None):
    """사전 적재. 서버 기동·리로드 시 1회. 같은 표면형이 두 키에 걸리면 먼저 온 쪽을 둔다
    (중복은 사전 오류이므로 조용히 덮어쓰지 않는다)."""
    p = path or _PATH
    with open(p, encoding="utf-8") as f:
        rows = json.load(f)
    terms, surface, axis, label_ko = {}, {}, {}, {}
    dup = []
    for t in rows:
        key = t["id"]
        terms[key] = t
        axis.setdefault(t.get("axis") or "OTHER", []).append(key)
        ko = (t.get("labels") or {}).get("ko")
        if ko:
            label_ko.setdefault(ko, key)      # 한국어 원문 → 키(보고서 문구 조회용)
        if t.get("axis") in _DISPLAY_ONLY_AXES:
            # 표기 전용 항목은 표면형 색인에 넣지 않는다.
            #
            # 두 종류가 한 사전에 산다. (1) 매칭용 — 문서·성분 이름을 표준 키로 잇는다.
            # (2) 표기용 — 코드값을 화면 문구로 바꾼다. 표기용을 매칭 색인에 넣으면
            # '식품첨가물'·'할랄' 같은 말이 두 키에 걸려 조회가 갈리고, 문서 분류가
            # 흔들린다(실측: UI 라벨을 사전에 옮기자 표면형 중복 54건이 터졌다).
            # 표기용은 정확일치 표(label_ko)와 code_labels 로만 쓴다.
            continue
        cands = [t["id"]]
        for v in (t.get("labels") or {}).values():
            cands.append(v)
        for v in (t.get("surface") or {}).values():
            cands += v if isinstance(v, list) else [v]
        for c in cands:
            n = normalize(c)
            if not n:
                continue
            if n in surface and surface[n] != key:
                dup.append((n, surface[n], key))
                continue
            surface[n] = key
    with _LOCK:
        _STATE.update({"terms": terms, "surface": surface, "axis": axis, "loaded": True,
                       "label_ko": label_ko, "duplicates": dup})
    return {"terms": len(terms), "surface": len(surface), "axes": len(axis),
            "label_ko": len(label_ko), "duplicates": dup}


def _ensure():
    if not _STATE["loaded"]:
        load()


def lookup(text, axis=None):
    """원문 표현 → 표준 키. 정확 일치 우선, 없으면 가장 긴 표면형 포함 매칭.
    axis를 주면 그 축으로 한정한다(같은 말이 축마다 다른 뜻일 때)."""
    _ensure()
    n = normalize(text)
    if not n:
        return None
    key = _STATE["surface"].get(n)
    if key and (axis is None or _STATE["terms"][key].get("axis") == axis):
        return key
    best, best_len = None, 0
    for s, k in _STATE["surface"].items():
        if len(s) < 3 or len(s) <= best_len:
            continue
        if axis is not None and _STATE["terms"][k].get("axis") != axis:
            continue
        if re.search(r"(^| )%s( |$)" % re.escape(s), n):   # 단어 경계 — 부분 오탐 차단
            best, best_len = k, len(s)
    return best


def term(key):
    _ensure()
    return _STATE["terms"].get(key)


def label(key, lang="ko"):
    """표준 키 → 언어별 표기. 없는 언어는 ko→en→id 순으로 폴백한다."""
    t = term(key)
    if not t:
        return None
    lb = t.get("labels") or {}
    return lb.get((lang or "ko").lower()) or lb.get("ko") or lb.get("en") or lb.get("id")


def actions(key):
    t = term(key)
    return dict(t.get("actions") or {}) if t else {}


def context(key):
    t = term(key)
    return list(t.get("context") or []) if t else []


def axis_of(key):
    t = term(key)
    return t.get("axis") if t else None


def by_axis(axis):
    _ensure()
    return list(_STATE["axis"].get(axis) or [])


def material_type(raw):
    """서류의 자재유형 값(Jenis Bahan) → (표준 키, 스크리닝 대상 여부).
    'CLEANING AGENT'·'KEMASAN'은 성분 판정 대상이 아니라 설비·포장 검토 대상이다."""
    key = lookup(raw, axis="MATERIAL")
    if not key:
        return None, True          # 모르면 스크리닝은 돌린다(빠뜨리지 않게)
    return key, bool(actions(key).get("screen", True))


def is_msme_scale(raw):
    """신청서 '사업 규모(Skala Usaha)' 값 → 중소영세(MSME) 여부.
    Mikro/Kecil만 자기선언(SEHATI) 자격이 열린다. 모르는 값은 None을 돌려
    호출부가 기존 값을 유지하게 한다 — 지어낸 규모로 경로 자격을 바꾸면 안 된다."""
    k = lookup(raw, axis="SCALE")
    if not k:
        return None
    return bool(actions(k).get("msme"))


def doc_labels(lang="ko"):
    """doc_type → 언어별 표기. intake.DOC_KO/EN/ID를 이 사전에서 파생시킨다.
    같은 doc_type이 여러 항목에 있으면(증빙용 인니 서류) 필수 서류 쪽을 우선한다."""
    _ensure()
    out = {}
    for k in by_axis("DOC"):
        t = _STATE["terms"][k]
        dt = (t.get("actions") or {}).get("doc_type")
        if not dt:
            continue
        if dt in out and not (t.get("actions") or {}).get("required"):
            continue          # 이미 담긴 필수 서류 표기를 증빙용 항목이 덮지 않게
        out[dt] = label(k, lang)
    return out


def doc_type_of(text):
    """서류명(원문 어느 언어든) → 표준 doc_type. 없으면 None."""
    k = lookup(text, axis="DOC")
    return (actions(k) or {}).get("doc_type") if k else None


def evidence_key_of(text):
    """서류명 → SJPH 증빙 항목 키(있으면). 인니 실무 서류가 증빙 항목에 붙는다.

    DOC 축을 먼저 보고, 없으면 축을 풀어 다시 찾는다. 같은 말이 문서 축과 개념 축에
    동시에 있을 수 없어(표면형 전역 유일) 'Internal_Audit' 같은 이름은 개념 축의
    AUDIT_INTERNAL에만 걸리는데, 그 개념이 이미 증빙 항목을 알고 있다."""
    k = lookup(text, axis="DOC") or lookup(text)
    return (actions(k) or {}).get("evidence_key") if k else None


def text(ko, lang="ko"):
    """한국어 원문 문구 → 해당 언어 표기. 사전에 없거나 그 언어 표기가 비면 원문을 그대로 쓴다.

    보고서·화면 문구를 코드에 세 벌 두는 대신 사전 한 곳에서 꺼낸다.
    없는 문구를 지어내지 않는 것이 중요하다 — 빈 문자열을 내보내면 문서에 구멍이 난다."""
    lang = (lang or "ko").lower()
    if lang == "ko" or not ko:
        return ko
    _ensure()
    key = _STATE.get("label_ko", {}).get(ko)
    if not key:
        return ko
    return (term(key).get("labels") or {}).get(lang) or ko


def text_fn(lang):
    """호출부가 L('한국어 문구') 형태로 쓰도록 묶어준다."""
    return lambda s: text(s, lang)


def unit_surfaces():
    """단위 표면형 전체 — 매칭에서 떼어내고 번역에서 표로 고정할 대상."""
    _ensure()
    out = set()
    for k in by_axis("UNIT"):
        for v in (term(k).get("surface") or {}).values():
            out.update(normalize(x) for x in (v if isinstance(v, list) else [v]))
    return {x for x in out if x}


def forbidden_translations(lang):
    """번역 결과에 나오면 안 되는 말 — 단위 오역 차단('개'가 anjing(犬)이 되던 사고)."""
    _ensure()
    out = {}
    for k, t in _STATE["terms"].items():
        fb = (t.get("actions") or {}).get("forbid") or {}
        for w in fb.get((lang or "").lower(), []):
            out.setdefault(w.lower(), []).append(k)
    return out


# 번역이 바꾸면 안 되는 도메인 고유명사 — 기관·직책·제도 이름.
#
# 왜 필요한가(실측): 같은 문장을 3회 번역시켰더니 1회에서 'penyelia halal'(할랄감독자)이
# 'pemegang kehalalan'이라는 없는 말로 바뀌었다. 대문자 약어(BPJPH·SJPH·SIHALAL)는
# 살아남는데, 소문자 복합어인 직책·제도 이름이 번역 대상으로 오인돼 창작된다.
# 심사 문서에서 직책명이 바뀌면 그 문서는 틀린 문서다.
#
# 사전의 표기형(surface)은 건드리지 않는다 — 거기에 손대면 서류 분류 매칭이 흔들린다.
# 보호 목록은 라벨에서 파생하고, 사전에 항목이 없는 제도 약어만 여기에 더한다.
_PROTECTED_AXES = ("ORG", "ROLE", "HPAS", "PROCESS", "FATWA")
_PROTECTED_EXTRA = ("BPJPH", "LPPOM MUI", "LPH", "PPH", "SJPH", "SIHALAL", "SEHATI",
                    "HAS 23000", "KMA 1360", "MUI", "Halal", "Haram", "Syubhat", "Najis")


def protected_terms(lang="id"):
    """해당 언어 번역문에 그대로 남아야 하는 용어들(긴 것부터)."""
    _ensure()
    lg = (lang or "id").lower()
    out = set(_PROTECTED_EXTRA)
    for t in _STATE["terms"].values():
        if t.get("axis") in _PROTECTED_AXES:
            v = (t.get("labels") or {}).get(lg)
            if v and len(v) >= 3:
                out.add(v)
    # 긴 용어가 짧은 용어를 포함할 수 있다(MUI 이 LPPOM MUI 안에) — 긴 것부터 본다
    return sorted(out, key=lambda x: (-len(x), x.lower()))


def missing_protected(src, out, lang="id"):
    """원문에 있었는데 번역문에서 사라진 보호 용어 — 재시도 판단용.

    원문에 없던 용어까지 요구하지 않는다(없는 말을 넣으라는 뜻이 되면 더 나쁘다)."""
    lo_src, lo_out = (src or "").lower(), (out or "").lower()
    return [t for t in protected_terms(lang)
            if t.lower() in lo_src and t.lower() not in lo_out]


_CODE_LABEL_CACHE = {}


def code_labels(axis, action_key, lang="ko"):
    """축의 항목을 {코드: 표기} 표로 — actions[action_key] 값이 코드다.

    코드값(halal·animal_protein·halal_cert)을 화면 문구로 바꾸는 표를 코드에 세 벌씩
    두던 것을 사전 한 곳에서 꺼낸다. 표가 쪼개져 있으면 한쪽만 고쳐진다 — 실측으로
    유래(_SOURCE) 표에서 한국어만 7개 비어 화면에 코드값이 그대로 나오고 있었다."""
    ck = (axis, action_key, (lang or "ko").lower())
    if ck not in _CODE_LABEL_CACHE:
        _ensure()
        out = {}
        for k in by_axis(axis):
            code = (actions(k) or {}).get(action_key)
            if not code:
                continue
            v = (term(k).get("labels") or {}).get(ck[2])
            if v:
                out[code] = v
        _CODE_LABEL_CACHE[ck] = out
    return dict(_CODE_LABEL_CACHE[ck])


def axis_text_map(axis, lang):
    """한국어 문구를 키로 쓰는 축(LABEL 등)의 {한국어: 해당언어} 표."""
    _ensure()
    out = {}
    for k in by_axis(axis):
        lb = term(k).get("labels") or {}
        ko, v = lb.get("ko"), lb.get((lang or "ko").lower())
        if ko and v:
            out[ko] = v
    return out


def stats():
    _ensure()
    return {"terms": len(_STATE["terms"]), "surface_forms": len(_STATE["surface"]),
            "axes": {a: len(v) for a, v in sorted(_STATE["axis"].items())},
            "duplicates": _STATE.get("duplicates") or []}
