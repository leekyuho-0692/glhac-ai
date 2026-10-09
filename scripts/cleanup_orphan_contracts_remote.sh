#!/usr/bin/env bash
# 원격 실행용: 운영 DB 의 고아 계약 4건만 삭제
set -euo pipefail

DB=/var/lib/glhac/glhac_v3.db
APPLY=0
if [ "${1:-}" = "--apply" ]; then
  APPLY=1
fi

# 고정 대상 ID — 이 4개 외에는 절대 지우지 않는다
IDS="'433422360f0e483eb6675ee9f9b7ad68','c1d0be65a8264c75a0d5d8edcf769d07','d78b56bdcbff49148257bf19132756c1','a85b26f30c5d4ffb8729ccf92df3a721'"
# 대상 조건: 지정 ID 이면서, 케이스가 이미 사라진 행만 (이중 안전장치)
WHERE="contract_id in ($IDS) and case_id not in (select case_id from case_application)"

echo "── 대상 계약"
sudo sqlite3 -header "$DB" "select contract_id, contract_no, party_a, status, created_at from contract where $WHERE;"

N=$(sudo sqlite3 "$DB" "select count(*) from contract where $WHERE;")
echo "대상 ${N}건"

if [ "$N" -ne 4 ]; then
  echo "⚠ 예상(4건)과 다름 — 중단"
  exit 1
fi

if [ "$APPLY" -eq 0 ]; then
  echo "dry-run — 지우려면 --apply 를 붙여 다시 실행"
  exit 0
fi

BK="$DB.bak-orphancontract-$(date +%Y%m%d%H%M%S)"
sudo sqlite3 "$DB" ".backup $BK"
echo "백업: $BK ($(sudo du -h "$BK" | cut -f1))"

sudo sqlite3 "$DB" "begin; delete from contract where $WHERE; commit;"

LEFT=$(sudo sqlite3 "$DB" "select count(*) from contract where contract_id in ($IDS);")
echo "삭제 후 남은 대상: $LEFT"

TOTAL=$(sudo sqlite3 "$DB" "select count(*) from contract;")
echo "전체 계약 행: $TOTAL"

ORPH=$(sudo sqlite3 "$DB" "select count(*) from contract where case_id not in (select case_id from case_application);")
echo "남은 고아 계약: $ORPH"

echo "무결성: $(sudo sqlite3 "$DB" 'pragma integrity_check;')"

echo "되돌리기: sudo systemctl stop glhac-app && sudo cp $BK $DB && sudo systemctl start glhac-app"

if [ "$LEFT" -ne 0 ]; then
  exit 1
fi
