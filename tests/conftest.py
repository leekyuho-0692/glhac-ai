"""[E1] 테스트 하네스 멱등화 — pytest 세션마다 신선한 격리 임시 DB.

문제: 각 테스트가 os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_v3_test.db")로
공유 DB를 써서 이전 실행의 잔존계정·상태가 남아 order-dependent 실패(비멱등).
해결: conftest는 테스트 모듈 import보다 먼저 로드되므로, 여기서 GLHAC_DB_URL을 세션 고유
임시 파일로 '강제' 설정 → 모든 모듈의 setdefault가 이를 사용 → 매 실행 신선 DB(멱등).
"""
import glob
import os

import pytest
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


@pytest.fixture(autouse=True, scope="module")
def fresh_db_per_module():
    """테스트 '파일' 경계마다 앱 DB를 비운다 — 한 프로세스 실행을 파일별 실행과 같게.

    배경: 25개 파일이 import 시점에 GLHAC_DB_URL을 자기 경로로 덮어쓰지만 app/db.py의
    engine은 최초 import 때 한 번만 만들어진다. 즉 한 프로세스에서는 모든 파일이 '한' DB를
    공유하고, 앞 파일이 남긴 org_demo penyelia·케이스·일정이 다음 파일의 전제를 깬다.
    파일별 실행(run_isolated.sh)에서는 숨고 전체 실행·무작위 순서에서만 터졌다.

    파일 경계에서 테이블을 지우면 다음 파일의 첫 `with TestClient(app)`이 startup 훅에서
    create_all·_migrate·seed를 다시 돌려 신선한 상태로 시작한다(훅은 멱등).
    자기 엔진을 따로 만드는 테스트(tmp_path 등)는 이 엔진을 쓰지 않으므로 영향 없다.
    """
    from app.db import engine, Base
    import app.models  # noqa: F401  — Base.metadata 채우기
    import app.screening as _screening
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    # 온톨로지 캐시는 지워진 행을 붙들고 있다 — 아래 기동의 load_ontology가 다시 채운다.
    for _attr in ("_ONTOLOGY", "_ontology", "ONTOLOGY"):
        if hasattr(_screening, _attr):
            try:
                getattr(_screening, _attr).clear()
            except Exception:  # noqa: BLE001
                pass
    # 여기서 앱을 한 번 기동해 시드(데모 계정·메뉴·온톨로지)까지 끝내 둔다.
    # 여러 테스트가 첫 TestClient 진입 '전에' SessionLocal로 DB를 직접 읽는다
    # (예: 오디터 계정 조회) — 비운 채로 넘기면 그 테스트가 파일 첫 순서일 때만 죽는다.
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app):
        pass
    yield


def pytest_sessionfinish(session, exitstatus):
    """세션 종료 시 임시 DB + 레포에 남은 상대 테스트 DB 정리(레포 청결 유지)."""
    shutil.rmtree(_TMPDIR, ignore_errors=True)
    _purge_relative_test_dbs()
