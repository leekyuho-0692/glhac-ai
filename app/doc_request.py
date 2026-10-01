"""부족 서류 요청서 생성(P33). 요구 증빙 전체를 나열하지 않고 실제로 미충족인 것만,
무엇을 보내면 되는지와 왜 필요한지를 고정 문구로 만든다."""

_REASON_ORDER = (
    "NOT_SUBMITTED",
    "SUBMITTED_CONTENT_MISSING",
    "SUBMITTED_FOR_OTHER_MATERIAL",
    "SUPPORTING_ONLY",
)

_UNMET = ("NOT_SATISFIED", "NOT_SCOPED", "SUPPORTING_ONLY")


def _labels(lang):
    from . import domain_dict as dd
    from .intake import DOC_KO, DOC_EN, DOC_ID

    try:
        evidence_labels = dd.code_labels("EVIDENCE", "evidence_code", lang) or {}
    except Exception:
        evidence_labels = {}

    if lang == "en":
        doc_labels = DOC_EN
    elif lang == "id":
        doc_labels = DOC_ID
    else:
        doc_labels = DOC_KO
    return evidence_labels, doc_labels


def _accepted_doc_types(code):
    """정본으로 인정되는 문서유형만. supporting 은 요청 대상이 아니다(보조자료를 더 받아도 해소되지 않는다)."""
    from . import evidence_map as em

    entry = (em._load().get("evidence") or {}).get(code) or {}
    out = []
    for kind in ("primary", "acceptable"):
        for t in entry.get(kind) or []:
            c = em.canonical_doc_type(t)
            if c and c not in out:
                out.append(c)
    return out


def _reason_code(verdict, code, docs):
    from . import evidence_map as em

    if verdict == "SUPPORTING_ONLY":
        return "SUPPORTING_ONLY"
    if verdict == "NOT_SCOPED":
        return "SUBMITTED_FOR_OTHER_MATERIAL"
    if verdict == "NOT_SATISFIED":
        entry = (em._load().get("evidence") or {}).get(code) or {}
        accepted = set()
        for kind in ("primary", "acceptable"):
            for t in entry.get(kind) or []:
                c = em.canonical_doc_type(t)
                if c:
                    accepted.add(c)
        for doc in docs or []:
            c = em.canonical_doc_type((doc or {}).get("doc_type"))
            if c and c in accepted:
                return "SUBMITTED_CONTENT_MISSING"
        return "NOT_SUBMITTED"
    return "NOT_SUBMITTED"


def material_requests(material_key, material_name, required_codes, docs,
                      subject=None, lang="ko", content_check="strict"):
    from . import evidence_map as em

    evidence_labels, doc_labels = _labels(lang)
    result = em.coverage(required_codes, docs, content_check=content_check, subject=subject)

    reasons = (em._load().get("request_reasons") or {})
    out = []
    for item in (result or {}).get("items") or []:
        verdict = item.get("verdict")
        if verdict not in _UNMET:
            continue
        raw_code = item.get("code")
        code = item.get("resolved_code") or raw_code
        reason_code = _reason_code(verdict, raw_code, docs)

        reason_entry = reasons.get(reason_code) or {}
        reason_text = reason_entry.get(lang)
        if not reason_text:
            reason_text = reason_entry.get("ko") or ""

        label = evidence_labels.get(code) or item.get("label") or code
        accepted = _accepted_doc_types(raw_code)

        out.append({
            "material_key": material_key,
            "material_name": material_name,
            "code": code,
            "label": label,
            "verdict": verdict,
            "reason_code": reason_code,
            "reason_text": reason_text,
            "accepted_doc_types": [
                {"doc_type": t, "label": doc_labels.get(t, t)} for t in accepted
            ],
        })
    return out


def build_request_sheet(materials, lang="ko", content_check="strict"):
    groups = {}
    materials_needing = set()
    request_count = 0

    for m in materials or []:
        key = m.get("key")
        name = m.get("name")
        reqs = material_requests(
            key, name, m.get("required_codes"), m.get("docs"),
            subject=m.get("subject"), lang=lang, content_check=content_check,
        )
        for r in reqs:
            request_count += 1
            materials_needing.add(key)
            gkey = (r["code"], r["reason_code"])
            g = groups.get(gkey)
            if g is None:
                g = {
                    "code": r["code"],
                    "label": r["label"],
                    "reason_code": r["reason_code"],
                    "reason_text": r["reason_text"],
                    "accepted_doc_types": r["accepted_doc_types"],
                    "materials": [],
                    "material_count": 0,
                }
                groups[gkey] = g
            if not any(mm["key"] == key for mm in g["materials"]):
                g["materials"].append({"key": key, "name": name})
                g["material_count"] += 1

    def sort_key(g):
        try:
            ridx = _REASON_ORDER.index(g["reason_code"])
        except ValueError:
            ridx = len(_REASON_ORDER)
        return (-g["material_count"], ridx, g["code"])

    grouped = sorted(groups.values(), key=sort_key)

    by_reason = {}
    for g in grouped:
        by_reason[g["reason_code"]] = by_reason.get(g["reason_code"], 0) + 1

    return {
        "lang": lang,
        "groups": grouped,
        "material_count": len(materials_needing),
        "request_count": request_count,
        "by_reason": by_reason,
    }


