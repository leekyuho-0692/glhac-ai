#!/usr/bin/env python3
"""운영 DB 테이블 재생성으로 FK 도입 — R2-4. SQLite 공식 12단계 절차.

SQLite 는 기존 테이블에 FK 를 ALTER 로 붙일 수 없다. 모델(app/models.py)의 정의(FK 포함)로
`<이름>__new` 를 만들고 → 공통 컬럼만 복사 → 원 테이블 DROP → RENAME → 인덱스 재생성한다.

  python scripts/fk_rebuild.py --db /var/lib/glhac/glhac_v3.db            # dry-run(기본, 읽기 전용)
  python scripts/fk_rebuild.py --db /var/lib/glhac/glhac_v3.db --apply    # 실제 적용

순서(--apply): 사전 점검 → PRAGMA foreign_keys=OFF → BEGIN IMMEDIATE → 테이블별 [생성 → 복사 → 행 수 대조 →
DROP → RENAME → 인덱스] → 전 테이블 행 수 전·후 대조 → PRAGMA foreign_key_check(0건이어야 함) → COMMIT →
PRAGMA integrity_check. 하나라도 어긋나면 ROLLBACK 한다(DB 는 손대기 전 그대로).

사전 점검(하나라도 걸리면 아무것도 하지 않고 종료)
  · DB 가 integrity_check ok
  · 고아 행 0 — scripts/fk_orphans.py 로 먼저 정리해야 한다(R2-3)
  · 모델에 없는 컬럼이 DB 에 있으면 중단(복사하면 그 데이터가 사라진다)
  · 모델엔 있고 DB 엔 없는 컬럼은 NULL 허용일 때만 통과(NOT NULL 이면 앱을 한 번 띄워 _migrate 를 돌릴 것)
  · NOT NULL 로 바뀌는 컬럼에 NULL 이 있으면 중단(_migrate 는 컬럼을 NULL 허용으로 추가한다)
  · 트리거·뷰가 있으면 중단(재생성 대상이 아니라 놓칠 수 있다)
이미 모델과 같은 FK 를 가진 테이블은 건너뛴다 — 두 번 돌려도 안전하다(멱등).

종료 코드: 0=성공(또는 할 일 없음/dry-run 통과) · 2=사전 점검 불통과 · 4=실패(롤백됨)
"""
import argparse
import os
import re
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fk_orphans  # noqa: E402

EXIT_OK, EXIT_PRECHECK, EXIT_FAIL = 0, 2, 4


def _q(name):
    return '"%s"' % name.replace('"', '""')


def model_tables():
    from app import models  # noqa: F401
    from app.db import Base
    return {t.name: t for t in Base.metadata.sorted_tables}


def db_fks(conn, table):
    """DB 에 실제로 있는 FK 집합 {(from, 부모테이블, to, on_delete)}."""
    out = set()
    for r in conn.execute("pragma foreign_key_list(%s)" % _q(table)):
        out.add((r[3], r[2], r[4], (r[6] or "NO ACTION").upper()))
    return out


def model_fks(table):
    out = set()
    for fk in table.foreign_key_constraints:
        col = fk.columns[0]
        parent = list(fk.elements)[0].column
        od = (fk.ondelete or "NO ACTION").upper()
        out.add((col.name, parent.table.name, parent.name, od))
    return out


def create_sql(table, dialect):
    """모델 정의 그대로의 CREATE TABLE 문을 `<name>__new` 로 바꿔 돌려준다."""
    from sqlalchemy.schema import CreateTable
    ddl = str(CreateTable(table).compile(dialect=dialect)).strip()
    m = re.match(r'CREATE TABLE (\S+) \(', ddl)
    if not m:
        raise RuntimeError("CREATE TABLE 해석 실패: %s" % ddl[:80])
    return ddl.replace(m.group(1), _q(table.name + "__new"), 1)


