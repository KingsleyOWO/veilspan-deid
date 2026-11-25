import json

import pytest

from deid.agent import tools
from deid.agent.loop import run_agent
from deid.llm import LLMError
from deid.pipeline import analyze, build_context
from tests.builders import make_docx, text_image
from tests.fakes import FakeFaces, FakeLLM, FakeOcr, ocr_line, reply, tool_call

TABLE = {"王小明": "姓名", "陳美玲": "姓名"}


def scripted(script, table=TABLE):
    """Agent turns come from `script`; detection requests (no tools) are answered from `table`."""
    script = list(script)

    def handler(messages, tool_schemas):
        if tool_schemas is None:
            content = messages[-1]["content"]
            if not isinstance(content, str):
                return {"role": "assistant", "content": "{}"}
            return reply([{"text": k, "type": v} for k, v in table.items() if k in content])
        if not script:
            return {"role": "assistant", "content": "我不知道接下來要做什麼"}
        step = script.pop(0)
        return step(messages) if callable(step) else step

    return FakeLLM(handler)


@pytest.fixture
def ctx_factory(tmp_path, settings):
    def make(llm, **kw):
        doc = make_docx(tmp_path / "a.docx", ["承辦人 | 王小明 | 分機 2301", "王先生表示系統 A123-4567 型已上線。", "小明另提供資料。"],
                        images=[("image1.png", text_image(["x"], size=(400, 200)))], creator="林志豪")
        ctx = build_context(doc, tmp_path / "work", settings, llm=llm,
                            ocr=kw.get("ocr", FakeOcr([ocr_line("登入者：王小明")])), faces=FakeFaces())
        ctx.load()
        ctx.write_previews()
        ctx.add_safety_net()
        return ctx
    return make


def agent_calls(llm):
    return [c for c in llm.calls if c["tools"] is not None]


def last_tool_result(call):
    return json.loads(next(m for m in reversed(call["messages"]) if m["role"] == "tool")["content"])


def test_finish_is_refused_until_every_unit_is_checked(ctx_factory):
    llm = scripted([tool_call("get_inventory", {}), tool_call("scan_text", {}), tool_call("finish", {"summary": "完成"}),
                    tool_call("scan_images", {}), tool_call("finish", {"summary": "真的完成"})])
    ctx = ctx_factory(llm)
    outcome = run_agent(ctx)
    assert (outcome.finished, outcome.steps, outcome.fallback_reason) == (True, 5, None)
    refused = last_tool_result(agent_calls(llm)[3])
    assert refused["ok"] is False and "img1" in refused["error"]
    assert ctx.unchecked_units() == [] and not any("標準流程" in w for w in ctx.warnings)
    assert {f.text for f in ctx.store.findings if f.kind == "text"} >= {"王小明"}


def test_inventory_tells_the_agent_what_exists_and_what_is_unchecked(ctx_factory):
    ctx = ctx_factory(scripted([]))
    inv = tools.call(ctx, "get_inventory", {})
    assert inv["text_units"] == 3 and inv["images"][0]["id"] == "img1" and inv["unchecked"] == ["t1", "t2", "t3", "img1"]
    assert {"label": "作者", "value": "林志豪"} in [{"label": m["label"], "value": m["value"]} for m in inv["metadata"]]


def test_added_findings_must_exist_verbatim(ctx_factory):
    ctx = ctx_factory(scripted([]))
    res = tools.call(ctx, "add_findings", {"items": [{"text": "王先生", "type": "姓名", "reason": "姓氏加稱謂"},
                                                    {"text": "王大明", "type": "姓名", "reason": "不存在"},
                                                    {"text": "小", "type": "姓名", "reason": "太短"}]})
    assert [r["accepted"] for r in res["results"]] == [True, False, False]
    assert "找不到" in res["results"][1]["error"]
    [f] = [f for f in ctx.store.findings if f.kind == "text"]
    assert (f.text, f.source, f.reason) == ("王先生", "agent", "姓氏加稱謂")


