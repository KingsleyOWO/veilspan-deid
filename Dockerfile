# 去識別化服務本體。模型寫的檢查腳本不在這個容器裡跑，而是由它透過主機的 Docker 另開隔離容器
# （sandbox/Dockerfile、deid/agent/sandbox.py）。啟動方式見 docker-compose.yml 與 scripts/docker-up.sh。
FROM docker:29.2.1-cli AS dockercli

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 HOME=/tmp
WORKDIR /app
# requirements.lock 是測試通過時實際安裝的版本，映像與測試環境一致。
COPY requirements.txt requirements.lock ./
RUN pip install -r requirements.txt -c requirements.lock
RUN python -m pip install --upgrade pip
# 模型在建置時下載，執行時不連外：OCR（PP-OCRv6）放在套件目錄，人臉偵測放在 models/。
RUN python -I -c "from rapidocr import RapidOCR, OCRVersion; \
RapidOCR(params={'Det.ocr_version': OCRVersion.PPOCRV6, 'Rec.ocr_version': OCRVersion.PPOCRV6})"
RUN mkdir models && python -I -c "import hashlib, urllib.request; \
data = urllib.request.urlopen('https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx', timeout=120).read(); \
assert hashlib.sha256(data).hexdigest() == '8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4', 'yunet.onnx 內容不符'; \
open('models/yunet.onnx', 'wb').write(data)"
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY deid ./deid
USER 65534:65534
EXPOSE 8620
# 有回應就算活著：不在允許清單內時得到 403，也代表服務正常。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["python", "-I", "-c", "import os, urllib.request, urllib.error\ntry:\n    urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8620') + '/', timeout=4)\nexcept urllib.error.HTTPError:\n    pass"]
CMD ["python", "-m", "deid", "serve"]
