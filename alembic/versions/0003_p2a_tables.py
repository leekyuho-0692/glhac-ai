"""P2-A 테이블 — audit_plan / fatwa_vote / corrective_action (§P2)

기존 Alembic DB(0002)에 신규 3개 테이블 추가. create_all(checkfirst) 이므로
이미 존재하면 무시(fresh 업그레이드 시 0001 baseline이 먼저 생성).

Revision ID: 0003_p2a_tables
Revises: 0002_fk_constraints
Create Date: 2026-07-03
"""
from alembic import op

revision = "0003_p2a_tables"
down_revision = "0002_fk_constraints"
branch_labels = None
depends_on = None

_TABLES = ("audit_plan", "fatwa_vote", "corrective_action")


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
