"""Which names on the list are one person. Asked once, after detection, for the code function only: names of
one person then share a number in the coded text ([姓名1], [姓名1b]), so whoever reads it sees one person.

The model proposes and the program checks: a link is accepted only between strings that are active name
findings, word for word. Nothing here changes what is masked. If the model cannot finish, the links already
accepted stay and every other name keeps a code of its own - what the coded text did before this step existed."""
import json

from deid.agent import tools
from deid.agent.context import JobContext
from deid.agent.runner import run_tools
from deid.findings import NAME_TYPE
from deid.llm import LLMError

SEARCH_LIMIT = 30  # this step's own count: the detection agent's searches are not held against it
FEW_LEFT = 5  # steps before the limit at which the model is told to wrap up
WRAP_UP = f"還剩 {FEW_LEFT} 步。請收尾：把已經確定是同一個人的寫法用 link 連起來，然後呼叫 finish；沒有可以連的就直接呼叫 finish。"

SYSTEM = """你是去識別化系統的助手。這份文件裡的姓名之後會換成代號。你的工作只有一件：判斷清單上哪些姓名寫法指的是同一個人，用 link 連起來，讓同一個人使用同一個號碼。

做法：
1. list_names 看清單上的姓名。
2. 用 search、read_text 看每個寫法出現的上下文。
3. 確定是同一個人的寫法，用 link 連起來，並說明依據。
4. 做完呼叫 finish。沒有可以連的也直接呼叫 finish。

判斷原則：
- 要有文件裡的依據才連，例如：全名和只寫名字（王小明、小明）；中文名和在同一處對照的英文名；姓氏加稱謂，而且文件裡只有一位這個姓的人。
- 不確定就不要連。文件裡有兩位同姓的人時，「王先生」不連到任何一位。
- 只是同姓、同名不同姓、或看起來是親屬的，是不同的人，不要連。
- link 的 texts 必須是 list_names 列出的寫法，一字不差。一個寫法只能屬於一組。

每次回覆都必須呼叫工具，不要只用文字回答。"""

SCHEMAS = [
    tools._fn("list_names", "列出清單上的姓名寫法、各出現幾次，以及已經連起來的組。"),
    tools._fn("search", "在全文、圖片文字、檔案屬性裡搜尋字串（或正規表示式），回傳所在單元與前後文。",
              {"query": tools._STR, "regex": {"type": "boolean"}}, ["query"]),
    tools._fn("read_text", "讀取文字單元的原文，或圖片的文字（給圖片 id）。一次回傳有長度上限，沒回傳的會列在 not_returned。",
              {"unit_ids": tools._IDS}, ["unit_ids"]),
    tools._fn("link", "提出這幾個寫法是同一個人。texts 必須是 list_names 列出的寫法，一字不差，至少兩個。",
              {"texts": tools._IDS, "reason": tools._STR}, ["texts", "reason"]),
    tools._fn("finish", "判斷完成後呼叫。"),
]


def _names(ctx: JobContext) -> list:
    return [f for f in ctx.store.findings if f.kind == "text" and f.type == NAME_TYPE and f.on]


def _list_names(ctx: JobContext) -> dict:
    return {"names": [{"text": f.text, "count": len(f.occurrences)} for f in _names(ctx)],
            "groups": [[f.text for f in members] for members in ctx.store.groups().values()]}


def _read(ctx: JobContext, args: dict) -> dict:
    """The text of units, and what was already read from a picture. Looking is not checking: nothing is marked
    as checked and no picture is read here."""
    texts, unknown, skipped, used = {}, [], [], 0
    for uid in tools._ids(args.get("unit_ids")) or []:
        unit, image = ctx.inv.text(uid), ctx.inv.image(uid)
        if unit is None and image is None:
            unknown.append(uid)
            continue
        body = unit.text if unit is not None else "\n".join(ln.text for ln in image.ocr)
        cost = len(json.dumps({uid: body}, ensure_ascii=False)) + 1
        if texts and used + cost > tools.READ_BUDGET:
            skipped.append(uid)
            continue
        texts[uid] = body[:tools.READ_BUDGET]
        used += min(cost, tools.READ_BUDGET)
    out: dict = {"texts": texts}
    if unknown:
        out["unknown_ids"] = unknown[:20]
    if skipped:
        out["not_returned"] = skipped[:tools.READ_LIST_CAP]
    return out


def _link(ctx: JobContext, args: dict) -> dict:
    wanted = tools._ids(args.get("texts")) or []
    by_text = {f.text: f for f in _names(ctx)}
    for text in wanted:
        if text not in by_text:
            return {"ok": False, "error": f"清單上沒有這個姓名：{text}（要和 list_names 列出的寫法一字不差）"}
    members = list(dict.fromkeys(wanted))
    if len(members) < 2:
        return {"ok": False, "error": "一組至少要兩個寫法"}
    have = list(dict.fromkeys(by_text[t].group for t in members if by_text[t].group))
    if len(have) > 1:
        other = next(t for t in members if by_text[t].group == have[1])
        return {"ok": False, "error": f"{other} 已經屬於另一組；一個寫法只能屬於一組"}
    group = ctx.store.link([by_text[t].id for t in members])
    return {"ok": True, "group": [f.text for f in ctx.store.groups()[group]]}


def link_names(ctx: JobContext) -> dict:
    """Ask the model which active names are one person and record the groups it could show. Returns
    {"candidates", "groups", "finished", "reason"}; never raises for a model that fails."""
    out = {"candidates": len(_names(ctx)), "groups": 0, "finished": True, "reason": None}
    if out["candidates"] < 2:
        return out
    searches = 0

    def call(name: str, args: dict):
        nonlocal searches
        if name == "list_names":
            return _list_names(ctx)
        if name == "search":
            searches += 1
            if searches > SEARCH_LIMIT:
                return {"error": "搜尋次數已達上限。請依現有資訊判斷，做完呼叫 finish。"}
            return tools.find(ctx, str(args.get("query", "")), bool(args.get("regex")))
        if name == "read_text":
            return _read(ctx, args)
        if name == "link":
            return _link(ctx, args)
        if name == "finish":
            return {"ok": True}
        return {"error": f"沒有這個工具：{name}"}

    try:
        run = run_tools(ctx.llm, system=SYSTEM, opening="請開始判斷。", schemas=SCHEMAS, call=call,
                        max_steps=ctx.settings.link_max_steps,
                        note=lambda step, limit: WRAP_UP if limit - step == FEW_LEFT else None,
                        progress=lambda step, tool: ctx.progress(f"判斷同一人（第 {step} 步）", 0.96),
                        record=lambda step, name, args, result, started: ctx.record(step, f"names.{name}", args, result, started))
        finished, reason = run.finished, run.reason
    except LLMError:
        finished, reason = False, "模型端點回應異常"
    out.update(groups=len(ctx.store.groups()), finished=finished, reason=reason)
    if not finished:
        ctx.warn(f"同一人的判斷沒有完成（{reason}），沒有連起來的姓名各自使用一個代號")
    return out
