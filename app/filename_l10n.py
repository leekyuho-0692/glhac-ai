"""한글 문서 파일명 → 영문 표시명 (결정적 용어사전).

왜 LLM을 쓰지 않는가: 실측에서 qwen2.5가 성분명·제품명·공급사명을 틀렸다.
  타우린 → "Tauryin" (Taurine 아님) / 얌티 피치 x 샷 → "Yamtique Pitch x Shot"
  옥수수 수입서류 …(대상) → "(Target" — '대상'은 제조사 이름인데 '표적'으로 옮겼다.
할랄 서류에서 공급사·성분 이름이 틀리면 서류 자체가 무효가 된다. 그래서 정본에서 온
용어만 쓰고, 사전에 없는 한글은 지어내지 않고 원문을 남긴 뒤 미번역으로 표시한다.

용어 출처: Form 3(제품 국문/영문 대조표), 원재료 DB의 영문명, intake.DOC_EN(서류 종류).
"""
import re

# 긴 표현이 먼저 잡혀야 한다("원산지증명서"가 "원산지"보다 먼저).
_TERMS = [
    # ── 서류 종류 ────────────────────────────────────────────
    ("사업자등록증명원", "Business Registration Certificate"),
    ("사업자 등록증", "Business Registration"),
    ("사업자등록증", "Business Registration"),
    ("건강기능식품영업허가증", "Health Functional Food Business Permit"),
    ("식품제조가공업 영업등록증", "Food Manufacturing Business License"),
    ("공장등록증", "Factory Registration Certificate"),
    ("유기취급자인증서", "Organic Handler Certificate"),
    ("원산지증명서", "Certificate of Origin"),
    ("원산지증명원", "Certificate of Origin"),
    ("원산지확인서", "Certificate of Origin"),
    ("원산지 확인서", "Certificate of Origin"),
    ("원산지 설명서", "Origin Statement"),
    ("제조공정도", "Manufacturing Process Flow"),
    ("공정흐름도", "Process Flow"),
    ("공정도", "Process Flow"),
    ("고객 접수 양식", "Client Intake Form"),
    ("시설 데이터 양식", "Facility Data Form"),
    ("수입서류", "Import Documents"),
    ("시험 성적서", "Test Report"),
    ("성적서", "Test Report"),
    ("회사소개서", "Company Profile"),
    ("제안서", "Proposal"),
    ("영업허가증", "Business Permit"),
    ("영업등록증", "Business License"),
    ("명세서", "Specification"),
    ("설명서", "Statement"),
    ("확인서", "Confirmation"),
    ("증명원", "Certificate"),
    ("증명서", "Certificate"),
    ("인증서", "Certificate"),
    ("체크리스트", "Checklist"),
    ("보관 기록", "Storage Record"),
    ("구매 기록", "Purchase Record"),
    ("생산 기록", "Production Record"),
    ("목록표", "List"),
    ("양식", "Form"),
    ("기록", "Record"),
    ("목록", "List"),
    # ── 성분·원료 (원재료 DB 영문명 기준) ──────────────────
    ("비타민미네랄믹스", "VitaminMineralMix"),
    ("비타민B복합물", "Vitamin B Complex"),
    ("말토덱스트린", "Maltodextrin"),
    ("구연산칼륨", "Potassium Citrate"),
    ("블루베리농축분말", "Blueberry Concentrate Powder"),
    ("청포도농축분말", "Green Grape Concentrate Powder"),
    ("청포도향분말", "Green Grape Flavor Powder"),
    ("청사과향분말", "Green Apple Flavor Powder"),
    ("스테비텐 후레쉬", "Steviten Fresh"),
    ("타우린", "Taurine"),
    ("옥수수", "Corn"),
    ("고과당", "high-fructose"),
    ("아이스당", "ice sugar"),
    ("저감미당", "low-sweetness sugar"),
    ("물엿", "starch syrup"),
    ("가루엿", "powdered syrup"),
    # ── 제품 (Form 3 국문/영문 대조표) ─────────────────────
    ("얌티", "YUMTEA"),
    ("샤인머스캣", "Shine Muscat"),
    ("샤인머스켓", "Shine Muscat"),
    ("블루베리", "Blueberry"),
    ("라즈베리", "Raspberry"),
    ("오렌지", "Orange"),
    ("애플", "Apple"),
    ("망고", "Mango"),
    ("플럼", "Plum"),
    ("피치", "Peach"),
    ("리치", "Lychee"),
    ("레몬", "Lemon"),
    ("흑당", "Black Sugar"),
    ("샷", "Shot"),
    # ── 법인격 (업체 무관 일반어) ──────────────────────────
    ("주식회사", "Co., Ltd."),
    # 주의: 특정 회사명(바이오로제트·버즈업 등)은 여기 넣지 않는다. 그 업체에만 동작하고
    # 다음 신청자는 다시 한글로 남는다. 공급사명은 원재료 DB의 supplier 값으로 채운다.
    # ── 국가 ────────────────────────────────────────────────
    ("우크라이나", "Ukraine"),
    ("러시아", "Russia"),
    ("헝가리", "Hungary"),
    ("대한민국", "Korea"),
    ("중국", "China"),
    # ── 잡어 ────────────────────────────────────────────────
    ("대표자변경", "rep. change"),
    ("발급일자", "issued"),
    ("앞면", "front"),
    ("전체", "full"),
    ("영문", "EN"),
    ("원산지", "Origin"),      # 단독으로 쓰인 경우 — 위 합성어들이 먼저 걸린 뒤에 온다
]
_HANGUL = re.compile(r"[가-힣]")


