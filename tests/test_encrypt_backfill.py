"""encrypt_backfill.py 테스트 — 백필 스크립트 기능 검증."""
import base64
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# 임시 테스트 DB 설정 (암호화 OFF)
_TMPDIR = tempfile.mkdtemp(prefix="glhac_encrypt_test_")
_DBFILE = os.path.join(_TMPDIR, "test_encrypt.db")
os.environ["GLHAC_DB_URL"] = "sqlite:///" + _DBFILE
os.environ["GLHAC_ENCRYPTION"] = "0"  # 스크립트는 이 플래그를 무시하지만 테스트는 OFF로 시작
os.environ.setdefault("GLHAC_SECRET", "test-" + "x" * 36)

import pytest
from sqlalchemy import create_engine, Column, String, Integer, text
from sqlalchemy.orm import sessionmaker
from app.db import Base, SessionLocal, DB_URL
from app.crypto import EncryptedType, enc, dec
from scripts.encrypt_backfill import (
    _get_encrypted_columns,
    _init_crypto_keys,
    main as backfill_main,
)


# 테스트용 모델 (임시)
class TestCaseForEncryption(Base):
    __tablename__ = "test_case_for_encryption"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    nib = Column(EncryptedType)
    email = Column(EncryptedType)


def _init_test_db():
    """테스트 DB 초기화 및 테스트 모델 생성."""
    engine = create_engine(
        DB_URL,
        connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {},
    )
    Base.metadata.create_all(engine)
    return engine


def test_discover_encrypted_columns():
    """EncryptedType 컬럼 자동 발견."""
    engine = _init_test_db()

    encrypted_cols_map = _get_encrypted_columns()

    # CaseApplication과 다른 기존 모델들이 있어야 함
    assert "case_application" in encrypted_cols_map, "CaseApplication 테이블 없음"

    # CaseApplication에 예상되는 컬럼들
    cols = encrypted_cols_map["case_application"]["encrypted_cols"]
    expected = ["nib", "responsible_person", "halal_supervisor", "email", "phone", "address"]
    for exp in expected:
        assert exp in cols, f"{exp} 컬럼 미발견"

    engine.dispose()


def test_backfill_dry_run():
    """dry-run: 통계 출력, 무변경."""
    engine = _init_test_db()
    session = SessionLocal()

    # 테스트 데이터 삽입 (평문)
    test_case = TestCaseForEncryption(
        id=1,
        name="Test",
        nib="1234567890",
        email="test@example.com",
    )
    session.add(test_case)
    session.commit()
    session.close()

    # dry-run 실행
    _init_crypto_keys()
    result = backfill_main(["--batch-size=2"])

    assert result is True, "dry-run 실패"

    # DB 상태 검증: 평문 유지
    session = SessionLocal()
    case = session.query(TestCaseForEncryption).filter_by(id=1).first()
    assert case.nib == "1234567890", "dry-run 후 값이 변경됨"
    assert case.email == "test@example.com"
    session.close()

    engine.dispose()


def test_backfill_apply():
    """--apply: 평문→암호화 변환."""
    engine = _init_test_db()
    session = SessionLocal()

    # 테스트 데이터 삽입 (평문)
    for i in range(5):
        test_case = TestCaseForEncryption(
            id=100 + i,
            name=f"Case {i}",
            nib=f"NIB-{i:03d}",
            email=f"user{i}@example.com",
        )
        session.add(test_case)
    session.commit()
    session.close()

    # --apply 실행
    _init_crypto_keys()
    result = backfill_main(["--apply", "--allow-derived-key", "--batch-size=2"])

    assert result is True, "--apply 실패"

    # DB 상태 검증: 암호화됨
    session = SessionLocal()
    for i in range(5):
        case = session.query(TestCaseForEncryption).filter_by(id=100 + i).first()
        # ORM은 자동으로 dec()를 호출하므로 평문으로 읽힘
        assert case.nib == f"NIB-{i:03d}", f"case {i}: nib 복호화 실패"
        assert case.email == f"user{i}@example.com", f"case {i}: email 복호화 실패"

    # 원시 SQL로 읽으면 암호화됨 (v1: 접두)
    for i in range(5):
        query = text(
            f"SELECT nib, email FROM test_case_for_encryption WHERE id = {100 + i}"
        )
        row = session.execute(query).fetchone()
        assert row[0].startswith("v1:"), f"case {i}: nib이 암호화되지 않음"
        assert row[1].startswith("v1:"), f"case {i}: email이 암호화되지 않음"

    session.close()
    engine.dispose()


def test_backfill_idempotent():
    """두 번째 --apply는 0건(멱등성)."""
    engine = _init_test_db()
    session = SessionLocal()

    # 테스트 데이터 삽입
    test_case = TestCaseForEncryption(
        id=200,
        name="Idempotent",
        nib="IDEMPOTENT-NIB",
        email="idempotent@example.com",
    )
    session.add(test_case)
    session.commit()
    session.close()

    # 첫 번째 --apply
    _init_crypto_keys()
    result1 = backfill_main(["--apply", "--allow-derived-key"])
    assert result1 is True

    # 원시 SQL로 암호화 값 저장
    session = SessionLocal()
    query = text("SELECT nib FROM test_case_for_encryption WHERE id = 200")
    encrypted_val = session.execute(query).scalar()
    session.close()

    # 두 번째 --apply (아무것도 안 해야 함)
    result2 = backfill_main(["--apply", "--allow-derived-key"])
    assert result2 is True

    # 값이 같아야 함 (멱등성)
    session = SessionLocal()
    query = text("SELECT nib FROM test_case_for_encryption WHERE id = 200")
    encrypted_val2 = session.execute(query).scalar()
    assert encrypted_val == encrypted_val2, "멱등성 위반"
    session.close()

    engine.dispose()


def test_backfill_verify_encrypted():
    """--verify: 전부 암호화되면 exit 0."""
    engine = _init_test_db()
    session = SessionLocal()

    # 암호화된 데이터만
    test_case = TestCaseForEncryption(
        id=300,
        name="Verified",
        nib="VERIFIED-NIB",
        email="verified@example.com",
    )
    session.add(test_case)
    session.commit()
    session.close()

    # --apply로 암호화
    _init_crypto_keys()
    result = backfill_main(["--apply", "--allow-derived-key"])
    assert result is True

    # --verify 실행
    result = backfill_main(["--verify"])
    assert result is True, "--verify: 전부 암호화되었으나 실패"

    engine.dispose()


def test_backfill_verify_plaintext():
    """--verify: 평문이 남으면 exit 1."""
    engine = _init_test_db()
    session = SessionLocal()

    # 평문 데이터
    test_case = TestCaseForEncryption(
        id=400,
        name="Plaintext",
        nib="PLAINTEXT-NIB",
        email="plaintext@example.com",
    )
    session.add(test_case)
    session.commit()
    session.close()

    # --verify 실행 (암호화 안 함)
    _init_crypto_keys()
    result = backfill_main(["--verify"])
    assert result is False, "--verify: 평문이 있으나 true 반환"

    engine.dispose()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
