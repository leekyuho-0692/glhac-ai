"""DB 세션/엔진 (M0). SQLite 시작 → 추후 Postgres 승격 (설계 D.1)."""
import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker, declarative_base

DB_URL = os.environ.get("GLHAC_DB_URL", "sqlite:///./glhac.db")
engine = create_engine(
    DB_URL,
    connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {},
)

# SQLite는 기본적으로 FK를 강제하지 않음 → 연결마다 PRAGMA로 활성화(§6.1).
# 모델(app/models.py)에 ForeignKey 가 선언돼 있어 create_all 로 만든 DB 도 FK 가 있다.
# 단 운영 DB 는 FK 없이 만들어진 테이블이라 scripts/fk_rebuild.py 로 재생성하기 전에는 PRAGMA 가 효과가 없다.
# GLHAC_SQLITE_FK=0 이면 끈다(되돌리기용 · 기본 "1").
_SQLITE_FK = os.environ.get("GLHAC_SQLITE_FK", "1").strip() != "0"
if DB_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_fk_pragma(dbapi_conn, _rec):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON" if _SQLITE_FK else "PRAGMA foreign_keys=OFF")
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


def lock_sqlite_writer(db):
    """실제 연결의 루트에서 확보한 SQLite writer 잠금만 재사용한다."""
    from fastapi import HTTPException
    from sqlalchemy.exc import OperationalError

    transaction = db.get_transaction() or db.begin()
    conn = db.connection()
    raw = conn.connection.driver_connection
    root = (transaction, conn.get_transaction())
    if db.info.get("sqlite_case_writer") == root and raw.in_transaction:
        return
    db.info.pop("sqlite_case_writer", None)
    try:
        if not raw.in_transaction:
            lock_chain_tail(db, "case-writer")
        else:
            # BEGIN/SELECT와 SAVEPOINT도 in_transaction=True다. 값은 바꾸지
            # 않는 DML로 writer로 승격해야 읽기 트랜잭션을 잠금으로 오인하지 않는다.
            conn.exec_driver_sql("UPDATE case_application SET case_id = case_id WHERE 0")
    except OperationalError as exc:
        db.rollback()
        if "locked" in str(exc).lower():
            raise HTTPException(409, {"code": "CONCURRENT_REQUEST"}) from exc
        raise
    # pysqlite의 최상위 SAVEPOINT는 RELEASE 시 실제 트랜잭션까지 끝낸다.
    # SAVEPOINT 안에서 확보한 writer는 루트 잠금의 근거로 남기지 않는다.
    if not db.in_nested_transaction() and not conn.in_nested_transaction():
        db.info["sqlite_case_writer"] = root


def lock_cases(db, case_ids):
    """SQLite는 단일 writer 잠금, 행 잠금 DB는 ID 정렬 → wf advisory → 행 잠금.

    identity map을 갱신하거나 flush하지 않는다. 이미 보유한 잠금은 재획득하지
    않으며 역순 확장은 SQL을 실행하기 전에 거절한다. 동적 집합은 아래의
    lock_case_set에서 쓰기 시작 전에 확정해야 한다.
    """
    from fastapi import HTTPException
    from sqlalchemy.exc import OperationalError
    from .models import CaseApplication

    ids = sorted(set(cid for cid in case_ids if cid))
    if not ids:
        return set(db.info.get("case_locks", (None, set()))[1])
    with db.no_autoflush:
        transaction = db.get_transaction() or db.begin()
        state = db.info.get("case_locks")
        if state is None or state[0] is not transaction:
            state = (transaction, set())
            db.info["case_locks"] = state
        held = state[1]
        owners = db.info.setdefault("case_lock_owners", {})
        if db.get_bind().dialect.name == "sqlite":
            lock_sqlite_writer(db)
            for cid in set(ids) - held:
                owners[cid] = db.get_nested_transaction() or transaction
            held.update(ids)
            return set(held)
        if set(ids) <= held:
            return set(held)
        # WorkflowEvent는 초대·심사자 프로필에도 사용된다. 케이스 행이 없는
        # 이벤트 체인은 케이스 집합 순서에 섞지 않는다(동일 helper로 직렬화).
        existing = {cid for cid, in db.query(CaseApplication.case_id).filter(CaseApplication.case_id.in_(ids))}
        chains = db.info.setdefault("event_chain_locks", set())
        new = sorted(set(ids) - held - chains)
        new_cases = sorted(set(new) & existing)
        # 행별 잠금을 쓰는 DB에서는 역순 확장을 SQL 실행 전에 차단한다.
        if new_cases and held and new_cases[0] < max(held):
            raise HTTPException(409, {"code": "CASE_LOCK_ORDER", "message": "케이스 잠금 집합을 다시 계산해야 합니다"})
        try:
            for cid in new:
                lock_chain_tail(db, "wf:" + cid)
                db.query(CaseApplication.case_id).filter_by(case_id=cid).with_for_update().all()
                (held if cid in existing else chains).add(cid)
                owners[cid] = db.get_nested_transaction() or transaction
        except OperationalError as exc:
            db.rollback()
            if "locked" in str(exc).lower() or getattr(exc.orig, "pgcode", None) in ("40P01", "40001", "55P03"):
                raise HTTPException(409, {"code": "CONCURRENT_REQUEST"}) from exc
            raise
    return set(held)


