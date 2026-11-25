"""Jobs on disk: one folder per upload, one background worker, automatic deletion.
There is deliberately no way to list jobs: a job can only be reached by its unguessable id."""
import errno
import json
import logging
import os
import queue
import re
import secrets
import shutil
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import BinaryIO

from deid import codefile, codes, restorefile
from deid.agent.context import JobStopped
from deid.config import Settings
from deid.findings import FindingStore
from deid.formats.base import SUPPORTED, BrokenFile, UnsupportedFormat
from deid.llm import LLMError
from deid.pipeline import VerifyResult, analyze, apply_and_verify, build_context

log = logging.getLogger("deid")  # ids, stages, timings and counts only — never file names or content
JOB_ID = re.compile(r"[A-Za-z0-9_-]{20,64}")
CHUNK = 1024 * 1024


class TooLarge(Exception):
    pass


class WrongMode(Exception):
    """The job was uploaded for the other function (masking a file, or making a coded text)."""


class JobWriteError(Exception):
    """job.json could not be written (full disk, I/O error). That is not the same as the job having been deleted."""

    def __init__(self, cause: OSError):
        super().__init__(cause)
        self.cause = cause


DISK_FULL = (errno.ENOSPC, errno.EDQUOT)


def write_failure_message(cause: OSError | None) -> str:
    """What the user is told when the server could not save a job's state: what to do, not what broke."""
    if cause is not None and cause.errno in DISK_FULL:
        return "伺服器的磁碟空間不足，沒辦法儲存處理中的資料。請通知管理員清出空間後，再按「重試」。"
    return "伺服器寫入檔案失敗，沒辦法儲存處理進度。請稍後按「重試」；一直發生的話，請通知管理員。"


MODES = ("mask", "code")
# The way a document is read into units, as a number. Findings are saved by unit number and position, so what a
# job saved under another number no longer fits what this code reads: such a job is analysed again when the
# service starts (JobStore.start). Raise it whenever a format reader lists other units for the same file.
# 2: hidden parts of Word and PDF files that were not read before, an Excel header without its format codes,
#    embedded files by the paragraph (2026-11-24).
ANALYSIS = 2
REANALYSED = "系統更新後，這份文件已重新分析；先前在清單上做的增減沒有保留，請重新檢查。"
EXPORTS = {"text": "export.txt", "restore": "export.restore"}


def _export_name(ext: str, what: str, lang: str) -> str:
    name = f"coded{ext}" if what == "coded" else EXPORTS[what]
    stem, suffix = name.rsplit(".", 1)
    return f"{stem}-en.{suffix}" if lang == "en" else name


