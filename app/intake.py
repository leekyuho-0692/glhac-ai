"""Document Intake AI — 설계 Part 5: ZIP → 파싱(OCR/PDF/DOCX) → gemma3 분류 → 필수서류 라우팅."""
import base64
import io
import os
import re
import unicodedata
import logging
import math
import zipfile
import zlib
from xml.etree.ElementTree import ParseError
from . import ai_local

log = logging.getLogger(__name__)

# ZIP 추출 상한 — 압축폭탄 방어
# 업로드 1건 상한 20MB (SRS NFR-05) — main.py 의 _UPLOAD_MAX_BYTES 와 원본 보관(content_b64) 상한이 같은 상수를 쓴다.
UPLOAD_MAX_BYTES = 20 * 1024 * 1024
PDF_MAX_PAGES = 40          # PDF OCR/LLM 최대 처리 쪽수(native text 사전 스캔은 전체)
PDF_MAX_TEXT_CHARS = 200000  # PDF 추출 텍스트 상한(20만 자)
OFFICE_MAX_TEXT_CHARS = PDF_MAX_TEXT_CHARS
ZIP_MAX_ENTRIES = 200   # 압축 묶음 1건에서 처리할 최대 파일 수
ZIP_MAX_UNCOMPRESSED = int(os.environ.get("GLHAC_ZIP_MAX_UNCOMPRESSED", "200000000"))  # 기본 200MB
ZIP_MAX_SINGLE_FILE = int(os.environ.get("GLHAC_ZIP_MAX_SINGLE_FILE", ZIP_MAX_UNCOMPRESSED))  # 단일 파일도 동일
ZIP_MAX_COMPRESSION_RATIO = float(os.environ.get("GLHAC_ZIP_MAX_COMPRESSION_RATIO", "100"))  # 압축비 100 초과 차단

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
    "origin_certificate",       # 원산지증명서 — 유래(source) 증빙의 정본
    "material_list",            # 원재료 목록
    "coa_msds",                 # CoA / MSDS / 성분 명세
    "supplier_declaration",     # 공급사 선언서
    "quality_cert",             # 품질/식품안전 인증(HACCP·ISO·GMP·FSSC) — 할랄 인증 아님
    "sjph_manual",              # SJPH/HPAS 매뉴얼
    # ── 물류(jasa logistik) 고유 서류 ──
    "logistics_scope",          # 취급 화물·서비스 범위(어떤 제품군을, 어떤 jasa로)
    "warehouse_layout",         # 창고 배치도 + Halal Zone 구획
    "vehicle_list",             # 차량·컨테이너 목록(인증 대상은 서비스지만 관리대상)
    "cleaning_sop",             # 세척 SOP(차량·컨테이너·창고) + Sertu 절차
    "other",
]

# DOC_TYPES 는 LLM 분류기에 주는 어휘다.
# 다른 경로가 직접 기록하는 유형(sjph_evidence — SJPH 증빙 적재 엔드포인트가 쓴다)을
# 여기 넣으면 분류기가 일반 문서를 그쪽으로 끌어간다.
# 선언된 전체 어휘는 DOC_TYPES_ALL 로 따로 둔다.
DOC_TYPES_ALL = DOC_TYPES + ["sjph_evidence"]

# 서류명 3개 언어 — 도메인 사전(domain_dict.json)의 DOC 축에서 파생한다.
# 전에는 여기 세 벌을 손으로 유지했는데, 같은 말을 사전과 코드가 따로 갖고 있어
# 인니어 원문 표기('Diagram alir proses produksi')를 서류 유형으로 잇지 못했다.
from .domain_dict import doc_labels as _doc_labels   # noqa: E402

DOC_KO = _doc_labels("ko")
DOC_EN = _doc_labels("en")
DOC_ID = _doc_labels("id")
DOC_NAME_L10N = {"en": DOC_EN, "id": DOC_ID}
# 신청 필수 서류 — 경로별로 다르다.
#
# 왜 나누는가: 인도네시아 소규모 자기선언(SEHATI)은 NIB 하나로 사업자·시설을 갈음하고,
# 공급사 할랄 인증서는 사본 대신 인증번호를 BPJPH가 직접 대조한다. 한국 정규 경로 기준
# 목록을 그대로 적용하면 인니 완성본이 '서류 부족'으로 잘못 표시된다(CV CITRA 실측).
# 목록에서 뺀다고 검토를 건너뛰는 것이 아니라, 그 경로에서 '요구되지 않는 서류'를
# 미제출로 세지 않는 것이다.
REQUIRED_DOCS = ["nib_business_license", "factory_registration", "product_list",
                 "process_flow", "material_list", "halal_certificate", "sjph_manual"]

# 물류(jasa logistik) 필수 서류 — jasa(보관·포장·유통)에 따라 갈린다.
# BPJPH 임계활동: 보관·포장=창고/시설(warehouse_layout), 유통=차량(vehicle_list).
# 공통은 사업자·SJPH·취급범위·세척SOP. 유통만 하면 창고 배치도는 요구하지 않는다.
REQUIRED_DOCS_LOGISTICS_COMMON = ["nib_business_license", "sjph_manual",
                                  "logistics_scope", "cleaning_sop"]
REQUIRED_DOCS_BY_JASA = {
    "penyimpanan": ["warehouse_layout"],           # 보관 — 창고 배치도(Halal Zone)
    "pengemasan": ["warehouse_layout"],            # 포장 — 포장 시설(창고 배치도로 갈음)
    "pendistribusian": ["vehicle_list"],           # 유통 — 차량·컨테이너 목록
}
# 전체(jasa 미선택 시 안전하게 넓게 요구)
REQUIRED_DOCS_LOGISTICS = (REQUIRED_DOCS_LOGISTICS_COMMON
                           + ["warehouse_layout", "vehicle_list"])


def logistics_required_docs(logistics_scope=None):
    """물류 필수 서류 — jasa 조합에 따라. jasa 미선택이면 넓게(창고+차량 모두)."""
    js = [x for x in (logistics_scope or []) if x in REQUIRED_DOCS_BY_JASA]
    if not js:
        return list(REQUIRED_DOCS_LOGISTICS)
    req = list(REQUIRED_DOCS_LOGISTICS_COMMON)
    for j in js:
        for d in REQUIRED_DOCS_BY_JASA[j]:
            if d not in req:
                req.append(d)
    return req

# 경로별 필수 목록. 미정(undetermined)은 정규 기준을 쓴다 — 넓게 요구하는 쪽이 안전하다.
REQUIRED_DOCS_BY_PATHWAY = {
    "reguler": REQUIRED_DOCS,
    "self_declare": ["nib_business_license", "product_list", "process_flow",
                     "material_list", "halal_certificate", "sjph_manual"],
}
# 서류 원본 대신 다른 근거로 충족할 수 있는 항목 — 무엇으로 갈음했는지 반드시 표기한다.
DOC_ALT_SATISFY = {
    "self_declare": {
        "halal_certificate": {"by": "supplier_cert_no",
                              "note": "인도네시아 자기선언 경로 — 공급사 인증번호로 갈음(BPJPH 대조)"},
    },
}
# 그 경로에서 요구되지 않는 서류 — 화면에 '해당 없음'으로 표시해 누락과 구분한다.
DOC_NOT_APPLICABLE = {
    "self_declare": {"factory_registration":
                     "소규모 자기선언 — NIB로 갈음(별도 공장등록증 요구 없음)"},
}


# 관할·규모에서 오는 면제 — 경로와는 다른 축이다.
# 실측(CV. CITRA PRATAMA): 육류·가금 원료 때문에 판정이 reguler로 나와도, 인도네시아
# 소규모 사업자에게 '공장등록증'이라는 서류 자체가 존재하지 않는다. NIB(사업자번호)가
# 사업자 등록과 시설 등록을 겸한다. 그래서 경로 축만으로는 이 면제를 표현할 수 없다.
DOC_JURISDICTION = {
    ("ID", True): {"factory_registration":
                   "인도네시아 소규모 사업자 — NIB가 시설 등록을 겸함(별도 공장등록증 제도 없음)"},
}
_COUNTRY_ID = {"id", "idn", "indonesia", "republic of indonesia", "인도네시아"}


def _country_key(country):
    """국가 표기 흔들림 흡수. 모르는 나라는 None — 면제를 적용하지 않는다(넓은 쪽)."""
    c = (country or "").strip().lower()
    return "ID" if c in _COUNTRY_ID else None


def required_docs(pathway=None):
    """경로별 필수 서류 목록. 모르는 경로는 정규 기준(넓은 쪽)."""
    return REQUIRED_DOCS_BY_PATHWAY.get((pathway or "").lower(), REQUIRED_DOCS)


SECTORS = ("food", "cosmetics", "household", "warehouse", "transport")
SECTOR_SCHEME = {"food": "product", "cosmetics": "product", "household": "product",
                 "warehouse": "logistics", "transport": "logistics"}
SECTOR_DEFAULT_SCOPE = {"warehouse": ["penyimpanan", "pengemasan"], "transport": ["pendistribusian"]}
# 분야별 추가 요구 서류(제품 분야: 성분분석표). 화장품은 공장등록증 자리에 제조업 등록필증이 온다(라벨 메모).
SECTOR_EXTRA_DOCS = {"food": ["coa_msds"], "cosmetics": ["coa_msds"], "household": ["coa_msds"]}
SECTOR_DOC_NOTE = {"cosmetics": {"factory_registration": "화장품 — 제조업 등록필증으로 갈음"}}


def sector_defaults(sector):
    """분야 → (scheme, logistics_scope 기본값). 모르는 분야는 제품."""
    s = (sector or "").lower()
    if s not in SECTORS:
        return "product", None
    return SECTOR_SCHEME[s], list(SECTOR_DEFAULT_SCOPE.get(s) or []) or None


def doc_requirements(pathway=None, country=None, is_msme=None, scheme="product",
                    logistics_scope=None, sector=None):
    """이 신청 건에 실제로 요구되는 서류 — 인증 종류(scheme)·경로(pathway)·관할·규모를 함께 본다.

    scheme 이 가장 바깥 축이다. 물류(logistics)는 제품과 서류 세트 자체가 다르므로
    pathway 분기 이전에 갈라진다. 제품(product)은 기존 pathway 로직을 그대로 탄다.

    돌려주는 것
      required        요구되는 doc_type 목록
      not_applicable  {doc_type: 사유} — 요구되지 않는 서류. 목록에서 지우지 않고
                      사유와 함께 남긴다. 조용히 사라지면 면제인지 누락인지 알 수 없다.
      alt             {doc_type: {by, note}} — 원본 서류 대신 다른 근거로 충족 가능한 항목
    """
    if (scheme or "product").lower() == "logistics":
        # jasa(보관·포장·유통)에 따라 서류가 갈린다 — 유통만이면 창고 배치도는 안 나온다.
        req = logistics_required_docs(logistics_scope)
        # 선택 안 된 jasa의 서류는 '해당 없음'으로 남겨 누락과 구분한다.
        na = {}
        _all = set(REQUIRED_DOCS_LOGISTICS)
        for d in _all - set(req):
            na[d] = "선택한 물류 서비스(jasa)에 해당하지 않는 서류입니다"
        return {"required": req, "not_applicable": na, "alt": {}, "notes": {}}
    pw = (pathway or "").lower()
    req = list(required_docs(pw))
    na = dict(DOC_NOT_APPLICABLE.get(pw, {}))
    na.update(DOC_JURISDICTION.get((_country_key(country), bool(is_msme)), {}))
    req = [d for d in req if d not in na]
    alt = {k: v for k, v in DOC_ALT_SATISFY.get(pw, {}).items() if k in req}
    if sector in SECTOR_EXTRA_DOCS:
        for _d in SECTOR_EXTRA_DOCS[sector]:
            if _d not in req:
                req.append(_d)
    return {"required": req, "not_applicable": na, "alt": alt,
            "notes": dict(SECTOR_DOC_NOTE.get(sector) or {})}

# 필수 서류별 요구 내용(보완 안내용) — 무엇이 담겨야 하는지 상세 설명
DOC_REQUIREMENT = {
    "nib_business_license": "사업자등록증(NIB) 스캔본 — 회사명·NIB 번호·사업장 주소가 판독 가능해야 함",
    "factory_registration": "공장등록증 — 공장 등록번호와 공장 소재지 주소 포함",
    "product_list": "인증 대상 전(全) 제품 목록 — 제품명·분류·등록유형(신규/기존 등)",
    "process_flow": "제조 공정 흐름도 — 원료입고→배합→가열→충전→포장 등 단계 순서",
    "material_list": "전(全) 원재료 목록 — 원재료명·공급사·할랄 상태(인증/선언)",
    "halal_certificate": "임계 원재료 공급사가 받은 할랄 인증서 사본 — 이 플랫폼이 발급하는 인증서가 아니라 신청자가 제출하는 입력 서류다(해당 원재료가 있는 경우)",
    "origin_certificate": "원산지증명서 — 원재료의 원산지 국가와 공급사·제조사가 판독 가능해야 함. 유래(동물/식물) 표기가 있으면 유래 선언서도 갈음한다",
    "sjph_manual": "SJPH 매뉴얼 — 5요소(경영약속·원재료·공정·제품·모니터링) 포함",
    "logistics_scope": "취급 화물·서비스 범위 — 어떤 제품군(식품·의약·화장품)을 penyimpanan·pengemasan·pendistribusian 중 어느 서비스로 다루는지",
    "warehouse_layout": "창고 배치도 — 할랄/비할랄 구획(Halal Zone)과 동선 분리가 드러나야 함",
    "vehicle_list": "차량·컨테이너 목록 — 등록번호·유형. 인증 대상은 서비스이나 관리대상 증빙",
    "cleaning_sop": "세척 SOP — 차량·컨테이너·창고 세척 절차. 이전 화물이 비할랄일 때 Sertu 세정 포함",
}

# 서류 요건 설명 — 인니 신청기업·심사자가 읽는 문구다. 한국어만 두면 인니어 화면에
# 그대로 뜬다(실측: 사전심사 화면 잔여 한글의 최대 덩어리).
# 서류 요건 설명 — 사전에서 꺼낸다(표를 코드에 두 벌 두지 않는다).
from .domain_dict import code_labels as _code_labels   # noqa: E402

DOC_REQUIREMENT_L10N = {lg: _code_labels("DOC_REQUIREMENT", "doc_requirement", lg)
                        for lg in ("en", "id")}

# 면제·갈음 사유 — 왜 이 서류를 안 내도 되는지. 심사자가 읽고 판단하는 문장이다.
# 면제·갈음 사유 — 사전에서(한국어 문구가 키다).
def _exempt_l10n():
    from .domain_dict import text as _t
    kos = [v for tbl in list(DOC_NOT_APPLICABLE.values()) + [
        {k: x["note"] for k, x in v.items()} for v in DOC_ALT_SATISFY.values()]
        for v in [tbl] for v in tbl.values()]
    kos += [v for tbl in DOC_JURISDICTION.values() for v in tbl.values()]
    return {ko: {lg: _t(ko, lg) for lg in ("en", "id")} for ko in set(kos)}


_EXEMPT_L10N = _exempt_l10n()

def requirement_text(doc_type, lang="ko"):
    """서류 요건 설명 — 해당 언어가 없으면 한국어로 물러선다(빈칸보다 낫다)."""
    tbl = DOC_REQUIREMENT_L10N.get((lang or "ko").lower())
    return (tbl or {}).get(doc_type) or DOC_REQUIREMENT.get(doc_type, "")


def exempt_text(ko, lang="ko"):
    """면제·갈음 사유 문구를 해당 언어로."""
    if (lang or "ko").lower() == "ko":
        return ko
    return (_EXEMPT_L10N.get(ko) or {}).get((lang or "").lower(), ko)

_IMG = ("png", "jpg", "jpeg", "bmp", "tiff", "tif", "webp")


def _nfc(name):
    """파일명을 NFC(완성형)로 맞춘다.

    macOS(Finder '압축하기'·Safari 업로드)는 한글을 NFD(자모 분해)로 보낸다 — 화면에는
    똑같이 보이지만 '사업자등록증' 같은 규칙·사전 키워드와 문자열이 달라 매칭이 안 된다.
    실측: 아카이브.zip 의 '사업자등록증'·'공장등록증'이 파일명 판정을 놓쳐 기타로 떨어졌다."""
    return unicodedata.normalize("NFC", name) if name else name


def _zip_name(zi):
    """한국어 Windows ZIP은 파일명이 CP949인데 UTF-8 플래그(0x800)가 없으면
    zipfile이 CP437로 디코드해 깨진다(┴╓..). CP437로 되돌려 CP949로 재디코드.
    macOS ZIP은 플래그 없이 UTF-8(NFD)이라 CP949가 실패하고 UTF-8로 풀린다 — NFC로 맞춘다."""
    name = zi.filename
    if not (zi.flag_bits & 0x800):
        for enc in ("cp949", "euc-kr", "utf-8"):
            try:
                return _nfc(name.encode("cp437").decode(enc))
            except Exception:
                continue
    return _nfc(name)