def lock_case_set(db, resolve_ids, *, attempts=3):
    """외부 작업 이후, 변경 이전에 동적 잠금 집합을 확정한다.

    대기 중 후보/연결이 바뀌어 집합이 커지면 추가 잠금 없이 전체 rollback 후
    합집합을 다시 정렬한다. 이미 변경을 시작한 호출자는 재시도할 수 없다.
    """
    from fastapi import HTTPException

    def rollback_for_retry():
        # flush 후 new/dirty/deleted는 비어도 쓰기 이력은 루트 트랜잭션에 남는다.
        # 아직 flush하지 않은 로컬 변경 역시 재시도로 조용히 버리지 않는다.
        started = db.info.get("transaction_write_started", False)
        pending = bool(db.new or db.dirty or db.deleted)
        db.rollback()
        if started or pending:
            raise HTTPException(409, {"code": "CONCURRENT_CHANGE_RETRY"})

    with db.no_autoflush:
        wanted = set(resolve_ids())
        for _ in range(attempts):
            held = db.info.get("case_locks", (None, set()))[1]
            if held and not wanted <= held:
                rollback_for_retry()
            lock_cases(db, wanted)
            latest = set(resolve_ids())
            if latest <= wanted:
                return wanted
            rollback_for_retry()
            wanted |= latest
    raise HTTPException(409, {"code": "CASE_LOCK_SET_CHANGED"})


@event.listens_for(Session, "after_flush")
def _remember_transaction_write(db, flush_context):
    db.info["transaction_write_started"] = True


@event.listens_for(Session, "do_orm_execute")
def _remember_bulk_write(execute_state):
    # Query.update/delete, Session.execute(update/delete)는 flush를 거치지 않는다.
    if execute_state.is_update or execute_state.is_delete:
        db = execute_state.session
        if db.get_transaction() is None:
            db.begin()
        db.info["transaction_write_started"] = True


@event.listens_for(Session, "after_transaction_end")
def _clear_case_locks(db, transaction):
    if transaction.parent is None:
        db.info.pop("case_locks", None)
        db.info.pop("event_chain_locks", None)
        db.info.pop("case_lock_owners", None)
        db.info.pop("sqlite_case_writer", None)
        db.info.pop("case_close_lineage", None)
        db.info.pop("transaction_write_started", None)


@event.listens_for(Session, "after_soft_rollback")
def _forget_savepoint_locks(db, previous):
    # PostgreSQL은 SAVEPOINT 이후 획득한 행/advisory 잠금도 그 SAVEPOINT의
    # rollback에서 해제한다. 바깥 트랜잭션의 잠금만 남기고 다시 획득하게 한다.
    owners = db.info.get("case_lock_owners", {})
    for cid, owner in list(owners.items()):
        while owner is not None and owner is not previous:
            owner = owner.parent
        if owner is previous:
            db.info.get("case_locks", (None, set()))[1].discard(cid)
            db.info.get("event_chain_locks", set()).discard(cid)
            del owners[cid]
