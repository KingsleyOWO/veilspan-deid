"""The tools the model may call while checking a document. Every tool reads or records; none edits the file."""
import json
import re
import time

import regex  # like re, but a match can be given a time limit

from deid import imaging, patterns
from deid.agent import sandbox
from deid.agent.context import JobContext
from deid.detect import TYPES
from deid.findings import AGENT_CANCEL, AGENT_DOUBT
from deid.model import tight

READ_BUDGET = 3600  # size of the texts one read_text call returns; the whole reply must stay under the loop's limit
READ_LIST_CAP = 250  # ids listed as not returned
MAX_HITS = 50
SEARCH_LIMIT = 40  # per job; generous, it only stops a model that searches in circles without finishing
SEARCH_SECONDS = 5.0  # a pattern the model wrote may backtrack without end; the worker must not hang on it
MAX_LISTED_IMAGES = 60


def _fn(name: str, description: str, properties: dict | None = None, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties or {}, "required": required or []}}}


_IDS = {"type": "array", "items": {"type": "string"}}
_STR = {"type": "string"}

SCHEMAS = [
    _fn("get_inventory", "列出這份文件的組成：文字單元、圖片、檔案屬性、無法檢查的項目，以及還沒檢查過的單元。"),
    _fn("scan_text", "對文字單元執行標準個資偵測並把結果加入遮蔽項目。不給 unit_ids 就掃描全部文字。", {"unit_ids": _IDS}),
    _fn("scan_images", "對圖片執行 OCR、文字個資偵測、人臉偵測與圖片類型判斷。不給 image_ids 就處理全部圖片。", {"image_ids": _IDS}),
    _fn("read_text", "讀取文字單元的原文，或圖片的 OCR 文字（給圖片 id）。一次回傳有長度上限，沒回傳的會列在 not_returned。",
        {"unit_ids": _IDS}, ["unit_ids"]),
    _fn("view_image", "直接看一張圖片。圖片會附在下一則訊息。只是看，不算檢查過；圖片要用 scan_images 才算檢查。",
        {"image_id": _STR}, ["image_id"]),
    _fn("search", "在全文、圖片文字、檔案屬性裡搜尋字串（或正規表示式），回傳所在單元與前後文。",
        {"query": _STR, "regex": {"type": "boolean"}}, ["query"]),
    _fn("pattern_candidates", "列出程式找到的候選與前後文，由你判斷：樣式比對（身分證字號、居留證號、信用卡號、手機、電子郵件），"
        "以及已找到的人名的其他寫法（姓氏加稱謂、只寫名字）。already_found 為 false 的需要你決定要不要用 add_findings 加入。"),
    _fn("list_findings", "列出目前的遮蔽項目。"),
    _fn("add_findings", "新增要遮蔽的個資。text 必須是原文中一字不差的連續字串，只取個資本身；會套用到全文所有出現處。",
        {"items": {"type": "array", "items": {"type": "object", "properties": {
            "text": _STR, "type": {"type": "string", "enum": TYPES}, "reason": _STR}, "required": ["text", "type", "reason"]}}},
        ["items"]),
    _fn("add_image_mask", "把整張圖片（PDF 為整頁影像）列為遮蔽。只用於證件、簽名印章、手寫文件、病歷這類要看圖才知道的內容，並指定對應的 type。"
        "截圖或圖片裡的姓名、電話等文字，程式已依位置遮蔽，不要為了那些文字把整張圖遮掉。",
        {"image_id": _STR, "type": {"type": "string", "enum": ["證件", "簽名印章", "手寫個資", "病歷", "其他"]}, "reason": _STR},
        ["image_id", "reason"]),
    _fn("remove_finding", "對一筆項目提出異議。你自己用 add_findings 加的會被取消；第一輪偵測或規則找到的只會標記為存疑，仍然遮蔽，"
        "由人工檢查時決定。", {"id": _STR, "reason": _STR}, ["id", "reason"]),
    _fn("report_unmaskable", "回報程式改不到的位置有個資，需要人工處理。只用在 unchecked_items 或 run_python 發現的位置；"
        "內文、表格、頁首頁尾、圖片裡的文字與人臉，程式都會自動遮蔽，不需要回報。", {"where": _STR, "text": _STR, "reason": _STR},
        ["where", "text", "reason"]),
    _fn("run_python", "在隔離環境執行你寫的 Python 腳本，用來檢查固定工具讀不到的部分。原檔在 /work/input.docx、/work/input.pdf 或 /work/input.xlsx（依上傳的格式），"
        "只能讀取；沒有網路；60 秒逾時；可用 zipfile、pymupdf、PIL、openpyxl、docx、lxml、olefile。用 print 輸出，最多回傳 8000 字。",
        {"code": _STR}, ["code"]),
    _fn("finish", "所有單元都檢查完後呼叫。還有沒檢查的單元時會被拒絕並告知缺哪些。", {"summary": _STR}, ["summary"]),
]


