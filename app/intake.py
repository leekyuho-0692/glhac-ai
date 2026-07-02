"""Document Intake AI — 설계 Part 5: ZIP → 파싱(OCR/PDF/DOCX) → gemma3 분류 → 필수서류 라우팅."""
import base64
import io
import os
import zipfile
from . import ai_local

_MIME = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
         "bmp": "image/bmp", "tiff": "image/tiff", "tif": "image/tiff", "webp": "image/webp",
         "txt": "text/plain; charset=utf-8", "csv": "text/csv",
         "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


def _ctype(name):
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    return _MIME.get(ext, "application/octet-stream")

# 할랄인증 서류 종류 (BPJPH/SIHALAL 기준)
DOC_TYPES = [
    "nib_business_license",     # NIB/사업자등록증
    "factory_registration",     # 공장등록증
    "product_list",             # 제품 목록
    "process_flow",             # 공정 흐름도
    "halal_certificate",        # 공급사 할랄 인증서
    "material_list",            # 원재료 목록
    "coa_msds",                 # CoA / MSDS / 성분 명세
    "supplier_declaration",     # 공급사 선언서
    "sjph_manual",              # SJPH/HPAS 매뉴얼
    "other",
]
DOC_KO = {
    "nib_business_license": "사업자등록증(NIB)", "factory_registration": "공장등록증",
    "product_list": "제품 목록", "process_flow": "공정 흐름도",
    "halal_certificate": "할랄 인증서", "material_list": "원재료 목록",
    "coa_msds": "CoA/MSDS 성분명세", "supplier_declaration": "공급사 선언서",
    "sjph_manual": "SJPH 매뉴얼", "other": "기타/미분류",
}
# 신청 필수 서류(라우팅 대상)
REQUIRED_DOCS = ["nib_business_license", "factory_registration", "product_list",
                 "process_flow", "material_list", "halal_certificate", "sjph_manual"]

_IMG = ("png", "jpg", "jpeg", "bmp", "tiff", "tif", "webp")


def extract_zip(data):
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if name.endswith("/"):
                continue
            base = os.path.basename(name)
            if not base or base.startswith("."):
                continue
            try:
                out.append((name, z.read(name)))
            except Exception:  # noqa: BLE001
                pass
    return out


def parse_file(name, data):
    """확장자별 텍스트 추출. 이미지·스캔PDF=PaddleOCR, PDF=텍스트, docx=python-docx."""
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    try:
        if ext in _IMG:
            p = "/tmp/glhac_intake.%s" % ext
            with open(p, "wb") as f:
                f.write(data)
            r = ai_local.ocr_image(p)
            return " ".join(line["text"] for line in r.get("lines", []))
        if ext == "pdf":
            import fitz
            doc = fitz.open(stream=data, filetype="pdf")
            txt = ""
            for pg in list(doc)[:5]:
                t = pg.get_text()
                if len(t.strip()) < 20:  # 스캔본 → 렌더 후 OCR
                    pix = pg.get_pixmap(dpi=140)
                    p = "/tmp/glhac_pdf.png"
                    pix.save(p)
                    t = " ".join(line["text"] for line in ai_local.ocr_image(p).get("lines", []))
                txt += t + "\n"
            return txt
        if ext == "txt":
            return data.decode("utf-8", "ignore")
        if ext == "docx":
            import docx
            d = docx.Document(io.BytesIO(data))
            return "\n".join(p.text for p in d.paragraphs)
    except Exception:  # noqa: BLE001
        return ""
    return ""


_CLASSIFY_SYS = (
    "당신은 할랄 인증 서류 분류기입니다. 파일명과 본문 발췌를 보고 doc_type을 분류하고 핵심 필드를 추출하세요. "
    "doc_type은 반드시 다음 중 하나: " + ", ".join(DOC_TYPES) + ". "
    '반드시 JSON으로만: {"doc_type":"...","confidence":0.0,'
    '"fields":{"company_name":null,"nib":null,"product_names":[],"cert_no":null,'
    '"issuer":null,"expiry_date":null,"material_names":[]}}'
)


def classify(name, text):
    if not text.strip():
        return {"doc_type": "other", "confidence": 0.0, "fields": {}, "empty": True}
    r = ai_local.llm_json(_CLASSIFY_SYS, "파일명: %s\n본문 발췌:\n%s" % (name, text[:2000]))
    if not isinstance(r, dict) or "doc_type" not in r:
        return {"doc_type": "other", "confidence": 0.0, "fields": {}}
    if r.get("doc_type") not in DOC_TYPES:
        r["doc_type"] = "other"
    r.setdefault("confidence", 0.0)
    r.setdefault("fields", {})
    return r


