"""회원가입 등록증 자동 채우기 — 실제 한 줄 OCR의 띄어진 라벨과 안내문을 구분한다."""
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest                              # noqa: E402
from fastapi.testclient import TestClient   # noqa: E402

import app.main as m                        # noqa: E402
from app import intake                      # noqa: E402


# 운영 원본 OCR 문자열을 그대로 둔다 — 띄어쓰기·대시·오인식도 회귀 조건이다.
OCR_TEXT = "국세청 ( 1/ 1 ) [국세청민원사무처리규정 제6호 서식](2024.4.17.개정) 발급 번 호 사업자등록증명 처리기간 I s s u a n c e Num b e r Certificate of Business Registration Processing Time (일반과세자 즉 시 9656-773-2888-879 ( Individual VAT Taxpayer ) I m m e d i a t e l y 상 호 ( 법 인 명) 더마앤에스테틱코리아 N a m e o f B u s i n e s s DA K0REA (597-19–01669) 사 업 자 등록번호 597-19-01669 Business Taxpayer ID Number 성 명 (대 표 자) 김태호 Name o f Re pr e s en t at iv e 대 유 형 Theo Kim (850510-*******) 표 Ty p e o f Re p r e se n t at i on 주 민 ( 법 인  등 록 번 호 Re s i de n t ( C or p o r a t i o n ) 850510-******* R e g i s t r a t i o n N u m b e r 사 업 장 소 재 지 서울특별시 중구 퇴계로36나길6, 101동4층 401호(필동2가, 필동2가 젬스톤 헤리티지) B u s i n e s s A d d r e s s 6 Toegye-ro 36na-gil, Jung-gu, Seoul, Republic of Korea 개 업 일 2022년(Year) 11월(Month) 01일(Day) Da t e of Busine ss St a rt 사 업 자 록 일 2022년(Year ) 10월 (Month) 17일 (Day) Dat e of Business Registr ation 업 도매 및 소매업 B u s i n e s s T y p e Wholesale and retail trade 상품종합 도매업 종 목 Medical aesthetic goods, Medical devices, Cosmetics and etc B u s i n e s s I t e m 위와 같이 증명합니다 I certify that above information is true and correct ※위 내용은 발급일 현재 상황으로서 추후 변경될 수 있습니다 This information is true as of the issuance date ※영문 임의기재사항[상호 성명(대표자), 업태, 종목, 상세주소]은 실체적 권리관계를 증명하는 효력이 없습니다 Arbitrary data written in English [Name of Business, Name of Representat ive, Business Address, Business Type, and Business Item] do not hold a power to prove substant ive rights and responsibilities. 접 수 번 호 504885071284 Receipt No ."
ADDRESS = "서울특별시 중구 퇴계로36나길6, 101동4층 401호(필동2가, 필동2가 젬스톤 헤리티지)"
ADDRESS_EN = "6 Toegye-ro 36na-gil, Jung-gu, Seoul, Republic of Korea"
KOREAN_TEXT = ("상호 : 가나다식품 사업자등록번호 : 120-81-00000 대표자 : 홍길동 "
               "사업장소재지 : 서울시 중구 세종대로 10 개업일 : 2022년 11월 1일 업태 : 도매업 종목 : 식품")


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    monkeypatch.setattr(m.ai_local, "provider", lambda: "none")

    def unexpected(*args, **kw):
        pytest.fail("규칙으로 읽었거나 AI가 없는 문서에서 LLM을 호출했다")

    monkeypatch.setattr(m.ai_local, "llm_json", unexpected)


@pytest.fixture
def client(no_llm):
    with TestClient(m.app) as c:
        yield c


def _ocr(client, monkeypatch, text):
    import fitz
    with fitz.open() as document:
        document.new_page()
        pdf_bytes = document.tobytes()
    calls = []

    def parse(filename, raw):
        calls.append(filename)
        assert raw == pdf_bytes
        return text

    monkeypatch.setattr(intake, "parse_file", parse)
    r = client.post("/auth/ocr-extract", json={
        "image_b64": base64.b64encode(pdf_bytes).decode(), "doc_type": "business_registration"})
    assert r.status_code == 200, r.text
    assert calls == ["upload.pdf"]
    return r.json()


