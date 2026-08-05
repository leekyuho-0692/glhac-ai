"""테스트 대상 서버 URL 을 안전하게 해석하는 공용 모듈.

이 파일이 존재하는 이유 — 라이브 오염 2회:
  · 2026-07-12 (glhac_v3.db.bak-e2epollution-20260712151420)
  · 2026-08-04 (케이스 11건 + 가짜 인증서 4건, bak-e2eclean-20260805113415)
두 번 다 테스트 스크립트가 프로덕션(127.0.0.1:8800)을 직접 때려서 실사용 DB 가 오염됐다.
원인은 스크립트들이 8800 을 '기본값' 또는 하드코딩으로 들고 있었던 것 — 아무 것도 지정하지
않고 실행하면 라이브가 맞아버리는 구조였다. run_isolated.sh 에서 e2e 를 SKIP 하는 우회는
있었지만 위험한 기본값 자체는 남아 있었고, 그래서 재발했다.

그래서 여기서는 '지정하지 않으면 아무 일도 일어나지 않는다'를 규칙으로 삼는다:
  · GLHAC_E2E_BASE 미지정/빈 값 → 실행 거부(기본값으로 대체하지 않는다)
  · 프로덕션 포트(8800) 지정   → 실행 거부(GLHAC_E2E_ALLOW_PROD=1 명시 시에만 허용)

사용법:
    GLHAC_E2E_BASE=http://127.0.0.1:8899 python tests/e2e.py

스크립트 쪽:
    from _target import base
    B = base()             # -> "http://127.0.0.1:8899"
    BASE = base("/ui/")    # -> "http://127.0.0.1:8899/ui/"
"""

import os
import sys
from urllib.parse import urlparse

PROD_PORT = 8800


def base(path: str = "") -> str:
    """테스트 대상 서버 URL 을 해석해 반환. 안전하지 않으면 SystemExit(2) 로 중단한다."""
    raw = (os.environ.get("GLHAC_E2E_BASE") or "").strip()
    if not raw:
        sys.stderr.write(
            "오류: GLHAC_E2E_BASE 환경변수가 지정되지 않았습니다.\n"
            "테스트가 라이브 서버를 때리는 사고를 막기 위해 대상 서버를 반드시 명시해야 합니다.\n"
            "\n"
            "격리 테스트 서버를 띄운 뒤 대상을 지정해 실행하세요:\n"
            "  GLHAC_E2E_BASE=http://127.0.0.1:8899 python tests/e2e.py\n"
        )
        raise SystemExit(2)

    # 스킴이 없으면 urlparse 가 netloc 을 비우고 path 로 넘겨 .port 가 None 이 된다
    # ('127.0.0.1:8800' 이 프로덕션 판정을 통과해버림). 판정 전에 정규화한다.
    url = raw if "://" in raw else "http://" + raw

    try:
        port = urlparse(url).port
    except ValueError:
        sys.stderr.write(
            "오류: GLHAC_E2E_BASE 의 URL 형식이 올바르지 않습니다: %s\n"
            "예: http://127.0.0.1:8899\n" % raw
        )
        raise SystemExit(2)

    if port == PROD_PORT:
        if os.environ.get("GLHAC_E2E_ALLOW_PROD") != "1":
            sys.stderr.write(
                "오류: 대상이 프로덕션 포트(%d)입니다 — 실사용 데이터가 있는 라이브 서버입니다.\n"
                "격리 테스트 서버를 사용하세요:\n"
                "  GLHAC_E2E_BASE=http://127.0.0.1:8899 python tests/e2e.py\n"
                "\n"
                "정말 라이브를 대상으로 실행해야 한다면 명시적으로 허용해야 합니다:\n"
                "  GLHAC_E2E_ALLOW_PROD=1 GLHAC_E2E_BASE=%s python tests/e2e.py\n"
                % (PROD_PORT, raw)
            )
            raise SystemExit(2)
        sys.stderr.write(
            "경고: GLHAC_E2E_ALLOW_PROD=1 — 프로덕션(%d)을 대상으로 실행합니다. "
            "실사용 데이터가 오염될 수 있습니다.\n" % PROD_PORT
        )

    return url.rstrip("/") + path


def db_path() -> str:
    """테스트가 직접 열 SQLite 파일의 절대경로를 반환한다.

    일부 시나리오(e2e_s8/s10/s12/s13)는 API 만으로 만들기 어려운 전제 상태를
    DB 직접 UPDATE 로 만든다(예: renew 테스트를 위한 certificate_issued 강제).
    종전엔 DB_PATH="glhac.db" 상대경로 하드코딩이라 서버가 다른 DB 로 뜨면
    'no such table' 로 실패했다. 서버가 쓰는 GLHAC_DB_URL 을 테스트도 그대로 읽어
    단일 출처로 삼는다 — 러너·CI 가 서버와 같은 값을 테스트 프로세스에도 넘긴다.
    """
    raw = (os.environ.get("GLHAC_DB_URL") or "").strip()
    if not raw:
        sys.stderr.write(
            "오류: GLHAC_DB_URL 환경변수가 지정되지 않았습니다.\n"
            "이 테스트는 서버와 '같은' DB 를 직접 열어야 하므로 서버와 동일한 값이 필요합니다.\n"
            "  GLHAC_DB_URL=sqlite:////tmp/t.db GLHAC_E2E_BASE=http://127.0.0.1:8899 python tests/e2e_s8.py\n"
        )
        raise SystemExit(2)
    if not raw.startswith("sqlite:///"):
        sys.stderr.write(
            "오류: DB 직접 조작은 SQLite 에서만 지원합니다(GLHAC_DB_URL=%s).\n"
            "파일 기반 sqlite URL 로 서버를 띄우세요: sqlite:////절대경로 또는 sqlite:///상대경로\n" % raw
        )
        raise SystemExit(2)

    # sqlite:///상대경로 · sqlite:////절대경로 — 접두사를 떼면 나머지가 곧 경로다.
    path = raw[len("sqlite:///"):]
    if "?" in path:                      # sqlite:///x.db?check_same_thread=False
        path = path.split("?", 1)[0]
    if path in ("", ":memory:"):
        sys.stderr.write(
            "오류: 메모리 DB(sqlite:///:memory:)는 프로세스 간에 공유되지 않습니다.\n"
            "별도 프로세스인 테스트가 서버의 메모리 DB 를 열 수 없으므로, 파일 기반 sqlite 로 서버를 띄우세요.\n"
        )
        raise SystemExit(2)

    # 상대경로면 테스트 프로세스의 cwd 기준이다 — 어디를 찾았는지 드러나도록 절대경로로 정규화한다.
    path = os.path.abspath(path)
    if not os.path.exists(path):
        sys.stderr.write(
            "오류: DB 파일이 없습니다: %s\n"
            "서버와 다른 DB 를 보고 있을 수 있습니다. 서버에 준 GLHAC_DB_URL 과 같은 값인지 확인하세요.\n" % path
        )
        raise SystemExit(2)
    return path