def to_en(name):
    """(영문 표시명, 완전번역 여부). 사전에 없는 한글은 원문으로 남긴다(지어내지 않는다)."""
    if not name or not _HANGUL.search(name):
        return name, True          # 이미 영문/숫자 — 그대로가 정답
    # 한국어는 낱말을 붙여 쓰므로("타우린원산지증명서") 치환 시 공백을 넣어 두고 나중에 정리한다.
    out = " %s " % name
    for ko, en in _TERMS:
        if ko in out:
            out = out.replace(ko, " %s " % en)
    out = re.sub(r"\s+", " ", out)
    out = re.sub(r"\s+([)\]_,.])", r"\1", out)    # 닫는 기호·구두점 앞 공백 제거
    out = re.sub(r"([(\[])\s+", r"\1", out)       # 여는 기호 뒤 공백 제거
    out = re.sub(r"\s+-(?=\S)", "-", out)         # "…Mix -FS1" → "…Mix-FS1" (구분자 '-'는 보존)
    out = out.strip()
    return out, not bool(_HANGUL.search(out))


# ── 일반 용어 (법인격·직책) ──────────────────────────────────────────────
# 여기에는 '어느 업체에나 통하는 말'만 둔다. 특정 회사명·사람 이름을 코드에 박으면
# 그 업체에만 동작하고 다음 신청자는 다시 한글로 남는다. 고유명사는 데이터로 받는다
# (신청서의 '기업명(영문)' 등) — 값이 없으면 원문을 그대로 두고 지어내지 않는다.
_GENERIC = [
    # 법인격
    ("주식회사", "Co., Ltd."),
    ("(주)", "Co., Ltd."),
    ("㈜", "Co., Ltd."),
    ("유한회사", "Ltd."),
    ("합자회사", "LP"),
    # 직책 — 조직도·담당자 표기
    ("경영책임자", "Management Representative"),
    ("할랄감독자", "Halal Supervisor"),
    ("품질관리", "Quality Control"),
    ("생산부장", "Production Manager"),
    ("실무담당", "Coordinator"),
    ("대외연락", "Liaison"),
    ("부서 대표", "Team Member"),
    ("대표이사", "CEO"),
    ("생산", "Production"),
    ("구매", "Purchasing"),
    ("창고", "Warehouse"),
]


def generic_en(value):
    """법인격·직책 같은 일반 용어만 영문화. 고유명사는 건드리지 않는다."""
    if not value or not _HANGUL.search(value):
        return value
    out = " %s " % value
    for ko, en in _GENERIC:
        if ko in out:
            out = out.replace(ko, " %s " % en)
    out = re.sub(r"\s+", " ", out)
    out = re.sub(r"\s+([)\]_,.])", r"\1", out)
    return out.strip()


def entity(value, lang, value_en=None):
    """표시값 — ko면 원문. en·id면 (1) 입력된 영문명 (2) 일반 용어 치환 순으로 쓴다.
    영문명이 없으면 원문을 남긴다. 없는 이름을 만들어내면 서류가 틀린다."""
    if (lang or "ko").lower() == "ko":
        return value
    if value_en and str(value_en).strip():
        return str(value_en).strip()
    return generic_en(value)


def display(name, lang):
    """화면·보고서용 표시명 — ko면 원문 그대로, en·id면 영문 표시명.
    완전히 옮기지 못한 이름은 (원문 그대로 남은 부분이 있으므로) 그대로 보여준다."""
    if (lang or "ko").lower() == "ko":
        return name
    en, _full = to_en(name)
    return en
