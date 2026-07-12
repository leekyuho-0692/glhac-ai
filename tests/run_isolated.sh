#!/usr/bin/env bash
# [E1] 파일별 격리 실행 — 각 테스트 파일을 독립 pytest 프로세스로 돌려
# 공유 app.main 엔진 오염을 원천 제거(멱등·결정적). CI(E3)의 실행 방식.
set -uo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
pass=0; fail=0; failed=()
FILES=$(ls tests/test_*.py tests/e2e*.py 2>/dev/null | grep -v conftest)
for f in $FILES; do
  [ -f "$f" ] || continue
  if $PY -m pytest "$f" -q >/tmp/glhac_iso_out 2>&1; then
    pass=$((pass+1)); echo "  ✅ $(basename "$f")"
  else
    fail=$((fail+1)); failed+=("$f"); echo "  ❌ $(basename "$f")"; tail -4 /tmp/glhac_iso_out
  fi
done
echo "──────── 파일 $((pass+fail)) · 통과 $pass · 실패 $fail ────────"
[ $fail -eq 0 ] || { printf '실패파일: %s\n' "${failed[@]}"; exit 1; }
echo "전 파일 격리 실행 통과 ✅"