def precheck(conn, mt, allow_drop_empty=False, check_orphans=True):
    """사전 점검. 반환: (오류[], 계획[{table,rows,common,fks_add,missing_in_db}], 건너뜀[], 참고[])"""
    errors, plan, skipped, notes = [], [], [], []
    tabs = fk_orphans._tables(conn)

    ic = conn.execute("pragma integrity_check").fetchall()
    if [r[0] for r in ic] != ["ok"]:
        errors.append("integrity_check 실패: %s" % [r[0] for r in ic][:5])

    extra = conn.execute("select type,name from sqlite_master where type in ('trigger','view')").fetchall()
    if extra:
        errors.append("트리거/뷰가 있다(재생성 대상 아님): %s" % extra)

    # FK 가 붙는 테이블의 부모는 전부 DB 에 있어야 한다(없으면 앱을 한 번 띄워 create_all).
    for name, t in mt.items():
        if name in tabs:
            continue
        if any(fk.columns[0].table.name == name for fk in t.foreign_key_constraints) or any(
                list(fk.elements)[0].column.table.name == name
                for tt in mt.values() for fk in tt.foreign_key_constraints):
            errors.append("DB 에 모델 테이블 %s 가 없다 — 앱을 한 번 기동해 create_all 을 돌린 뒤 다시 실행" % name)

    orph = [(r, n) for r, n, _k in fk_orphans.scan(conn) if n] if check_orphans else []
    if orph:
        errors.append("고아 행이 있다(먼저 scripts/fk_orphans.py): "
                      + ", ".join("%s.%s=%d" % (r.child, r.ccol, n) for r, n in orph))

    for name, t in mt.items():
        if name not in tabs or not model_fks(t):
            continue                                  # FK 가 없는 테이블은 재생성 대상이 아니다
        if model_fks(t) <= db_fks(conn, name):
            skipped.append(name)                      # 이미 같은 FK — 멱등
            continue
        info = list(conn.execute("pragma table_info(%s)" % _q(name)))
        dbcols = [r[1] for r in info]
        mcols = [c.name for c in t.columns]
        only_db = [c for c in dbcols if c not in mcols]
        for c in only_db:
            nn = conn.execute("select count(*) from %s where %s is not null" % (_q(name), _q(c))).fetchone()[0]
            if nn:
                errors.append("%s.%s: 모델에 없는 DB 컬럼(값 %d행) — 재생성하면 데이터가 사라진다" % (name, c, nn))
            elif allow_drop_empty:
                notes.append("%s.%s: 모델에 없는 DB 컬럼이지만 전부 NULL → 버린다(--allow-drop-empty-columns)" % (name, c))
            else:
                errors.append("%s.%s: 모델에 없는 DB 컬럼(전부 NULL) — 버려도 되면 --allow-drop-empty-columns" % (name, c))
        missing = [c for c in mcols if c not in dbcols]
        for c in missing:
            if not t.columns[c].nullable and not t.columns[c].primary_key:
                errors.append("%s.%s: DB 에 없는 NOT NULL 컬럼 — 앱을 한 번 기동해 _migrate 를 돌릴 것" % (name, c))
        common = [c for c in mcols if c in dbcols]
        backfill = {}
        for c in common:                              # NOT NULL 로 새로 막히는 컬럼에 NULL 이 있는가
            col = t.columns[c]
            if col.nullable or col.primary_key:
                continue
            if next(r[3] for r in info if r[1] == c):
                continue
            n = conn.execute("select count(*) from %s where %s is null" % (_q(name), _q(c))).fetchone()[0]
            if not n:
                continue
            # _migrate 는 컬럼을 NULL 허용으로 추가하고 앱은 NULL 을 기본값으로 읽는다(예: scheme NULL = product).
            # 모델의 스칼라 기본값이 있으면 그 값으로 채워 복사한다 — 앱이 이미 그렇게 해석하던 값이다.
            if col.default is not None and col.default.is_scalar:
                backfill[c] = col.default.arg
                notes.append("%s.%s: NULL %d행 → 모델 기본값 %r 로 채워 복사" % (name, c, n, col.default.arg))
            else:
                errors.append("%s.%s: NOT NULL 인데 NULL %d행이고 모델 기본값이 없다 — 값을 정한 뒤 다시" % (name, c, n))
        plan.append({"table": name, "rows": conn.execute("select count(*) from %s" % _q(name)).fetchone()[0],
                     "common": common, "backfill": backfill, "fks": sorted(model_fks(t)), "missing_in_db": missing})
        if missing:
            notes.append("%s: DB 에 없는 NULL 허용 컬럼 %s → NULL 로 생성" % (name, missing))
    return errors, plan, skipped, notes