# 개별 업로드 컨텍스트 파싱 — doc_type별 추출 필드 스펙
_FIELD_SPEC = {
    "nib_business_license": ("회사명, NIB(사업자등록번호), 주소",
                             '{"company_name":null,"nib":null,"address":null}'),
    "factory_registration": ("공장등록번호, 공장 주소",
                             '{"factory_reg_no":null,"factory_address":null}'),
    "halal_certificate": ("인증번호, 발급기관, 만료일, 대상(제품/원재료)",
                          '{"cert_no":null,"issuer":null,"expiry_date":null,"scope":null}'),
    "quality_cert": ("인증종류(HACCP/ISO/GMP/FSSC), 인증번호, 만료일",
                     '{"cert_type":null,"cert_no":null,"expiry_date":null}'),
    "consent": ("서명 여부, 서명일", '{"signed":false,"signed_date":null}'),
    "product_list": ("제품명 목록", '{"product_names":[]}'),
    "material_list": ("원재료명 목록", '{"material_names":[]}'),
    "product_label": ("성분 목록", '{"ingredients":[]}'),
}


def parse_typed(doc_type, filename, data):
    """업로드 칸(문구)에 맞는 doc_type으로 해당 필드만 파싱."""
    text = parse_file(filename, data)
    if not text.strip():
        return {"doc_type": doc_type, "fields": {}, "confidence": 0.0, "empty": True, "text_len": 0}
    spec = _FIELD_SPEC.get(doc_type)
    if not spec:
        cl = classify(filename, text)
        cl["text_len"] = len(text)
        return cl
    desc, fmt = spec
    sys = "문서에서 다음 필드만 추출하세요(없으면 null). 반드시 JSON으로만: " + fmt + " (대상: " + desc + ")"
    r = ai_local.llm_json(sys, "파일명: %s\n본문:\n%s" % (filename, text[:2500]))
    fields = r if isinstance(r, dict) else {}
    return {"doc_type": doc_type, "fields": fields, "confidence": 0.85,
            "text_len": len(text), "excerpt": text[:300]}


def aggregate_fields(docs):
    """분류 문서들의 추출 필드를 신청서용으로 집계."""
    agg = {"company_name": None, "nib": None, "products": [], "materials": [], "certificates": []}
    for d in docs:
        f = d.get("fields") or {}
        if not agg["company_name"] and f.get("company_name"):
            agg["company_name"] = f["company_name"]
        if not agg["nib"] and f.get("nib"):
            agg["nib"] = f["nib"]
        for p in (f.get("product_names") or []):
            if p and p not in agg["products"]:
                agg["products"].append(p)
        for m in (f.get("material_names") or []):
            if m and m not in agg["materials"]:
                agg["materials"].append(m)
        if f.get("cert_no"):
            agg["certificates"].append({"cert_no": f.get("cert_no"), "issuer": f.get("issuer"),
                                        "expiry": f.get("expiry_date")})
    return agg


def intake_zip_iter(data, limit=30):
    """ZIP → 파일별 파싱+분류를 진행하며 진행상황을 yield (스트리밍용).

    yield ("progress", {...})  파일 처리할 때마다
    yield ("result", {...})    마지막에 전체 결과(라우팅·집계)
    """
    files = extract_zip(data)
    total = len(files[:limit])
    docs = []
    yield ("start", {"total": total})
    for i, (name, raw) in enumerate(files[:limit], 1):
        text = parse_file(name, raw)
        cl = classify(name, text)
        d = {"filename": os.path.basename(name), "doc_type": cl["doc_type"],
             "doc_type_ko": DOC_KO.get(cl["doc_type"], cl["doc_type"]),
             "confidence": cl.get("confidence", 0.0), "fields": cl.get("fields", {}),
             "text_len": len(text), "excerpt": text[:300],
             "content_b64": base64.b64encode(raw).decode() if len(raw) < 3_000_000 else None,
             "content_type": _ctype(name)}
        docs.append(d)
        yield ("progress", {"done": i, "total": total, "file": d["filename"],
                            "doc_type_ko": d["doc_type_ko"]})
    found = {d["doc_type"] for d in docs}
    checklist = [{"doc_type": dt, "doc_type_ko": DOC_KO[dt], "satisfied": dt in found,
                  "files": [d["filename"] for d in docs if d["doc_type"] == dt]}
                 for dt in REQUIRED_DOCS]
    missing = [c["doc_type_ko"] for c in checklist if not c["satisfied"]]
    yield ("result", {"file_count": len(files), "classified": docs, "checklist": checklist,
                      "missing": missing, "complete": len(missing) == 0,
                      "extracted": aggregate_fields(docs)})


def intake_zip(data):
    """동기 래퍼 — 진행 이벤트를 소진하고 최종 결과만 반환."""
    res = None
    for kind, payload in intake_zip_iter(data):
        if kind == "result":
            res = payload
    return res
