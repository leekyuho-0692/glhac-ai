"""baseline — 현행 모델 전체 스키마 생성(§6.1 Alembic 채택 기준선)

기존 SQLite/create_all 스키마를 Alembic 관리로 전환하는 기준 리비전.
- 신규 DB: `alembic upgrade head` 로 전체 테이블 생성
- 기존 DB(이미 create_all로 생성됨): `alembic stamp 0001_baseline` 로 기준선 표시
이후 변경은 `alembic revision --autogenerate` 로 관리.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-07-03
"""
from alembic import op  # noqa: F401
import app.models  # noqa: F401  — 모델을 Base.metadata에 등록
from app.db import Base

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(bind=op.get_bind())


def downgrade():
    Base.metadata.drop_all(bind=op.get_bind())
