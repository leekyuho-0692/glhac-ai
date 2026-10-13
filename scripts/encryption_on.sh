#!/bin/bash
# 로컬에서 실행: 암호화 활성화 원격 런북을 서버로 보내고 실행.
# 사용: bash scripts/encryption_on.sh            (되돌리기는 서버에서: sed -i '/^GLHAC_ENCRYPTION=/d' /opt/glhac/.env && sudo systemctl restart glhac-app)
# 사전 조건:
#   - SSH 키가 ~/.ssh 에 있거나 GLHAC_SSH_KEY 환경변수로 지정
#   - 서버에 /opt/glhac 디렉터리와 .venv 가 있을 것
set -e

KEY="${GLHAC_SSH_KEY:-$HOME/Downloads/SSH_KeyPair-260908134815.pem}"
REMOTE="rocky@1.201.116.174"
SRC="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== 암호화 활성화 로컬 런북 ==="
echo "SSH 키: $KEY"
echo "원격: $REMOTE"
echo "로컬 소스: $SRC"
echo ""

# 원격 스크립트와 백필 스크립트 전송
echo "→ scripts/ 전송 중..."
scp -q -i "$KEY" "$SRC/scripts/encryption_on_remote.sh" "$REMOTE:/opt/glhac/scripts/"
scp -q -i "$KEY" "$SRC/scripts/encrypt_backfill.py" "$REMOTE:/opt/glhac/scripts/"

# 원격 실행
echo "→ 원격 런북 실행 중..."
ssh -i "$KEY" "$REMOTE" 'bash /opt/glhac/scripts/encryption_on_remote.sh'

echo "── 운영 확인 ──"
curl -s https://glhac.co.kr/health; echo

echo ""
echo "✓ 암호화 활성화 완료"
echo ""
echo "주의: 기존 백업 파일들은 평문으로 남습니다. 별도 정리가 필요합니다."
echo "  - 일일 백업(14일 순환): /var/lib/glhac/backups/"
echo "  - 수동 백업(bak-*): 별도 정리 필요"
