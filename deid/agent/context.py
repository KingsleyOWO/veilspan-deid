"""Everything one job needs while it is being analysed: the document, the findings, and what has been checked."""
import functools
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from deid import imaging, patterns
from deid.config import Settings
from deid.detect import DetectResult, detect_lines
from deid.findings import FindingStore
from deid.model import Inventory, inventory_from_json, to_json, tight

FACE_TYPE = "人臉"
GIVE_UP = 2
UNREAD_LISTED = 30
SHORT_NUMBER = re.compile(r"\d{1,4}")  # extensions and the like: the spec leaves these unmasked by default
RAW_PREVIEW = {"PNG": "png", "JPEG": "jpg", "GIF": "gif"}
# A title or form of address on its own identifies nobody. The model is told not to list these, but now
# and then it does ("執行長", next to a name it also listed): such a string is kept as a suggestion,
# switched off, instead of being masked everywhere it occurs.
TITLES = frozenset((
    "先生|小姐|女士|太太|老師|醫師|醫生|護理師|藥師|律師|會計師|建築師|博士|教授|副教授|講師|同學|主任|副主任|經理|副理|協理|襄理|"
    "組長|課長|股長|處長|副處長|科長|院長|副院長|所長|校長|局長|部長|署長|司長|廠長|店長|會長|社長|館長|隊長|班長|"
    "董事長|副董事長|董事|監事|總經理|副總經理|總監|執行長|營運長|財務長|技術長|資訊長|理事長|理事|秘書長|主委|主任委員|委員|"
    "專員|秘書|顧問|助理|負責人|代表人|承辦人|聯絡人|聯絡窗口|主管|員工|職員|使用者|管理員|講者|主持人|受文者|申請人|當事人|"
    "ceo|cfo|cto|coo|cio|manager|director|president|chairman").split("|"))


