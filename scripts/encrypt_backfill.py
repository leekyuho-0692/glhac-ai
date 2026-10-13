#!/usr/bin/env python3
"""개인정보 컬럼 일괄 암호화 백필 — EncryptedType 컬럼 자동 발견·암호화.

사용법:
  python3 scripts/encrypt_backfill.py                           # dry-run(통계만)
  python3 scripts/encrypt_backfill.py --apply                   # 평문→암호화 적용
  python3 scripts/encrypt_backfill.py --verify                  # 남은 평문 수 확인

환경변수:
  GLHAC_DB_URL: sqlite:///./glhac.db (기본) 또는 Postgres 연결
  GLHAC_ENC_KEY: base64(44자) 또는 hex(64자) = 32바이트 키 (기본값: SECRET 파생 키)
  GLHAC_SECRET: 앱 시크릿 (암호화 키 파생용)
  GLHAC_ENCRYPTION: 이 스크립트는 무시(앱 ORM 회피 목적)
"""
import argparse
import base64
import logging
import os
import sys
from typing import Optional, List, Dict, Tuple

# 앱 디렉터리 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect, text, create_engine
from app.db import Base, DB_URL
from app.crypto import enc, dec, is_encrypted, ACTIVE_VER
import app.crypto as crypto_module

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("encrypt_backfill")


def _get_encrypted_columns() -> Dict:
    """Base의 모든 테이블에서 EncryptedType 컬럼 자동 발견.

    Returns:
      {table_name: {'encrypted_cols': [...], 'pk_cols': [...]}}
    """
    from app.crypto import EncryptedType
    from app import models  # 모델 import로 registry 채우기

    result = {}
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        table_mapper = inspect(cls)
        encrypted_cols = []
        pk_cols = []

        for col in table_mapper.columns:
            # EncryptedType인지 확인
            col_type = col.type
            if isinstance(col_type, EncryptedType):
                encrypted_cols.append(col.name)

            # PK인지 확인
            if col.primary_key:
                pk_cols.append(col.name)

        if encrypted_cols:
            result[table_mapper.class_.__tablename__] = {
                'encrypted_cols': encrypted_cols,
                'pk_cols': pk_cols,
            }

    return result


def _get_key_source() -> str:
    """현재 키 소스 판별 및 경고."""
    if os.environ.get("GLHAC_ENC_KEY"):
        return "GLHAC_ENC_KEY"
    else:
        log.warning(
            "[crypto] GLHAC_ENC_KEY 미설정 — 앱 SECRET에서 파생(데모 폴백). "
            "프로덕션은 GLHAC_ENC_KEY에 전용 키 설정 권장."
        )
        return "SECRET (파생)"


# 암호문 판별 — crypto.is_encrypted 와 같은 기준(앞 3글자가 키버전 접두).
# LIKE 'v%:%' 같은 느슨한 패턴은 "via: ..." 같은 평문을 암호문으로 오인해 검증을 통과시킨다.
_ENC_PREFIXES = ("v1:", "v2:", "v3:")


def _is_enc_sql(col_name):
    return "substr(%s, 1, 3) IN (%s)" % (col_name, ", ".join("'%s'" % p for p in _ENC_PREFIXES))


def _init_crypto_keys():
    """암호화 모듈 키 초기화."""
    # 지연 로딩 캐시 초기화
    crypto_module._KEYS = None
    # 키 로드 강제
    _ = crypto_module._keys()


def dry_run(encrypted_cols_map: Dict, db_url: str):
    """dry-run: 통계 출력만, 무변경."""
    log.info("=== DRY-RUN (무변경) ===")
    log.info(f"DB: {db_url}")
    log.info(f"키 소스: {_get_key_source()}")
    log.info("")
    log.info("테이블/컬럼별 통계:")
    log.info(f"{'테이블':<25} {'컬럼':<20} {'평문':>10} {'암호됨':>10} {'빈값':>10}")
    log.info("-" * 75)

    engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False} if db_url.startswith("sqlite") else {},
    )

    try:
        with engine.connect() as conn:
            total_plaintext = 0
            total_encrypted = 0
            total_empty = 0

            for table_name, info in encrypted_cols_map.items():
                encrypted_cols = info['encrypted_cols']

                for col_name in encrypted_cols:
                    # 비어있지 않은 행 수
                    query = text(
                        f"SELECT COUNT(*) FROM {table_name} "
                        f"WHERE {col_name} IS NULL OR {col_name} = ''"
                    )
                    empty_count = conn.execute(query).scalar()

                    nonempty = f"{col_name} IS NOT NULL AND {col_name} != ''"
                    encrypted_count = conn.execute(text(
                        f"SELECT COUNT(*) FROM {table_name} WHERE {nonempty} AND {_is_enc_sql(col_name)}"
                    )).scalar()
                    plaintext_count = conn.execute(text(
                        f"SELECT COUNT(*) FROM {table_name} WHERE {nonempty} AND NOT ({_is_enc_sql(col_name)})"
                    )).scalar()

                    if plaintext_count or encrypted_count:
                        log.info(
                            f"{table_name:<25} {col_name:<20} {plaintext_count:>10} "
                            f"{encrypted_count:>10} {empty_count:>10}"
                        )
                        total_plaintext += plaintext_count
                        total_encrypted += encrypted_count

                    total_empty += empty_count
    finally:
        engine.dispose()

    log.info("-" * 75)
    log.info(f"{'합계':<25} {'':20} {total_plaintext:>10} {total_encrypted:>10} {total_empty:>10}")


