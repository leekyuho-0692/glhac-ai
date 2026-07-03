"""P2-C 테이블 — payment (§P2 billing/payment)

Revision ID: 0005_p2c_tables
Revises: 0004_p2b_tables
Create Date: 2026-07-03
"""
from alembic import op

revision = "0005_p2c_tables"
down_revision = "0004_p2b_tables"
branch_labels = None
depends_on = None

_TABLES = ("payment",)


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