def test_실측_OCR의_상호_대표자_주소_사업자번호와_영문을_읽는다():
    fields = m._parse_biz_doc(OCR_TEXT)
    assert fields["company_name"] == "더마앤에스테틱코리아"
    assert fields["responsible_person"] == "김태호"
    assert fields["address"] == ADDRESS
    assert fields["nib"] == "597-19-01669"
    assert fields["company_name_en"] == "DA K0REA"
    assert fields["address_en"] == ADDRESS_EN
    assert all("효력" not in value and "실체적" not in value for value in fields.values())


def test_국문_전용_등록증도_다음_라벨_앞에서_끊고_영문_키를_만들지_않는다():
    fields = m._parse_biz_doc(KOREAN_TEXT)
    assert fields["company_name"] == "가나다식품"
    assert fields["responsible_person"] == "홍길동"
    assert fields["address"] == "서울시 중구 세종대로 10"
    assert fields["nib"] == "120-81-00000"
    assert fields["business_type"] == "도매업"
    assert "company_name_en" not in fields and "address_en" not in fields


@pytest.mark.parametrize("text, expected", [
    pytest.param(
        "국세청 사 업자등록증 (법인사업자) 등록번호:123-45-67890 "
        "법인명(단체명):주식회사하늘식품 대표 자:홍길동 "
        "개 업 연 월 일 :2019 년03 월14일 법인등록번호:110111-1234567 "
        "사업 장 소 재지 :가상시 누리구 샘플로 10, 201호,202호(가상동,샘플지식산업센터) "
        "본 점 소 재 지:가상시누리구 샘플로 20, 301호(가상동,가상본사센터) "
        "사업의 종류: 업태 제조업 종목 식품 발급사유: (별지 출력) "
        "사업자단위과세적용사업자여부:여()부(V) 전자세금계산서 전용 전자우편주소 : "
        "2021 년 09 월 17 일 가 상 세 무 서장",
        {"company_name": "주식회사하늘식품", "responsible_person": "홍길동",
         "address": "가상시 누리구 샘플로 10, 201호,202호(가상동,샘플지식산업센터)",
         "nib": "123-45-67890"},
        id="legacy-single-line"),
    pytest.param(
        "국세청\n사 업 자 등 록 증 (법 인 사 업 자)\n등 록 번 호 ： 234-56-78901\n"
        "상 호 ( 법 인 명 ) ： 주식회사누리상사\n대 표 자 ： 김 가 상\n"
        "개 업 년 월 일 ： 2020 년 04 월 15 일\n법 인 등 록 번 호 ： 220222-2345678\n"
        "사 업 장 소 재 지 ： 예시시 가상구 누리길 22, 302호(누리동,예시센터)\n"
        "본 점 소 재 지 ： 예시시 가상구 본사길 99, 901호(예시동,본사센터)\n"
        "사 업 의 종 류 ： 업 태 ： 도매업 종 목 ： 생활용품\n발 급 사 유 ： 재발급\n"
        "사 업 자 단 위 과 세 적 용 사 업 자 여 부 ： 여() 부(V)\n"
        "전 자 세 금 계 산 서 전 용 전 자 우 편 주 소 ：\n2023 년 05 월 16 일\n예 시 세 무 서장",
        {"company_name": "주식회사누리상사", "responsible_person": "김 가 상",
         "address": "예시시 가상구 누리길 22, 302호(누리동,예시센터)",
         "nib": "234-56-78901"},
        id="legacy-multiline"),
])
def test_구양식_법인사업자등록증의_보조라벨과_후속항목은_값에_남지_않는다(text, expected):
    # 신규 회귀 입력은 실측 OCR과 구조만 같은 가짜 회사·인명·주소·번호다.
    fields = m._parse_biz_doc(text)
    assert {key: fields[key] for key in expected} == expected
    for key in expected:
        value = "".join(fields[key].split())
        assert all(fragment not in value for fragment in ("단체명", "개업", "본점", ":", "："))