def apply_encryption(encrypted_cols_map: Dict, db_url: str, batch_size: int = 200) -> Tuple[bool, int, int]:
    """실제 암호화 적용 — 배치 커밋 (비교 후 갱신 - lost update 방지).

    Returns:
      (success, total_updated, total_skipped_conflict)
    """
    log.info("=== APPLY (암호화 적용) ===")
    log.info(f"DB: {db_url}")
    log.info(f"키 소스: {_get_key_source()}")
    log.info("")

    if os.environ.get("GLHAC_ENC_KEY") is None:
        log.warning(
            "[crypto] GLHAC_ENC_KEY 미설정 — 앱 SECRET에서 파생(데모 폴백). "
            "프로덕션은 GLHAC_ENC_KEY에 전용 키 설정 권장."
        )

    engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False} if db_url.startswith("sqlite") else {},
    )

    try:
        total_updated = 0
        total_skipped_conflict = 0

        for table_name, info in encrypted_cols_map.items():
            encrypted_cols = info['encrypted_cols']
            pk_cols = info['pk_cols']

            if not pk_cols:
                log.warning(f"  {table_name}: PK 없음, 건너뜀")
                continue

            pk_expr = ", ".join(pk_cols)

            for col_name in encrypted_cols:
                # 평문 행의 PK 만 먼저 모으고 읽기 연결은 닫는다 — 읽기 트랜잭션을 연 채로 다른
                # 연결에서 쓰면 WAL 이 아닌 SQLite 에서 'database is locked' 가 난다. 값은 배치마다 다시 읽는다.
                with engine.connect() as conn:
                    pks = [tuple(r) for r in conn.execute(text(
                        f"SELECT {pk_expr} FROM {table_name} "
                        f"WHERE {col_name} IS NOT NULL AND {col_name} != '' "
                        f"AND NOT ({_is_enc_sql(col_name)}) ORDER BY {pk_cols[0]}"))]

                col_updated = col_skipped = 0
                for i in range(0, len(pks), batch_size):
                    chunk = pks[i:i + batch_size]
                    batch = []
                    with engine.connect() as conn:
                        for pk_vals in chunk:
                            where = " AND ".join(f"{c} = :{c}" for c in pk_cols)
                            v = conn.execute(text(f"SELECT {col_name} FROM {table_name} WHERE {where}"),
                                             dict(zip(pk_cols, pk_vals))).scalar()
                            if v and not is_encrypted(v):
                                batch.append((pk_vals, v))
                    if not batch:
                        continue
                    updated, skipped = _commit_batch(engine, table_name, pk_cols, col_name, batch)
                    if updated is None:  # 왕복 검증 실패 등 — 그 배치는 롤백됐다
                        return False, total_updated, total_skipped_conflict
                    col_updated += updated
                    col_skipped += skipped

                total_updated += col_updated
                total_skipped_conflict += col_skipped
                if col_updated or col_skipped:
                    log.info(f"  {table_name}.{col_name}: {col_updated}행 암호화, {col_skipped}행 경합 건너뜀")

        log.info(
            f"총 업데이트: {total_updated}행, 경합 건너뜀: {total_skipped_conflict}행"
        )
        return True, total_updated, total_skipped_conflict

    finally:
        engine.dispose()


def _commit_batch(
    engine,
    table_name: str,
    pk_cols: List[str],
    col_name: str,
    batch: List,
) -> Tuple[int, int]:
    """배치 커밋 및 비교 후 갱신 — lost update 방지.

    Returns:
      (updated_count, skipped_conflict_count) 또는 에러 시 (None, None)
    """
    try:
        return _commit_batch_tx(engine, table_name, pk_cols, col_name, batch)
    except _BatchAbort as e:
        # engine.begin() 블록 안에서 return 하면 커밋된다 — 예외로 빠져나와야 배치 전체가 롤백된다.
        log.error("배치 롤백 %s.%s: %s", table_name, col_name, e)
        return None, None


class _BatchAbort(Exception):
    pass


