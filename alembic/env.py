"""Alembic 환경 — 앱과 동일한 GLHAC_DB_URL·Base.metadata 사용(§6.1)."""
import os
import sys
from logging.config import fileConfig

from alembic import context

# 프로젝트 루트를 sys.path에 추가(app 패키지 import)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import DB_URL, Base  # noqa: E402
import app.models  # noqa: F401,E402  — 모델을 Base.metadata에 등록

config = context.config
if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:  # noqa: BLE001
        pass

config.set_main_option("sqlalchemy.url", DB_URL)
target_metadata = Base.metadata
_is_sqlite = DB_URL.startswith("sqlite")


def run_migrations_offline():
    context.configure(url=DB_URL, target_metadata=target_metadata,
                      literal_binds=True, render_as_batch=_is_sqlite,
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    from sqlalchemy import engine_from_config, pool
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=_is_sqlite)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
