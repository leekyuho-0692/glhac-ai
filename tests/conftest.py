"""[E1] 테스트 하네스 멱등화 — pytest 세션마다 신선한 격리 임시 DB.

문제: 각 테스트가 os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_v3_test.db")로
공유 DB를 써서 이전 실행의 잔존계정·상태가 남아 order-dependent 실패(비멱등).
해결: conftest는 테스트 모듈 import보다 먼저 로드되므로, 여기서 GLHAC_DB_URL을 세션 고유
임시 파일로 '강제' 설정 → 모든 모듈의 setdefault가 이를 사용 → 매 실행 신선 DB(멱등).
"""
import os
import shutil
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="glhac_test_")
_DBFILE = os.path.join(_TMPDIR, "glhac_test.db")           # 절대경로
os.environ["GLHAC_DB_URL"] = "sqlite:///" + _DBFILE         # 절대경로 → sqlite:////abs (setdefault보다 우선)
os.environ.setdefault("GLHAC_DEV", "1")                     # 데모 계정 시드(테스트)
os.environ.setdefault("GLHAC_SECRET", "test-" + "x" * 36)   # 기본시크릿 부팅차단 회피


def pytest_sessionfinish(session, exitstatus):
    """세션 종료 시 임시 DB 정리."""
    shutil.rmtree(_TMPDIR, ignore_errors=True)
