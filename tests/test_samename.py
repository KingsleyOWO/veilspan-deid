"""After detection, a code job asks the model which names on the list are one person. The model proposes;
the program accepts only strings that are active names on the list. All names are made up."""
import json

import pytest

from deid.agent import samename
from deid.agent.samename import link_names
from deid.llm import LLMError
from deid.pipeline import build_context
from tests.builders import make_docx, text_image
from tests.fakes import FakeFaces, FakeOcr, ocr_line, routed, tool_call

TABLE = {"王小明": "姓名", "小明": "姓名", "王先生": "姓名", "陳美玲": "姓名"}
ROUTE = "指的是同一個人"  # words of samename.SYSTEM


def model(script):
    """Detection requests are answered from TABLE; the same-person step gets `script`, one turn at a time."""
    script = list(script)

    def turn(messages, tools):
        if not script:
            return {"role": "assistant", "content": "我不知道接下來要做什麼"}
        step = script.pop(0)
        return step(messages) if callable(step) else step

    return routed({ROUTE: turn}, table=TABLE)


@pytest.fixture
def make(tmp_path, settings):
    def build(script, **kw):
        doc = make_docx(tmp_path / "a.docx", ["王小明到場。", "小明另提供資料。", "王先生與陳美玲核對，電話 0912-345-678。"],
                        images=[("image1.png", text_image(["x"], size=(400, 200)))])
        llm = model(script)
        ctx = build_context(doc, tmp_path / "work", settings, llm=llm, ocr=kw.get("ocr", FakeOcr([ocr_line("登入者：小明")])),
                            faces=FakeFaces())
        ctx.load()
        ctx.scan_text()
        ctx.add_safety_net()
        return ctx, llm
    return build


def asked(llm):
    return [c for c in llm.calls if c["tools"] is not None]


def answer(call, n=-1):
    """What the model was told by the n-th tool result of a request."""
    return json.loads([m for m in call["messages"] if m["role"] == "tool"][n]["content"])


def groups(ctx):
    return {g: [f.text for f in members] for g, members in ctx.store.groups().items()}


def test_names_the_model_links_become_one_group(make):
    ctx, llm = make([tool_call("list_names", {}), tool_call("link", {"texts": ["王小明", "小明"], "reason": "同一段"}),
                     tool_call("finish", {})])
    out = link_names(ctx)
    assert out == {"candidates": 4, "groups": 1, "finished": True, "reason": None}
    assert groups(ctx) == {"g1": ["王小明", "小明"]} and ctx.warnings == []
    first = asked(llm)[0]
    assert first["messages"][0]["content"] == samename.SYSTEM
    assert [t["function"]["name"] for t in first["tools"]] == ["list_names", "search", "read_text", "link", "finish"]
    listed = answer(asked(llm)[1])
    assert {n["text"]: n["count"] for n in listed["names"]} == {"王小明": 1, "小明": 2, "王先生": 1, "陳美玲": 1}


def test_a_link_is_refused_unless_every_string_is_an_active_name_on_the_list(make):
    ctx, llm = make([tool_call("link", {"texts": ["王小明", "王小名"], "reason": "x"}),
                     tool_call("link", {"texts": ["王小明", "0912-345-678"], "reason": "x"}),
                     tool_call("link", {"texts": ["王小明", "陳美玲"], "reason": "x"}),
                     tool_call("link", {"texts": ["王小明"], "reason": "x"}),
                     tool_call("link", {"texts": ["王小明", "王小明"], "reason": "x"}),
                     tool_call("link", {"texts": "王小明", "reason": "x"}),
                     tool_call("finish", {})])
    off = next(f for f in ctx.store.findings if f.text == "陳美玲")
    ctx.store.set_on(off.id, False)
    out = link_names(ctx)
    replies = [answer(c) for c in asked(llm)[1:]]
    assert all(r["ok"] is False and r["error"] for r in replies)
    assert "王小名" in replies[0]["error"] and "0912-345-678" in replies[1]["error"] and "陳美玲" in replies[2]["error"]
    assert "至少要兩個" in replies[3]["error"] and "至少要兩個" in replies[4]["error"]
    assert groups(ctx) == {} and out["groups"] == 0 and out["finished"] is True


def test_a_writing_already_in_a_group_cannot_join_another(make):
    ctx, llm = make([tool_call("link", {"texts": ["王小明", "小明"], "reason": "x"}),
                     tool_call("link", {"texts": ["陳美玲", "王先生"], "reason": "x"}),
                     tool_call("link", {"texts": ["小明", "王先生"], "reason": "x"}),
                     tool_call("finish", {})])
    link_names(ctx)
    refused = answer(asked(llm)[3])
    assert refused["ok"] is False and "已經屬於另一組" in refused["error"]
    assert groups(ctx) == {"g1": ["王小明", "小明"], "g2": ["王先生", "陳美玲"]}


