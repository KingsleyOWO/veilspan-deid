"""Fixes from the review of 2026-11-23, in the detection agent and the standard detection.

1. Text the agent asked for counts as checked only once the model has answered after getting it.
2. A string that detection or a rule also found is not the agent's alone to withdraw.
3. A stretch of an overlong line that no detection pass could read is reported, however the passes cut it."""
import json

from deid.agent import tools
from deid.detect import detect_lines
from deid.llm import LLMError
from deid.pipeline import analyze, build_context
from tests.builders import make_docx, text_image
from tests.fakes import FakeFaces, FakeLLM, FakeOcr, ocr_line, reply

TABLE = {"王小明": "姓名", "陳美玲": "姓名"}
PARAGRAPHS = ["承辦人 | 王小明 | 分機 2301", "會議由陳美玲主持。", "一般內容。"]
READ_ALL = ("read_text", {"unit_ids": ["t1", "t2", "t3"]})


def calls(*pairs):
    """One assistant turn with several tool calls."""
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}}
        for i, (n, a) in enumerate(pairs)]}


def detector(messages):
    content = messages[-1]["content"]
    if not isinstance(content, str):
        return {"role": "assistant", "content": "{}"}
    return reply([{"text": k, "type": v} for k, v in TABLE.items() if k in content])


def scripted(script):
    """Agent turns come from `script`, then the model only talks; detection requests are answered from TABLE."""
    script = list(script)

    def handler(messages, tool_schemas):
        if tool_schemas is None:
            return detector(messages)
        if not script:
            return {"role": "assistant", "content": "我不知道接下來要做什麼"}
        return script.pop(0)

    return FakeLLM(handler)


def make_ctx(tmp_path, settings, llm, *, ocr=None, paragraphs=PARAGRAPHS):
    doc = make_docx(tmp_path / "a.docx", paragraphs, images=[("image1.png", text_image(["x"], size=(400, 200)))])
    return build_context(doc, tmp_path / "work", settings, llm=llm, ocr=ocr or FakeOcr([]), faces=FakeFaces())


def loaded(tmp_path, settings, llm, **kw):
    ctx = make_ctx(tmp_path, settings, llm, **kw)
    ctx.load()
    ctx.write_previews()
    ctx.add_safety_net()
    return ctx


def active(ctx):
    return {f.text for f in ctx.store.findings if f.kind == "text" and f.on}


def text_detections(llm):
    return [c for c in llm.calls if c["tools"] is None and isinstance(c["messages"][-1]["content"], str)]


def tool_results(call):
    return [json.loads(m["content"]) for m in call["messages"] if m["role"] == "tool"]


# ---- 1. text the model has not seen is not checked ---------------------------------------------------------

def test_reading_text_does_not_count_until_the_model_has_answered(tmp_path, settings):
    ctx = loaded(tmp_path, settings, scripted([]))
    tools.call(ctx, "read_text", {"unit_ids": ["t1"]})
    assert "t1" not in ctx.covered and "t1" in ctx.unchecked_units()


def test_text_asked_for_in_the_same_reply_as_finish_goes_to_the_standard_flow(tmp_path, settings):
    llm = scripted([calls(READ_ALL, ("scan_images", {}), ("finish", {"summary": "完成"}))])
    ctx = make_ctx(tmp_path, settings, llm)
    analyze(ctx)
    assert ctx.outcome["finished"] is False and "已檢查過" not in ctx.outcome["fallback_reason"]
    assert {"王小明", "陳美玲"} <= active(ctx) and text_detections(llm)
    assert any("標準流程" in w for w in ctx.warnings)


def test_finish_in_the_same_reply_as_a_read_says_the_text_was_not_seen_yet(tmp_path, settings):
    llm = scripted([calls(READ_ALL, ("scan_images", {}), ("finish", {"summary": "完成"})),
                    calls(("finish", {"summary": "看過了"}))])
    ctx = make_ctx(tmp_path, settings, llm)
    analyze(ctx)
    refused = tool_results([c for c in llm.calls if c["tools"] is not None][1])[-1]
    assert refused["ok"] is False and "t1" in refused["error"] and "還沒看過" in refused["error"]
    # the next reply came after the text: now it counts, and finish goes through
    assert ctx.outcome["finished"] is True and not text_detections(llm)


def test_a_read_on_the_last_step_goes_to_the_standard_flow(tmp_path, settings):
    settings.agent_max_steps = 2
    llm = scripted([calls(("scan_images", {})), calls(READ_ALL)])
    ctx = make_ctx(tmp_path, settings, llm)
    analyze(ctx)
    assert "已檢查過" not in ctx.outcome["fallback_reason"]
    assert {"王小明", "陳美玲"} <= active(ctx) and any("標準流程" in w for w in ctx.warnings)


