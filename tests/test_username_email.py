"""이메일 아이디 — v4 화면은 로그인·가입 모두 이메일을 아이디로 보낸다.

예전 규칙(영숫자 3~32자)은 '@' 를 막아 v4 의 인증기업 가입·스태프 가입 신청이 전부 422 였다.
이메일 형식이면 64자까지 받는다. 기존 영숫자 아이디 규칙은 그대로다.

실행: <venv>/bin/python -m pytest tests/test_username_email.py -q
"""
import logging
import os
import sys
import tempfile

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///" + tempfile.mktemp(suffix=".db"))
os.environ.setdefault("GLHAC_DEV", "1")
os.environ.setdefault("GLHAC_SECRET", "test-" + "x" * 36)
logging.disable(logging.CRITICAL)

from fastapi.testclient import TestClient  # noqa: E402

from app import schemas as S  # noqa: E402
from app.main import app  # noqa: E402


@pytest.mark.parametrize("good", ["ent1@test.kr", "a.b+tag@glhac.co.kr", "user_01@sub.example.com"])
def test_이메일_아이디는_가입과_스태프신청에서_통과한다(good):
    assert S.RegisterReq(username=good, password="halal2026").username == good
    assert S.StaffSignupReq(username=good, password="halal2026",
                            requested_role="consultant").username == good


@pytest.mark.parametrize("bad", ["a@b", "@test.kr", "user@", "user@@test.kr", "한글@test.kr",
                                 "user name@test.kr", "x" * 60 + "@test.kr"])
def test_이메일_형식이_아니거나_너무_길면_막는다(bad):
    with pytest.raises(ValidationError):
        S.RegisterReq(username=bad, password="halal2026")


@pytest.mark.parametrize("plain", ["admin", "buzzup2_1784165830"])
def test_기존_영숫자_아이디는_그대로_통과한다(plain):
    assert S.RegisterReq(username=plain, password="halal2026").username == plain


def test_v4_화면처럼_이메일로_가입하고_로그인한다():
    with TestClient(app) as c:
        r = c.post("/auth/register", json={"username": "apitest1@test.kr", "password": "EntPass!123",
                                           "company_name": "이메일테스트"})
        assert r.status_code == 200, r.text
        assert r.json()["role"] == "applicant"
        r = c.post("/auth/login", json={"username": "apitest1@test.kr", "password": "EntPass!123"})
        assert r.status_code == 200, r.text
        r = c.post("/auth/staff-signup", json={"username": "apicons1@test.kr", "password": "StaffPass!123",
                                               "requested_role": "consultant"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "pending"
