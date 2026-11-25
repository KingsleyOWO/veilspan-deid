# 第三方元件與授權

套件版本見 [requirements.lock](requirements.lock)。以下授權適用於各第三方元件及模型。

## Python 套件

| 授權 | 元件 |
|---|---|
| GNU AGPL 3.0 或 Artifex 商業授權 | PyMuPDF（含 MuPDF）。[PyMuPDF 授權](https://pymupdf.io/licensing) |
| MIT | annotated-doc、annotated-types、anyio、charset-normalizer、colorlog、FastAPI、h11、iniconfig、ONNX Runtime、pluggy、pyclipper、Pydantic、pydantic-core、pytest、PyYAML、six、tabulate、typing-inspection、urllib3 |
| BSD-3-Clause | click、httpcore、httpx、idna、NetworkX、OmegaConf、protobuf、psutil、pycparser、Shapely、Starlette、Uvicorn |
| Apache-2.0 | FlatBuffers、opentelemetry-api、python-multipart、RapidOCR、Requests、OpenCV。OpenCV 的 Python 打包腳本另採 MIT。[OpenCV 授權](https://github.com/opencv/opencv-python#licensing) |
| 其他寬鬆授權 | cffi（MIT-0）、Pillow（MIT-CMU）、Pygments（BSD-2-Clause）、typing-extensions（PSF-2.0） |
| 複合授權 | certifi（MPL-2.0）、tqdm（MPL-2.0 與 MIT）、cryptography（Apache-2.0 或 BSD-3-Clause）、packaging（Apache-2.0 或 BSD-2-Clause）、regex（Apache-2.0 與 CNRI-Python）、NumPy（BSD-3-Clause 與 0BSD、MIT、Zlib、CC0-1.0 元件） |

文件檢查用的 [隔離映像](sandbox/Dockerfile)另外使用 `openpyxl`、`et_xmlfile`、`python-docx`（MIT）、`lxml`（BSD-3-Clause）及 `olefile`（BSD-2-Clause，附 PIL 聲明）。[olefile 授權](https://olefile.readthedocs.io/en/latest/License.html)

隔離映像使用 PyMuPDF 2.1 讀取文件，其安裝也會帶入 `pymupdf-layout` 和 `pymupdf4llm`。[PyMuPDF 發行說明](https://pymupdf.readthedocs.io/en/latest/changes.html)

## 辨識模型

| 模型 | 授權與來源 |
|---|---|
| PP-OCRv6 small 文字偵測及辨識、PP-OCRv4 的文字方向分類模型 | Baidu／PaddleOCR 上游模型與 RapidOCR 提供的 ONNX 權重採 Apache-2.0。檔名與雜湊可查 RapidOCR 套件內的 `MODEL_LICENSES.md`。[RapidOCR 模型授權](https://github.com/RapidAI/RapidOCR#models) |
| YuNet `face_detection_yunet_2023mar.onnx` | Copyright © 2020 Shiqi Yu，MIT。[YuNet 授權](https://github.com/opencv/opencv_zoo/blob/main/models/face_detection_yunet/LICENSE) |

語言模型由使用者設定的端點提供，依實際選用的模型與服務條款使用。

## 容器及原生函式庫

兩個容器使用 `python:3.12-slim`。服務映像還包含 Docker CLI（Apache-2.0）。Python wheels 可能附帶原生元件，例如 OpenCV 附帶的 FFmpeg（LGPL）和 Shapely 附帶的 GEOS（LGPL）。再發行預建映像時，請依實際建置版本確認 Python wheels、原生函式庫、基底映像與模型的授權，保留所需聲明並提供適用的對應原始碼取得方式。 [Docker CLI](https://github.com/docker/cli#licensing) · [FFmpeg 授權](https://ffmpeg.org/legal.html) · [Shapely／GEOS 授權](https://shapely.readthedocs.io/en/latest/#copyright-license)