def test_a_read_the_endpoint_never_answered_goes_to_the_standard_flow(tmp_path, settings):
    turns = []

    def handler(messages, tool_schemas):
        if tool_schemas is None:
            return detector(messages)
        turns.append(1)
        if len(turns) == 1:
            return calls(("scan_images", {}), READ_ALL)
        raise LLMError("endpoint unavailable (ConnectError)")

    ctx = make_ctx(tmp_path, settings, FakeLLM(handler))
    analyze(ctx)
    assert "已檢查過" not in ctx.outcome["fallback_reason"]
    assert {"王小明", "陳美玲"} <= active(ctx) and any("標準流程" in w for w in ctx.warnings)


def test_a_read_answered_with_talk_instead_of_a_tool_goes_to_the_standard_flow(tmp_path, settings):
    llm = scripted([calls(("scan_images", {}), READ_ALL)])  # then two replies without a tool call
    ctx = make_ctx(tmp_path, settings, llm)
    analyze(ctx)
    assert "已檢查過" not in ctx.outcome["fallback_reason"]
    assert {"王小明", "陳美玲"} <= active(ctx) and any("標準流程" in w for w in ctx.warnings)


def test_text_the_model_read_and_then_acted_on_counts_as_checked(tmp_path, settings):
    llm = scripted([calls(("scan_images", {}), READ_ALL),
                    calls(("add_findings", {"items": [{"text": "王小明", "type": "姓名", "reason": "承辦人"}]})),
                    calls(("finish", {"summary": "完成"}))])
    ctx = make_ctx(tmp_path, settings, llm)
    analyze(ctx)
    assert ctx.outcome["finished"] is True and ctx.unchecked_units() == []
    assert not text_detections(llm) and not any("標準流程" in w for w in ctx.warnings)


# ---- 2. what detection or a rule also found is not the agent's to withdraw ---------------------------------

def test_a_name_detection_also_found_cannot_be_withdrawn_by_the_agent(tmp_path, settings):
    ctx = loaded(tmp_path, settings, scripted([]))
    added = tools.call(ctx, "add_findings", {"items": [{"text": "王小明", "type": "姓名", "reason": "承辦人"}]})["results"][0]
    assert "王小明" in [x["text"] for x in tools.call(ctx, "scan_text", {})["found"]]
    res = tools.call(ctx, "remove_finding", {"id": added["id"], "reason": "文件說這是公司名稱"})
    f = ctx.store.get(added["id"])
    assert res["switched_off"] is False and f.on and f.reason.startswith("代理存疑")


def test_detection_brings_back_a_name_the_agent_added_and_then_withdrew(tmp_path, settings):
    ctx = loaded(tmp_path, settings, scripted([]))
    added = tools.call(ctx, "add_findings", {"items": [{"text": "王小明", "type": "姓名", "reason": "承辦人"}]})["results"][0]
    assert tools.call(ctx, "remove_finding", {"id": added["id"], "reason": "文件說這是公司名稱"})["switched_off"] is True
    tools.call(ctx, "scan_text", {})
    f = ctx.store.get(added["id"])
    assert f.on and all(o.on for o in f.occurrences)
    assert f.source == "model" and f.reason == "代理存疑：文件說這是公司名稱"


def test_a_mailbox_the_agent_withdrew_is_still_masked_by_rule(tmp_path, settings):
    # The agent reads a picture's text before the picture is scanned, lists the mailbox in it and withdraws it
    # again. The rule that masks every mailbox runs with the scan, after that.
    ctx = loaded(tmp_path, settings, scripted([]), ocr=FakeOcr([ocr_line("聯絡 someone@example.com")]))
    tools.call(ctx, "read_text", {"unit_ids": ["img1"]})
    added = tools.call(ctx, "add_findings", {"items": [{"text": "someone@example.com", "type": "電子郵件", "reason": "信箱"}]})
    fid = added["results"][0]["id"]
    assert tools.call(ctx, "remove_finding", {"id": fid, "reason": "公司信箱"})["switched_off"] is True
    tools.call(ctx, "scan_images", {})
    f = ctx.store.get(fid)
    assert f.on and all(o.on for o in f.occurrences) and f.source == "rule"


def test_the_agent_still_withdraws_what_only_it_found(tmp_path, settings):
    ctx = loaded(tmp_path, settings, scripted([]))
    added = tools.call(ctx, "add_findings", {"items": [{"text": "一般內容", "type": "其他", "reason": "誤判"}]})["results"][0]
    tools.call(ctx, "scan_text", {})
    assert tools.call(ctx, "remove_finding", {"id": added["id"], "reason": "不是個資"})["switched_off"] is True
    assert not ctx.store.get(added["id"]).on


