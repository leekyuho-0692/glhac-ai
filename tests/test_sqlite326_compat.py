"""운영 SQLite(3.26) 호환 — NULLS FIRST/LAST 는 3.30 부터라 운영에서 문법 오류(500)가 난다.

실측: 10/21 운영 로그 'near "NULLS": syntax error'(_case_fatwa_no), 10/28 v4-ux 의 동반자 배정
정렬 6곳도 같은 구문. 로컬 SQLite 는 최신이라 다른 테스트로는 안 잡힌다.
SQLite 는 DESC 정렬에서 NULL 을 원래 맨 뒤에 두므로 .desc() 만으로 같은 순서다.
"""
import os
import re

APP = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app")


def test_no_nulls_first_last_in_app_code():
    bad = []
    for name in sorted(os.listdir(APP)):
        if name.endswith(".py"):
            src = open(os.path.join(APP, name), encoding="utf-8").read()
            for i, line in enumerate(src.splitlines(), 1):
                if re.search(r"nulls_?(first|last)\(|NULLS\s+(FIRST|LAST)", line, re.I):
                    bad.append("%s:%d" % (name, i))
    assert not bad, bad