class ArchiveError(Exception):
    """압축을 열 수 없음 — 호출부가 사용자에게 사유를 그대로 보여준다."""


def _check_zip_limits(infos, max_entries=None):
    """중앙 디렉터리만 읽어 검사한다. Office도 XML을 해제하기 전에 같은 상한을 쓴다."""
    if max_entries is not None and len(infos) > max_entries:
        raise ArchiveError("압축 엔트리 수가 너무 많습니다")
    total = 0
    for zi in infos:
        if zi.is_dir():
            continue
        if zi.file_size > ZIP_MAX_SINGLE_FILE:
            raise ArchiveError(f"파일이 너무 큽니다 ({zi.filename}: {zi.file_size} bytes > {ZIP_MAX_SINGLE_FILE})")
        total += zi.file_size
        if total > ZIP_MAX_UNCOMPRESSED:
            raise ArchiveError(f"압축 해제 후 크기가 너무 큽니다 (총 {total} bytes > {ZIP_MAX_UNCOMPRESSED})")
        ratio = zi.file_size / max(1, zi.compress_size)
        if ratio > ZIP_MAX_COMPRESSION_RATIO:
            raise ArchiveError(f"압축폭탄 의심 ({zi.filename}: 압축비 {ratio:.1f} > {ZIP_MAX_COMPRESSION_RATIO})")


# 형식은 확장자가 아니라 매직바이트로 가린다. 이름만 .zip 으로 바꿔 보내는 경우가 있다.
_MAGIC = [(b"PK\x03\x04", "zip"), (b"PK\x05\x06", "zip"), (b"PK\x07\x08", "zip"),
          (b"Rar!\x1a\x07", "rar"), (b"7z\xbc\xaf\x27\x1c", "7z")]


def archive_kind(data):
    for sig, kind in _MAGIC:
        if data[:len(sig)] == sig:
            return kind
    return None


def _extract_with_tool(data, suffix):
    """bsdtar(macOS 기본 내장 libarchive)로 RAR·7z 를 푼다. 없으면 unar 로 폴백.

    파이썬 rarfile 패키지도 결국 외부 unrar 바이너리를 요구한다. bsdtar 는 이미 깔려
    있으므로 새 의존성 없이 같은 일을 한다."""
    import shutil
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "archive" + suffix)
        dst = os.path.join(td, "out")
        os.makedirs(dst)
        with open(src, "wb") as f:
            f.write(data)
        if shutil.which("bsdtar"):
            cmd = ["bsdtar", "-xf", src, "-C", dst]
        elif shutil.which("unar"):
            cmd = ["unar", "-quiet", "-force-overwrite", "-output-directory", dst, src]
        else:
            raise ArchiveError("이 서버에 %s 해제 도구가 없습니다 (bsdtar·unar 미설치)"
                               % suffix.lstrip("."))
        r = subprocess.run(cmd, capture_output=True, timeout=180)
        if r.returncode != 0:
            raise ArchiveError("압축을 풀지 못했습니다: %s"
                               % (r.stderr.decode("utf-8", "ignore")[:200] or "unknown"))
        out = []
        # 심볼릭 링크 방어: followlinks=False + 경로 검증
        dst_real = os.path.realpath(dst)
        for root, _dirs, names in os.walk(dst, followlinks=False):
            for n in sorted(names):
                if n.startswith("."):
                    continue
                p = os.path.join(root, n)
                # 심볼릭 링크 건너뛰기
                if os.path.islink(p):
                    continue
                # 해제 디렉터리 밖을 가리키는 경로 검증
                p_real = os.path.realpath(p)
                if not p_real.startswith(dst_real):
                    continue
                rel = _nfc(os.path.relpath(p, dst))   # macOS 압축은 NFD
                if "__MACOSX" in rel:
                    continue
                try:
                    with open(p, "rb") as f:
                        out.append((rel, f.read()))
                except Exception:  # noqa: BLE001
                    pass
        return out


class _ExtractedFiles(list):
    """[(파일명, 바이트)] 목록 — 개수 상한 때문에 건너뛴 엔트리 수를 skipped 로 함께 싣는다."""
    skipped = 0


def extract_zip(data):
    """압축 묶음 → [(파일명, 바이트)]. ZIP·RAR·7z 를 모두 받는다.

    함수명은 zip 시절 그대로 두되(호출부가 여럿) 실제로는 형식을 가려서 연다.
    인도네시아 업체는 RAR로 보내는 일이 흔하다 — 예전에는 BadZipFile 로 500이 났고
    화면에는 '인테이크 완료'라고 떠서 서류 0건인 채 넘어갔다(실측)."""
    kind = archive_kind(data)
    if kind in ("rar", "7z"):
        return _extract_with_tool(data, "." + kind)
    if kind is None:
        raise ArchiveError("압축 파일이 아닙니다 (ZIP·RAR·7z 만 지원합니다)")
    out = _ExtractedFiles()
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ArchiveError("ZIP 파일이 손상되었습니다: %s" % e) from e
    with z:
        # 압축폭탄 방어: 전체 해제 크기 및 엔트리별 검증
        _check_zip_limits(z.infolist())

        # 실제 파일 추출 — 최대 ZIP_MAX_ENTRIES 개, 초과분은 skipped 로 센다
        infos = z.infolist()
        for i, zi in enumerate(infos):
            if len(out) >= ZIP_MAX_ENTRIES:
                out.skipped = sum(1 for x in infos[i:] if not x.is_dir())
                if out.skipped:
                    log.warning("ZIP 엔트리 초과: %d개 건너뜀 (최대 %d개)", out.skipped, ZIP_MAX_ENTRIES)
                break
            if zi.is_dir():
                continue
            name = _zip_name(zi)
            base = os.path.basename(name)
            if not base or base.startswith(".") or "__MACOSX" in name:
                continue
            try:
                out.append((name, z.read(zi)))
            except Exception as e:  # noqa: BLE001
                log.warning("ZIP 엔트리 읽기 실패 %s: %s", zi.filename, e)
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
# 기본 OCR 은 한국어 모델이다 — 한글이 아닌 스크립트(키릴/러시아어)는 못 읽는다.
# 함정: korean 모델은 키릴을 '빈 값'이 아니라 라틴 유사문자로 자신있게 오독한다
# (실측: 'МОЛОКО'→'MonoKo', 평균 confidence 0.82). 그래서 길이·신뢰도 임계만으론
# 폴백 트리거가 안 걸린다. 대신 korean 결과에 한글이 없으면(=한국어 문서가 아니면)
# 아래 언어로도 읽어 평균 신뢰도가 가장 높은 결과를 채택한다. ru→eslav(동슬라브) 모델.
# 한글이 있으면 한국어 문서이므로 재-OCR 하지 않아 기존 속도를 유지한다.
_OCR_FALLBACK_LANGS = [s.strip() for s in
                       os.environ.get("GLHAC_OCR_FALLBACK_LANGS", "ru").split(",") if s.strip()]


def _has_hangul(s):
    return any("가" <= c <= "힣" for c in s)


def _mean_conf(lines):
    cs = [l.get("confidence", 0.0) for l in lines]
    return sum(cs) / len(cs) if cs else 0.0


def _ocr_kept(lines):
    """confidence 낮은(오인식) 라인을 걸러 텍스트만. 전부 저confidence면 빈 텍스트 방지 폴백."""
    kept = [l["text"] for l in lines if l.get("confidence", 1) >= _OCR_MIN_CONF]
    if not kept and lines:
        kept = [l["text"] for l in lines]
    return kept


def _ocr_score(lines, kept):
    """OCR 결과 품질 점수 — 내용이 없으면 -1(항상 탈락), 있으면 평균 신뢰도."""
    return _mean_conf(lines) if "".join(kept).strip() else -1.0


class _ParsedText(str):
    """기존 문자열 API를 유지하면서 추출 실패/상한 정보를 분류 단계까지 전달한다."""
    def __new__(cls, text="", fields=None):
        obj = super().__new__(cls, text)
        obj.parse_fields = dict(fields or {})
        return obj


def _record_truncation(fields, kind, limit, total):
    marker = {"kind": kind, "limit": limit, "total": total}
    if "_truncated" not in fields:
        fields["_truncated"] = marker
    elif fields["_truncated"] != marker:
        markers = list(fields.get("_truncations", [fields["_truncated"]]))
        if marker not in markers:
            markers.append(marker)
        # 본문/목록 자체의 누락이 우선이다. 일반 필드 입력창 표시는 함께 보존한다.
        if fields["_truncated"]["kind"] == "llm_input" and kind != "llm_input":
            fields["_truncated"] = marker
            markers.remove(marker)
            markers.insert(0, marker)
        fields["_truncations"] = markers


def _clear_truncation(fields, kind):
    markers = [m for m in fields.get("_truncations", [fields.get("_truncated")])
               if isinstance(m, dict) and m.get("kind") != kind]
    fields.pop("_truncated", None)
    fields.pop("_truncations", None)
    for marker in markers:
        _record_truncation(fields, **marker)
    if not markers:
        fields.pop("_tail_screening", None)


def _scan_tail(fields, text):
    """잘린 원문은 OCR/LLM 없이 사전으로만 검사한다. 추출 목록에 섞지는 않는다."""
    from . import screening
    _merge_tail_scan(fields, screening.screen_text(text))


def _merge_tail_scan(fields, scan):
    scan = dict(scan)
    previous = fields.get("_tail_screening")
    if previous:
        hits = {h["uid"]: h for h in previous["haram_hits"] + scan["haram_hits"]}
        scan["haram_hits"] = list(hits.values())
        rank = {"low": 0, "medium": 1, "high": 2}
        scan["verdict"] = max(previous["verdict"], scan["verdict"], key=rank.get)
        scan["keyword_risk"] = max(previous["keyword_risk"], scan["keyword_risk"], key=rank.get)
    fields["_tail_screening"] = {k: scan[k] for k in ("verdict", "keyword_risk", "haram_hits")}


def _parsed_result(result, text):
    """분류·유형별 추출의 모든 반환 경로에 동일한 파싱 상태를 붙인다."""
    fields = dict(result.get("fields") or {})
    doc_type = result.get("doc_type")
    if marker := (certificate_invalid_marker(result.get("filename", ""), text)
                  or ("invalid" if result.get("reason") == "CERT_NOT_VALID" else None)):
        fields["_cert_not_valid"] = marker
        fields["_counts_as_evidence"] = False
        result["doc_type"] = doc_type = "other"
        for key in ("cert_no", "issuer", "expiry_date", "scope"):
            fields.pop(key, None)
    for key in ("nib", "factory_reg_no"):
        if fields.get(key) and not autofill_value_in_text(key, fields[key], text):
            # 원문 대조/충돌 안내를 위한 후보는 남기되 공통 autofill 가드가 저장을 막는다.
            fields.setdefault("_rejected_identifiers", {})[key] = fields[key]
    if (doc_type in ("halal_certificate", "quality_cert") and (text or "").strip()
            and not getattr(text, "parse_fields", {}).get("_ocr_error")):
        evidence = _deterministic_fields(doc_type, text or "")
        fields.update(evidence)
        required = ("cert_no", "expiry_date", "issuer") if doc_type == "halal_certificate" else ("cert_no", "expiry_date")
        missing = [key for key in required if not isinstance(fields.get(key), str) or not fields[key].strip()]
        if missing:
            fields["_missing_fields"] = missing
        if sum(bool(evidence.get(key)) for key in ("cert_no", "expiry_date", "issuer")) < 2:
            result["confidence"] = min(result.get("confidence", 0.0), 0.4)
    metadata = getattr(text, "parse_fields", {})
    for key, value in metadata.items():
        if key == "_truncated":
            _record_truncation(fields, **value)
        elif key == "_truncations":
            for marker in value:
                _record_truncation(fields, **marker)
        elif key == "_tail_screening":
            _merge_tail_scan(fields, value)
        else:
            fields[key] = value
    if not (text or "").strip():
        fields["_empty"] = True
        result["confidence"] = 0.0
    if fields.get("_ocr_error"):
        result["confidence"] = 0.0
    if fields.get("_llm_error"):
        result["llm_error"] = fields["_llm_error"]
        result["confidence"] = 0.0
    result["fields"] = fields
    if "text" in result:
        result["text"] = text
    return result


def _ocr_bytes(data, ext, sink=None):
    """OS 독립 임시파일 OCR. confidence 낮은(오인식) 라인 제거로 품질↑.
    (LLM 한글교정은 qwen2.5 테스트 결과 성분명 환각으로 더 악화 → 미채택, 할랄 안전)

    sink 를 주면 인식 라인(좌표 포함)을 그대로 담아준다 — 표 사진의 행 복원은 좌표가
    있어야 하고, 없으면 나중에 같은 사진을 또 OCR하게 된다."""
    import tempfile
    path = None
    failures = []
    def recognize(lang=None):
        try:
            result = ai_local.ocr_image(path, **({"lang": lang} if lang else {}))
            if not isinstance(result, dict) or result.get("ok") is False or result.get("error"):
                reason = str((result or {}).get("error", "ERROR")) if isinstance(result, dict) else "ERROR"
                failures.append("TIMEOUT" if "TIMEOUT" in reason.upper() else "ERROR")
                return []
            return result.get("lines") or []
        except Exception as exc:  # OCR 프로세스/통신 예외도 실패로 남긴다.
            failures.append("TIMEOUT" if isinstance(exc, TimeoutError) else "ERROR")
            return []
    try:
        fd, path = tempfile.mkstemp(suffix="." + ext)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        lines = recognize()
        kept = _ocr_kept(lines)
        joined = "".join(kept).strip()
        # 한글이 없으면(=한국어 문서가 아니면) 다른 스크립트로도 읽어 신뢰도 최고를 채택.
        # 빈 결과(스캔 실패)도 여기 들어와 폴백 언어로 재시도된다.
        if (not joined) or (not _has_hangul(joined)):
            best = _ocr_score(lines, kept)
            for lg in _OCR_FALLBACK_LANGS:
                alt = recognize(lg)
                alt_kept = _ocr_kept(alt)
                sc = _ocr_score(alt, alt_kept)
                if sc > best:
                    best, lines, kept = sc, alt, alt_kept
        if sink is not None:
            sink.extend(lines[:4000])     # 병적으로 큰 스캔본 방어(채택된 언어의 라인)
        text = " ".join(kept)
        fields = {}
        if failures and not text.strip():
            fields["_ocr_error"] = "TIMEOUT" if "TIMEOUT" in failures else "ERROR"
        return _ParsedText(text, fields)
    except Exception:  # noqa: BLE001
        return _ParsedText("", {"_ocr_error": "ERROR"})
    finally:
        try:
            if path:
                os.remove(path)
        except OSError:
            pass


# 전성분표(원료명|INS No.|용도 반복 구조) 전용 구조 인식 파서.
# 근본 해결: 2차원 표를 " ".join으로 1차원화하면 열 의미가 소실돼(원료명·용도·제품명 뒤섞임,
# 인접 셀 병합) LLM이 오분류한다. 표 구조 그대로 제품명(시트 상단)·원재료(원료명 열)를 결정론적 추출.
_ING_HDR_RE = re.compile(r"원료\s*명|ingredient\s*name")
_ING_SKIP_RE = re.compile(r"^(원료\s*명|INS|용도|1차|2차|3차|ingredient|function|no\.?)$", re.I)

# 인니어 원재료 목록(Daftar Bahan): 헤더가 'Nama'/'Nama Bahan' 이라 위 한국어 규칙에
# 걸리지 않았다. 그 결과 136행짜리 표를 청크 LLM으로 다시 읽었고 — 87초를 쓰고 134개만
# 건졌다(실측). 표에 그대로 있는 값을 결정론적으로 읽는다.
# 헤더 셀은 '한 줄 = 한 언어'로 겹쳐 쓰인다("Nama Bahan (Material Name)\n재료명").
# 예전 규칙은 셀 전체가 정확히 'Nama'/'Nama Bahan' 일 때만 걸려서, GL-HAC 정식 서식
# Form.5 처럼 괄호 병기·한국어 병기가 붙은 표를 통째로 놓쳤다 — 그 표를 LLM 이 대신
# 읽었고 45건짜리 목록을 모델마다 39/45/54건으로 다르게 냈다(실측).
_ID_NAME_HDR_RE = re.compile(
    r"^\s*(nama(\s*bahan)?|material\s*name|ingredient\s*name|원료\s*명|재료\s*명)\b", re.I)
# 일련번호 열 — 이 열에 번호가 있는 행만 데이터로 본다(하위헤더·주석·합계행 배제).
_ID_NO_HDR_RE = re.compile(r"^\s*(no|번호)\.?\s*$", re.I)
# 이름 열 아래 언어 하위헤더(KOR/ENG). 원재료명으로 새어들면 목록 맨 앞에 박힌다.
_ID_SUB_HDR_RE = re.compile(r"^(kor|eng|ko|en|한글|영문|국문)$", re.I)


