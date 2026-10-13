"""헤드 병합 — 0007_audit_log 와 42fd1a346122_baseline_schema 를 하나의 head 로 (MA6)

두 계보(0001_baseline→…→0007_audit_log, 42fd1a346122 자동생성 baseline)가 각자 root/head 라
`alembic upgrade head` 가 'Multiple head revisions' 로 실패했다. 스키마 변경 없는 병합 리비전.

Revision ID: b56e1b133c5a
Revises: 0007_audit_log, 42fd1a346122
Create Date: 2026-10-13 12:47:12.539939
"""

revision = "b56e1b133c5a"
down_revision = ("0007_audit_log", "42fd1a346122")
branch_labels = None
depends_on = None


def upgrade():
    pass   # 병합만 — 스키마 변경 없음


def downgrade():
    pass
