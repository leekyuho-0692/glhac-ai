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
    calls = []

    def parse(filename, raw):
        calls.append(filename)
        assert raw == b"%PDF-1.4\n"
        return text

    monkeypatch.setattr(intake, "parse_file", parse)
    r = client.post("/auth/ocr-extract", json={
        "image_b64": base64.b64encode(b"%PDF-1.4\n").decode(), "doc_type": "business_registration"})
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
