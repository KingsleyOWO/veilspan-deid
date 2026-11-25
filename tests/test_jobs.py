import io
import logging
import threading
import time

import pytest

from deid.formats.base import UnsupportedFormat
from deid.jobs import JobStore, TooLarge
from deid.llm import LLMError
from deid.pipeline import build_context
from tests.builders import make_docx, make_pdf
from tests.fakes import FakeFaces, FakeLLM, FakeOcr, keyword_llm, tool_call

NAMES = {"王小明": "姓名"}


def factory(llm):
    def make(src, workdir, settings, progress=None, restore=False):
        return build_context(src, workdir, settings, llm=llm, progress=progress, ocr=FakeOcr(), faces=FakeFaces(), restore=restore)
    return make


@pytest.fixture
def docx_bytes(tmp_path):
    return make_docx(tmp_path / "src.docx", ["承辦人 王小明", "一般內容"], creator="林志豪").read_bytes()


@pytest.fixture
def store(settings):
    settings.use_agent = False
    s = JobStore(settings, context_factory=factory(keyword_llm(NAMES)))
    s.start()
    yield s
    s.stop()


def wait(store, job_id, statuses=("review", "failed"), timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        job = store.get(job_id)
        if job["status"] in statuses:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job stuck in {store.get(job_id)['status']}")


def test_uploaded_document_is_processed_to_review(store, docx_bytes, settings):
    job_id = store.create("報告.docx", io.BytesIO(docx_bytes))
    assert len(job_id) >= 20
    job = wait(store, job_id)
    assert (job["status"], job["filename"], job["ext"], job["error"], job["progress"]) == ("review", "報告.docx", ".docx", None, 1.0)
    assert (settings.data_dir / "jobs" / job_id / "findings.json").is_file()
    ctx = store.context(job_id)
    assert "王小明" in {f.text for f in ctx.store.findings}


def test_unsupported_extension_is_rejected_at_upload(store):
    with pytest.raises(UnsupportedFormat):
        store.create("a.pptx", io.BytesIO(b"x"))
    with pytest.raises(UnsupportedFormat):
        store.create("noextension", io.BytesIO(b"x"))


def test_oversized_upload_is_rejected_and_leaves_nothing_behind(store, settings):
    settings.max_upload_mb = 1
    with pytest.raises(TooLarge):
        store.create("big.docx", io.BytesIO(b"x" * (1024 * 1024 + 10)))
    assert list((settings.data_dir / "jobs").glob("*")) == []


def test_file_that_is_not_a_document_fails_with_a_readable_message_and_the_worker_survives(store, docx_bytes):
    bad = store.create("bad.docx", io.BytesIO(b"not a zip"))
    job = wait(store, bad)
    assert job["status"] == "failed" and "Word" in job["error"]
    good = store.create("good.docx", io.BytesIO(docx_bytes))
    assert wait(store, good)["status"] == "review"


def test_password_protected_pdf_fails_with_a_message_about_the_password(store, tmp_path):
    locked = make_pdf(tmp_path / "locked.pdf", [{"lines": ["x"]}], password="pw").read_bytes()
    job = wait(store, store.create("locked.pdf", io.BytesIO(locked)))
    assert job["status"] == "failed" and "密碼" in job["error"]


def test_endpoint_failure_fails_the_job_and_retry_recovers(settings, docx_bytes):
    state = {"down": True}

    def handler(messages, tools):
        if state["down"]:
            raise LLMError("down")
        return {"role": "assistant", "content": "[]"}

    settings.use_agent = False
    store = JobStore(settings, context_factory=factory(FakeLLM(handler)))
    store.start()
    try:
        job_id = store.create("a.docx", io.BytesIO(docx_bytes))
        job = wait(store, job_id)
        assert job["status"] == "failed" and "模型端點" in job["error"]
        assert not (settings.data_dir / "jobs" / job_id / "findings.json").exists()
        state["down"] = False
        store.retry(job_id)
        assert wait(store, job_id)["status"] == "review"
    finally:
        store.stop()


def test_unexpected_error_is_reported_without_details(settings, docx_bytes):
    def handler(messages, tools):
        raise RuntimeError("secret detail 王小明")

    settings.use_agent = False
    store = JobStore(settings, context_factory=factory(FakeLLM(handler)))
    store.start()
    try:
        job = wait(store, store.create("a.docx", io.BytesIO(docx_bytes)))
        assert job["status"] == "failed" and job["error"] == "處理失敗（RuntimeError）"
    finally:
        store.stop()


def test_apply_writes_the_output_and_records_the_result(store, docx_bytes):
    job_id = store.create("a.docx", io.BytesIO(docx_bytes))
    wait(store, job_id)
    result = store.apply(job_id)
    assert result.ok and store.output_path(job_id).is_file()
    assert store.get(job_id)["result"]["ok"] is True


def test_editing_findings_invalidates_the_previous_result(store, docx_bytes):
    job_id = store.create("a.docx", io.BytesIO(docx_bytes))
    wait(store, job_id)
    store.apply(job_id)
    ctx = store.context(job_id)
    ctx.store.set_on(next(f.id for f in ctx.store.findings if f.text == "王小明"), False)
    store.save_findings(job_id, ctx.store)
    assert store.get(job_id)["result"] is None
    assert store.context(job_id).store.plan() == ctx.store.plan()
    with pytest.raises(FileNotFoundError):
        store.output_path(job_id)


def test_jobs_are_deleted_after_download_and_after_the_time_limit(store, docx_bytes, settings):
    downloaded = store.create("a.docx", io.BytesIO(docx_bytes))
    idle = store.create("b.docx", io.BytesIO(docx_bytes))
    wait(store, downloaded)
    wait(store, idle)
    store.apply(downloaded)
    store.mark_downloaded(downloaded)
    now = time.time()
    assert store.sweep(now + 29 * 60) == 0
    assert store.sweep(now + 31 * 60) == 1
    with pytest.raises(KeyError):
        store.get(downloaded)
    assert store.get(idle)["status"] == "review"
    assert store.sweep(now + 23 * 3600) == 0
    assert store.sweep(now + 24 * 3600 + 60) == 1
    assert not (settings.data_dir / "jobs" / idle).exists()


def test_delete_removes_everything(store, docx_bytes, settings):
    job_id = store.create("a.docx", io.BytesIO(docx_bytes))
    wait(store, job_id)
    store.delete(job_id)
    assert not (settings.data_dir / "jobs" / job_id).exists()
    with pytest.raises(KeyError):
        store.get(job_id)


def test_ids_that_are_not_job_ids_are_never_used_as_paths(store, settings):
    (settings.data_dir / "secret.txt").parent.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "secret.txt").write_text("x")
    for bad in ("../secret.txt", "..", "a/b", "", "short"):
        with pytest.raises(KeyError):
            store.get(bad)
        with pytest.raises(KeyError):
            store.delete(bad)
    assert (settings.data_dir / "secret.txt").exists()


