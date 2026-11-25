"""The tool loop the newer model steps share: it sends the conversation, runs the tools the model asks for,
and ends when the model finishes or cannot get there."""
import json

import pytest

from deid.agent import loop
from deid.agent.runner import RunOutcome, run_tools
from deid.llm import LLMError
from tests.fakes import FakeLLM, routed, tool_call

SCHEMAS = [{"type": "function", "function": {"name": "finish", "description": "", "parameters": {"type": "object", "properties": {}}}}]


def scripted(script):
    script = list(script)

    def handler(messages, tools):
        if not script:
            return {"role": "assistant", "content": "不知道"}
        step = script.pop(0)
        return step(messages) if callable(step) else step

    return FakeLLM(handler)


def run(llm, call, **kw):
    kw.setdefault("max_steps", 10)
    return run_tools(llm, system="系統", opening="開始", schemas=SCHEMAS, call=call, **kw)


def test_finish_ends_the_run():
    llm = scripted([tool_call("finish", {})])
    assert run(llm, lambda name, args: {"ok": True}) == RunOutcome(True, 1)
    assert llm.calls[0]["messages"][:2] == [{"role": "system", "content": "系統"}, {"role": "user", "content": "開始"}]
    assert llm.calls[0]["tools"] == SCHEMAS


def test_a_refused_finish_keeps_going():
    answers = [{"ok": False, "error": "還沒"}, {"ok": True}]
    out = run(scripted([tool_call("finish", {}), tool_call("finish", {})]), lambda name, args: answers.pop(0))
    assert out == RunOutcome(True, 2)


def test_two_turns_without_a_tool_end_the_run():
    llm = scripted([{"role": "assistant", "content": "嗯"}, {"role": "assistant", "content": "嗯"}])
    out = run(llm, lambda name, args: {})
    assert (out.finished, out.reason) == (False, "模型沒有呼叫工具")
    assert llm.calls[1]["messages"][-1] == {"role": "user", "content": loop.NUDGE}


def test_the_step_limit_ends_the_run():
    llm = scripted([tool_call("x", {})] * 5)
    assert run(llm, lambda name, args: {"seen": name}, max_steps=3) == RunOutcome(False, 3, "超過步數上限")
    assert len(llm.calls) == 3


def test_three_unreadable_argument_sets_end_the_run():
    called = []
    out = run(scripted([tool_call("x", "{")] * 5), lambda name, args: called.append(name))
    assert (out.finished, out.reason) == (False, "工具參數格式一再錯誤")
    assert called == []


def test_a_tool_that_raises_is_reported_and_the_run_goes_on():
    def call(name, args):
        if name == "x":
            raise ZeroDivisionError("secret detail")
        return {"ok": True}

    llm = scripted([tool_call("x", {}), tool_call("finish", {})])
    assert run(llm, call) == RunOutcome(True, 2)
    told = llm.calls[1]["messages"][-1]
    assert told["role"] == "tool" and "工具執行失敗（ZeroDivisionError）" in told["content"] and "secret" not in told["content"]


def test_an_endpoint_failure_reaches_the_caller():
    def boom(messages, tools):
        raise LLMError("down")

    with pytest.raises(LLMError):
        run(FakeLLM(boom), lambda name, args: {})


def test_long_results_are_cut_before_the_model_sees_them():
    llm = scripted([tool_call("x", {}), tool_call("finish", {})])
    run(llm, lambda name, args: {"ok": True, "x": "a" * 20000})
    assert len(llm.calls[1]["messages"][-1]["content"]) <= 6000


def test_a_note_follows_the_tool_results():
    llm = scripted([tool_call("x", {}), tool_call("finish", {})])
    run(llm, lambda name, args: {"ok": True}, note=lambda step, limit: f"第 {step} 步，共 {limit} 步")
    assert llm.calls[1]["messages"][-1] == {"role": "user", "content": "第 1 步，共 10 步"}


def test_every_tool_call_is_recorded_and_progress_is_reported():
    seen, stages = [], []
    llm = scripted([tool_call("x", {"a": 1}), tool_call("finish", {})])
    run(llm, lambda name, args: {"ok": True}, record=lambda step, name, args, result, started: seen.append((step, name, args, result)),
        progress=lambda step, name: stages.append((step, name)))
    assert seen == [(1, "x", {"a": 1}, {"ok": True}), (2, "finish", {}, {"ok": True})]
    assert (1, "x") in stages and (2, "finish") in stages


def test_the_setting_for_the_same_person_step_defaults_to_on(tmp_path):
    from deid.config import load_settings
    s = load_settings(tmp_path / "none.env", environ={})
    assert (s.link_names, s.link_max_steps) == (True, 30)
    assert load_settings(tmp_path / "none.env", environ={"LINK_NAMES": "0"}).link_names is False


def test_tests_run_with_the_same_person_step_off_unless_they_ask(settings):
    assert settings.link_names is False


def test_a_double_can_answer_by_which_step_is_asking():
    llm = routed({"同一人": lambda messages, tools: {"role": "assistant", "content": "同一人的回覆"}}, table={"王小明": "姓名"})
    assert llm.chat([{"role": "system", "content": "你是同一人代理"}, {"role": "user", "content": "x"}])["content"] == "同一人的回覆"
    detected = llm.chat([{"role": "system", "content": "偵測"}, {"role": "user", "content": "承辦人 王小明"}])
    assert json.loads(detected["content"]) == [{"text": "王小明", "type": "姓名"}]
