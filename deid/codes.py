"""The code version of a document: plain text in which every masked string is replaced by a code such as
[姓名1], and the table that turns the codes back into the strings.

The text is what leaves the building, so it is built only from units that were checked and are shown for
review: body text, tables, headers, notes, comments and the text read from pictures. File properties and
hidden content are not part of it. The table is the personal data; it never goes into the text."""
import re
import unicodedata
from dataclasses import dataclass, field

from deid.findings import FindingStore, _merge
from deid.formats.docx import HIDDEN
from deid.formats.xmltok import compile_strings
from deid.model import ImageUnit, Inventory, OcrLine, TextUnit

NOTICE = "（說明：下文中 [姓名1]、[電話2] 這類方括號代號代替了個人資料，請原樣保留，不要翻譯、改寫或刪除。）"
# Added only when some names were said to be one person: without that the text is what it always was.
NOTICE_SAME = "（號碼相同、後面帶字母的代號，例如 [姓名1] 與 [姓名1b]，是同一個人的不同寫法。）"
LETTERS = "bcdefghijklmnopqrstuvwxyz"  # the longest writing of a person has no letter; the others take these
BLANK = "[已遮蔽]"  # covered by a face box, a hand-drawn box or a whole-picture mask: there is no string to restore
LABELS = {"其他": "個資", "其他識別碼": "識別碼"}  # what a type is called inside a code, where its own name reads badly
EN_LABELS = {
    "姓名": "NAME", "出生年月日": "DOB", "身分證字號": "NATIONAL_ID", "護照號碼": "PASSPORT",
    "其他識別碼": "ID", "電話": "PHONE", "電子郵件": "EMAIL", "地址": "ADDRESS", "帳號密碼": "CREDENTIAL",
    "金融帳號": "FIN_ACCOUNT", "財務情況": "FIN_INFO", "醫療健康": "HEALTH", "家庭婚姻": "FAMILY",
    "教育職業": "EDU_WORK", "其他": "PII", "人臉": "FACE", "證件": "ID_DOCUMENT", "簽名印章": "SIGNATURE",
    "手寫個資": "HANDWRITING", "病歷": "MED_RECORD", "檔案屬性": "METADATA",
}
EN_NOTICE = "Bracketed placeholders such as [NAME1] and [PHONE2] stand for personal data. Keep each one exactly as written."
EN_NOTICE_SAME = "Placeholders with the same name number, such as [NAME1] and [NAME1b], represent different forms of the same person's name."
EN_BLANK = "[REDACTED]"
EN_SECTIONS = {
    "內文": "Body", "頁首": "Header", "頁尾": "Footer", "註腳": "Footnotes", "章節附註": "Endnotes",
    "註解": "Comments", "圖表": "Charts", "圖形文字": "Drawing text", "表單欄位": "Form fields", "書籤": "Bookmarks",
    "活頁簿": "Workbook",
}
EN_SECTION_PATTERNS = (
    (re.compile(r"第 (\d+) 頁"), "Page {}"),
    (re.compile(r"第 (\d+) 頁圖片的文字"), "Image text on page {}"),
    (re.compile(r"圖片 (\d+) 的文字"), "Image {} text"),
    (re.compile(r"第 (\d+) 張工作表"), "worksheet {}"),
)
SHEET_NAMES = "工作表名稱"  # not lines of their own: a sheet's name is the title of its section
# Besides everything under 隱藏內容: content a reader of the document does not see.
NOT_EXPORTED = ("連結位址", SHEET_NAMES, "已定義名稱", "樞紐分析快取", "樞紐分析表更新者")
NOT_EXPORTED_PREFIX = ("內嵌檔案",)  # the workbook behind a chart, an embedded document
# A code as written, and as a cloud model may hand it back: other brackets, a backslash before a bracket,
# spaces inside, full-width digits, the letter in capitals or full width. A backslash before the opening bracket
# is taken as part of the code only when the closing bracket has one too (\[姓名1\], a Markdown escape); on its own
# it is the text's own character, as in a path: C:\Users\[帳號1]\Desktop.
CODE = re.compile(r"(?:\\(?=[\[［【〔][^\\\]］】〕]*\\[\]］】〕]))?"
                  r"[\[［【〔]\s*([^\s\d\[\]［］【】〔〕\\]{1,12}?)\s*([0-9０-９]{1,6})\s*([A-Za-zＡ-Ｚａ-ｚ]?)\s*\\?[\]］】〕]")


