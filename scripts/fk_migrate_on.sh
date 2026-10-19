#!/bin/bash
# 로컬에서 실행: 운영 DB 에 FK 를 도입하는 원격 런북을 서버로 보내고 실행한다. (R2-5)
# 사용법:
#   bash scripts/fk_migrate_on.sh check   # 사전 점검만 — 서비스 정지·DB 변경 없음(고아 보고 + 재생성 dry-run)
#   bash scripts/fk_migrate_on.sh         # 실제 적용(서비스 정지 → 백업 → 고아 정리 → 재생성 → 기동)
# 사전 조건:
#   - 새 코드가 서버에 배포돼 있을 것(app/models.py 에 ForeignKey 선언): 먼저 bash scripts/deploy.sh
#   - SSH 키가 ~/.ssh 에 있거나 GLHAC_SSH_KEY 환경변수로 지정
#   - 먼저 check 를 돌려 사전 점검이 통과하는지 본다. 값이 전부 NULL 인 모델 외 컬럼을 버려도 된다고 판단했다면
#     FK_ALLOW_DROP_EMPTY=1 bash scripts/fk_migrate_on.sh [check] 로 허용을 명시한다.
# 되돌리기: 적용 중 실패하면 원격 런북이 백업 복원 절차를 그대로 출력한다. docs/운영_배포_가이드.md §6-1 참조.
set -e

KEY="${GLHAC_SSH_KEY:-$HOME/Downloads/SSH_KeyPair-260908134815.pem}"
REMOTE="rocky@1.201.116.174"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
MODE="${1:-run}"
case "$MODE" in check|run) ;; *) echo "사용법: bash scripts/fk_migrate_on.sh [check]"; exit 2 ;; esac

echo "=== FK 도입 로컬 런북 ($MODE) ==="
echo "SSH 키: $KEY"
echo "원격: $REMOTE"
echo "로컬 소스: $SRC"
echo ""

echo "→ scripts/ 전송 중..."
scp -q -i "$KEY" "$SRC/scripts/fk_migrate_remote.sh" "$REMOTE:/opt/glhac/scripts/"
scp -q -i "$KEY" "$SRC/scripts/fk_orphans.py" "$REMOTE:/opt/glhac/scripts/"
scp -q -i "$KEY" "$SRC/scripts/fk_rebuild.py" "$REMOTE:/opt/glhac/scripts/"

echo "→ 원격 런북 실행 중..."
# 긴 one-liner 금지(붙여넣기 깨짐) — 서버의 스크립트 파일을 짧은 명령으로 부른다.
ssh -i "$KEY" "$REMOTE" "FK_ALLOW_DROP_EMPTY=${FK_ALLOW_DROP_EMPTY:-0} bash /opt/glhac/scripts/fk_migrate_remote.sh $MODE"

echo "── 운영 확인 ──"
curl -s https://glhac.co.kr/health; echo
echo ""
if [ "$MODE" = "check" ]; then
  echo "✓ 사전 점검 완료(서비스·DB 변경 없음). 통과했으면: bash scripts/fk_migrate_on.sh"
else
  echo "✓ FK 도입 완료"
  echo "주의: bak-fk-* 백업(개인정보 포함)은 문제가 없음을 확인한 뒤 따로 정리하세요."
fi
