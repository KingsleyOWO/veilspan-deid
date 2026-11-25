#!/usr/bin/env bash
# 安裝：建立 .venv、安裝套件、下載人臉偵測模型、預熱 OCR 模型、建立腳本隔離映像。
set -euo pipefail
cd "$(dirname "$0")/.."
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt -c requirements.lock
mkdir -p models
if [ ! -s models/yunet.onnx ]; then
  curl -sL --max-time 120 -o models/yunet.onnx \
    https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
fi
# 第一次使用時 RapidOCR 會下載 PP-OCRv6 模型；在這裡先做掉。
.venv/bin/python -I -c "
from rapidocr import RapidOCR, OCRVersion
RapidOCR(params={'Det.ocr_version': OCRVersion.PPOCRV6, 'Rec.ocr_version': OCRVersion.PPOCRV6})
print('OCR 模型就緒')"
if [ -f sandbox/Dockerfile ] && command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  docker build -q -t deid-sandbox:1 sandbox/ && echo "腳本隔離映像就緒"
else
  echo "略過腳本隔離映像（沒有 Docker 或 Dockerfile）；run_python 工具會停用"
fi
[ -f .env ] || { cp .env.example .env; echo "已建立 .env，請填入 LLM_API_KEY"; }
echo "完成"