def _hdr_first_line(cell):
    """헤더 셀의 첫 줄 — 병기된 다른 언어 줄에 규칙이 헛걸리지 않게."""
    return str(cell).splitlines()[0].strip() if cell is not None else ""
# 원재료표임을 뒷받침하는 이웃 헤더 — 'Nama' 한 단어만으로 판단하면 아무 표나 걸린다.
_ID_ATTR_HDR_RE = re.compile(
    r"jenis\s*bahan|produsen|negara|supplier|pemasok|lembaga|sertifikat|no\.?\s*sert", re.I)
# 제품×원재료 매트릭스 표시값 — 이런 열이 이어지면 목록이 아니라 매트릭스다.
_MATRIX_MARK_RE = re.compile(r"^[vV✓xX✔○●\-–—]$")


def _looks_like_matrix(rows, hdr, name_col):
    """제품×원재료 매트릭스인가 — 목록과 같은 헤더를 쓰지만 성격이 다르다.

    'Bahan vs Produk matriks.xlsx' 는 'Nama Bahan' 헤더를 쓰면서 오른쪽이 전부 제품 열이고
    칸은 V 표시다. 이걸 원재료표로 읽으면 doc_type 이 product_list 에서 material_list 로
    뒤집혀 제품 목록이 사라진다. 표시값 열이 3개 이상이면 매트릭스로 본다."""
    body = rows[hdr + 1:hdr + 12]
    width = max((len(r) for r in body), default=0)
    marks = 0
    for j in range(name_col + 1, min(name_col + 12, width)):
        vals = [str(r[j]).strip() for r in body
                if j < len(r) and r[j] is not None and str(r[j]).strip()]
        if vals and all(_MATRIX_MARK_RE.match(v) for v in vals):
            marks += 1
    return marks >= 3


MATRIX_MARK = "[구조:제품×원재료매트릭스]"


def _id_material_rows(rows, seen=None):
    """인니어 원재료 목록 시트면 원재료명 리스트, 아니면 None.

    매트릭스로 판정하면 seen['matrix']=True 로 알린다. 그 사실을 남기지 않으면
    구조 파서는 '목록 아님'이라고 옳게 판단해 놓고, 같은 본문을 LLM 이 읽어 목록을
    지어낸다(실측: 같은 파일에서 로컬 LLM 이 제품 35건·원재료 21건을 뽑았고
    공급자·실행마다 값이 달랐다)."""
    for i, row in enumerate(rows[:8]):
        cols = [j for j, c in enumerate(row)
                if c and _ID_NAME_HDR_RE.match(_hdr_first_line(c))]
        if not cols:
            continue
        name_col = cols[0]
        # 매트릭스 검사를 먼저 한다. 이웃 헤더(Jenis/Produsen…) 개수로 먼저 걸러내면
        # 오른쪽이 전부 '제품 열'인 매트릭스는 attrs=0 이라 검사에 닿지도 못한다
        # (실측: 'Bahan vs Produk matriks.xlsx' 가 그랬고, 그 사이 LLM 이 목록을 지어냈다).
        if _looks_like_matrix(rows, i, name_col):
            if seen is not None:
                seen["matrix"] = True
            return None
        attrs = sum(1 for c in row if c and _ID_ATTR_HDR_RE.search(str(c)))
        if attrs < 2:            # 이웃 헤더가 없으면 원재료표라고 볼 근거가 없다
            continue
        # 일련번호 열이 있으면 그 열이 표의 경계다 — 번호 있는 행만 데이터로 본다.
        # 이름 열은 하위 언어열(KOR/ENG)로 쪼개져 있을 수 있어, 다음 헤더 열 직전까지를
        # 한 묶음으로 보고 그 안에서 처음 채워진 값을 쓴다(KOR 비면 ENG).
        no_col = next((j for j, c in enumerate(row)
                       if c and _ID_NO_HDR_RE.match(_hdr_first_line(c))), None)
        nxt = next((j for j, c in enumerate(row)
                    if j > name_col and c and str(c).strip()), None)
        span = range(name_col, nxt if nxt is not None else name_col + 1)
        out = []
        for row2 in rows[i + 1:]:
            if no_col is not None:
                seq = row2[no_col] if no_col < len(row2) else None
                seq = "" if seq is None else str(seq).strip().rstrip(".")
                if not seq.isdigit():
                    continue
                v = next((str(row2[j]).strip() for j in span
                          if j < len(row2) and row2[j] is not None
                          and str(row2[j]).strip()), "")
            else:
                if name_col >= len(row2) or not row2[name_col]:
                    continue
                v = str(row2[name_col]).strip()
            if (v and not _ING_SKIP_RE.match(v) and not _ID_NAME_HDR_RE.match(v)
                    and not _ID_SUB_HDR_RE.match(v) and v not in out):
                out.append(v)
        return out or None
    return None


def _xlsx_ingredient_table(wb):
    """전성분표 구조가 감지되면 (products, materials) 반환, 아니면 None.
    - 제품명 = 각 시트 1행의 첫 비어있지 않은 셀(완제품 1개/시트)
    - 원재료 = '원료명' 헤더가 있는 열들의 데이터 값만(INS No.·용도 열 제외)"""
    products, materials = [], []
    detected = False
    matrix_seen = False
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        if not rows:
            continue
        # 헤더행: '원료명' 헤더를 포함하는 첫 행 → 원료명 열 인덱스 수집
        name_cols, hdr = [], None
        for i, row in enumerate(rows[:8]):
            cols = [j for j, c in enumerate(row) if c and _ING_HDR_RE.search(str(c))]
            if cols:
                name_cols, hdr = cols, i
                break
        if hdr is None:
            # 인니어 원재료 목록(Nama + Jenis Bahan/Produsen/…). 제품명은 없다 —
            # 시트 1행은 표 제목('Daftar Bahan Halal …')이라 제품으로 쓰면 안 된다.
            _seen = {}
            id_mats = _id_material_rows(rows, _seen)
            if _seen.get("matrix"):
                matrix_seen = True
            if id_mats:
                detected = True
                for v in id_mats:
                    if v not in materials:
                        materials.append(v)
            continue   # 이 시트는 (한국어) 전성분표 구조 아님
        detected = True
        prod = next((str(c).strip() for c in rows[0] if c and str(c).strip()), None)
        if prod and prod not in products:
            products.append(prod)
        for row in rows[hdr + 1:]:
            for j in name_cols:
                if j < len(row) and row[j]:
                    v = str(row[j]).strip()
                    if v and not _ING_SKIP_RE.match(v) and v not in materials:
                        materials.append(v)
    if matrix_seen and not detected:
        # 매트릭스만 있고 목록은 없다 — '아무것도 못 찾음'과 구분해 돌려준다.
        return "matrix"
    if not detected:
        return None
    return products, materials


def _bounded_office_text(parts, limit):
    fields, out, used, total = {}, [], 0, 0
    for part in parts:
        part = ("\n" if total else "") + part
        total += len(part)
        remaining = max(0, limit - used)
        if remaining:
            out.append(part[:remaining])
            used += len(out[-1])
        if len(part) > remaining:
            _scan_tail(fields, part)
    if total > limit:
        _record_truncation(fields, "chars", limit, total)
    return _ParsedText("".join(out), fields)


def _docx_parts(document):
    for paragraph in document.paragraphs:
        if paragraph.text.strip():
            yield paragraph.text
    for table in document.tables:
        for row in table.rows:
            # 빈 셀도 보존해야 Sample|Control 헤더와 결과 열이 어긋나지 않는다.
            yield " | ".join(" ".join(p.text for p in cell.paragraphs).strip()
                             for cell in row.cells)


def _xlsx_parts(wb, matrix_note):
    if matrix_note:
        yield matrix_note
    for ws in wb.worksheets:
        title = (ws.title or "").strip()
        if title and title.lower() not in ("sheet", "sheet1", "sheet2", "sheet3", "시트1"):
            yield "[시트/제품명: %s]" % title
        for row in ws.iter_rows(values_only=True):
            if any(c is not None for c in row):
                yield " | ".join(str(c) if c is not None else "" for c in row)


def _office_corrupt(exc):
    # 런타임/의존성 오류는 OCR 오류와 별개로 ERROR를 유지한다. 패키지/XML 손상만 CORRUPT.
    return (isinstance(exc, (zipfile.BadZipFile, ParseError, zlib.error,
                             KeyError, ValueError, EOFError, OSError))
            or (type(exc).__module__ == "lxml.etree" and type(exc).__name__ == "XMLSyntaxError"))


def parse_file(name, data, dpi=None, ocr_sink=None):
    """확장자별 텍스트 추출 — OS 독립. 이미지/스캔PDF=OCR, PDF=fitz, docx/xlsx/txt.
    dpi 지정 시 스캔 렌더 해상도 오버라이드(고해상도 재처리용, 기본 _OCR_DPI).

    ocr_sink 를 주면 OCR 라인(좌표 포함)을 담아준다 — 재-OCR을 막는 캐시용. 단일 이미지에만
    채운다. 스캔 PDF는 페이지마다 좌표계가 새로 시작해 여러 페이지를 한 목록에 담으면 서로
    다른 페이지의 행이 같은 y 값으로 겹친다 — 표 행 복원이 조용히 틀어지느니 캐시하지 않는다."""
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    _dpi = int(dpi) if dpi else _OCR_DPI
    _dpi = max(72, min(600, _dpi))   # 안전 범위
    try:
        if ext in ("docx", "xlsx"):
            with zipfile.ZipFile(io.BytesIO(data)) as package:
                try:
                    _check_zip_limits(package.infolist(), ZIP_MAX_ENTRIES)
                except ArchiveError:
                    return _ParsedText("", {"_ocr_error": "BOMB"})
        if ext in _IMG:
            return _ocr_bytes(data, ext, ocr_sink)
        if ext == "pdf":
            import fitz
            fields, parts, used, total_chars = {}, [], 0, 0
            with fitz.open(stream=data, filetype="pdf") as doc:
                if doc.needs_pass:
                    return _ParsedText("", {"_ocr_error": "ENCRYPTED"})
                if doc.is_repaired:
                    fields["_ocr_error"] = "CORRUPT"
                if len(doc) > PDF_MAX_PAGES:
                    _record_truncation(fields, "pages", PDF_MAX_PAGES, len(doc))
                for index, pg in enumerate(doc):
                    t = pg.get_text()
                    remaining = max(0, PDF_MAX_TEXT_CHARS - used)
                    within_limit = index < PDF_MAX_PAGES and remaining > 0
                    if within_limit and len(t.strip()) < 20:
                        ocr = _ocr_bytes(pg.get_pixmap(dpi=_dpi).tobytes("png"), "png")
                        fields.update(getattr(ocr, "parse_fields", {}))
                        t = str(ocr) or t
                    t += "\n"
                    total_chars += len(t)
                    if within_limit:
                        parts.append(t[:remaining])
                        used += len(parts[-1])
                    if not within_limit or len(t) > remaining:
                        # 페이지/문자 상한 뒤에서는 native text만 읽는다. 렌더·OCR·LLM 없음.
                        _scan_tail(fields, t)
                if total_chars > PDF_MAX_TEXT_CHARS:
                    _record_truncation(fields, "chars", PDF_MAX_TEXT_CHARS, total_chars)
            return _ParsedText("".join(parts), fields)
        if ext in ("txt", "csv"):
            return _decode_text(data)
        if ext == "docx":
            import docx
            d = docx.Document(io.BytesIO(data))
            return _bounded_office_text(_docx_parts(d), OFFICE_MAX_TEXT_CHARS)
        if ext == "xlsx":
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            try:
                # 신뢰할 수 없는 dimension 속성으로 거대한 빈 셀 범위를 생성하지 않는다.
                for ws in wb.worksheets:
                    ws.reset_dimensions()
                ing = _xlsx_ingredient_table(wb)
                matrix_note = MATRIX_MARK if ing == "matrix" else ""
                if ing and not matrix_note:
                    prods, mats = ing
                    parts = (["[전성분표]"] + ["[제품/시트: %s]" % p for p in prods]
                             + ["[원재료목록]"] + mats)
                    return _bounded_office_text(parts, min(60000, OFFICE_MAX_TEXT_CHARS))
                return _bounded_office_text(_xlsx_parts(wb, matrix_note),
                                            min(20000, OFFICE_MAX_TEXT_CHARS))
            finally:
                wb.close()
    except Exception as e:  # noqa: BLE001
        log.warning("파일 파싱 실패 %s: %s", name, e)
        corrupt = ext == "pdf" or (ext in ("docx", "xlsx") and _office_corrupt(e))
        return _ParsedText("", {"_ocr_error": "CORRUPT" if corrupt else "ERROR"})
    return ""


_CLASSIFY_SYS = (
    "당신은 할랄 인증 서류 분류기입니다. 파일명과 본문 발췌를 보고 doc_type을 분류하고 핵심 필드를 추출하세요. "
    "doc_type은 반드시 다음 중 하나: " + ", ".join(DOC_TYPES) + ". "
    "address는 회사(사업자) 주소, city/province/country/zip은 회사 주소의 도시/도(주,state/province)/국가/우편번호. "
    "factory_address는 공장 주소, factory_city/factory_province/factory_country/factory_zip은 공장 주소의 도시/도(주)/국가/우편번호. "
    "factory_reg_no는 공장등록번호(Plant Registration No. — 공장 고유번호, 예: G20026130). "
    "responsible_person은 대표자/책임자 이름. "
    "phone은 회사 전화번호, factory_phone은 공장 전화번호(Tel). "
    "business_type은 사업의 종류/업태(예: 제조업, 도소매업). "
    "employee_count는 총 직원 수(숫자만, 남녀 합계). "
    "establishment_date는 개업일/설립일(YYYY-MM-DD). "
    "corporate_reg_no는 법인등록번호(Corporate Registration No., 형식 123456-1234567 — 공장등록번호 factory_reg_no와 다른 번호이니 혼동하지 말 것). "
    "주소(address/factory_address)는 도로명부터 도·시·국가까지 전체를 그대로 넣으세요(중간에서 자르지 말 것). "
    "process_steps는 공정흐름도의 공정 단계 순서 목록(원료입고→배합→가열→포장 등). "
    "product_names는 완제품 이름 목록 — 제품목록 문서뿐 아니라 전성분표/원재료 문서라도 "
    "시트명(예: '[시트/제품명: ...]')·표 제목·문서 상단에 완제품명이 있으면 product_names에 넣으세요. "
    "material_names는 원재료(성분) 이름 목록(완제품이 아님). "
    "has_commitment/has_materials/has_process/has_product/has_monitoring는 SJPH 매뉴얼에 "
    "해당 5요소(약속·원재료·공정·제품·모니터링)가 포함되면 true. 없으면 null/false/[]. "
    "confidence는 doc_type 분류가 맞다는 확신도를 0.0~1.0 사이 실수로 채우세요 "
    "(확실하면 0.9 이상, 애매하면 0.5 내외, 근거가 거의 없으면 0.3 이하). "
    "아래 예시의 0.0을 그대로 복사하지 말고 실제 확신도를 계산해 넣으세요. "
    '반드시 JSON으로만: {"doc_type":"...","confidence":<0.0~1.0 실수>,'
    '"fields":{"company_name":null,"nib":null,"address":null,"city":null,"province":null,"country":null,"zip":null,'
    '"factory_address":null,"factory_city":null,"factory_province":null,"factory_country":null,"factory_zip":null,'
    '"responsible_person":null,"factory_reg_no":null,"phone":null,"factory_phone":null,'
    '"business_type":null,"employee_count":null,"establishment_date":null,"corporate_reg_no":null,'
    '"product_names":[],"cert_no":null,'
    '"issuer":null,"expiry_date":null,"material_names":[],"process_steps":[],'
    '"has_commitment":false,"has_materials":false,"has_process":false,'
    '"has_product":false,"has_monitoring":false}}'
)


