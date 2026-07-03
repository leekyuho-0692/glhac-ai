"""P2-B 테이블 — integration_event / signature (§10.1·§6.4)

Revision ID: 0004_p2b_tables
Revises: 0003_p2a_tables
Create Date: 2026-07-03
"""
from alembic import op

revision = "0004_p2b_tables"
down_revision = "0003_p2a_tables"
branch_labels = None
depends_on = None

_TABLES = ("integration_event", "signature")


def _tables():
    from app.db import Base
    import app.models  # noqa: F401
    return [Base.metadata.tables[t] for t in _TABLES]


def upgrade():
    from app.db import Base
    Base.metadata.create_all(bind=op.get_bind(), tables=_tables())


def downgrade():
    from app.db import Base
    Base.metadata.drop_all(bind=op.get_bind(), tables=_tables())
