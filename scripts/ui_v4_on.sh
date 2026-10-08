#!/bin/bash
# 로컬에서 실행: 원격 런북을 서버로 보내고 서버에서 GLHAC_UI_DEFAULT 를 v4 로 전환한다.
# 사용법: bash scripts/ui_v4_on.sh [off]  (off 면 되돌리기)
set -e
KEY="${GLHAC_SSH_KEY:-$HOME/Downloads/SSH_KeyPair-260908134815.pem}"
REMOTE="rocky@1.201.116.174"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
MODE="${1:-on}"
scp -q -i "$KEY" "$SRC/scripts/ui_v4_on_remote.sh" "$REMOTE:/opt/glhac/scripts/"
ssh -i "$KEY" "$REMOTE" "bash /opt/glhac/scripts/ui_v4_on_remote.sh $MODE"
echo "── 공개 확인 ──"
curl -s -o /dev/null -w "glhac.co.kr/ → %{redirect_url}\n" https://glhac.co.kr/
curl -s https://glhac.co.kr/health; echo