@pytest.mark.parametrize("label", [
    "법인명(단체명)", "법 인 명 ( 단 체 명 )", "상호(법인명)", "상 호 ( 법 인 명 )",
    "단체명", "단 체 명",
])
def test_회사명_보조라벨은_소비하고_값의_괄호는_보존한다(label):
    fields = m._parse_biz_doc(label + " : (주)가상식품 대표자 : 홍길동")
    assert fields["company_name"] == "(주)가상식품"
    assert fields["responsible_person"] == "홍길동"


@pytest.mark.parametrize("label", [
    "개업연월일", "개업년월일", "개업일", "법인등록번호", "본점소재지", "사업의종류",
    "발급사유", "사업자단위과세", "사업자단위과세적용사업자여부", "전자세금계산서",
    "전자세금계산서 전용 전자우편주소", "전자우편주소", "공동사업자",
])
@pytest.mark.parametrize("separator", ["", " "], ids=["compact", "spaced"])
def test_구양식_후속라벨에서_대표자와_주소를_끊는다(label, separator):
    stop = separator.join(label)
    fields = m._parse_biz_doc(
        "대표자 : 홍길동 " + stop + " : 후속정보 "
        "사업장 소재지 : 가상시 샘플로 10 " + stop + " : 후속정보")
    assert fields["responsible_person"] == "홍길동"
    assert fields["address"] == "가상시 샘플로 10"


def test_공백으로_나뉜_본점소재지가_앞에_있어도_사업장주소를_읽는다():
    fields = m._parse_biz_doc(
        "본 점 소 재 지 : 가상시 본사로 99 사업 장 소 재 지 : 가상시 샘플로 10 "
        "사업의 종류 : 업태 제조업")
    assert fields["address"] == "가상시 샘플로 10"


@pytest.mark.parametrize("label", ["본점소재지", "본점 소재지", "본 점 소 재 지"])
@pytest.mark.parametrize("tail", ["업태 : 도매업", "전자우편 주 소 : info@example.com"])
def test_본점주소만_있으면_주소로_사용하고_전자우편을_섞지_않는다(label, tail):
    fields = m._parse_biz_doc(label + " : 서울시 중구 세종대로 10 " + tail)
    assert fields["address"] == "서울시 중구 세종대로 10"
    assert "head_office_address" not in fields


@pytest.mark.parametrize("label", ["본점소재지", "본점 소재지", "본 점 소 재 지"])
def test_한줄_OCR의_콜론없는_본점주소를_항상_읽는다(label):
    fields = m._parse_biz_doc("사업자등록번호 123-45-67890 " + label + " 서울시 중구 세종대로 10")
    assert fields == {"nib": "123-45-67890", "address": "서울시 중구 세종대로 10"}


@pytest.mark.parametrize("head_first", [True, False], ids=["head-first", "business-first"])
@pytest.mark.parametrize("business_address", [
    "가상시 샘플로 10", "", "가", "효력이 없습니다", "info@example.com",
])
def test_등장순서와_무관하게_유효한_사업장주소가_본점보다_우선한다(head_first, business_address):
    parts = ["본점 소재지 : 가상시 본사로 99", "사업장 소재지 : " + business_address]
    if not head_first:
        parts.reverse()
    fields = m._parse_biz_doc(" ".join(parts) + " 전자우편 주소 : info@example.com")
    expected = business_address if business_address == "가상시 샘플로 10" else "가상시 본사로 99"
    assert fields == {"address": expected}


@pytest.mark.parametrize("label", [
    "전자우편 주소", "전 자 우 편 주 소", "전자세금계산서 전용 전자우편주소",
])
def test_전자우편주소만으로는_주소를_채우지_않는다(label):
    assert m._parse_biz_doc(label + " : info@example.com") == {}


@pytest.mark.parametrize("phrase", ["단체명의 통장 사본", "단체명부 열람 안내", "단체명칭 : 가상협회"])
def test_단체명으로_시작하는_일반문구가_뒤의_상호를_가리지_않는다(phrase):
    fields = m._parse_biz_doc(phrase + " 상호 : 가나다식품 대표자 : 홍길동")
    assert fields["company_name"] == "가나다식품"
    assert fields["responsible_person"] == "홍길동"
    assert "company_name" not in m._parse_biz_doc(phrase)


