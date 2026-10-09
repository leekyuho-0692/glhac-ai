#!/usr/bin/env bash
# 원격 실행 — 운영 DB(glhac_v3.db)의 고아 행 정리.
# 감사로그(audit_log)·처리 이력(workflow_event)은 법적 기록이라 절대 건드리지 않는다.
set -euo pipefail

DB=/var/lib/glhac/glhac_v3.db
APPLY=0
if [ "${1:-}" = "--apply" ]; then APPLY=1; fi

ORPH="case_id is not null and case_id not like '%:%' and case_id not in (select case_id from case_application)"

TABLES="payment invoice approval_request onsite_checklist fatwa_decision external_identity generated_document notification"
declare -A EXP=([payment]=2 [invoice]=3 [approval_request]=4 [onsite_checklist]=3 [fatwa_decision]=5 [external_identity]=11 [generated_document]=13 [notification]=53)

MISMATCH=0
SUM=0

# 1) 표시 — 삭제 대상 고아 행
echo "── 고아 행(삭제 대상)"
for t in $TABLES; do
  n=$(sudo sqlite3 "$DB" "select count(*) from $t where $ORPH;")
  printf "%-20s %4s (예상 %s)\n" "$t" "$n" "${EXP[$t]}"
  SUM=$((SUM + n))
  if [ "$n" != "${EXP[$t]}" ]; then MISMATCH=1; fi
done

# 보존 대상 — 지우지 않음
AL0=$(sudo sqlite3 "$DB" "select count(*) from audit_log where $ORPH;")
WE0=$(sudo sqlite3 "$DB" "select count(*) from workflow_event where $ORPH;")
printf "%-20s %4s (보존)\n" "audit_log" "$AL0"
printf "%-20s %4s (보존)\n" "workflow_event" "$WE0"

CASES=$(sudo sqlite3 "$DB" "select count(distinct case_id) from (select case_id from payment union all select case_id from invoice union all select case_id from approval_request union all select case_id from onsite_checklist union all select case_id from fatwa_decision union all select case_id from external_identity union all select case_id from generated_document union all select case_id from notification) where $ORPH;")
echo "고아 케이스 ${CASES}개 (예상 19)"
if [ "$CASES" != "19" ]; then MISMATCH=1; fi

if [ "$MISMATCH" = "1" ]; then
  echo "⚠ 예상과 다름 — 중단(아무것도 지우지 않음)"
  exit 1
fi

if [ "$APPLY" = "0" ]; then
  echo "dry-run — 지우려면 --apply"
  exit 0
fi

# 2) 백업
BK="$DB.bak-orphanrows-$(date +%Y%m%d%H%M%S)"
sudo sqlite3 "$DB" ".backup $BK"
echo "백업: $BK ($(sudo du -h "$BK" | cut -f1))"

# 3) 한 트랜잭션으로 삭제
SQL="begin;"
for t in $TABLES; do
  SQL="$SQL delete from $t where $ORPH;"
done
SQL="$SQL commit;"
sudo sqlite3 "$DB" "$SQL"

# 4) 확인 — 남은 고아 행
LEFT=0
echo "── 삭제 후 남은 고아 행"
for t in $TABLES; do
  n=$(sudo sqlite3 "$DB" "select count(*) from $t where $ORPH;")
  printf "%-20s %4s\n" "$t" "$n"
  if [ "$n" != "0" ]; then LEFT=1; fi
done

# 보존 대상 확인
AL1=$(sudo sqlite3 "$DB" "select count(*) from audit_log where $ORPH;")
WE1=$(sudo sqlite3 "$DB" "select count(*) from workflow_event where $ORPH;")
printf "%-20s %4s (보존)\n" "audit_log" "$AL1"
printf "%-20s %4s (보존)\n" "workflow_event" "$WE1"
if [ "$AL1" != "$AL0" ] || [ "$WE1" != "$WE0" ]; then
  echo "⚠ 보존 대상이 바뀜"
  LEFT=1
fi

echo "무결성: $(sudo sqlite3 "$DB" 'pragma integrity_check;')"
echo "되돌리기: sudo systemctl stop glhac-app && sudo cp $BK $DB && sudo systemctl start glhac-app"

if [ "$LEFT" = "1" ]; then exit 1; fi
