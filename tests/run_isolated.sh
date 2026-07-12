#!/usr/bin/env bash
# [E1] 파일별 격리 실행 — pytest 파일은 pytest(conftest 신선 DB), e2e 시나리오는
# 스크립트로 각자 신선 임시 DB에서. 각 파일 독립 프로세스 → 공유 엔진 오염 제거(멱등). CI(E3) 방식.
set -uo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
pass=0; fail=0; failed=()
echo "▶ pytest 테스트 (conftest 세션 신선 DB)"
for f in $(ls tests/test_*.py 2>/dev/null | grep -v conftest); do
  if $PY -m pytest "$f" -q >/tmp/glhac_iso_out 2>&1; then pass=$((pass+1)); echo "  ✅ $(basename $f)"
  else fail=$((fail+1)); failed+=("$(basename $f)"); echo "  ❌ $(basename $f)"; tail -3 /tmp/glhac_iso_out; fi
done
echo "▶ e2e 시나리오 스크립트 (각자 신선 임시 DB)"
for f in $(ls tests/e2e_s*.py tests/e2e.py 2>/dev/null); do
  D=$(mktemp -u).db
  if GLHAC_DEV=1 GLHAC_SECRET=$(printf 'x%.0s' {1..40}) GLHAC_DB_URL="sqlite:///$D" $PY "$f" >/tmp/glhac_iso_out 2>&1; then pass=$((pass+1)); echo "  ✅ $(basename $f)"
  else fail=$((fail+1)); failed+=("$(basename $f)"); echo "  ❌ $(basename $f)"; tail -3 /tmp/glhac_iso_out; fi
  rm -f "$D"*
done
echo "──────── 파일 $((pass+fail)) · 통과 $pass · 실패 $fail ────────"
[ $fail -eq 0 ] || { printf '실패: %s\n' "${failed[@]}"; exit 1; }
echo "전 파일 통과 ✅"
