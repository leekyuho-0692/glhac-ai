"""DB 세션/엔진 (M0). SQLite 시작 → 추후 Postgres 승격 (설계 D.1)."""
import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base

DB_URL = os.environ.get("GLHAC_DB_URL", "sqlite:///./glhac.db")
engine = create_engine(
    DB_URL,
    connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {},
)

# SQLite는 기본적으로 FK를 강제하지 않음 → PRAGMA로 활성화(§6.1).
# create_all DB엔 FK가 없어 무효, Alembic 적용 DB에서만 실제 강제.
if DB_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_fk_pragma(dbapi_conn, _rec):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        # 동시성: WAL = 읽기-쓰기 비차단(다중 심사자 동시작업 시 'database is locked' 완화).
        # busy_timeout으로 순간 경합은 대기, synchronous=NORMAL은 WAL에서 안전·고성능 기본값.
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()