"""입력 검증 P2 — 품질·정합.

실데이터를 보고 규칙을 정했다:
  · 이메일: 'glhac@naver.com', 'ryeong@cheonu.kr'
  · 전화:   '+82-10-4928-1733'(붙임표 있음)와 '+821049281733'(없음)이 섞여 있었다
  · 인증서 날짜: 시스템 발급분은 계산값이라 멀쩡하다. 위험한 건 **AI 가 서류에서 읽어**
    status=active 인증서로 그대로 저장되는 경로다(_apply_typed_to_case). 거기서 들어온
    엉터리 날짜는 나중에 date.fromisoformat 으로 읽는 곳에서 죽는다.

실행: <venv>/bin/python -m pytest tests/test_input_validation_p2.py -q
"""
import os
import sys

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_val_p2_test.db")
os.environ.setdefault("GLHAC_DEV", "1")

from app import schemas as S  # noqa: E402


# ── ⑫ 이메일·전화 ────────────────────────────────────────────────────────
@pytest.mark.parametrize("ok", ["glhac@naver.com", "ryeong@cheonu.kr", "a.b+c@sub.example.co.id"])
def test_real_emails_pass(ok):
    assert S.CaseProfileReq(email=ok).email == ok


@pytest.mark.parametrize("bad", ["없는이메일", "a@b", "a b@c.com", "@x.com", "x@.com"])
def test_malformed_email_is_rejected(bad):
    with pytest.raises(ValidationError):
        S.CaseProfileReq(email=bad)


@pytest.mark.parametrize("given", ["+82-10-4928-1733", "+821049281733", "010-4928-1733"])
def test_phone_is_stored_in_one_shape(given):
    """같은 번호가 표기만 다르게 저장되면 대조·중복검사가 어긋난다."""
    v = S.CaseProfileReq(phone=given).phone
    assert v.startswith("+") and "-" not in v


def test_phone_normalisation_is_single_sourced():
    """검증(schemas)과 저장(main)이 다른 규칙을 쓰면 검증을 통과한 값이 다르게 저장된다."""
    from app import main as m
    for v in ("+82-10-4928-1733", "010-4928-1733", "0812345678"):
        assert m._normalize_phone(v) == S.normalize_phone(v)


@pytest.mark.parametrize("bad", ["123", "1", "+8210492817331234567"])
def test_absurd_phone_length_is_rejected(bad):
    with pytest.raises(ValidationError):
        S.CaseProfileReq(phone=bad)


# ── ⑪ 숫자 필드 ─────────────────────────────────────────────────────────
def test_numbers_with_thousand_separators_are_accepted():
    assert S.CaseProfileReq(profile_ext={"annual_revenue": "1,000,000"}).profile_ext[
        "annual_revenue"] == 1_000_000


@pytest.mark.parametrize("field,bad", [("annual_revenue", -1), ("outlet_count", -3),
                                       ("annual_revenue", "많음"), ("employee_count", "다수")])
def test_negative_or_unreadable_numbers_are_rejected(field, bad):
    """종전에는 int() 실패를 조용히 넘겨 값이 사라졌다 — 자기선언 자격이 이 숫자로 갈린다."""
    with pytest.raises(ValidationError):
        S.CaseProfileReq(profile_ext={field: bad})


def test_empty_number_is_still_allowed_as_unknown():
    assert S.CaseProfileReq(profile_ext={"outlet_count": ""}).profile_ext["outlet_count"] is None


# ── ⑬ AI 가 읽은 인증서 날짜는 관문을 거친다 ──────────────────────────────
@pytest.mark.parametrize("given,expect", [
    ("2026-08-04", "2026-08-04"), ("20260804", "2026-08-04"),
    ("2026-13-99", None), ("모름", None), ("", None), (None, None)])
def test_extracted_date_is_sanitised(given, expect):
    from app import main as m
    assert m._safe_date(given) == expect


