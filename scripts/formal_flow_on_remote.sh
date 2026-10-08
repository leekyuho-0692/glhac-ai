#!/bin/bash
# GLHAC_FORMAL_FLOW 켜기 — 운영 서버에서 실행(rocky). 순서: DB 백업 → 면제 스크립트 dry-run → --apply → .env 플래그 → 재기동 → 헬스.
# 되돌리기: sed -i '/^GLHAC_FORMAL_FLOW=/d' /opt/glhac/.env && sudo systemctl restart glhac-app  (면제 이벤트는 무해해 남겨 둔다)
set -e
TS=$(date +%Y%m%d%H%M%S)
cd /opt/glhac
sudo cp /var/lib/glhac/glhac_v3.db /var/lib/glhac/glhac_v3.db.bak-formalflow-$TS
echo "DB 백업: /var/lib/glhac/glhac_v3.db.bak-formalflow-$TS"
set -a; source /opt/glhac/.env; set +a
PY=/opt/glhac/.venv/bin/python
$PY scripts/formal_flow_grandfather.py
$PY scripts/formal_flow_grandfather.py --apply
grep -q '^GLHAC_FORMAL_FLOW=' .env && sudo sed -i 's/^GLHAC_FORMAL_FLOW=.*/GLHAC_FORMAL_FLOW=1/' .env || echo 'GLHAC_FORMAL_FLOW=1' | sudo tee -a .env >/dev/null
sudo systemctl restart glhac-app
for i in $(seq 1 15); do c=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8800/health); [ "$c" = 200 ] && break; sleep 2; done
echo "health=$c  GLHAC_FORMAL_FLOW=$(grep '^GLHAC_FORMAL_FLOW=' .env | cut -d= -f2)"
