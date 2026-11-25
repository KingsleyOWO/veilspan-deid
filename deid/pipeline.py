"""The two halves of a job: analyse (find what to mask) and apply (mask it, then prove it worked)."""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from deid.agent.context import JobContext
from deid.config import Settings
from deid.formats.base import BrokenFile, adapter_for
from deid.imaging import FaceDetector, OcrEngine
from deid.llm import LLMClient


@dataclass
class VerifyResult:
    ok: bool
    residue: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)  # not failures: places where content was left by the user's choice


def build_context(src: Path, workdir: Path, settings: Settings, llm=None, progress: Callable[[str, float], None] | None = None,
                  *, ocr=None, faces=None, restore: bool = False) -> JobContext:
    ctx = JobContext(src, workdir, settings, adapter_for(Path(src), settings), llm or LLMClient(settings),
                     ocr or OcrEngine(settings), faces or FaceDetector(settings), progress)
    if restore:
        ctx.restore()
    return ctx


def analyze(ctx: JobContext, *, use_agent: bool = True, mode: str = "mask", lang: str = "zh-TW") -> None:
    """Parse, check every unit (agent first, standard flow as the guarantee), then save. Nothing is saved
    if the model endpoint fails: a half-checked document is never presented as checked.
    For the code function (`mode` "code") the model is then asked which names are one person."""
    ctx.progress("拆解文件", 0.02)
    ctx.load()
    ctx.progress("準備圖片", 0.05)
    ctx.write_previews()
    ctx.add_safety_net()
    if use_agent:
        from deid.agent.loop import run_agent
        ctx.progress("代理檢查中", 0.1)
        outcome = run_agent(ctx, lang=lang)
        ctx.outcome = {"agent": True, "finished": outcome.finished, "steps": outcome.steps,
                       "fallback_reason": outcome.fallback_reason}
    else:
        ctx.outcome = {"agent": False}
    ctx.cover_remaining()
    ctx.add_safety_net()
    ctx.add_derived()
    ctx.report_unread()
    if mode == "code" and ctx.settings.link_names:
        from deid.agent.samename import link_names
        ctx.progress("判斷同一人", 0.95)
        ctx.outcome["names"] = link_names(ctx)
    ctx.progress("整理結果", 0.98)
    ctx.save()
    ctx.progress("完成", 1.0)


def apply_and_verify(ctx: JobContext) -> VerifyResult:
    plan = ctx.store.plan()
    dst = ctx.workdir / f"output{ctx.src.suffix.lower()}"
    ctx.workdir.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name("output.tmp" + dst.suffix)
    ctx.adapter.apply(ctx.src, tmp, plan, ctx.settings)
    tmp.replace(dst)
    notes: list[str] = []
    try:
        residue = ctx.adapter.residue(dst, plan.strings)
        problems = ctx.adapter.verify(ctx.src, dst, plan, ctx.settings)
        # Strings with some places deliberately left unmasked are not residue, but the user is told where
        # they can still be found, including places the screen does not show. The same goes for a string
        # that a longer, partially masked one leaves readable (the domain of a mailbox masked as l**n@domain).
        partial = ctx.store.partial_strings()
        if partial:
            notes = [f"部分保留：{line}" for line in ctx.adapter.residue(dst, partial)]
        kept = [s for s in plan.lenient if s not in partial]
        if kept:
            notes += [f"遮一半保留：{line}" for line in ctx.adapter.residue(dst, kept)]
    except BrokenFile:
        residue, problems = [], ["輸出檔無法重新開啟"]
    stats = {"text_spans": sum(len(v) for v in plan.text.values()), "boxes": sum(len(v) for v in plan.boxes.values()),
             "meta": len(plan.meta), "strings": len(plan.strings)}
    return VerifyResult(ok=not residue and not problems, residue=residue, problems=problems, stats=stats, notes=notes)