# 파일명 규칙 — LLM 오분류 교정. 위에서부터 먼저 매칭되는 규칙이 최종 doc_type을 결정한다.
# 파일명이 명백한 경우(소개서·제안서·수입서류·품질인증 등)는 LLM 판단을 무시하고 규칙을 신뢰한다.
_NAME_RULES = [
    # (정규식, doc_type, 사유) — 순서 중요(구체적인 것 먼저)
    (r"신청서|신청\s*양식|제안서|견적|\b(?:proposal|quotation)\b|"
     r"\bapplication[\s_-]+(?:form\b|for[\s_-]+(?:halal[\s_-]+)?certificat(?:e|ion)\b)|"
     r"\b(?:permohonan|pengajuan|penawaran)\b|"
     r"request[\s_-]*form|certificate[\s_-]*requirements|"
     r"requirements\s+for\s+(?:halal\s+)?certificat|document\s+requirements|"
     r"\b(?:sample|draft)\s+(?:halal\s+)?certificate\b|"
     r"\b(?:halal\s+)?certificate\s+(?:sample|draft)\b",
     "other", "신청·제안·견적 문서(발급된 인증서 아님)"),
    (r"halal.{0,25}training[\s_]*manual", "sjph_manual", "할랄 교육 매뉴얼"),
    (r"할랄\s*인증|halal[\s_]*cert|sertifikat[\s_]*halal|"
     r"surat[\s_]*keterangan[\s_]*halal|(?<![a-z])skh(?![a-z])",
     "halal_certificate", "명시적 할랄 인증서(첨부 품질인증·사업자번호보다 우선)"),
    (r"bahan\s*vs\s*produk|produk\s*matriks|matriks\s*produk|product.{0,3}matri|matri.{0,3}produk",
     "product_list", "제품×원재료 매트릭스(제품 카탈로그 — 제품명 컬럼이 정본 제품 목록)"),
    (r"\bcatatan\b|기록부|생산\s*일지|작업\s*일지|monitoring\s*record|rekaman",
     "other", "운영·모니터링 기록(구매·생산·재고·검사 기록 — 제품/원재료 카탈로그 아님)"),
    (r"management[\s_]*review|tinjauan[\s_]*manajemen|관리\s*검토|경영\s*검토|"
     r"halal[\s_]*policy|kebijakan[\s_]*halal|할랄\s*정책|"
     r"internal[\s_]*audit|audit[\s_]*internal|내부\s*심사|"
     r"halal[\s_]*team|tim[\s_]*halal|할랄\s*팀|"
     r"halal[\s_]*manual|manual[\s_]*halal|할랄\s*매뉴얼|\bsjph\b",
     "sjph_manual", "SJPH 구성문서(할랄정책·할랄팀·내부심사·경영검토·매뉴얼 — 사업자/공급사 아님)"),
    (r"제조\s*공정\s*도|공정\s*흐름|process\s*flow|flow\s*chart", "process_flow", "공정도"),
    (r"fssc|haccp|\biso\b|\bgmp\b|22000|식품안전|유기취급|organic|kosher", "quality_cert", "품질/식품안전 인증(HACCP·FSSC·ISO·GMP — 할랄 아님)"),
    (r"소개서|회사\s*소개|company\s*profile|제안서|proposal|접수\s*양식|고객\s*접수|데이터\s*양식|intake\s*form|application\s*form", "other", "소개서·제안서·양식(등록증 아님)"),
    (r"원산지|수입\s*서류|country\s*of\s*origin|\bcoo\b|certificate\s*of\s*origin|negara\s*asal|surat\s*keterangan\s*asal", "origin_certificate", "원산지증명서(유래 증빙 정본 — 공급사 선언서와 구분)"),
    (r"선언서|declaration|확인서|설명서|pernyataan", "supplier_declaration", "공급사 선언서·확인서"),
    (r"성적서|시험\s*성적|(?<![a-z])(?:coa|msds)(?![a-z])|성분\s*명세|성분\s*분석|분석\s*성적|"
     r"lembar\s*data\s*keselamatan\s*bahan", "coa_msds", "성적서·성분명세"),
    (r"원재료\s*목록|komposisi\s*bahan", "material_list", "원재료 목록"),
    (r"제품\s*목록", "product_list", "제품 목록"),
    (r"product[\s_]*label", "other", "제품 라벨(첨부 서류)"),
    (r"사업자\s*등록|사업자등록증|business\s*(registration|license)|\bnib\b|법인\s*등기|등록증명원", "nib_business_license", "사업자등록"),
    (r"공장\s*등록|공장등록증|factory\s*registration|manufactur.*licen|izin\s*usaha\s*industri", "factory_registration", "공장등록"),
    (r"할랄\s*인증|halal\s*cert", "halal_certificate", "할랄 인증"),
]


# 서류 제목으로 볼 만한 줄 — 표지·시트명·머리글. 본문 아무 데나 찾으면 원재료표 안의
# 한 단어에 걸려 유형이 뒤집힌다. 앞부분의 짧은 줄만 본다.
_TITLE_MAX_LINES = 3
_TITLE_MAX_LEN = 90
_SHEET_MARK_RE = re.compile(r"^\[시트/제품명:\s*(.+?)\]\s*$")


def _title_lines(text):
    """첫 세 비어 있지 않은 줄만 제목 후보로 본다(번호 라벨은 제외)."""
    if not text:
        return []
    out, plain = [], 0
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if plain >= _TITLE_MAX_LINES:
            break
        plain += 1
        m = _SHEET_MARK_RE.match(ln)
        if m:
            ln = m.group(1).strip()
        if re.fullmatch(r"NIB\s*[:：#-]?\s*\d[\d\s.\-–—]*", ln, re.I):
            continue
        if len(ln) <= _TITLE_MAX_LEN:
            out.append(ln)
    return out


def certificate_invalid_marker(filename, text):
    """미발급/무효 표지를 인증서의 내용으로 해석하지 않는다(Sample ID는 제외)."""
    context = str(filename or "") + "\n" + str(text or "")
    if not re.search(r"certificat|sertifikat|인증|성적서", context, re.I):
        return None
    marker = re.compile(r"\b(?:draft|void|specimen|contoh|template|cancelled|canceled|revoked|"
                        r"not[\s-]+issued|not[\s-]+valid|tidak\s+berlaku|belum\s+terbit)\b|"
                        r"견본|무효|취소|미발급|초안", re.I)
    for line in context.splitlines():
        # 표지 워터마크와 인증서 제목/파일명에 붙은 상태만 인정한다.
        value = line.strip()
        hit = marker.search(value)
        if hit and (hit.start() == 0 or re.search(r"certificat|sertifikat|인증|성적서", value, re.I)):
            return hit[0]
        if re.search(r"\bsample\s+(?:halal\s+)?certificate\b|\bcertificate\s+sample\b", value, re.I):
            return "sample"
    return None


def _request_document(filename, text):
    for candidate in [filename or "", *_title_lines(text)]:
        if re.match(r"\s*(?:renewal|procedure|instructions?)\s*:", candidate, re.I):
            continue  # 갱신 안내 문구는 문서 제목이 아니다.
        if re.search(_NAME_RULES[0][0], candidate, re.I):
            return True
    return False


def refine_doctype_reason(name, llm_type, text=None):
    """(doc_type, 규칙근거) — 파일명 규칙 → 사전(파일명) → 사전(제목줄) → LLM 판단 순.

    사전 폴백을 둔 이유: 인니어 원문 파일명('Diagram alir proses produksi',
    'Catatan pembelian barang')은 한국어·영어로 짜인 _NAME_RULES에 걸리지 않는다.
    사전은 세 언어 표면형을 한 표준 키로 모으므로 규칙을 언어마다 늘리지 않아도 된다.

    제목줄까지 보는 이유: 파일명은 고객이 마음대로 바꾼다. 'Form 9(피치×샷).xlsx' 는
    사전이 아무것도 모르지만, 그 안의 시트명은 'Form.9 재료 보관 기록' 이고 사전은
    그것을 운영 기록물로 정확히 안다. 파일명만 보던 동안 이 서류는 원재료 목록으로
    분류돼 보관 기록의 입출고 행에서 원재료 42~74건이 지어졌다(모델마다 달랐다)."""
    name = _nfc(name)   # 단건 업로드도 macOS 브라우저면 NFD 로 온다
    n = (name or "").lower()
    from .domain_dict import doc_type_of, evidence_key_of, lookup
    base = re.sub(r"\.[a-z0-9]{2,5}$", "", (name or "").strip())
    # 신청 목적이 명시된 제목만 우선한다. 본문의 Application No/용도는 근거가 아니다.
    if certificate_invalid_marker(name, text):
        return "other", "CERT_NOT_VALID"
    if _request_document(name, text):
        return "other", _NAME_RULES[0][2]
    for pat, dt, why in _NAME_RULES:
        if re.search(pat, n):
            # 규칙이 잡았더라도 사전이 증빙 항목을 알고 있으면 근거에 함께 남긴다
            # (인니 실무 기록물은 doc_type은 other여도 SJPH 증빙 항목에 붙는다).
            ev = evidence_key_of(base)
            return dt, (why + " · 증빙 " + ev) if ev else why
    dt = doc_type_of(base)
    if dt:
        key = lookup(base, axis="DOC")
        ev = evidence_key_of(base)
        return dt, "도메인사전 %s%s" % (key, ("·증빙 " + ev) if ev else "")
    for title in _title_lines(text):
        for pat, dt, why in _NAME_RULES:
            if re.search(pat, title, re.I):
                return dt, "제목줄 '%s' → %s" % (title[:40], why)
        if re.fullmatch(r"(?:material\s+)?safety\s+data\s+sheet(?:\s*\(MSDS\))?", title, re.I):
            return "coa_msds", "제목줄 '%s' → MSDS" % title[:40]
        dt = doc_type_of(title)
        if dt:
            key = lookup(title, axis="DOC")
            ev = evidence_key_of(title)
            return dt, "제목줄 '%s' → 도메인사전 %s%s" % (
                title[:40], key, ("·증빙 " + ev) if ev else "")
    return llm_type, None


def _refine_doctype(name, llm_type, text=None):
    return refine_doctype_reason(name, llm_type, text)[0]


# 리스트형 문서(원재료·제품·공정·성분)는 전체 본문에서 항목을 빠짐없이 추출한다.
# classify(2000자)·parse_typed(2500자) 절단으로 목록 뒤쪽이 통째로 누락되던 문제
# (예: Daftar bahan.xlsx 원재료 136개 → 18개만 추출)를 해결한다.
_LIST_FIELDS = {
    "material_list": ("material_names", "원재료명"),
    # product_list(제품×원재료 매트릭스)는 헤더행이 정식 제품목록이므로 기본 분류값을 신뢰한다.
    # 전체본문 list-completion을 돌리면 매트릭스의 원재료 셀까지 제품명으로 과다추출됨 → 제외.
    "process_flow": ("process_steps", "공정 단계"),
    "product_label": ("ingredients", "성분명"),
}


def _norm_key(s):
    """제품/원재료 중복 판정 키 — 대소문자·구두점·공백 무시(한글 등 유니코드 문자 보존).
    [^a-z0-9]로 지우면 한글 제품명이 빈 키가 돼 통째로 유실되므로 \\W_ 만 제거한다."""
    return re.sub(r"[\W_]", "", (s or "").lower())


def _chunk_text(text, size=4500):
    """줄 단위로 ~size자 청크 분할(항목이 중간에서 잘리지 않게)."""
    chunks, buf, n = [], [], 0
    for line in text.splitlines():
        ln = len(line) + 1
        if n + ln > size and buf:
            chunks.append("\n".join(buf)); buf, n = [], 0
        buf.append(line); n += ln
    if buf:
        chunks.append("\n".join(buf))
    return chunks or [text]


def _extract_list_complete(filename, text, label, max_chunks=12, cap=1000, fields=None, status=None):
    """상한 내 청크의 목록 합집합. 미처리 구간과 사전 위험 탐지는 fields에 남긴다."""
    sys = ('문서에서 %s 목록을 빠짐없이 추출하세요. 표·번호목록의 모든 행을 포함하고 '
           '헤더/합계/빈칸은 제외. 반드시 JSON으로만: {"names":[]}' % label)
    seen, out = set(), []
    from .models import normalize_material_name
    key_of = normalize_material_name if label in ("원재료명", "성분명") else _norm_key
    fields = fields if fields is not None else {}
    status = status if status is not None else {}
    status["complete"] = False
    chunks = _chunk_text(text)
    if len(chunks) > max_chunks:
        _record_truncation(fields, "chunks", max_chunks, len(chunks))
        _scan_tail(fields, "\n".join(chunks[max_chunks:]))
    for index, ch in enumerate(chunks[:max_chunks]):
        r = ai_local.llm_json(sys, "파일명: %s\n본문:\n%s" % (filename, ch))
        if isinstance(r, dict) and r.get("error") == "LLM_UNAVAILABLE":
            status["unavailable"] = True
            break
        failure = _llm_failure(r)
        if not failure and (not isinstance(r.get("names"), list)
                            or any(not isinstance(n, str) for n in r["names"])):
            failure = {"llm_error": "BAD_RESPONSE"}
        if failure:
            fields["_llm_error"] = failure["llm_error"]
        names = _normalise_names(r.get("names") if isinstance(r, dict) and not r.get("error") else None)
        for position, nm in enumerate(names):
            if not isinstance(nm, str):
                continue
            nm = nm.strip()
            k = key_of(nm)
            if nm and k and k not in seen:
                seen.add(k); out.append(nm)
                if len(out) >= cap:
                    remaining = any(isinstance(v, str) and key_of(v) and key_of(v) not in seen
                                    for v in names[position + 1:])
                    if remaining or index + 1 < len(chunks):
                        # 항목 상한에 먼저 닿으면 완전히 추출된 청크 수만 기록한다.
                        _record_truncation(fields, "chunks", index if remaining else index + 1, len(chunks))
                        _scan_tail(fields, "\n".join(chunks[index:]))
                    else:
                        status["complete"] = not fields.get("_llm_error")
                    return out
    else:
        status["complete"] = len(chunks) <= max_chunks and not fields.get("_llm_error")
    return out


def _infer_country_zip(addr):
    """주소 문자열에서 국가/우편번호 추론(LLM이 분리 못했을 때 보정)."""
    if not isinstance(addr, str) or not addr:
        return None, None
    zipc = None
    m = re.search(r'\b(\d{5})\b', addr)
    if m:
        zipc = m.group(1)
    low = addr.lower()
    country = None
    if any(x in low for x in ("indonesia", "dki", "provinsi", "kota adm", "jakarta",
                              "kabupaten", "kelurahan", "kecamatan")):
        country = "Indonesia"
    elif "korea" in low:
        country = "Republic of Korea"
    return country, zipc


_ZIP_CACHE = {}
_ZIP_LOOKUP = os.environ.get("GLHAC_ZIP_LOOKUP", "1") == "1"


def _lookup_zip(address):
    """문서에 우편번호가 없을 때 주소로 우편번호를 조회(Nominatim/OpenStreetMap 지오코딩, 전세계).
    무료·키 불필요. 실패/미발견 시 None. 캐시. 한국 상세 도로명은 정확도가 낮을 수 있음."""
    if not _ZIP_LOOKUP or not address:
        return None
    a = str(address).strip()
    if not a or a in _ZIP_CACHE:
        return _ZIP_CACHE.get(a)
    z = None
    try:
        import httpx
        r = httpx.get("https://nominatim.openstreetmap.org/search",
                      params={"q": a, "format": "json", "addressdetails": 1, "limit": 1},
                      headers={"User-Agent": "GLHAC-Halal-Cert/1.0"}, timeout=6)
        d = r.json()
        if d:
            z = (d[0].get("address") or {}).get("postcode")
    except Exception:  # noqa: BLE001 — 네트워크 장애 시 조용히 폴백(추출 차단 없음)
        z = None
    _ZIP_CACHE[a] = z
    return z


def _complete_address(addr, city, province, country):
    """LLM이 주소 뒷부분(시·도·국가)을 절단한 경우 도시→도(주)→국가 순으로 이어붙여 완성.
    이미 포함돼 있으면 그대로 둔다(중복 방지). 표기 순서(…시, 도, 국가)를 지킨다."""
    if not addr:
        return addr
    out = str(addr).rstrip(" .,")
    low = out.lower()
    for part in (city, province, country):   # 도시 → 도(주) → 국가 순
        if part and str(part).strip() and str(part).lower() not in low:
            out += ", " + str(part).strip()
            low = out.lower()
    return out


def _enrich_address(fields):
    """LLM이 country/zip을 비웠으면 주소에서 보정(도시는 LLM 프롬프트에 의존)."""
    co, zp = _infer_country_zip(fields.get("address"))
    if co and not fields.get("country"):
        fields["country"] = co
    if zp and not fields.get("zip"):
        fields["zip"] = zp
    fco, fzp = _infer_country_zip(fields.get("factory_address"))
    if fco and not fields.get("factory_country"):
        fields["factory_country"] = fco
    if fzp and not fields.get("factory_zip"):
        fields["factory_zip"] = fzp


def _parse_ingredient_markers(text):
    """parse_file이 전성분표에 심은 구조 마커에서 (products, materials) 추출. 없으면 None.
    구조 파서 결과이므로 LLM 목록추출을 대체(오분류·셀병합·비결정성 제거)."""
    if "[전성분표]" not in text:
        return None
    prods = [m.strip() for m in re.findall(r'\[제품/시트:\s*([^\]]+)\]', text) if m.strip()]
    mats = []
    if "[원재료목록]" in text:
        body = text.split("[원재료목록]", 1)[1]
        mats = [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.startswith("[")]
    return list(dict.fromkeys(prods)), list(dict.fromkeys(mats))


