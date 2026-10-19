#!/usr/bin/env python3
"""FK 위반(고아) 행 점검·정리 — R2-3.

관계 목록은 app/models.py 의 ForeignKey 메타데이터에서 자동으로 읽는다(하드코딩 없음).
모델에 FK 를 더하거나 ondelete 를 바꾸면 이 도구도 따라간다.

  python scripts/fk_orphans.py --db /var/lib/glhac/glhac_v3.db            # 보고(읽기 전용)
  python scripts/fk_orphans.py --db /var/lib/glhac/glhac_v3.db --apply    # 정책대로 정리

정책(= 모델의 ondelete)
  · CASCADE    고아 행을 삭제한다(부모 없는 순수 종속 행).
  · SET NULL   고아 키를 NULL 로 만든다(선택 참조).
  · RESTRICT   지우지 않는다. 감사·법적 기록(인보이스·결제·인증서·계약 등)이라 사람이 판단한다.
               목록만 내고 실패 종료한다 — --apply 도 이 경우엔 아무것도 바꾸지 않는다.

종료 코드(보고 모드): 0=위반 없음 · 3=정리 가능한 고아만 있음 · 2=RESTRICT 고아 있음(사람 판단 필요)
종료 코드(--apply):   0=정리 후 위반 0 · 2=RESTRICT 고아 때문에 거부 · 4=실패(롤백됨)
--apply 는 단일 트랜잭션이고 실패하면 전부 롤백한다.
"""
import argparse
import os
import sqlite3
import sys
from collections import namedtuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

Rel = namedtuple("Rel", "child ccol parent pcol ondelete nullable")

EXIT_OK, EXIT_CLEANABLE, EXIT_RESTRICT, EXIT_FAIL = 0, 3, 2, 4


def _q(name):
    return '"%s"' % name.replace('"', '""')


def relations():
    """모델 메타데이터의 모든 단일 컬럼 FK → Rel 목록(자식 테이블·컬럼 순)."""
    from app import models  # noqa: F401  (모델 등록)
    from app.db import Base
    out = []
    for t in Base.metadata.sorted_tables:
        for fk in t.foreign_key_constraints:
            if len(fk.columns) != 1:
                raise SystemExit("복합 FK 는 지원하지 않는다: %s" % fk)
            col = fk.columns[0]
            parent = list(fk.elements)[0].column
            od = (fk.ondelete or "RESTRICT").upper()
            if od == "NO ACTION":
                od = "RESTRICT"
            out.append(Rel(t.name, col.name, parent.table.name, parent.name, od, bool(col.nullable)))
    return sorted(out, key=lambda r: (r.child, r.ccol))


def _tables(conn):
    return {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}


def _cols(conn, table):
    return {r[1] for r in conn.execute("pragma table_info(%s)" % _q(table))}


def _orphan_where(r):
    """고아 조건. 자기참조 FK(sys_menu.parent_menu_id 등)에서 안쪽 테이블이 바깥을 가리지 않도록
    부모에 별칭 _p 를 쓰고, 바깥(자식)은 테이블명 그대로 한정한다."""
    return ("%(ch)s.%(c)s IS NOT NULL AND NOT EXISTS (SELECT 1 FROM %(p)s AS _p WHERE _p.%(pc)s = %(ch)s.%(c)s)"
            % {"c": _q(r.ccol), "p": _q(r.parent), "pc": _q(r.pcol), "ch": _q(r.child)})


def active_relations(conn):
    """DB 에 자식·부모 테이블/컬럼이 모두 있는 관계만(없는 것은 skipped 로 따로 돌려준다)."""
    tabs = _tables(conn)
    ok, skipped = [], []
    for r in relations():
        if (r.child in tabs and r.parent in tabs and r.ccol in _cols(conn, r.child)
                and r.pcol in _cols(conn, r.parent)):
            ok.append(r)
        else:
            skipped.append(r)
    return ok, skipped


def scan(conn, sample=5):
    """관계별 위반 행 수. [(Rel, 위반수, 표본키[…])] — 위반 0 인 관계도 포함."""
    ok, _sk = active_relations(conn)
    res = []
    for r in ok:
        w = _orphan_where(r)
        n = conn.execute("SELECT COUNT(*) FROM %s WHERE %s" % (_q(r.child), w)).fetchone()[0]
        keys = []
        if n:
            keys = [x[0] for x in conn.execute(
                "SELECT DISTINCT %s FROM %s WHERE %s LIMIT %d" % (_q(r.ccol), _q(r.child), w, sample))]
        res.append((r, n, keys))
    return res


def _pk_cols(conn, table):
    pk = [r[1] for r in sorted(conn.execute("pragma table_info(%s)" % _q(table)), key=lambda x: x[5]) if r[5]]
    return pk


def restrict_rows(conn, r, limit=50):
    """RESTRICT 고아 행 목록(PK 와 위반 키) — 사람이 판단할 자료."""
    pk = _pk_cols(conn, r.child) or ["rowid"]
    sel = ", ".join([_q(c) if c != "rowid" else "rowid" for c in pk] + [_q(r.ccol)])
    return conn.execute("SELECT %s FROM %s WHERE %s LIMIT %d"
                        % (sel, _q(r.child), _orphan_where(r), limit)).fetchall(), pk


