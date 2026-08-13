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
        if t.get("axis") == "LABEL":
            # 보고서 문구는 문장이라 표면형 색인에 넣으면 lookup의 포함매칭을 오염시킨다.
            # 정확일치 표(label_ko)로만 쓴다.
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
    """서류명 → SJPH 증빙 항목 키(있으면). 인니 실무 서류가 증빙 항목에 붙는다."""
    k = lookup(text, axis="DOC")
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


def stats():
    _ensure()
    return {"terms": len(_STATE["terms"]), "surface_forms": len(_STATE["surface"]),
            "axes": {a: len(v) for a, v in sorted(_STATE["axis"].items())},
            "duplicates": _STATE.get("duplicates") or []}
