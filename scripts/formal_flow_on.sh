#!/bin/bash
# 로컬에서 실행: 면제 스크립트·원격 런북을 서버로 보내고 서버에서 GLHAC_FORMAL_FLOW 를 켠다.
# 전제: scripts/deploy.sh --app-only 로 3d532f7 이상의 코드가 배포돼 있을 것.
# 사용: bash scripts/formal_flow_on.sh            (되돌리기는 서버에서: sed -i '/^GLHAC_FORMAL_FLOW=/d' /opt/glhac/.env && sudo systemctl restart glhac-app)
set -e
KEY="${GLHAC_SSH_KEY:-$HOME/Downloads/SSH_KeyPair-260908134815.pem}"
REMOTE="rocky@1.201.116.174"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
scp -q -i "$KEY" "$SRC/scripts/formal_flow_grandfather.py" "$SRC/scripts/formal_flow_on_remote.sh" "$REMOTE:/opt/glhac/scripts/"
ssh -i "$KEY" "$REMOTE" 'bash /opt/glhac/scripts/formal_flow_on_remote.sh'
echo "── 공개 확인 ──"
curl -s https://glhac.co.kr/health; echo
