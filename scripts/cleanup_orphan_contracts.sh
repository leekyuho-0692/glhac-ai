#!/usr/bin/env bash
# 사용법:
#   bash scripts/cleanup_orphan_contracts.sh          (dry-run: 대상만 표시)
#   bash scripts/cleanup_orphan_contracts.sh --apply  (백업 후 삭제)
set -euo pipefail

KEY="${GLHAC_SSH_KEY:-$HOME/Downloads/SSH_KeyPair-260908134815.pem}"
REMOTE="rocky@1.201.116.174"
SRC="$(cd "$(dirname "$0")/.." && pwd)"

scp -q -i "$KEY" "$SRC/scripts/cleanup_orphan_contracts_remote.sh" "$REMOTE:/tmp/cleanup_orphan_contracts_remote.sh"
ssh -i "$KEY" "$REMOTE" "bash /tmp/cleanup_orphan_contracts_remote.sh $*; rm -f /tmp/cleanup_orphan_contracts_remote.sh"
