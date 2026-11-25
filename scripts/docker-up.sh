#!/usr/bin/env bash
# 用 Docker 啟動服務：建立兩個映像（服務本體、腳本隔離環境）後在背景啟動，開機會自動起來。
# 停止：docker compose down　　看紀錄：docker logs -f deid
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "找不到 .env：請先 cp .env.example .env 並填入 LLM_API_KEY"; exit 1; }
if ! grep -Eq '^ALLOWED_CLIENTS=[^[:space:]]' .env; then
  echo "請先在 .env 設定 ALLOWED_CLIENTS（允許連線的位址，用逗號分隔），例如："
  echo "  ALLOWED_CLIENTS=127.0.0.1,192.0.2.166"
  exit 1
fi
mkdir -p data
export DEID_UID="$(id -u)" DEID_GID="$(id -g)"
export DOCKER_GID="$(stat -c %g /var/run/docker.sock)"
export DEID_DATA="$(realpath data)"
docker build -q -t deid-sandbox:1 sandbox/ >/dev/null && echo "腳本隔離映像就緒"
docker compose up -d --build "$@"
echo "已啟動。允許連線的位址：$(grep -E '^ALLOWED_CLIENTS=' .env | cut -d= -f2-)"
