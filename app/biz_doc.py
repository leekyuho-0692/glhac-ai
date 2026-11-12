"""사업자등록증의 규칙 후보 추출. AI 결과 검산 및 AI 불가 시에만 사용."""

def _parse_biz_doc(text):
    """사업자/공장 등록증 텍스트 → 프로필 필드 추출 (Rizky #1).

    한국 양식만 알던 파서다. 실제 신청자는 인도네시아 업체이고 그쪽 서류는
    'Nama Perusahaan / Nomor Induk Berusaha / Nama Pimpinan / Alamat Perusahaan' 으로
    적힌다(실측: CV. CITRA PRATAMA 등록증). 인니 양식을 먼저 보고, 없으면 한국 양식을 본다.
    NIB는 13자리 숫자, 한국 사업자번호는 000-00-00000 로 서로 형태가 달라 섞이지 않는다."""
    import re
    t = text or ""
    def spaced(label):
        return r"\s*".join(re.escape(ch) for ch in label if not ch.isspace())

    # 한 줄로 합쳐진 OCR에서도 안내문 속 상호·대표자·주소를 항목으로 읽지 않는다.
    t = re.sub(r"※.*?(?=※|접\s*수\s*번\s*호|" + spaced("Receipt") + r"|$)",
               " ", t, flags=re.I | re.S)
    out = {}
    # ── 인도네시아 양식 ────────────────────────────────────────────────
    m = re.search(r"Nomor\s+Induk\s+Berusaha\s*[:：]?\s*(\d[\d\s-]{10,20})", t, re.I)
    if m:
        out["nib"] = re.sub(r"[^\d]", "", m.group(1))
    for pat, key in [(r"Nama\s+Perusahaan", "company_name"),
                     (r"Nama\s+Pimpinan", "responsible_person"),
                     (r"Nama\s+Penyelia\s+Halal", "halal_supervisor"),
                     (r"Alamat\s+Perusahaan", "address"),
                     (r"Alamat\s+Pabrik", "factory_address"),
                     (r"Jenis\s+Usaha", "business_type"),
                     (r"Skala\s+Usaha", "business_scale")]:
        mm = re.search(pat + r"\s*[:：]\s*([^\n]{1,120}?)(?=\s+(?:Nama|Nomor|Alamat|Jenis|Skala|Kode)\s|\n|$)",
                       t, re.I)
        if mm:
            v = mm.group(1).strip(" :：·|*)")
            if v and not out.get(key):
                out[key] = v
    # ── 한국 양식 ──────────────────────────────────────────────────────
    m = re.search(r"\d{3}-\d{2}-\d{5}", t)                       # 사업자등록번호
    if m and not out.get("nib"):
        out["nib"] = m.group(0)
    # 항목명은 글자 사이 공백을 허용하고, 값은 다음 한글·영문 라벨 앞에서 끊는다.
    labels = {
        "company_name": (r"(?:상\s*호|법\s*인\s*명)"
                         r"(?:\s*\(\s*(?:법\s*인\s*명|단\s*체\s*명|상\s*호)\s*\))?"
                         r"|단\s*체\s*명(?=\s*[:：])"),
        "factory_name": spaced("공장명"),
        "responsible_person": (r"성\s*명(?:\s*\(?\s*대\s*표\s*자\s*\)?)?|"
                               r"대\s*표\s*자(?:\s*\(\s*성\s*명\s*\))?"),
        "factory_address": r"공\s*장\s*(?:소\s*재\s*지|주\s*소)",
        "head_office_address": spaced("본점소재지"),
        "address": r"사\s*업\s*장\s*(?:소\s*재\s*지|주\s*소)|소\s*재\s*지|주\s*소|사\s*업\s*장",
        "business_type": r"업\s*태|업\s*종|종\s*목",
        "company_name_en": spaced("Name of Business"),
        "address_en": spaced("Business Address"),
    }
    stops = [spaced(label) for label in (
        "사업자등록번호", "사업자등록일", "사업자록일", "법인등록번호", "주민")]
    field_stops = [spaced(label) for label in (
        "개업연월일", "개업년월일", "개업일", "사업의종류", "발급사유", "공동사업자",
        "전자우편주소")]
    field_stops += [spaced("사업자단위과세") + r"(?:\s*" + spaced("적용사업자여부") + ")?",
                    spaced("전자세금계산서") + r"(?:\s*" + spaced("전용전자우편주소") + ")?"]
    other_stops = [spaced(label) for label in (
        "Name of", "Business", "Taxpayer", "Resident", "Type of", "Date of",
        "위와 같이", "접수번호", "Receipt")]
    # 긴 라벨을 먼저 소비해 내부의 '주소/소재지'를 별도 항목으로 읽지 않는다.
    pattern = "|".join(["(?P<stop>%s)" % "|".join(stops),
                       "(?P<field_stop>%s)" % "|".join(field_stops)] +
                       ["(?P<%s>%s)" % (key, pat) for key, pat in labels.items()] +
                       ["(?:%s)" % "|".join(other_stops)])
    # '도매업 종목'의 끝 글자부터 '업종'으로 붙여 읽지 않는다.
    matches = []
    for mm in re.finditer(r"(?<![가-힣A-Za-z0-9])(?:" + pattern + ")", t, re.I):
        if mm.lastgroup == "field_stop":
            tail = t[mm.end():]
            label = re.sub(r"\s+", "", mm.group())
            # 값도 줄바꿈될 수 있으므로 콜론이나 라벨 뒤의 구체 문맥으로만 구분한다.
            context = None
            if label in ("개업연월일", "개업년월일", "개업일"):
                context = r"\s*(?:\d{4}\s*(?:년|[./-])|" + spaced("Date of") + ")"
            elif label == "사업의종류":
                context = r"\s+(?:업\s*태|종\s*목)(?=\s|[:：])"
            elif label == "공동사업자":
                context = r"\s+" + spaced("Joint Business Owner") + r"(?![A-Za-z])"
            elif label == "발급사유":
                # 기존 콜론 없는 재발급 행은 보존하되 '발급사유 재발급 연구소'는 값이다.
                context = r"\s+" + spaced("재발급") + r"[ \t]*(?=\r?\n|$)"
            elif label.endswith("전자우편주소"):
                context = r"\s+[^\s@]+@[^\s@]+"
            if not (re.match(r"\s*[:：]", tail)
                    or (context and re.match(context, tail, re.I))):
                continue
        matches.append(mm)
    head_office_address = None
    for i, mm in enumerate(matches):
        key = mm.lastgroup
        if key not in labels or out.get(key):
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(t)
        value = t[mm.end():end].strip(" :：·|\r\n\t")
        if len(value) < 2 or re.search(r"효력|실체적|증명합니다|임의기재|※|\[|\]", value):
            continue
        if key in ("address", "head_office_address") and re.search(r"\S+@\S+", value):
            continue
        if key == "company_name_en":
            # 영문 상호 뒤 괄호 안 사업자번호는 회사명에 포함하지 않는다.
            value = re.sub(r"\s*\(\s*\d{3}\s*[-–—]\s*\d{2}\s*[-–—]\s*\d{5}\s*\)\s*$", "", value)
            if not re.fullmatch(r"[A-Z0-9&.,\-\s]{5,}", value):
                continue
        if key == "address_en" and (not re.search(r"[A-Za-z]", value) or re.search(r"[가-힣]", value)):
            continue
        if key == "head_office_address":
            head_office_address = head_office_address or value
        else:
            out[key] = value
    if not out.get("address") and head_office_address:
        out["address"] = head_office_address
    return out

