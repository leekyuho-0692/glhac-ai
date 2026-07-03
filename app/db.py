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
        cur.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()