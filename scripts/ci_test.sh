#!/usr/bin/env bash
# GL-HAC AI v3 — CI 테스트 러너
# TestClient 테스트(서버 불필요) + 서버기반 e2e(각 테스트마다 fresh 서버·DB).
# 실패 시 non-zero exit. 사용: PYTHON=python bash scripts/ci_test.sh
set -u
cd "$(dirname "$0")/.."           # → glhac-ai-v3 루트
PY="${PYTHON:-python}"
PORT="${GLHAC_PORT:-8800}"
BASE="http://127.0.0.1:${PORT}"
FAIL=0

_check() {   # $1=label  $2=output
  local label="$1" out="$2"
  if echo "$out" | grep -qE "Traceback|OperationalError|Error:"; then
    echo "  ❌ ${label} (error)"; echo "$out" | tail -3 | sed 's/^/      /'; FAIL=1; return; fi
  if echo "$out" | grep -qE "FAIL [1-9]"; then
    echo "  ❌ ${label} $(echo "$out" | grep -oE 'FAIL [0-9]+' | tail -1)"; FAIL=1; return; fi
  local xy; xy=$(echo "$out" | grep -oE "[0-9]+/[0-9]+" | tail -1)
  if [ -n "$xy" ] && [ "${xy%/*}" != "${xy#*/}" ]; then
    echo "  ❌ ${label} ${xy}"; FAIL=1; return; fi
  echo "  ✅ ${label} $(echo "$out" | grep -oE 'PASS [0-9].*' | tail -1)"
}

echo "== TestClient 테스트 (서버 불필요) =="
for t in tests/test_smoke.py tests/test_v3_smoke.py; do
  rm -f glhac_test.db glhac_v3_test.db 2>/dev/null
  out=$("$PY" "$t" 2>&1); rc=$?
  [ $rc -ne 0 ] && FAIL=1
  _check "$(basename "$t")" "$out"
done

echo "== 서버기반 e2e (fresh 서버·DB) =="
for t in tests/e2e_rbac.py $(ls tests/e2e_s*.py | sort -V); do
  rm -f glhac.db 2>/dev/null
  "$PY" -m uvicorn app.main:app --port "$PORT" --log-level error >/tmp/glhac_uv.log 2>&1 &
  PID=$!
  up=0
  for _ in $(seq 1 60); do
    if curl -sf "${BASE}/health" >/dev/null 2>&1; then up=1; break; fi
    sleep 0.5
  done
  if [ $up -ne 1 ]; then
    echo "  ❌ $(basename "$t") (서버 기동 실패)"; tail -5 /tmp/glhac_uv.log | sed 's/^/      /'
    FAIL=1; kill "$PID" 2>/dev/null; wait "$PID" 2>/dev/null; continue
  fi
  out=$("$PY" "$t" 2>&1)
  _check "$(basename "$t")" "$out"
  kill "$PID" 2>/dev/null; wait "$PID" 2>/dev/null
done

rm -f glhac.db glhac_test.db glhac_v3_test.db 2>/dev/null
echo ""
if [ $FAIL -eq 0 ]; then echo "=== ALL GREEN ✅ ==="; else echo "=== FAILURES DETECTED ❌ ==="; fi
exit $FAIL