def _commit_batch_tx(engine, table_name, pk_cols, col_name, batch):
    updated_count = 0
    skipped_conflict = 0

    with engine.begin() as conn:
        for pk_vals, col_val in batch:
            # enc() 호출
            encrypted = enc(col_val)

            # 검증: dec(enc(v)) == v
            decrypted = dec(encrypted)
            if decrypted != col_val:
                raise _BatchAbort("enc/dec 왕복이 일치하지 않음")

            # 비교 후 갱신 — WHERE 절에 원시 값 포함
            # UPDATE t SET col=:enc WHERE pk=:pk AND col=:old
            # rowcount 0 = 값이 바뀌었거나 행이 삭제됨 → 경합으로 건너뜀
            pk_where_parts = []
            params = {}
            for c, v in zip(pk_cols, pk_vals):
                pk_where_parts.append(f"{c} = :{c}")
                params[c] = v

            pk_where = " AND ".join(pk_where_parts)
            # WHERE 절에 col 원시 값도 포함 (lost update 방지)
            update_query = text(
                f"UPDATE {table_name} SET {col_name} = :enc "
                f"WHERE {pk_where} AND {col_name} = :old"
            )
            params["enc"] = encrypted
            params["old"] = col_val

            result = conn.execute(update_query, params)
            if result.rowcount == 1:
                updated_count += 1
            elif result.rowcount == 0:
                # 값이 바뀌었거나 행이 없음 → 경합으로 건너뜀 (다음 --apply에서 다시 시도)
                skipped_conflict += 1
            else:
                raise _BatchAbort(f"예상치 못한 rowcount {result.rowcount} (pk={pk_vals})")

    return updated_count, skipped_conflict


def verify(encrypted_cols_map: Dict, db_url: str) -> bool:
    """verify: 남은 평문 수만 세기."""
    log.info("=== VERIFY (검증) ===")
    log.info(f"DB: {db_url}")

    engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False} if db_url.startswith("sqlite") else {},
    )

    try:
        with engine.connect() as conn:
            total_plaintext = 0

            for table_name, info in encrypted_cols_map.items():
                encrypted_cols = info['encrypted_cols']

                for col_name in encrypted_cols:
                    query = text(
                        f"SELECT COUNT(*) FROM {table_name} "
                        f"WHERE {col_name} IS NOT NULL AND {col_name} != '' "
                        f"AND NOT ({_is_enc_sql(col_name)})"
                    )
                    count = conn.execute(query).scalar()

                    if count > 0:
                        log.warning(f"  {table_name}.{col_name}: 평문 {count}행")
                        total_plaintext += count

            if total_plaintext == 0:
                log.info("모든 컬럼이 암호화됨 ✓")
                return True
            else:
                log.error(f"남은 평문: {total_plaintext}행 ✗")
                return False

    finally:
        engine.dispose()


def main(argv: Optional[List[str]] = None):
    parser = argparse.ArgumentParser(description="개인정보 컬럼 암호화 백필")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="실제 암호화 적용 (기본: dry-run)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="남은 평문 수 확인 (exit 0 = 전부 암호화됨, 1 = 평문 남음)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=200,
        help="배치 크기 (기본: 200)",
    )
    parser.add_argument(
        "--allow-derived-key",
        action="store_true",
        help="GLHAC_ENC_KEY 없이 SECRET 파생 키로 --apply 허용(운영 비권장)",
    )

    args = parser.parse_args(argv)

    # 파생 키는 로그인 토큰 서명키(GLHAC_SECRET)를 바꾸는 순간 복호화가 깨진다 — 운영 데이터를
    # 그 키로 굳히지 않도록 명시 동의가 있어야만 진행한다(앱 부팅과는 무관한 스크립트 안전장치).
    if args.apply and not os.environ.get("GLHAC_ENC_KEY") and not args.allow_derived_key:
        log.error("GLHAC_ENC_KEY 가 없습니다. 전용 키를 설정하거나, 의도한 것이면 --allow-derived-key 를 함께 주세요.")
        return False

    # 암호화 모듈 키 초기화
    _init_crypto_keys()

    # EncryptedType 컬럼 자동 발견
    encrypted_cols_map = _get_encrypted_columns()

    if not encrypted_cols_map:
        log.info("EncryptedType 컬럼이 없습니다.")
        return True

    log.info("발견된 암호화 컬럼:")
    for table_name in sorted(encrypted_cols_map.keys()):
        info = encrypted_cols_map[table_name]
        log.info(f"  {table_name}: {', '.join(info['encrypted_cols'])}")
    log.info("")

    if args.apply:
        success, updated, skipped = apply_encryption(
            encrypted_cols_map,
            DB_URL,
            args.batch_size,
        )
        return success
    elif args.verify:
        return verify(encrypted_cols_map, DB_URL)
    else:
        dry_run(encrypted_cols_map, DB_URL)
        return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
