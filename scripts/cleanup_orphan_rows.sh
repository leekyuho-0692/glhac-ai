#!/usr/bin/env bash
# 로컬 래퍼 — 운영 DB 의 '삭제된 케이스에 남은 고아 행' 정리 런북을 원격에서 실행한다.
#
# 사용법:
#   scripts/cleanup_orphan_rows.sh            # (인자 없음) dry-run — 대상 건수만 확인
#   scripts/cleanup_orphan_rows.sh --apply    # 백업 후 삭제
#
# 감사로그(audit_log)·처리 이력(workflow_event)은 법적 기록이라 절대 건드리지 않는다.
set -euo pipefail

KEY="${GLHAC_SSH_KEY:-$HOME/Downloads/SSH_KeyPair-260908134815.pem}"; REMOTE="rocky@1.201.116.174"; SRC="$(cd "$(dirname "$0")/.." && pwd)"
scp -q -i "$KEY" "$SRC/scripts/cleanup_orphan_rows_remote.sh" "$REMOTE:/tmp/cleanup_orphan_rows_remote.sh"
ssh -i "$KEY" "$REMOTE" "bash /tmp/cleanup_orphan_rows_remote.sh $*; rc=\$?; rm -f /tmp/cleanup_orphan_rows_remote.sh; exit \$rc"
