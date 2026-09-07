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
def test_duplicate_product_name_is_rejected():
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        tok = c.post("/auth/login", json={"username": "consultant1",
                                          "password": "pw"}).json()["token"]
        h = {"Authorization": "Bearer " + tok}
        cid = c.post("/cases", json={"company_name": "제품중복"}, headers=h).json()["case_id"]
        assert c.post("/cases/%s/products" % cid, json={"name": "딸기잼"},
                      headers=h).status_code == 200
        r = c.post("/cases/%s/products" % cid, json={"name": "  딸기잼 "}, headers=h)
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "PRODUCT_DUPLICATE"
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
