#!/bin/bash
# 서버에서 실행(rocky): 운영 DB 에 FK 도입 — 고아 정리 + 테이블 재생성. (R2-5)
# 사용법: bash /opt/glhac/scripts/fk_migrate_remote.sh [check]
#   check : 서비스 정지·백업·DB 변경 없이 사전 점검만(고아 보고 + 재생성 dry-run)
# 순서: 사전 확인 → 서비스 정지 → DB 백업(bak-fk-<TS>) → 고아 보고 → (감사기록 고아면 중단)
#       → 고아 정리 → 재생성 dry-run → 재생성 적용 → 서비스 기동 → /health
# 어느 단계에서든 실패하면 백업 복원 절차를 출력한다(docs/운영_배포_가이드.md §6 방식).
# 환경변수: FK_ALLOW_DROP_EMPTY=1 → 모델에 없고 값이 전부 NULL 인 DB 컬럼을 버리는 것을 허용
#           (값이 있으면 이 옵션으로도 중단한다).
set -e

MODE="${1:-run}"
TS=$(date +%Y%m%d%H%M%S)
DB=/var/lib/glhac/glhac_v3.db
BK="$DB.bak-fk-$TS"
cd /opt/glhac
EXTRA=""
[ "${FK_ALLOW_DROP_EMPTY:-0}" = "1" ] && EXTRA="--allow-drop-empty-columns"

echo "=== 운영 서버 FK 도입 ($MODE) ==="
echo ""

DBOWN=$(stat -c %U "$DB")

# DB 소유자 권한으로 파이썬 도구 실행. .env 는 rocky 소유 600 이라 DB 소유자가 직접 못 읽는다
# → rocky 가 읽어 stdin 으로 넘긴다(키를 명령줄 인자로 넘기면 ps 에 보이므로 쓰지 않는다). encryption_on_remote.sh 와 같은 방식.
tool() {
  sudo -u "$DBOWN" bash -c 'set -a; source /dev/stdin; set +a; cd /opt/glhac && PYTHONPATH=/opt/glhac /opt/glhac/.venv/bin/python "$@"' _ "$@" < /opt/glhac/.env
}

print_restore() {
  echo ""
  echo "← 백업으로 복원 (필요할 때만 · 서비스 정지 상태에서):"
  echo "  sudo systemctl stop glhac-app"
  echo "  sudo rm -f $DB-wal $DB-shm            # WAL 정리 필수 — 남으면 복원본에 재적용돼 DB 가 섞인다"
  echo "  sudo cp $BK $DB"
  echo "  sudo chown $DBOWN:$DBOWN $DB"
  echo "  sudo systemctl start glhac-app"
  echo "  curl -s http://127.0.0.1:8800/health"
  echo "  (코드 문제로 FK 강제만 끄려면: echo 'GLHAC_SQLITE_FK=0' | sudo tee -a /opt/glhac/.env && sudo systemctl restart glhac-app)"
}

SVC_STOPPED=0
start_service() {
  sudo systemctl start glhac-app
  SVC_STOPPED=0
}
fail() {   # fail "메시지" [서비스 재기동 여부 0/1]
  echo ""
  echo "❌ $1"
  if [ "$SVC_STOPPED" = "1" ]; then
    if [ "${2:-0}" = "1" ]; then
      echo "  → DB 는 일관된 상태라 서비스를 다시 기동합니다."
      start_service || true
    else
      echo "  ⚠ 서비스는 정지 상태입니다. 원인 확인 후: sudo systemctl start glhac-app"
    fi
  fi
  [ -f "$BK" ] && print_restore
  exit 1
}

# [0] 사전 확인 ─────────────────────────────────────────────
echo "[0] 사전 확인..."
[ -f /opt/glhac/app/models.py ] && grep -q 'ForeignKey(' /opt/glhac/app/models.py \
  || { echo "❌ 서버 코드에 FK 모델이 없다 — 먼저 bash scripts/deploy.sh 로 새 코드를 배포하세요."; exit 1; }
for f in fk_orphans.py fk_rebuild.py; do
  [ -f "/opt/glhac/scripts/$f" ] || { echo "❌ /opt/glhac/scripts/$f 없음"; exit 1; }
done
DBSZ=$(stat -c %s "$DB")
FREE=$(df --output=avail -B1 "$(dirname "$DB")" | tail -1)
NEED=$((DBSZ * 3))
if [ "$FREE" -lt "$NEED" ]; then
  echo "❌ 디스크 여유 부족: 필요 약 $((NEED/1024/1024))MB(백업+재생성 사본) / 여유 $((FREE/1024/1024))MB"
  exit 1
fi
echo "  ✓ 코드·도구 확인 · DB $((DBSZ/1024/1024))MB · 여유 $((FREE/1024/1024))MB · DB 소유자 $DBOWN"
echo ""