def test_a_group_can_take_in_one_more_writing(make):
    ctx, llm = make([tool_call("link", {"texts": ["王小明", "小明"], "reason": "x"}),
                     tool_call("link", {"texts": ["小明", "王先生"], "reason": "只有一位姓王"}), tool_call("finish", {})])
    assert link_names(ctx)["groups"] == 1
    assert groups(ctx) == {"g1": ["王小明", "小明", "王先生"]}
    assert answer(asked(llm)[2]) == {"ok": True, "group": ["王小明", "小明", "王先生"]}


def test_fewer_than_two_names_asks_the_model_nothing(make):
    ctx, llm = make([])
    for f in ctx.store.findings:
        if f.type == "姓名" and f.text != "王小明":
            ctx.store.set_on(f.id, False)
    before = len(llm.calls)
    assert link_names(ctx) == {"candidates": 1, "groups": 0, "finished": True, "reason": None}
    assert len(llm.calls) == before and ctx.warnings == []


def test_an_endpoint_failure_keeps_what_was_linked_and_warns(make):
    def boom(messages):
        raise LLMError("down")

    ctx, llm = make([tool_call("link", {"texts": ["王小明", "小明"], "reason": "x"}), boom])
    out = link_names(ctx)
    assert out["finished"] is False and out["groups"] == 1 and out["reason"] == "模型端點回應異常"
    assert groups(ctx) == {"g1": ["王小明", "小明"]}
    assert any("同一人的判斷沒有完成" in w for w in ctx.warnings)


def test_running_out_of_steps_keeps_what_was_linked_and_warns(make, settings):
    settings.link_max_steps = 2
    ctx, llm = make([tool_call("link", {"texts": ["王小明", "小明"], "reason": "x"})] + [tool_call("list_names", {})] * 5)
    out = link_names(ctx)
    assert (out["finished"], out["reason"], out["groups"]) == (False, "超過步數上限", 1)
    assert len(asked(llm)) == 2 and any("同一人的判斷沒有完成（超過步數上限）" in w for w in ctx.warnings)


def test_the_model_is_told_when_few_steps_are_left(make, settings):
    """A sheet of fourteen different people has nothing to link; without being told, the model looked until
    the steps ran out and the reviewer got a notice for a judgement that had nothing left to decide."""
    settings.link_max_steps = 8
    ctx, llm = make([tool_call("search", {"query": "小明"})] * 3 + [tool_call("finish", {})])
    out = link_names(ctx)

    def told(call):  # what the program said after the opening line
        return [m["content"] for m in call["messages"] if m["role"] == "user"][1:]

    requests = asked(llm)
    assert told(requests[2]) == []  # six steps left: nothing said yet
    assert len(told(requests[3])) == 1
    assert "還剩 5 步" in told(requests[3])[0] and "link" in told(requests[3])[0] and "finish" in told(requests[3])[0]
    assert out["finished"] is True and ctx.warnings == []


def test_looking_does_not_count_as_checking_and_has_its_own_search_limit(make):
    ocr = FakeOcr([ocr_line("登入者：小明")])
    ctx, llm = make([], ocr=ocr)  # only to learn the ids
    image = ctx.inv.images[0].id
    unit = ctx.text_ids()[0]
    script = [tool_call("search", {"query": "小明"}), tool_call("read_text", {"unit_ids": [unit, image, "nope"]}), tool_call("finish", {})]
    ctx, llm = make(script, ocr=ocr)
    ctx.covered.clear()
    ctx.tool_counts["search"] = 41  # the detection agent used up its own searches
    calls_before = ocr.calls
    link_names(ctx)
    found, read = answer(asked(llm)[1]), answer(asked(llm)[2])
    assert found["total"] >= 2 and "error" not in found
    assert read["texts"] == {unit: "王小明到場。", image: ""} and read["unknown_ids"] == ["nope"]
    assert ctx.covered == set() and ocr.calls == calls_before and ctx.tool_counts["search"] == 41


def test_searching_in_circles_is_stopped(make):
    ctx, llm = make([tool_call("search", {"query": "小明"})] * 40)
    ctx.settings.link_max_steps = 40
    link_names(ctx)
    results = [answer(c) for c in asked(llm)[1:]]
    assert "error" not in results[29] and "搜尋次數已達上限" in results[30]["error"]


def test_what_the_step_did_is_in_the_log(make):
    ctx, llm = make([tool_call("link", {"texts": ["王小明", "小明"], "reason": "同一段"}), tool_call("finish", {})])
    link_names(ctx)
    assert [entry["tool"] for entry in ctx.log] == ["names.link", "names.finish"]
