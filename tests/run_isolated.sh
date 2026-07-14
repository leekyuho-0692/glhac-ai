#!/usr/bin/env bash
# [E1] 파일별 격리 실행 — pytest 파일은 pytest(conftest 신선 DB), e2e 시나리오는
# 스크립트로 각자 신선 임시 DB에서. 각 파일 독립 프로세스 → 공유 엔진 오염 제거(멱등). CI(E3) 방식.
set -uo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
# 파일별 격리 실행 공통 env — 데모 계정 시드 + 레이트리밋 무력화(조합/파일별 안전) + 강한 테스트 시크릿.
export GLHAC_DEV="${GLHAC_DEV:-1}"
export GLHAC_RATE_LIMIT_PER_MIN="${GLHAC_RATE_LIMIT_PER_MIN:-0}"
export GLHAC_SECRET="${GLHAC_SECRET:-ci-test-secret-$(printf 'x%.0s' {1..40})}"
# 커버리지: GLHAC_COV=1 이면 파일별 --cov-append 로 누적 → 마지막에 리포트.
COV_ARGS=""
if [ "${GLHAC_COV:-0}" = "1" ]; then
  rm -f .coverage 2>/dev/null || true
  COV_ARGS="--cov=app --cov-append --cov-report="
fi
pass=0; fail=0; failed=()
echo "▶ pytest 테스트 (conftest 세션 신선 DB · RL=$GLHAC_RATE_LIMIT_PER_MIN)"
for f in $(ls tests/test_*.py 2>/dev/null | grep -v conftest); do
  if $PY -m pytest "$f" -q $COV_ARGS >/tmp/glhac_iso_out 2>&1; then pass=$((pass+1)); echo "  ✅ $(basename $f)"
  else fail=$((fail+1)); failed+=("$(basename $f)"); echo "  ❌ $(basename $f)"; tail -3 /tmp/glhac_iso_out; fi
done
if [ "${GLHAC_COV:-0}" = "1" ]; then
  echo "▶ 커버리지 리포트 (라인)"; $PY -m coverage report 2>/dev/null | tail -30 || true
fi
# e2e_s*.py는 GLHAC_E2E_BASE(기본 127.0.0.1:8800=프로덕션)에 HTTP로 쏨 → 라이브 오염 위험.
# 명시적으로 비-8800 테스트 서버를 지정한 경우에만 실행(기본 SKIP).
if [ -n "${GLHAC_E2E_BASE:-}" ] && [[ "${GLHAC_E2E_BASE}" != *":8800"* ]]; then
echo "▶ e2e 시나리오 스크립트 (대상 $GLHAC_E2E_BASE · 각자 신선 임시 DB)"
for f in $(ls tests/e2e_s*.py tests/e2e.py 2>/dev/null); do
  D=$(mktemp -u).db
  if GLHAC_DEV=1 GLHAC_SECRET=$(printf 'x%.0s' {1..40}) GLHAC_DB_URL="sqlite:///$D" $PY "$f" >/tmp/glhac_iso_out 2>&1; then pass=$((pass+1)); echo "  ✅ $(basename $f)"
  else fail=$((fail+1)); failed+=("$(basename $f)"); echo "  ❌ $(basename $f)"; tail -3 /tmp/glhac_iso_out; fi
  rm -f "$D"*
done
else
echo "▶ e2e 시나리오 스크립트 — SKIP (라이브 8800 오염 방지 · GLHAC_E2E_BASE에 테스트 서버 지정 시 실행)"
fi
echo "──────── 파일 $((pass+fail)) · 통과 $pass · 실패 $fail ────────"
[ $fail -eq 0 ] || { printf '실패: %s\n' "${failed[@]}"; exit 1; }
echo "전 파일 통과 ✅"
