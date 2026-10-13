#!/bin/bash
# GLHAC_ENCRYPTION 켜기 — 운영 서버에서 실행(rocky).
# 순서: DB 백업 → GLHAC_ENC_KEY 확인 → .env 플래그 → 재기동 → 암호화 적용 → 검증.
# 되돌리기: sed -i '/^GLHAC_ENCRYPTION=/d' /opt/glhac/.env && sudo systemctl restart glhac-app
#   (데이터는 그대로 읽히므로 무해. 단 키는 계속 필요함.)
set -e

TS=$(date +%Y%m%d%H%M%S)
cd /opt/glhac

echo "=== 운영 서버 암호화 활성화 ==="
echo ""

# 1) DB 백업
echo "[1/6] DB 백업 중..."
DBOWN=$(stat -c %U /var/lib/glhac/glhac_v3.db)
sudo -u "$DBOWN" /opt/glhac/.venv/bin/python << EOF
import sqlite3
db_path = "/var/lib/glhac/glhac_v3.db"
backup_path = "/var/lib/glhac/glhac_v3.db.bak-encryption-$TS"
conn = sqlite3.connect("file:/var/lib/glhac/glhac_v3.db?mode=ro", uri=True)
backup_conn = sqlite3.connect(backup_path)
conn.backup(backup_conn)
backup_conn.close()
conn.close()
print(f"DB 백업 완료: {backup_path}")
EOF

result=$(sudo -u "$DBOWN" sqlite3 /var/lib/glhac/glhac_v3.db.bak-encryption-$TS "PRAGMA integrity_check")
if [ "$result" != "ok" ]; then
  echo "❌ 백업 무결성 검사 실패: $result"
  exit 1
fi
echo "  ✓ /var/lib/glhac/glhac_v3.db.bak-encryption-$TS"
echo ""

# 2) GLHAC_ENC_KEY 확인 및 필요 시 생성 지시
echo "[2/6] 암호화 키 확인 중..."
set -a; source /opt/glhac/.env; set +a

if [ -z "$GLHAC_ENC_KEY" ]; then
  echo "❌ GLHAC_ENC_KEY가 설정되지 않았습니다."
  echo ""
  echo "다음 명령으로 키를 생성하고 .env에 추가하세요:"
  echo "  python3 -c \"import secrets,base64;print(base64.b64encode(secrets.token_bytes(32)).decode())\""
  echo ""
  echo "⚠️  중요: 이 키를 잃으면 암호화된 데이터를 복구할 수 없습니다!"
  echo "         반드시 오프사이트(NAS 등)에 따로 보관하세요."
  echo ""
  exit 1
fi
echo "  ✓ GLHAC_ENC_KEY 설정됨 (길이: ${#GLHAC_ENC_KEY} 글자)"
echo ""

# 3) .env 백업 및 GLHAC_ENCRYPTION 플래그 추가
echo "[3/6] .env 설정 중..."
cp /opt/glhac/.env /opt/glhac/.env.bak-encryption-$TS
echo "  ✓ /opt/glhac/.env.bak-encryption-$TS 백업 완료"

# GLHAC_ENCRYPTION 플래그 추가 (주석이 없어야 systemd EnvironmentFile이 제대로 읽음)
if grep -q '^GLHAC_ENCRYPTION=' /opt/glhac/.env; then
  sudo sed -i 's/^GLHAC_ENCRYPTION=.*/GLHAC_ENCRYPTION=1/' /opt/glhac/.env
else
  echo 'GLHAC_ENCRYPTION=1' | sudo tee -a /opt/glhac/.env >/dev/null
fi
echo "  ✓ GLHAC_ENCRYPTION=1 설정 완료"
echo ""

# 4) 서비스 재기동
echo "[4/6] 서비스 재기동 중..."
sudo systemctl restart glhac-app
echo "  ✓ glhac-app 재기동 완료"

# 헬스 확인 (최대 15초)
for i in $(seq 1 15); do
  c=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8800/health)
  if [ "$c" = 200 ]; then
    echo "  ✓ 헬스 체크 통과 (HTTP $c)"
    break
  fi
  [ "$i" -lt 15 ] && sleep 2
done
if [ "$c" != 200 ]; then
  echo "❌ 헬스 체크 실패 (HTTP $c) — .env 롤백: sed -i '/^GLHAC_ENCRYPTION=/d' /opt/glhac/.env && sudo systemctl restart glhac-app"
  exit 1
fi
echo ""

# 5) 암호화 적용
echo "[5/6] 데이터 암호화 중..."
set -a; source /opt/glhac/.env; set +a
PY=/opt/glhac/.venv/bin/python
PYTHONPATH=/opt/glhac $PY scripts/encrypt_backfill.py --apply --allow-derived-key || {
  echo "❌ 암호화 적용 실패"
  echo ""
  echo "← 되돌리기:"
  echo "  sed -i '/^GLHAC_ENCRYPTION=/d' /opt/glhac/.env"
  echo "  sudo systemctl restart glhac-app"
  echo "  (데이터는 그대로 읽히므로 안전함)"
  exit 1
}
echo "  ✓ 데이터 암호화 완료"
echo ""

# 6) 검증
echo "[6/6] 암호화 검증 중..."
PYTHONPATH=/opt/glhac $PY scripts/encrypt_backfill.py --verify || {
  echo "❌ 검증 실패 (평문 데이터 남음)"
  exit 1
}
echo "  ✓ 모든 데이터 암호화 확인"
echo ""

echo "✓ 암호화 활성화 완료"
echo ""
echo "── 상태 확인 ──"
echo "GLHAC_ENCRYPTION=$(grep '^GLHAC_ENCRYPTION=' /opt/glhac/.env | cut -d= -f2)"
echo "GLHAC_ENC_KEY=$(grep '^GLHAC_ENC_KEY=' /opt/glhac/.env | head -c 20)..."
echo ""
echo "── 되돌리기 (필요 시) ──"
echo "sed -i '/^GLHAC_ENCRYPTION=/d' /opt/glhac/.env"
echo "sudo systemctl restart glhac-app"
echo "(데이터는 그대로 읽히므로 무해함)"