def test_a_suggestion_detection_leaves_off_does_not_take_the_agents_finding_over(tmp_path, settings):
    # Detection reports the extension too, but as a suggestion that is off: it did not ask for it to be masked.
    TABLE["2301"] = "電話"
    try:
        ctx = loaded(tmp_path, settings, scripted([]))
        added = tools.call(ctx, "add_findings", {"items": [{"text": "2301", "type": "電話", "reason": "分機"}]})["results"][0]
        tools.call(ctx, "scan_text", {})
        assert ctx.store.get(added["id"]).source == "agent"
        assert tools.call(ctx, "remove_finding", {"id": added["id"], "reason": "只是分機"})["switched_off"] is True
    finally:
        del TABLE["2301"]


# ---- 3. a stretch no pass could read ------------------------------------------------------------------------

MARK = "王小明"


def unreadable_with_mark(messages, tool_schemas):
    """The model's reply cannot be parsed for any piece that holds the whole name."""
    if MARK in messages[-1]["content"]:
        return {"role": "assistant", "content": "（回覆被截斷"}
    return reply([])


def test_a_stretch_of_a_long_line_that_no_pass_could_read_is_reported():
    line = "甲" * 3000 + MARK + "乙" * 1997  # the failing pieces of the two passes overlap, neither holds the other
    res = detect_lines(FakeLLM(unreadable_with_mark), [line], chunk_chars=2500, recheck_chars=1200, workers=1)
    assert res.failed_lines and all(p in line for p in res.failed_lines)
    assert any(MARK in p for p in res.failed_lines)


def test_a_name_on_a_cut_that_only_pieces_holding_part_of_it_were_read_for_is_reported():
    # Cut at 100 and at 60 characters, one pass ends a piece at 160 and the other starts one there. The pieces
    # holding the whole name fail; the pieces that end or start inside it are read, and saw half a name each.
    line = "甲" * 158 + MARK + "乙" * 139
    res = detect_lines(FakeLLM(unreadable_with_mark), [line], chunk_chars=100, recheck_chars=60, workers=1)
    assert any(MARK in p for p in res.failed_lines) and all(p in line for p in res.failed_lines)


def test_a_long_line_one_pass_read_in_full_is_not_reported():
    line = "甲" * 3000 + MARK + "乙" * 1997
    seen = []

    def handler(messages, tool_schemas):
        text = messages[-1]["content"]
        seen.append(text)
        if len(text) > 1200 and MARK in text:  # only the first pass's piece fails
            return {"role": "assistant", "content": "（回覆被截斷"}
        return reply([{"text": MARK, "type": "姓名"}] if MARK in text else [])

    res = detect_lines(FakeLLM(handler), [line], chunk_chars=2500, recheck_chars=1200, workers=1)
    assert res.failed_lines == [] and res.items == [(MARK, "姓名")]


# ---- 4. a report of something the program cannot mask is checked against what it does mask ------------------

def test_a_report_about_a_string_the_program_already_masks_is_questioned_once(tmp_path, settings):
    # The model's own script reads a number as it is stored (912345678); the program reads and masks the cell as it
    # is shown (0912345678). Told so, the model need not report; if it means another place, it says so again.
    ctx = loaded(tmp_path, settings, scripted([]), paragraphs=["陳美玲 | 0912345678", "一般內容。"])
    assert "0912345678" in active(ctx)  # by rule
    report = {"where": "工作表「名單」儲存格 B3", "text": "912345678", "reason": "以數字儲存"}
    first = tools.call(ctx, "report_unmaskable", report)
    assert first["ok"] is False and "已經在遮蔽範圍" in first["error"] and "t1" in first["error"]
    assert ctx.unmaskable == [] and not any("無法自動遮蔽" in w for w in ctx.warnings)
    again = tools.call(ctx, "report_unmaskable", dict(report, where="內嵌的 OLE 物件 oleObject1.bin"))
    # listed once, in unmaskable: the review screen shows it from there (a warning as well showed it twice)
    assert again == {"ok": True} and len(ctx.unmaskable) == 1 and not any("無法自動遮蔽" in w for w in ctx.warnings)


def test_a_report_about_something_the_program_does_not_read_or_mask_is_taken_at_once(tmp_path, settings):
    ctx = loaded(tmp_path, settings, scripted([]), paragraphs=["陳美玲 | 0912345678", "承辦人 王小明"])
    assert tools.call(ctx, "report_unmaskable", {"where": "巨集", "text": "林志豪", "reason": "寫在巨集裡"}) == {"ok": True}
    # read by the program but not masked: the report stands (it may be about another copy)
    assert tools.call(ctx, "report_unmaskable", {"where": "某處", "text": "王小明", "reason": "x"}) == {"ok": True}
    assert tools.call(ctx, "report_unmaskable", {"where": "圖片", "text": "", "reason": "看不清楚"}) == {"ok": True}
    assert len(ctx.unmaskable) == 3