def test_agent_can_only_doubt_findings_it_did_not_add_itself(ctx_factory):
    """On a live run the agent switched off a correct "王先生". Detections stay on; the human decides."""
    ctx = ctx_factory(scripted([]))
    f = ctx.store.add_text("2301", "其他識別碼", "model")
    res = tools.call(ctx, "remove_finding", {"id": f.id, "reason": "這是分機"})
    assert res["ok"] and res["switched_off"] is False
    assert f.on is True and f.reason == "代理存疑：這是分機"
    assert "error" in tools.call(ctx, "remove_finding", {"id": "f999", "reason": "x"})


def test_agent_can_switch_off_a_finding_it_added(ctx_factory):
    ctx = ctx_factory(scripted([]))
    tools.call(ctx, "add_findings", {"items": [{"text": "王先生", "type": "姓名", "reason": "稱謂"}]})
    f = next(f for f in ctx.store.findings if f.text == "王先生")
    res = tools.call(ctx, "remove_finding", {"id": f.id, "reason": "加錯了"})
    assert res["switched_off"] is True and f in ctx.store.findings and f.on is False and "加錯了" in f.reason


def test_search_and_pattern_candidates_give_context(ctx_factory):
    ctx = ctx_factory(scripted([]))
    hits = tools.call(ctx, "search", {"query": "小明"})["hits"]
    assert {h["unit"] for h in hits} == {"t1", "t3"} and all("小明" in h["context"] for h in hits)
    assert tools.call(ctx, "search", {"query": r"A\d{3}-\d{4}", "regex": True})["hits"][0]["unit"] == "t2"
    assert "error" in tools.call(ctx, "search", {"query": "(", "regex": True})
    assert tools.call(ctx, "pattern_candidates", {})["candidates"] == []


def test_text_that_was_read_waits_for_the_models_answer_and_image_text_never_counts(ctx_factory):
    ctx = ctx_factory(scripted([]))
    res = tools.call(ctx, "read_text", {"unit_ids": ["t1", "img1", "nope"]})
    assert res["texts"]["t1"] == "承辦人 | 王小明 | 分機 2301" and res["texts"]["img1"] == "登入者：王小明"
    assert res["unknown_ids"] == ["nope"]
    # Not checked yet: the loop counts a text once the model has answered after getting it (test_fix_core.py).
    assert ctx.pending_reads == {"t1"} and not ctx.covered & {"t1", "img1"}


def test_viewing_an_image_attaches_it_to_the_next_request(ctx_factory):
    # Looking at an image is not checking it (review finding I6): scan_images must still run.
    llm = scripted([tool_call("view_image", {"image_id": "img1"}), tool_call("scan_text", {}), tool_call("scan_images", {}),
                    tool_call("finish", {"summary": "ok"})])
    ctx = ctx_factory(llm)
    assert run_agent(ctx).finished
    second = agent_calls(llm)[1]["messages"]
    assert second[-1]["role"] == "user"
    parts = second[-1]["content"]
    assert any(p["type"] == "image_url" and p["image_url"]["url"].startswith("data:image/jpeg;base64,") for p in parts)
    assert second[-2]["role"] == "tool"


def test_whole_image_mask_and_unmaskable_report(ctx_factory):
    ctx = ctx_factory(scripted([]))
    res = tools.call(ctx, "add_image_mask", {"image_id": "img1", "reason": "身分證影本", "type": "證件"})
    f = ctx.store.get(res["id"])
    assert (f.kind, f.source, f.on, f.occurrences[0].box) == ("region", "agent", True, (0.0, 0.0, 400.0, 200.0))
    tools.call(ctx, "report_unmaskable", {"where": "內嵌的試算表", "text": "王小明", "reason": "程式改不到"})
    # listed once, in unmaskable: the review screen shows it from there (a warning as well showed it twice)
    assert ctx.unmaskable[0]["where"] == "內嵌的試算表" and not any("內嵌的試算表" in w for w in ctx.warnings)
    assert not any("王小明" in w for w in ctx.warnings)


def test_model_that_stops_calling_tools_triggers_the_standard_flow(ctx_factory):
    llm = scripted([tool_call("get_inventory", {})])
    ctx = ctx_factory(llm)
    outcome = run_agent(ctx)
    assert outcome.finished is False and "沒有呼叫工具" in outcome.fallback_reason
    assert ctx.unchecked_units() == []
    assert any("已改用標準流程" in w for w in ctx.warnings)
    assert "王小明" in {f.text for f in ctx.store.findings}
    nudge = agent_calls(llm)[2]["messages"][-1]
    assert nudge["role"] == "user" and "finish" in nudge["content"]


