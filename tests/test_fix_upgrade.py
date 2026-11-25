"""A job analysed before the way documents are read changed is analysed again when the service comes up with
the new code. What was saved for it - findings by unit number and position - no longer fits the units the new
code reads (2026-11-24: hidden parts are read that were not, a header's text lost its format codes), and masking
by it would mask the wrong places. All names are made up."""
import json
import time

import pytest

from deid import jobs
from deid.jobs import JobStore
from deid.pipeline import build_context
from tests.builders import make_docx
from tests.fakes import FakeFaces, FakeOcr, keyword_llm


def started(settings, llm):
    def make(src, workdir, settings, progress=None, restore=False):
        return build_context(src, workdir, settings, llm=llm, progress=progress, ocr=FakeOcr(), faces=FakeFaces(), restore=restore)
    store = JobStore(settings, context_factory=make)
    store.start()
    return store


def wait(store, job_id, statuses=("review", "failed"), timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if store.get(job_id)["status"] in statuses:
            return store.get(job_id)
        time.sleep(0.02)
    raise AssertionError(f"job stayed {store.get(job_id)['status']}")


@pytest.fixture
def reviewed(tmp_path, settings):
    """A job that reached review and had its masked file made, and the store stopped (the service going down)."""
    import io
    settings.use_agent = False
    llm = keyword_llm({"王小明": "姓名"})
    store = started(settings, llm)
    data = make_docx(tmp_path / "src.docx", ["承辦人 王小明", "一般內容"]).read_bytes()
    job_id = store.create("a.docx", io.BytesIO(data))
    wait(store, job_id)
    store.apply(job_id)
    store.stop()
    return settings, llm, job_id


def job_file(settings, job_id):
    return settings.data_dir / "jobs" / job_id / "job.json"


def test_a_finished_analysis_is_stamped_with_the_way_it_was_read(reviewed):
    settings, _llm, job_id = reviewed
    assert json.loads(job_file(settings, job_id).read_text(encoding="utf-8"))["analysis"] == jobs.ANALYSIS


def test_a_job_read_the_current_way_is_left_as_it_is_after_a_restart(reviewed):
    settings, llm, job_id = reviewed
    asked = len(llm.calls)
    store = started(settings, llm)
    try:
        time.sleep(0.3)
        job = store.get(job_id)
        assert job["status"] == "review" and job["result"] is not None and len(llm.calls) == asked
        assert store.output_path(job_id).is_file()
    finally:
        store.stop()


@pytest.mark.parametrize("stamp", [None, "older"], ids=["no stamp", "another stamp"])
def test_a_job_read_an_older_way_is_analysed_again_after_an_upgrade(reviewed, stamp):
    settings, llm, job_id = reviewed
    path = job_file(settings, job_id)
    job = json.loads(path.read_text(encoding="utf-8"))
    if stamp is None:
        job.pop("analysis")
    else:
        job["analysis"] = stamp
    path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    output = settings.data_dir / "jobs" / job_id / "output.docx"
    assert output.is_file()
    asked = len(llm.calls)
    store = started(settings, llm)
    try:
        job = wait(store, job_id, statuses=("review",))
        deadline = time.time() + 20
        while len(llm.calls) == asked and time.time() < deadline:  # "review" may still be the state from before
            time.sleep(0.02)
        job = wait(store, job_id, statuses=("review",))
        assert len(llm.calls) > asked and job["analysis"] == jobs.ANALYSIS
        assert job["result"] is None and not output.exists()  # what was made from the old reading is not offered
        assert {f.text for f in store.context(job_id).store.findings} >= {"王小明"}
    finally:
        store.stop()


def test_the_reviewer_is_told_that_the_document_was_analysed_again(reviewed):
    settings, llm, job_id = reviewed
    path = job_file(settings, job_id)
    job = json.loads(path.read_text(encoding="utf-8"))
    job.pop("analysis")
    path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    asked = len(llm.calls)
    store = started(settings, llm)
    try:
        deadline = time.time() + 20
        while (len(llm.calls) == asked or store.get(job_id)["status"] != "review") and time.time() < deadline:
            time.sleep(0.02)
        assert any("重新分析" in w for w in store.context(job_id).warnings)
    finally:
        store.stop()