def test_extracted_certificate_with_reversed_dates_keeps_no_dates():
    """만료가 발급보다 이르면 둘 다 버린다 — 지어낸 유효기간으로 '유효한 인증서'를 만들지 않는다."""
    from fastapi.testclient import TestClient

    from app import models
    from app.db import SessionLocal
    from app.main import app
    with TestClient(app) as c:
        tok = c.post("/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
        h = {"Authorization": "Bearer " + tok}
        cid = c.post("/cases", json={"company_name": "인증서날짜"}, headers=h).json()["case_id"]
        from app import main as m
        db = SessionLocal()
        case = db.get(models.CaseApplication, cid)
        m._process_doc(db, case, {"doc_type": "halal_certificate", "fields": {
            "cert_no": "REV-0001", "issue_date": "2030-01-01",
            "expiry_date": "2026-01-01"}}, {})
        db.commit()
        cert = db.query(models.HalalCertificate).filter_by(certificate_no="REV-0001").first()
        assert cert is not None
        assert cert.issue_date is None and cert.expiry_date is None
        db.close()


# ── ⑩ 제품명 중복 ────────────────────────────────────────────────────────
def test_duplicate_product_name_is_reported_not_blocked():
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        tok = c.post("/auth/login", json={"username": "consultant1",
                                          "password": "pw"}).json()["token"]
        h = {"Authorization": "Bearer " + tok}
        cid = c.post("/cases", json={"company_name": "제품중복"}, headers=h).json()["case_id"]
        assert c.post("/cases/%s/products" % cid, json={"name": "딸기잼"},
                      headers=h).status_code == 200
        # 막지 않는다 — 신규 업체는 남의 제품 사정을 알 수 없고, 같은 이름의 다른 규격을
        # 따로 올리는 일도 있다. 등록은 시키고 "이미 있습니다"라고 알려 준다.
        r = c.post("/cases/%s/products" % cid, json={"name": "  딸기잼 "}, headers=h)
        assert r.status_code == 200, r.text
        assert r.json().get("duplicate_of"), r.json()
        assert "이미 있습니다" in r.json().get("warning", "")
        # 다른 제품은 정상 등록
        assert c.post("/cases/%s/products" % cid, json={"name": "포도잼"},
                      headers=h).status_code == 200


# ── 검증이 화면에 닿는지 — 소스 수준으로 못 박는다 ────────────────────────
def test_no_screen_shows_a_bare_error_code():
    """오류 표시에 e.code 를 직접 쓰면 검증 사유가 화면에서 사라진다.

    422 의 detail 은 {code, field, message} 인데 code 만 찍으면 사용자는
    'VALIDATION_ERROR' 만 보게 된다. EMSG(e) 를 쓰라는 규약을 코드로 강제한다.

    이 규약을 손으로 지키려다 세 번 놓쳤다(변형 10종 · catch 변수명이 err 인 곳 등).
    비교 용도(e.code === 'X')는 정상이므로 제외한다."""
    import re

    html = open(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                             "app/static/index.html"), encoding="utf-8").read()
    # 표시 목적으로 code 를 꺼내 쓰는 형태: (X && X.code) 뒤에 비교연산자가 없는 것
    bad = []
    for m in re.finditer(r"\((\w+)&&\1\.code\)?(?!\s*[=!]==)", html):
        seg = html[m.start():m.start() + 90]
        if "EMSG" in seg:
            continue
        bad.append(seg.replace("\n", " ")[:70])
    assert not bad, "오류 코드를 그대로 표시하는 곳 %d곳: %s" % (len(bad), bad[:3])


def test_emsg_prefers_the_human_message():
    """EMSG 의 계약 — 검증 실패는 문장을, 그 밖에는 코드를 낸다."""
    import re

    html = open(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                             "app/static/index.html"), encoding="utf-8").read()
    m = re.search(r"function EMSG\(e\)\{(.*?)\n\}", html, re.S)
    assert m, "EMSG 헬퍼가 없다"
    body = m.group(1)
    assert "e.message" in body and "e.code" in body
    assert body.index("e.message") < body.index("e.code"), "message 가 code 보다 먼저여야 한다"


# ── 잔여 정리: 계정 아이디 · 주소 ────────────────────────────────────────
@pytest.mark.parametrize("bad,why", [
    ("ab", "너무 짧음"), ("my id", "공백"), ("한글아이디", "비ASCII"),
    ("_start", "기호로 시작"), ("x" * 40, "너무 김"), ("", "빈 값")])
def test_bad_username_is_rejected(bad, why):
    """아이디는 로그인 키이자 감사로그·알림에 찍히는 이름이다.

    공백이나 눈에 안 보이는 문자가 섞이면 '분명히 만들었는데 로그인이 안 되는' 상태가 된다."""
    with pytest.raises(ValidationError):
        S.RegisterReq(username=bad, password="halal2026")


def test_existing_account_names_all_still_pass():
    """규칙을 새로 걸 때는 기존 계정이 전부 통과하는지 먼저 확인한다."""
    for u in ("admin", "applicant1", "consultant1", "buzzup2_1784165830",
              "newco1785824124", "demoguide01", "pendamping1"):
        assert S.RegisterReq(username=u, password="halal2026").username == u