def test_jobs_run_one_at_a_time(settings, docx_bytes):
    gate = threading.Event()

    def handler(messages, tools):
        gate.wait(10)
        return {"role": "assistant", "content": "[]"}

    settings.use_agent = False
    store = JobStore(settings, context_factory=factory(FakeLLM(handler)))
    store.start()
    try:
        first = store.create("a.docx", io.BytesIO(docx_bytes))
        second = store.create("b.docx", io.BytesIO(docx_bytes))
        wait(store, first, statuses=("processing",))
        time.sleep(0.2)
        assert store.get(second)["status"] == "queued"
        gate.set()
        assert wait(store, first)["status"] == "review" and wait(store, second)["status"] == "review"
    finally:
        gate.set()
        store.stop()


def test_agent_is_used_when_enabled(settings, docx_bytes):
    script = [tool_call("scan_text", {}), tool_call("finish", {"summary": "ok"})]

    def handler(messages, tools):
        if tools is None:
            return {"role": "assistant", "content": "[]"}
        return script.pop(0)

    settings.use_agent = True
    store = JobStore(settings, context_factory=factory(FakeLLM(handler)))
    store.start()
    try:
        job_id = store.create("a.docx", io.BytesIO(docx_bytes))
        wait(store, job_id)
        assert store.context(job_id).outcome["finished"] is True
    finally:
        store.stop()


def test_interrupted_jobs_are_picked_up_again_after_a_restart(settings, docx_bytes):
    settings.use_agent = False
    first = JobStore(settings, context_factory=factory(keyword_llm(NAMES)))  # never started: the job stays queued
    job_id = first.create("a.docx", io.BytesIO(docx_bytes))
    assert first.get(job_id)["status"] == "queued"
    second = JobStore(settings, context_factory=factory(keyword_llm(NAMES)))
    second.start()
    try:
        assert wait(second, job_id)["status"] == "review"
    finally:
        second.stop()


def test_log_never_contains_document_content(store, docx_bytes, caplog):
    with caplog.at_level(logging.DEBUG, logger="deid"):
        job_id = store.create("王小明的報告.docx", io.BytesIO(docx_bytes))
        wait(store, job_id)
        store.apply(job_id)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert job_id[:8] in text and "review" in text
    assert "王小明" not in text and "林志豪" not in text


# ---- the code function (a job uploaded for codes instead of masks) -------------------------------



def test_a_job_remembers_which_function_it_was_uploaded_for(store, docx_bytes):
    plain = store.create("a.docx", io.BytesIO(docx_bytes))
    coded = store.create("a.docx", io.BytesIO(docx_bytes), mode="code")
    assert store.get(plain)["mode"] == "mask" and store.get(coded)["mode"] == "code"
    with pytest.raises(ValueError):
        store.create("a.docx", io.BytesIO(docx_bytes), mode="both")