def cleanup(conn, max_passes=20):
    """정책대로 정리(호출자가 트랜잭션을 관리). 반환: (삭제수 dict, NULL화 dict, 남은 RESTRICT 위반[(Rel,n)]).

    고아 행을 지우면 그 행의 자식이 새 고아가 될 수 있다(고아 제품 → 그 제품의 product_material).
    FK 가 켜진 DB 의 연쇄 동작을 흉내내며 변화가 없을 때까지 되풀이한다."""
    ok, _sk = active_relations(conn)
    deleted, nulled = {}, {}
    for _ in range(max_passes):
        changed = 0
        for r in ok:
            if r.ondelete not in ("CASCADE", "SET NULL"):
                continue
            w = _orphan_where(r)
            if r.ondelete == "CASCADE":
                n = conn.execute("DELETE FROM %s WHERE %s" % (_q(r.child), w)).rowcount
                if n:
                    deleted[r.child] = deleted.get(r.child, 0) + n
            else:
                n = conn.execute("UPDATE %s SET %s = NULL WHERE %s" % (_q(r.child), _q(r.ccol), w)).rowcount
                if n:
                    k = "%s.%s" % (r.child, r.ccol)
                    nulled[k] = nulled.get(k, 0) + n
            changed += n
        if not changed:
            break
    else:
        raise RuntimeError("정리가 %d 회 안에 수렴하지 않았다" % max_passes)
    left = [(r, n) for r, n, _k in scan(conn) if n]
    return deleted, nulled, left


def _print_report(res, skipped):
    print("%-26s %-22s %-24s %-9s %6s %s" % ("자식 테이블", "컬럼", "부모", "정책", "위반", "표본 키"))
    for r, n, keys in res:
        print("%-26s %-22s %-24s %-9s %6d %s" % (r.child, r.ccol, "%s.%s" % (r.parent, r.pcol), r.ondelete, n,
                                                 [str(k)[:20] for k in keys] if n else ""))
    if skipped:
        print("\n(DB 에 테이블/컬럼이 없어 건너뜀: %s)" % ", ".join("%s.%s" % (r.child, r.ccol) for r in skipped))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="SQLite DB 파일 경로")
    ap.add_argument("--apply", action="store_true", help="정책대로 정리(기본은 읽기 전용 보고)")
    a = ap.parse_args(argv)
    if not os.path.exists(a.db):
        print("DB 파일이 없다: %s" % a.db)
        return EXIT_FAIL
    if not relations():
        print("⛔ 모델에 ForeignKey 가 하나도 없다 — 새 코드가 배포되지 않은 상태로 보인다.")
        return EXIT_FAIL

    uri = "file:%s?mode=%s" % (a.db, "rw" if a.apply else "ro")
    conn = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=OFF")   # 정리 중 연쇄 동작이 끼어들지 않게 — 연쇄는 우리가 직접 한다
    try:
        res = scan(conn)
        _ok, skipped = active_relations(conn)
        _print_report(res, skipped)
        bad = [(r, n, k) for r, n, k in res if n]
        restrict = [(r, n, k) for r, n, k in bad if r.ondelete == "RESTRICT"]
        total = sum(n for _r, n, _k in bad)
        print("\n관계 %d개 · 위반 관계 %d개 · 위반 행 합계 %d" % (len(res), len(bad), total))

        if restrict:
            print("\n⛔ RESTRICT(감사·법적 기록) 고아 — 지우지 않는다. 사람이 판단할 것:")
            for r, n, _k in restrict:
                rows, pk = restrict_rows(conn, r)
                print("  %s.%s → %s.%s : %d건 (컬럼: %s)" % (r.child, r.ccol, r.parent, r.pcol, n,
                                                          ", ".join(pk + [r.ccol])))
                for row in rows:
                    print("    ", tuple(str(x)[:36] for x in row))

        if not a.apply:
            if restrict:
                return EXIT_RESTRICT
            if bad:
                print("\n정리 가능(CASCADE 삭제 / SET NULL) — 적용하려면 --apply")
                return EXIT_CLEANABLE
            print("\n위반 없음 ✅")
            return EXIT_OK

        if restrict:
            print("\n거부: RESTRICT 고아가 있어 --apply 를 하지 않았다(아무것도 바뀌지 않음).")
            return EXIT_RESTRICT
        if not bad:
            print("\n정리할 것 없음 ✅")
            return EXIT_OK

        conn.execute("BEGIN IMMEDIATE")
        try:
            deleted, nulled, left = cleanup(conn)
            if left:   # 연쇄 중 새로 드러난 RESTRICT 위반 등 — 되돌린다
                conn.execute("ROLLBACK")
                print("\n거부(롤백): 정리 뒤에도 위반이 남았다 — %s"
                      % ", ".join("%s.%s=%d" % (r.child, r.ccol, n) for r, n in left))
                return EXIT_RESTRICT
            conn.execute("COMMIT")
        except Exception as e:  # noqa: BLE001
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            print("\n실패(롤백됨): %s" % e)
            return EXIT_FAIL
        print("\n정리 완료(단일 트랜잭션 커밋)")
        for t, n in sorted(deleted.items()):
            print("  삭제   %-28s %d행" % (t, n))
        for k, n in sorted(nulled.items()):
            print("  NULL화 %-28s %d행" % (k, n))
        after = sum(n for _r, n, _k in scan(conn))
        print("정리 후 위반 행 합계: %d" % after)
        return EXIT_OK if after == 0 else EXIT_FAIL
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
