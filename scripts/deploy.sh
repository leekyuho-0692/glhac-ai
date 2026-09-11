#!/usr/bin/env bash
# GL-HAC AI v3 — 배포
#
# 지금까지 손으로 하던 것(백업 → rsync → 소유권 → 재기동 → 확인)을 한 줄로 묶는다.
# 손으로 하면 매번 빠뜨리는 게 생긴다 — 실제로 겪은 것들:
#   · chown 을 root 로 해서 서비스가 못 읽고 죽었다(서비스는 rocky 로 돈다)
#   · SELinux 라벨(restorecon)을 안 해서 nginx 가 403 을 냈다
#   · 번들 파일명을 그대로 둔 채 내용만 바꿔 캐시가 옛 파일을 계속 줬다
#
# 핵심 규칙 — **실패하면 되돌린다.** 배포 후 헬스체크가 통과해야 끝난 것으로 본다.
#
# 사용:
#   bash scripts/deploy.sh                 # 코드+홈페이지 배포
#   bash scripts/deploy.sh --app-only      # 코드만
#   bash scripts/deploy.sh --home-only     # 홈페이지만
#   bash scripts/deploy.sh --dry-run       # 뭘 보낼지만 보여 준다
#   bash scripts/deploy.sh --rollback      # 직전 백업으로 되돌린다
#
# 환경변수(없으면 아래 기본값):
#   GLHAC_SSH_KEY  GLHAC_SSH_HOST  GLHAC_HOME_SRC
set -euo pipefail

KEY="${GLHAC_SSH_KEY:-$HOME/Downloads/SSH_KeyPair-260908134815.pem}"
HOST="${GLHAC_SSH_HOST:-rocky@1.201.116.174}"
HOME_SRC="${GLHAC_HOME_SRC:-$HOME/Downloads/GLHAC_홈페이지_인트로적용}"
APP_SRC="$(cd "$(dirname "$0")/.." && pwd)"

REMOTE_APP=/opt/glhac/app
REMOTE_HOME=/var/www/glhac-home
SVC=glhac-app
OWNER=rocky:rocky            # 서비스 실행 계정 — root 로 바꾸면 앱이 못 읽는다
HEALTH=http://127.0.0.1:8800/health

DO_APP=1; DO_HOME=1; DRY=0; ROLLBACK=0
for a in "$@"; do
  case "$a" in
    --app-only)  DO_HOME=0 ;;
    --home-only) DO_APP=0 ;;
    --dry-run)   DRY=1 ;;
    --rollback)  ROLLBACK=1 ;;
    -h|--help)   sed -n '2,26p' "$0"; exit 0 ;;
    *) echo "모르는 옵션: $a"; exit 2 ;;
  esac
done

SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=no "$HOST")
RSYNC_SSH="ssh -i $KEY -o StrictHostKeyChecking=no"
TS="$(date +%Y%m%d%H%M%S)"
say() { printf '  %s\n' "$*"; }

# ── 되돌리기 ──────────────────────────────────────────────────────────────
if [ "$ROLLBACK" = 1 ]; then
  say "직전 백업으로 되돌립니다"
  "${SSH[@]}" bash -se <<'EOS'
set -euo pipefail
LAST=$(ls -t /opt/glhac-app-bak-*.tgz 2>/dev/null | head -1)
[ -n "$LAST" ] || { echo "  되돌릴 백업이 없습니다"; exit 1; }
echo "  사용할 백업: $LAST"
sudo tar xzf "$LAST" -C /opt/glhac
sudo chown -R rocky:rocky /opt/glhac/app
sudo systemctl restart glhac-app
sleep 8
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8800/health || echo 000)
echo "  health: $code"
[ "$code" = "200" ] || exit 1
EOS
  say "되돌리기 완료"
  exit 0
fi

# ── 사전 점검 ─────────────────────────────────────────────────────────────
[ -f "$KEY" ] || { echo "SSH 키가 없습니다: $KEY"; exit 1; }
if [ "$DO_HOME" = 1 ] && [ ! -d "$HOME_SRC" ]; then
  echo "홈페이지 소스가 없습니다: $HOME_SRC"; exit 1
fi

say "대상 $HOST"
say "코드 $([ $DO_APP = 1 ] && echo "$APP_SRC/app → $REMOTE_APP" || echo '(건너뜀)')"
say "홈  $([ $DO_HOME = 1 ] && echo "$HOME_SRC → $REMOTE_HOME" || echo '(건너뜀)')"

# 보낼 것이 뭔지 먼저 보여 준다(--dry-run 이면 여기서 끝)
RS_EXCL=(--exclude '__pycache__' --exclude '*.bak-*' --exclude '*.pyc'
         --exclude '.DS_Store' --exclude '*.db' --exclude '*.db-*')