# 시험성적서(CoA/MSDS) 정량 측정값 추출 — 정규식(결정적). param_key는 main.QUANT_CRITERIA와 일치.
_QUANT_PATTERNS = {
    "ethanol_pct":  r"\b(?:ethanol|etanol|alcohol|alkohol)\b|알코올|에탄올",
    "lead_ppm":     r"\b(?:lead|timbal|Pb)\b|납",
    "cadmium_ppm":  r"\b(?:cadmium|kadmium|Cd)\b|카드뮴",
    "mercury_ppm":  r"\b(?:mercury|merkuri|Hg)\b|수은",
    "arsenic_ppm":  r"\b(?:arsenic|arsen|As)\b|비소",
}
_QUANT_NUMBER = r"(?:\d+(?:[.,]\d+)?|[.,]\d+)(?:[eE][+-]?\d+)?"
_QUANT_UNIT = r"(?:%|ppm\b|ppb\b|mg\s*/\s*kg\b|[µμu]g\s*/\s*kg\b)"
_QUANT_ND = r"(?:ND\b|not[ -]*detected\b|non[ -]*detected\b|tidak\s+terdeteksi\b|불검출)"
_QUANT_UNCLEAR = (r"\b(?:pending|tbd|unknown|unavailable|indeterminate|inconclusive|invalid|"
                  r"not\s+(?:tested|analyzed)|no\s+result|awaiting\s+analysis)\b|"
                  r"시험\s*중|미실시|판정\s*보류")
_QUANT_RESULT = re.compile(
    r"^[\s:：=|()\[\]]*(?:(?:content|result|hasil|kadar|함량|결과)\s*[:=|]?\s*)?"
    r"(?P<qual>" + _QUANT_ND + r"|<\s*LOD\b|<=|>=|[<>≤≥])?\s*"
    r"(?P<value>" + _QUANT_NUMBER + r")?\s*(?P<unit>" + _QUANT_UNIT + r")?", re.I)

_SPEC_HEADER = r"specification|spec|limit|standard|batas|syarat|규격|기준"
_RESULT_HEADER = r"result|hasil|결과|측정값"
_QUANT_TOKEN = re.compile(r"(?:<=|>=|[<>≤≥])?\s*" + _QUANT_NUMBER + r"\s*" + _QUANT_UNIT, re.I)


def _quant_suffix(key, after):
    """항목의 별칭·CAS·점선을 결과와 규격 양쪽에서 동일하게 제거한다."""
    alias = _QUANT_PATTERNS[key]
    after = re.sub(r"^\s*\(\s*(?:" + alias + r")\s*\)", "", after, flags=re.I)
    after = re.sub(r"^\s*\(?\s*(?:CAS\s*(?:No\.?|number)?\s*[:#]?\s*)?"
                   r"\d{2,7}-\d{2}-\d\s*\)?", "", after, flags=re.I)
    return re.sub(r"^\s*\.{2,}\s*", "", after)


def _quant_absence_claim(line, match):
    before, after = line[:match.start()], line[match.end():]
    return (re.search(r"(?:\b(?:free\s+from|bebas(?:\s+dari)?)|무)\s*[-:]?\s*$", before, re.I)
            or re.match(r"\s*(?:[-‐‑–]?\s*free\b|bebas\b|무(?:첨가|함유)?(?![가-힣]))", after, re.I))


def _quant_factor(key, unit):
    unit = unit.replace("μ", "µ")
    ppm = {"%": 10000.0, "ppm": 1.0, "mg/kg": 1.0, "ppb": .001, "µg/kg": .001, "ug/kg": .001}[unit]
    return ppm / 10000 if key == "ethanol_pct" else ppm


def _quant_review(key, raw, reason="unparsed_result"):
    return None, {"qualifier": "unparsed", "unit": "%" if key == "ethanol_pct" else "ppm",
                  "raw": raw, "reason": reason}


def _quant_result(key, line, match, unit_hint=None, strict=False):
    import math
    # 자유문장을 건너뛰지 않는다. 항목의 별칭·CAS·결과 라벨만 수치 앞에 올 수 있다.
    if _quant_absence_claim(line, match):
        return None
    after = _quant_suffix(key, line[match.end():])
    if re.search(_QUANT_UNCLEAR, after, re.I):
        return _quant_review(key, after, "unconfirmed_result")
    result = _QUANT_RESULT.match(after)
    qualifier, value, unit = result.group("qual", "value", "unit")
    # 범위/불확도를 첫 숫자 하나로 축소하지 않는다. 원 구간을 보존하고 검토를 요구한다.
    interval = re.match(r"\s*(?P<op>±|\+/-|to\b|[-–—~])\s*(?P<end>" +
                        _QUANT_NUMBER + r")\s*(?P<unit>" + _QUANT_UNIT + r")?",
                        after[result.end():], re.I)
    if interval and value and not qualifier:
        if after[result.end() + interval.end():].strip(" \t)]"):
            return _quant_review(key, after)
        first_unit = re.sub(r"\s", "", unit or unit_hint or interval["unit"] or "").lower()
        last_unit = re.sub(r"\s", "", interval["unit"] or first_unit).lower()
        if not first_unit:
            return _quant_review(key, after, "missing_unit")
        first = float(value.replace(",", ".")) * _quant_factor(key, first_unit)
        last = float(interval["end"].replace(",", ".")) * _quant_factor(key, last_unit)
        lower, upper = ((first - last, first + last) if interval["op"] in ("±", "+/-")
                        else (first, last))
        if not all(math.isfinite(v) for v in (lower, upper)) or lower > upper:
            return _quant_review(key, after, "invalid_interval")
        return None, {"qualifier": "interval", "lower": lower, "upper": upper,
                      "unit": "%" if key == "ethanol_pct" else "ppm", "raw": after}
    if not qualifier and not (value and (unit or unit_hint)):
        return None
    if not unit and unit_hint and not qualifier and after[result.end():].strip(" \t)]"):
        return None
    if strict and after[result.end():].strip(" \t)]"):
        lod_label = r"(?:LO[DQ]\s*[:=(]?\s*)?" if qualifier and "LOD" in qualifier.upper() else r"LO[DQ]\s*[:=(]?\s*"
        if not qualifier or not re.fullmatch(r"\s*[(\[]?\s*" + lod_label + _QUANT_NUMBER +
                                            r"\s*(?:" + _QUANT_UNIT + r")?\s*[)\]]*\s*",
                                            after[result.end():], re.I):
            return None
    unit = re.sub(r"\s", "", unit or unit_hint or ("%" if key == "ethanol_pct" else "ppm")).lower()
    factor = _quant_factor(key, unit)
    number = float(value.replace(",", ".")) * factor if value else None
    if number is not None and not math.isfinite(number):
        return _quant_review(key, after, "invalid_number")
    meta = {"unit": "%" if key == "ethanol_pct" else "ppm", "source_unit": unit}
    if qualifier:
        meta["qualifier"] = ({"<=": "≤", ">=": "≥"}.get(qualifier, qualifier)
                             if qualifier in ("<", ">", "≤", "≥", "<=", ">=")
                             else "<" if qualifier.startswith("<") else "ND")
        tail = after[result.end():].lstrip(" \t\r\n([")
        lod_prefix = r"(?:LO[DQ]\s*[:=(]?\s*)?" if "LOD" in qualifier.upper() else r"LO[DQ]\s*[:=(]?\s*"
        lod = re.match(lod_prefix + "(" + _QUANT_NUMBER + r")\s*(" + _QUANT_UNIT + r")?", tail, re.I)
        if lod:
            lod_unit = re.sub(r"\s", "", lod.group(2) or unit).lower()
            meta["lod"] = float(lod.group(1).replace(",", ".")) * _quant_factor(key, lod_unit)
            if not math.isfinite(meta["lod"]):
                return _quant_review(key, after, "invalid_number")
            if not result.group("unit"):
                meta["source_unit"] = lod_unit
        elif number is not None and meta["qualifier"] in ("<", "≤", "ND"):
            meta["lod"] = number
        # 구간의 경계값을 보존한다. 검출한계가 없으면 대표값도 미확정이다.
        return meta.get("lod", number), meta
    return number, meta


def _quant_row_results(key, line, match):
    """머리글 없는 행: 명시된 규격 수치는 제외하고 연결된 결과를 모두 읽는다."""
    after = _quant_suffix(key, line[match.end():])
    line = line[:match.end()] + after
    whole = _quant_result(key, line, match)
    if whole and whole[1].get("qualifier") == "interval":
        return [whole]
    # 결과 라벨/ND가 있는 복수 셀은 각 관측을 따로 읽는다. 상한 부호는 규격 근거가 아니다.
    parts = re.split(r"[|;\t]", after)
    if len(parts) > 1 and (re.search(r"(?:" + _RESULT_HEADER + r")\s*[:=]?", after, re.I)
                           or re.search(_QUANT_ND + "|" + _QUANT_UNCLEAR, after, re.I)):
        results = []
        for part in parts:
            if not part.strip() or re.search(r"\b(?:" + _SPEC_HEADER + r"|max|maks|maximum)\b|이하", part, re.I):
                continue
            if re.match(r"\s*(?:≤|<=)", part) and not re.search(_RESULT_HEADER, part, re.I):
                continue  # 기존 머리글 없는 규격|결과 표. 명시적 Result 상한에는 적용하지 않는다.
            candidate = line[:match.end()] + ": " + part.strip().lstrip(":= ")
            result = _quant_result(key, candidate, match)
            if result:
                results.append(result)
            elif re.search(r"(?:" + _RESULT_HEADER + r")\b", part, re.I):
                results.append(_quant_review(key, part))
        return results
    tokens = list(_QUANT_TOKEN.finditer(after))
    if len(tokens) < 2:
        result = _quant_result(key, line, match)
        return [result] if result else ([_quant_review(key, after)]
                                        if re.search(_RESULT_HEADER + "|" + _QUANT_UNCLEAR, after, re.I) else [])
    # 자유문장·다른 성분을 건너뛰어 그 수치를 현재 항목에 붙이지 않는다.
    prefix = after[:tokens[0].start()]
    allowed = re.sub(r"\b(?:" + _SPEC_HEADER + "|" + _RESULT_HEADER +
                     r"|max|maks|maximum|content|kadar)\b|함량", "", prefix, flags=re.I)
    if re.search(r"[^\s:：=|;().\[\]]", allowed):
        result = _quant_result(key, line, match)
        return [result] if result else []
    results = []
    for index, token in enumerate(tokens):
        before = after[tokens[index - 1].end() if index else 0:token.start()]
        following = after[token.end():tokens[index + 1].start() if index + 1 < len(tokens) else len(after)]
        before = re.split(r"[|;\t]", before)[-1]
        residual = re.sub(r"(?:" + _QUANT_PATTERNS[key] + "|" + _SPEC_HEADER + "|" +
                          _RESULT_HEADER + r"|\bmax(?:imum)?\b|\bmaks\b|함량|이하)", "", before, flags=re.I)
        if re.search(r"[^\s:：=().\[\]]", residual):
            break
        if (token[0].lstrip().startswith(("≤", "<=")) or
                re.search(r"(?:" + _SPEC_HEADER + r"|\bmax(?:imum)?\b|\bmaks\b)", before, re.I) or
                re.match(r"\s*(?:이하|\bmax(?:imum)?\b|\bmaks\b)", following, re.I)):
            continue
        candidate = line[:match.end()] + ": " + token[0]
        result = _quant_result(key, candidate, match)
        if result:
            results.append(result)
    return results


_MEASUREMENT_DOC_TYPES = {"coa_msds", "quality_cert", "supplier_declaration"}
_PORK_MARKER = re.compile(r"\b(?:pork|porcine|swine|pig|babi)\b|돼지|돈지|돈육|(?<![가-힣])돈(?![가-힣])", re.I)
_PORK_TEST_MARKER = re.compile(
    r"dna|pcr|qpcr|real[\s-]*time|species\s*identification|"
    r"identifikasi\s*spesies|유전자|종\s*판별", re.I)
_PORK_NEGATIVE = r"\b(?:not[\s-]*detected|non[\s-]*detected|tidak\s+terdeteksi|negative|negatif|none|absent)\b|불\s*검출|음성|없음"
_PORK_POSITIVE = r"\b(?:detected|positive|positif|terdeteksi)\b|검출|양성"
_PORK_UNCLEAR = (r"\b(?:inconclusive|invalid|equivocal|pending|indeterminate|not\s+tested|"
                 r"to\s+follow|menunggu|belum\s+(?:diuji|diperiksa)|tidak\s+dapat\s+ditentukan|"
                 r"failed|unavailable|insufficient\s+sample|no\s+result|not\s+analyzed|"
                 r"not\s+yet\s+detected|not\s+valid|tidak\s+valid|"
                 r"not\s+(?:negative|positive))\b|부적합|판정\s*(?:불가|보류)|시험\s*중|미실시|진행\s*중")
_CONTROL = re.compile(r"\b(?:(?:negative|positive)\s+)?control\b|"
                      r"\bkontrol(?:\s+(?:negatif|positif))?\b|(?:음성|양성)\s*대조(?:군)?", re.I)
_SAMPLE = re.compile(r"\b(?:result\s+of\s+(?:the\s+)?sample|sample|sampel)\b|시료|검체", re.I)
_SAMPLE_LINE = re.compile(r"^\s*(?:result\s+of\s+(?:the\s+)?sample\b|"
                          r"(?:hasil\s+)?sampel\b|sample\b|(?:결과\s*)?(?:시료|검체))", re.I)
_RESULT_LINE = re.compile(r"^[\s|:=]*(?:(?:result|hasil|결과|판정)\s*[:|=]?\s*)?"
                          r"(?:[<>≤≥]?\s*\d+(?:[.,]\d+)?\s*%?\s*)?(?:" +
                          _PORK_NEGATIVE + "|" + _PORK_POSITIVE + "|" + _PORK_UNCLEAR + ")", re.I)
_RESULT_LABEL = re.compile(r"^[\s|:=]*(?:result|hasil|결과|판정)\s*(?:[:|=]|$)", re.I)


def _pork_result_words(text):
    negative = bool(re.search(_PORK_NEGATIVE, text, re.I))
    positive = bool(re.search(_PORK_POSITIVE, re.sub(_PORK_NEGATIVE, "", text, flags=re.I), re.I))
    unclear = bool(re.search(_PORK_UNCLEAR, text, re.I))
    return {value for value, found in ((0.0, negative), (1.0, positive), (None, unclear)) if found}


def _control_valid(label, result):
    """대조군 극성은 시료 결과와 독립적이다. 실패/오염/미확정은 시험 전체 무효."""
    if re.search(_PORK_UNCLEAR, result, re.I):
        return False
    values = _pork_result_words(result)
    if not values:
        return bool(re.fullmatch(r"\s*(?:passed?|valid|validado|적합|정상)\s*", result, re.I))
    if re.search(r"negative|negatif|음성", label, re.I):
        return values == {0.0}
    if re.search(r"positive|positif|양성", label, re.I):
        return values == {1.0}
    return None not in values and len(values) == 1


def _invalid_table_controls(header, cells):
    return any(_CONTROL.search(label) and not _control_valid(label, cells[i])
               for i, label in enumerate(_table_cells(header)) if i < len(cells))


def _table_cells(line):
    line = line.strip()
    # Markdown 외곽선만 제거한다. XLSX의 마지막 빈 결과 열은 보존한다.
    if line.startswith("|") and line.endswith("|"):
        line = line[1:-1]
    return re.split(r"\s*[|\t]\s*| {2,}", line)


def _sample_column(line):
    cells = _table_cells(line)
    samples = [i for i, cell in enumerate(cells) if _SAMPLE.search(cell) and not _CONTROL.search(cell)]
    if (len(samples) == 1 and not _pork_result_words(cells[samples[0]])
            and any(_CONTROL.search(cell) for cell in cells)):
        return samples[0], len(cells)
    return None


