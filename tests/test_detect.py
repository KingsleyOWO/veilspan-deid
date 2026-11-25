import json

import httpx
import pytest

from deid.detect import SYSTEM_PROMPT, TYPES, detect_lines
from deid.llm import LLMClient, LLMError
from tests.fakes import FakeLLM, keyword_llm, reply


def user_text(messages):
    return messages[-1]["content"]


def test_system_prompt_is_the_validated_one():
    assert SYSTEM_PROMPT.startswith("你是去識別化系統的偵測元件。")
    assert "text 必須是原文中完全一致的連續字串" in SYSTEM_PROMPT
    assert "、".join(TYPES) in SYSTEM_PROMPT and TYPES[0] == "姓名" and TYPES[-1] == "其他"


def test_chunks_never_exceed_the_limit():
    lines = [f"第{i}行" + "字" * 96 for i in range(30)]
    llm = keyword_llm({})
    detect_lines(llm, lines, chunk_chars=500, workers=2)
    assert len(llm.calls) > 1
    assert all(len(user_text(c["messages"])) <= 500 for c in llm.calls)
    assert all(c["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT} for c in llm.calls)


def test_found_items_are_deduplicated_across_chunks():
    lines = ["承辦人 王小明" + "。" * 300, "王小明 電話 0912-345-678" + "。" * 300]
    res = detect_lines(keyword_llm({"王小明": "姓名", "0912-345-678": "電話"}), lines, chunk_chars=350)
    assert sorted(res.items) == [("0912-345-678", "電話"), ("王小明", "姓名")]
    assert (res.failed_chunks, res.not_in_source) == (0, 0)


def test_unparseable_reply_is_retried_on_each_half():
    lines = ["甲 王小明", "乙 陳美玲"]

    def handler(messages, tools):
        text = user_text(messages)
        if "\n" in text:
            return {"role": "assistant", "content": "抱歉，我無法輸出 JSON"}
        return reply([{"text": text.split()[1], "type": "姓名"}])

    res = detect_lines(FakeLLM(handler), lines)
    assert sorted(t for t, _ in res.items) == ["王小明", "陳美玲"]
    assert res.failed_chunks == 0


def test_single_line_that_keeps_failing_counts_as_one_failed_chunk():
    res = detect_lines(FakeLLM(lambda m, t: {"role": "assistant", "content": "???"}), ["只有一行 王小明"])
    assert (res.items, res.failed_chunks) == ([], 1)


def test_items_absent_from_source_are_dropped_and_counted():
    llm = FakeLLM(lambda m, t: reply([{"text": "王小明", "type": "姓名"}, {"text": "王大明", "type": "姓名"}]))
    res = detect_lines(llm, ["承辦人 王小明"])
    assert res.items == [("王小明", "姓名")] and res.not_in_source == 1


def test_single_character_results_and_unknown_types_are_normalised():
    llm = FakeLLM(lambda m, t: reply([{"text": "王", "type": "姓名"}, {"text": " 王小明 ", "type": "人名"}, "junk", {"text": 5}]))
    res = detect_lines(llm, ["承辦人 王小明"])
    assert res.items == [("王小明", "其他")]


def test_json_wrapped_in_prose_or_fences_is_still_parsed():
    llm = FakeLLM(lambda m, t: {"role": "assistant", "content": '```json\n[{"text":"王小明","type":"姓名"}]\n```'})
    assert detect_lines(llm, ["承辦人 王小明"]).items == [("王小明", "姓名")]


def test_endpoint_failure_propagates_instead_of_counting_as_a_failed_chunk():
    def boom(messages, tools):
        raise LLMError("down")
    with pytest.raises(LLMError):
        detect_lines(FakeLLM(boom), ["承辦人 王小明"])


def test_overlong_line_is_split_but_still_matched_against_the_whole_line():
    line = "字" * 700 + "王小明" + "字" * 700
    res = detect_lines(keyword_llm({"王小明": "姓名"}), [line], chunk_chars=500)
    assert res.items == [("王小明", "姓名")]


# ---- LLMClient over a mock transport -------------------------------------------------

def client(settings, handler, **kw):
    return LLMClient(settings, transport=httpx.MockTransport(handler), sleep=lambda s: None, **kw)


def ok(content="hi"):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})


