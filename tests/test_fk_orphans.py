"""R2-3 — scripts/fk_orphans.py: 고아 점검·정리. 실제 SQLite 임시 파일에서 실제 main() 을 부른다(목 없음)."""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _fk_legacy import legacy_db as _legacy_db, seed_clean as _seed_clean   # noqa: E402,F401
import fk_orphans  # noqa: E402


def test_고아_보고는_읽기전용이고_정책별로_분류한다(tmp_path, capsys):
    p = str(tmp_path / "a.db")
    c = _legacy_db(p)
    _seed_clean(c)
    c.executescript("""
    insert into product (product_id, case_id, name) values ('px','GONE','고아제품');
    insert into product_material (id, case_id, product_id, material_id) values ('pmx','c1','px','m1');
    update document_asset set material_id='MGONE' where document_id='d1';
    """)
    c.commit()
    before = c.execute("select count(*) from product").fetchone()[0]
    c.close()
    assert fk_orphans.main(["--db", p]) == fk_orphans.EXIT_CLEANABLE
    out = capsys.readouterr().out
    assert "product" in out and "document_asset" in out
    c = sqlite3.connect(p)
    assert c.execute("select count(*) from product").fetchone()[0] == before   # 보고는 안 지운다


def test_apply는_CASCADE삭제_SETNULL_연쇄까지_정리한다(tmp_path):
    p = str(tmp_path / "a.db")
    c = _legacy_db(p)
    _seed_clean(c)
    c.executescript("""
    insert into product (product_id, case_id, name) values ('px','GONE','고아제품');
    insert into product_material (id, case_id, product_id, material_id) values ('pmx','c1','px','m1');
    update document_asset set material_id='MGONE' where document_id='d1';
    """)
    c.commit()
    c.close()
    assert fk_orphans.main(["--db", p, "--apply"]) == fk_orphans.EXIT_OK
    c = sqlite3.connect(p)
    assert c.execute("select count(*) from product where product_id='px'").fetchone()[0] == 0     # CASCADE 삭제
    assert c.execute("select count(*) from product_material where id='pmx'").fetchone()[0] == 0   # 연쇄(고아 제품의 연결)
    assert c.execute("select material_id from document_asset where document_id='d1'").fetchone()[0] is None  # SET NULL
    assert c.execute("select count(*) from product where product_id='p1'").fetchone()[0] == 1     # 정상 행은 그대로
    assert c.execute("select count(*) from product_material where id='pm1'").fetchone()[0] == 1
    assert fk_orphans.main(["--db", p]) == fk_orphans.EXIT_OK


def test_감사법적기록_고아는_지우지_않고_실패한다(tmp_path, capsys):
    p = str(tmp_path / "a.db")
    c = _legacy_db(p)
    _seed_clean(c)
    c.executescript("""
    insert into contract (contract_id, case_id) values ('k1','GONE');
    insert into product (product_id, case_id, name) values ('px','GONE','고아제품');
    """)
    c.commit()
    c.close()
    assert fk_orphans.main(["--db", p]) == fk_orphans.EXIT_RESTRICT
    assert fk_orphans.main(["--db", p, "--apply"]) == fk_orphans.EXIT_RESTRICT
    out = capsys.readouterr().out
    assert "k1" in out                                   # 사람이 판단할 목록에 PK 가 나온다
    c = sqlite3.connect(p)
    assert c.execute("select count(*) from contract").fetchone()[0] == 1       # 계약은 그대로
    assert c.execute("select count(*) from product where product_id='px'").fetchone()[0] == 1  # 아무것도 안 바뀜


def test_워크플로이벤트_감사로그의_의사키는_고아로_보지_않는다(tmp_path):
    p = str(tmp_path / "a.db")
    c = _legacy_db(p)
    _seed_clean(c)    # workflow_event.case_id = 'invite:ABC'
    c.execute("insert into audit_log (id, case_id, action) values ('l1','삭제된케이스','case.purge')")
    c.commit()
    c.close()
    assert fk_orphans.main(["--db", p]) == fk_orphans.EXIT_OK