@pytest.mark.parametrize("separator", [":", " ：", "\t:"])
def test_단독_단체명은_콜론이_있을_때만_회사명_라벨이다(separator):
    assert m._parse_biz_doc("단 체 명" + separator + "가상협회") == {"company_name": "가상협회"}
    assert m._parse_biz_doc("단 체 명 가상협회") == {}


@pytest.mark.parametrize("label", [
    "공동사업자", "발급사유", "사업의종류", "사업자단위과세",
    "사업자단위과세적용사업자여부", "전자세금계산서", "전자세금계산서 전용 전자우편 주소",
    "개업연월일", "개업년월일", "개업일",
])
@pytest.mark.parametrize("template", ["가상 {} 협회", "가상 ({}) 협회", "가상 {}협회", "한국\n{} 협회"])
def test_값_안의_새_종료라벨은_구분_문맥이_없으면_보존한다(label, template):
    name = template.format(label)
    address = "가상시 누리로 10 (" + name + ")"
    fields = m._parse_biz_doc("상호 : " + name + " 대표자 : 홍길동 사업장소재지 : " + address)
    assert fields["company_name"] == name
    assert fields["address"] == address
    assert fields["responsible_person"] == "홍길동"


@pytest.mark.parametrize("text, expected", [
    pytest.param(
        "상호 : 한국\n공동사업자 협회\n대표자 : 홍길동",
        {"company_name": "한국\n공동사업자 협회", "responsible_person": "홍길동"},
        id="multiline-joint-owner-name"),
    pytest.param(
        "상호 : 한국\n발급사유 연구소\n대표자 : 홍길동",
        {"company_name": "한국\n발급사유 연구소", "responsible_person": "홍길동"},
        id="multiline-issuance-reason-name"),
    pytest.param(
        "상호 : 한국\n발급사유 재발급 연구소\n대표자 : 홍길동",
        {"company_name": "한국\n발급사유 재발급 연구소", "responsible_person": "홍길동"},
        id="issuance-reason-value-with-suffix"),
    pytest.param(
        "사업장소재지 : 서울시 중구 세종대로 10 (한국\n공동사업자 회관)\n업태 : 도매업",
        {"address": "서울시 중구 세종대로 10 (한국\n공동사업자 회관)", "business_type": "도매업"},
        id="multiline-joint-owner-address"),
    pytest.param(
        "사업장소재지 : 서울시 중구 세종대로 10 (한국\n발급사유 연구소)\n업태 : 도매업",
        {"address": "서울시 중구 세종대로 10 (한국\n발급사유 연구소)", "business_type": "도매업"},
        id="multiline-issuance-reason-address"),
])
def test_줄시작만으로_값_안의_문구를_종료라벨로_읽지_않는다(text, expected):
    assert m._parse_biz_doc(text) == expected


@pytest.mark.parametrize("label, value", [
    ("개 업 연 월 일", "2020년 4월 15일"), ("개업년월일", "2020.04.15"),
    ("개업일", "2020-04-15"), ("사업의 종류", "업태 제조업 종목 식품"),
    ("공 동 사 업 자", "Joint Business Owner 김가상"),
])
def test_콜론이_없어도_날짜나_다음_필드나_영문병기로_종료를_구분한다(label, value):
    fields = m._parse_biz_doc("사업장소재지 : 가상시 샘플로 10 " + label + " " + value)
    assert fields["address"] == "가상시 샘플로 10"


def test_콜론없는_본점주소와_재발급_사유를_읽는다():
    fields = m._parse_biz_doc(
        "대표자 : 홍길동\n본 점 소 재 지 서울시 중구 세종대로 10\n"
        "발 급 사 유 재발급\n전자우편 주소 info@example.com")
    assert fields == {"responsible_person": "홍길동", "address": "서울시 중구 세종대로 10"}


