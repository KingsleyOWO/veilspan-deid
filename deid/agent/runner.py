"""A tool-calling loop for the model steps that are not the detection agent. Today that is one step: deciding
which names are one person (deid.agent.samename). The detection agent keeps its own loop (deid.agent.loop):
how it behaves was measured on real documents and is not touched from here.

The caller supplies the prompt, the tools and what each tool does. This loop only carries the conversation and
ends it: when the model finishes, when it stops calling tools, when its arguments stay unreadable, or when it
runs out of steps. A dead endpoint is the caller's to handle."""
import json
import time
from dataclasses import dataclass
from typing import Callable

from deid.agent.context import JobStopped
from deid.agent.loop import NUDGE, _clip
from deid.llm import LLMError


@dataclass
class RunOutcome:
    finished: bool
    steps: int
    reason: str | None = None


def run_tools(llm, *, system: str, opening: str, schemas: list[dict], call: Callable[[str, dict], object], max_steps: int,
              progress: Callable[[int, str], None] | None = None,
              record: Callable[[int, str, dict, object, float], None] | None = None,
              note: Callable[[int, int], str | None] | None = None) -> RunOutcome:
    """`call(name, args)` runs one tool. The run is finished when the tool named finish answers {"ok": True}.
    `note(step, limit)` may return a line to tell the model after a step's tool results."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": opening}]
    limit = max(1, max_steps)
    silent = bad_args = 0
    for step in range(1, limit + 1):
        if progress:
            progress(step, "")
        msg = llm.chat(messages, tools=schemas, max_tokens=2000)
        calls = msg.get("tool_calls") or []
        if not calls:
            messages.append({"role": "assistant", "content": msg.get("content") or ""})
            silent += 1
            if silent >= 2:
                return RunOutcome(False, step, "模型沒有呼叫工具")
            messages.append({"role": "user", "content": NUDGE})
            continue
        silent = 0
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        done = False
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
                args, result = {}, {"error": "參數不是合法的 JSON 物件，請重新呼叫"}
            else:
                if progress:
                    progress(step, name)
                try:
                    result = call(name, args)
                except (LLMError, JobStopped):
                    raise
                except Exception as e:  # noqa: BLE001 - a tool bug must not end the run: tell the model and carry on
                    result = {"error": f"工具執行失敗（{type(e).__name__}）"}
            if record:
                record(step, name, args, result, started)
            messages.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": _clip(result)})
            if name == "finish" and isinstance(result, dict) and result.get("ok"):
                done = True
        if done:
            return RunOutcome(True, step)
        if bad_args >= 3:
            return RunOutcome(False, step, "工具參數格式一再錯誤")
        told = note(step, limit) if note else None
        if told:
            messages.append({"role": "user", "content": told})
    return RunOutcome(False, limit, "超過步數上限")
