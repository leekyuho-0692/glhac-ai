"""app_user.token_version — 토큰 취소(§9.1)

기존 DB에 컬럼 추가. baseline(0001)이 이미 만든 fresh DB에선 idempotent 스킵.

Revision ID: 0006_user_token_version
Revises: 0005_p2c_tables
Create Date: 2026-07-04
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_user_token_version"
down_revision = "0005_p2c_tables"
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_bind())
    cols = [c["name"] for c in insp.get_columns("app_user")]
    if "token_version" not in cols:
        with op.batch_alter_table("app_user") as b:
            b.add_column(sa.Column("token_version", sa.Integer(), server_default="0"))


def downgrade():
    insp = sa.inspect(op.get_bind())
    cols = [c["name"] for c in insp.get_columns("app_user")]
    if "token_version" in cols:
        with op.batch_alter_table("app_user") as b:
            b.drop_column("token_version")