@pytest.mark.parametrize("label", ["개 업 일", "개 업 연 월 일", "개업년월일"])
@pytest.mark.parametrize("date_text", [
    "Date of Business Start 2022년 1월 1일",
    "D a t e o f B u s i n e s s S t a r t 2022년 1월 1일",
    "Date of Establishment 2022년 1월 1일", "date of business start 2022년 1월 1일",
    "DateofBusinessStart 2022년 1월 1일",
    ": 2022년 1월 1일", "：2022년 1월 1일",
])
def test_개업라벨_뒤_영문병기나_콜론도_영문주소를_종료한다(label, date_text):
    fields = m._parse_biz_doc("Business Address 10 Sample Road " + label + " " + date_text + " 업태 도매업")
    assert fields == {"address_en": "10 Sample Road", "business_type": "도매업"}


def test_값에_붙어있는_라벨_글자를_새_항목으로_읽지_않는다():
    fields = m._parse_biz_doc(
        "상호 : 가상대표자식품 대표자 : 홍길동 사업장소재지 : 가상시 본점로 10 "
        "사업의종류 : 업태 : 도매업 종목 : 식품")
    assert fields["company_name"] == "가상대표자식품"
    assert fields["address"] == "가상시 본점로 10"
    assert fields["business_type"] == "도매업"


@pytest.mark.parametrize("joint_owner", ["", "공 동 사 업 자 Joint Business Owner 김가상\n"])
@pytest.mark.parametrize("line_separator", ["\n", " "], ids=["table", "flattened-ocr"])
@pytest.mark.parametrize("start_date", [
    "2022년 06월 18일 Date of Business Start", "Date of Business Start 2022년 06월 18일",
], ids=["date-first", "english-first"])
def test_가짜_영문병기_표양식의_국문과_영문_필드를_유지한다(joint_owner, line_separator, start_date):
    text = (
        "사업자등록증명 Certificate of Business Registration\n"
        "상 호 ( 법 인 명 ) 주식회사샘플식품\nN a m e o f B u s i n e s s SAMPLE FOODS (345-67–89012)\n"
        "사 업 자 등 록 번 호 345-67-89012 Business Taxpayer ID Number\n"
        "성 명 ( 대 표 자 ) 박예시 Name of Representative\n"
        "주 민 ( 법 인 등 록 번 호 ) 330333-3456789 Resident (Corporation) Registration Number\n"
        "사 업 장 소 재 지 가상시 예시구 샘플길 30, 401호(샘플동,예시센터)\n"
        "B u s i n e s s A d d r e s s 30 Sample Road, Example City\n"
        + joint_owner +
        "개 업 일 " + start_date + "\n"
        "업 태 제조업 Business Type Manufacturing 종 목 식품 Business Item Food\n위와 같이 증명합니다")
    fields = m._parse_biz_doc(text.replace("\n", line_separator))
    assert fields == {
        "company_name": "주식회사샘플식품", "responsible_person": "박예시",
        "address": "가상시 예시구 샘플길 30, 401호(샘플동,예시센터)", "nib": "345-67-89012",
        "company_name_en": "SAMPLE FOODS", "address_en": "30 Sample Road, Example City",
        "business_type": "제조업",
    }


@pytest.mark.parametrize("end", ["접 수 번 호 123", "Receipt No. 123", ""])
def test_안내문에만_있는_항목명은_읽지_않는다(end):
    text = ("※변경될 수 있습니다 ※영문 임의기재사항[상호 성명(대표자), 업태, 종목, 상세주소]은 "
            "실체적 권리관계를 증명하는 효력이 없습니다 " + end)
    assert m._parse_biz_doc(text) == {}


@pytest.mark.parametrize("value", ["효력이 없습니다", "실체적 권리", "증명합니다", "임의기재", "[회사]", "가"])
def test_안내문_표현이나_너무_짧은_값은_버린다(value):
    fields = m._parse_biz_doc("상 호 : " + value + " 사 업 자 등 록 번 호 : 120-81-00000")
    assert "company_name" not in fields


@pytest.mark.parametrize("name", ["AB", "Mixed Case", "COMPANY★", "AB/CD"])
def test_영문_상호는_허용_문자와_길이를_확인한다(name):
    fields = m._parse_biz_doc("Name of Business " + name + " 사업자등록번호 120-81-00000")
    assert "company_name_en" not in fields