class JobStopped(Exception):
    """Raised from a progress report to end a job early: it was deleted ("cancelled") or ran too long ("timeout")."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class JobContext:
    def __init__(self, src: Path, workdir: Path, settings: Settings, adapter, llm, ocr, faces,
                 progress: Callable[[str, float], None] | None = None):
        self.src, self.workdir, self.settings = Path(src), Path(workdir), settings
        self.adapter, self.llm, self.ocr, self.faces = adapter, llm, ocr, faces
        self.progress = progress or (lambda stage, fraction: None)
        self.inv: Inventory = Inventory(kind=adapter.kind)
        self.store = FindingStore(self.inv)
        self.covered: set[str] = set()
        self.pending_reads: set[str] = set()  # text units read_text returned that the model has not answered on yet
        self.questioned: set[str] = set()  # strings reported as unmaskable that the program already masks: asked once
        self.failed: dict[str, int] = {}  # unit id -> how many times detection could not judge it
        self.warnings: list[str] = []
        self.log: list[dict] = []
        self.unmaskable: list[dict] = []
        self.outcome: dict = {}
        self.tool_counts: dict[str, int] = {}

    # ---- loading and saving --------------------------------------------------------------

    def _locate(self):
        return functools.partial(self.adapter.locate, self.src)

    def load(self) -> None:
        self.inv = self.adapter.parse(self.src)
        self.store = FindingStore(self.inv, self._locate())
        for t in self.inv.texts:
            if t.mirror:
                self.covered.add(t.id)  # a compatibility copy of text that is checked elsewhere
        for im in self.inv.images:
            if im.supported and not im.needs_scan:
                self.covered.add(im.id)
        for u in self.inv.unchecked:
            self.warn(f"無法檢查：{u.name}（{u.reason}）")
        for note in self.inv.notes:
            self.warn(note)

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def save(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        for name, data in (("inventory.json", to_json(self.inv)), ("findings.json", self.store.to_json()),
                           ("log.json", self.log), ("warnings.json", {"warnings": self.warnings, "unmaskable": self.unmaskable,
                                                                    "covered": sorted(self.covered), "outcome": self.outcome})):
            tmp = self.workdir / (name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.workdir / name)

    def restore(self) -> None:
        read = lambda n: json.loads((self.workdir / n).read_text(encoding="utf-8"))  # noqa: E731
        self.inv = inventory_from_json(read("inventory.json"))
        self.store = FindingStore.from_json(self.inv, read("findings.json"), self._locate())
        self.log = read("log.json")
        extra = read("warnings.json")
        self.warnings, self.unmaskable = extra["warnings"], extra["unmaskable"]
        self.covered, self.outcome = set(extra["covered"]), extra.get("outcome", {})

    # ---- images --------------------------------------------------------------------------

    def asset_path(self, image_id: str) -> Path | None:
        hits = sorted((self.workdir / "assets").glob(f"{image_id}.*"))
        return hits[0] if hits else None

    def image_data(self, image_id: str) -> bytes:
        """Bytes of a supported image in a form Pillow, the OCR engine and a browser can all read."""
        path = self.asset_path(image_id)
        if path is None:
            raw = self.adapter.image_bytes(self.src, image_id)
            source = imaging.open_image(raw)
            # A photo stored sideways is redrawn upright: boxes are drawn over the picture as displayed.
            ext = RAW_PREVIEW.get(source.format) if imaging.orientation(source) == 1 else None
            data = raw if ext else imaging.to_browser_png(raw)
            path = self.workdir / "assets" / f"{image_id}.{ext or 'png'}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return data
        return path.read_bytes()

    def write_previews(self) -> None:
        todo = [im.id for im in self.inv.images if im.supported]
        with ThreadPoolExecutor(max(1, self.settings.ocr_workers)) as pool:
            list(pool.map(self.image_data, todo))

    # ---- coverage ------------------------------------------------------------------------

    def text_ids(self) -> list[str]:
        return [t.id for t in self.inv.texts if not t.mirror]

    def image_ids(self) -> list[str]:
        return [im.id for im in self.inv.images if im.supported and im.needs_scan]

    def unchecked_units(self) -> list[str]:
        """Units still to be checked. One that detection failed on twice is given up on: it is no longer
        asked for here, and is reported by `unread_items` as not checked."""
        return [u for u in self.text_ids() + self.image_ids() if u not in self.covered and self.failed.get(u, 0) < GIVE_UP]

    def unread_items(self) -> list[dict]:
        """What ended up without a personal-data check, named so a person can check it by hand."""
        out = []
        for t in self.inv.texts:
            if not t.mirror and t.id not in self.covered:
                snippet = t.text[:30] + ("…" if len(t.text) > 30 else "")
                out.append({"name": f"{t.section}：「{snippet}」", "reason": "模型回覆無法解析，這段文字沒有經過個資偵測"})
        for im in self.inv.images:
            if im.supported and im.needs_scan and im.id not in self.covered:
                out.append({"name": im.name, "reason": "圖片文字的模型回覆無法解析，圖中的文字沒有經過個資偵測（人臉與圖片類型判斷仍有執行）"})
        return out

    def report_unread(self) -> None:
        items = self.unread_items()
        if items:
            self.warn(f"有 {len(items)} 個段落或圖片的模型回覆無法解析，沒有經過個資偵測，請人工確認：")
            for item in items[:UNREAD_LISTED]:
                self.warn(f"無法檢查：{item['name']}（{item['reason']}）")
            if len(items) > UNREAD_LISTED:
                self.warn(f"……另有 {len(items) - UNREAD_LISTED} 個，完整清單在遮蔽清單（report.json）裡")

    # ---- scanning ------------------------------------------------------------------------

    @staticmethod
    def suggestion_only(text: str) -> str:
        """Why a string is recorded switched off, or "" when it is masked like any other finding."""
        if text.strip().lower() in TITLES:
            return "職稱或稱謂本身不是個資，預設不遮"
        return ""

    def why_off(self, text: str, kind: str) -> str:
        """Why a string the model or the agent reported is recorded switched off, or "" when it is masked."""
        why = self.suggestion_only(text)
        if not why and kind in ("其他識別碼", "其他") and self._only_in_links(text) and not self._customer_number(text):
            # A document id, form code or the link itself. Customer and member numbers in a link are added by
            # rule (patterns.py); the agent, seeing those, tends to add every other id it finds in a link.
            # One the rule reads as a customer or member number stays on: in a picture the model's report comes
            # before the rule has read the picture's text, and a finding recorded off is not switched on by the rule.
            why = "只出現在網址裡的編號或代碼，預設不遮"
        return why

    def _customer_number(self, text: str) -> bool:
        """True when the rule reads the string, somewhere in the document, as a customer or member number in a link."""
        sources = [t.text for t in self.inv.texts] + [ln.text for im in self.inv.images for ln in im.ocr]
        return any(c.kind == "客戶編號" and c.text == text
                   for source in sources if text in source for c in patterns.candidates(source))

    def _only_in_links(self, text: str) -> bool:
        seen = False
        for source in [t.text for t in self.inv.texts] + [ln.text for im in self.inv.images for ln in im.ocr]:
            start = source.find(text)
            if start < 0:
                continue
            links = [(m.start(), m.end()) for m in patterns._URL.finditer(source)]
            while start >= 0:
                seen = True
                if not any(s <= start and start + len(text) <= e for s, e in links):
                    return False
                start = source.find(text, start + 1)
        return seen

    def _add_detected(self, items, source="model") -> list[dict]:
        out = []
        for text, kind in items:
            why = "短數字（可能是分機），預設不遮" if SHORT_NUMBER.fullmatch(text) else self.why_off(text, kind)
            f = self.store.add_text(text, kind, source, on=not why, reason=why)
            if f is not None:
                out.append({"id": f.id, "text": f.text, "type": f.type, "count": len(f.occurrences), "on": f.on})
        return out

    def scan_text(self, unit_ids: list[str] | None = None) -> dict:
        ids = [u for u in (unit_ids if unit_ids else self.text_ids()) if self.inv.text(u) is not None]
        lines = [self.inv.text(u).text for u in ids]
        res = detect_lines(self.llm, lines, chunk_chars=self.settings.chunk_chars, workers=self.settings.llm_concurrency,
                           recheck_chars=self.settings.recheck_chunk_chars,
                           also=[joined[0] for u in ids if (joined := tight(self.inv.text(u)))])
        found = self._add_detected(res.items)
        failed = [u for u in ids if any(piece in self.inv.text(u).text for piece in res.failed_lines)]
        for u in failed:
            self.failed[u] = self.failed.get(u, 0) + 1
        self.covered.update(u for u in ids if u not in failed)
        out = {"scanned": len(ids), "found": found, "failed_chunks": res.failed_chunks,
               "unknown_ids": [u for u in (unit_ids or []) if self.inv.text(u) is None]}
        if failed:
            out["failed_units"] = failed[:40]
            out["note"] = "failed_units 的模型回覆無法解析，還不算檢查過。請用 read_text 讀這些單元，自行判斷並用 add_findings 加入個資。"
        return out

    def ocr_image(self, image_id: str) -> None:
        im = self.inv.image(image_id)
        data = self.image_data(image_id)
        im.ocr = self.adapter.filter_ocr(self.src, image_id, self.ocr.read(data))

    def scan_images(self, image_ids: list[str] | None = None, on_progress: Callable[[int, int], None] | None = None) -> dict:
        wanted = image_ids if image_ids else self.image_ids()
        ids = [i for i in wanted if self.inv.image(i) is not None and self.inv.image(i).supported]
        done = 0

        def read(image_id):
            nonlocal done
            im = self.inv.image(image_id)
            data = self.image_data(image_id)
            im.ocr = self.adapter.filter_ocr(self.src, image_id, self.ocr.read(data))
            im.faces = self.faces.detect(data)
            done += 1
            if on_progress:
                on_progress(done, len(ids))
            return image_id

        with ThreadPoolExecutor(max(1, self.settings.ocr_workers)) as pool:
            list(pool.map(read, ids))

        def classify(job):
            image_id, area = job
            im = self.inv.image(image_id)
            data = self.image_data(image_id)
            whole = area[0] <= 0 and area[1] <= 0 and area[2] >= im.width and area[3] >= im.height
            return image_id, area, imaging.classify_image(self.llm, data if whole else imaging.crop_png(data, area))

        small = self.settings.min_vision_px
        jobs = [(i, a) for i in ids for a in self.inv.image(i).areas if a[2] - a[0] >= small and a[3] - a[1] >= small]
        with ThreadPoolExecutor(max(1, self.settings.llm_concurrency)) as pool:
            flags = list(pool.map(classify, jobs))

        # One request per image: mixed together, a name drowns in interface text from other screenshots.
        def detect(image_id):
            lines = [ln.text for ln in self.inv.image(image_id).ocr]
            return detect_lines(self.llm, lines, chunk_chars=self.settings.chunk_chars, workers=1,
                                recheck_chars=self.settings.recheck_chunk_chars)

        with_text = [i for i in ids if self.inv.image(i).ocr]
        with ThreadPoolExecutor(max(1, self.settings.llm_concurrency)) as pool:
            results = list(pool.map(detect, with_text))
        res = DetectResult(items=list(dict(item for r in results for item in r.items).items()),
                           failed_lines=[line for r in results for line in r.failed_lines])
        failed = [i for i, r in zip(with_text, results) if r.failed_lines]
        for i in failed:
            self.failed[i] = self.failed.get(i, 0) + 1
        self.store.rematch()
        found = self._add_detected(res.items)
        summary = {}
        for i in ids:
            im = self.inv.image(i)
            for box in im.faces:
                self.store.add_region(i, box, FACE_TYPE, "face", on=self.settings.face_default_on)
            if im.ocr or im.faces:
                summary[i] = {"ocr_chars": sum(len(ln.text) for ln in im.ocr), "faces": len(im.faces)}
        for i, area, labels in flags:
            for label in labels:
                self.store.add_region(i, area, label, "vision", on=False, reason="看圖判斷，建議整張遮蔽")
                summary.setdefault(i, {}).setdefault("flags", []).append(label)
        self.covered.update(i for i in ids if i not in failed)
        self.add_safety_net()
        out = {"scanned": len(ids), "found": found, "images": summary, "failed_chunks": res.failed_chunks,
               "unknown_ids": [i for i in (image_ids or []) if i not in ids]}
        if failed:
            out["failed_images"] = failed[:40]
            out["note"] = "failed_images 的圖片文字，模型回覆無法解析。請用 read_text 讀這些圖片的文字，自行判斷並用 add_findings 加入個資。"
        return out

    def cover_remaining(self) -> None:
        """Whatever has not been checked yet is checked here, by the standard flow."""
        todo = set(self.unchecked_units())
        texts = [u for u in self.text_ids() if u in todo]
        if texts:
            self.progress("偵測文字中的個資", 0.15)
            self.scan_text(texts)
        images = [i for i in self.image_ids() if i in todo]
        if images:
            self.progress("檢查圖片", 0.3)
            self.scan_images(images, on_progress=lambda done, total: self.progress(f"檢查圖片 {done}/{total}",
                                                                                 0.3 + 0.6 * done / total))

    def add_safety_net(self) -> int:
        """Checksum-verified ids, mailboxes, customer numbers in links and person-valued file properties are
        masked without asking the model."""
        added = 0
        sources = [t.text for t in self.inv.texts] + [ln.text for im in self.inv.images for ln in im.ocr]
        sources += [joined[0] for t in self.inv.texts if (joined := tight(t))]  # a number broken over two lines of a page
        for text in sources:
            for c in patterns.candidates(text):
                # A string only the agent has listed is listed again by rule: it may have withdrawn it since.
                if patterns.is_safety_net(c, text) and not any(f.kind == "text" and f.text == c.text and f.source != "agent"
                                                               for f in self.store.findings):
                    kind = {"居留證號": "其他識別碼", "信用卡號": "金融帳號", "客戶編號": "其他識別碼", "手機": "電話"}.get(c.kind, c.kind)
                    why = patterns.ALWAYS.get(c.kind, "通過檢查碼驗證")
                    if self.store.add_text(c.text, kind, "rule", reason=why) is not None:
                        added += 1
        for m in self.inv.metas:
            if m.value.strip():
                self.store.add_meta(m.id, on=m.person)
        return added

    def add_derived(self) -> int:
        """Spreadsheets: cells computed from a masked cell are listed as suggestions that are off. The owner
        asked to be shown them (a yearly total gives the monthly figure back) rather than have them masked."""
        find = getattr(self.adapter, "derived", None)
        if find is None:
            return 0
        added = 0
        for unit, start, end, origin in find(self.src, self.store.plan().text):
            text = self.inv.text(unit).text[start:end]
            known = any(f.kind == "text" and f.text == text and any(o.unit == unit and o.start == start for o in f.occurrences)
                        for f in self.store.findings)
            if known:
                continue
            why = f"由已遮蔽的儲存格（{origin}）計算而來，可能反推出原值；預設不遮"
            f = self.store.add_text(text, "其他", "rule", on=False, only=(unit, start, end), reason=why)
            if f is None:
                continue
            added += 1
            if f.reason != why:  # the same value is already a finding elsewhere: this place joins it switched off
                for o in f.occurrences:
                    if (o.unit, o.start, o.end) == (unit, start, end):
                        o.on = False
        return added

    def record(self, step: int, tool: str, args, result, started: float) -> None:
        clip = lambda v, n: (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))[:n]  # noqa: E731
        self.log.append({"step": step, "tool": tool, "args": clip(args, 200), "result": clip(result, 300),
                         "seconds": round(time.time() - started, 1)})
