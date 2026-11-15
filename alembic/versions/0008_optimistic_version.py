"""Nullable write-service versions; NULL is version zero, without a backfill."""
from alembic import op
import sqlalchemy as sa

revision = "0008_optimistic_version"
down_revision = "b56e1b133c5a"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("case_application", "document_asset"):
        # create_all / 앱 시작 _migrate가 먼저 추가한 신규 설치에도 멱등이다.
        if "row_version" not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}:
            op.add_column(table, sa.Column("row_version", sa.Integer(), nullable=True))


def downgrade():
    for table in ("document_asset", "case_application"):
        if "row_version" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}:
            op.drop_column(table, "row_version")
