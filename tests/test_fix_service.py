"""Service fixes of 2026-11-24 (review of 2026-11-23): the script sandbox must be able to read an upload that
is stored the way the service stores it (mode 0600), and a failed write of job.json is not a cancel."""
import errno
import io
import json
import logging
import pathlib
import re
import shutil
import stat
import tempfile
import threading
import time

import pytest

from deid.agent import sandbox
from deid.config import load_settings
from deid.jobs import JobStore
from deid.pipeline import build_context
from tests.builders import make_docx
from tests.fakes import FakeFaces, FakeLLM, FakeOcr, keyword_llm


# ---- S1: run_python reads an upload that only its owner can read ----------------------------------------

READ_TWO_BYTES = "print(open('/work/input.docx','rb').read(2))"


def _leftovers(folder):
    return sorted(p.name for p in folder.iterdir() if p.name.startswith("sandbox-"))


@pytest.fixture
def private_doc(tmp_path):
    """An upload as JobStore.create stores it: mode 0600, owned by the service user."""
    folder = tmp_path / "job"
    folder.mkdir(mode=0o700)
    doc = make_docx(folder / "original.docx", ["內文 王小明"])
    doc.chmod(0o600)
    return doc


def service_settings(doc):
    """Settings for an upload inside the data folder, as in the service (the job folder is under it)."""
    return load_settings(doc.parent.parent / "none.env", environ={"DATA_DIR": str(doc.parent.parent)})


def command_line_settings(doc):
    """Settings for a file that is not inside the data folder, as with `python -m deid run file`."""
    return load_settings(doc.parent.parent / "none.env", environ={"DATA_DIR": str(doc.parent.parent / "somewhere-else")})


def _spy_on_the_command(monkeypatch, outcome=None, seen=None):
    """Replace the container start by a recorder: what the host would bind, and what that looked like at the time."""
    seen = {} if seen is None else seen

    def fake(cmd, stdin_data, timeout, on_timeout=None):
        bind = cmd[cmd.index("-v") + 1]
        host, container, mode = bind.rsplit(":", 2)
        host = pathlib.Path(host)
        seen.update(host=host, container=container, mode=mode, cmd=cmd,
                    file_mode=stat.S_IMODE(host.stat().st_mode), dir_mode=stat.S_IMODE(host.parent.stat().st_mode),
                    content=host.read_bytes())
        if isinstance(outcome, BaseException):
            raise outcome
        return sandbox.Bounded(0, b"ok", False, False)

    monkeypatch.setattr(sandbox, "run_bounded", fake)
    return seen


def test_the_container_is_given_a_readable_copy_not_the_private_upload(private_doc, monkeypatch):
    seen = _spy_on_the_command(monkeypatch)
    res = sandbox.run_python(service_settings(private_doc), private_doc, "print(1)")
    assert res["ok"] is True
    assert seen["host"] != private_doc.resolve()
    assert seen["container"] == "/work/input.docx" and seen["mode"] == "ro"
    assert seen["content"] == private_doc.read_bytes()
    assert seen["file_mode"] & 0o004, "the sandbox user (65534) reads the file as 'other'"
    assert not seen["file_mode"] & 0o022, "nobody but the owner could write it"
    assert seen["dir_mode"] == 0o700, "the copy's folder stays closed to other host users"


def test_the_copy_lives_beside_the_upload_so_the_host_docker_can_find_it(private_doc, monkeypatch):
    """In production the service runs in a container whose /tmp is its own; only the data folder has the same
    path on the host. The copy therefore goes next to the upload, inside the job folder, not into /tmp."""
    seen = _spy_on_the_command(monkeypatch)
    sandbox.run_python(service_settings(private_doc), private_doc, "print(1)")
    assert seen["host"].parent.parent == private_doc.parent.resolve()


