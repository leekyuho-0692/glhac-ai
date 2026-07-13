import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# 프로젝트 루트를 path에 추가 → app.* import 가능
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.db import Base            # noqa: E402
from app import models            # noqa: E402,F401  (모든 모델을 metadata에 등록)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# 연결 URL은 GLHAC_DB_URL 환경변수 우선(런타임 엔진과 단일 소스). 없으면 alembic.ini 값.
_db_url = os.environ.get("GLHAC_DB_URL")
if _db_url:
    config.set_main_option("sqlalchemy.url", _db_url)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# autogenerate 대상 — 전체 모델 메타데이터
target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):
    """EncryptedType(암호화 컬럼)은 저장 레벨에서 String → 마이그레이션은 sa.String으로 렌더
    (마이그레이션이 앱 코드에 의존하지 않게, SQLite/Postgres 공통)."""
    from app.crypto import EncryptedType
    if type_ == "type" and isinstance(obj, EncryptedType):
        return "sa.String()"
    return False

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_item=render_item,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata,
            render_item=render_item,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
