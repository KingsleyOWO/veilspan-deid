"""The agent loop. The model decides what to do; this loop guarantees the result is complete:
finish is refused while units are unchecked, and if the model cannot get there the standard flow does."""
import json
import time
from dataclasses import dataclass

from deid.agent import tools
from deid.agent.context import JobContext, JobStopped
from deid.llm import LLMError

RESULT_LIMIT = 6000

SYSTEM = """你是去識別化系統的檢查代理。目標：依台灣《個人資料保護法》找出這份文件中所有可識別特定自然人的個人資料，交給程式遮蔽。你只負責判斷，改檔由程式執行。

你自己決定檢查的步驟與使用的工具。常見做法：
1. get_inventory 看文件有哪些部分。
2. scan_text、scan_images 做第一輪偵測（不給參數就是全部）。
3. 再檢查第一輪容易漏的地方：
   - pattern_candidates 列出的候選：樣式比對的結果，以及已找到人名的其他寫法（姓氏加稱謂、只寫名字）。
     掃描完之後一定要呼叫一次，把其中指向自然人、還沒加入的用 add_findings 加入
   - 英文名、暱稱等其他寫法（用 search 找，用 add_findings 加入）
   - 檔案屬性裡的人名
   - 沒有文字、但可能是證件、簽名或手寫文件的圖片（用 view_image 看，必要時用 add_image_mask）
   - 第一輪明顯的誤判，例如把機關名稱、型號當成個資（用 list_findings 檢視，用 remove_finding 提出異議並說明理由）
4. get_inventory 的 unchecked_items 是固定工具讀不到的部分{script_hint}。在那些地方發現個資、而程式改不到時，用 report_unmaskable 回報。

判斷原則：
- 姓氏加稱謂（如「王先生」）、只寫名字（如「小明」）都是個資，要遮，不要提出異議。寧可多遮一筆人名，不要漏。
- 不要遮：機關、公司、學校、部門、品牌名稱，及其公開地址；職稱；法規、系統、產品名稱與型號、編號；文件日期。分機、學歷、年資預設不遮。
- 電話號碼與電子郵件信箱（包含機關的總機與部門信箱）由程式自動遮蔽，不需要加入，也不要對它們提出異議；廠商、公司的名稱仍然不遮。
- 網址本身，以及網址裡的文件編號、表單代碼、guid 不是個資，不要加入。
- 職稱本身（如「執行長」「經理」）即使緊鄰姓名也不要加入，只加姓名。
- 截圖或圖片裡有姓名、電話時，不要把整張圖遮掉；add_image_mask 只用在證件、簽名印章、手寫文件、病歷。
- 內文、表格、頁首頁尾、註解、檔案屬性、圖片裡的文字與人臉，程式都會依位置自動遮蔽；scan_images 找到的項目不需要再回報。
- add_findings 的 text 必須是原文中一字不差的連續字串，只取個資本身。

所有單元都檢查過才能呼叫 finish；還有沒檢查的，finish 會告訴你缺哪些。
不要重複做已經做過的檢查；確認完就呼叫 finish。
每次回覆都必須呼叫工具，不要只用文字回答。"""

NUDGE = "請呼叫工具繼續檢查，或在所有單元都檢查完後呼叫 finish 結束。"


@dataclass
class AgentOutcome:
    finished: bool
    steps: int
    fallback_reason: str | None = None


def _clip(result) -> str:
    text = json.dumps(result, ensure_ascii=False)
    if len(text) <= RESULT_LIMIT:
        return text
    return json.dumps({"truncated": True, "note": "結果太長已截斷，請縮小範圍再查", "partial": text[:RESULT_LIMIT - 200]},
                      ensure_ascii=False)


COMPLETE = "所有單元都已檢查過。你還有 {n} 步可以做補充（例如同一人的其他寫法），做完請呼叫 finish。"
FEW_LEFT = 5

# What a step does, as the processing screen says it to a colleague. A tool not listed here is shown without its
# name: the screen is no place for a function name.
STEP_PHRASES = {
    "get_inventory": "查看文件組成",
    "scan_text": "偵測文字中的個資",
    "scan_images": "檢查圖片",
    "read_text": "閱讀原文",
    "view_image": "查看圖片",
    "search": "搜尋全文",
    "pattern_candidates": "檢查候選個資",
    "list_findings": "檢視遮蔽清單",
    "add_findings": "新增遮蔽項目",
    "add_image_mask": "把整張圖列為遮蔽",
    "remove_finding": "對遮蔽項目提出異議",
    "report_unmaskable": "回報需人工處理的位置",
    "run_python": "用腳本檢查檔案",
    "finish": "結束檢查",
}


