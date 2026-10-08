#!/bin/bash
# 서버에서 실행: GLHAC_UI_DEFAULT 를 v4 로 전환(on)하거나 해제(off)한다.
# 사용법: bash /opt/glhac/scripts/ui_v4_on_remote.sh [off]
set -e
ENV=/opt/glhac/.env
MODE="${1:-on}"
STAMP="$(date +%Y%m%d%H%M)"

# .env 백업
sudo cp "$ENV" "$ENV.bak-ui-$(date +%Y%m%d%H%M%S)"

if [ "$MODE" = "off" ]; then
  # 해제 모드: 활성 v4 줄을 주석 처리
  if grep -qE '^GLHAC_UI_DEFAULT=' "$ENV"; then
    sudo sed -i -E "s|^GLHAC_UI_DEFAULT=.*|# GLHAC_UI_DEFAULT=v4|" "$ENV"
    sudo sed -i "/^# GLHAC_UI_DEFAULT=v4\$/i # $STAMP 기본화면 v4 해제(구 화면으로 복귀)" "$ENV"
  else
    echo "이미 꺼짐"
  fi
else
  # 전환 모드
  if grep -qE '^GLHAC_UI_DEFAULT=v4[[:space:]]*$' "$ENV"; then
    echo "이미 켜짐"
  elif grep -qE '^#?[[:space:]]*GLHAC_UI_DEFAULT=' "$ENV"; then
    sudo sed -i -E "s|^#?[[:space:]]*GLHAC_UI_DEFAULT=.*|GLHAC_UI_DEFAULT=v4|" "$ENV"
    sudo sed -i "/^GLHAC_UI_DEFAULT=v4\$/i # $STAMP 기본화면 v4 전환(구 화면 \/ui\/index.html 유지)" "$ENV"
  else
    printf '# %s 기본화면 v4 전환(구 화면 /ui/index.html 유지)\nGLHAC_UI_DEFAULT=v4\n' "$STAMP" | sudo tee -a "$ENV" >/dev/null
  fi
fi

grep -n UI_DEFAULT "$ENV"
sudo systemctl restart glhac-app
sleep 4
if [ "$(systemctl is-active glhac-app)" != "active" ]; then
  sudo journalctl -u glhac-app -n 30 --no-pager
  exit 1
fi

REDIR_ROOT="$(curl -s -o /dev/null -w "%{redirect_url}" http://127.0.0.1:8800/)"
echo "root → $REDIR_ROOT"
curl -s -o /dev/null -w "ui/?ref=X → %{redirect_url}\n" "http://127.0.0.1:8800/ui/?ref=X"
curl -s -o /dev/null -w "ui/index.html(구 화면) %{http_code}\n" http://127.0.0.1:8800/ui/index.html
curl -s http://127.0.0.1:8800/health; echo

if [ "$MODE" = "off" ]; then
  # 해제 모드: 리다이렉트가 비어있거나 /ui/ 이면 정상
  case "$REDIR_ROOT" in
    ""|*/ui/) : ;;
    *) echo "경고: 리다이렉트가 구 화면(/ui/)이 아님"; exit 1 ;;
  esac
else
  # 전환 모드: 리다이렉트가 /ui/v4/ 로 끝나야 정상
  case "$REDIR_ROOT" in
    */ui/v4/) : ;;
    *) echo "경고: 리다이렉트가 v4 가 아님"; exit 1 ;;
  esac
fi
