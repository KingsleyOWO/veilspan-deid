"""The model step added after detection (which names are one person), against the real model, on made-up
documents. The list of findings is put together by hand, so what is measured is the step itself and not
detection. Each sample is
run three times: the model does not answer the same way every time, and the requirement says how often it
must be right (design document, "2026-11-18 變更", 上線前的量測)."""
import pytest

from deid.agent.samename import link_names
from deid.llm import LLMClient
from deid.pipeline import build_context
from tests.builders import make_docx
from tests.fakes import FakeFaces, FakeOcr

RUNS = 3


def context(tmp_path, live_settings, lines, findings, n):
    live_settings.data_dir = tmp_path / "data"
    doc = make_docx(tmp_path / f"s{n}.docx", lines)
    ctx = build_context(doc, tmp_path / f"work{n}", live_settings, llm=LLMClient(live_settings), ocr=FakeOcr(), faces=FakeFaces())
    ctx.load()
    for text, kind in findings:
        assert ctx.store.add_text(text, kind, "model") is not None, text
    return ctx


def group_of(ctx, text):
    return next(f.group for f in ctx.store.findings if f.text == text)


# ---- which names are one person ----------------------------------------------------------

@pytest.mark.live
def test_three_writings_of_one_person_are_linked_and_another_person_is_not(tmp_path, live_settings):
    lines = ["會議紀錄：出席者王小明（Wang Xiaoming）、陳美玲。", "小明報告系統上線進度，陳美玲補充測試結果。",
             "會後 Wang Xiaoming 將寄出紀錄，小明並負責追蹤。"]
    names = [(n, "姓名") for n in ("王小明", "小明", "Wang Xiaoming", "陳美玲")]
    seen = []
    for n in range(RUNS):
        ctx = context(tmp_path, live_settings, lines, names, n)
        out = link_names(ctx)
        seen.append({"out": out, "groups": [[f.text for f in g] for g in ctx.store.groups().values()]})
        same = group_of(ctx, "王小明")
        assert not (same and group_of(ctx, "陳美玲") == same), seen  # another person is never taken in
    linked = sum(1 for s in seen if any({"王小明", "小明"} <= set(g) for g in s["groups"]))
    assert linked >= 2, seen


@pytest.mark.live
def test_a_surname_shared_by_two_people_is_linked_to_neither(tmp_path, live_settings):
    lines = ["出席者：王小明、王大華、陳美玲。", "王小明報告預算，王大華報告人事。", "會後王先生表示將再確認。"]
    names = [(n, "姓名") for n in ("王小明", "王大華", "王先生", "陳美玲")]
    for n in range(RUNS):
        ctx = context(tmp_path, live_settings, lines, names, n)
        out = link_names(ctx)
        groups = [[f.text for f in g] for g in ctx.store.groups().values()]
        assert group_of(ctx, "王先生") == "", (out, groups)
        assert not (group_of(ctx, "王小明") and group_of(ctx, "王小明") == group_of(ctx, "王大華")), (out, groups)