def table_indexes(conn, table):
    """DB 의 명시 인덱스(앱이 만든 _HOT_INDEXES·_UNIQUE_INDEXES 포함) — 자동 인덱스(sql NULL)는 제외."""
    return conn.execute("select name, sql from sqlite_master where type='index' and tbl_name=? and sql is not null",
                        (table,)).fetchall()


def model_index_sql(table, dialect):
    from sqlalchemy.schema import CreateIndex
    out = []
    for ix in table.indexes:
        out.append((ix.name, str(CreateIndex(ix).compile(dialect=dialect)).strip()))
    return out


def all_index_names(conn):
    return {r[0] for r in conn.execute("select name from sqlite_master where type='index' and sql is not null")}


def rebuild(conn, mt, plan, dialect, log):
    """단일 트랜잭션 안에서 실행(호출자가 BEGIN/COMMIT/ROLLBACK)."""
    counts_before = {t: conn.execute("select count(*) from %s" % _q(t)).fetchone()[0]
                     for t in fk_orphans._tables(conn) if not t.startswith("sqlite_")}
    idx_before = all_index_names(conn)
    for p in plan:
        name, t = p["table"], mt[p["table"]]
        new = name + "__new"
        old_ix = table_indexes(conn, name)          # DROP 하면 사라지므로 먼저 읽어 둔다
        conn.execute("DROP TABLE IF EXISTS %s" % _q(new))
        conn.execute(create_sql(t, dialect))
        cols = ", ".join(_q(c) for c in p["common"])
        bf = p.get("backfill") or {}
        sel = ", ".join(("COALESCE(%s, ?)" % _q(c)) if c in bf else _q(c) for c in p["common"])
        conn.execute("INSERT INTO %s (%s) SELECT %s FROM %s" % (_q(new), cols, sel, _q(name)),
                     [bf[c] for c in p["common"] if c in bf])
        n_new = conn.execute("select count(*) from %s" % _q(new)).fetchone()[0]
        if n_new != counts_before[name]:
            raise RuntimeError("%s 복사 행 수 불일치: 원본 %d / 새 테이블 %d" % (name, counts_before[name], n_new))
        conn.execute("DROP TABLE %s" % _q(name))
        conn.execute("ALTER TABLE %s RENAME TO %s" % (_q(new), _q(name)))
        made = set()
        for ixname, sql in old_ix:                  # DB 에 있던 인덱스를 그대로(테이블과 함께 사라졌으므로 충돌 없음)
            conn.execute(sql)
            made.add(ixname)
        for ixname, sql in model_index_sql(t, dialect):   # 모델이 선언한 인덱스(없었던 것만 새로 생김)
            if ixname not in made:
                conn.execute(sql)
        log("  ✓ %-26s %6d행 · FK %d개 · 인덱스 %d개 복원" % (name, n_new, len(p["fks"]), len(old_ix)))

    counts_after = {t: conn.execute("select count(*) from %s" % _q(t)).fetchone()[0]
                    for t in counts_before}
    diff = {t: (counts_before[t], counts_after.get(t)) for t in counts_before if counts_before[t] != counts_after.get(t)}
    if diff:
        raise RuntimeError("행 수 전·후 불일치: %s" % diff)
    lost = idx_before - all_index_names(conn)
    if lost:
        raise RuntimeError("재생성 뒤 사라진 인덱스: %s" % sorted(lost))
    chk = conn.execute("pragma foreign_key_check").fetchall()
    if chk:
        raise RuntimeError("foreign_key_check 위반 %d건: %s" % (len(chk), chk[:5]))
    return counts_before, counts_after


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="SQLite DB 파일 경로")
    ap.add_argument("--apply", action="store_true", help="실제 적용(기본은 dry-run)")
    ap.add_argument("--skip-orphan-check", action="store_true",
                    help="(dry-run 전용) 고아 점검을 건너뛰고 다른 사전 점검만 본다 — 정리 전 사전 점검용")
    ap.add_argument("--allow-drop-empty-columns", action="store_true",
                    help="모델에 없고 값이 전부 NULL 인 DB 컬럼을 버리는 것을 허용(값이 있으면 이 옵션으로도 중단)")
    a = ap.parse_args(argv)
    if not os.path.exists(a.db):
        print("DB 파일이 없다: %s" % a.db)
        return EXIT_FAIL

    if a.apply and a.skip_orphan_check:
        print("--skip-orphan-check 는 dry-run 에서만 쓸 수 있다")
        return EXIT_PRECHECK
    from sqlalchemy.dialects import sqlite as sqlite_dialect
    dialect = sqlite_dialect.dialect()
    mt = model_tables()
    if not any(t.foreign_key_constraints for t in mt.values()):
        print("⛔ 모델에 ForeignKey 가 하나도 없다 — 새 코드가 배포되지 않은 상태로 보인다.")
        return EXIT_PRECHECK

    t0 = time.time()
    uri = "file:%s?mode=%s" % (a.db, "rw" if a.apply else "ro")
    conn = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")    # 12단계의 첫 단계 — 트랜잭션 밖에서만 바뀐다
        errors, plan, skipped, notes = precheck(conn, mt, a.allow_drop_empty_columns, not a.skip_orphan_check)
        print("── 사전 점검 (%s)" % ("적용" if a.apply else "dry-run"))
        for n in notes:
            print("  참고:", n)
        if errors:
            for e in errors:
                print("  ⛔", e)
            print("사전 점검 불통과 — 아무것도 바꾸지 않았다.")
            return EXIT_PRECHECK
        print("  ✓ integrity_check ok · %s · 컬럼 대응 · NOT NULL 충돌 없음 · 트리거/뷰 없음"
              % ("고아 점검 생략(--skip-orphan-check)" if a.skip_orphan_check else "고아 0"))
        extra_tabs = sorted(t for t in fk_orphans._tables(conn) if t not in mt and not t.startswith("sqlite_"))
        if extra_tabs:
            print("  참고: 모델에 없는 테이블(건드리지 않음): %s" % ", ".join(extra_tabs))
        if skipped:
            print("  이미 FK 적용됨(건너뜀) %d개" % len(skipped))
        if not plan:
            print("재생성할 테이블이 없다 — 할 일 없음 ✅")
            return EXIT_OK
        print("\n── 재생성 대상 %d개 테이블 (행 합계 %d)" % (len(plan), sum(p["rows"] for p in plan)))
        for p in plan:
            print("  %-26s %6d행  FK: %s" % (p["table"], p["rows"],
                                              ", ".join("%s→%s(%s)" % (f[0], f[1], f[3]) for f in p["fks"])))
        if not a.apply:
            print("\ndry-run 끝 — 적용하려면 --apply (서비스를 먼저 멈출 것)")
            return EXIT_OK

        print("\n── 적용")
        conn.execute("BEGIN IMMEDIATE")
        try:
            before, after = rebuild(conn, mt, plan, dialect, print)
            conn.execute("COMMIT")
        except Exception as e:  # noqa: BLE001
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            print("\n⛔ 실패 — ROLLBACK 했다(DB 는 그대로): %s" % e)
            return EXIT_FAIL
        ic = [r[0] for r in conn.execute("pragma integrity_check")]
        fkc = conn.execute("pragma foreign_key_check").fetchall()
        print("\n행 수 전·후 일치: %d개 테이블 · 합계 %d행" % (len(before), sum(before.values())))
        print("foreign_key_check: %d건 · integrity_check: %s · 소요 %.1fs" % (len(fkc), ic, time.time() - t0))
        if ic != ["ok"] or fkc:
            print("⛔ 커밋 뒤 검증 실패 — 백업으로 복원할 것")
            return EXIT_FAIL
        print("FK 도입 완료 ✅")
        return EXIT_OK
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