def test_a_file_outside_the_data_folder_gets_its_copy_in_the_system_temp_folder(private_doc, tmp_path, monkeypatch):
    """Command line use: nothing is written beside the user's own document."""
    seen = _spy_on_the_command(monkeypatch)
    system_temp = tmp_path / "system-temp"
    system_temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system_temp))
    res = sandbox.run_python(command_line_settings(private_doc), private_doc, "print(1)")
    assert res["ok"] is True
    assert seen["host"].parent.parent == system_temp and seen["dir_mode"] == 0o700
    assert seen["content"] == private_doc.read_bytes()
    assert _leftovers(system_temp) == [] and _leftovers(private_doc.parent) == []
    assert sorted(p.name for p in private_doc.parent.iterdir()) == ["original.docx"]


def test_an_explicit_work_directory_is_used_for_the_copy(private_doc, tmp_path, monkeypatch):
    seen = _spy_on_the_command(monkeypatch)
    other = tmp_path / "elsewhere"
    other.mkdir()
    sandbox.run_python(service_settings(private_doc), private_doc, "print(1)", workdir=other)
    assert seen["host"].parent.parent == other


def test_the_upload_is_untouched_by_a_run(private_doc, monkeypatch):
    before = private_doc.read_bytes()
    _spy_on_the_command(monkeypatch)
    sandbox.run_python(service_settings(private_doc), private_doc, "print(1)")
    assert private_doc.read_bytes() == before
    assert stat.S_IMODE(private_doc.stat().st_mode) == 0o600


@pytest.mark.parametrize("outcome", [None, OSError("docker is missing"), RuntimeError("boom")])
def test_the_copy_is_removed_whatever_the_run_does(private_doc, monkeypatch, outcome):
    seen = _spy_on_the_command(monkeypatch, outcome)
    settings = service_settings(private_doc)
    try:
        res = sandbox.run_python(settings, private_doc, "print(1)")
    except RuntimeError:
        res = None
    assert seen["host"].exists() is False
    assert _leftovers(private_doc.parent) == []
    if isinstance(outcome, OSError):
        assert res == {"ok": False, "timed_out": False, "output": "", "error": "腳本環境無法啟動"}


def test_a_failed_copy_ends_in_the_same_message_and_leaves_nothing(private_doc, monkeypatch):
    def full_disk(src, dst, **kwargs):
        pathlib.Path(dst).write_bytes(b"PK")  # a partial copy is already on disk
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(sandbox.shutil, "copyfile", full_disk)
    monkeypatch.setattr(sandbox, "run_bounded", lambda *a, **k: pytest.fail("the container must not start"))
    res = sandbox.run_python(service_settings(private_doc), private_doc, "print(1)")
    assert res == {"ok": False, "timed_out": False, "output": "", "error": "腳本環境無法啟動"}
    assert _leftovers(private_doc.parent) == []
    assert private_doc.read_bytes()[:2] == b"PK"


def test_a_job_deleted_during_a_run_does_not_break_the_cleanup(private_doc, monkeypatch):
    import shutil

    def fake(cmd, stdin_data, timeout, on_timeout=None):
        shutil.rmtree(private_doc.parent)  # the user deletes the job while the script runs
        return sandbox.Bounded(0, b"ok", False, False)

    monkeypatch.setattr(sandbox, "run_bounded", fake)
    res = sandbox.run_python(service_settings(private_doc), private_doc, "print(1)")
    assert res["ok"] is True and not private_doc.parent.exists()


# the same, with a real container (needs Docker and the sandbox image)

@pytest.mark.docker
@pytest.mark.parametrize("where", [service_settings, command_line_settings], ids=["in the data folder", "outside it"])
def test_script_reads_an_upload_stored_with_mode_0600(private_doc, where):
    settings = where(private_doc)
    assert sandbox.available(settings)
    assert stat.S_IMODE(private_doc.stat().st_mode) == 0o600
    res = sandbox.run_python(settings, private_doc, READ_TWO_BYTES)
    assert res["ok"] and res["output"].strip() == "b'PK'", res
    assert stat.S_IMODE(private_doc.stat().st_mode) == 0o600
    assert _leftovers(private_doc.parent) == []


