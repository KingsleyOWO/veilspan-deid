"""HTTP API and static front end. A job is reachable only through its id; there is no listing endpoint."""
import base64
import json
import logging
import re
import threading
import time
from collections import Counter
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Body, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from deid.config import Settings
from deid.detect import TYPES
from deid.formats.base import SUPPORTED, BrokenFile, UnsupportedFormat
from deid import replyfile, restorefile
from deid.jobs import MODES, JobStore, JobWriteError, TooLarge, WrongMode, write_failure_message
from deid.model import to_json
from deid.partial import PARTIAL_TYPES, applies
from deid.restore import restore
from deid.web.access import AllowedClients, parse_clients

log = logging.getLogger("deid")
STATIC = Path(__file__).parent / "static"
ALL_TYPES = TYPES + ["人臉", "證件", "簽名印章", "手寫個資", "病歷", "檔案屬性"]
ASSET = re.compile(r"[A-Za-z0-9_-]+\.(png|jpg|gif)")
MEDIA = {".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".pdf": "application/pdf",
         ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12"}
FORM_OVERHEAD = 64 * 1024  # multipart framing around the file itself
RESTORE_MAX = 16 * 1024 * 1024  # a restore request: the table and the reply (a text or a file) to put the strings back into
CSP = "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'"


def _error(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


class JobLocks:
    """One lock per job, held while a request changes the job's findings or makes its output. An entry lives only
    while a request holds or waits for it: two requests for one job always share it, a held lock is never
    forgotten, and an id that nobody uses any more (one in a URL for a job that does not exist, too) leaves the
    table, which so stays as small as the number of requests under way. (Emptying a full table instead let an
    edit run while the masked file of the same job was being made, from the list before the edit.)"""

    def __init__(self):
        self._guard = threading.Lock()
        self._entries: dict[str, list] = {}  # job id -> [lock, requests holding or waiting for it]

    @contextmanager
    def __call__(self, job_id: str):
        with self._guard:
            entry = self._entries.setdefault(job_id, [threading.Lock(), 0])
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._guard:
                entry[1] -= 1
                if not entry[1]:
                    del self._entries[job_id]

    def __len__(self) -> int:
        with self._guard:
            return len(self._entries)


def create_app(settings: Settings, store: JobStore | None = None) -> FastAPI:
    networks = parse_clients(settings.allowed_clients)  # an unreadable list stops the service here
    own_store = store is None
    store = store or JobStore(settings)
    job_lock = JobLocks()

    @asynccontextmanager
    async def lifespan(app):
        if own_store:
            store.start()
        replyfile.sweep()  # scratch of a restore that a crash interrupted
        yield
        if own_store:
            store.stop()

    app = FastAPI(title="去識別化", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = None
        if request.method == "POST" and request.url.path == "/api/jobs":
            # Refuse an oversized upload from its declared size, before any of it is received and stored.
            declared = request.headers.get("content-length", "")
            if not declared.isdigit():
                response = _error(411, "上傳時必須標明檔案大小")
            elif int(declared) > settings.max_upload_mb * 1024 * 1024 + FORM_OVERHEAD:
                response = _error(413, f"檔案超過 {settings.max_upload_mb} MB 上限")
        elif request.method == "POST" and request.url.path == "/api/restore":
            declared = request.headers.get("content-length", "")
            if not declared.isdigit():
                response = _error(411, "上傳時必須標明大小")
            elif int(declared) > RESTORE_MAX:
                response = _error(413, f"還原檔與回覆（文字或檔案）合計不可超過 {RESTORE_MAX // (1024 * 1024)} MB")
        if response is None:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = CSP
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(KeyError)
    async def not_found(request, exc):
        return _error(404, "找不到這個工作，可能已被刪除")

    def public(job: dict) -> dict:
        keys = ("id", "filename", "ext", "mode", "status", "stage", "progress", "error", "created", "downloaded_at", "result")
        return {k: job.get(k) for k in keys}

    def finding_json(ctx, f) -> dict:
        """The stored finding plus what the page shows about how it will be masked."""
        d = to_json(f)
        d["preview"] = ctx.store.shown_as(f, settings.mask_full, settings.mask_half)
        d["partial_ok"] = f.kind == "text" and f.type in PARTIAL_TYPES
        d["fits"] = d["partial_ok"] and applies(f.type, f.text)  # False: the rule cannot be applied, so it is masked whole
        d["custom_ok"] = ctx.store.can_customize(f)  # the reviewer may pick the masked characters (d["spans"])
        return d

    def masks(ctx) -> dict | None:
        """Per text unit, the character ranges that will really be masked. Only needed, and only sent, once
        something is masked in part: until then every marked place is masked whole."""
        if not any(f.style in ("partial", "custom") for f in ctx.store.findings):
            return None
        return ctx.store.plan().text

    # ---- jobs ----------------------------------------------------------------------------

    @app.post("/api/jobs")
    def create(file: UploadFile = File(...), mode: str = Form("mask"), lang: str = Form("zh-TW")):
        if mode not in MODES:
            return _error(400, "沒有這個功能")
        try:
            return {"id": store.create(file.filename or "", file.file, mode=mode, lang="en" if lang == "en" else "zh-TW")}
        except UnsupportedFormat:
            return _error(400, f"目前只支援 {'、'.join(SUPPORTED)}")
        except TooLarge:
            return _error(413, f"檔案超過 {settings.max_upload_mb} MB 上限")

    @app.get("/api/jobs/{job_id}")
    def status(job_id: str):
        return public(store.get(job_id))

    @app.post("/api/jobs/{job_id}/retry")
    def retry(job_id: str):
        try:
            store.retry(job_id)  # takes one of two requests at the same moment; the other finds the job queued
        except ValueError:
            return _error(409, "只有失敗的工作可以重試")
        except JobWriteError as e:  # not queued; the job's failure now says the same
            return _error(503, write_failure_message(e.cause))
        return {"ok": True}

    @app.delete("/api/jobs/{job_id}")
    def delete(job_id: str):
        store.delete(job_id)
        return {"ok": True}

    # ---- review --------------------------------------------------------------------------

    def loaded(job_id: str):
        job = store.get(job_id)
        if job["status"] != "review":
            return None, job
        return store.context(job_id), job

    @app.get("/api/jobs/{job_id}/review")
    def review(job_id: str):
        ctx, job = loaded(job_id)
        if ctx is None:
            return _error(409, "文件還沒處理完成")
        images = {}
        for im in ctx.inv.images:
            asset = ctx.asset_path(im.id) if im.supported else None
            images[im.id] = {"name": im.name, "width": im.width, "height": im.height, "supported": im.supported, "page": im.page,
                             "url": f"/api/jobs/{job_id}/assets/{asset.name}" if asset else None}
        listed = ctx.store.listed()
        on = Counter(f.type for f in listed if f.on)
        return {"job": public(job), "kind": ctx.inv.kind, "view": ctx.adapter.view(ctx.src, ctx.inv),
                "units": {t.id: {"text": t.text, "section": t.section} for t in ctx.inv.texts if not t.mirror},
                "images": images, "findings": [finding_json(ctx, f) for f in listed], "types": ALL_TYPES,
                "partial_types": list(PARTIAL_TYPES), "styles": dict(ctx.store.styles), "masks": masks(ctx),
                "warnings": ctx.warnings, "unmaskable": ctx.unmaskable,
                "unchecked": [{"name": u.name, "reason": u.reason} for u in ctx.inv.unchecked] + ctx.unread_items(),
                "log": ctx.log, "outcome": ctx.outcome, "names": ctx.outcome.get("names"),
                "summary": {"on": dict(on), "off": sum(1 for f in listed if not f.on)}}

    @app.get("/api/jobs/{job_id}/assets/{name}")
    def asset(job_id: str, name: str):
        store.get(job_id)
        if not ASSET.fullmatch(name):
            return _error(404, "找不到這張圖")
        path = store.root / job_id / "assets" / name
        if not path.is_file():
            return _error(404, "找不到這張圖")
        return FileResponse(path)

    def edit(job_id: str, change):
        """Load the findings, apply `change(ctx)`, save. Returns the handler's response."""
        with job_lock(job_id):
            ctx, _job = loaded(job_id)
            if ctx is None:
                return _error(409, "文件還沒處理完成")
            hidden = ctx.store.redundant()
            grouped = {f.id: f.group for f in ctx.store.findings}
            had_masks = masks(ctx) is not None
            out = change(ctx)
            if not isinstance(out, JSONResponse):
                # Either of these changes which entries exist, so the page fetches the list again.
                more = {"derived": ctx.add_derived()}  # spreadsheets: cells computed from what is masked now
                more["relist"] = ctx.store.redundant() != hidden  # agent strings inside a mask come and go with it
                # Switching off or retyping a name takes it out of its group, and can dissolve the group.
                more["relist"] = more["relist"] or any(grouped.get(f.id, "") != f.group for f in ctx.store.findings)
                more = {k: v for k, v in more.items() if v}
                now = masks(ctx)
                if had_masks or now is not None:
                    more["masks"] = now  # the page redraws which characters stay readable
                if more and (out is None or isinstance(out, dict)):
                    out = {**(out or {}), **more}
                store.save_findings(job_id, ctx.store)
            return out

    @app.patch("/api/jobs/{job_id}/findings/{fid}")
    def patch_finding(job_id: str, fid: str, body: dict = Body(...)):

        def change(ctx):
            try:
                f = ctx.store.get(fid)
            except KeyError:
                return _error(404, "找不到這個項目")
            if "type" in body:
                if body["type"] not in ALL_TYPES:
                    return _error(400, "沒有這個類型")
                ctx.store.set_type(fid, body["type"])
            try:
                if isinstance(body.get("occurrence"), dict):
                    ctx.store.set_on(fid, bool(body["occurrence"].get("on")), occurrence=int(body["occurrence"].get("index", -1)))
                if "on" in body:
                    ctx.store.set_on(fid, bool(body["on"]))
            except (ValueError, TypeError):
                return _error(400, "這一處不能單獨開關")
            if "style" in body:
                try:
                    ctx.store.set_style(fid, str(body["style"]))
                except ValueError:
                    return _error(400, "這個項目不能用這種遮法")
            if "spans" in body:
                if not ctx.store.can_customize(f):
                    return _error(400, "這個項目不能自訂遮法")
                try:
                    ctx.store.set_spans(fid, body["spans"])
                except ValueError:
                    return _error(400, "至少要遮一個字，而且要在這一筆的文字範圍內；一個字都不遮的話，請直接關掉這一筆")
            return finding_json(ctx, f)

        return edit(job_id, change)

    @app.post("/api/jobs/{job_id}/findings/bulk")
    def bulk(job_id: str, body: dict = Body(...)):
        style = body.get("style")
        if not (body.get("type") or body.get("source")) or ("on" not in body and style is None):
            return _error(400, "需要 type 或 source，以及 on 或 style")
        if style is not None and not body.get("type"):
            return _error(400, "遮法要指定類型")

        def change(ctx):
            try:
                return {"changed": ctx.store.bulk(type=body.get("type"), source=body.get("source"),
                                                  on=bool(body["on"]) if "on" in body else None, style=style)}
            except ValueError:
                return _error(400, "這一類不能用這種遮法")

        return edit(job_id, change)

    @app.post("/api/jobs/{job_id}/findings")
    def add_finding(job_id: str, body: dict = Body(...)):
        kind = body.get("type") if body.get("type") in ALL_TYPES else "其他"

        def change(ctx):
            if body.get("kind") == "region":
                im = ctx.inv.image(str(body.get("image", "")))
                box = body.get("box")
                if im is None or not im.supported or not isinstance(box, list) or len(box) != 4:
                    return _error(422, "找不到這張圖，或範圍格式不對")
                try:
                    x0, y0, x1, y1 = (float(v) for v in box)
                except (TypeError, ValueError):
                    return _error(422, "找不到這張圖，或範圍格式不對")
                x0, x1 = sorted((max(0.0, min(x0, im.width)), max(0.0, min(x1, im.width))))
                y0, y1 = sorted((max(0.0, min(y0, im.height)), max(0.0, min(y1, im.height))))
                if x1 - x0 < 3 or y1 - y0 < 3:
                    return _error(422, "範圍太小")
                return finding_json(ctx, ctx.store.add_region(im.id, (x0, y0, x1, y1), kind, "user"))
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                return _error(422, "文件中找不到這段文字")
            only = None
            if not body.get("all", True):
                try:
                    only = (str(body["unit"]), int(body["start"]), int(body["end"]))
                except (KeyError, TypeError, ValueError):
                    return _error(422, "文件中找不到這段文字")
            f = ctx.store.add_text(text if only else text.strip(), kind, "user", only=only)
            if f is None:
                return _error(422, "文件中找不到這段文字")
            return finding_json(ctx, f)

        return edit(job_id, change)

    @app.post("/api/jobs/{job_id}/findings/{fid}/group")
    def same_person(job_id: str, fid: str, body: dict = Body(...)):
        """The code function: say that two names are one person ({"with": id}), or take a name out of its
        group ({"with": null}). Names of one person share a number in the coded text."""
        if store.get(job_id)["mode"] != "code":
            return _error(400, "只有「代號＋還原」可以指定同一人")
        if "with" not in body:
            return _error(400, "需要指定和哪一筆是同一人")

        def change(ctx):
            try:
                mine = ctx.store.get(fid)
                if body["with"] is None:
                    ctx.store.unlink(fid)
                    return {"relist": True}
                other = ctx.store.get(str(body["with"]))
            except KeyError:
                return _error(404, "找不到這個項目")
            if mine.group and other.group and mine.group != other.group:
                return _error(400, "這兩筆各自屬於不同的組，請先把其中一筆移出原本的組")
            try:
                ctx.store.link([mine.id, other.id])
            except ValueError:
                return _error(400, "只有啟用中的姓名可以指定為同一人")
            return {"relist": True}

        return edit(job_id, change)

    @app.delete("/api/jobs/{job_id}/findings/{fid}")
    def delete_finding(job_id: str, fid: str):
        def change(ctx):
            try:
                ctx.store.remove(fid)
            except KeyError:
                return _error(404, "找不到這個項目")
            except ValueError:
                return _error(400, "只有手動新增的項目可以刪除；其他項目請關閉")
            return {"ok": True}

        return edit(job_id, change)

    # ---- output --------------------------------------------------------------------------

    @app.post("/api/jobs/{job_id}/apply")
    def apply(job_id: str):
        store.get(job_id)
        with job_lock(job_id):
            try:
                result = store.apply(job_id)
            except ValueError:
                return _error(409, "文件還沒處理完成")
            except WrongMode:
                return _error(409, "這份文件是用「代號＋還原」上傳的，沒有遮蔽檔")
            except KeyError:
                raise
            except Exception as e:  # noqa: BLE001 - the class name tells the user enough; details may quote content
                log.error("job %s apply failed: %s", job_id, type(e).__name__)
                return _error(500, f"產生遮蔽檔失敗（{type(e).__name__}）。原檔可能已損壞，請重新上傳。")
        return {"ok": result.ok, "residue": result.residue, "problems": result.problems, "stats": result.stats,
                "notes": result.notes}

    @app.post("/api/jobs/{job_id}/export")
    def export(job_id: str, lang: str = "zh-TW"):
        """The code function: make the text with codes and the table that maps them back. No password: a body
        sent by a page loaded before 2026-11-19 (it carried one) is not read."""
        store.get(job_id)
        with job_lock(job_id):
            try:
                return store.export(job_id, lang="en" if lang == "en" else "zh-TW")
            except ValueError:
                return _error(409, "文件還沒處理完成")
            except WrongMode:
                return _error(409, "這份文件是用「遮蔽」上傳的，沒有代號版文字")
            except KeyError:
                raise
            except Exception as e:  # noqa: BLE001 - the class name tells the user enough; details may quote content
                log.error("job %s export failed: %s", job_id, type(e).__name__)
                return _error(500, f"產生代號版失敗（{type(e).__name__}）。")

    @app.get("/api/jobs/{job_id}/download")
    def download(job_id: str, ack: str = "", what: str = "file", lang: str = "", batch: float | None = None):
        job = store.get(job_id)
        if what not in ("file", "text", "restore", "coded"):
            return _error(400, "沒有這種下載")
        result = job.get("result")
        if batch is not None and (result or {}).get("made") != batch:
            return _error(409, "This export has changed. Generate and download both files again." if lang == "en"
                          else "代號版已更新，請重新產生並下載這一批的代號版和還原檔")
        export_lang = (result or {}).get("lang", "zh-TW")
        if what == "coded" and result is not None and job["mode"] == "code" and not result.get("file"):
            return _error(409, "這份文件沒有代號版檔案，請下載代號版文字")
        try:
            path = store.output_path(job_id) if what == "file" else store.export_path(job_id, what, lang=export_lang)
        except FileNotFoundError:
            result = None
        if result is None or (what == "file") != (job["mode"] == "mask"):
            return _error(409, "請先產生遮蔽檔" if job["mode"] == "mask" else "請先產生代號版文字")
        if what != "restore" and not result.get("ok") and ack != "1":
            return _error(409, "仍有殘留或問題，需確認後才能下載", needs_ack=True)
        # What leaves the building is named by a time only: a document's own name often holds a person's name,
        # and nothing checks it. The time is when this output was made, so the coded text and the table of one
        # making carry the same name and differ from any made before (the same string can get another code).
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(result.get("made") or job["created"]))
        restore_prefix, coded_prefix = ("restore", "coded") if (lang or export_lang) == "en" else ("還原檔", "代號版")
        if what == "restore":  # downloading the table alone does not start the deletion clock
            name, plain, media = f"{restore_prefix}_{stamp}.deidr", "restore.deidr", "application/octet-stream"
        elif what == "text":
            store.mark_downloaded(job_id)
            name, plain, media = f"{coded_prefix}_{stamp}.txt", "coded.txt", "text/plain; charset=utf-8"
        elif what == "coded":  # the document with the codes in it: named like the coded text made with it
            store.mark_downloaded(job_id)
            ext = job["ext"]
            name, plain, media = f"{coded_prefix}_{stamp}{ext}", f"coded{ext}", MEDIA.get(ext, "application/octet-stream")
        else:
            store.mark_downloaded(job_id)
            ext = job["ext"]
            name, plain, media = "masked_" + job["filename"], f"masked{ext}", MEDIA.get(ext, "application/octet-stream")
        disposition = f"attachment; filename=\"{plain}\"; filename*=UTF-8''{quote(name, safe='')}"
        return FileResponse(path, media_type=media, headers={"Content-Disposition": disposition})

    @app.get("/api/jobs/{job_id}/report")
    def report(job_id: str):
        ctx, job = loaded(job_id)
        if ctx is None:
            return _error(409, "文件還沒處理完成")
        data = {"file": job["filename"], "outcome": ctx.outcome,
                "findings": ctx.store.report(settings.mask_full, settings.mask_half),
                "warnings": ctx.warnings, "unmaskable": ctx.unmaskable,
                "unchecked": [{"name": u.name, "reason": u.reason} for u in ctx.inv.unchecked] + ctx.unread_items(), "verify": job.get("result")}
        return Response(json.dumps(data, ensure_ascii=False, indent=1), media_type="application/json",
                        headers={"Content-Disposition": "attachment; filename=\"report.json\""})

    # ---- restoring -----------------------------------------------------------------------

    @app.post("/api/restore")
    def restore_text(key: UploadFile = File(...), text: UploadFile | None = File(None), file: UploadFile | None = File(None),
                     password: str = Form(""), lang: str = Form("zh-TW")):
        """Put the strings back into what a cloud model answered: a text that was pasted (`text`), or a Word,
        PDF or Excel file it handed back (`file`). A file is read as text; a Word or Excel file also comes back
        in its own format with the strings in place. No job is made and the log gets counts only. A pasted text
        stays in memory; a file is on disk only while it is read (deid.replyfile).
        A password is needed only for a restore file made before 2026-11-19."""
        try:
            table = restorefile.open_(key.file.read(RESTORE_MAX), password)
        except restorefile.NotRestoreFile:
            return _error(400, "這不是本系統產生的還原檔")
        except restorefile.NeedsPassword:
            return _error(400, "這個還原檔是之前用密碼加密的，請輸入當時設定的密碼", needs_password=True)
        except restorefile.WrongKey:
            return _error(400, "密碼不對，或還原檔已損毀")
        if text is not None and file is not None:
            return _error(400, "貼上文字和上傳檔案請擇一")
        if text is None and file is None:
            return _error(400, "請貼上雲端的回覆，或選擇雲端給的檔案")
        if file is not None:
            try:
                done = replyfile.restore_file(file.file.read(RESTORE_MAX), file.filename or "", table, settings, lang=lang)
            except UnsupportedFormat:
                return _error(400, "只能上傳 Word（.docx）、PDF（.pdf）或 Excel（.xlsx、.xlsm）")
            except BrokenFile as e:
                return _error(400, str(e))
            except replyfile.NoText:
                return _error(400, "這個檔案讀不到文字（可能是掃描檔，或內容都在圖片裡）。請開啟檔案，把文字複製後貼上")
            log.info("restore: %s, %d replaced, %d unknown, %d unused, file %s", done.ext, done.replaced, len(done.unknown),
                     len(done.unused), "made" if done.file else "not made")
            out = {"text": done.text, "replaced": done.replaced, "unknown": done.unknown, "unused": done.unused, "notes": done.notes}
            if done.file is not None:  # the reply in its own format, strings in place; kept nowhere on the server
                out.update(file_b64=base64.b64encode(done.file).decode("ascii"), file_ext=done.ext)
            return out
        try:
            reply = text.file.read(RESTORE_MAX).decode("utf-8")
        except UnicodeDecodeError:
            return _error(400, "文字必須是 UTF-8 編碼")
        done = restore(reply, table)
        log.info("restore: text, %d replaced, %d unknown, %d unused", done.replaced, len(done.unknown), len(done.unused))
        return {"text": done.text, "replaced": done.replaced, "unknown": done.unknown, "unused": done.unused, "notes": []}

    # ---- front end -----------------------------------------------------------------------

    @app.get("/favicon.ico")
    def favicon():
        return Response(status_code=204)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", media_type="text/html; charset=utf-8")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    if networks:
        app.add_middleware(AllowedClients, networks=networks)  # added last, so it runs before everything else
    return app