def _pork_measurement(text):
    # 같은 줄의 별도 DNA 시험도 독립 블록이다(대조군/시료 셀 구분은 유지).
    text = re.sub(r"[;|]\s*(?=(?:porcine|pork|swine|pig|babi|돼지)\b[^;|\n]*(?:DNA|PCR))",
                  "\n", text, flags=re.I)
    lines, values, found, ambiguous = text.splitlines(), set(), False, False
    table_header, header_line = None, ""
    for i, line in enumerate(lines):
        if header := _sample_column(line):
            table_header = header
            header_line = line
        elif len(_table_cells(line)) <= 1:
            table_header = None
        if not (_PORK_MARKER.search(line) and (_PORK_TEST_MARKER.search(line) or
                (i > 0 and _PORK_TEST_MARKER.search(lines[i - 1])))):
            continue
        found = True
        # 문서 제목과 시료명은 시험 결과 블록이 아니다. 뒤의 실제 항목이 결과를 제공한다.
        if (re.search(r"\b(?:test\s+)?report\s*$|시험\s*성적서\s*$", line, re.I)
                and not _pork_result_words(line)
                and any(_PORK_MARKER.search(row) and _PORK_TEST_MARKER.search(row) for row in lines[i + 1:])):
            continue
        # 앞줄의 Sample|Control 머리글은 해당 시험 행의 열 위치로 연결한다.
        header = table_header or (_sample_column(lines[i - 1]) if i else None)
        if header:
            column, width = header
            cells = _table_cells(line)
            offset = 1 if len(cells) == width + 1 and _PORK_MARKER.search(cells[0]) else 0
            result = _pork_result_words(cells[column + offset]) if len(cells) == width + offset else set()
            values.update(result)
            ambiguous |= not bool(result) or bool(re.search(_PORK_UNCLEAR, line, re.I))
            ambiguous |= _invalid_table_controls(header_line or lines[i - 1], cells[offset:])
            continue
        block = [line]
        for following in lines[i + 1:]:
            if _PORK_MARKER.search(following) and _PORK_TEST_MARKER.search(following):
                break
            if (_SAMPLE_LINE.search(following) or _CONTROL.search(following)
                    or _sample_column(following)
                    or _RESULT_LINE.match(following) or _RESULT_LABEL.match(following)
                    or re.fullmatch(r"[\s|:-]+", following)):
                block.append(following)
            else:
                break
        explicit, implicit, controls, missing_sample = set(), set(), False, False
        if i and _CONTROL.search(lines[i - 1]):
            controls = True
        table_seen = False
        for j, row in enumerate(block):
            header = _sample_column(row)
            if header:
                table_seen = controls = True
                column, width = header
                cells = _table_cells(block[j + 1]) if j + 1 < len(block) else []
                result = _pork_result_words(cells[column]) if len(cells) == width else set()
                explicit.update(result)
                missing_sample |= not bool(result)
                ambiguous |= _invalid_table_controls(row, cells)
                continue
            if table_seen:
                continue
            for k, cell in enumerate(re.split(r"[|;,\t]", row)):
                if control := _CONTROL.search(cell):
                    controls = True
                    status = cell[control.end():].strip(" :=")
                    if status or re.search(r"[:=]", cell[control.end():]):
                        ambiguous |= not _control_valid(control[0], status)
                    continue
                if _SAMPLE_LINE.search(cell):
                    result = _pork_result_words(cell)
                    explicit.update(result)
                    missing_sample |= not bool(result)
                elif (j == 0 and k == 0) or _RESULT_LINE.match(cell) or _RESULT_LABEL.match(cell):
                    result = _pork_result_words(cell)
                    implicit.update(result)
                    wrapped_header = (j == 0 and re.fullmatch(_RESULT_HEADER, cell.strip(), re.I)
                                      and not _pork_result_words(row))
                    missing_sample |= bool(_RESULT_LABEL.match(cell)) and not result and not wrapped_header
        selected = explicit or (implicit if not controls else set())
        values.update(selected)
        # 표에 결과가 없거나 대조군과의 연결이 불분명하면 다른 행의 음성으로 덮지 않는다.
        ambiguous |= (not bool(selected) or missing_sample or (controls and not explicit) or
                      any(re.search(_PORK_UNCLEAR, row, re.I) for row in block))
    if not found:
        return {}
    if ambiguous or None in values or len(values) != 1:
        return {"pork_dna_unclear": True}
    return {"pork_dna": values.pop()}


def extract_measurements(text, metadata=None):
    """정량 측정값 추출. 돼지 DNA 결과가 모호하면 수치 대신 pork_dna_unclear=True."""
    if not text:
        return {}
    out = {}
    lines = text.splitlines()
    observations = {}
    columns = None
    previous_analyte, wrapped_line = None, None
    for index, line in enumerate(lines):
        if index == wrapped_line:
            continue
        if previous_analyte and re.match(r"^\s*(?:(?:" + _RESULT_HEADER + r")\s*[:=]?\s*)?(?:" +
                                        _QUANT_UNCLEAR + r")", line, re.I):
            line = previous_analyte + ": " + line
        cells = _table_cells(line)
        header_suffix = r"(?:\s*\([^)]*\)|\s*\[[^]]*\]|\s*" + _QUANT_UNIT + r")?"
        result_columns = [i for i, cell in enumerate(cells)
                          if re.fullmatch(r"(?:" + _RESULT_HEADER + r")" + header_suffix, cell, re.I)]
        spec_columns = [i for i, cell in enumerate(cells)
                        if re.fullmatch(r"(?:" + _SPEC_HEADER + r")" + header_suffix, cell, re.I)]
        if len(cells) > 1 and (result_columns or spec_columns):
            units = {i: unit[0] for i in result_columns
                     if (unit := re.search(_QUANT_UNIT, cells[i], re.I))}
            unit_columns = [i for i, cell in enumerate(cells)
                            if re.fullmatch(r"units?|satuan|단위", cell, re.I)]
            columns = (result_columns, len(cells), units, unit_columns)
            continue
        matches = sorted(((key, match) for key, pattern in _QUANT_PATTERNS.items()
                          for match in re.finditer(pattern, line, re.I)), key=lambda item: item[1].start())
        analytes = []
        for key, match in matches:
            if analytes and analytes[-1][0] == key:
                previous = analytes[-1][1]
                if (re.fullmatch(r"\s*\(\s*", line[previous.end():match.start()])
                        and re.match(r"\s*\)", line[match.end():])):
                    continue  # Pb/(Lead) 별칭은 같은 관측이다.
            analytes.append((key, match))
        previous_analyte = analytes[-1][1][0] if analytes else None
        for pos, (key, match) in enumerate(analytes):
            if _quant_absence_claim(line, match):
                continue
            # 다음 항목/반복 항목 앞에서 자른다. ND와 상한도 서로를 덮지 않는다.
            end = analytes[pos + 1][1].start() if pos + 1 < len(analytes) else len(line)
            scoped = line[:end].rstrip(" ;|\t")
            if columns and len(cells) == columns[1]:
                results = []
                for column in columns[0]:
                    hints = [columns[2][column]] if column in columns[2] else []
                    hints += [cells[i] for i in columns[3] if cells[i].strip()]
                    hints = {re.sub(r"\s", "", hint).lower() for hint in hints}
                    if len(hints) > 1 or any(not re.fullmatch(_QUANT_UNIT, hint, re.I) for hint in hints):
                        result = _quant_review(key, cells[column], "ambiguous_unit")
                    else:
                        candidate = line[:match.end()] + ": " + cells[column]
                        result = _quant_result(key, candidate, match, next(iter(hints), None), strict=True)
                    value, meta = result or _quant_review(key, cells[column])
                    results.append((value, {**meta, "column": column + 1, "raw": cells[column]}))
            elif columns and (len(cells) > 1 or not _quant_suffix(key, line[match.end():]).strip(" :：=")):
                # read_only XLSX는 맨 뒤 빈 셀을 생략할 수 있다. 항목명만 남은 행도 결과 누락이다.
                results = [_quant_review(key, line, "misaligned_columns")] if columns[0] else []
            else:
                results = _quant_row_results(key, scoped, match)
                if (not results and end == len(line) and not _QUANT_TOKEN.search(line[match.end():])
                        and index + 1 < len(lines)):
                    result = _quant_result(key, line + "\n" + lines[index + 1], match)
                    results = [result] if result else []
                    if result:
                        wrapped_line = index + 1
            for value, meta in results:
                observations.setdefault(key, []).append({"value": value, **meta, "line": index + 1})
    for key, items in observations.items():
        representative = max(items, key=lambda item: item["value"] if item["value"] is not None else float("-inf"))
        out[key] = representative["value"]
        if metadata is not None:
            metadata[key] = {k: v for k, v in representative.items() if k != "value"}
            metadata[key]["observations"] = items
    out.update(_pork_measurement(text))
    return out


def _measurement_fields(fields, text):
    fields.pop("measurements", None)
    fields.pop("measurements_meta", None)
    meta = {}
    measurements = extract_measurements(text, meta)
    if measurements:
        fields["measurements"] = measurements
    if meta:
        fields["measurements_meta"] = meta


# 파일명으로 유형이 확정돼도 필드는 추출해야 한다. 구조 파서가 없는 유형만 LLM으로 보낸다.
_NAME_DECIDES = {"sjph_manual", "supplier_declaration", "process_flow",
                 "quality_cert", "origin_certificate", "halal_certificate"}


def _deterministic_fields(doc_type, text, *, explicit_only=False):
    """지원하는 유형의 명시적 필드만 읽는다. None은 파서 없음, {}는 근거 없음."""
    fields = {}
    if doc_type in ("halal_certificate", "quality_cert"):
        labels = {
            "cert_no": r"(?:certificate\s*(?:no\.?|number)|cert\s*no\.?|"
                       r"(?:nomor|no\.?)\s*sertifikat(?:\s+halal)?|"
                       r"sertifikat(?:\s+halal)?\s+(?:no\.?|nomor)(?![. \t]*sertifikat)|인증(?:서)?\s*번호)",
            "cert_fallback": r"(?:^[ \t]*(?:nomor|no\.?)|"
                             r"(?:halal\s+certificate|sertifikat\s+halal|surat\s+keterangan\s+halal)"
                             r"\s+(?:nomor|no\.?))(?![. \t]*sertifikat)",
            "other_number": r"(?:telephone|phone|telp?|fax|nib|registration|reg\.?|application|lot|batch|page)"
                            r"(?:\s*(?:no\.?|number|nomor|reference))?",
            "issuer": r"(?:issuer|issued\s+by|issuing\s+(?:body|authority)|"
                      r"lembaga\s+penerbit|diterbitkan\s+oleh|발급\s*기관)",
            "expiry_date": r"(?:expiry(?:\s*date)?|expiration(?:\s*date)?|valid\s*(?:until|to)|"
                           r"berlaku(?:\s*(?:sampai|hingga|s\s*/\s*d))?|만료일|유효\s*기간)",
        }
        labelled = list(re.finditer(r"(?<!\w)(?:" + "|".join(
            "(?P<" + key + ">" + label + ")" for key, label in labels.items()) +
            r")[ \t:：#|.-]+", text, re.I | re.M))
        cert_candidates = []
        for index, match in enumerate(labelled):
            key = match.lastgroup
            value = text[match.end():labelled[index + 1].start() if index + 1 < len(labelled) else len(text)]
            value = re.split(r"[\n|]", value.lstrip())[0].strip()
            if value:
                if key in ("cert_no", "cert_fallback"):
                    if explicit_only and key == "cert_fallback":
                        continue
                    # 명시 인증번호 라벨의 값은 원문 후보로 보존한다. 형식 판정은 별도다.
                    if key == "cert_no" and re.fullmatch(r"[+\d][\d ()/_.-]*|\(\d[\d ()/_.-]*", value):
                        cert_candidates.append((False, index, value))
                        continue
                    number = re.match(r"[A-Z0-9][A-Z0-9/_.-]*", value, re.I)
                    if number and any(c.isdigit() for c in number[0]):
                        cert_candidates.append((key != "cert_no", index, number[0]))
                elif key == "expiry_date":
                    dates = re.findall(r"\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b|\b\d{1,2}[-/.]\d{1,2}[-/.]\d{4}\b", value)
                    if dates:
                        from datetime import datetime
                        date = re.sub(r"[/.]", "-", dates[-1])
                        for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
                            try:
                                fields[key] = datetime.strptime(date, fmt).date().isoformat()
                                break
                            except ValueError:
                                continue
                elif key == "issuer":
                    fields[key] = value
        if cert_candidates:
            fields["cert_no"] = min(cert_candidates)[2]
        if "issuer" not in fields and doc_type == "halal_certificate":
            issuer = re.search(r"\b(?:BPJPH|MUI|JAKIM|KMF|KHA)\b", text, re.I)
            if issuer:
                fields["issuer"] = issuer[0].upper()
        if doc_type == "quality_cert":
            cert_type = re.search(r"\b(?:HACCP|ISO\s*\d+|GMP|FSSC\s*\d*)\b", text, re.I)
            if cert_type:
                fields["cert_type"] = cert_type[0]
        return fields
    if doc_type == "process_flow":
        steps = []
        for line in text.splitlines():
            if re.search(r"→|->|⇒", line):
                line = re.sub(r"^.*?(?:steps|stages|단계)\s*[:：]\s*", "", line, flags=re.I)
                steps.extend(s.strip() for s in re.split(r"→|->|⇒", line) if s.strip())
            else:
                step = re.match(r"^\s*(?:\d+[.)]?\s+|[-•]\s+)(\S.*)", line)
                if step:
                    steps.append(step[1].strip())
        return {"process_steps": steps} if steps else {}
    if doc_type == "sjph_manual":
        return {key: bool(re.search(pattern, text, re.I)) for key, pattern in {
            "has_commitment": r"commitment|komitmen|kebijakan\s+halal|경영\s*약속|할랄\s*정책",
            "has_materials": r"\bmaterials?\b|\bbahan\b|원재료",
            "has_process": r"\bprocess\b|\bproses\b|공정",
            "has_product": r"\bproducts?\b|\bproduk\b|제품",
            "has_monitoring": r"monitoring|pemantauan|evaluasi|모니터링|내부\s*심사",
        }.items()}
    if doc_type == "supplier_declaration":
        _measurement_fields(fields, text)
        return fields
    return None


_LIST_VALUE_KEYS = {"material_names", "product_names", "process_steps", "ingredients", "names"}


def _normalise_names(value):
    if isinstance(value, str):
        value = re.split(r"[,\n\r]+", value)
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _normalise_fields(value):
    if not isinstance(value, dict):
        return {}, {"llm_error": "BAD_RESPONSE"}
    failure = _llm_failure(value)
    if value.get("error"):
        return {}, failure
    fields = {}
    for key, val in value.items():
        # 처리 상태와 정량값은 로컬 파서만 기록한다. 모델 응답에서 신뢰하지 않는다.
        if (not isinstance(key, str) or key.startswith("_") or key in
                ("error", "llm_error", "confidence", "doc_type", "field_sources", "locked_fields",
                 "measurements", "measurements_meta")):
            continue
        if key in _LIST_VALUE_KEYS:
            fields[key] = _normalise_names(val)
        elif val is None or isinstance(val, (str, bool, int, float)):
            if isinstance(val, float) and not math.isfinite(val):
                failure = {"llm_error": "BAD_RESPONSE"}
            else:
                fields[key] = val
        else:
            failure = {"llm_error": "BAD_RESPONSE"}
    return fields, failure


def _confidence(value):
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        number = {"high": 0.9, "medium": 0.5, "low": 0.25}.get(str(value).lower(), 0.5)
    return max(0.0, min(1.0, number)) if math.isfinite(number) else 0.5


def _llm_excerpt(text, limit, fields):
    if len(text) > limit:
        _record_truncation(fields, "llm_input", limit, len(text))
        _scan_tail(fields, text[limit:])
    return text[:limit]


def _llm_failure(r):
    """LLM 호출이 실패했으면 {'llm_error': 사유}, 아니면 {}.

    'AI 없음' 배포(LLM_UNAVAILABLE)는 실패가 아니라 설계된 상태다 — 그 모드에서 모든
    서류를 확인 대기로 밀어 올리면 대기 목록이 의미를 잃는다."""
    if not isinstance(r, dict):
        return {"llm_error": "BAD_RESPONSE"}
    err = r.get("error")
    if not err or err == "LLM_UNAVAILABLE":
        return {}
    return {"llm_error": str(err)[:200]}


def _structural_material_result(ing):
    """구조 파서가 읽은 전성분표/원재료표 결과 — LLM 이 관여하지 않은 확정값.

    이 목록은 LLM 이 덮지 못한다. 같은 서류가 배포(없음/로컬/원격)마다 다른 원재료를
    내면 심사 결과가 갈린다."""
    prods, mats = ing
    fields = {}
    if prods:
        fields["product_names"] = prods     # 시트당 완제품 1개(A1)
    if mats:
        fields["material_names"] = mats     # 이름 열 값만
    _enrich_address(fields)
    srcs = {k: "structural" for k in ("product_names", "material_names") if fields.get(k)}
    return {"doc_type": "material_list", "confidence": 0.95, "fields": fields,
            "decided_by": "structural", "field_sources": srcs,
            "locked_fields": sorted(srcs)}


def classify(name, text):
    return _parsed_result(_classify_text(_nfc(name), text), text)