def canonical(label: str, number: str, letter: str = "") -> str:
    return f"[{label}{int(number)}{unicodedata.normalize('NFKC', letter or '').lower()}]"


def section_heading(section: str, lang: str) -> str:
    if lang != "en":
        return f"【{section}】"
    if section.startswith("工作表："):
        name = section.removeprefix("工作表：")
        if name.endswith("（隱藏）"):
            name = name.removesuffix("（隱藏）") + " (hidden)"
        return f"[Worksheet: {name}]"
    if section.startswith("頁首頁尾："):
        where = section_heading(section.removeprefix("頁首頁尾："), lang)[1:-1]
        return f"[Headers and footers: {where}]"
    for pattern, title in EN_SECTION_PATTERNS:
        match = pattern.fullmatch(section)
        if match:
            return f"[{title.format(match[1])}]"
    return f"[{EN_SECTIONS.get(section, section)}]"


@dataclass
class CodedText:
    text: str
    table: dict[str, str] = field(default_factory=dict)  # code -> the string it stands for
    residue: list[str] = field(default_factory=list)  # strings that should be gone and can still be read
    notes: list[str] = field(default_factory=list)  # strings left readable by the reviewer's choice
    blanked: int = 0  # places written as BLANK
    # unit -> (start, end, code): every stretch of a text unit that is a code in the text, and the same strings
    # in the second copy of a text box. What writes the codes into a file of the document's own format goes by it.
    places: dict[str, list[tuple[int, int, str]]] = field(default_factory=dict)


class _Codes:
    """Hands out the codes. The text is gone through twice: the first time only notes which strings need a
    code, in order, and which person each belongs to; `assign` then numbers them; the second time writes."""

    def __init__(self, taken: set[tuple[str, int]], lang: str):
        self.taken, self.by_text, self.table = taken, {}, {}
        self.lang = lang
        self.blank = EN_BLANK if lang == "en" else BLANK
        self.seen: dict[str, tuple[str, str]] = {}  # string -> (label, group), in the order first met
        self.lettered = False

    def code(self, original: str, kind: str, group: str = "") -> str:
        if original not in self.seen:
            label = EN_LABELS.get(kind, "PII") if self.lang == "en" else LABELS.get(kind, kind)
            self.seen[original] = (label, group)
        return self.by_text.get(original, "")

    def assign(self) -> None:
        upcoming: dict[str, int] = {}

        def number(label: str) -> int:
            n = upcoming.get(label, 1)
            while (label, n) in self.taken:  # the document already has text that reads like this code
                n += 1
            upcoming[label] = n + 1
            return n

        def give(original: str, code: str) -> None:
            self.by_text[original] = code
            self.table[code] = original

        members: dict[str, list[str]] = {}
        for original, (_label, group) in self.seen.items():
            if group:
                members.setdefault(group, []).append(original)
        for original, (label, group) in self.seen.items():
            if original in self.by_text:
                continue
            writings = members.get(group, [])
            if len(writings) < 2:
                give(original, f"[{label}{number(label)}]")
                continue
            # One number for the person. The longest writing has no letter (the first met, among equals); the
            # others take b, c, ... in the order met. What does not fit gets a number of its own when its turn comes.
            n = number(label)
            lead = max(writings, key=len)
            give(lead, f"[{label}{n}]")
            for other, letter in zip([w for w in writings if w != lead], LETTERS):
                give(other, f"[{label}{n}{letter}]")
                self.lettered = True
            members[group] = []
            if original not in self.by_text:
                give(original, f"[{label}{number(label)}]")


def _shown(t: TextUnit) -> bool:
    """What a reader of the document sees, the second copy of a text box included."""
    return not t.section.startswith((HIDDEN, *NOT_EXPORTED_PREFIX)) and t.section not in NOT_EXPORTED


def _exported(t: TextUnit) -> bool:
    return not t.mirror and _shown(t)


def _ordered(inv: Inventory):
    """Text units and pictures in reading order: a picture after the paragraph it sits in, or with its page."""
    texts = [t for t in inv.texts if _exported(t)]
    if inv.kind == "pdf":
        pages = sorted({t.page for t in texts if t.page is not None} | {i.page for i in inv.images if i.page is not None})
        for page in pages:
            yield from (t for t in texts if t.page == page)
            yield from (i for i in inv.images if i.page == page)
        yield from (t for t in texts if t.page is None)
        yield from (i for i in inv.images if i.page is None)
        return
    shown = {t.id for t in texts}
    for t in texts:
        yield t
        yield from (i for i in inv.images if i.anchor == t.id)
    yield from (i for i in inv.images if i.anchor not in shown)