def test_client_sends_auth_model_and_thinking_off(settings):
    seen = {}

    def handler(request):
        seen["url"], seen["auth"], seen["body"] = str(request.url), request.headers["authorization"], json.loads(request.content)
        return ok()

    msg = client(settings, handler).chat([{"role": "user", "content": "x"}], max_tokens=123)
    assert msg["content"] == "hi"
    assert seen["url"] == "http://127.0.0.1:9/v1/chat/completions" and seen["auth"] == "Bearer test-key"
    body = seen["body"]
    assert (body["model"], body["temperature"], body["max_tokens"]) == ("test-model", 0, 123)
    assert body["chat_template_kwargs"] == {"enable_thinking": False} and "tools" not in body


def test_client_passes_tools(settings):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return ok()

    tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object", "properties": {}}}}]
    client(settings, handler).chat([{"role": "user", "content": "x"}], tools=tools)
    assert seen["body"]["tools"] == tools and seen["body"]["tool_choice"] == "auto"


def test_client_retries_server_errors_then_succeeds(settings):
    n = {"calls": 0}

    def handler(request):
        n["calls"] += 1
        return httpx.Response(503, text="busy") if n["calls"] < 3 else ok("done")

    assert client(settings, handler).chat([{"role": "user", "content": "x"}])["content"] == "done"
    assert n["calls"] == 3


def test_client_gives_up_after_three_attempts(settings):
    n = {"calls": 0}

    def handler(request):
        n["calls"] += 1
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMError):
        client(settings, handler).chat([{"role": "user", "content": "x"}])
    assert n["calls"] == 3


def test_client_does_not_retry_auth_errors_and_hides_the_body(settings):
    n = {"calls": 0}

    def handler(request):
        n["calls"] += 1
        return httpx.Response(401, text="secret detail")

    with pytest.raises(LLMError) as e:
        client(settings, handler).chat([{"role": "user", "content": "x"}])
    assert n["calls"] == 1 and "401" in str(e.value) and "secret detail" not in str(e.value)


def test_client_rejects_malformed_success_body(settings):
    with pytest.raises(LLMError):
        client(settings, lambda r: httpx.Response(200, json={"oops": 1})).chat([{"role": "user", "content": "x"}])


def lists_names_only_in_short_messages(limit):
    """A model that sees a name only when it is not given too much text at once."""
    def handler(messages, tools):
        text = user_text(messages)
        return reply([{"text": "王小明", "type": "姓名"}] if "王小明" in text and len(text) <= limit else [])
    return FakeLLM(handler)


LONG = ["開頭" + "字" * 300, "承辦人王小明負責本案", "結尾" + "字" * 300]


def test_a_second_pass_with_smaller_pieces_finds_what_the_first_overlooked():
    assert detect_lines(lists_names_only_in_short_messages(400), LONG, chunk_chars=1000, workers=2).items == []
    res = detect_lines(lists_names_only_in_short_messages(400), LONG, chunk_chars=1000, workers=2, recheck_chars=400)
    assert res.items == [("王小明", "姓名")]


def test_the_second_pass_is_skipped_when_it_would_cut_the_text_the_same_way():
    llm = keyword_llm({"王小明": "姓名"})
    res = detect_lines(llm, ["承辦人王小明", "一般內容"], chunk_chars=1000, workers=2, recheck_chars=400)
    assert len(llm.calls) == 1 and res.items == [("王小明", "姓名")]


def test_a_piece_counts_as_unread_only_when_no_pass_could_read_it():
    def broken_when_long(messages, tools):
        text = user_text(messages)
        return {"role": "assistant", "content": "抱歉，我無法處理"} if len(text) > 350 else reply([])
    lines = ["甲" * 500, "乙" * 300]  # the first line is too long for either pass to get an answer; the second is not
    res = detect_lines(FakeLLM(broken_when_long), lines, chunk_chars=1000, workers=2, recheck_chars=400)
    assert res.failed_lines and all("甲" in line for line in res.failed_lines)
