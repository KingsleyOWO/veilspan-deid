"""Test doubles. FakeLLM mirrors LLMClient.chat and records every call."""
import json
from typing import Callable

from deid.model import OcrLine


class FakeLLM:
    def __init__(self, handler: Callable[[list[dict], list[dict] | None], dict]):
        self.handler = handler
        self.calls: list[dict] = []

    def chat(self, messages, *, tools=None, max_tokens=4000):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools, "max_tokens": max_tokens})
        return self.handler(messages, tools)


def reply(items) -> dict:
    """An assistant message whose content is the JSON the detector expects."""
    return {"role": "assistant", "content": json.dumps(items, ensure_ascii=False)}


def keyword_llm(table: dict[str, str]) -> FakeLLM:
    """Detector double: reports every key of `table` that occurs in the user text."""
    def handler(messages, tools):
        user = messages[-1]["content"]
        text = user if isinstance(user, str) else " ".join(p.get("text", "") for p in user if isinstance(p, dict))
        return reply([{"text": k, "type": v} for k, v in table.items() if k in text])
    return FakeLLM(handler)


def routed(routes: dict[str, Callable], table: dict[str, str] | None = None) -> FakeLLM:
    """A double for jobs with several model steps: a request goes to the handler whose key occurs in its
    system message. Any other request without tools is a detection request, answered from `table`."""
    detector = keyword_llm(table or {}).handler

    def handler(messages, tools):
        system = messages[0]["content"] if messages and messages[0].get("role") == "system" else ""
        for key, fn in routes.items():
            if isinstance(system, str) and key in system:
                return fn(messages, tools)
        if tools is None:
            return detector(messages, tools)
        return {"role": "assistant", "content": "不知道接下來要做什麼"}

    return FakeLLM(handler)


def tool_call(name: str, args: dict | str, call_id: str = "c1") -> dict:
    arguments = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}]}


def ocr_line(text: str, x: float = 10, y: float = 10, w: float = 20, h: float = 24) -> OcrLine:
    chars = [(x + i * w, y, x + (i + 1) * w, y + h) for i in range(len(text))]
    return OcrLine(text=text, box=(x, y, x + len(text) * w, y + h), chars=chars)


class FakeOcr:
    """OCR double: returns preset lines per call, keyed by image size or a default."""
    def __init__(self, lines: list[OcrLine] | None = None, by_size: dict | None = None):
        self.lines, self.by_size, self.calls = lines or [], by_size or {}, 0

    def read(self, data: bytes) -> list[OcrLine]:
        import io
        from PIL import Image
        self.calls += 1
        size = Image.open(io.BytesIO(data)).size
        return list(self.by_size.get(size, self.lines))


class FakeFaces:
    def __init__(self, boxes=None):
        self.boxes = boxes or []

    def detect(self, data: bytes):
        return list(self.boxes)
