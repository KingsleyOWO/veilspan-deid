#!/usr/bin/env bash
# 啟動網頁服務（設定見 .env）
cd "$(dirname "$0")"
exec .venv/bin/python -m deid serve "$@"
