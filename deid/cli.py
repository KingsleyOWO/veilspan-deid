"""Command line: run one file through the whole flow, or start the web server."""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

from deid import codefile, codes, patterns, replyfile, restorefile
from deid.config import SettingError, load_settings
from deid.formats.base import SUPPORTED, BrokenFile, UnsupportedFormat
from deid.llm import LLMError
from deid.partial import PARTIAL_TYPES
from deid.pipeline import VerifyResult, analyze, apply_and_verify, build_context
from deid.restore import restore as put_back


class _RuleLLM:
    """Stand-in model for tests only (DEID_FAKE_LLM=1): reports pattern candidates and DEID_FAKE_NAMES."""

    def chat(self, messages, *, tools=None, max_tokens=4000):
        content = messages[-1]["content"]
        if not isinstance(content, str):
            return {"role": "assistant", "content": "{}"}
        names = [n for n in os.environ.get("DEID_FAKE_NAMES", "").split(",") if n]
        items = [{"text": c.text, "type": "其他"} for c in patterns.candidates(content)]
        items += [{"text": n, "type": "姓名"} for n in names if n in content]
        return {"role": "assistant", "content": json.dumps(items, ensure_ascii=False)}


def _report(ctx, result, seconds: float) -> dict:
    return {"file": ctx.src.name, "seconds": round(seconds, 1), "outcome": ctx.outcome,
            "findings": ctx.store.report(ctx.settings.mask_full, ctx.settings.mask_half),
            "warnings": ctx.warnings, "unmaskable": ctx.unmaskable,
            "unchecked": [{"name": u.name, "reason": u.reason} for u in ctx.inv.unchecked],
            "verify": {"ok": result.ok, "residue": result.residue, "problems": result.problems, "stats": result.stats}}


def _score(ctx, answers: dict) -> str:
    active = [f.text for f in ctx.store.findings if f.kind == "text" and f.on]
    expected, forbidden = answers.get("expected", []), answers.get("forbidden", [])
    missing = [i for i, e in enumerate(expected, 1) if not any(e in a or a in e for a in active)]
    wrong = [i for i, t in enumerate(forbidden, 1) if any(t == a or t in a for a in active)]
    line = f"答案比對：找到 {len(expected) - len(missing)}/{len(expected)}"
    if missing:
        line += f"（缺少第 {'、'.join(map(str, missing))} 筆）"
    line += f"；誤遮 {len(wrong)}"
    if wrong:
        line += f"（第 {'、'.join(map(str, wrong))} 筆）"
    return line


def _password() -> str:
    """For a restore file made before 2026-11-19, which is encrypted: from DEID_RESTORE_PASSWORD, or asked for
    at the terminal (never taken from the command line)."""
    given = os.environ.get("DEID_RESTORE_PASSWORD")
    if given is not None:
        return given
    import getpass
    return getpass.getpass("這個還原檔是之前用密碼加密的，請輸入密碼：")


