"""Document Intake AI — 설계 Part 5: ZIP → 파싱(OCR/PDF/DOCX) → gemma3 분류 → 필수서류 라우팅."""
import base64
import io
import os
import re
import zipfile
from . import ai_local

_MIME = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
         "bmp": "image/bmp", "tiff": "image/tiff", "tif": "image/tiff", "webp": "image/webp",
         "txt": "text/plain; charset=utf-8", "csv": "text/csv",
         "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         "mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm",
         "m4v": "video/x-m4v"}


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
    "quality_cert",             # 품질/식품안전 인증(HACCP·ISO·GMP·FSSC) — 할랄 인증 아님
    "sjph_manual",              # SJPH/HPAS 매뉴얼
    "other",
]
DOC_KO = {
    "nib_business_license": "사업자등록증(NIB)", "factory_registration": "공장등록증",
    "product_list": "제품 목록", "process_flow": "공정 흐름도",
    "halal_certificate": "할랄 인증서", "material_list": "원재료 목록",
    "coa_msds": "CoA/MSDS 성분명세", "supplier_declaration": "공급사 선언서",
    "quality_cert": "품질/식품안전 인증(HACCP·FSSC·GMP)",
    "sjph_manual": "SJPH 매뉴얼", "other": "기타/미분류",
}
# 신청 필수 서류(라우팅 대상)
REQUIRED_DOCS = ["nib_business_license", "factory_registration", "product_list",
                 "process_flow", "material_list", "halal_certificate", "sjph_manual"]

_IMG = ("png", "jpg", "jpeg", "bmp", "tiff", "tif", "webp")


def _zip_name(zi):
    """한국어 Windows ZIP은 파일명이 CP949인데 UTF-8 플래그(0x800)가 없으면
    zipfile이 CP437로 디코드해 깨진다(┴╓..). CP437로 되돌려 CP949로 재디코드."""
    name = zi.filename
    if not (zi.flag_bits & 0x800):
        for enc in ("cp949", "euc-kr", "utf-8"):
            try:
                return name.encode("cp437").decode(enc)
            except Exception:
                continue
    return name


def extract_zip(data):
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for zi in z.infolist():
            if zi.is_dir():
                continue
            name = _zip_name(zi)
            base = os.path.basename(name)
            if not base or base.startswith("."):
                continue
            try:
                out.append((name, z.read(zi)))
            except Exception:  # noqa: BLE001
                pass
    return out


