#!/usr/bin/env bash
# CI(.github/workflows/ci.yml)와 같은 조건으로 로컬에서 돌린다 — 격리 테스트 서버를 띄워 e2e 시나리오까지.
# 그냥 `bash tests/run_isolated.sh` 는 GLHAC_E2E_BASE 가 없어 e2e(tests/e2e*.py)를 건너뛴다.
# 그래서 2026-10 감사 수정이 e2e 와 한 번도 안 맞춰진 채 배포됐고, main CI 에서야 드러났다.
#
# 사용법: bash tests/run_ci_local.sh [포트(기본 8898)] [run_isolated.sh 에 넘길 env 는 앞에 붙여서]
#   예) GLHAC_FORMAL_FLOW=1 bash tests/run_ci_local.sh
# 운영 포트 8800 은 쓰지 않는다.
set -uo pipefail
cd "$(dirname "$0")/.."
PORT="${1:-8898}"
[ "$PORT" = "8800" ] && { echo "8800 은 운영 포트라 쓰지 않습니다"; exit 2; }
PY="${PYTHON:-.venv/bin/python}"

# ci.yml 전역 env
export GLHAC_DEV="${GLHAC_DEV:-1}"
export GLHAC_RATE_LIMIT_PER_MIN="${GLHAC_RATE_LIMIT_PER_MIN:-0}"
export GLHAC_SECRET="${GLHAC_SECRET:-ci-test-secret-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx}"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/glhac-ci-local.XXXXXX")"
export GLHAC_DB_URL="sqlite:///$TMP/ci_e2e.db"      # 서버와 테스트가 같은 파일이어야 한다(ci.yml 과 동일)

"$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" > "$TMP/server.log" 2>&1 &
SERVER_PID=$!
cleanup() { kill "$SERVER_PID" 2>/dev/null; wait "$SERVER_PID" 2>/dev/null; echo "서버 종료 · 로그 $TMP/server.log"; }
trap cleanup EXIT

for i in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/health" || echo 000)
  [ "$code" = "200" ] && { echo "테스트 서버 기동 (${i}s, :$PORT)"; break; }
  sleep 1
  [ "$i" = 60 ] && { echo "서버가 60초 안에 뜨지 않았습니다"; tail -30 "$TMP/server.log"; exit 1; }
done

# ci.yml '테스트 (파일별 격리)' 단계 env
export PYTHON="$PY"
export GLHAC_E2E_SKIP_OLLAMA=1
GLHAC_E2E_BASE="http://127.0.0.1:$PORT" bash tests/run_isolated.sh
RC_ISO=$?

# ci.yml '테스트 (정식 흐름 서버 · SRS 전 과정)' — e2e_srs_cycle 은 GLHAC_FORMAL_FLOW=1 서버에서만 돈다.
FPORT=$((PORT - 1))
GLHAC_FORMAL_FLOW=1 GLHAC_DB_URL="sqlite:///$TMP/ci_formal.db" \
  "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$FPORT" > "$TMP/server_formal.log" 2>&1 &
FPID=$!
trap 'cleanup; kill "$FPID" 2>/dev/null' EXIT
for i in $(seq 1 60); do
  [ "$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$FPORT/health" || echo 000)" = 200 ] && break
  sleep 1
done
echo "▶ 정식 흐름 서버(:$FPORT) — e2e_srs_cycle"
GLHAC_E2E_BASE="http://127.0.0.1:$FPORT" GLHAC_E2E_FORMAL=1 GLHAC_DB_URL="sqlite:///$TMP/ci_formal.db" \
  "$PY" tests/e2e_srs_cycle.py | grep -E "PASS|❌|SKIP"
RC_SRS=${PIPESTATUS[0]}
echo "──────── 격리 실행 rc=$RC_ISO · 정식 흐름 SRS rc=$RC_SRS ────────"
[ "$RC_ISO" = 0 ] && [ "$RC_SRS" = 0 ]