def run(args) -> int:
    src = Path(args.file)
    partial = list(PARTIAL_TYPES) if args.partial == "all" else [t.strip() for t in (args.partial or "").split(",") if t.strip()]
    if any(t not in PARTIAL_TYPES for t in partial):
        print(f"這些類別不能遮一半：{'、'.join(t for t in partial if t not in PARTIAL_TYPES)}。可以的有：{'、'.join(PARTIAL_TYPES)}",
              file=sys.stderr)
        return 1
    settings = load_settings()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="deid-cli-"))
    llm = _RuleLLM() if os.environ.get("DEID_FAKE_LLM") == "1" else None
    started = time.time()
    try:
        try:
            ctx = build_context(src, work, settings, llm=llm,
                                progress=(lambda stage, frac: print(f"  [{frac:4.0%}] {stage}", flush=True)) if args.verbose else None)
            analyze(ctx, use_agent=settings.use_agent and not args.no_agent, mode="code" if args.code else "mask")
            changed = {t: ctx.store.bulk(type=t, style="partial") for t in partial}
            coded = codes.build(ctx.inv, ctx.store) if args.code else None
            result = (VerifyResult(ok=not coded.residue, residue=coded.residue, notes=coded.notes,
                                   stats={"codes": len(coded.table), "blanked": coded.blanked})
                      if coded else apply_and_verify(ctx))
        except UnsupportedFormat:
            print(f"目前只支援 {'、'.join(SUPPORTED)}", file=sys.stderr)
            return 1
        except BrokenFile as e:
            print(f"無法處理這個檔案：{e}", file=sys.stderr)
            return 1
        except LLMError as e:
            print(f"模型端點無法連線或回應異常（{e}）", file=sys.stderr)
            return 1
        seconds = time.time() - started
        if coded:
            stamp = time.strftime("%Y%m%d-%H%M%S")  # not the document's name: it often holds a person's name
            (out / f"代號版_{stamp}.txt").write_text(coded.text, encoding="utf-8")
            (out / f"還原檔_{stamp}.deidr").write_bytes(restorefile.pack(coded.table))
            own = out / f"代號版_{stamp}{src.suffix.lower()}"  # the document itself with the codes in it (Word, Excel)
            made = codefile.make(ctx, coded, own)
            print(f"輸出：代號版_{stamp}.txt、還原檔_{stamp}.deidr" + (f"、{own.name}" if made.ok else ""))
        else:
            shutil.copyfile(work / f"output{src.suffix.lower()}", out / f"masked_{src.name}")
        (out / f"{src.name}.report.json").write_text(json.dumps(_report(ctx, result, seconds), ensure_ascii=False, indent=1),
                                                     encoding="utf-8")
        listed = ctx.store.listed()
        on = [f for f in listed if f.on]
        print(f"檔案：{len(ctx.inv.texts)} 個文字單元、{len(ctx.inv.images)} 張圖、{len(ctx.inv.metas)} 個屬性欄位；耗時 {seconds:.0f} 秒")
        print("啟用的項目：" + ("、".join(f"{t} {n}" for t, n in Counter(f.type for f in on).most_common()) or "無"))
        print(f"未啟用的建議：{sum(1 for f in listed if not f.on)}")
        if partial:
            print("遮一半：" + "、".join(f"{t} {n}" for t, n in changed.items()))
        if ctx.outcome.get("agent"):
            note = "完成" if ctx.outcome.get("finished") else f"未自行結束（{ctx.outcome.get('fallback_reason')}）"
            print(f"代理：{ctx.outcome.get('steps')} 步，{note}")
        for w in ctx.warnings:
            print(f"警告：{w}")
        for u in ctx.unmaskable:  # where and why; the string itself is in the report file only
            print(f"無法自動遮蔽，需人工處理：{u['where']}（{u['reason']}）")
        if coded:
            print(f"代號版文字：代號 {len(coded.table)}、不能還原的 [已遮蔽] {coded.blanked} 處、{len(coded.text)} 字")
            if made.ok:
                print(f"代號版檔案：寫入 {made.placed} 處代號、維持遮蔽 {made.left} 處")
            for note in made.notes:
                print(f"  - {note}")
        print(f"驗證：{'通過' if result.ok else '未通過'}（殘留 {len(result.residue)}、問題 {len(result.problems)}）")
        for line in result.residue + result.problems:
            print(f"  - {line}")
        if args.answers:
            print(_score(ctx, json.loads(Path(args.answers).read_text(encoding="utf-8"))))
        return 0 if result.ok else 2
    finally:
        shutil.rmtree(work, ignore_errors=True)


