# VeilSpan

Self-hosted redaction and reversible masking for Traditional Chinese documents.

English · [繁體中文](README.zh-TW.md)

VeilSpan finds sensitive information in Word, Excel, and PDF files. Review the findings before exporting, choose how much to redact, or replace selected values with placeholders for a cloud model and restore them locally afterward.

![VeilSpan home page in English with the Redaction and Placeholders workflows](docs/images/en-home.png)

## Why I built it

I started with Presidio. In my setup, legitimate street names, model numbers, and organization names were often redacted from Taiwanese documents. I built VeilSpan so I could use document context to review findings and keep the original file format when sharing the result.

The agent can inspect document structure, search for alternate spellings, and flag uncertain findings. Rules cover common identifiers and the user approves the findings. The export applies those selections and checks the output for remaining matches.

## Redact a document

Upload a file, review the findings, and choose **full**, **partial**, or **custom** redaction for individual values. You can add missed text or select a region of an image or PDF page. The redacted download keeps the file format.

![An English workbook in the review screen with full and partial redaction controls](docs/images/en-review.png)

VeilSpan can also redact text or selected regions inside embedded images. In this Traditional Chinese email screenshot, black boxes cover the sender and recipient details.

![An email screenshot with black boxes covering the sender and recipient details](docs/images/image-redaction.png)

## Use placeholders and restore a cloud reply

1. Choose **Placeholders + restore**, upload a file, and review the findings. Download the copy with placeholders and the `.deidr` restore key from the same export.
2. Share the reviewed copy with your cloud model. Keep the restore key local. It contains the original values in plaintext. The optional redaction report also contains original values.
3. Paste the model’s reply or upload its file in VeilSpan and select the matching restore key. VeilSpan fills in the original values while retaining the other changes in the reply.

> Keep bracketed placeholders such as `[NAME1]`, `[NAME1b]`, and `[EMAIL1]` exactly as written.

![VeilSpan downloads for the workbook with placeholders and the matching restore key](docs/images/en-placeholders.png)

![A fictional cloud reply with placeholders and the values restored locally](docs/images/en-restore.png)

The language selector changes the interface and new placeholder exports. English uses codes such as `[NAME1]` and `[EMAIL1]`. Traditional Chinese uses `[姓名1]` and `[電子郵件1]`. Restore uses the codes stored in the original export’s key in either interface language.

## Formats and limits

| Input | Redacted download | Placeholders and restore |
| --- | --- | --- |
| Word (`.docx`) | Word | Word or plain text |
| Excel (`.xlsx`, `.xlsm`) | Excel | Excel or plain text |
| PDF, including scanned pages for redaction | PDF | Plain text |

VeilSpan is designed for Traditional Chinese and Taiwanese identifiers. It also handles examples such as English names and email addresses. Phone, identity number, and partial redaction rules are primarily tailored to Taiwan.

Some fields, such as Word ChartEx charts, Excel chart print headers, and PDF form field names and tooltips, are outside the current detection coverage and may be skipped without a warning. In Word and Excel placeholder files, images and some hidden content are redacted permanently. PDF replies with image-only content need to be pasted as text. Review the findings and output before sharing. [Detailed usage and limits (Traditional Chinese)](docs/usage.md)

## Run locally

Use Linux with Python 3.12 and an OpenAI-compatible model endpoint. Tool calling enables the agent workflow, and image input enables model-assisted picture review. Setup downloads the OCR and face detection models.

```bash
./scripts/setup.sh
```

Set `LLM_BASE_URL`, `LLM_MODEL`, and, if required, `LLM_API_KEY` in the generated `.env` file. The example binds the web service to loopback. Original document content goes to the configured model endpoint. Use a local or private endpoint if it needs to stay within your environment.

```bash
./run.sh
```

Open `http://127.0.0.1:8620/?lang=en`. For Docker, run `./scripts/docker-up.sh` after setting up `.env`. This option uses the host Docker socket to start read-only document inspection containers. The app has no user authentication, so deploy it in a trusted environment.

## Test and contribute

```bash
DEID_SKIP_LIVE=1 .venv/bin/python -m pytest
```

The suite covers file parsing, redaction, placeholder export, restore, and the web API. Docker and browser tests use a local sandbox image and Chromium where available.

I welcome reproducible reports of false positives, missed findings, and layout issues, especially for Traditional Chinese documents. Please use fictional examples and include what you expected to be redacted or retained.

## License

[AGPL-3.0-only](LICENSE). [Third-party notices](THIRD_PARTY_NOTICES.md) cover dependencies and the OCR and face detection models.