def schemas(ctx: JobContext) -> list[dict]:
    if sandbox.available(ctx.settings):
        return SCHEMAS
    return [t for t in SCHEMAS if t["function"]["name"] != "run_python"]


def _ids(value) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value] if isinstance(value, list) else None


def _search_space(ctx: JobContext):
    for t in ctx.inv.texts:
        if not t.mirror:
            yield t.id, t.text
    for im in ctx.inv.images:
        for ln in im.ocr:
            yield im.id, ln.text
    for m in ctx.inv.metas:
        yield m.id, m.value


def _context(text: str, start: int, end: int, pad: int = 20) -> str:
    return text[max(0, start - pad): end + pad]


def get_inventory(ctx, args):
    sections: dict[str, int] = {}
    for t in ctx.inv.texts:
        if not t.mirror:
            sections[t.section] = sections.get(t.section, 0) + 1
    images = [{"id": im.id, "width": im.width, "height": im.height, "ocr_chars": sum(len(ln.text) for ln in im.ocr),
               "faces": len(im.faces), "checked": im.id in ctx.covered} for im in ctx.inv.images if im.supported and im.needs_scan]
    out = {"kind": ctx.inv.kind, "text_units": len(ctx.text_ids()),
           "text_chars": sum(len(t.text) for t in ctx.inv.texts if not t.mirror), "sections": sections,
           "image_count": len(images), "images": images[:MAX_LISTED_IMAGES],
           "metadata": [{"id": m.id, "label": m.label, "value": m.value} for m in ctx.inv.metas],
           "unchecked_items": [{"name": u.name, "reason": u.reason} for u in ctx.inv.unchecked][:40],
           "unchecked": ctx.unchecked_units()[:100], "unchecked_count": len(ctx.unchecked_units()),
           "findings": len(ctx.store.findings)}
    if len(images) > MAX_LISTED_IMAGES:
        out["images_note"] = f"只列出前 {MAX_LISTED_IMAGES} 張，其餘 id 依序為 {images[MAX_LISTED_IMAGES]['id']} 到 {images[-1]['id']}"
    return out


def scan_text(ctx, args):
    return ctx.scan_text(_ids(args.get("unit_ids")))


def scan_images(ctx, args):
    return ctx.scan_images(_ids(args.get("image_ids")))


def read_text(ctx, args):
    ids = _ids(args.get("unit_ids")) or []
    texts, unknown, skipped, used = {}, [], [], 0
    for uid in ids:
        unit = ctx.inv.text(uid)
        image = ctx.inv.image(uid) if unit is None else None
        if unit is None and (image is None or not image.supported):
            unknown.append(uid)
            continue
        if unit is None:
            if not image.ocr and uid not in ctx.covered:
                ctx.ocr_image(uid)
            body = "\n".join(ln.text for ln in image.ocr)
        else:
            body = unit.text
        cost = len(json.dumps({uid: body}, ensure_ascii=False)) + 1  # what this entry adds to the reply
        if texts and used + cost > READ_BUDGET:
            skipped.append(uid)
            continue
        texts[uid] = body[:READ_BUDGET]
        used += min(cost, READ_BUDGET)
        if unit is not None and len(body) <= READ_BUDGET:
            # Not checked yet: the model has not seen this text. The loop counts it once the model has answered
            # (deid.agent.loop). Images are only checked by scan_images: OCR and face detection must run.
            ctx.pending_reads.add(uid)
    out = {"texts": texts}
    if unknown:
        out["unknown_ids"] = unknown[:20]
    if skipped:
        out["not_returned"] = skipped[:READ_LIST_CAP]
        if len(skipped) > READ_LIST_CAP:
            out["not_returned_count"] = len(skipped)
    return out


