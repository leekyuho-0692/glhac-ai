"""FK/제약 강화 (§6.1·§6.2) — case_id 등 핵심 관계에 외래키 + CASCADE

SQLite는 batch 모드(테이블 재생성)로, Postgres는 네이티브 ALTER로 적용.
빈 DB(신규)에서는 무손실. 기존 데이터가 있는 DB는 고아행 정리 후 적용 필요.

Revision ID: 0002_fk_constraints
Revises: 0001_baseline
Create Date: 2026-07-03
"""
from alembic import op  # noqa: F401

revision = "0002_fk_constraints"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

# (table, column, ref_table, ref_column) — 전부 case_application 참조(+ product_material 자식)
_CASE_CHILDREN = [
    "product", "material", "product_material", "generated_document", "notification",
    "change_impact", "workflow_event", "pendamping_assignment", "audit_finding",
    "lph_assignment", "external_identity", "halal_certificate", "fatwa_decision",
    "invoice", "hpas_evaluation", "document_asset", "sjph_evidence",
    "onsite_checklist", "auditor_pool", "discussion", "ai_extraction",
]
_EXTRA = [
    ("product_material", "product_id", "product", "product_id"),
    ("product_material", "material_id", "material", "material_id"),
    ("document_asset", "material_id", "material", "material_id"),
    ("document_asset", "product_id", "product", "product_id"),
]


def _fks():
    for t in _CASE_CHILDREN:
        yield (t, "case_id", "case_application", "case_id")
    for row in _EXTRA:
        yield row


def upgrade():
    for tbl, col, rt, rc in _fks():
        name = "fk_%s_%s" % (tbl, col)
        with op.batch_alter_table(tbl) as b:
            b.create_foreign_key(name, rt, [col], [rc], ondelete="CASCADE")


def downgrade():
    for tbl, col, rt, rc in _fks():
        name = "fk_%s_%s" % (tbl, col)
        with op.batch_alter_table(tbl) as b:
            b.drop_constraint(name, type_="foreignkey")
