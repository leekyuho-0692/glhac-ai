"""FK 도구 테스트 공용 헬퍼 — 'FK 없던 옛 운영 DB' 를 모델에서 만든다.

모델의 CREATE TABLE 에서 FOREIGN KEY 절만 걷어내 만들므로 운영 DB 와 같은 상태(FK 없음 · 앱이 만든 인덱스 있음)다.
"""
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
os.environ.setdefault("GLHAC_DEV", "1")

from sqlalchemy.dialects import sqlite as _sqlite   # noqa: E402
from sqlalchemy.schema import CreateIndex, CreateTable   # noqa: E402

from app import models  # noqa: E402,F401
from app.db import Base  # noqa: E402

_FK_CLAUSE = re.compile(r",\s*FOREIGN KEY\([^)]*\) REFERENCES [^\s(]+ \([^)]*\)(?: ON DELETE (?:CASCADE|RESTRICT|SET NULL))?")


def legacy_db(path, extra_cols=None, relax=()):
    """운영 DB 와 같은 모양 — FK 없는 테이블 + 앱이 만든 인덱스."""
    d = _sqlite.dialect()
    c = sqlite3.connect(path)
    for t in Base.metadata.sorted_tables:
        ddl = _FK_CLAUSE.sub("", str(CreateTable(t).compile(dialect=d)))
        assert "FOREIGN KEY" not in ddl
        for rt, rc in relax:   # 옛 DB 는 _migrate 가 NULL 허용으로 추가한 컬럼 — NOT NULL 을 걷어낸다
            if rt == t.name:
                ddl, n = re.subn(r"(\n\t%s \S+) NOT NULL" % rc, r"\1", ddl)
                assert n == 1, (rt, rc)
        c.execute(ddl)
        for ix in t.indexes:
            c.execute(str(CreateIndex(ix).compile(dialect=d)))
    # 앱이 _ensure_indexes 로 만드는 종류(모델에 없는 인덱스) — 재생성 뒤에도 살아 있어야 한다
    c.execute("CREATE UNIQUE INDEX uq_product_material ON product_material (case_id, product_id, material_id)")
    c.execute("CREATE INDEX ix_material_case_id ON material (case_id)")
    for tbl, col, typ in (extra_cols or []):
        c.execute('ALTER TABLE %s ADD COLUMN "%s" %s' % (tbl, col, typ))
    c.commit()
    return c


def seed_clean(c):
    c.executescript("""
    insert into case_application (case_id, org_id, status, pathway, scheme) values ('c1','o1','onboarding','undetermined','product');
    insert into product (product_id, case_id, name) values ('p1','c1','제품1');
    insert into material (material_id, case_id, name) values ('m1','c1','설탕');
    insert into product_material (id, case_id, product_id, material_id) values ('pm1','c1','p1','m1');
    insert into document_asset (document_id, case_id, filename, material_id) values ('d1','c1','a.pdf','m1');
    insert into invoice (invoice_id, case_id) values ('i1','c1');
    insert into payment (id, invoice_id, case_id) values ('pay1','i1','c1');
    insert into workflow_event (event_id, case_id, action, actor_type) values ('w1','invite:ABC','x','system');
    """)
    c.commit()