def view_image(ctx, args):
    image_id = str(args.get("image_id", ""))
    im = ctx.inv.image(image_id)
    if im is None or not im.supported:
        return {"error": f"沒有這張圖片，或格式不支援：{image_id}"}
    url = imaging.vision_jpeg_url(ctx.image_data(image_id))
    # Looking is not checking: the image still needs scan_images so OCR and face detection run on it.
    note = "圖片附在下一則訊息" + ("" if image_id in ctx.covered else "；這張圖還沒做 scan_images")
    return {"ok": True, "note": note}, url


def search(ctx, args):
    query = str(args.get("query", ""))
    if not query:
        return {"error": "query 不可為空"}
    if ctx.tool_counts.get("search", 0) > SEARCH_LIMIT:
        return {"error": "搜尋次數已達上限。請依現有資訊判斷，補上需要的項目後呼叫 finish。"}
    return find(ctx, query, bool(args.get("regex")))


def find(ctx, query: str, is_regex: bool) -> dict:
    """The search itself, without the detection agent's limit: other model steps look things up with it."""
    if not query:
        return {"error": "query 不可為空"}
    try:
        pattern = regex.compile(query if is_regex else regex.escape(query))
    except regex.error:
        return {"error": "正規表示式格式錯誤"}
    hits, total = [], 0
    deadline = time.monotonic() + SEARCH_SECONDS
    try:
        for unit, text in _search_space(ctx):
            # One limit for the whole search: each unit gets what is left of it, and after the deadline nothing.
            # A fresh allowance per unit let a pattern a little cheaper than that allowance run on over every unit.
            # (The regex module reads a negative timeout as none at all, so it is never handed one.)
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            for m in pattern.finditer(text, timeout=left):
                if m.end() == m.start():
                    continue
                total += 1
                if len(hits) < MAX_HITS:
                    hits.append({"unit": unit, "match": m.group(0), "context": _context(text, m.start(), m.end())})
    except TimeoutError:
        return {"error": "這個樣式比對逾時（可能會無限回溯），已中止。請改用較簡單的樣式，或直接搜尋字串。"}
    return {"total": total, "hits": hits}


HONORIFICS = ("先生|小姐|女士|太太|老師|醫師|醫生|護理師|律師|博士|教授|同學|主任|經理|副理|組長|課長|處長|科長|院長|"
              "所長|校長|董事長|總經理|專員|秘書|顧問")
COMPOUND_SURNAMES = ("歐陽", "司馬", "諸葛", "上官", "司徒", "夏侯", "張簡", "范姜", "皇甫", "尉遲", "端木", "東方")


def _name_variants(ctx: JobContext) -> list[dict]:
    """Other ways a person already found may be written: surname plus title, or the given name alone.
    The model misses these now and then; the harness finds the candidates, the model decides."""
    names = [f.text for f in ctx.store.findings if f.kind == "text" and f.type == "姓名" and f.on
             and 2 <= len(f.text) <= 4 and all("\u4e00" <= c <= "\u9fff" for c in f.text)]
    known = {f.text for f in ctx.store.findings if f.kind == "text" and f.on}
    out, seen = [], set()
    for full in names:
        surname = full[:2] if full[:2] in COMPOUND_SURNAMES and len(full) > 2 else full[:1]
        given = full[len(surname):]
        variants = [re.compile(re.escape(surname) + "(?:" + HONORIFICS + ")")]
        if len(given) == 2:
            variants.append(re.compile("(?<!" + re.escape(surname) + ")" + re.escape(given)))
        for pattern in variants:
            for unit, text in _search_space(ctx):
                for m in pattern.finditer(text):
                    if m.group(0) == full or (m.group(0), unit) in seen:
                        continue
                    seen.add((m.group(0), unit))
                    out.append({"text": m.group(0), "kind": "姓名的其他寫法", "related_to": full, "verified": False, "unit": unit,
                                "context": _context(text, m.start(), m.end()), "already_found": m.group(0) in known})
    return out


