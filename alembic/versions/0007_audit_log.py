"""audit_log 테이블 — 조회/접근 감사(§2.4)

기존 Alembic DB(0006)에 신규 audit_log 테이블 추가. create_all(checkfirst) 이므로
이미 존재하면 무시(fresh 업그레이드 시 0001 baseline이 먼저 생성).

Revision ID: 0007_audit_log
Revises: 0006_user_token_version
Create Date: 2026-07-04
"""
from alembic import op

revision = "0007_audit_log"
down_revision = "0006_user_token_version"
branch_labels = None
depends_on = None

_TABLES = ("audit_log",)


def _tables():
    from app.db import Base
    import app.models  # noqa: F401 — 등록
    return [Base.metadata.tables[t] for t in _TABLES]


def upgrade():
    from app.db import Base
    Base.metadata.create_all(bind=op.get_bind(), tables=_tables())


def downgrade():
    from app.db import Base
    Base.metadata.drop_all(bind=op.get_bind(), tables=_tables())