# 사전 점검 — 읽기 전용이라 서비스를 멈추지 않는다. 감사기록 고아 같은 중단 사유를 '정지 전에' 걸러 불필요한 중단을 없앤다.
preflight() {
  echo "[점검 1/2] 고아 행 보고 (읽기 전용)"
  set +e; tool scripts/fk_orphans.py --db "$DB"; RC=$?; set -e
  case "$RC" in
    0) echo "  ✓ 위반 없음" ;;
    3) echo "  ℹ 정리 가능한 고아만 있음 — 적용 시 자동 정리" ;;
    2) echo "  ⛔ 감사·법적 기록(RESTRICT) 고아가 있다 — 적용 전에 사람이 판단해야 한다(위 목록). 서비스·DB 는 건드리지 않았다."; exit 1 ;;
    *) echo "  ❌ fk_orphans 실패(rc=$RC)"; exit 1 ;;
  esac
  echo ""
  echo "[점검 2/2] 재생성 dry-run"
  SKIP=""
  # 정리 가능한 고아(rc=3)는 적용 단계에서 먼저 정리되므로, 점검에서는 고아 검사만 건너뛰고 나머지 사전 점검을 본다.
  [ "$RC" = "3" ] && SKIP="--skip-orphan-check"
  set +e; tool scripts/fk_rebuild.py --db "$DB" $SKIP $EXTRA; RC2=$?; set -e
  [ "$RC2" = "0" ] || { echo "  ⚠ 재생성 사전 점검 불통과(rc=$RC2) — 위 사유를 해결해야 적용할 수 있다. 서비스·DB 는 건드리지 않았다."; exit 1; }
  echo ""
  echo "✓ 사전 점검 통과"
}

preflight
if [ "$MODE" = "check" ]; then
  echo "(check 모드 — 서비스·DB 변경 없음)"
  exit 0
fi
echo ""

# [1] 서비스 정지 ───────────────────────────────────────────
echo "[1/8] 서비스 정지..."
T0=$(date +%s)
sudo systemctl stop glhac-app
SVC_STOPPED=1
echo "  ✓ glhac-app 정지"
echo ""

# [2] DB 백업 ───────────────────────────────────────────────
echo "[2/8] DB 백업 중..."
sudo -u "$DBOWN" /opt/glhac/.venv/bin/python << PYEOF
import sqlite3
conn = sqlite3.connect("file:$DB?mode=ro", uri=True)
backup_conn = sqlite3.connect("$BK")
conn.backup(backup_conn)
backup_conn.close()
conn.close()
print("DB 백업 완료: $BK")
PYEOF
result=$(sudo -u "$DBOWN" sqlite3 "$BK" "PRAGMA integrity_check")
[ "$result" = "ok" ] || fail "백업 무결성 검사 실패: $result" 1
echo "  ✓ $BK (integrity_check ok)"
echo ""

# [3] 고아 보고 ─────────────────────────────────────────────
echo "[3/8] 고아 행 보고 (fk_orphans.py)"
set +e; tool scripts/fk_orphans.py --db "$DB"; RC=$?; set -e
case "$RC" in
  0) echo "  ✓ 위반 없음" ;;
  3) echo "  ℹ 정리 가능한 고아만 있음(CASCADE 삭제 / SET NULL)" ;;
  2) fail "감사·법적 기록(RESTRICT) 고아가 있어 중단합니다 — DB 는 바뀌지 않았습니다. 위 목록을 사람이 판단한 뒤 다시 실행하세요." 1 ;;
  *) fail "fk_orphans 실패(rc=$RC)" 1 ;;
esac
echo ""

# [4] 고아 정리 ─────────────────────────────────────────────
echo "[4/8] 고아 정리 (fk_orphans.py --apply)"
if [ "$RC" = "3" ]; then
  tool scripts/fk_orphans.py --db "$DB" --apply || fail "고아 정리 실패(단일 트랜잭션이라 롤백됨)" 1
else
  echo "  (정리할 것 없음)"
fi
echo ""

# [5] 재생성 dry-run ────────────────────────────────────────
echo "[5/8] 재생성 사전 점검 (fk_rebuild.py dry-run)"
tool scripts/fk_rebuild.py --db "$DB" $EXTRA || fail "재생성 사전 점검 불통과 — 재생성은 시작하지 않았습니다(고아 정리만 반영됨). 위 사유를 해결한 뒤 다시 실행하세요." 1
echo ""

# [6] 재생성 적용 ───────────────────────────────────────────
echo "[6/8] 테이블 재생성 (fk_rebuild.py --apply)"
tool scripts/fk_rebuild.py --db "$DB" --apply $EXTRA || fail "재생성 실패 — 스크립트가 ROLLBACK 했습니다(재생성 전 상태). 복원이 필요한지 위 출력으로 판단하세요." 1
echo ""

# [7] 서비스 기동 + 헬스 ────────────────────────────────────
echo "[7/8] 서비스 기동..."
start_service
c=000
for i in $(seq 1 20); do
  c=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8800/health || true)
  [ "$c" = 200 ] && break
  [ "$i" -lt 20 ] && sleep 2
done
T1=$(date +%s)
if [ "$c" != 200 ]; then
  echo "❌ 헬스 체크 실패 (HTTP $c) — 로그: sudo journalctl -u glhac-app -n 50 --no-pager"
  print_restore
  exit 1
fi
echo "  ✓ 헬스 체크 통과 (HTTP $c) · 서비스 중단 시간 약 $((T1 - T0))초"
echo ""

# [8] 사후 확인 ─────────────────────────────────────────────
echo "[8/8] 사후 확인..."
FKC=$(sudo -u "$DBOWN" sqlite3 "$DB" "PRAGMA foreign_key_check" | wc -l | tr -d ' ')
IC=$(sudo -u "$DBOWN" sqlite3 "$DB" "PRAGMA integrity_check")
NFK=$(sudo -u "$DBOWN" sqlite3 "$DB" "SELECT COUNT(*) FROM pragma_foreign_key_list('payment')")
echo "  foreign_key_check 위반: ${FKC}건 · integrity_check: $IC · payment 의 FK: ${NFK}개(2 기대)"
if [ "$FKC" != "0" ] || [ "$IC" != "ok" ] || [ "$NFK" != "2" ]; then
  echo "❌ 사후 확인 불일치"
  print_restore
  exit 1
fi
echo ""
echo "✓ FK 도입 완료"
echo ""
echo "── 되돌리기 (필요 시) ──"
print_restore