def test_step_limit_triggers_the_standard_flow(ctx_factory):
    ctx = ctx_factory(scripted([tool_call("get_inventory", {})] * 50))
    ctx.settings.agent_max_steps = 4
    outcome = run_agent(ctx)
    assert (outcome.finished, outcome.steps) == (False, 4) and "步數" in outcome.fallback_reason
    assert ctx.unchecked_units() == []


def test_repeated_malformed_arguments_trigger_the_standard_flow(ctx_factory):
    ctx = ctx_factory(scripted([tool_call("search", "{not json")] * 10))
    outcome = run_agent(ctx)
    assert outcome.finished is False and "參數" in outcome.fallback_reason and outcome.steps == 3
    assert ctx.unchecked_units() == []


def test_unknown_tool_is_reported_back_to_the_model(ctx_factory):
    llm = scripted([tool_call("delete_everything", {}), tool_call("scan_text", {}), tool_call("scan_images", {}),
                    tool_call("finish", {"summary": "ok"})])
    ctx = ctx_factory(llm)
    assert run_agent(ctx).finished
    assert "error" in last_tool_result(agent_calls(llm)[1])


def test_endpoint_failure_during_the_agent_falls_back_and_then_propagates(ctx_factory):
    def boom(messages, tool_schemas):
        raise LLMError("down")

    ctx = ctx_factory(FakeLLM(boom))
    with pytest.raises(LLMError):
        run_agent(ctx)


def test_endpoint_hiccup_in_the_agent_loop_alone_is_survived(ctx_factory):
    state = {"n": 0}

    def handler(messages, tool_schemas):
        if tool_schemas is None:
            content = messages[-1]["content"]
            return reply([{"text": "王小明", "type": "姓名"}] if isinstance(content, str) and "王小明" in content else [])
        state["n"] += 1
        raise LLMError("agent turn failed")

    ctx = ctx_factory(FakeLLM(handler))
    outcome = run_agent(ctx)
    assert outcome.finished is False and ctx.unchecked_units() == [] and state["n"] == 1


def test_every_tool_call_is_logged_with_bounded_fields(ctx_factory):
    llm = scripted([tool_call("search", {"query": "王" * 500}), tool_call("scan_text", {}), tool_call("scan_images", {}),
                    tool_call("finish", {"summary": "ok"})])
    ctx = ctx_factory(llm)
    run_agent(ctx)
    assert [e["tool"] for e in ctx.log] == ["search", "scan_text", "scan_images", "finish"]
    assert all(len(e["args"]) <= 200 and len(e["result"]) <= 300 and e["step"] >= 1 and "seconds" in e for e in ctx.log)


def test_long_tool_results_are_truncated_before_they_reach_the_model(ctx_factory, tmp_path, settings):
    doc = make_docx(tmp_path / "long.docx", ["字" * 3000 for _ in range(5)])
    llm = scripted([tool_call("read_text", {"unit_ids": ["t1", "t2", "t3", "t4", "t5"]}), tool_call("scan_text", {}),
                    tool_call("finish", {"summary": "ok"})])
    ctx = build_context(doc, tmp_path / "work2", settings, llm=llm, ocr=FakeOcr(), faces=FakeFaces())
    ctx.load()
    assert run_agent(ctx).finished
    result = next(m for m in agent_calls(llm)[1]["messages"] if m["role"] == "tool")["content"]
    assert len(result) <= 6200
    data = json.loads(result)
    assert data["not_returned"] and set(data["texts"]) | set(data["not_returned"]) == {"t1", "t2", "t3", "t4", "t5"}


def test_run_python_is_offered_only_when_the_sandbox_exists(ctx_factory, monkeypatch):
    from deid.agent import sandbox
    ctx = ctx_factory(scripted([]))
    monkeypatch.setattr(sandbox, "available", lambda s: False)
    assert "run_python" not in [t["function"]["name"] for t in tools.schemas(ctx)]
    assert "error" in tools.call(ctx, "run_python", {"code": "print(1)"})
    monkeypatch.setattr(sandbox, "available", lambda s: True)
    assert "run_python" in [t["function"]["name"] for t in tools.schemas(ctx)]