def _fallback(ctx: JobContext, steps: int, reason: str) -> AgentOutcome:
    if ctx.unchecked_units():
        ctx.warn(f"代理流程未完成（{reason}），已改用標準流程")
        ctx.cover_remaining()  # an endpoint failure here propagates: a half-checked document is not a result
        return AgentOutcome(False, steps, reason)
    # Nothing was left unchecked: the result is complete, the agent just did not say it was done.
    return AgentOutcome(False, steps, f"{reason}；所有部分都已檢查過")


def run_agent(ctx: JobContext, *, lang: str = "zh-TW") -> AgentOutcome:
    schema = tools.schemas(ctx)
    has_script = any(t["function"]["name"] == "run_python" for t in schema)
    system = SYSTEM.replace("{script_hint}", "，可以用 run_python 自己寫腳本檢查（腳本只能讀檔）" if has_script else "")
    if lang == "en":
        system += "\nWrite user-facing explanations (the reason and where fields) in English. Keep extracted text and type values in their required original form."
    messages = [{"role": "system", "content": system}, {"role": "user", "content": "請開始檢查這份文件。"}]
    limit = max(1, ctx.settings.agent_max_steps)
    silent = bad_args = 0
    told_complete = False
    step = 0
    while step < limit:
        step += 1
        ctx.progress(f"代理檢查中（第 {step} 步）", 0.1 + 0.8 * (step - 1) / limit)
        try:
            msg = ctx.llm.chat(messages, tools=schema, max_tokens=2000)
        except LLMError:
            return _fallback(ctx, step, "模型端點回應異常")
        calls = msg.get("tool_calls") or []
        if not calls:
            messages.append({"role": "assistant", "content": msg.get("content") or ""})
            silent += 1
            if silent >= 2:
                return _fallback(ctx, step, "模型沒有呼叫工具")
            messages.append({"role": "user", "content": NUDGE})
            continue
        silent = 0
        # What read_text returned in the step before has now been in front of the model, and the model went on
        # working: from here those texts count as checked. Not earlier: text asked for in the same reply as
        # finish, on the last step, or just before the endpoint failed was never seen by the model.
        ctx.covered |= ctx.pending_reads
        ctx.pending_reads.clear()
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        images, done = [], False
        for c in calls:
            fn = c.get("function") or {}
            name = str(fn.get("name", ""))
            raw = fn.get("arguments") or "{}"
            started = time.time()
            try:
                args = raw if isinstance(raw, dict) else json.loads(raw)
                if not isinstance(args, dict):
                    raise ValueError
            except ValueError:
                bad_args += 1
                result = {"error": "參數不是合法的 JSON 物件，請重新呼叫"}
                args = {}
            else:
                phrase = STEP_PHRASES.get(name)
                ctx.progress(f"代理檢查中（第 {step} 步：{phrase}）" if phrase else f"代理檢查中（第 {step} 步）",
                             0.1 + 0.8 * (step - 1) / limit)
                try:
                    result = tools.call(ctx, name, args)
                except LLMError:
                    return _fallback(ctx, step, "模型端點回應異常")
                except JobStopped:
                    raise
                except Exception as e:  # a tool bug must not end the job: tell the model and carry on
                    result = {"error": f"工具執行失敗（{type(e).__name__}）"}
                if isinstance(result, tuple):
                    result, url = result
                    images.append(url)
            ctx.record(step, name, args, result, started)
            messages.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": _clip(result)})
            if name == "finish" and isinstance(result, dict) and result.get("ok"):
                done = True
        if done:
            return AgentOutcome(True, step)
        if bad_args >= 3:
            return _fallback(ctx, step, "工具參數格式一再錯誤")
        notes = []
        if not told_complete and not ctx.unchecked_units():
            told_complete = True
            extra = max(1, ctx.settings.agent_extra_steps)
            limit = min(limit, step + extra)
            notes.append(COMPLETE.format(n=limit - step))
        elif limit - step == FEW_LEFT:
            notes.append(f"還剩 {FEW_LEFT} 步。請收尾：補上需要的項目，然後呼叫 finish。")
        if images:
            notes.append("這是你要看的圖片。")
            messages.append({"role": "user", "content": [{"type": "image_url", "image_url": {"url": u}} for u in images]
                             + [{"type": "text", "text": "\n".join(notes)}]})
        elif notes:
            messages.append({"role": "user", "content": "\n".join(notes)})
    return _fallback(ctx, limit, "超過步數上限")