def test_인도네시아_등록증_추출은_유지한다():
    fields = m._parse_biz_doc("Nama Perusahaan : CV. CITRA PRATAMA\nNomor Induk Berusaha : 1234567890123\n"
                              "Nama Pimpinan : BUDI\nAlamat Perusahaan : Jalan Merdeka No. 1, Jakarta\n")
    assert fields["company_name"] == "CV. CITRA PRATAMA"
    assert fields["nib"] == "1234567890123"
    assert fields["responsible_person"] == "BUDI"
    assert fields["address"] == "Jalan Merdeka No. 1, Jakarta"


@pytest.mark.parametrize("provider", ["none", "ollama"])
def test_OCR_API가_실측_문자열의_filled와_missing을_알린다(client, monkeypatch, provider):
    monkeypatch.setattr(m.ai_local, "provider", lambda: provider)
    res = _ocr(client, monkeypatch, OCR_TEXT)
    assert res["ocr_available"] is True and res["source"] == "pdf_text"
    assert res["fields"] == m._parse_biz_doc(OCR_TEXT)
    assert res["missing"] == []
    assert set(res["filled"]) == set(res["fields"])
    assert {"company_name", "responsible_person", "address", "nib", "company_name_en"} <= set(res["filled"])


def test_OCR_API는_국문_전용이면_영문_미판독을_알린다(client, monkeypatch):
    res = _ocr(client, monkeypatch, KOREAN_TEXT)
    assert res["missing"] == ["company_name_en"]
    assert "company_name_en" not in res["fields"] and "company_name_en" not in res["filled"]
    assert {"company_name", "responsible_person", "address", "nib"} <= set(res["filled"])


def test_LLM은_누락값만_한번_보충하고_원문에_없는_값은_거부한다(client, monkeypatch):
    original = m._parse_biz_doc

    def partial(text):
        fields = original(text)
        for key in ("responsible_person", "address", "company_name_en"):
            fields.pop(key)
        return fields

    calls = []

    def supplement(system, text):
        calls.append((system, text))
        assert json.loads(system.split("요청 필드: ", 1)[1]) == {
            "responsible_person": None, "address": None, "company_name_en": None}
        assert text == OCR_TEXT
        return {"company_name": "기존값을 덮으면 안 됨", "responsible_person": "김 태호",
                "address": "서울특별시 강남구 원문에 없는 주소", "company_name_en": "DA K0REA"}

    monkeypatch.setattr(m, "_parse_biz_doc", partial)
    monkeypatch.setattr(m.ai_local, "provider", lambda: "ollama")
    monkeypatch.setattr(m.ai_local, "llm_json", supplement)
    res = _ocr(client, monkeypatch, OCR_TEXT)
    assert len(calls) == 1
    assert res["fields"]["company_name"] == "더마앤에스테틱코리아"
    assert res["fields"]["responsible_person"] == "김 태호"
    assert res["fields"]["company_name_en"] == "DA K0REA"
    assert "address" not in res["fields"] and "address" not in res["filled"]
    assert res["missing"] == ["address"]


@pytest.mark.parametrize("extra", [{"company_name_en": "MADE UP COMPANY"},
                                   {"company_name_en": None}, {"error": "timeout"}, None])
def test_LLM이_보충하지_못해도_규칙값과_누락_안내는_유지한다(client, monkeypatch, extra):
    calls = []

    def supplement(system, text):
        calls.append(system)
        return extra

    monkeypatch.setattr(m.ai_local, "provider", lambda: "ollama")
    monkeypatch.setattr(m.ai_local, "llm_json", supplement)
    res = _ocr(client, monkeypatch, KOREAN_TEXT)
    assert len(calls) == 1
    assert res["fields"] == m._parse_biz_doc(KOREAN_TEXT)
    assert res["missing"] == ["company_name_en"]
    assert set(res["filled"]) == set(res["fields"])


def test_빈_OCR_응답도_missing과_filled를_준다(client, monkeypatch):
    res = _ocr(client, monkeypatch, "")
    assert res["ocr_available"] is False
    assert res["fields"] == {} and res["filled"] == []
    assert set(res["missing"]) == {"company_name", "nib", "responsible_person", "address", "company_name_en"}
