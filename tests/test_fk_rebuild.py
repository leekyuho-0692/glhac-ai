"""R2-4 — scripts/fk_rebuild.py: 테이블 재생성 마이그레이션. 실제 SQLite 임시 파일에서 실제 main() 을 부른다(목 없음)."""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _fk_legacy import legacy_db as _legacy_db, seed_clean as _seed_clean   # noqa: E402,F401
import fk_rebuild  # noqa: E402


def _counts(c):
    return {t: c.execute("select count(*) from %s" % t).fetchone()[0]
            for (t,) in c.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")}


def test_재생성_dry_run은_아무것도_바꾸지_않는다(tmp_path, capsys):
    p = str(tmp_path / "a.db")
    c = _legacy_db(p)
    _seed_clean(c)
    c.close()
    assert fk_rebuild.main(["--db", p]) == fk_rebuild.EXIT_OK
    assert "재생성 대상" in capsys.readouterr().out
    c = sqlite3.connect(p)
    assert c.execute("pragma foreign_key_list(payment)").fetchall() == []
    assert c.execute("select count(*) from sqlite_master where name like '%__new'").fetchone()[0] == 0


def test_재생성_apply_FK생성_행수보존_인덱스보존_멱등(tmp_path, capsys):
    p = str(tmp_path / "a.db")
    c = _legacy_db(p, relax=[("penyelia_halal", "status")])
    _seed_clean(c)
    # 컬럼 값이 NULL 인 NOT NULL 컬럼(모델 기본값 있음) — _migrate 로 생긴 옛 행 모사
    c.execute("insert into penyelia_halal (penyelia_id, org_id, name, status) values ('pe1','o1','홍길동',NULL)")
    c.commit()
    cnt0 = _counts(c)
    idx0 = {r[0] for r in c.execute("select name from sqlite_master where type='index' and sql is not null")}
    c.close()

    assert fk_rebuild.main(["--db", p, "--apply"]) == fk_rebuild.EXIT_OK
    c = sqlite3.connect(p)
    assert _counts(c) == cnt0                                                  # 행 수 전·후 동일
    fk = {(r[3], r[2], r[6]) for r in c.execute("pragma foreign_key_list(payment)")}
    assert fk == {("case_id", "case_application", "RESTRICT"), ("invoice_id", "invoice", "RESTRICT")}
    idx1 = {r[0] for r in c.execute("select name from sqlite_master where type='index' and sql is not null")}
    assert idx0 <= idx1                                                        # 인덱스 하나도 안 잃음
    assert c.execute("pragma foreign_key_check").fetchall() == []
    assert c.execute("pragma integrity_check").fetchone()[0] == "ok"
    assert c.execute("select status from penyelia_halal").fetchone()[0] == "active"      # 모델 기본값으로 채움
    assert c.execute("select count(*) from sqlite_master where name like '%__new'").fetchone()[0] == 0
    # 값은 그대로(암호화 컬럼 포함 원문 복사)
    assert c.execute("select name from product where product_id='p1'").fetchone()[0] == "제품1"
    c.close()
    # 두 번째 실행은 할 일 없음
    capsys.readouterr()
    assert fk_rebuild.main(["--db", p, "--apply"]) == fk_rebuild.EXIT_OK
    assert "할 일 없음" in capsys.readouterr().out


def test_재생성_뒤_FK가_실제로_삭제를_통제한다(tmp_path):
    p = str(tmp_path / "a.db")
    c = _legacy_db(p)
    _seed_clean(c)
    c.close()
    assert fk_rebuild.main(["--db", p, "--apply"]) == fk_rebuild.EXIT_OK
    c = sqlite3.connect(p)
    c.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(sqlite3.IntegrityError):          # 인보이스(RESTRICT)가 있는 케이스는 못 지운다
        c.execute("delete from case_application where case_id='c1'")
    c.execute("delete from payment")
    c.execute("delete from invoice")
    c.execute("delete from case_application where case_id='c1'")   # 나머지는 CASCADE
    assert c.execute("select count(*) from product").fetchone()[0] == 0
    assert c.execute("select count(*) from workflow_event").fetchone()[0] == 1   # 의사키 이벤트는 그대로


def test_재생성_사전점검_고아가_있으면_거부한다(tmp_path):
    p = str(tmp_path / "a.db")
    c = _legacy_db(p)
    _seed_clean(c)
    c.execute("insert into product (product_id, case_id, name) values ('px','GONE','고아')")
    c.commit()
    c.close()
    assert fk_rebuild.main(["--db", p, "--apply"]) == fk_rebuild.EXIT_PRECHECK
    c = sqlite3.connect(p)
    assert c.execute("pragma foreign_key_list(product)").fetchall() == []     # 손대지 않음


def test_재생성_모델에_없는_컬럼은_값이_있으면_중단_비어있으면_옵션으로_허용(tmp_path):
    p = str(tmp_path / "a.db")
    c = _legacy_db(p, extra_cols=[("material", "legacy_col", "TEXT")])
    _seed_clean(c)
    c.close()
    assert fk_rebuild.main(["--db", p, "--apply"]) == fk_rebuild.EXIT_PRECHECK      # 전부 NULL 이어도 기본은 중단
    c = sqlite3.connect(p)
    c.execute("update material set legacy_col='중요'")
    c.commit()
    c.close()
    assert fk_rebuild.main(["--db", p, "--apply", "--allow-drop-empty-columns"]) == fk_rebuild.EXIT_PRECHECK
    c = sqlite3.connect(p)
    assert c.execute("select legacy_col from material").fetchone()[0] == "중요"       # 값이 있으면 옵션으로도 못 버린다
    c.execute("update material set legacy_col=NULL")
    c.commit()
    c.close()
    assert fk_rebuild.main(["--db", p, "--apply", "--allow-drop-empty-columns"]) == fk_rebuild.EXIT_OK


def test_재생성_NOT_NULL인데_기본값없는_NULL이면_중단(tmp_path):
    p = str(tmp_path / "a.db")
    c = _legacy_db(p, relax=[("product", "name")])      # product.name: NOT NULL · 기본값 없음
    _seed_clean(c)
    c.execute("update product set name=NULL")
    c.commit()
    c.close()
    assert fk_rebuild.main(["--db", p, "--apply"]) == fk_rebuild.EXIT_PRECHECK