def render_text(sheet):
    """요청서 평문 렌더. 문구는 전부 evidence_map.json 에서 꺼낸다 — 렌더러가 한국어를 박으면
    인니어 심사자 시트에 한글이 남는다."""
    from . import evidence_map as em

    lang = sheet.get("lang") or "ko"
    t = (em._load().get("request_sheet_text") or {})

    def w(key, **kw):
        tbl = t.get(key) or {}
        s = tbl.get(lang) or tbl.get("ko") or ""
        return (s % kw) if kw else s

    lines = [w("header", req=sheet.get("request_count", 0), mat=sheet.get("material_count", 0))]
    blocks = []
    for g in sheet.get("groups") or []:
        block = [
            "\u25a0 %s  [%s]" % (g.get("label"), g.get("reason_code")),
            "   %s: %s" % (w("reason"), g.get("reason_text") or ""),
        ]
        accepted = g.get("accepted_doc_types") or []
        if accepted:
            block.append("   %s: %s" % (w("accepted"),
                                        ", ".join(a.get("label") for a in accepted)))
        names = [mm.get("name") for mm in (g.get("materials") or [])]
        shown = names[:8]
        tail = w("more", n=len(names) - 8) if len(names) > 8 else ""
        block.append("   %s (%d): %s%s" % (w("targets"),
                                           g.get("material_count", len(names)),
                                           ", ".join(shown), tail))
        blocks.append("\n".join(block))

    if blocks:
        lines.append("")
        lines.append("\n\n".join(blocks))
    return "\n".join(lines)


def subject_names(material_name, canonical_name=None, aliases=None):
    """지목 판정에 쓸 이름 후보. 원료명 + ontology 정규명 + 별칭 전부.
    중복·공백을 걷어낸다. 순서는 원료명이 먼저다."""
    raw = [material_name, canonical_name]
    if isinstance(aliases, dict):
        for v in aliases.values():
            if isinstance(v, (list, tuple, set)):
                raw.extend(v)
            else:
                raw.append(v)
    elif isinstance(aliases, (list, tuple, set)):
        raw.extend(aliases)
    out = []
    for x in raw:
        s = str(x).strip() if x is not None else ""
        if s and s not in out:
            out.append(s)
    return out


def build_case_sheet(db, case_id, lang="ko", content_check="strict", unresolved_only=True):
    """케이스 하나의 부족 서류 요청서. ORM 세션을 받아 원료·문서·ontology 를 모아
    build_request_sheet 에 넘긴다."""
    from .models import (Material, DocumentAsset, IngredientOntology,
                         MaterialEvidenceLink)

    # 링크 테이블이 없는 DB 에서도 요청서는 나와야 한다.
    links_by_doc = {}   # document_id -> {material_id: link_type}
    try:
        for link in db.query(MaterialEvidenceLink).filter_by(case_id=case_id).all():
            did = (link.document_id or "").strip()
            mid = (link.material_id or "").strip()
            if did and mid:
                links_by_doc.setdefault(did, {})[mid] = (link.link_type or "DIRECT").upper()
    except Exception:
        links_by_doc = {}

    onto = {o.ingredient_uid: o for o in db.query(IngredientOntology).all()}

    docs = []
    for d in db.query(DocumentAsset).filter_by(case_id=case_id).all():
        docs.append({
            "doc_type": d.doc_type,
            "fields": d.fields or {},
            "text": (d.filename or "") + " " + (d.filename_en or "") + " " + (d.text_excerpt or ""),
            "text_full": d.text_full or "",
            "material_id": (d.material_id or "").strip(),
            "linked": links_by_doc.get(d.document_id, {}),
        })

    q = db.query(Material).filter_by(case_id=case_id)
    if unresolved_only:
        q = q.filter(Material.screen_result == "NEEDS_EVIDENCE")

    skipped = 0
    items = []
    for m in q.all():
        uid = (m.matched_uid or "").strip()
        o = onto.get(uid)
        required = list(o.required_evidence or []) if o else []
        if not required:
            skipped += 1
            continue
        items.append({
            "key": m.material_id,
            "name": m.name or "",
            "required_codes": required,
            "docs": docs,
            "subject": {
                "id": m.material_id,
                "names": subject_names(m.name, o.canonical_name if o else None,
                                       o.aliases if o else None),
            },
        })

    sheet = build_request_sheet(items, lang=lang, content_check=content_check)
    sheet["case_id"] = case_id
    sheet["skipped"] = skipped
    return sheet