def test_pipeline_uses_the_agent_and_records_the_outcome(tmp_path, settings):
    doc = make_docx(tmp_path / "p.docx", ["承辦人 王小明"])
    llm = scripted([tool_call("scan_text", {}), tool_call("add_findings", {"items": [{"text": "承辦人", "type": "其他", "reason": "測試"}]}),
                    tool_call("finish", {"summary": "ok"})])
    ctx = build_context(doc, tmp_path / "work", settings, llm=llm, ocr=FakeOcr(), faces=FakeFaces())
    analyze(ctx)
    assert ctx.outcome == {"agent": True, "finished": True, "steps": 3, "fallback_reason": None}
    assert {f.source for f in ctx.store.findings if f.kind == "text"} == {"model", "agent"}
    assert json.loads((tmp_path / "work" / "log.json").read_text(encoding="utf-8"))[0]["tool"] == "scan_text"


def test_agent_is_told_once_when_everything_has_been_checked(ctx_factory):
    llm = scripted([tool_call("scan_text", {}), tool_call("scan_images", {}), tool_call("list_findings", {}),
                    tool_call("finish", {"summary": "ok"})])
    ctx = ctx_factory(llm)
    assert run_agent(ctx).finished
    calls = agent_calls(llm)
    reminders = [[m for m in c["messages"] if m["role"] == "user" and isinstance(m["content"], str) and "都已檢查" in m["content"]]
                 for c in calls]
    assert [len(r) for r in reminders] == [0, 0, 1, 1]
    assert "finish" in reminders[2][0]["content"]


def test_search_is_capped_so_the_agent_cannot_spiral(ctx_factory):
    ctx = ctx_factory(scripted([]))
    results = [tools.call(ctx, "search", {"query": "王"}) for _ in range(tools.SEARCH_LIMIT + 2)]
    assert all("hits" in r for r in results[:tools.SEARCH_LIMIT])
    assert "error" in results[-1] and "finish" in results[-1]["error"]


def test_agent_is_warned_when_few_steps_remain(ctx_factory):
    ctx = ctx_factory(scripted([tool_call("get_inventory", {})] * 50))
    ctx.settings.agent_max_steps = 8
    run_agent(ctx)
    last = agent_calls(ctx.llm)[-1]["messages"]
    assert any(m["role"] == "user" and isinstance(m["content"], str) and "還剩" in m["content"] for m in last)


def test_after_full_coverage_the_agent_gets_a_few_more_steps_and_no_alarm_is_raised(ctx_factory):
    llm = scripted([tool_call("scan_text", {}), tool_call("scan_images", {})] + [tool_call("list_findings", {})] * 50)
    ctx = ctx_factory(llm)
    ctx.settings.agent_extra_steps = 3
    outcome = run_agent(ctx)
    assert outcome.finished is False and outcome.steps == 5 and "都已檢查" in outcome.fallback_reason
    assert ctx.warnings == [w for w in ctx.warnings if "代理" not in w]
    told = [m["content"] for m in agent_calls(llm)[2]["messages"] if m["role"] == "user" and isinstance(m["content"], str)]
    assert any("3 步" in t for t in told)


def test_pattern_candidates_offer_other_ways_a_found_person_is_written(ctx_factory):
    """On a live run the agent found 「小明」 but not 「王先生」; the harness now hands it both to judge."""
    ctx = ctx_factory(scripted([]))
    ctx.store.add_text("王小明", "姓名", "model")
    got = {c["text"]: c for c in tools.call(ctx, "pattern_candidates", {})["candidates"]}
    assert got["王先生"]["kind"] == "姓名的其他寫法" and got["王先生"]["related_to"] == "王小明" and "王先生表示" in got["王先生"]["context"]
    assert got["小明"]["related_to"] == "王小明" and got["小明"]["unit"] == "t3"
    ctx.store.add_text("王先生", "姓名", "agent")
    again = {c["text"]: c for c in tools.call(ctx, "pattern_candidates", {})["candidates"]}
    assert again["王先生"]["already_found"] is True


def test_name_variants_are_not_invented_for_organisations_or_long_strings(ctx_factory):
    ctx = ctx_factory(scripted([]))
    ctx.store.add_text("承辦人", "其他", "user")
    assert tools.call(ctx, "pattern_candidates", {})["candidates"] == []
