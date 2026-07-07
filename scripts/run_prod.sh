#!/bin/zsh
# glhac-ai-v3 프로덕션 기동 (launchd에서 호출). .env를 소싱해 uvicorn을 8800에 띄운다.
set -e
cd "$(dirname "$0")/.."          # → glhac-ai-v3 루트
if [ -f .env ]; then
  set -a; source .env; set +a
fi
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8800 --log-level info