def _classify_text(name, text):
    if not text.strip():
        # 파일명은 편철 유형의 힌트로만 남긴다. 본문을 읽지 못했으므로 신뢰도는 0이다.
        _dt, _why = refine_doctype_reason(name, None, text)
        if _dt and _dt != "other":
            return {"doc_type": _dt, "confidence": 0.0, "fields": {}, "empty": True,
                    "decided_by": "filename", "reason": _why}
        return {"doc_type": "other", "confidence": 0.0, "fields": {}, "empty": True}
    dt_by_name, why = refine_doctype_reason(name, None, text)
    if dt_by_name in _NAME_DECIDES:
        fields = _deterministic_fields(dt_by_name, text)
        if fields is not None or ai_local.provider() == "none":
            fields = fields if fields is not None else {}
            if dt_by_name in _MEASUREMENT_DOC_TYPES:
                _measurement_fields(fields, text)
            return {"doc_type": dt_by_name, "confidence": 0.9, "fields": fields,
                    "field_sources": {key: "structural" for key in fields},
                    "decided_by": "filename", "reason": why}
    # 사전이 '운영 기록물'(구매·생산·재고·검사 기록)로 확정한 문서는 LLM을 부르지 않는다.
    # 불러봐야 뽑은 값이 전부 버려진다 — other 는 _APPLICANT_DOCS·_PRODUCT_SRC·
    # _MATERIAL_SRC 어디에도 없어 aggregate_fields 가 회사정보·제품·원재료를 모두
    # 무시하고, _process_doc 도 다루지 않는다(실측: 기록 사진 6장에서 cert_no 는 전부
    # None, 나머지 필드는 집계 진입조차 못 함). 파일당 15~20초를 그렇게 썼다.
    # dt_by_name 이 'other' 인 경우는 사전이 아는 문서뿐이다 — doc_type_of 는 모르는
    # 이름·제목에 None 을 준다. 따라서 미지 문서의 LLM 판정을 뺏지 않는다.
    if dt_by_name == "other" and why:
        return {"doc_type": "other", "confidence": 0.9, "fields": {},
                "decided_by": "filename", "reason": why}
    # 구조 파서가 전성분표/원재료표를 이미 읽었으면 LLM 을 부르지 않는다.
    # 종전에는 부른 뒤 그 결과를 덮었다 — 목록은 지켜졌지만 서류당 10~90초를 버렸고,
    # 더 나쁜 건 그 호출이 실패하면 같은 서류가 배포·시점에 따라 다른 상태로 남았다는
    # 점이다(본문이 마커뿐이라 LLM 이 낼 수 있는 건 지어낸 주소 정도였다).
    ing = _parse_ingredient_markers(text)
    if ing is not None:
        return _structural_material_result(ing)
    metadata = {}
    r = ai_local.llm_json(_CLASSIFY_SYS, "파일명: %s\n본문 발췌:\n%s" % (
        name, _llm_excerpt(text, 2000, metadata)))
    unavailable = isinstance(r, dict) and r.get("error") == "LLM_UNAVAILABLE"
    if not isinstance(r, dict) or "doc_type" not in r or r.get("error"):
        # 호출이 실패한 것과 '모델이 보고도 못 찾은 것'은 다르다. 둘 다 other/0.0 으로
        # 떨어뜨리면 화면에는 똑같이 '분석 완료, 특이사항 없음'으로 보인다 — 실측:
        # 다른 서비스가 Ollama 를 점유한 동안 서류가 90초 타임아웃으로 줄줄이 빈 결과가
        # 됐는데 실패한 티가 어디에도 없었다. 실패는 실패라고 남겨 사람이 다시 돌린다.
        r = {"doc_type": "other", "confidence": 0.0, "fields": {},
             **(_llm_failure(r) or ({} if unavailable else {"llm_error": "BAD_RESPONSE"}))}
    else:
        r = dict(r)
    r["fields"], failure = _normalise_fields(r.get("fields", {}))
    r.update(failure)
    r["fields"].update(metadata)
    if not isinstance(r.get("doc_type"), str):
        r["doc_type"] = "other"
        r["llm_error"] = "BAD_RESPONSE"
    r["doc_type"] = _refine_doctype(name, r.get("doc_type"), text)  # 파일명·제목줄 교정
    if r.get("doc_type") not in DOC_TYPES:
        r["doc_type"] = "other"
    r["confidence"] = (0.0 if r.get("llm_error") else
                       (0.9 if unavailable and dt_by_name else _confidence(r.get("confidence", 0.0))))
    # 리스트형 문서는 전체 본문에서 목록을 완전 추출(절단 2000자로는 뒤쪽 누락).
    lf = _LIST_FIELDS.get(r["doc_type"])
    if lf and len(text) > 2000 and not unavailable and not r.get("llm_error"):
        fk, label = lf
        status = {}
        complete = _extract_list_complete(name, text, label, fields=r["fields"], status=status)
        if len(complete) > len(r["fields"].get(fk) or []):
            r["fields"][fk] = complete
        if status.get("complete"):
            _clear_truncation(r["fields"], "llm_input")
    # 구조 파서가 '이건 목록 문서가 아니다'라고 판정했으면(제품×원재료 매트릭스)
    # LLM 이 뽑은 목록은 버린다. 구조 판정이 옳고 LLM 이 지어낸 것이다 —
    # 실측: 같은 매트릭스 파일에서 로컬 LLM 이 제품 35건, GPT 가 원재료 21건을 뽑았고
    # 실행마다 값이 달라 배포별로 신청서 내용이 갈렸다.
    if MATRIX_MARK in text:
        for _lk in ("material_names", "product_names"):
            r["fields"].pop(_lk, None)
        r["field_sources"] = {"material_names": "structural_reject",
                              "product_names": "structural_reject"}
        r["locked_fields"] = ["material_names", "product_names"]
        r["reason"] = "제품×원재료 매트릭스 — 목록 문서가 아님"
        _enrich_address(r["fields"])
        return r
    # 구조 파서가 안 걸린 목록은 LLM 이 낸 값이다. 출처를 남겨 두면 화면이
    # '사람이 확인할 값'으로 표시할 수 있고, 배포별 차이도 추적된다.
    if not isinstance(r.get("field_sources"), dict):
        r["field_sources"] = {}
    for _lk in ("material_names", "product_names"):
        if r["fields"].get(_lk) and _lk not in r["field_sources"]:
            r["field_sources"][_lk] = "llm"
    if not isinstance(r.get("locked_fields"), list):
        r["locked_fields"] = []
    _enrich_address(r["fields"])   # 도시/국가/우편 보정
    # 시험성적서/성분명세/품질인증 → 정량 측정값 추출(자동 반영용)
    if r["doc_type"] in _MEASUREMENT_DOC_TYPES:
        _measurement_fields(r["fields"], text)
    return r


# 개별 업로드 컨텍스트 파싱 — doc_type별 추출 필드 스펙
_FIELD_SPEC = {
    "nib_business_license": ("회사명, NIB(사업자등록번호), 주소(도·시·국가 전체), 도시, 도(주), 국가, 우편번호, 회사 전화번호, 사업의 종류/업태, 개업일(YYYY-MM-DD), 법인등록번호",
                             '{"company_name":null,"nib":null,"address":null,"city":null,"province":null,"country":null,"zip":null,"phone":null,"business_type":null,"establishment_date":null,"corporate_reg_no":null}'),
    "factory_registration": ("등록된 회사명, 사업자번호(있으면), 공장등록번호, 공장 주소(도·시·국가 전체), 도시, 도(주), 국가, 우편번호, 공장 전화번호(Tel), 총 직원 수(숫자)",
                             '{"company_name":null,"nib":null,"factory_reg_no":null,"factory_address":null,"factory_city":null,"factory_province":null,"factory_country":null,"factory_zip":null,"factory_phone":null,"employee_count":null}'),
    "halal_certificate": ("인증번호, 발급기관, 만료일, 대상(제품/원재료)",
                          '{"cert_no":null,"issuer":null,"expiry_date":null,"scope":null}'),
    "quality_cert": ("인증종류(HACCP/ISO/GMP/FSSC), 인증번호, 만료일",
                     '{"cert_type":null,"cert_no":null,"expiry_date":null}'),
    "consent": ("서명 여부, 서명일", '{"signed":false,"signed_date":null}'),
    "product_list": ("제품명 목록", '{"product_names":[]}'),
    "material_list": ("원재료명 목록. 표 제목·시트명(예: '[시트/제품명: ...]')·문서 상단에 제품명이 있으면 product_names에도 넣으세요(원재료가 아닌 완제품명)",
                      '{"material_names":[],"product_names":[]}'),
    "process_flow": ("공정 단계 순서 목록(원료입고→배합→가열→충전→포장 등)",
                     '{"process_steps":[]}'),
    "sjph_manual": ("SJPH 5요소 포함 여부(약속·원재료·공정·제품·모니터링)",
                    '{"has_commitment":false,"has_materials":false,"has_process":false,'
                    '"has_product":false,"has_monitoring":false}'),
    "product_label": ("성분 목록", '{"ingredients":[]}'),
    # 원료 성적서(CoA)·MSDS — 범용 분류기로 넘기면 매뉴얼용 필드가 채워졌다(2026-11-11 실측).
    # product_names 는 완제품명만: 원료 자체의 성적서면 비운다(원료가 제품으로 등록되던 문제).
    "coa_msds": ("원료 성적서(CoA)·물질안전보건자료(MSDS): 이 문서가 다루는 원료명과 구성 성분명(material_names), "
                 "완제품명(원료 자체의 문서면 빈 목록), 제조사, 로트번호, 발행일(YYYY-MM-DD)",
                 '{"material_names":[],"product_names":[],"manufacturer":null,"lot_no":null,"issue_date":null}'),
}


# 추출 결과 교차검증 — 업로드 경로는 '사용자가 고른 칸'을 믿고 그 칸의 필드만 뽑는다.
# 칸을 잘못 고르면 모델은 시킨 대로 엉뚱한 값을 뽑아 온다(2026-11-11: MSDS 6건이 사업자등록증
# 칸으로 들어가 공급사 회사명·주소가 업체 정보를 덮음). 원문과 추출값을 함께 보여 다시 묻는다.
_VERIFY_SYS = (
    "당신은 할랄 인증 신청 서류의 접수 검수자입니다. 업로드된 문서 원문과, 시스템이 이 문서에서 "
    "추출해 등록한 값을 비교하세요. (1) 원문이 '선택된 서류 종류'가 맞는가 (2) 추출값 하나하나가 "
    "원문에 실제로 있는 값인가(지어낸 값이 아닌가) (3) 신청 업체 정보(회사명·사업자번호·주소)에 "
    "공급사·제조사 등 다른 회사 정보가 들어가지 않았는가 — 이 (3)은 서류 종류가 사업자등록증(nib_business_license)·"
    "공장등록증(factory_registration)일 때만 본다. 성적서·MSDS(coa_msds)·공급자 선언서·할랄 인증서·원산지 증명서·"
    "품질 인증서는 원래 제조사·공급사 문서이므로 회사명이 신청 업체와 다른 것은 정상이며 문제로 보지 마라. "
    "확실하지 않으면 warn 으로 답하세요. "
    '반드시 JSON 으로만: {"doc_type_ok":true,"detected_type":null,'
    '"field_issues":[{"field":"","issue":""}],"verdict":"ok|warn|fail","summary":"한 문장"}. '
    "detected_type 은 다음 중 하나 또는 null: " + ", ".join(DOC_TYPES))


def autofill_value_in_text(field, value, text):
    """역할 라벨과 완전한 식별값이 함께 있어야 자동 반영/검증 근거로 쓴다."""
    value, text = str(value or ""), str(text or "")
    if field in ("cert_no", "certificate_no"):
        candidate = _deterministic_fields("halal_certificate", text, explicit_only=True).get("cert_no")
        return bool(candidate and re.sub(r"\s+", "", value).casefold() == re.sub(r"\s+", "", candidate).casefold())
    labels = {
        "nib": r"\bNIB\b|nomor\s+induk\s+berusaha|사\s*업\s*자\s*등\s*록\s*번\s*호|사업자\s*번호|"
               r"business\s+(?:registration|license)\s*(?:no\.?|number)",
        "factory_reg_no": r"factory\s*(?:registration\s*)?(?:no\.?|number)|공장\s*등록\s*번호|"
                          r"nomor\s+(?:registrasi\s+)?pabrik|izin\s+usaha\s+industri",
    }
    if field == "factory_reg_no" and re.search(r"^\s*factory\s+registration\b", text, re.I | re.M):
        labels[field] += r"|registration(?:\s*(?:no\.?|number))?"
    if field in labels:
        from .schemas import NIB_DIGIT_LENGTHS
        if field == "nib" and (not re.fullmatch(r"[0-9\s.\-‐‑‒–—―－−]+", value)
                               or len(re.sub(r"\D", "", value)) not in NIB_DIGIT_LENGTHS):
            return False
        norm = lambda s: re.sub(r"[\s.\-‐‑‒–—―－−]", "", s).casefold()
        for label in re.finditer(r"(?<!\w)(?:" + labels[field] + r")(?!\w)", text, re.I):
            tail = text[label.end():].lstrip(" \t:：#|.-\r\n")
            token = re.match(r"[0-9]+(?:[ .\-‐‑‒–—―－−]+[0-9]+)*" if field == "nib"
                             else r"[A-Z0-9][A-Z0-9/_.-]*", tail, re.I)
            if token and norm(token[0]) == norm(value) and not re.match(r"[\w]", tail[token.end():]):
                return True
        return False
    norm = lambda s: re.sub(r"[\W_]+", "", s, flags=re.UNICODE).casefold()
    key = norm(value)
    return bool(key and key in norm(text))


def normalise_verification(response, *, internal=False):
    """검증 경계에서 스키마를 확정한다. 복구할 수 없는 타입은 오류로 남긴다."""
    bad = {"verdict": "unknown", "summary": "AI 검증 응답 형식 오류",
           "doc_type_ok": None, "detected_type": None, "field_issues": [],
           "source": "llm", "llm_error": "BAD_RESPONSE"}
    if not isinstance(response, dict):
        return bad
    verdict = response.get("verdict")
    if verdict not in (("ok", "warn", "fail", "unknown") if internal else ("ok", "warn", "fail")):
        return bad
    doc_ok = response.get("doc_type_ok")
    if isinstance(doc_ok, str) and doc_ok.strip().lower() in ("true", "false"):
        doc_ok = doc_ok.strip().lower() == "true"
    if doc_ok is not None and not isinstance(doc_ok, bool):
        return bad
    issues = response.get("field_issues")
    if issues is None:
        issues = []
    if isinstance(issues, dict):
        issues = [{"field": field, "issue": issue} for field, issue in issues.items()]
    if not isinstance(issues, list):
        return bad
    normal = []
    for issue in issues:
        if (not isinstance(issue, dict) or not isinstance(issue.get("field"), str)
                or not issue["field"].strip() or not isinstance(issue.get("issue"), str)
                or not issue["issue"].strip()):
            return bad
        item = {"field": issue["field"].strip(), "issue": issue["issue"].strip()}
        if isinstance(issue.get("code"), str):
            item["code"] = issue["code"]
        normal.append(item)
    detected = response.get("detected_type")
    if detected is not None and (not isinstance(detected, str) or detected not in DOC_TYPES):
        return bad
    summary = response.get("summary") or ""
    if not isinstance(summary, str):
        return bad
    result = {"verdict": "warn" if verdict == "ok" and (normal or doc_ok is False) else verdict,
              "summary": summary[:300], "doc_type_ok": doc_ok, "detected_type": detected,
              "field_issues": normal, "source": "llm"}
    if internal:
        for key in ("source", "llm_error", "_truncated", "_truncations"):
            if key in response:
                result[key] = response[key]
    return result


def verify_extraction(doc_type, filename, text, fields, case_company=None):
    """추출 결과를 원문과 대조한 판정만 돌려준다 — 저장·반영은 호출자가 한다.

    verdict: ok | warn | fail | unknown(전문 없음·호출 실패·AI 없음)."""
    import json as _json
    shown = {k: v for k, v in (fields or {}).items() if not str(k).startswith("_")}
    if not (text or "").strip():
        return {"verdict": "unknown", "summary": "전문 없음 — 재처리 필요",
                "doc_type_ok": None, "detected_type": None, "field_issues": [], "source": "rule"}
    # 원문에 없는 값은 모델의 판단과 무관하게 먼저 표시한다. 예약 키는 추출값이 아니다.
    norm_text = re.sub(r"\s+", "", text).casefold()
    issues = [{"field": key, "issue": "원문 역할 라벨 또는 번호 형식 불일치"}
              for key in (fields or {}).get("_rejected_identifiers", {})]
    # 정규화·추론되는 값(날짜 YYYY-MM-DD, 주소에서 보정한 도시·국가 등)은 원문 문자열과 달라도 정상이다.
    _derived = {"city", "province", "country", "zip", "factory_city", "factory_province", "factory_country",
                "factory_zip", "establishment_date", "issue_date", "expiry_date", "signed_date"}
    for k, v in shown.items():
        if k in _derived:
            continue
        if k in ("nib", "factory_reg_no", "cert_no", "certificate_no"):
            if v and not autofill_value_in_text(k, v, text):
                issues.append({"field": k, "issue": "원문에 없는 값"})
            continue
        values = v if isinstance(v, list) else [v]
        if any(isinstance(value, str) and len(value.strip()) >= 3 and
               re.sub(r"\s+", "", value).casefold() not in norm_text for value in values):
            issues.append({"field": k, "issue": "원문에 없는 값"})
    shown_json = _json.dumps(shown, ensure_ascii=False)
    limits = {}
    for content, limit in ((shown_json, 1500), (text, 8000)):
        if len(content) > limit:
            _record_truncation(limits, "verify_input", limit, len(content))
    if isinstance(fields, dict):
        _clear_truncation(fields, "verify_input")
        for marker in limits.get("_truncations", [limits.get("_truncated")]):
            if marker:
                _record_truncation(fields, **marker)
    user = ("선택된 서류 종류: %s\n파일명: %s\n신청 업체명: %s\n추출값(JSON): %s\n원문:\n%s"
            % (doc_type, filename, case_company or "-",
               shown_json[:1500], text[:8000]))
    r = ai_local.llm_json(_VERIFY_SYS, user)
    fail = _llm_failure(r)
    if not fail and isinstance(r, dict) and not r.get("error"):
        r = normalise_verification(r)
        fail = {"llm_error": r["llm_error"]} if r.get("llm_error") else {}
    if fail or not isinstance(r, dict) or r.get("error"):
        return {"verdict": "warn" if issues else "unknown",
                "summary": "원문에 없는 추출값 — AI 검증을 하지 못함" if issues else "AI 검증을 하지 못함",
                "doc_type_ok": None, "detected_type": None, "field_issues": issues,
                "source": "rule" if issues else "llm", **fail, **limits}
    dt = r.get("detected_type")
    for issue in r["field_issues"]:
        if issue not in issues:
            issues.append(issue)
    verdict = "warn" if issues and r["verdict"] == "ok" else r["verdict"]
    return {"verdict": verdict, "summary": str(r.get("summary") or "")[:300],
            "doc_type_ok": r.get("doc_type_ok"),
            "detected_type": dt if dt in DOC_TYPES else None,
            "field_issues": issues, "source": "llm", **limits}


