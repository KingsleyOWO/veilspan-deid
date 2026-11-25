"""Thin client for the OpenAI-compatible endpoint. The only place document content leaves the process."""
import threading
import time

import httpx

from deid.config import Settings


class LLMError(Exception):
    """The endpoint could not be reached or answered abnormally."""


_limit_lock = threading.Lock()
_limits: dict[int, threading.Semaphore] = {}
_shared: httpx.Client | None = None
RETRY_STATUS = (408, 429)  # busy or timed out: worth another try, like a 5xx
MAX_RETRY_AFTER = 30.0


def _new_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    # trust_env=False: HTTP(S)_PROXY must never send document text somewhere other than the configured endpoint
    return httpx.Client(timeout=httpx.Timeout(300.0, connect=10.0), transport=transport, trust_env=False)


def _shared_client() -> httpx.Client:
    """One connection pool for the whole process, opened on first use. A client object is created for
    every job and every review request; most of them never talk to the endpoint."""
    global _shared
    with _limit_lock:
        if _shared is None:
            _shared = _new_client()
        return _shared


def _limit(n: int) -> threading.Semaphore:
    with _limit_lock:
        return _limits.setdefault(n, threading.Semaphore(n))


class LLMClient:
    ATTEMPTS = 3

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        self.settings = settings
        self._sleep = sleep
        self._own = _new_client(transport) if transport is not None else None  # tests bring their own transport
        self._sem = _limit(max(1, settings.llm_concurrency))

    def chat(self, messages: list[dict], *, tools: list[dict] | None = None, max_tokens: int = 4000) -> dict:
        s = self.settings
        body = {"model": s.llm_model, "messages": messages, "temperature": 0, "max_tokens": max_tokens,
                "chat_template_kwargs": {"enable_thinking": bool(s.llm_thinking)}}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        url = s.llm_base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {s.llm_api_key}"}
        last = "unknown"
        pause = 0.0
        http = self._own or _shared_client()
        for attempt in range(self.ATTEMPTS):
            if attempt:
                self._sleep(max(1.5 * attempt, pause))
            try:
                with self._sem:
                    r = http.post(url, json=body, headers=headers)
            except httpx.HTTPError as e:
                last = type(e).__name__
                continue
            if r.status_code >= 500 or r.status_code in RETRY_STATUS:
                last = f"HTTP {r.status_code}"
                try:
                    pause = min(MAX_RETRY_AFTER, float(r.headers.get("retry-after", "0")))
                except ValueError:
                    pause = 0.0
                continue
            if r.status_code != 200:
                raise LLMError(f"HTTP {r.status_code}")  # never echo the body: it may quote document text
            try:
                msg = r.json()["choices"][0]["message"]
                if not isinstance(msg, dict):
                    raise TypeError
                return msg
            except (ValueError, KeyError, IndexError, TypeError):
                raise LLMError("malformed response") from None
        raise LLMError(f"endpoint unavailable ({last})")