def restore(args) -> int:
    """Put the strings back into a reply that carries codes. Counts go to the screen; the text goes to --out
    (or to the screen when no file is named)."""
    data = Path(args.key).read_bytes()
    try:
        try:
            table = restorefile.open_(data)
        except restorefile.NeedsPassword:
            table = restorefile.open_(data, _password())
    except restorefile.NeedsPassword:
        print("這個還原檔是之前用密碼加密的，需要密碼", file=sys.stderr)
        return 1
    except restorefile.NotRestoreFile:
        print("這不是本系統產生的還原檔", file=sys.stderr)
        return 1
    except restorefile.WrongKey:
        print("密碼不對，或還原檔已損毀", file=sys.stderr)
        return 1
    source = Path(args.infile)
    made = None
    if source.suffix.lower() in SUPPORTED:  # a file the cloud handed back
        try:
            done = replyfile.restore_file(source.read_bytes(), source.name, table, load_settings())
        except BrokenFile as e:
            print(str(e), file=sys.stderr)
            return 1
        except replyfile.NoText:
            print("這個檔案讀不到文字（可能是掃描檔，或內容都在圖片裡）", file=sys.stderr)
            return 1
        for note in done.notes:
            print(note, file=sys.stderr)
        if args.out and done.file is not None and Path(args.out).suffix.lower() == done.ext:
            made = done.file  # --out names the same kind of file: the file itself, strings in place
    else:
        done = put_back(source.read_text(encoding="utf-8"), table)
    if made is not None:
        Path(args.out).write_bytes(made)
    elif args.out:
        Path(args.out).write_text(done.text, encoding="utf-8")
    else:
        print(done.text)
    print(f"換回 {done.replaced} 處；還原檔裡沒有的代號 {len(done.unknown)} 個；沒出現的代號 {len(done.unused)} 個",
          file=sys.stdout if args.out else sys.stderr)
    if done.unknown:
        print("還原檔裡沒有的代號：" + "、".join(done.unknown), file=sys.stdout if args.out else sys.stderr)
    return 0


def serve(args) -> int:
    import logging

    import uvicorn

    # Job ids, stages, timings and counts only; the application never logs file names or content.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per model request is noise

    from deid.web.app import create_app
    settings = load_settings()
    # proxy_headers off: the client is whoever holds the connection, not whoever a header names (ALLOWED_CLIENTS).
    uvicorn.run(create_app(settings), host=args.host or settings.host, port=args.port or settings.port,
                log_level="warning", proxy_headers=False)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="deid", description="本地去識別化工具")
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="處理一個檔案，輸出遮蔽檔與報告")
    r.add_argument("file")
    r.add_argument("--out", default=".")
    r.add_argument("--no-agent", action="store_true", help="不使用代理，只跑標準流程")
    r.add_argument("--partial", help="這些類別只遮一部分的字，用逗號分隔（例如 姓名,電話）；all 表示所有可以遮一半的類別")
    r.add_argument("--code", action="store_true",
                   help="不產生遮蔽檔，改為輸出代號版文字、還原檔，以及 Word／Excel 的代號版檔案（還原檔沒有加密，請自己保管）")
    r.add_argument("--answers", help="正確答案 JSON：{\"expected\": [...], \"forbidden\": [...]}")
    r.add_argument("-v", "--verbose", action="store_true")
    r.set_defaults(func=run)
    b = sub.add_parser("restore", help="把含有代號的文字還原（需要還原檔；之前用密碼加密的還原檔會詢問密碼）")
    b.add_argument("--key", required=True, help="還原檔（.deidr）")
    b.add_argument("--in", dest="infile", required=True, help="含有代號的文字檔（UTF-8），或雲端給的 .docx、.pdf、.xlsx")
    b.add_argument("--out", help="還原後的文字要寫到哪個檔案；不指定就印在畫面上。輸入是 .docx 或 .xlsx、這裡的副檔名也相同時，寫出還原後的檔案")
    b.set_defaults(func=restore)
    s = sub.add_parser("serve", help="啟動網頁服務")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.set_defaults(func=serve)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SettingError as e:  # the message names the setting and says what to write: no traceback needed
        print(e, file=sys.stderr)
        return 1
