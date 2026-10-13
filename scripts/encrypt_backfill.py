#!/usr/bin/env python3
"""개인정보 컬럼 일괄 암호화 백필 — EncryptedType 컬럼 자동 발견·암호화.

사용법:
  python3 scripts/encrypt_backfill.py                           # dry-run(통계만)
  python3 scripts/encrypt_backfill.py --apply                   # 평문→암호화 적용
  python3 scripts/encrypt_backfill.py --apply --allow-derived-key  # SECRET 파생키로 적용
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
from typing import Optional, List, Dict

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

    engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False} if db_url.startswith("sqlite") else {},
    )

    try:
        with engine.connect() as conn:
            for table_name, info in encrypted_cols_map.items():
                encrypted_cols = info['encrypted_cols']

                for col_name in encrypted_cols:
                    # 비어있지 않은 행 수
                    query = text(
                        f"SELECT COUNT(*) FROM {table_name} "
                        f"WHERE {col_name} IS NOT NULL AND {col_name} != ''"
                    )
                    count = conn.execute(query).scalar()

                    if count > 0:
                        log.info(
                            f"  {table_name}.{col_name}: {count}행 (암호화 대상)"
                        )
    finally:
        engine.dispose()


def apply_encryption(encrypted_cols_map: Dict, db_url: str, allow_derived_key: bool, batch_size: int = 200):
    """실제 암호화 적용 — 배치 커밋."""
    log.info("=== APPLY (암호화 적용) ===")
    log.info(f"DB: {db_url}")
    log.info(f"키 소스: {_get_key_source()}")

    if os.environ.get("GLHAC_ENC_KEY") is None and not allow_derived_key:
        log.error(
            "ERROR: GLHAC_ENC_KEY가 없고 --allow-derived-key를 지정하지 않았습니다.\n"
            "SECRET 파생 키로 암호화하려면 --allow-derived-key를 함께 지정하세요.\n"
            "파생 키를 사용하면 로그인 토큰 서명키(SECRET)가 바뀌면 복호화가 깨집니다."
        )
        return False

    engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False} if db_url.startswith("sqlite") else {},
    )

    try:
        total_updated = 0

        for table_name, info in encrypted_cols_map.items():
            encrypted_cols = info['encrypted_cols']
            pk_cols = info['pk_cols']

            if not pk_cols:
                log.warning(f"  {table_name}: PK 없음, 건너뜀")
                continue

            pk_expr = ", ".join(pk_cols)

            for col_name in encrypted_cols:
                with engine.connect() as conn:
                    # 모든 평문 행을 읽기
                    query = text(
                        f"SELECT {pk_expr}, {col_name} FROM {table_name} "
                        f"WHERE {col_name} IS NOT NULL AND {col_name} != '' "
                        f"ORDER BY {pk_cols[0]}"
                    )
                    rows = list(conn.execute(query))

                    if not rows:
                        continue

                    # 배치 처리
                    batch = []
                    for row in rows:
                        pk_vals = row[:-1]
                        col_val = row[-1]

                        # 이미 암호화되어 있으면 건너뜀
                        if is_encrypted(col_val):
                            continue

                        batch.append((pk_vals, col_val))

                        if len(batch) >= batch_size:
                            if not _commit_batch(engine, table_name, pk_cols, col_name, batch):
                                return False
                            total_updated += len(batch)
                            batch = []

                    # 남은 배치 커밋
                    if batch:
                        if not _commit_batch(engine, table_name, pk_cols, col_name, batch):
                            return False
                        total_updated += len(batch)

                    if total_updated > 0:
                        log.info(f"  {table_name}.{col_name}: {total_updated}행 암호화 완료")

        log.info(f"총 업데이트: {total_updated}행")
        return True

    finally:
        engine.dispose()


def _commit_batch(engine, table_name: str, pk_cols: List[str], col_name: str, batch: List) -> bool:
    """배치 커밋 및 검증."""
    with engine.begin() as conn:
        for pk_vals, col_val in batch:
            # enc() 호출
            encrypted = enc(col_val)

            # 검증: dec(enc(v)) == v
            decrypted = dec(encrypted)
            if decrypted != col_val:
                log.error(
                    f"검증 실패 {table_name}.{col_name}: "
                    f"enc/dec 왕복이 일치하지 않음"
                )
                return False

            # 원시 UPDATE
            pk_where_parts = []
            params = {}
            for c, v in zip(pk_cols, pk_vals):
                pk_where_parts.append(f"{c} = :{c}")
                params[c] = v

            pk_where = " AND ".join(pk_where_parts)
            update_query = text(f"UPDATE {table_name} SET {col_name} = :enc WHERE {pk_where}")
            params["enc"] = encrypted

            conn.execute(update_query, params)

    return True


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
                    # 암호화된 값은 'v1:' 등 접두로 시작
                    # LIKE 'v%:%' 로 대략 판별
                    query = text(
                        f"SELECT COUNT(*) FROM {table_name} "
                        f"WHERE {col_name} IS NOT NULL AND {col_name} != '' "
                        f"AND {col_name} NOT LIKE 'v%:%'"
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
        "--allow-derived-key",
        action="store_true",
        help="SECRET 파생 키로 암호화 허용 (기본: GLHAC_ENC_KEY 필수)",
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

    args = parser.parse_args(argv)

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
        return apply_encryption(
            encrypted_cols_map,
            DB_URL,
            args.allow_derived_key,
            args.batch_size,
        )
    elif args.verify:
        return verify(encrypted_cols_map, DB_URL)
    else:
        dry_run(encrypted_cols_map, DB_URL)
        return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