def _decode_text(data):
    """OS/로케일 독립 텍스트 디코드 — UTF-8→CP949→EUC-KR 순 시도(한국 Windows 파일 대응)."""
    for enc in ("utf-8", "cp949", "euc-kr", "latin-1"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", "ignore")


_OCR_MIN_CONF = float(os.environ.get("GLHAC_OCR_MIN_CONF", "0.5"))
_OCR_DPI = int(os.environ.get("GLHAC_OCR_DPI", "140"))   # 깨끗한 스캔은 140이 충분(측정), 저품질만 env 상향


def _ocr_bytes(data, ext):
    """OS 독립 임시파일 OCR. confidence 낮은(오인식) 라인 제거로 품질↑.
    (LLM 한글교정은 qwen2.5 테스트 결과 성분명 환각으로 더 악화 → 미채택, 할랄 안전)."""
    import tempfile
    fd, path = tempfile.mkstemp(suffix="." + ext)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        lines = ai_local.ocr_image(path).get("lines", [])
        kept = [l["text"] for l in lines if l.get("confidence", 1) >= _OCR_MIN_CONF]
        if not kept and lines:            # 전부 저confidence면 폴백(빈 텍스트 방지)
            kept = [l["text"] for l in lines]
        return " ".join(kept)
    except Exception:  # noqa: BLE001
        return ""
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def parse_file(name, data, dpi=None):
    """확장자별 텍스트 추출 — OS 독립. 이미지/스캔PDF=OCR, PDF=fitz, docx/xlsx/txt.
    dpi 지정 시 스캔 렌더 해상도 오버라이드(고해상도 재처리용, 기본 _OCR_DPI)."""
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    _dpi = int(dpi) if dpi else _OCR_DPI
    _dpi = max(72, min(600, _dpi))   # 안전 범위
    try:
        if ext in _IMG:
            return _ocr_bytes(data, ext)
        if ext == "pdf":
            import fitz
            doc = fitz.open(stream=data, filetype="pdf")
            txt = ""
            for pg in list(doc)[:8]:
                t = pg.get_text()
                if len(t.strip()) < 20:  # 스캔본 → PNG 렌더 후 OCR (파일경로 없이 bytes)
                    t = _ocr_bytes(pg.get_pixmap(dpi=_dpi).tobytes("png"), "png")
                txt += t + "\n"
            return txt
        if ext in ("txt", "csv"):
            return _decode_text(data)
        if ext == "docx":
            import docx
            d = docx.Document(io.BytesIO(data))
            return "\n".join(pp.text for pp in d.paragraphs)
        if ext == "xlsx":
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            out = []
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    cells = [str(c) for c in row if c is not None]
                    if cells:
                        out.append(" ".join(cells))
            wb.close()
            return "\n".join(out)[:20000]
    except Exception:  # noqa: BLE001
        return ""
    return ""


_CLASSIFY_SYS = (
    "당신은 할랄 인증 서류 분류기입니다. 파일명과 본문 발췌를 보고 doc_type을 분류하고 핵심 필드를 추출하세요. "
    "doc_type은 반드시 다음 중 하나: " + ", ".join(DOC_TYPES) + ". "
    "address는 회사(사업자) 주소, city/country/zip은 회사 주소의 도시/국가/우편번호. "
    "factory_address는 공장 주소, factory_city/factory_country/factory_zip은 공장 주소의 도시/국가/우편번호. "
    "factory_reg_no는 공장/영업 등록번호, responsible_person은 대표자/책임자 이름. "
    "process_steps는 공정흐름도의 공정 단계 순서 목록(원료입고→배합→가열→포장 등). "
    "has_commitment/has_materials/has_process/has_product/has_monitoring는 SJPH 매뉴얼에 "
    "해당 5요소(약속·원재료·공정·제품·모니터링)가 포함되면 true. 없으면 null/false/[]. "
    '반드시 JSON으로만: {"doc_type":"...","confidence":0.0,'
    '"fields":{"company_name":null,"nib":null,"address":null,"city":null,"country":null,"zip":null,'
    '"factory_address":null,"factory_city":null,"factory_country":null,"factory_zip":null,'
    '"responsible_person":null,"factory_reg_no":null,"product_names":[],"cert_no":null,'
    '"issuer":null,"expiry_date":null,"material_names":[],"process_steps":[],'
    '"has_commitment":false,"has_materials":false,"has_process":false,'
    '"has_product":false,"has_monitoring":false}}'
)


# 파일명 규칙 — LLM 오분류 교정. 위에서부터 먼저 매칭되는 규칙이 최종 doc_type을 결정한다.
# 파일명이 명백한 경우(소개서·제안서·수입서류·품질인증 등)는 LLM 판단을 무시하고 규칙을 신뢰한다.
_NAME_RULES = [
    # (정규식, doc_type, 사유) — 순서 중요(구체적인 것 먼저)
    (r"제조\s*공정\s*도|공정\s*흐름|process\s*flow|flow\s*chart", "process_flow", "공정도"),
    (r"fssc|haccp|\biso\b|\bgmp\b|22000|식품안전|유기취급|organic|kosher", "quality_cert", "품질/식품안전 인증(HACCP·FSSC·ISO·GMP — 할랄 아님)"),
    (r"소개서|회사\s*소개|company\s*profile|제안서|proposal|접수\s*양식|고객\s*접수|데이터\s*양식|intake\s*form|application\s*form", "other", "소개서·제안서·양식(등록증 아님)"),
    (r"원산지|수입\s*서류|country\s*of\s*origin|\bcoo\b|certificate\s*of\s*origin|선언서|declaration|확인서|설명서", "supplier_declaration", "원산지·수입·선언서(공급사 선언)"),
    (r"성적서|시험\s*성적|\bcoa\b|\bmsds\b|성분\s*명세|성분\s*분석|분석\s*성적", "coa_msds", "성적서·성분명세"),
    (r"사업자\s*등록|사업자등록증|business\s*(registration|license)|\bnib\b|법인\s*등기|등록증명원", "nib_business_license", "사업자등록"),
    (r"공장\s*등록|공장등록증|factory\s*registration|manufactur.*licen", "factory_registration", "공장등록"),
    (r"할랄\s*인증|halal\s*cert", "halal_certificate", "할랄 인증"),
]


def refine_doctype_reason(name, llm_type):
    """(doc_type, 규칙근거) — 파일명 규칙이 매칭되면 그 규칙을, 아니면 (llm_type, None)."""
    n = (name or "").lower()
    for pat, dt, why in _NAME_RULES:
        if re.search(pat, n):
            return dt, why
    return llm_type, None


def _refine_doctype(name, llm_type):
    return refine_doctype_reason(name, llm_type)[0]


def classify(name, text):
    if not text.strip():
        return {"doc_type": "other", "confidence": 0.0, "fields": {}, "empty": True}
    r = ai_local.llm_json(_CLASSIFY_SYS, "파일명: %s\n본문 발췌:\n%s" % (name, text[:2000]))
    if not isinstance(r, dict) or "doc_type" not in r:
        r = {"doc_type": "other", "confidence": 0.0, "fields": {}}
    r["doc_type"] = _refine_doctype(name, r.get("doc_type"))  # 파일명 규칙 교정
    if r.get("doc_type") not in DOC_TYPES:
        r["doc_type"] = "other"
    r.setdefault("confidence", 0.0)
    r.setdefault("fields", {})
    return r


# 개별 업로드 컨텍스트 파싱 — doc_type별 추출 필드 스펙
_FIELD_SPEC = {
    "nib_business_license": ("회사명, NIB(사업자등록번호), 주소, 도시, 국가, 우편번호",
                             '{"company_name":null,"nib":null,"address":null,"city":null,"country":null,"zip":null}'),
    "factory_registration": ("공장등록번호, 공장 주소, 도시, 국가, 우편번호",
                             '{"factory_reg_no":null,"factory_address":null,"factory_city":null,"factory_country":null,"factory_zip":null}'),
    "halal_certificate": ("인증번호, 발급기관, 만료일, 대상(제품/원재료)",
                          '{"cert_no":null,"issuer":null,"expiry_date":null,"scope":null}'),
    "quality_cert": ("인증종류(HACCP/ISO/GMP/FSSC), 인증번호, 만료일",
                     '{"cert_type":null,"cert_no":null,"expiry_date":null}'),
    "consent": ("서명 여부, 서명일", '{"signed":false,"signed_date":null}'),
    "product_list": ("제품명 목록", '{"product_names":[]}'),
    "material_list": ("원재료명 목록", '{"material_names":[]}'),
    "process_flow": ("공정 단계 순서 목록(원료입고→배합→가열→충전→포장 등)",
                     '{"process_steps":[]}'),
    "sjph_manual": ("SJPH 5요소 포함 여부(약속·원재료·공정·제품·모니터링)",
                    '{"has_commitment":false,"has_materials":false,"has_process":false,'
                    '"has_product":false,"has_monitoring":false}'),
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


_APPLICANT_DOCS = ("nib_business_license", "factory_registration")


def aggregate_fields(docs):
    """분류 문서들의 추출 필드를 신청서용으로 집계.
    회사명·NIB·주소·책임자·공장등록번호는 신청기업 서류(사업자/공장등록증)에서만 취함(공급사 제외)."""
    agg = {"company_name": None, "nib": None, "address": None, "factory_address": None,
           "city": None, "country": None, "zip": None,
           "factory_city": None, "factory_country": None, "factory_zip": None,
           "responsible_person": None,
           "factory_reg_no": None, "products": [], "materials": [], "certificates": []}
    for d in docs:
        f = d.get("fields") or {}
        applicant = d.get("doc_type") in _APPLICANT_DOCS or d.get("doc_type") is None
        if applicant:
            if not agg["company_name"] and f.get("company_name"):
                agg["company_name"] = f["company_name"]
            if not agg["nib"] and f.get("nib"):
                agg["nib"] = f["nib"]
            # 회사 주소(NIB)와 공장 주소(공장등록증)를 분리 — 뭉치면 회사주소가 공장주소로 잘못 저장됨
            if not agg["address"] and f.get("address") and d.get("doc_type") != "factory_registration":
                agg["address"] = f["address"]
            if not agg["factory_address"] and f.get("factory_address"):
                agg["factory_address"] = f["factory_address"]
            if d.get("doc_type") == "factory_registration" and not agg["factory_address"] and f.get("address"):
                agg["factory_address"] = f["address"]   # 공장등록증의 주소는 공장 주소
            for _c in ("city", "country", "zip"):        # 회사 도시/국가/우편(NIB)
                if not agg[_c] and f.get(_c) and d.get("doc_type") != "factory_registration":
                    agg[_c] = f[_c]
            for _fc in ("factory_city", "factory_country", "factory_zip"):   # 공장 도시/국가/우편(공장등록증)
                if not agg[_fc] and f.get(_fc):
                    agg[_fc] = f[_fc]
            if not agg["responsible_person"] and f.get("responsible_person"):
                agg["responsible_person"] = f["responsible_person"]
            if not agg["factory_reg_no"] and f.get("factory_reg_no"):
                agg["factory_reg_no"] = f["factory_reg_no"]
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


def intake_zip_iter(data, limit=200):
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