@pytest.mark.docker
def test_script_reads_an_upload_made_by_the_job_store(tmp_path):
    settings = load_settings(tmp_path / "none.env", environ={"DATA_DIR": str(tmp_path / "data")})
    doc = make_docx(tmp_path / "a.docx", ["內文 王小明"])
    store = JobStore(settings)
    job_id = store.create("a.docx", io.BytesIO(doc.read_bytes()))
    src = store.source_path(job_id)
    assert stat.S_IMODE(src.stat().st_mode) == 0o600
    res = sandbox.run_python(settings, src, READ_TWO_BYTES)
    assert res["ok"] and res["output"].strip() == "b'PK'", res
    assert _leftovers(src.parent) == []
    assert stat.S_IMODE(src.stat().st_mode) == 0o600


@pytest.mark.docker
def test_script_still_cannot_change_a_private_original(private_doc):
    settings = service_settings(private_doc)
    before = private_doc.read_bytes()
    code = ("import os\n"
            "for what in ('write', 'append', 'chmod', 'remove', 'rename'):\n"
            "    try:\n"
            "        if what == 'write': open('/work/input.docx', 'wb').write(b'x')\n"
            "        elif what == 'append': open('/work/input.docx', 'ab').write(b'x')\n"
            "        elif what == 'chmod': os.chmod('/work/input.docx', 0o777)\n"
            "        elif what == 'remove': os.remove('/work/input.docx')\n"
            "        else: os.rename('/work/input.docx', '/work/other')\n"
            "        print('DID', what)\n"
            "    except OSError:\n"
            "        print('denied', what)\n"
            "print(open('/work/input.docx', 'rb').read(2))")
    res = sandbox.run_python(settings, private_doc, code)
    assert "DID" not in res["output"] and res["output"].count("denied") == 5 and "b'PK'" in res["output"], res
    assert private_doc.read_bytes() == before
    assert stat.S_IMODE(private_doc.stat().st_mode) == 0o600


@pytest.mark.docker
@pytest.mark.parametrize("code, kwargs", [("print('fine')", {}), ("raise ValueError('boom')", {}),
                                          ("while True:\n    pass", {"timeout": 3})])
def test_copy_is_gone_after_success_error_and_timeout(private_doc, code, kwargs):
    settings = service_settings(private_doc)
    sandbox.run_python(settings, private_doc, code, **kwargs)
    assert _leftovers(private_doc.parent) == []
    assert sorted(p.name for p in private_doc.parent.iterdir()) == ["original.docx"]


# ---- S2: a failed write of job.json is not "the user cancelled" ----------------------------------------

NAMES = {"王小明": "姓名"}
CJK = re.compile(r"[一-鿿]")


@pytest.fixture
def docx_bytes(tmp_path):
    return make_docx(tmp_path / "src.docx", ["承辦人 王小明", "一般內容"], creator="林志豪").read_bytes()


class Disk:
    """A disk whose writes of job.json fail with ENOSPC while `full` is set (`left` times, or without end)."""

    def __init__(self, monkeypatch, left=None):
        self.full, self.left, self.failed = False, left, 0
        self.fill_before = self.fill_after = None  # the stage that fills the disk, just before or just after it is reported
        real = pathlib.Path.write_text

        def write_text(path, *args, **kwargs):
            if self.full and path.name == "job.tmp" and (self.left is None or self.left > 0):
                self.failed += 1
                if self.left is not None:
                    self.left -= 1
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "write_text", write_text)