def pattern_candidates(ctx, args):
    active = {f.text for f in ctx.store.findings if f.kind == "text" and f.on}
    out, seen = [], set()
    # also each page text read without the spaces put in at line ends: a number the layout broke over two lines
    broken = [(t.id, joined[0]) for t in ctx.inv.texts if not t.mirror and (joined := tight(t))]
    for unit, text in list(_search_space(ctx)) + broken:
        for c in patterns.candidates(text):
            if (c.text, unit) in seen:
                continue
            seen.add((c.text, unit))
            out.append({"text": c.text, "kind": c.kind, "verified": c.verified, "unit": unit,
                        "context": _context(text, c.start, c.end), "already_found": c.text in active})
    out += _name_variants(ctx)
    out.sort(key=lambda c: c["already_found"])  # what still needs a decision comes first
    return {"total": len(out), "candidates": out[:MAX_HITS]}


def list_findings(ctx, args):
    texts = [{"id": f.id, "text": f.text, "type": f.type, "source": f.source, "on": f.on, "count": len(f.occurrences)}
             for f in ctx.store.findings if f.kind in ("text", "meta")]
    regions: dict[str, dict] = {}
    for f in ctx.store.findings:
        if f.kind == "region":
            r = regions.setdefault(f.type, {"on": 0, "off": 0})
            r["on" if f.on else "off"] += 1
    return {"items": texts[:150], "item_count": len(texts), "regions": regions}


def add_findings(ctx, args):
    items = args.get("items")
    if not isinstance(items, list):
        return {"error": "items 必須是陣列"}
    results = []
    for it in items:
        text = (it.get("text") if isinstance(it, dict) else None) or ""
        text = text.strip() if isinstance(text, str) else ""
        if len(text) < 2:
            results.append({"text": text, "accepted": False, "error": "至少要 2 個字"})
            continue
        kind = it.get("type") if it.get("type") in TYPES else "其他"
        why = ctx.why_off(text, kind)
        reason = str(it.get("reason", ""))
        f = ctx.store.add_text(text, kind, "agent", on=not why, reason=(f"{why}（代理：{reason}）" if why else reason)[:200])
        if f is None:
            results.append({"text": text, "accepted": False, "error": "原文中找不到完全一致的字串"})
        else:
            entry = {"text": text, "accepted": True, "id": f.id, "count": len(f.occurrences), "on": f.on}
            if why:
                entry["note"] = f"{why}，已列為未啟用的建議。" + ("" if "網址" in why else "要遮的是姓名本身。")
            results.append(entry)
    return {"results": results}


def add_image_mask(ctx, args):
    image_id = str(args.get("image_id", ""))
    im = ctx.inv.image(image_id)
    if im is None or not im.supported:
        return {"error": f"沒有這張圖片，或格式不支援：{image_id}"}
    kind = str(args.get("type") or "其他")
    if kind == "其他":
        # Blacking out a whole screenshot because a name is in it throws away everything else on it.
        masked = sum(1 for f in ctx.store.findings if f.kind == "text" and f.on
                     for o in f.occurrences if o.kind == "box" and o.unit == image_id and o.on)
        if masked:
            return {"ok": False, "error": f"這張圖裡的文字個資已由程式依位置遮蔽（{masked} 處），不需要整張遮蔽。整張遮蔽只用在證件、"
                                          "簽名印章、手寫個資、病歷這類要看圖才知道的內容；確實是的話，請指定對應的 type 再呼叫一次。"}
    f = ctx.store.add_region(image_id, (0.0, 0.0, float(im.width), float(im.height)), kind,
                             "agent", on=True, reason=str(args.get("reason", ""))[:200])
    return {"ok": True, "id": f.id}