def test_export_writes_the_coded_text_and_the_table_and_asks_for_no_password(store, docx_bytes, settings):
    from deid.restorefile import open_
    job_id = store.create("a.docx", io.BytesIO(docx_bytes), mode="code")
    wait(store, job_id)
    result = store.export(job_id)
    assert result["ok"] and result["kind"] == "code" and result["stats"]["codes"] == 1
    text = store.export_path(job_id, "text").read_text(encoding="utf-8")
    assert "承辦人 [姓名1]" in text and "王小明" not in text
    assert open_(store.export_path(job_id, "restore").read_bytes()) == {"[姓名1]": "王小明"}
    assert store.get(job_id)["result"]["kind"] == "code"
    folder = settings.data_dir / "jobs" / job_id
    for path in folder.iterdir():  # of what was made, only the table holds the original
        if path.is_file() and not path.name.startswith(("original", "inventory", "findings", "export.restore")):
            assert "王小明".encode("utf-8") not in path.read_bytes(), path.name
    assert (folder / "export.restore").stat().st_mode & 0o777 == 0o600


def test_export_also_writes_the_document_with_codes_in_its_own_format(store, docx_bytes, settings):
    from deid import replyfile
    job_id = store.create("a.docx", io.BytesIO(docx_bytes), mode="code")
    wait(store, job_id)
    result = store.export(job_id)
    assert result["file"] == {"ext": ".docx", "placed": 1, "left": 0} and result["file_notes"] == []
    path = store.export_path(job_id, "coded")
    assert path.suffix == ".docx" and path.stat().st_mode & 0o777 == 0o600
    assert replyfile.read(path.read_bytes(), "a.docx").text == "承辦人 [姓名1]\n一般內容"
    data = path.read_bytes()
    assert sorted(p.name for p in (settings.data_dir / "jobs" / job_id).glob("coded*")) == ["coded.docx"]
    store.export(job_id)  # made again, it is the same file: nothing in it depends on when it was made
    assert store.export_path(job_id, "coded").read_bytes() == data


def test_a_pdf_gets_the_coded_text_only_and_is_told_why(store, tmp_path):
    from deid import codefile
    pdf = make_pdf(tmp_path / "a.pdf", [{"lines": ["承辦人 王小明"]}]).read_bytes()
    job_id = store.create("a.pdf", io.BytesIO(pdf), mode="code")
    wait(store, job_id)
    result = store.export(job_id)
    assert result["ok"] and result["file"] is None and result["file_notes"] == [codefile.PDF_NOTE]
    with pytest.raises(FileNotFoundError):
        store.export_path(job_id, "coded")


def test_the_two_functions_do_not_produce_each_others_output(store, docx_bytes):
    from deid.jobs import WrongMode
    masked = store.create("a.docx", io.BytesIO(docx_bytes))
    coded = store.create("a.docx", io.BytesIO(docx_bytes), mode="code")
    wait(store, masked)
    wait(store, coded)
    with pytest.raises(WrongMode):
        store.export(masked)
    with pytest.raises(WrongMode):
        store.apply(coded)
    with pytest.raises(FileNotFoundError):
        store.export_path(coded, "text")


def test_editing_findings_removes_the_coded_text_and_the_table(store, docx_bytes):
    job_id = store.create("a.docx", io.BytesIO(docx_bytes), mode="code")
    wait(store, job_id)
    store.export(job_id)
    ctx = store.context(job_id)
    ctx.store.set_on(ctx.store.findings[0].id, False)
    store.save_findings(job_id, ctx.store)
    assert store.get(job_id)["result"] is None
    for what in ("text", "restore", "coded"):
        with pytest.raises(FileNotFoundError):
            store.export_path(job_id, what)


def test_export_log_has_counts_only(store, docx_bytes, caplog):
    with caplog.at_level(logging.DEBUG, logger="deid"):
        job_id = store.create("a.docx", io.BytesIO(docx_bytes), mode="code")
        wait(store, job_id)
        store.export(job_id)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "coded" in text and "file=made placed=1 left=0" in text and "王小明" not in text and "姓名1" not in text


def test_a_code_job_asks_which_names_are_one_person_and_a_mask_job_does_not(settings, tmp_path):
    from tests.fakes import routed
    settings.use_agent, settings.link_names = False, True
    names = {"王小明": "姓名", "小明": "姓名"}
    data = make_docx(tmp_path / "n.docx", ["王小明到場。", "小明另提供資料。"]).read_bytes()

    def turns():
        script = [tool_call("link", {"texts": ["王小明", "小明"], "reason": "同一段"}), tool_call("finish", {})]
        return lambda messages, tools: script.pop(0)

    for mode, groups in (("code", {"g1"}), ("mask", {""})):
        s = JobStore(settings, context_factory=factory(routed({"指的是同一個人": turns()}, table=names)))
        s.start()
        try:
            job_id = s.create("n.docx", io.BytesIO(data), mode=mode)
            assert wait(s, job_id)["status"] == "review"
            assert {f.group for f in s.context(job_id).store.findings if f.type == "姓名"} == groups
        finally:
            s.stop()