def make_store(settings, disk=None, *, fill_before=None, fill_after=None, llm=None):
    """A store whose disk fills up just before the stage `fill_before` is reported, or just after `fill_after`."""
    if disk is not None:
        disk.fill_before, disk.fill_after = fill_before, fill_after

    def make(src, workdir, settings, progress=None, restore=False):
        def report(stage, fraction):
            if disk is not None and stage == disk.fill_before:
                disk.full = True
            progress(stage, fraction)
            if disk is not None and stage == disk.fill_after:
                disk.full = True
        return build_context(src, workdir, settings, llm=llm or keyword_llm(NAMES), progress=report if progress else None,
                             ocr=FakeOcr(), faces=FakeFaces(), restore=restore)

    settings.use_agent = False
    return JobStore(settings, context_factory=make)


def wait_for(store, job_id, statuses=("review", "failed"), timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        job = store.get(job_id)
        if job["status"] in statuses:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job stuck in {store.get(job_id)['status']}")


def on_disk(settings, job_id):
    return json.loads((settings.data_dir / "jobs" / job_id / "job.json").read_text(encoding="utf-8"))


def log_text(caplog):
    return "\n".join(f"{r.levelname} {r.getMessage()}" for r in caplog.records)


def test_one_failed_write_during_analysis_fails_the_job_it_does_not_delete_it(settings, docx_bytes, monkeypatch, caplog):
    disk = Disk(monkeypatch, left=1)
    store = make_store(settings, disk, fill_before="偵測文字中的個資")
    store.start()
    try:
        with caplog.at_level(logging.DEBUG, logger="deid"):
            job_id = store.create("a.docx", io.BytesIO(docx_bytes))
            job = wait_for(store, job_id)
        folder = settings.data_dir / "jobs" / job_id
        assert disk.failed == 1
        assert folder.is_dir() and (folder / "original.docx").is_file(), "the job and its upload are still there"
        assert job["status"] == "failed" and job["stage"] == "失敗"
        assert CJK.search(job["error"]) and "磁碟" in job["error"] and "重試" in job["error"]
        assert on_disk(settings, job_id)["status"] == "failed", "the next write worked, so the failure is on disk"
        text = log_text(caplog)
        assert "cancelled" not in text
        assert "ENOSPC" in text and "No space left on device" in text, "the real error is in the log"
        assert any(r.levelno >= logging.WARNING and job_id in r.getMessage() for r in caplog.records)
        assert "王小明" not in text
        store.retry(job_id)  # the disk has room again
        assert wait_for(store, job_id, ("review",))["status"] == "review"
    finally:
        store.stop()


def test_a_full_disk_that_cannot_record_even_the_failure_still_does_not_delete_the_job(settings, docx_bytes, monkeypatch, caplog):
    disk = Disk(monkeypatch)
    store = make_store(settings, disk, fill_before="偵測文字中的個資")
    store.start()
    try:
        with caplog.at_level(logging.DEBUG, logger="deid"):
            job_id = store.create("a.docx", io.BytesIO(docx_bytes))
            job = wait_for(store, job_id)
        folder = settings.data_dir / "jobs" / job_id
        assert folder.is_dir() and (folder / "original.docx").is_file()
        assert job["status"] == "failed" and "磁碟" in job["error"] and "重試" in job["error"], "the user is still told"
        assert on_disk(settings, job_id)["status"] == "processing", "nothing could be written"
        text = log_text(caplog)
        assert "cancelled" not in text and "ENOSPC" in text
        assert any(r.levelno >= logging.ERROR and job_id in r.getMessage() for r in caplog.records)
        # it is not stuck: a job whose failure was never written is not exempt from the clock
        assert store.sweep(time.time() + 25 * 3600) == 1 and not folder.exists()
    finally:
        store.stop()


def test_a_failure_that_could_not_be_written_is_retryable_once_the_disk_has_room(settings, docx_bytes, monkeypatch):
    disk = Disk(monkeypatch)
    store = make_store(settings, disk, fill_before="偵測文字中的個資")
    store.start()
    try:
        job_id = store.create("a.docx", io.BytesIO(docx_bytes))
        assert wait_for(store, job_id)["status"] == "failed"
        disk.full = disk.fill_before = None  # room again
        store.retry(job_id)
        job = wait_for(store, job_id, ("review",))
        assert job["status"] == "review" and job["error"] is None
        assert on_disk(settings, job_id)["status"] == "review"
        assert store.get(job_id)["status"] == "review", "the remembered failure is gone"
    finally:
        store.stop()


def test_the_disk_full_before_the_first_write_fails_the_job_too(settings, docx_bytes, monkeypatch, caplog):
    disk = Disk(monkeypatch)
    disk.full = True
    store = make_store(settings, disk)
    store.start()
    try:
        with caplog.at_level(logging.DEBUG, logger="deid"):
            job_id = store.create("a.docx", io.BytesIO(docx_bytes))
            job = wait_for(store, job_id)
        assert (settings.data_dir / "jobs" / job_id / "original.docx").is_file()
        assert job["status"] == "failed" and "磁碟" in job["error"]
        assert "cancelled" not in log_text(caplog)
    finally:
        store.stop()


def test_a_failed_write_of_the_final_state_fails_the_job_instead_of_deleting_the_finished_analysis(settings, docx_bytes, monkeypatch, caplog):
    disk = Disk(monkeypatch)
    store = make_store(settings, disk, fill_after="完成")
    store.start()
    try:
        with caplog.at_level(logging.DEBUG, logger="deid"):
            job_id = store.create("a.docx", io.BytesIO(docx_bytes))
            job = wait_for(store, job_id)
        folder = settings.data_dir / "jobs" / job_id
        assert folder.is_dir() and (folder / "original.docx").is_file()
        assert job["status"] == "failed" and "磁碟" in job["error"]
        assert "cancelled" not in log_text(caplog) and "deleted just as it finished" not in log_text(caplog)
    finally:
        store.stop()


def test_another_kind_of_write_error_gets_its_own_plain_message(settings, docx_bytes, monkeypatch):
    real = pathlib.Path.write_text

    def write_text(path, *args, **kwargs):
        if path.name == "job.tmp" and json.loads(args[0]).get("stage") == "偵測文字中的個資":
            raise OSError(errno.EIO, "Input/output error")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", write_text)
    store = make_store(settings)
    store.start()
    try:
        job = wait_for(store, store.create("a.docx", io.BytesIO(docx_bytes)))
        assert job["status"] == "failed" and CJK.search(job["error"]) and "重試" in job["error"]
        assert "磁碟空間" not in job["error"], "an I/O error is not a full disk"
    finally:
        store.stop()


def test_a_real_cancel_still_removes_the_job_and_the_worker_goes_on(settings, docx_bytes, caplog):
    inside, gate = threading.Event(), threading.Event()
    detect = keyword_llm(NAMES).handler

    def handler(messages, tools):
        inside.set()
        gate.wait(10)
        return detect(messages, tools)

    store = make_store(settings, llm=FakeLLM(handler))
    store.start()
    try:
        with caplog.at_level(logging.DEBUG, logger="deid"):
            cancelled = store.create("a.docx", io.BytesIO(docx_bytes))
            assert inside.wait(10)
            store.delete(cancelled)  # the user deletes the job while it is being processed
            gate.set()
            later = store.create("b.docx", io.BytesIO(docx_bytes))
            assert wait_for(store, later, ("review",))["status"] == "review"
        assert not (settings.data_dir / "jobs" / cancelled).exists(), "whatever the worker wrote afterwards is gone"
        with pytest.raises(KeyError):
            store.get(cancelled)
        mine = [r.getMessage() for r in caplog.records if cancelled in r.getMessage()]
        assert any("cancelled" in m for m in mine) and not any("failed" in m for m in mine)
    finally:
        gate.set()
        store.stop()


def test_a_job_deleted_between_reading_and_writing_is_a_cancel_not_a_write_error(settings, docx_bytes, monkeypatch):
    store = JobStore(settings)
    job_id = store.create("a.docx", io.BytesIO(docx_bytes))
    folder = settings.data_dir / "jobs" / job_id

    def vanish(path, *args, **kwargs):
        shutil.rmtree(folder)
        raise FileNotFoundError(errno.ENOENT, "No such file or directory")

    monkeypatch.setattr(pathlib.Path, "write_text", vanish)
    assert store._update(job_id, stage="x") is None


def test_update_tells_a_write_error_from_a_deleted_job_and_leaves_no_partial_file(settings, docx_bytes, monkeypatch):
    from deid.jobs import JobWriteError

    store = JobStore(settings)
    job_id = store.create("a.docx", io.BytesIO(docx_bytes))
    before = on_disk(settings, job_id)
    folder = settings.data_dir / "jobs" / job_id
    real = pathlib.Path.write_text

    def partial(path, *args, **kwargs):
        real(path, "{", encoding="utf-8")  # a half-written file, then the disk fills up
        raise OSError(errno.ENOSPC, "No space left on device")

    with monkeypatch.context() as m:
        m.setattr(pathlib.Path, "write_text", partial)
        with pytest.raises(JobWriteError) as caught:
            store._update(job_id, stage="x")
    assert caught.value.cause.errno == errno.ENOSPC
    assert not (folder / "job.tmp").exists()
    assert on_disk(settings, job_id) == before and store.get(job_id)["stage"] == before["stage"]
    store.delete(job_id)
    assert store._update(job_id, stage="x") is None


def test_a_full_disk_met_by_another_write_during_analysis_is_also_told_plainly(settings, docx_bytes):
    def full(messages, tools):
        raise OSError(errno.ENOSPC, "No space left on device")

    store = make_store(settings, llm=FakeLLM(full))
    store.start()
    try:
        job = wait_for(store, store.create("a.docx", io.BytesIO(docx_bytes)))
        assert job["status"] == "failed" and "磁碟空間" in job["error"] and "重試" in job["error"]
    finally:
        store.stop()


def test_other_os_errors_keep_the_generic_message(settings, docx_bytes):
    def broken(messages, tools):
        raise OSError(errno.EIO, "Input/output error")

    store = make_store(settings, llm=FakeLLM(broken))
    store.start()
    try:
        job = wait_for(store, store.create("a.docx", io.BytesIO(docx_bytes)))
        assert job["status"] == "failed" and job["error"] == "處理失敗（OSError）"
    finally:
        store.stop()


def test_a_download_that_cannot_be_recorded_still_goes_through_and_leaves_a_trace(settings, docx_bytes, monkeypatch, caplog):
    disk = Disk(monkeypatch)
    store = make_store(settings, disk)
    store.start()
    try:
        job_id = store.create("a.docx", io.BytesIO(docx_bytes))
        wait_for(store, job_id, ("review",))
        store.apply(job_id)
        disk.full = True
        with caplog.at_level(logging.DEBUG, logger="deid"):
            store.mark_downloaded(job_id)  # must not raise: the file is being handed over
        assert store.get(job_id)["downloaded_at"] is None
        text = log_text(caplog)
        assert "ENOSPC" in text and job_id in text and f"job {job_id} downloaded" not in text
        assert (settings.data_dir / "jobs" / job_id / "original.docx").is_file()
        disk.full = False
        store.mark_downloaded(job_id)
        assert store.get(job_id)["downloaded_at"] is not None
    finally:
        store.stop()


def test_a_job_that_is_being_processed_is_not_swept(settings, docx_bytes):
    gate = threading.Event()
    detect = keyword_llm(NAMES).handler

    def handler(messages, tools):
        gate.wait(10)
        return detect(messages, tools)

    store = make_store(settings, llm=FakeLLM(handler))
    store.start()
    try:
        job_id = store.create("a.docx", io.BytesIO(docx_bytes))
        wait_for(store, job_id, ("processing",))
        assert store.sweep(time.time() + 48 * 3600) == 0  # still working: the clock does not apply
        gate.set()
        wait_for(store, job_id, ("review",))
    finally:
        gate.set()
        store.stop()