if [ "$DO_APP" = 1 ]; then
  say "── 코드 변경분 ──"
  rsync -az --delete --dry-run --itemize-changes -e "$RSYNC_SSH" \
    "${RS_EXCL[@]}" "$APP_SRC/app/" "$HOST:/tmp/dep-app-$TS/" 2>/dev/null \
    | grep -vE '^\.d|^$' | head -30 || true
fi
if [ "$DRY" = 1 ]; then say "dry-run — 아무것도 바꾸지 않았습니다"; exit 0; fi

# ── 백업 ──────────────────────────────────────────────────────────────────
say "백업 중"
"${SSH[@]}" bash -se <<EOS
set -euo pipefail
sudo cp /var/lib/glhac/glhac_v3.db /var/lib/glhac/glhac_v3.db.bak-deploy-$TS
sudo tar czf /opt/glhac-app-bak-$TS.tgz -C /opt/glhac app
sudo tar czf /opt/glhac-home-bak-$TS.tgz -C /var/www glhac-home
# 백업이 무한정 쌓이지 않게 최근 10개만 남긴다
ls -t /opt/glhac-app-bak-*.tgz  2>/dev/null | tail -n +11 | xargs -r sudo rm -f
ls -t /opt/glhac-home-bak-*.tgz 2>/dev/null | tail -n +11 | xargs -r sudo rm -f
ls -t /var/lib/glhac/glhac_v3.db.bak-deploy-* 2>/dev/null | tail -n +6 | xargs -r sudo rm -f
EOS

# ── 전송 ──────────────────────────────────────────────────────────────────
if [ "$DO_APP" = 1 ]; then
  say "코드 전송"
  rsync -az --delete -e "$RSYNC_SSH" "${RS_EXCL[@]}" \
    "$APP_SRC/app/" "$HOST:/tmp/dep-app-$TS/"
fi
if [ "$DO_HOME" = 1 ]; then
  say "홈페이지 전송"
  rsync -az -e "$RSYNC_SSH" --exclude '.DS_Store' --exclude '*.py' --exclude '*.json' \
    "$HOME_SRC/" "$HOST:/tmp/dep-home-$TS/"
fi

# ── 적용 · 확인 · 실패 시 되돌리기 ────────────────────────────────────────
say "적용 후 확인"
"${SSH[@]}" bash -se <<EOS
set -uo pipefail
APPLIED=0
if [ -d /tmp/dep-app-$TS ]; then
  sudo rsync -a --delete /tmp/dep-app-$TS/ $REMOTE_APP/
  sudo chown -R $OWNER $REMOTE_APP          # root 로 두면 서비스가 못 읽는다
  APPLIED=1
fi
if [ -d /tmp/dep-home-$TS ]; then
  sudo rsync -a /tmp/dep-home-$TS/ $REMOTE_HOME/
  sudo chown -R $OWNER $REMOTE_HOME
  sudo restorecon -R $REMOTE_HOME 2>/dev/null   # SELinux 라벨 — 없으면 nginx 가 403
fi
rm -rf /tmp/dep-app-$TS /tmp/dep-home-$TS

if [ "\$APPLIED" = 1 ]; then
  sudo systemctl restart $SVC
  ok=0
  for i in \$(seq 1 30); do
    code=\$(curl -s -o /dev/null -w '%{http_code}' $HEALTH || echo 000)
    if [ "\$code" = "200" ]; then ok=1; echo "  health 200 (\${i}s)"; break; fi
    sleep 1
  done
  if [ "\$ok" != 1 ]; then
    echo "  ❌ 기동 실패 — 되돌립니다"
    sudo journalctl -u $SVC -n 20 --no-pager | tail -12
    sudo tar xzf /opt/glhac-app-bak-$TS.tgz -C /opt/glhac
    sudo chown -R $OWNER $REMOTE_APP
    sudo systemctl restart $SVC; sleep 8
    echo "  되돌린 뒤 health: \$(curl -s -o /dev/null -w '%{http_code}' $HEALTH)"
    exit 1
  fi
fi
echo "  ui:   \$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8800/ui/)"
EOS

# ── 바깥에서 실제로 열리는지 ──────────────────────────────────────────────
say "── 공개 확인 ──"
for u in https://glhac.co.kr/ui/ https://glhac.com/ https://glhac.com/board/; do
  say "$u → $(curl -s -o /dev/null -w '%{http_code}' -m 20 "$u")"
done
say "배포 완료 (백업 태그 $TS · 되돌리려면 bash scripts/deploy.sh --rollback)"