def _inside(char, box) -> bool:
    x, y = (char[0] + char[2]) / 2, (char[1] + char[3]) / 2
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def _picture_boxes(store: FindingStore, image_id: str) -> list[tuple]:
    """The boxes that will be covered in one picture: (box, finding, whether the box is a near match)."""
    return [(o.box, f, o.fuzzy) for f in store.findings for o in f.occurrences
            if o.kind == "box" and o.unit == image_id and o.box is not None and store._active(f, o)]


def _picture_line(line: OcrLine, boxes, codes: _Codes) -> str:
    """One line read from a picture, with every covered character replaced: by a code, or by BLANK when the box
    is not about a string (a face, a hand-drawn area).

    Characters under boxes of strings that overlap are one stretch with one code, as in the text. A stretch one
    string covers exactly is coded as that string, an exact match before a near one. A near match (one character
    differs) and a stretch that no single string covers are coded as what the picture says there: a near match
    may be another person, so it never takes the code or the group of the name it resembles."""
    if len(line.chars) != len(line.text):  # no position per character: all or nothing
        hit = any(b[0] < line.box[2] and line.box[0] < b[2] and b[1] < line.box[3] and line.box[1] < b[3] for b, _f, _n in boxes)
        return codes.blank if hit else line.text
    named = [(b, f, near) for b, f, near in boxes if f.kind == "text" and f.text]
    covers = [{k for k, (b, _f, _n) in enumerate(named) if _inside(char, b)} for char in line.chars]
    others = [next((f for b, f, _n in boxes if not (f.kind == "text" and f.text) and _inside(char, b)), None) for char in line.chars]
    out, i = [], 0
    while i < len(covers):
        j = i + 1
        if covers[i]:
            while j < len(covers) and covers[j] & covers[j - 1]:  # boxes that overlap make one stretch
                j += 1
            out.append(_picture_code(line.text[i:j], [named[k] for k in sorted(set.intersection(*covers[i:j]))],
                                     [named[k] for k in sorted(set.union(*covers[i:j]))], codes))
        elif others[i] is None:
            while j < len(covers) and not covers[j] and others[j] is None:
                j += 1
            out.append(line.text[i:j])
        else:
            while j < len(covers) and not covers[j] and others[j] is others[i]:
                j += 1
            out.append(codes.blank)
        i = j
    return "".join(out)


def _picture_code(read: str, whole: list[tuple], over: list[tuple], codes: _Codes) -> str:
    """The code of one stretch of a picture line. `read` is what the picture says there; `whole` are the boxes
    that cover all of it, `over` every box that covers some of it."""
    exact = [f for _b, f, near in whole if not near]
    if exact:
        return codes.code(exact[0].text, exact[0].type, exact[0].group)
    lead = min((f for _b, f, _n in over), key=lambda f: (f.type == "其他", -len(f.text)))
    groups = {"" if near else f.group for _b, f, near in over}
    return codes.code(read, lead.type, groups.pop() if len(groups) == 1 else "")