def test_admin_user_creation_uses_the_same_username_rule():
    with pytest.raises(ValidationError):
        S.AdminUserReq(username="a b", password="halal2026", role="auditor")


@pytest.mark.parametrize("addr", [
    "경기도 광주시 곤지암읍 신만로275-43, 대한민국",
    "37-6 Ucheonsaneopdanji-ro, Ucheon-myeon, Hoengseong-gun, Gangwon-do",
    "Jalan Kayu Putih Selatan III C No 24 RT 008 RW 005"])
def test_real_addresses_pass(addr):
    """주소 형식은 나라마다 달라 길이만 최소한으로 본다 — 엄격하면 멀쩡한 주소를 막는다."""
    assert S.CaseProfileReq(address=addr).address == addr


@pytest.mark.parametrize("bad", ["서울", "x", "짧은주소"])
def test_too_short_address_is_rejected(bad):
    """주소는 인증서·보고서에 인쇄된다 — 두 글자짜리 주소가 찍히면 그 문서가 못 쓴다."""
    with pytest.raises(ValidationError):
        S.CaseProfileReq(address=bad)


def test_blank_address_is_unset_not_an_error():
    """주소는 선택 항목이다 — 공백만 넣은 것은 '안 넣은 것'으로 본다(다른 선택 항목과 같은 규칙)."""
    assert S.CaseProfileReq(address="   ").address is None
    assert S.CaseProfileReq(address=None).address is None


def test_material_cert_no_is_deliberately_not_format_checked():
    """원재료 인증번호는 형식을 강제하지 않는다 — 그게 옳다.

    실데이터가 발급기관마다 전혀 다르다:
      398240000 · ID00410000500391022 · LPPOM-00230049860209 ·
      ARA-504254310625 · KAI.5986.12633.250019.CN · DSM.MAN.2504.5036.COL
    공통 형식이 없는데 규칙을 만들면 멀쩡한 남의 인증번호를 거부하게 된다.
    이 테스트는 '나중에 누가 형식 검증을 넣지 않게' 이유를 붙들어 두는 용도다."""
    for v in ("398240000", "ID00410000500391022", "LPPOM-00230049860209",
              "ARA-504254310625", "KAI.5986.12633.250019.CN", "DSM.MAN.2504.5036.COL"):
        assert S.MaterialCreate(name="X", cert_no=v).cert_no == v


def test_a_wrong_value_can_be_cleared():
    """한 번 잘못 들어간 값을 화면에서 지울 수 있어야 한다.

    실측: 사업자번호에 '123' 이 박힌 케이스는 그 칸을 비워도 DB 에 그대로 남았고,
    화면이 저장할 때 그 값을 다시 보내니 매번 422 로 막혔다 — 손쓸 수 없는 상태였다.
    인도네시아 업체는 한국 사업자등록번호가 없고 NIB 도 발급 전일 수 있어,
    비워 두는 것이 정상적인 상태다."""
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        tok = c.post("/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
        h = {"Authorization": "Bearer " + tok}
        cid = c.post("/cases", json={"company_name": "지움테스트"}, headers=h).json()["case_id"]
        c.patch("/cases/%s/profile" % cid,
                json={"nib": "1234567890", "responsible_person": "홍길동"}, headers=h)
        assert c.get("/cases/%s" % cid, headers=h).json()["nib"] == "1234567890"

        # 비워서 보내면 지워진다
        r = c.patch("/cases/%s/profile" % cid, json={"nib": ""}, headers=h)
        assert r.status_code == 200, r.text
        got = c.get("/cases/%s" % cid, headers=h).json()
        assert got["nib"] is None
        assert got["responsible_person"] == "홍길동", "안 보낸 필드가 지워졌다"


def test_fields_not_sent_are_left_alone():
    """보낸 적 없는 필드는 건드리지 않는다 — 부분 저장이 다른 값을 날리면 안 된다."""
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        tok = c.post("/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
        h = {"Authorization": "Bearer " + tok}
        cid = c.post("/cases", json={"company_name": "부분저장"}, headers=h).json()["case_id"]
        c.patch("/cases/%s/profile" % cid, json={
            "email": "a@b.com", "address": "서울특별시 강남구 테헤란로 1"}, headers=h)
        c.patch("/cases/%s/profile" % cid, json={"due_date": "2026-12-01"}, headers=h)
        got = c.get("/cases/%s" % cid, headers=h).json()
        assert got["email"] == "a@b.com" and got["address"].startswith("서울")
        assert got["due_date"] == "2026-12-01"
