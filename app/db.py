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

def lock_chain_tail(db, key):
    """해시체인 '꼬리 조회 ~ 삽입 ~ 커밋'을 다른 세션(요청)과 직렬화한다 (MA4).

    왜 프로세스 내 threading.Lock 이 아닌가:
      record_event/_audit 은 같은 세션에서 flush 까지만 하고 커밋은 호출자가 한다. flush 직후 Lock 을
      풀면, 다른 세션은 아직 커밋되지 않은 꼬리를 볼 수 없어(SQLite WAL 은 커밋분만 보인다) 같은
      prev_hash 로 자식을 만들고, 앞 트랜잭션이 커밋된 뒤 그대로 삽입돼 체인이 갈라진다. Lock 을
      커밋까지 쥐면 호출자 코드가 락 안에서 끝나야 하는데 그럴 수 없고, 쥔 채 DB 쓰기 락을 기다리면
      (같은 요청이 두 번째 record_event 를 부를 때) 데드락이 된다.
    그래서 DB 의 쓰기 락을 '꼬리 조회 전에' 잡는다 — 락은 트랜잭션(커밋/롤백)까지 유지된다:
      · SQLite: BEGIN IMMEDIATE — 쓰기 락을 먼저 잡아 다른 세션의 BEGIN IMMEDIATE 는 busy_timeout 동안
        대기하고, 풀린 뒤에는 앞 세션이 커밋한 꼬리를 읽는다. pysqlite 는 SELECT 에서 트랜잭션을 열지
        않으므로(in_transaction=False) 이미 DML 이 있었다면(=이미 쓰기 락 보유) 건너뛴다.
      · PostgreSQL: pg_advisory_xact_lock(key) — 트랜잭션 종료 시 자동 해제(체인 단위 직렬화).
    key 는 체인 식별자(WorkflowEvent 는 'wf:<case_id>', 전역 AuditLog 는 'audit:global').
    """
    name = db.get_bind().dialect.name
    if name == "sqlite":
        conn = db.connection()
        raw = conn.connection.driver_connection
        if not raw.in_transaction:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
    elif name == "postgresql":
        from sqlalchemy import text
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})