def build(inv: Inventory, store: FindingStore, *, lang: str = "zh-TW") -> CodedText:
    ranges = store.masked_ranges(whole=True)
    plan = store.plan(whole=True)
    by_id = {f.id: f for f in store.findings}
    hidden = store.redundant()  # not on the reviewer's list: they name no code and say nothing about whose it is
    everything = [t.text for t in inv.texts] + [line.text for im in inv.images for line in im.ocr]
    codes = _Codes({(m.group(1), int(m.group(2))) for text in everything for m in CODE.finditer(text)}, lang)
    pictures = {im.id: n for n, im in enumerate(inv.images, 1)}

    def person(text: str, covering: list) -> str:
        """The group a stretch belongs to: that of the member it is, or the one group all the names covering
        it are in. A stretch that takes in anything else stands for no one person."""
        for f in covering:
            if f.group and f.text == text:
                return f.group
        groups = {f.group for f in covering}
        return groups.pop() if len(groups) == 1 else ""

    def coded(unit: TextUnit) -> str:
        rows = ranges.get(("text", unit.id), [])
        breaks = set(unit.breaks)  # spaces the reader put in at line ends: not part of a string broken there
        said = lambda s, e: "".join(c for i, c in enumerate(unit.text[s:e], s) if i not in breaks)  # noqa: E731
        out, pos = [], 0
        for s, e in _merge([(s, e) for _fid, s, e in rows]):
            # One code for the whole stretch. Its label comes from the finding that says most about it: a
            # specific type before 其他, then the longest.
            over = [r for r in rows if r[1] < e and s < r[2]]
            over = [r for r in over if r[0] not in hidden] or over
            lead = min(over, key=lambda r: (by_id[r[0]].type == "其他", r[1] - r[2], r[1]))
            out.append(unit.text[pos:s])
            out.append(codes.code(said(s, e), by_id[lead[0]].type, person(said(s, e), [by_id[r[0]] for r in over])))
            pos = e
        out.append(unit.text[pos:])
        return "".join(out)

    def write() -> list[tuple[str, str]]:
        """The lines of the text, each with the picture it was read from ("" for the document's own text)."""
        lines: list[tuple[str, str]] = []
        section = None
        # A sheet's name is shown as the title of its section, so a masked name is coded there too.
        renamed = {t.text: coded(t) for t in inv.texts if t.section == SHEET_NAMES and not t.mirror}
        renamed = {old: new for old, new in renamed.items() if old != new}

        def head(title: str) -> None:
            nonlocal section
            if title != section:
                if lines:
                    lines.append(("", ""))
                shown = title
                for old in sorted(renamed, key=len, reverse=True):
                    if shown.startswith("工作表：" + old):
                        shown = "工作表：" + renamed[old] + shown[len("工作表：" + old):]
                        break
                lines.append((section_heading(shown, lang), ""))
                section = title

        for item in _ordered(inv):
            if isinstance(item, ImageUnit):
                boxes = _picture_boxes(store, item.id)
                read = [_picture_line(line, boxes, codes) for line in item.ocr]
                read = [ln for n, ln in enumerate(read) if not (ln == codes.blank and n and read[n - 1] == codes.blank)]
                if read:
                    head(f"第 {item.page + 1} 頁圖片的文字" if inv.kind == "pdf" and item.page is not None else f"圖片 {pictures[item.id]} 的文字")
                    lines.extend((ln, item.id) for ln in read)
                continue
            line = coded(item)
            head(item.section)
            lines.append((line, ""))
        return lines

    write()  # first time through: which strings need a code, in reading order
    codes.assign()
    written = write()
    body = "\n".join(line for line, _picture in written)

    seen_as: dict[frozenset, str] = {}

    def plain(but: frozenset = frozenset()) -> str:
        """The text as a search sees it, without the lines read from the pictures in `but`."""
        if but not in seen_as:
            text = "\n".join("" if picture in but else line for line, picture in written)
            for code in codes.table:
                text = text.replace(code, "\0")  # a label inside a code is not the document's text
            seen_as[but] = text
        return seen_as[but]

    switched_off: dict[str, set[str]] = {}  # string -> the pictures in which the reviewer switched a match of it off
    for f in store.findings:
        if f.kind == "text" and f.on:
            for o in f.occurrences:
                if o.kind == "box" and not store._active(f, o):
                    switched_off.setdefault(f.text, set()).add(o.unit)

    def left_readable(string: str) -> frozenset:
        return frozenset(switched_off.get(string, ()))

    def still_there(strings: list[str], but=lambda s: frozenset()) -> list[str]:
        return [f"代號版文字: 「{s}」" for s in strings if s and compile_strings([s]).search(plain(but(s)))]

    # A string that must be gone and is still read in a picture where the reviewer switched its match off is
    # there by the reviewer's choice: it is reported as left readable, like a place switched off in the text.
    residue = still_there(plan.strings, left_readable)
    notes = still_there(store.partial_strings())
    notes += [line for line in still_there(plan.strings) if line not in residue and line not in notes]

    places: dict[str, list[tuple[int, int, str]]] = {}
    for unit in inv.texts:
        if not _shown(unit):
            continue
        for s, e in _merge([(s, e) for _fid, s, e in ranges.get(("text", unit.id), [])]):
            code = codes.by_text.get(unit.text[s:e])  # a copy gets no code of its own: only one the text has
            if code:
                places.setdefault(unit.id, []).append((s, e, code))

    notice, same = (EN_NOTICE, EN_NOTICE_SAME) if lang == "en" else (NOTICE, NOTICE_SAME)
    if codes.lettered:
        notice += "\n" + same
    return CodedText(text=notice + "\n\n" + body + "\n", table=dict(codes.table), residue=residue,
                     notes=notes, blanked=body.count(codes.blank), places=places)