class JobStore:
    def __init__(self, settings: Settings, *, context_factory=build_context):
        self.settings = settings
        self.root = Path(settings.data_dir) / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        for folder in (Path(settings.data_dir), self.root):  # uploads hold personal data: owner only
            os.chmod(folder, 0o700)
        self._factory = context_factory
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.RLock()
        self._apply_lock = threading.Lock()
        self._unsaved: dict[str, dict] = {}  # a failure that could not be written to job.json, kept so the page still shows it
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # ---- files ---------------------------------------------------------------------------

    def _dir(self, job_id: str) -> Path:
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise KeyError(job_id)
        path = self.root / job_id
        if not (path / "job.json").is_file():
            raise KeyError(job_id)
        return path

    def _read(self, job_id: str) -> dict:
        with self._lock:
            try:
                job = json.loads((self._dir(job_id) / "job.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise KeyError(job_id) from None
            job.update(self._unsaved.get(job_id, {}))
            return job

    def _update(self, job_id: str, **changes) -> dict | None:
        """Returns None if the job was deleted in the meantime. Raises JobWriteError if job.json cannot be
        written: the job is still there then, and the caller decides what that means."""
        with self._lock:
            try:
                job = self._read(job_id)
            except KeyError:
                return None
            job.update(changes)
            job["updated"] = time.time()
            path = self.root / job_id / "job.json"
            tmp = path.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
                tmp.replace(path)
            except OSError as e:
                try:
                    tmp.unlink(missing_ok=True)  # a half-written file would only take more of a full disk
                except OSError:
                    pass
                if not path.parent.is_dir():
                    return None  # the folder went while this was being written: the job was deleted
                log.error("job %s: job.json could not be written: %s %s (%s)", job_id, type(e).__name__,
                          errno.errorcode.get(e.errno, e.errno), e.strerror)
                raise JobWriteError(e) from e
            self._unsaved.pop(job_id, None)  # whatever was remembered is in the file now
            return job

    def _try_update(self, job_id: str, **changes) -> dict | None:
        """For bookkeeping that must not fail its caller: a write error is logged (by _update) and the job left as it was."""
        try:
            return self._update(job_id, **changes)
        except JobWriteError:
            return None

    def source_path(self, job_id: str) -> Path:
        return self._dir(job_id) / f"original{self._read(job_id)['ext']}"

    # ---- lifecycle -----------------------------------------------------------------------

    def create(self, filename: str, stream: BinaryIO, mode: str = "mask", *, lang: str = "zh-TW") -> str:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        # Browsers write a double quote in an uploaded file's name as %22; turn it back.
        name = Path((filename or "").replace("%22", '"')).name
        ext = Path(name).suffix.lower()
        if ext not in SUPPORTED:
            raise UnsupportedFormat(ext)
        job_id = secrets.token_urlsafe(16)
        folder = self.root / job_id
        folder.mkdir(parents=True, mode=0o700)
        limit = self.settings.max_upload_mb * 1024 * 1024
        size = 0
        try:
            with open(os.open(folder / f"original{ext}", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
                while chunk := stream.read(CHUNK):
                    size += len(chunk)
                    if size > limit:
                        raise TooLarge()
                    out.write(chunk)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        job = {"id": job_id, "filename": name, "ext": ext, "mode": mode, "lang": lang, "status": "queued", "stage": "排隊中", "progress": 0.0,
               "error": None, "created": time.time(), "updated": time.time(), "downloaded_at": None, "result": None}
        with open(os.open(folder / "job.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as f:
            f.write(json.dumps(job, ensure_ascii=False))
        log.info("job %s created: %s, %s, %d bytes", job_id, ext, mode, size)
        self._queue.put(job_id)
        return job_id

    def get(self, job_id: str) -> dict:
        job = self._read(job_id)
        job.setdefault("mode", "mask")  # jobs created before there were two functions
        return job

    def context(self, job_id: str):
        folder = self._dir(job_id)
        return self._factory(self.source_path(job_id), folder, self.settings, restore=True)

    def save_findings(self, job_id: str, store: FindingStore) -> None:
        folder = self._dir(job_id)
        with self._lock:
            tmp = folder / "findings.json.tmp"
            tmp.write_text(json.dumps(store.to_json(), ensure_ascii=False), encoding="utf-8")
            tmp.replace(folder / "findings.json")
            for old in (*folder.glob("output.*"), *folder.glob("coded.*"), *folder.glob("coded-en.*"),
                        *folder.glob("export.*"), *folder.glob("export-en.*")):
                old.unlink(missing_ok=True)  # the masked file, or the coded text, file and table, no longer match the findings
            self._try_update(job_id, result=None)

    def apply(self, job_id: str) -> VerifyResult:
        job = self.get(job_id)
        if job["status"] != "review":
            raise ValueError("job is not ready for masking")
        if job["mode"] != "mask":
            raise WrongMode(job["mode"])
        started = time.time()
        with self._apply_lock:
            result = apply_and_verify(self.context(job_id))
        self._try_update(job_id, result={**asdict(result), "made": time.time()})
        log.info("job %s applied in %.1fs: ok=%s residue=%d problems=%d spans=%d boxes=%d", job_id, time.time() - started,
                 result.ok, len(result.residue), len(result.problems), result.stats.get("text_spans", 0),
                 result.stats.get("boxes", 0))
        return result

    def export(self, job_id: str, *, lang: str = "zh-TW") -> dict:
        """The code function's output: the text with codes, the table that maps them back and, for a Word or
        Excel document, the document itself with the codes in it (deid.codefile). The table is written as it
        is, without a password (the owner's decision of 2026-11-19); like the upload beside it, it is readable
        only by this account and goes when the job is deleted."""
        job = self.get(job_id)
        if job["status"] != "review":
            raise ValueError("job is not ready")
        if job["mode"] != "code":
            raise WrongMode(job["mode"])
        folder = self._dir(job_id)
        with self._apply_lock:
            ctx = self.context(job_id)
            coded = codes.build(ctx.inv, ctx.store, lang=lang)
            table = restorefile.pack(coded.table)
            for what, data in (("text", coded.text.encode("utf-8")), ("restore", table)):
                name = _export_name(job["ext"], what, lang)
                tmp = folder / (name + ".tmp")
                with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as out:
                    out.write(data)
                tmp.replace(folder / name)
            own = folder / _export_name(job["ext"], "coded", lang)
            own.unlink(missing_ok=True)
            made = codefile.make(ctx, coded, own)
            if made.ok:
                os.chmod(own, 0o600)
        result = {"kind": "code", "lang": lang, "ok": not coded.residue, "residue": coded.residue, "problems": [],
                  "file": {"ext": job["ext"], "placed": made.placed, "left": made.left} if made.ok else None,
                  "file_notes": made.notes,
                  "notes": [f"部分保留：{line}" for line in coded.notes],
                  "stats": {"codes": len(coded.table), "blanked": coded.blanked, "chars": len(coded.text)},
                  "made": time.time()}  # names the coded text and its table: a pair made together, told from any other
        self._try_update(job_id, result=result)
        log.info("job %s coded: codes=%d blanked=%d residue=%d notes=%d file=%s placed=%d left=%d", job_id, len(coded.table),
                 coded.blanked, len(coded.residue), len(coded.notes), "made" if made.ok else "none", made.placed, made.left)
        return result

    def export_path(self, job_id: str, what: str, *, lang: str | None = None) -> Path:
        job = self.get(job_id)
        if lang is None:
            lang = (job.get("result") or {}).get("lang", "zh-TW")
        path = self._dir(job_id) / _export_name(job["ext"], what, lang)
        if not path.is_file():
            raise FileNotFoundError(job_id)
        return path

    def output_path(self, job_id: str) -> Path:
        path = self._dir(job_id) / f"output{self._read(job_id)['ext']}"
        if not path.is_file():
            raise FileNotFoundError(job_id)
        return path

    def mark_downloaded(self, job_id: str) -> None:
        try:
            self._update(job_id, downloaded_at=time.time())
        except JobWriteError:  # the file is being handed over anyway; only the early deletion is lost
            log.warning("job %s: the download could not be recorded, so the job stays until its time limit", job_id)
            return
        log.info("job %s downloaded", job_id)

    def retry(self, job_id: str) -> None:
        """Raises ValueError for a job that has not failed, JobWriteError when the job could not be marked queued."""
        # Read and changed under one lock: two requests at the same moment (a double click) both read "failed"
        # otherwise, the job went into the queue twice, and the second analysis wiped the reviewer's edits.
        with self._lock:
            if self._read(job_id)["status"] != "failed":
                raise ValueError("only failed jobs can be retried")
            try:
                saved = self._update(job_id, status="queued", stage="排隊中", progress=0.0, error=None)
            except JobWriteError as e:
                # Not queued: the worker takes only a job marked queued. The page reloads the job after a retry,
                # so the failure it shows now says why nothing happened.
                self._unsaved[job_id] = {"status": "failed", "stage": "失敗", "error": write_failure_message(e.cause)}
                raise
            if saved is None:
                raise KeyError(job_id)
        self._queue.put(job_id)

    def delete(self, job_id: str) -> None:
        """Also cancels: a job being processed notices at its next progress report, stops, and removes
        whatever it wrote in the meantime."""
        folder = self._dir(job_id)
        self._remove(folder)
        log.info("job %s deleted", job_id)

    def _remove(self, folder: Path) -> None:
        with self._lock:
            shutil.rmtree(folder, ignore_errors=True)
            self._unsaved.pop(folder.name, None)

    def sweep(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        after_download = self.settings.delete_after_download_min * 60
        ttl = self.settings.job_ttl_hours * 3600
        removed = 0
        for folder in list(self.root.iterdir()):
            if not folder.is_dir():
                continue
            try:
                job = json.loads((folder / "job.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                job = None
            else:
                job.update(self._unsaved.get(folder.name, {}))  # a failure that could not be written is still a failure
            if job is None:
                expired = now - folder.stat().st_mtime > ttl
            elif job.get("status") == "processing":
                expired = False
            else:
                downloaded = job.get("downloaded_at")
                expired = (downloaded is not None and now - downloaded > after_download) or now - job.get("created", now) > ttl
            if expired:
                self._remove(folder)
                removed += 1
                log.info("job %s expired and was deleted", folder.name)
        return removed

    # ---- background work -----------------------------------------------------------------

    def start(self) -> None:
        for folder in sorted(self.root.iterdir(), key=lambda p: p.stat().st_mtime):
            try:
                job = json.loads((folder / "job.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if folder.name in list(self._queue.queue):
                continue
            if job.get("status") in ("queued", "processing"):
                self._queue_again(folder.name, stage="排隊中", progress=0.0)
            elif job.get("status") == "review" and job.get("analysis") != ANALYSIS:
                # Read by an older version: its findings point at units this version may cut differently.
                for old in list(folder.glob("output.*")) + list(folder.glob("coded.*")) + [folder / name for name in EXPORTS.values()]:
                    old.unlink(missing_ok=True)  # made from the old reading
                if self._queue_again(folder.name, stage="系統已更新，這份文件排隊重新分析", progress=0.0, result=None,
                                     reanalysed=True):
                    log.info("job %s was analysed by an older version and is analysed again", folder.name)
        self._stop.clear()
        self._threads = [threading.Thread(target=self._work, name="deid-worker", daemon=True),
                         threading.Thread(target=self._sweep_loop, name="deid-sweeper", daemon=True)]
        for t in self._threads:
            t.start()

    def _queue_again(self, job_id: str, **changes) -> bool:
        """Marks a job queued and puts it in the queue. The worker takes only a job marked queued (_claim), so one
        that cannot be marked is failed instead, in memory if need be (see _fail), for the user to retry."""
        try:
            saved = self._update(job_id, status="queued", **changes)
        except JobWriteError as e:
            self._fail(job_id, time.time(), write_failure_message(e.cause))
            return False
        if saved is None:
            return False
        self._queue.put(job_id)
        return True

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)
        for t in self._threads:
            t.join(timeout=15)
        self._threads = []

    def _sweep_loop(self) -> None:
        while not self._stop.wait(60):
            try:
                self.sweep()
            except Exception as e:  # noqa: BLE001 - the sweeper must keep running
                log.warning("sweep failed: %s", type(e).__name__)

    def _work(self) -> None:
        while not self._stop.is_set():
            job_id = self._queue.get()
            if job_id is None:
                break
            try:
                self._process(job_id)
            except Exception as e:  # noqa: BLE001 - one bad job must not stop the worker
                log.error("job %s crashed outside processing: %s", job_id, type(e).__name__)

    def _claim(self, job_id: str) -> bool:
        """Marks a job taken from the queue as being processed. False when it is gone or not waiting: an id can be in
        the queue more than once, and a job that has left the queued state is not analysed again. That would wipe
        the reviewer's edits, while a file made from the edited list stayed downloadable."""
        with self._lock:
            try:
                status = self._read(job_id).get("status")
            except KeyError:
                return False
            if status != "queued":
                log.warning("job %s came out of the queue again while %s; it is not analysed again", job_id, status)
                return False
            return self._update(job_id, status="processing", stage="開始處理", progress=0.0, error=None) is not None

    def _process(self, job_id: str) -> None:
        started = time.time()
        try:
            if not self._claim(job_id):
                return
        except JobWriteError as e:
            self._fail(job_id, started, write_failure_message(e.cause))
            return
        last = {"stage": "", "at": 0.0}
        write_errors: list[OSError] = []
        limit = float(self.settings.job_timeout_min) * 60
        folder = self.root / job_id

        def progress(stage: str, fraction: float) -> None:
            now = time.time()
            if limit > 0 and now - started > limit:
                raise JobStopped("timeout")
            if stage != last["stage"] or now - last["at"] > 0.5 or fraction >= 1.0:
                last.update(stage=stage, at=now)
                try:
                    saved = self._update(job_id, stage=stage, progress=round(float(fraction), 3))
                except JobWriteError as e:
                    write_errors.append(e.cause)
                    raise JobStopped("write") from e  # a JobStopped, because the agent loop lets only that one through
                if saved is None:
                    raise JobStopped("cancelled")  # the job was deleted while it was being processed

        try:
            for stale in ("findings.json", "inventory.json", "log.json", "warnings.json"):
                (folder / stale).unlink(missing_ok=True)
            ctx = self._factory(self.source_path(job_id), folder, self.settings, progress=progress)
            job = self.get(job_id)
            if job.get("reanalysed"):
                ctx.warn(REANALYSED)
            analyze(ctx, use_agent=self.settings.use_agent, mode=job["mode"], lang=job.get("lang", "zh-TW"))
        except JobStopped as e:
            if e.reason == "write":
                error = write_failure_message(write_errors[-1] if write_errors else None)
            elif e.reason == "timeout":
                error = f"處理時間超過 {self.settings.job_timeout_min:g} 分鐘上限，已中止。可以重試，或把文件拆成較小的檔案再上傳。"
            else:
                self._remove(folder)  # files written after the deletion
                log.info("job %s cancelled after %.1fs", job_id, time.time() - started)
                return
        except KeyError:
            if not (folder / "job.json").is_file():
                self._remove(folder)  # deleted while it was being processed
                return
            error = "處理失敗（KeyError）"
        except BrokenFile as e:
            error = str(e)
        except UnsupportedFormat:
            error = f"目前只支援 {'、'.join(SUPPORTED)}"
        except LLMError:
            error = "模型端點無法連線或回應異常，請稍後重試"
        except OSError as e:
            if e.errno not in DISK_FULL:
                error = f"處理失敗（{type(e).__name__}）"
            else:  # some other write met the full disk before job.json did
                log.error("job %s: a write failed: %s %s", job_id, errno.errorcode.get(e.errno, e.errno), e.strerror)
                error = write_failure_message(e)
        except Exception as e:  # noqa: BLE001 - shown to the user by class name only
            error = f"處理失敗（{type(e).__name__}）"
        else:
            try:
                saved = self._update(job_id, status="review", stage="完成", progress=1.0, analysis=ANALYSIS)
            except JobWriteError as e:
                error = write_failure_message(e.cause)
            else:
                if saved is None:
                    self._remove(folder)  # deleted just as it finished
                    return
                log.info("job %s review after %.1fs: %d text units, %d images, %d findings, %d warnings", job_id,
                         time.time() - started, len(ctx.inv.texts), len(ctx.inv.images), len(ctx.store.findings), len(ctx.warnings))
                return
        self._fail(job_id, started, error)

    def _fail(self, job_id: str, started: float, error: str) -> None:
        """Records that a job failed. If even that cannot be written, the job is kept anyway: deleting it would turn
        a full disk into a "deleted" job, and leaving it as "processing" would show a job that is not running, one
        the clock never deletes. The failure is held in memory instead: the page shows it, retry and the clock
        treat it as the failure it is, and the next successful write of job.json carries it to disk. A restart
        forgets it, and the job is then processed again like any interrupted one."""
        try:
            saved = self._update(job_id, status="failed", stage="失敗", error=error)
        except JobWriteError:
            with self._lock:
                if (self.root / job_id / "job.json").is_file():
                    self._unsaved[job_id] = {"status": "failed", "stage": "失敗", "error": error}
            log.error("job %s failed after %.1fs and the failure could not be written; it is kept in memory only: %s",
                      job_id, time.time() - started, error)
            return
        if saved is None:
            self._remove(self.root / job_id)
            return
        log.warning("job %s failed after %.1fs: %s", job_id, time.time() - started, error)