def remove_finding(ctx, args):
    try:
        f = ctx.store.get(str(args.get("id", "")))
    except KeyError:
        return {"error": "沒有這個項目 id"}
    reason = str(args.get("reason", ""))[:200]
    if f.source == "agent":
        ctx.store.set_on(f.id, False)
        f.reason = AGENT_CANCEL + reason
        return {"ok": True, "switched_off": True}
    # A detection the agent did not make stays on: wrongly dropping a real name is worse than one extra
    # mask, and the person reviewing sees the doubt and decides.
    f.reason = AGENT_DOUBT + reason
    return {"ok": True, "switched_off": False, "note": "已標記為存疑，仍會遮蔽，由人工檢查時決定"}


def _masked_wherever_read(ctx: JobContext, text: str) -> list[str]:
    """The units in which the program reads the string, when every place it stands in them lies inside what is
    being masked; otherwise none. Looked for as it is written, inside a longer string too: a number the model's
    own script read as it is stored (912345678) is part of the cell the program reads as it is shown."""
    if len(text) < 2:
        return []
    masked = ctx.store.plan().text
    units = []
    for t in ctx.inv.texts:
        at = -1 if t.mirror else t.text.find(text)
        while at >= 0:
            if not any(s <= at and at + len(text) <= e for s, e in masked.get(t.id, [])):
                return []
            if t.id not in units:
                units.append(t.id)
            at = t.text.find(text, at + 1)
    return units


def report_unmaskable(ctx, args):
    text = str(args.get("text", "")).strip()
    units = _masked_wherever_read(ctx, text) if text not in ctx.questioned else []
    if units:
        # Most such reports are about a place the program does read and mask (the model looked at the file with
        # its own script and saw another spelling). Asked once; a second report of the same string is taken.
        ctx.questioned.add(text)
        return {"ok": False, "error": f"程式讀得到這個字串，它出現的地方（{units[:5]}）都已經在遮蔽範圍內，產生檔案後也會檢查。"
                                      "如果你指的就是這些位置，不需要回報。如果是程式讀不到的另一個位置，請再呼叫一次，並在 where 寫明是哪一個部分。"}
    # Recorded once, in `unmaskable` only: the review screen lists these after the warnings, the result screen lists
    # them with what was not checked, and report.json carries them with the string and the reason. A warning as
    # well showed the same place twice on the review screen and in report.json.
    ctx.unmaskable.append({"where": str(args.get("where", ""))[:200], "text": str(args.get("text", ""))[:200],
                           "reason": str(args.get("reason", ""))[:200]})
    return {"ok": True}


def run_python(ctx, args):
    if not sandbox.available(ctx.settings):
        return {"error": "腳本環境不可用"}
    return sandbox.run_python(ctx.settings, ctx.src, str(args.get("code", "")), workdir=ctx.workdir)


def finish(ctx, args):
    left = [u for u in ctx.unchecked_units() if u not in ctx.pending_reads]
    if left:
        more = f"（共 {len(left)} 個）" if len(left) > 40 else ""
        return {"ok": False, "error": f"尚有未檢查的單元：{left[:40]}{more}"}
    unseen = [u for u in ctx.unchecked_units() if u in ctx.pending_reads]
    if unseen:  # read_text and finish in one reply: the text has not been in front of the model yet
        more = f"（共 {len(unseen)} 個）" if len(unseen) > 40 else ""
        return {"ok": False, "error": f"這些單元的原文這一步才回傳，你還沒看過：{unseen[:40]}{more}。"
                                      "請先看內容，把其中的個資用 add_findings 加入，再呼叫 finish。"}
    ctx.outcome["summary"] = str(args.get("summary", ""))[:500]
    return {"ok": True}


_TOOLS = {f.__name__: f for f in (get_inventory, scan_text, scan_images, read_text, view_image, search, pattern_candidates,
                                  list_findings, add_findings, add_image_mask, remove_finding, report_unmaskable,
                                  run_python, finish)}


def call(ctx: JobContext, name: str, args: dict):
    """Returns the tool result, or (result, image data URL) for view_image."""
    fn = _TOOLS.get(name)
    if fn is None:
        return {"error": f"沒有這個工具：{name}"}
    if not isinstance(args, dict):
        return {"error": "參數必須是 JSON 物件"}
    ctx.tool_counts[name] = ctx.tool_counts.get(name, 0) + 1
    return fn(ctx, args)
