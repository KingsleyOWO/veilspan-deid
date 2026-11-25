"""Service fixes of 2026-11-24, third round (review of 2026-11-23): a pattern the model wrote is stopped at the time
limit, a retry is taken once, a job's lock is never forgotten while it is held, an empty environment variable does
not erase .env, the progress line says in Chinese what the agent is doing, and a place the program cannot mask is
reported once."""
import errno
import io
import json
import re
import secrets
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import deid.jobs as jobs
from deid.agent import sandbox, tools
from deid.agent.loop import run_agent
from deid.config import load_settings
from deid.jobs import JobStore, JobWriteError
from deid.model import Inventory, TextUnit
from deid.pipeline import build_context
from deid.web.access import AllowedClients
from deid.web.app import create_app
from tests.builders import make_docx
from tests.fakes import FakeFaces, FakeLLM, FakeOcr, keyword_llm, reply, tool_call


@pytest.fixture
def docx_bytes(tmp_path):
    return make_docx(tmp_path / "s.docx", ["承辦人 王小明", "會議由陳美玲主持。"]).read_bytes()


@pytest.fixture
def no_sandbox(monkeypatch):
    monkeypatch.setattr(sandbox, "available", lambda settings: False)  # no Docker call from these tests


def wait_for(store, job_id, statuses=("review", "failed"), timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        job = store.get(job_id)
        if job["status"] in statuses:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job stuck in {store.get(job_id)['status']}")


def upload(client, data, name="a.docx"):
    r = client.post("/api/jobs", files={"file": (name, io.BytesIO(data), "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ---- B1: a pattern the model wrote is stopped at the time limit, however many units there are -------------

SLOW = r"(.*)(.*)(.*)\d{5}"  # backtracks over every paragraph without five digits: some milliseconds each
PARA = ("本案由承辦人王小明於會議中說明，相關資料請參閱附件，聯絡方式另行通知。" * 6)[:210]


def units(n, text=PARA):
    inv = Inventory(kind="docx")
    inv.texts = [TextUnit(id=f"t{i}", text=text, section="內文") for i in range(n)]
    return SimpleNamespace(inv=inv)


def test_a_slow_pattern_over_many_paragraphs_is_stopped_at_the_time_limit(monkeypatch):
    monkeypatch.setattr(tools, "SEARCH_SECONDS", 0.5)
    ctx = units(400)  # each paragraph alone takes less than the 0.05 s every unit used to get after the limit
    started = time.monotonic()
    res = tools.find(ctx, SLOW, True)
    took = time.monotonic() - started
    assert took < 1.5, f"the search ran {took:.1f} s against a limit of 0.5 s"
    assert "逾時" in res.get("error", "")


def test_a_search_cut_short_over_many_units_is_told_like_one_unit_that_timed_out(monkeypatch):
    monkeypatch.setattr(tools, "SEARCH_SECONDS", 0.3)
    one = tools.find(units(1, "\n".join([PARA] * 100)), SLOW, True)  # one long unit: the engine's own time limit
    many = tools.find(units(400), SLOW, True)
    assert "error" in one and many == one


def test_a_search_that_finishes_in_time_still_returns_every_hit():
    res = tools.find(units(300, "承辦人王小明，電話另洽。"), "王小明", False)
    assert res["total"] == 300 and len(res["hits"]) == tools.MAX_HITS


# ---- B2: two retries at the same moment: one is taken ------------------------------------------------------

def failed_job(store, data):
    job_id = store.create("a.docx", io.BytesIO(data))
    store._queue.queue.clear()  # the worker is not running: only the queue is looked at
    store._update(job_id, status="failed", stage="失敗", error="處理失敗（測試）")
    return job_id


def slow_reads(monkeypatch, store, seconds=0.05):
    """Every read of job.json takes a moment, so two retries that are not kept apart both read "failed"."""
    real = store._read

    def read(job_id):
        job = real(job_id)
        time.sleep(seconds)
        return job

    monkeypatch.setattr(store, "_read", read)


def both_at_once(fn):
    gate, out = threading.Barrier(2), []

    def go():
        gate.wait()
        out.append(fn())

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    return out


def test_two_retries_at_the_same_moment_queue_the_job_once(settings, docx_bytes, monkeypatch):
    store = JobStore(settings)
    job_id = failed_job(store, docx_bytes)
    slow_reads(monkeypatch, store)

    def retry():
        try:
            store.retry(job_id)
            return "taken"
        except ValueError:
            return "refused"

    assert sorted(both_at_once(retry)) == ["refused", "taken"]
    assert list(store._queue.queue) == [job_id]
    assert store.get(job_id)["status"] == "queued"


def test_two_retry_requests_at_the_same_moment_one_is_answered_like_a_job_that_has_not_failed(settings, docx_bytes,
                                                                                               monkeypatch):
    store = JobStore(settings)  # not started: the queue stays as the requests leave it
    job_id = failed_job(store, docx_bytes)
    slow_reads(monkeypatch, store)
    with TestClient(create_app(settings, store)) as c:
        answers = both_at_once(lambda: c.post(f"/api/jobs/{job_id}/retry"))
    assert sorted(r.status_code for r in answers) == [200, 409]
    refused = next(r for r in answers if r.status_code == 409)
    assert refused.json()["error"] == "只有失敗的工作可以重試"
    assert list(store._queue.queue) == [job_id]


def counting_store(settings):
    """A store whose factory notes every analysis (a context that is not restored from disk)."""
    settings.use_agent = False
    analysed = []
    llm = keyword_llm({"王小明": "姓名"})

    def factory(src, workdir, s, progress=None, restore=False):
        if not restore:
            analysed.append(workdir.name)
        return build_context(src, workdir, s, llm=llm, progress=progress, restore=restore, ocr=FakeOcr(), faces=FakeFaces())

    return JobStore(settings, context_factory=factory), analysed


def test_a_job_taken_from_the_queue_again_after_review_keeps_the_reviewers_list(settings, docx_bytes):
    store, analysed = counting_store(settings)
    job_id = store.create("a.docx", io.BytesIO(docx_bytes))
    store._process(job_id)
    assert store.get(job_id)["status"] == "review"
    ctx = store.context(job_id)
    assert ctx.store.add_text("陳美玲", "姓名", "user") is not None  # the reviewer adds what the model missed
    store.save_findings(job_id, ctx.store)
    store._process(job_id)  # the same id comes out of the queue a second time
    assert analysed == [job_id], "analysed a second time"
    assert store.get(job_id)["status"] == "review"
    assert any(f.text == "陳美玲" and f.on for f in store.context(job_id).store.findings), "the reviewer's edit is gone"


def test_a_job_in_the_queue_twice_is_analysed_once_by_the_worker(settings, docx_bytes):
    store, analysed = counting_store(settings)
    first = store.create("a.docx", io.BytesIO(docx_bytes))
    store._queue.put(first)  # in the queue a second time, whatever the cause
    after = store.create("b.docx", io.BytesIO(docx_bytes))  # one worker takes them in order: once this is done, so is the double
    store.start()
    try:
        wait_for(store, after, ("review",))
        assert analysed.count(first) == 1 and analysed.count(after) == 1
        assert store.get(first)["status"] == "review"
    finally:
        store.stop()


def test_a_retry_that_cannot_be_written_is_not_queued_and_the_page_says_why(settings, docx_bytes, monkeypatch):
    store = JobStore(settings)
    job_id = failed_job(store, docx_bytes)
    real = store._update

    def update(jid, **changes):
        if changes.get("status") == "queued":
            raise JobWriteError(OSError(errno.ENOSPC, "No space left on device"))
        return real(jid, **changes)

    monkeypatch.setattr(store, "_update", update)
    with TestClient(create_app(settings, store)) as c:
        r = c.post(f"/api/jobs/{job_id}/retry")
        job = c.get(f"/api/jobs/{job_id}").json()
    assert r.status_code == 503 and "磁碟" in r.json()["error"]
    assert list(store._queue.queue) == [], "a job whose status still says failed is not handed to the worker"
    assert job["status"] == "failed" and "磁碟" in job["error"], "the page, which reloads the job, tells why"


def test_a_job_that_cannot_be_marked_queued_at_start_fails_instead_of_staying_processing(settings, docx_bytes, monkeypatch):
    # The worker takes only a job marked queued, so a job that start() cannot mark must not be left as it was.
    job_id = JobStore(settings).create("a.docx", io.BytesIO(docx_bytes))
    JobStore(settings)._update(job_id, status="processing", stage="偵測文字中的個資")  # the service stopped while it ran
    store, analysed = counting_store(settings)
    full = [True]
    real = store._update

    def update(jid, **changes):
        if full[0] and changes.get("status") == "queued":
            raise JobWriteError(OSError(errno.ENOSPC, "No space left on device"))
        return real(jid, **changes)

    monkeypatch.setattr(store, "_update", update)
    store.start()
    try:
        job = store.get(job_id)
        assert job["status"] == "failed" and "磁碟" in job["error"] and "重試" in job["error"]
        assert analysed == []
        full[0] = False  # room again
        store.retry(job_id)
        assert wait_for(store, job_id, ("review",))["status"] == "review" and analysed == [job_id]
    finally:
        store.stop()


# ---- B3: a lock that is held is never dropped from the table -----------------------------------------------

def test_an_edit_waits_for_the_masked_file_however_many_other_ids_came_by(settings, docx_bytes, monkeypatch):
    settings.use_agent = False
    llm = keyword_llm({"王小明": "姓名"})  # the model misses 陳美玲; the reviewer adds her by hand

    def factory(src, workdir, s, progress=None, restore=False):
        return build_context(src, workdir, s, llm=llm, progress=progress, restore=restore, ocr=FakeOcr(), faces=FakeFaces())

    store = JobStore(settings, context_factory=factory)
    store.start()
    making, finish = threading.Event(), threading.Event()
    real = jobs.apply_and_verify

    def slow(ctx):  # a big document: the list is loaded, and making the file takes a while
        making.set()
        finish.wait(10)
        return real(ctx)

    monkeypatch.setattr(jobs, "apply_and_verify", slow)
    try:
        with TestClient(create_app(settings, store)) as c:
            job_id = upload(c, docx_bytes)
            wait_for(store, job_id, ("review",))
            out = {}
            applying = threading.Thread(target=lambda: out.update(apply=c.post(f"/api/jobs/{job_id}/apply")))
            applying.start()
            assert making.wait(10)
            for _ in range(502):  # ids of jobs that do not exist: each is answered 404
                assert c.patch(f"/api/jobs/{secrets.token_urlsafe(16)}/findings/f1", json={"on": True}).status_code == 404
            adding = threading.Thread(target=lambda: out.update(
                add=c.post(f"/api/jobs/{job_id}/findings", json={"text": "陳美玲", "type": "姓名"})))
            adding.start()
            adding.join(0.5)
            waited = adding.is_alive()
            finish.set()
            applying.join(10)
            adding.join(10)
            # Looked at once both requests are done: a test client left with a request under way does not close.
            result = c.get(f"/api/jobs/{job_id}").json()["result"]
            download = c.get(f"/api/jobs/{job_id}/download").status_code
    finally:
        finish.set()
        store.stop()
    assert waited, "the edit ran while the masked file was being made from the old list"
    assert out["apply"].status_code == 200 and out["add"].status_code == 200
    assert result is None, "the file made before the edit is recorded as the result of the list after it"
    assert download == 409


def test_two_requests_for_one_job_share_its_lock_and_finished_requests_leave_nothing_behind():
    from deid.web.app import JobLocks
    locks = JobLocks()
    holding, release, second_in = threading.Event(), threading.Event(), threading.Event()

    def first():
        with locks("job-a"):
            holding.set()
            release.wait(10)

    def second():
        with locks("job-a"):
            second_in.set()

    a = threading.Thread(target=first)
    a.start()
    assert holding.wait(5)
    for _ in range(600):
        with locks(secrets.token_urlsafe(16)):
            pass
    b = threading.Thread(target=second)
    b.start()
    assert not second_in.wait(0.3), "a second request got another lock while the job's lock was held"
    release.set()
    a.join(5)
    b.join(5)
    assert second_in.is_set()
    with pytest.raises(KeyError):
        with locks("no-such-job"):
            raise KeyError("no-such-job")  # a request for a job that does not exist ends in a 404
    assert len(locks) == 0, "ids nobody holds any more stay in the table"


# ---- B4: settings ------------------------------------------------------------------------------------------

def env_file(tmp_path, text):
    path = tmp_path / "x.env"
    path.write_text(text, encoding="utf-8")
    return path


def test_an_empty_allowed_clients_variable_does_not_erase_the_list_in_env(tmp_path):
    env = env_file(tmp_path, f"ALLOWED_CLIENTS=127.0.0.1,192.0.2.0/24\nDATA_DIR={tmp_path / 'data'}\n")
    for empty in ("", "  "):
        s = load_settings(env, environ={"ALLOWED_CLIENTS": empty})
        assert s.allowed_clients == "127.0.0.1,192.0.2.0/24"
    app = create_app(s)
    assert any(m.cls is AllowedClients for m in app.user_middleware), "the access check is not installed"


def test_an_empty_variable_falls_back_to_env_for_every_setting(tmp_path):
    env = env_file(tmp_path, "LLM_API_KEY=filekey\nMAX_UPLOAD_MB=50\nUSE_AGENT=false\n")
    s = load_settings(env, environ={"LLM_API_KEY": "", "MAX_UPLOAD_MB": "", "USE_AGENT": " "})
    assert (s.llm_api_key, s.max_upload_mb, s.use_agent) == ("filekey", 50, False)
    s = load_settings(env, environ={"LLM_API_KEY": "envkey", "MAX_UPLOAD_MB": "70"})
    assert (s.llm_api_key, s.max_upload_mb) == ("envkey", 70), "a value that is given still wins over .env"


def test_a_comment_after_a_number_in_env_stops_the_start_with_the_settings_name(tmp_path):
    env = env_file(tmp_path, "MAX_UPLOAD_MB=200  # 上限\n")
    with pytest.raises(ValueError) as e:
        load_settings(env, environ={})
    assert "MAX_UPLOAD_MB" in str(e.value) and "註解" in str(e.value)


def test_a_comment_after_a_text_setting_in_env_is_not_taken_into_the_value(tmp_path):
    env = env_file(tmp_path, "MASK_FULL=■  # 全形方塊\n")
    with pytest.raises(ValueError, match="MASK_FULL"):
        load_settings(env, environ={})
    assert load_settings(env_file(tmp_path, "MASK_HALF=#\n"), environ={}).mask_half == "#", "a # that is the value"


def test_a_value_that_is_not_a_number_names_the_setting(tmp_path):
    with pytest.raises(ValueError, match="PORT") as e:
        load_settings(tmp_path / "none.env", environ={"PORT": "八千"})
    assert "整數" in str(e.value)
    with pytest.raises(ValueError, match="FACE_SCORE"):
        load_settings(env_file(tmp_path, "FACE_SCORE=高\n"), environ={})


def test_a_misspelt_switch_stops_the_start_with_the_settings_name(tmp_path):
    for line in ("USE_AGENT=ture", "FACE_DEFAULT_ON=Ture", "LINK_NAMES=n"):
        name = line.split("=")[0]
        with pytest.raises(ValueError) as e:
            load_settings(env_file(tmp_path, line + "\n"), environ={})
        assert name in str(e.value) and "true" in str(e.value)
    with pytest.raises(ValueError, match="USE_AGENT"):
        load_settings(tmp_path / "none.env", environ={"USE_AGENT": "ture"})
    with pytest.raises(ValueError) as e:  # every setting that cannot be read is named at once
        load_settings(env_file(tmp_path, "USE_AGENT=ture\nMAX_UPLOAD_MB=200  # 上限\n"), environ={})
    assert "USE_AGENT" in str(e.value) and "MAX_UPLOAD_MB" in str(e.value)


def test_the_spellings_of_a_switch_read_before_are_read_the_same(tmp_path):
    for raw, want in (("true", True), ("TRUE", True), ("1", True), ("yes", True), ("on", True),
                      ("false", False), ("False", False), ("0", False), ("no", False), ("off", False)):
        assert load_settings(tmp_path / "none.env", environ={"USE_AGENT": raw}).use_agent is want, raw


# ---- B5: the progress line says in Chinese what the step does ----------------------------------------------

EVERY_TOOL = [t["function"]["name"] for t in tools.SCHEMAS]


def test_the_progress_line_names_each_step_in_chinese_and_an_unknown_tool_not_at_all(tmp_path, settings, monkeypatch,
                                                                                    no_sandbox):
    stages = []
    doc = make_docx(tmp_path / "a.docx", ["承辦人 王小明", "一般內容。"])
    turn = {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": "{}"}}
        for i, name in enumerate(EVERY_TOOL + ["drop_everything"])]}
    llm = FakeLLM(lambda messages, schemas: turn if schemas is not None else reply([]))
    ctx = build_context(doc, tmp_path / "work", settings, llm=llm, ocr=FakeOcr(), faces=FakeFaces(),
                        progress=lambda stage, fraction: stages.append(stage))
    ctx.load()
    # Only the progress line is looked at here: every tool answers the same, and finish ends the run.
    monkeypatch.setattr(tools, "call", lambda ctx, name, args: {"ok": True})
    assert run_agent(ctx).finished
    named = [s for s in stages if s.startswith("代理檢查中（第 1 步：")]
    assert len(named) == len(EVERY_TOOL) == len(set(named)), named
    assert not any(re.search(r"[A-Za-z_]", s) for s in stages if s.startswith("代理檢查中")), stages
    assert stages.count("代理檢查中（第 1 步）") == 2, "the step itself, then the tool without a phrase, shown without its name"


def test_scanning_the_text_reads_as_detection_on_the_processing_screen(tmp_path, settings, no_sandbox):
    stages = []
    doc = make_docx(tmp_path / "a.docx", ["承辦人 王小明", "一般內容。"])
    script = [tool_call("get_inventory", {}), tool_call("scan_text", {}), tool_call("finish", {"summary": "完成"})]

    def handler(messages, schemas):
        if schemas is None:
            return reply([{"text": "王小明", "type": "姓名"}] if "王小明" in messages[-1]["content"] else [])
        return script.pop(0)

    ctx = build_context(doc, tmp_path / "work", settings, llm=FakeLLM(handler), ocr=FakeOcr(), faces=FakeFaces(),
                        progress=lambda stage, fraction: stages.append(stage))
    ctx.load()
    assert run_agent(ctx).finished
    assert "代理檢查中（第 2 步：偵測文字中的個資）" in stages, stages


# ---- B6: a place the program cannot mask is listed once ----------------------------------------------------

def test_a_place_the_program_cannot_mask_is_listed_once_on_the_screen_and_in_the_report(settings, docx_bytes, no_sandbox):
    where = "內嵌的 OLE 物件 oleObject1.bin"
    script = [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "scan_text", "arguments": "{}"}},
        {"id": "c2", "type": "function", "function": {"name": "report_unmaskable", "arguments": json.dumps(
            {"where": where, "text": "林志豪", "reason": "寫在內嵌物件裡"}, ensure_ascii=False)}}]},
        tool_call("finish", {"summary": "完成"})]

    def handler(messages, schemas):
        if schemas is None:
            content = messages[-1]["content"]
            return reply([{"text": "王小明", "type": "姓名"}] if isinstance(content, str) and "王小明" in content else [])
        return script.pop(0) if script else {"role": "assistant", "content": "完成"}

    llm = FakeLLM(handler)

    def factory(src, workdir, s, progress=None, restore=False):
        return build_context(src, workdir, s, llm=llm, progress=progress, restore=restore, ocr=FakeOcr(), faces=FakeFaces())

    store = JobStore(settings, context_factory=factory)
    store.start()
    try:
        with TestClient(create_app(settings, store)) as c:
            job_id = upload(c, docx_bytes)
            wait_for(store, job_id, ("review",))
            rev = c.get(f"/api/jobs/{job_id}/review").json()
            report = json.loads(c.get(f"/api/jobs/{job_id}/report").content)
    finally:
        store.stop()
    assert rev["outcome"]["finished"] is True
    for data in (rev, report):
        listed = [w for w in data["warnings"] if "oleObject1.bin" in w] + [u for u in data["unmaskable"] if "oleObject1.bin" in u["where"]]
        assert listed == [{"where": where, "text": "林志豪", "reason": "寫在內嵌物件裡"}], listed
