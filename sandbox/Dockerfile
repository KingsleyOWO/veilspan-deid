# 模型自己寫的檢查腳本在這個映像裡執行：無網路、唯讀、非 root（執行參數見 deid/agent/sandbox.py）。
FROM python:3.12-slim
RUN pip install --no-cache-dir pymupdf==2.1 pillow openpyxl python-docx lxml olefile
RUN python -m pip install --no-cache-dir --upgrade pip
ENV PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
USER 65534:65534
WORKDIR /tmp