def parse_typed(doc_type, filename, data):
    """업로드 칸(문구)에 맞는 doc_type으로 해당 필드만 파싱."""
    text = parse_file(filename, data)
    return parse_typed_text(doc_type, filename, text)


def parse_typed_text(doc_type, filename, text):
    """확보한 본문에서 해당 유형의 필드만 추출 — 칸 교정 때 파일·OCR을 다시 읽지 않는다."""
    return _parsed_result(_parse_typed_text(doc_type, _nfc(filename), text), text)


def _parse_typed_text(doc_type, filename, text):
    text = text or ""
    # 신청서를 인증서 업로드 칸에 넣어도 인증 필드와 체크리스트로 흘리지 않는다.
    invalid = certificate_invalid_marker(filename, text)
    if invalid or (doc_type in ("halal_certificate", "quality_cert") and _request_document(filename, text)):
        return {"doc_type": "other", "fields": {"_cert_not_valid": invalid, "_counts_as_evidence": False} if invalid else {}, "confidence": 0.9,
                "reason": _NAME_RULES[0][2], "text_len": len(text), "text": text}
    if not text.strip():
        return {"doc_type": doc_type, "fields": {}, "confidence": 0.0, "empty": True, "text_len": 0,
                "text": text or ""}
    # 근본: 전성분표 구조 마커가 있으면 구조 파서 결과를 신뢰(LLM 목록추출 우회).
    ing = _parse_ingredient_markers(text)
    if ing is not None:
        prods, mats = ing
        fields = {}
        if prods:
            fields["product_names"] = prods
        if mats:
            fields["material_names"] = mats
        return {"doc_type": "material_list", "fields": fields, "confidence": 0.95,
                "field_sources": {"material_names": "structural", "product_names": "structural"},
                "text_len": len(text), "excerpt": text[:300], "text": text}
    deterministic = _deterministic_fields(doc_type, text)
    if deterministic is not None:
        if doc_type in _MEASUREMENT_DOC_TYPES:
            _measurement_fields(deterministic, text)
        dt_by_name, _ = refine_doctype_reason(filename, None, text)
        return {"doc_type": doc_type, "fields": deterministic,
                "confidence": 0.9 if any(deterministic.values()) or dt_by_name == doc_type else 0.0,
                "field_sources": {key: "structural" for key in deterministic},
                "text_len": len(text), "excerpt": text[:300], "text": text}
    spec = _FIELD_SPEC.get(doc_type)
    if not spec:
        cl = classify(filename, text)
        cl["text_len"] = len(text)
        cl["text"] = text
        return cl
    desc, fmt = spec
    lf = _LIST_FIELDS.get(doc_type)
    unavailable = False
    if lf and len(text) > 2500:
        # 리스트형: 전체 본문에서 완전 추출(절단 방지)
        fk, label = lf
        fields, fail, status = {}, {}, {}
        fields[fk] = _extract_list_complete(filename, text, label, fields=fields, status=status)
        unavailable = status.get("unavailable", False)
        if fields.get("_llm_error"):
            fail["llm_error"] = fields["_llm_error"]
    else:
        sys = "문서에서 다음 필드만 추출하세요(없으면 null). 반드시 JSON으로만: " + fmt + " (대상: " + desc + ")"
        metadata = {}
        r = ai_local.llm_json(sys, "파일명: %s\n본문:\n%s" % (
            filename, _llm_excerpt(text, 2500, metadata)))
        unavailable = isinstance(r, dict) and r.get("error") == "LLM_UNAVAILABLE"
        fail = _llm_failure(r)
        # 실패 응답({'error': ...})을 그대로 필드로 쓰면 error 라는 이름의 값이 저장되고
        # confidence 0.85 까지 붙는다 — 못 뽑았는데 잘 뽑은 것처럼 기록된다.
        fields, field_failure = _normalise_fields(
            r.get("fields") if isinstance(r, dict) and "fields" in r and not r.get("error") else r)
        fail.update(field_failure)
        fields.update(metadata)
    # 라벨의 ingredients도 확인 대기 목록에서 사용하는 원재료 키로 통일한다.
    if fields.get("ingredients") and not fields.get("material_names"):
        fields["material_names"] = fields["ingredients"]
    if doc_type in _MEASUREMENT_DOC_TYPES:
        _measurement_fields(fields, text)
    _enrich_address(fields)   # 도시/국가/우편 보정
    dt_by_name, _ = refine_doctype_reason(filename, None, text)
    confidence = 0.0 if fail else 0.85
    if unavailable:
        confidence = 0.9 if dt_by_name == doc_type or fields.get("measurements") else 0.0
    out = {"doc_type": doc_type, "fields": fields,
           "field_sources": {k: "llm" for k in ("material_names", "product_names") if fields.get(k)},
           "confidence": confidence,
           "text_len": len(text), "excerpt": text[:300], "text": text}
    out.update(fail)
    return out


_APPLICANT_DOCS = ("nib_business_license", "factory_registration")
# 제품/원재료명을 신뢰할 카탈로그 문서 유형(기록·타목록의 오염 방지)
# material_list(전성분표)도 시트명 기반 완제품 추출 대상이므로 포함
_PRODUCT_SRC = {"product_list", "sjph_manual", "material_list", "coa_msds", "product_label"}
_MATERIAL_SRC = {"material_list", "coa_msds", "product_label", "quality_cert", "supplier_declaration"}


def aggregate_fields(docs):
    """분류 문서들의 추출 필드를 신청서용으로 집계.
    회사명·NIB·주소·책임자·공장등록번호는 신청기업 서류(사업자/공장등록증)에서만 취함(공급사 제외).

    제품 목록 문서의 제품명과 구조 파서의 목록은 즉시 반영한다.
    그 밖의 LLM 제품·원재료명은 원본 대조를 위해 확인 대기로 남긴다."""
    agg = {"company_name": None, "company_name_ko": None, "company_name_en": None,
           "nib": None, "address": None, "factory_address": None,
           "city": None, "province": None, "country": None, "zip": None,
           "factory_city": None, "factory_province": None, "factory_country": None, "factory_zip": None,
           "responsible_person": None, "responsible_person_ko": None, "responsible_person_en": None,
           "phone": None, "factory_phone": None,
           "business_type": None, "employee_count": None,
           "establishment_date": None, "corporate_reg_no": None,
           "factory_reg_no": None, "products": [], "materials": [], "certificates": [],
           # LLM 만 뽑은 목록 — 자동 반영하지 않고 사람 확인을 기다린다
           "pending": []}
    _pk = set()   # 제품 중복 판정 키(대소문자 무시)
    _mk = set()   # 원재료 중복 판정 키
    _pend_pk, _pend_mk = set(), set()
    for d in docs:
        f = d.get("fields") or {}
        applicant = d.get("doc_type") in _APPLICANT_DOCS or d.get("doc_type") is None
        if applicant:
            # 회사명 국문/영문 분리 — 한글 포함이면 국문(정식상호), 아니면 영문. 주 필드는 국문 우선.
            if f.get("company_name"):
                _cn = str(f["company_name"]).strip()
                if re.search(r'[가-힣]', _cn):
                    if not agg["company_name_ko"]:
                        agg["company_name_ko"] = _cn
                elif not agg["company_name_en"]:
                    agg["company_name_en"] = _cn
            if not agg["nib"] and f.get("nib"):
                agg["nib"] = f["nib"]
            # 회사 주소(NIB)와 공장 주소(공장등록증)를 분리 — 뭉치면 회사주소가 공장주소로 잘못 저장됨
            if not agg["address"] and f.get("address") and d.get("doc_type") != "factory_registration":
                agg["address"] = f["address"]
            if not agg["factory_address"] and f.get("factory_address"):
                agg["factory_address"] = f["factory_address"]
            if d.get("doc_type") == "factory_registration" and not agg["factory_address"] and f.get("address"):
                agg["factory_address"] = f["address"]   # 공장등록증의 주소는 공장 주소
            for _c in ("city", "province", "country", "zip"):        # 회사 도시/도/국가/우편(NIB)
                if not agg[_c] and f.get(_c) and d.get("doc_type") != "factory_registration":
                    agg[_c] = f[_c]
            for _fc in ("factory_city", "factory_province", "factory_country", "factory_zip"):   # 공장 도시/도/국가/우편(공장등록증)
                if not agg[_fc] and f.get(_fc):
                    agg[_fc] = f[_fc]
            # 대표자 국문/영문 분리 — 한글 포함이면 국문, 아니면 영문. 주 필드는 국문 우선.
            if f.get("responsible_person"):
                _rp = str(f["responsible_person"]).strip()
                if re.search(r'[가-힣]', _rp):
                    if not agg["responsible_person_ko"]:
                        agg["responsible_person_ko"] = _rp
                elif not agg["responsible_person_en"]:
                    agg["responsible_person_en"] = _rp
            if not agg["factory_reg_no"] and f.get("factory_reg_no"):
                agg["factory_reg_no"] = f["factory_reg_no"]
            # 전화 엄격 분리(오피스↔공장 혼입 방지): 회사 전화(office_phone)=사업자등록증(NIB)에서만,
            # 공장 전화=공장등록증에서만. 해당 문서에 전화가 없으면 비워둔다(타 문서 값으로 오염 금지).
            if d.get("doc_type") == "nib_business_license" and not agg["phone"] and f.get("phone"):
                agg["phone"] = f["phone"]
            if d.get("doc_type") == "factory_registration" and not agg["factory_phone"] and (f.get("factory_phone") or f.get("phone")):
                agg["factory_phone"] = f.get("factory_phone") or f.get("phone")
            # 사업유형은 회사 속성(사업자등록증의 업태·종목)이 정본 → NIB 우선, 없을 때만 타 문서
            if d.get("doc_type") == "nib_business_license" and f.get("business_type"):
                agg["business_type"] = f["business_type"]
            elif not agg["business_type"] and f.get("business_type"):
                agg["business_type"] = f["business_type"]
            if not agg["employee_count"] and f.get("employee_count") not in (None, ""):
                agg["employee_count"] = f["employee_count"]
            if not agg["establishment_date"] and f.get("establishment_date"):
                agg["establishment_date"] = f["establishment_date"]
            if not agg["corporate_reg_no"] and f.get("corporate_reg_no"):
                agg["corporate_reg_no"] = f["corporate_reg_no"]
        # 제품/원재료명은 해당 카탈로그 문서에서만 수집(기록·타목록의 오염 방지).
        _dt = d.get("doc_type")
        # 출처는 분류 결과(field_sources) 또는 문서에 저장된 fields._sources 에서 온다.
        _src = (d.get("field_sources") or (f.get("_sources") if isinstance(f, dict) else None) or {})

        def _take(key, bucket, seenset, srcset):
            """구조 파서 값과 제품 목록 문서의 제품명은 즉시 반영하고 나머지는 확인한다."""
            from .models import normalize_material_name
            key_of = normalize_material_name if key == "material_names" else _norm_key
            vals = [v for v in (f.get(key) or []) if v and key_of(v)]
            if not vals:
                return
            if (_src.get(key) == "structural"
                    or (_dt == "product_list" and key == "product_names"
                        and _src.get(key) != "structural_reject")):
                for v in vals:
                    if key_of(v) not in seenset:
                        seenset.add(key_of(v)); bucket.append(v)
            elif _src.get(key) == "structural_reject":
                return                      # 구조 파서가 '목록 아님'이라 판정한 문서
            else:
                for v in vals:
                    if key_of(v) not in srcset:
                        srcset.add(key_of(v))
                        agg["pending"].append({"kind": key, "value": v,
                                               "doc_type": _dt, "source": "llm"})

        if _dt in _PRODUCT_SRC or _dt is None:
            _take("product_names", agg["products"], _pk, _pend_pk)
        if _dt in _MATERIAL_SRC or _dt is None:
            _take("material_names", agg["materials"], _mk, _pend_mk)
        if f.get("cert_no"):
            agg["certificates"].append({"cert_no": f.get("cert_no"), "issuer": f.get("issuer"),
                                        "expiry": f.get("expiry_date")})
    # 회사명·대표자 주 필드 = 국문 우선(없으면 영문)
    agg["company_name"] = agg["company_name_ko"] or agg["company_name_en"]
    agg["responsible_person"] = agg["responsible_person_ko"] or agg["responsible_person_en"]
    # 주소 절단 보강: LLM이 주소 뒷부분(시·도·국가)을 자른 경우 도시→도→국가 순으로 이어붙여 완성.
    agg["address"] = _complete_address(agg["address"], agg["city"], agg["province"], agg["country"])
    agg["factory_address"] = _complete_address(agg["factory_address"], agg["factory_city"], agg["factory_province"], agg["factory_country"])
    # 우편번호가 문서에 없으면 주소로 조회(전세계 지오코딩) — 회사·공장 각각.
    if not agg["zip"] and agg["address"]:
        agg["zip"] = _lookup_zip(agg["address"])
    if not agg["factory_zip"] and agg["factory_address"]:
        agg["factory_zip"] = _lookup_zip(agg["factory_address"])
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
        _lines = []                       # 여기서 읽은 OCR을 버리지 않고 넘긴다
        text = parse_file(name, raw, ocr_sink=_lines)
        cl = classify(name, text)
        fields = dict(cl.get("fields") or {})
        if cl.get("field_sources"):
            fields["_sources"] = cl["field_sources"]
        if cl.get("llm_error"):
            fields["_llm_error"] = cl["llm_error"]
        # content_b64는 업로드 상한과 같은 값으로 제한 (20MB)
        # main.py 의 _UPLOAD_MAX_BYTES 와 같은 상수(UPLOAD_MAX_BYTES)를 공유한다 (SRS NFR-05)
        d = {"filename": os.path.basename(name), "doc_type": cl["doc_type"],
             "ocr_lines": _lines or None,
             "doc_type_ko": DOC_KO.get(cl["doc_type"], cl["doc_type"]),
             "confidence": cl.get("confidence", 0.0), "fields": fields,
             "text_len": len(text), "excerpt": text[:300], "text_full": text or "",
             "content_b64": base64.b64encode(raw).decode() if len(raw) <= UPLOAD_MAX_BYTES else None,
             "llm_error": cl.get("llm_error"),
             "content_type": _ctype(name)}
        if cl.get("field_sources"):
            d["fields"]["_sources"] = cl["field_sources"]
        if cl.get("llm_error"):
            d["fields"]["_llm_error"] = cl["llm_error"]
        docs.append(d)
        yield ("progress", {"done": i, "total": total, "file": d["filename"],
                            "doc_type_ko": d["doc_type_ko"]})
    found = {d["doc_type"] for d in docs if d["text_len"] and not
             any(d["fields"].get(k) for k in ("_empty", "_ocr_error", "_llm_error"))}
    checklist = [{"doc_type": dt, "doc_type_ko": DOC_KO[dt], "satisfied": dt in found,
                  "files": [d["filename"] for d in docs if d["doc_type"] == dt]}
                 for dt in REQUIRED_DOCS]
    missing = [c["doc_type_ko"] for c in checklist if not c["satisfied"]]
    skipped = getattr(files, "skipped", 0) + max(0, len(files) - limit)
    if skipped:
        log.warning("인테이크 대상 초과: %d개 건너뜀", skipped)
    yield ("result", {"file_count": len(files), "skipped_entries": skipped, "classified": docs, "checklist": checklist,
                      "missing": missing, "complete": len(missing) == 0,
                      "extracted": aggregate_fields(docs)})


def intake_zip(data):
    """동기 래퍼 — 진행 이벤트를 소진하고 최종 결과만 반환."""
    res = None
    for kind, payload in intake_zip_iter(data):
        if kind == "result":
            res = payload
    return res
