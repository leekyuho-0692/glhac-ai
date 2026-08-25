"""[E1] 테스트 하네스 멱등화 — pytest 세션마다 신선한 격리 임시 DB.

문제: 각 테스트가 os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_v3_test.db")로
공유 DB를 써서 이전 실행의 잔존계정·상태가 남아 order-dependent 실패(비멱등).
해결: conftest는 테스트 모듈 import보다 먼저 로드되므로, 여기서 GLHAC_DB_URL을 세션 고유
임시 파일로 '강제' 설정 → 모든 모듈의 setdefault가 이를 사용 → 매 실행 신선 DB(멱등).
"""
import glob
import os
import shutil
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 레포 루트


def _purge_relative_test_dbs():
    """[E1-보강] 레포 루트에 누적되는 상대경로 테스트 DB(및 WAL/SHM) 잔존 제거.

    약 30개 테스트가 import 시 os.environ['GLHAC_DB_URL']='sqlite:///./glhac_XXX_test.db'
    로 상대·영속 DB를 강제한다(아래 conftest의 절대경로 tmp 강제를 우회). 그 결과 이전 실행의
    상태가 남아 순서/잔존 의존 플래키를 유발한다. conftest는 테스트 모듈 import보다 먼저 로드되므로
    여기서 상대 테스트 DB를 먼저 지우면 각 파일이 신선한 DB로 시작한다(파일별 격리 실행 기준 멱등).
    절대경로 tmp DB(아래 강제분)는 패턴에 걸리지 않아 무관.
    """
    pats = ("glhac_*_test.db", "glhac_*_test.db-wal", "glhac_*_test.db-shm",
            "glhac_test.db", "glhac_test.db-wal", "glhac_test.db-shm",
            "glhac_v3_test.db", "glhac_v3_test.db-wal", "glhac_v3_test.db-shm")
    for pat in pats:
        for f in glob.glob(os.path.join(_ROOT, pat)):
            try:
                os.remove(f)
            except OSError:
                pass


_purge_relative_test_dbs()   # 세션 시작(모듈 import 전) 정리

_TMPDIR = tempfile.mkdtemp(prefix="glhac_test_")
_DBFILE = os.path.join(_TMPDIR, "glhac_test.db")           # 절대경로
os.environ["GLHAC_DB_URL"] = "sqlite:///" + _DBFILE         # 절대경로 → sqlite:////abs (setdefault보다 우선)
os.environ.setdefault("GLHAC_DEV", "1")                     # 데모 계정 시드(테스트)
os.environ.setdefault("GLHAC_SECRET", "test-" + "x" * 36)   # 기본시크릿 부팅차단 회피


def app_db_file():
    """앱이 '실제로' 붙어 있는 SQLite 파일 경로.

    함정: 여러 테스트 파일이 import 시점에 os.environ["GLHAC_DB_URL"]을 자기 경로로
    덮어쓴다. 그러나 app/db.py의 engine은 최초 import 때 한 번만 만들어지므로, 두 번째
    파일부터는 환경변수만 바뀌고 앱은 여전히 첫 DB를 쓴다. 그 상태에서 환경변수로
    경로를 유도해 sqlite3로 직접 열면 존재하지 않는 파일을 열어 'no such table'이 난다
    (한 프로세스로 전체 실행할 때만 터지고, 파일별 실행에서는 숨는다).

    그래서 경로의 단일 출처는 환경변수가 아니라 엔진이다.
    """
    from app.db import engine
    return engine.url.database


def pytest_sessionfinish(session, exitstatus):
    """세션 종료 시 임시 DB + 레포에 남은 상대 테스트 DB 정리(레포 청결 유지)."""
    shutil.rmtree(_TMPDIR, ignore_errors=True)
    _purge_relative_test_dbs()
