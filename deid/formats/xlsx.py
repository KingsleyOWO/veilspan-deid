"""Excel (.xlsx / .xlsm). The package layout is the same family as Word, so integrity checks, file properties,
hidden places, the generic replacement, the residue scan and verification are inherited; this module adds
what is specific to spreadsheets: cells, shared strings, formulas, sheet names, comments.

One row is one unit ("A | B | C") so a label stays next to its value. Text cells are masked character by
character. A number, date or formula cell cannot be masked partly: it becomes a text cell holding mask
characters, and a masked formula is replaced by that static value so it cannot recompute the original.

The package is first brought to one spelling (_canonical): UTF-8, no CDATA, the main namespace unprefixed.
Everything after that - reading, writing, checking - sees only that spelling, so a workbook written by a
library with other habits is either read in full or refused, never passed through unread."""
import html
import io
import re
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

from deid import imaging
from deid.config import Settings
from deid.formats.base import BrokenFile
from deid.formats.docx import (HIDDEN, NESTED_OOXML, DocxAdapter, _Doc, _hidden_in_part, _Meta, _natural, _rels, _Unit,
                               _wellformed, parse_part)
from deid.formats import numfmt, xlsx_refs
from deid.formats.xmltok import attr_values, compile_strings, iter_tokens, mask_chars, searcher, splice
from deid.model import ImageUnit, Inventory, MaskPlan, MetaUnit, TextUnit, Unchecked

SHARED = "xl/sharedStrings.xml"
BUILTIN_DATE_FORMATS = set(range(14, 23)) | set(range(27, 37)) | {45, 46, 47} | set(range(50, 59))
HEADER_FOOTER = {"oddHeader", "oddFooter", "evenHeader", "evenFooter", "firstHeader", "firstFooter"}
# Format codes of a header or footer, after "&": a section starts (L C R), a field is printed (page, pages, date,
# time, path, file, sheet, picture), a style is switched (bold ... subscript, outline, shadow, page arithmetic).
# Besides: && (one "&"), &"font,style", &nn (font size), &K and a colour.
HF_SECTIONS, HF_FIELDS, HF_SWITCHES = "LCR", "PNDTZFAG", "BIUESXYOH+-"
HF_COLOUR = re.compile(r"K(?:[0-9A-Fa-f]{6}|[0-9]{2}[+\-][0-9]{2,3})")
HF_SIZE = re.compile(r"[0-9]+")
HF_UNCLEAR = "頁首頁尾裡的字型大小代碼後面緊接著數字，無法確定哪些數字會印出來，這部分未完整檢查"
PIVOT_CAP = 500
SHEET_NAME_FORBIDDEN = re.compile(r"[\[\]:*?/\\]")
NEUTRAL_AUTHOR = "－"
MAX_XML_PART = 100 * 1024 * 1024  # one XML part, unpacked
MAX_ENTRY_BYTES = 300 * 1024 * 1024  # any other entry, unpacked
MAX_TOTAL_BYTES = 1024 * 1024 * 1024  # the whole package, unpacked
MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
# Namespaces whose elements this module names without a prefix.
DEFAULT_NS = {MAIN_NS, "http://purl.oclc.org/ooxml/spreadsheetml/main",
              "http://schemas.openxmlformats.org/package/2006/relationships",
              "http://schemas.openxmlformats.org/package/2006/content-types",
              "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties",
              "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties",
              "http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments"}
# Namespaces whose elements this module names with one fixed prefix.
CONVENTIONAL = {"http://schemas.openxmlformats.org/drawingml/2006/main": "a",
                "http://schemas.openxmlformats.org/drawingml/2006/chart": "c",
                "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing": "xdr",
                "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes": "vt",
                "http://purl.org/dc/elements/1.1/": "dc",
                "http://schemas.openxmlformats.org/package/2006/metadata/core-properties": "cp",
                "http://schemas.openxmlformats.org/officeDocument/2006/relationships": "r",
                "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main": "x14",
                "http://schemas.microsoft.com/office/excel/2006/main": "xm"}
ODD_SPELLING = "使用非預期的 XML 寫法，內容無法檢查"
# xm:f: a formula kept in an extension list (Excel 2010 conditional formatting, data validation, sparklines)
FORMULA_ELEMS = {"f", "formula", "formula1", "formula2", "definedName", "c:f", "xm:f", "calculatedColumnFormula", "totalsRowFormula"}
# Elements whose text is a position or a format, never content: a masked number may equal one of them.
POSITION_ELEMS = {"xdr:col", "xdr:row", "xdr:colOff", "xdr:rowOff", "c:formatCode", "x:Anchor", "x:Row", "x:Column", "xm:sqref"}
# element -> attributes holding text a person typed. Read as hidden units, masked and checked like text.
CONTENT_ATTRS = {
    "definedName": {"comment", "description", "help", "statusBar", "customMenu"}, "fileSharing": {"userName"},
    "customWorkbookView": {"name", "userName"}, "filter": {"val"}, "customFilter": {"val"}, "cfRule": {"text"},
    "dataValidation": {"prompt", "promptTitle", "error", "errorTitle"}, "scenario": {"name", "user", "comment"},
    "inputCells": {"val"}, "tableColumn": {"name", "totalsRowLabel"}, "person": {"displayName", "userId"},
    "hyperlink": {"display", "tooltip"}, "a:hlinkClick": {"tooltip"}, "a:hlinkHover": {"tooltip"}, "cacheField": {"name", "caption"}, "dataField": {"name"},
    "pivotField": {"name", "subtotalCaption"},
    "pivotTable": {"name"}, "property": {"name"},
    "pivotTableDefinition": {"name", "dataCaption", "grandTotalCaption", "errorCaption", "missingCaption", "rowHeaderCaption",
                             "colHeaderCaption"},
    "cellStyle": {"name"}, "sortCondition": {"customList"}, "x14:cfRule": {"text"},
    "x14:dataValidation": {"prompt", "promptTitle", "error", "errorTitle"}}
BUILTIN_STYLE = re.compile(r"\bbuiltinId\s*=")  # a built-in cell style: its name is the program's, in the program's language
PIVOT_ITEM = {"v"}  # <s v="…"/> in a pivot cache: one text item
# Entries that are read, or hold no text of the workbook's content. Anything else is listed as not checked.
KNOWN = re.compile(r"\[content_types\]\.xml|.*\.rels|xl/workbook\.xml|xl/sharedstrings\.xml|xl/styles\.xml|xl/theme/[^/]+|"
                   r"xl/calcchain\.xml|xl/metadata\.xml|xl/chartsheets/[^/]+\.xml|xl/comments\d*\.xml|xl/threadedcomments/[^/]+\.xml|"
                   r"xl/persons/[^/]+\.xml|xl/drawings/drawing\d*\.xml|xl/drawings/vmldrawing\d*\.vml|xl/charts/chart\d+\.xml|"
                   r"xl/charts/(?:style|colors)\d+\.xml|xl/pivotcache/[^/]+\.xml|xl/pivottables/[^/]+\.xml|xl/tables/[^/]+\.xml|"
                   r"xl/media/.+|xl/printersettings/.+|xl/revisions/.+|xl/embeddings/.+|docprops/.+|customxml/.+")
DEFAULT_TABLE_NAME = re.compile(r"(?:Table|表格|資料表|Tabelle)_?\d+")
COLUMN_NAMES = "表格欄名"
ENTITY = re.compile(r"&(?:#x?[0-9A-Fa-f]+|\w+);")
LITERAL = re.compile(r'"((?:[^"]|"")*)"')
CDATA = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
XMLNS = re.compile(r"""\bxmlns(?::([\w.\-]+))?\s*=\s*["']([^"']*)["']""")
# A sheet named in a reference, in raw XML (an apostrophe may be written &apos;): 'quoted', with an apostrophe
# inside written twice, or bare; either may name two sheets, the first and last of a 3-D reference. A bare name
# after [n] is a sheet of another workbook.
_BARE = r"[^\s!'\"()+\-*/^&=<>,;:\[\]{}%@#]+"
SHEET_REF = re.compile(rf"(?P<q>'|&apos;)(?P<body>(?:(?!(?P=q))[^<>]|(?P=q)(?P=q))+)(?P=q)!"
                       rf"|(?<![\w'.!\]])(?P<book>\[\d+\])?(?P<a>{_BARE})(?::(?P<b>{_BARE}))?!")
CELL_STYLE = re.compile(r"<cellStyle\b[^>]*>")


def _num(text: str | None) -> int | None:
    return int(text) if text and re.fullmatch(r"[0-9]+", text) else None


def _is_xml(name: str) -> bool:
    return name.lower().endswith((".xml", ".rels", ".vml"))


def _plain(raw: str) -> tuple[str, list[int]]:
    """A raw text node unescaped, and for each character of the result (and its end) the offset in the raw text."""
    out, index, pos = [], [], 0
    for m in ENTITY.finditer(raw):
        out.append(raw[pos:m.start()])
        index.extend(range(pos, m.start()))
        ch = html.unescape(m.group(0))
        if len(ch) == 1:
            out.append(ch)
            index.append(m.start())
        else:  # not an entity after all: kept as written
            out.append(m.group(0))
            index.extend(range(m.start(), m.end()))
        pos = m.end()
    out.append(raw[pos:])
    index.extend(range(pos, len(raw) + 1))
    return "".join(out), index


def _literals(raw: str):
    """The string literals of a formula held in a raw text node: (text, raw start, raw end), quotes excluded."""
    plain, index = _plain(raw)
    for m in LITERAL.finditer(plain):
        yield m.group(1), index[m.start(1)], index[m.end(1)]


def _outside_literals(plain: str, change) -> str:
    """The formula with `change` applied to everything that is not inside a string literal."""
    out, pos = [], 0
    for m in LITERAL.finditer(plain):
        out.append(change(plain[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(change(plain[pos:]))
    return "".join(out)


def _header(plain: str) -> tuple[str, list, str]:
    """A header or footer (unescaped) as it is printed, so that a word is not read as part of the format code
    before it (&CJohn is John in the centre). Returns the text, with the codes taken out, a field (page number,
    date ...) as one space and " | " between sections; for each of its characters the span of `plain` it stands
    for (None: a space or separator nothing is written for); and for each character of `plain` what it is: t
    (text), c (part of a format code) or x (part of a font size too long to be one: what is printed is unknown).
    An "&" that starts no known code is taken out and what follows it is read as text."""
    kinds = ["t"] * len(plain)
    sections: list[list] = [[]]
    i, n = 0, len(plain)
    while i < n:
        if plain[i] != "&":
            sections[-1].append((plain[i], (i, i + 1)))
            i += 1
            continue
        nxt, end, kind = plain[i + 1:i + 2], i + 1, "c"
        if nxt == "&":
            sections[-1].append(("&", (i, i + 2)))
            i += 2
            continue
        if nxt and nxt in HF_SECTIONS:
            sections.append([])
            end += 1
        elif nxt and nxt in HF_FIELDS:
            sections[-1].append((" ", None))
            end += 1
        elif nxt and nxt in HF_SWITCHES:
            end += 1
        elif nxt == '"' and plain.find('"', i + 2) >= 0:
            end = plain.find('"', i + 2) + 1
        elif (m := HF_SIZE.match(plain, i + 1)) or (m := HF_COLOUR.match(plain, i + 1)):
            end = m.end()
            if nxt.isdigit() and end - i > 4:  # the program reads every digit as the size; no size has four
                kind = "x"
        kinds[i:end] = kind * (end - i)
        i = end
    text, spans = "", []
    for part in sections:
        while part and part[0][0].isspace():
            part.pop(0)
        while part and part[-1][0].isspace():
            part.pop()
        if part:
            if text:
                text += " | "
                spans += [None] * 3
            text += "".join(c for c, _span in part)
            spans += [span for _c, span in part]
    return text, spans, "".join(kinds)


def _header_writes(raw: str, offset: int, changes) -> list[tuple[int, int, str]]:
    """Replacements that write `changes` - (index into the printed text, new character) - into a header or
    footer held in the raw text `raw`, which starts at `offset` of its part. Only characters of the text are
    rewritten; the format codes stay as they were written."""
    plain, index = _plain(raw)
    _text, spans, _kinds = _header(plain)
    out = []
    for i, ch in changes:
        span = spans[i] if 0 <= i < len(spans) else None
        if span is not None:
            out.append((offset + index[span[0]], offset + index[span[1]], escape(ch)))
    return out


def _header_hits(raw: str, pattern, strings: list[str]) -> set[str]:
    """The strings still in a header or footer: in the printed text, and anywhere a string runs over a format
    code or into a font size that is too long, where the printed text cannot show it."""
    plain = _plain(raw)[0]
    text, _spans, kinds = _header(plain)
    found = set(pattern.findall(text))
    for s in strings:
        at = plain.find(s)
        while at >= 0:
            seen = set(kinds[at:at + len(s)])
            if "x" in seen or len(seen) > 1:
                found.add(s)
                break
            at = plain.find(s, at + 1)
    return found


def _decode(data: bytes) -> str | None:
    """An XML part as text with its declaration saying UTF-8, whatever encoding it was stored in."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        enc = "utf-16"
    elif data[:4] == b"<\x00?\x00":
        enc = "utf-16-le"
    elif data[:4] == b"\x00<\x00?":
        enc = "utf-16-be"
    else:
        m = re.match(rb"""\s*(?:\xef\xbb\xbf)?<\?xml[^>]*?encoding\s*=\s*["']([A-Za-z0-9._\-]+)["']""", data[:300])
        enc = m.group(1).decode("ascii") if m else "utf-8"
        if enc.lower().replace("-", "") in ("utf8", "usascii", "ascii"):
            enc = "utf-8-sig"
    try:
        text = data.decode(enc)
    except (UnicodeDecodeError, LookupError):
        return None
    return re.sub(r"""^(\s*<\?xml[^>]*?encoding\s*=\s*["'])[^"']+""", r"\1UTF-8", text, count=1)


def _unprefix(text: str) -> tuple[str, str | None]:
    """Drop the prefix some writers bind to the part's own namespace (<x:worksheet>), so elements carry the
    names this module looks for. Returns the text and, if the part is spelled in a way that cannot be brought
    into line, the reason it has to be listed as not checked."""
    note = None
    root = re.search(r"<(?![?!])[^>]*>", text)
    if root is None:
        return text, None
    declared = {m.group(1) or "": m.group(2) for m in XMLNS.finditer(root.group(0))}
    prefix = re.match(r"<([A-Za-z_][\w.\-]*):", root.group(0))
    if prefix and declared.get(prefix.group(1)) in DEFAULT_NS:
        p, ns = prefix.group(1), declared[prefix.group(1)]
        if "" in declared or re.search(r"<(?![?!/])[A-Za-z_][\w.\-]*[\s/>]", text):
            note = ODD_SPELLING  # unprefixed elements of another namespace would be mistaken for this one
        else:
            text = text.replace(f"<{p}:", "<").replace(f"</{p}:", "</")
            text = re.sub(rf"""\bxmlns:{re.escape(p)}\s*=\s*(["']){re.escape(ns)}\1""", f'xmlns="{ns}" xmlns:{p}="{ns}"', text, count=1)
    for m in XMLNS.finditer(text):
        if m.group(1) and CONVENTIONAL.get(m.group(2), m.group(1)) != m.group(1):
            note = ODD_SPELLING
    return text, note


def _canonical(z: zipfile.ZipFile) -> tuple[bytes | None, list[tuple[str, str]]]:
    """The package in the one spelling the rest of this module reads, as bytes (None when it already is), and
    the parts that could not be brought into line. Refuses packages that are oversized, malformed or ambiguous."""
    infos = z.infolist()
    if len({i.filename for i in infos}) != len(infos):
        raise BrokenFile("檔案結構異常：內含重複的項目，無法處理")
    total = 0
    for i in infos:
        total += i.file_size
        if i.file_size > (MAX_XML_PART if _is_xml(i.filename) else MAX_ENTRY_BYTES) or total > MAX_TOTAL_BYTES:
            raise BrokenFile("活頁簿解壓縮後過大，無法處理")
    changed: dict[str, bytes] = {}
    notes: list[tuple[str, str]] = []
    for i in infos:
        if i.filename.lower().endswith(NESTED_OOXML):  # an embedded Office file is unpacked as well
            try:
                with zipfile.ZipFile(io.BytesIO(z.read(i))) as inner:
                    sizes = [n.file_size for n in inner.infolist()]
            except (zipfile.BadZipFile, zlib.error, OSError, RuntimeError, EOFError):
                sizes = []
            total += sum(sizes)
            if total > MAX_TOTAL_BYTES or any(size > MAX_XML_PART for size in sizes):
                raise BrokenFile("活頁簿解壓縮後過大，無法處理")
        if not _is_xml(i.filename):
            continue
        try:
            data = z.read(i)
        except (zipfile.BadZipFile, zlib.error, OSError, RuntimeError, EOFError):
            raise BrokenFile("檔案已損壞，無法讀取") from None
        text = _decode(data)
        if text is None:
            raise BrokenFile("檔案已損壞：活頁簿內部的 XML 編碼無法讀取")
        if not _wellformed(text.encode("utf-8")):  # also what keeps the tokenizer away from unterminated comments
            raise BrokenFile("檔案已損壞：活頁簿內部的 XML 格式錯誤")
        new = CDATA.sub(lambda m: escape(m.group(1)), text) if "<![CDATA[" in text else text
        new, note = _unprefix(new)
        if note:
            notes.append((i.filename, note))
        encoded = new.encode("utf-8")
        if encoded != data:
            changed[i.filename] = encoded
    if not changed:
        return None, notes
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        for i in infos:
            info = zipfile.ZipInfo(i.filename, date_time=i.date_time)
            info.compress_type, info.external_attr, info.create_system = i.compress_type, i.external_attr, i.create_system
            out.writestr(info, changed[i.filename] if i.filename in changed else z.read(i))
    return buf.getvalue(), notes


@dataclass
class _Cell:
    offset: int  # where this cell's text starts inside the row unit's text
    text: str
    start: int  # raw span of the whole <c>…</c> element in the sheet XML
    end: int
    ref: str
    style: str | None
    kind: str  # s (shared string) | is (inline string) | v (number, date, formula result)
    si: int = -1
    pieces: list = field(default_factory=list)  # inline string: (start, end, text) of each text node
    formula: bool = False
    shared_master: bool = False
    partial: bool = False  # a text result (string formula): can be masked character by character once made static
    group: str | None = None  # the first cell of a filled-down formula: the id the other cells refer to
    rph: list = field(default_factory=list)  # inline string: (start, end) of its reading guides
    ftext: str = ""  # the formula as written; empty for a cell that repeats a filled-down formula
    fkind: str = ""  # shared | array | "" (an ordinary formula, or none)
    fsi: str | None = None  # which filled-down formula a shared cell belongs to
    farea: str = ""  # shared master or array formula: the cells it covers


@dataclass
class _SI:
    text: str
    pieces: list  # (start, end, text) of the visible text nodes
    phonetic: list  # (start, end) of phonetic-guide text nodes: they spell out the reading of the string


def _is_date_format(code: str) -> bool:
    code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", code)
    return "general" not in code.lower() and re.search(r"[ymdhs]", code, re.I) is not None


def _date_text(serial: float, date1904: bool) -> str | None:
    try:
        base = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30)
        d = base + timedelta(days=serial)
    except (OverflowError, ValueError):
        return None
    clock = f"{d.hour}:{d.minute:02d}"
    if serial < 1:
        return clock
    day = f"{d.year}/{d.month}/{d.day}"
    return day if abs(serial - round(serial)) < 1e-9 else f"{day} {clock}"


class XlsxAdapter(DocxAdapter):
    kind = "xlsx"
    main_part = "xl/workbook.xml"
    not_valid = "檔案不是有效的 Excel 活頁簿（.xlsx）"
    legacy = "檔案有密碼保護，或是舊版 .xls 格式；請移除密碼或另存為 .xlsx 後再上傳"
    # Formulas are not here: a string found inside one is masked only within its text literals (_extras), so a
    # masked number can never rewrite a cell reference. Nor are headers and footers: they are searched in their
    # printed text, so that a format code is neither taken for part of a word nor masked (_generic).
    generic_elements = {"t", "a:t", "text", "author", "vt:lpstr", "vt:lpwstr", "dc:creator", "dc:title",
                        "dc:subject", "dc:description", "cp:lastModifiedBy", "cp:keywords", "cp:category", "Manager",
                        "Company"}
    generic_attrs = {"descr", "title", "display", "tooltip", "alt", "o:title"}
    residue_attrs = generic_attrs

    def __init__(self):
        super().__init__()
        self._canon: dict = {}  # (path, mtime, size) -> (canonical package bytes or None, notes)

    def _open(self, src: Path) -> zipfile.ZipFile:
        z = super()._open(src)
        try:
            st = Path(src).stat()
            key = (str(src), st.st_mtime_ns, st.st_size)
            if key not in self._canon:
                if len(self._canon) >= 2:
                    self._canon.clear()
                self._canon[key] = _canonical(z)
            data, notes = self._canon[key]
        except BaseException:
            z.close()
            raise
        self._notes = notes
        if data is None:
            return z
        z.close()
        return zipfile.ZipFile(io.BytesIO(data))

    def _joined_texts(self, name: str, xml: str):
        if name == SHARED:
            return (item.text for item in self._shared_strings(xml))
        if re.fullmatch(r"xl/comments\d*\.xml", name):
            return (p.text for p in parse_part(xml, {"comment"}, {"t": "seg"}))
        return ()

    # ---- reading -------------------------------------------------------------------------

    @staticmethod
    def _shared_strings(xml: str) -> list[_SI]:
        items: list[_SI] = []
        cur = None
        in_t = in_rph = False
        for kind, name, s, e in iter_tokens(xml):
            if kind == "open":
                if name == "si":
                    cur = _SI("", [], [])
                elif name == "rPh":
                    in_rph = True
                elif name == "t":
                    in_t = True
            elif kind == "close":
                if name == "si" and cur is not None:
                    cur.text = "".join(p[2] for p in cur.pieces)
                    items.append(cur)
                    cur = None
                elif name == "rPh":
                    in_rph = False
                elif name == "t":
                    in_t = False
            elif kind == "empty" and name == "si":
                items.append(_SI("", [], []))
            elif kind == "text" and in_t and cur is not None:
                if in_rph:
                    cur.phonetic.append((s, e))
                else:
                    cur.pieces.append((s, e, html.unescape(xml[s:e])))
        return items

    @staticmethod
    def _date_styles(xml: str) -> set[int]:
        """Indexes of cell formats that show a number as a date or time."""
        custom = {}
        for m in re.finditer(r"<numFmt\b[^>]*>", xml):
            a = {n: v for n, v, _s, _e in attr_values(m.group(0))}
            if _num(a.get("numFmtId")) is not None:
                custom[int(a["numFmtId"])] = a.get("formatCode", "")
        out = set()
        block = re.search(r"<cellXfs\b.*?</cellXfs>", xml, re.S)
        for i, m in enumerate(re.finditer(r"<xf\b[^>]*>", block.group(0)) if block else []):
            a = {n: v for n, v, _s, _e in attr_values(m.group(0))}
            fmt = _num(a.get("numFmtId")) or 0
            if fmt in BUILTIN_DATE_FORMATS or (fmt in custom and _is_date_format(custom[fmt])):
                out.add(i)
        return out

    @staticmethod
    def _shown_formats(xml: str) -> dict[int, str]:
        """Index of a cell format -> its number format as written, for the formats the workbook defines itself.
        A number under one of these may be on the screen in another spelling than it is stored in (numfmt)."""
        custom = {}
        for m in re.finditer(r"<numFmt\b[^>]*>", xml):
            a = {n: v for n, v, _s, _e in attr_values(m.group(0))}
            if _num(a.get("numFmtId")) is not None:
                custom[int(a["numFmtId"])] = a.get("formatCode", "")
        out = {}
        block = re.search(r"<cellXfs\b.*?</cellXfs>", xml, re.S)
        for i, m in enumerate(re.finditer(r"<xf\b[^>]*>", block.group(0)) if block else []):
            a = {n: v for n, v, _s, _e in attr_values(m.group(0))}
            fmt = _num(a.get("numFmtId")) or 0
            if fmt in custom:
                out[i] = custom[fmt]
        return out

    @staticmethod
    def _rows(xml: str, shared: list[_SI], date_styles: set[int], date1904: bool, info: dict, formats: dict[int, str] | None = None):
        """Yields one list of _Cell per row that has any text. `info` collects what concerns the whole sheet:
        bad_index (a cell points past the string table) and groups (filled-down formula id -> the spans of
        the <f> elements of the cells that only refer to it)."""
        row: list[_Cell] = []
        cell = None  # dict while inside <c>
        inside = None  # v | f | t
        in_is = in_rph = False
        f_open = rph_open = None
        for kind, name, s, e in iter_tokens(xml):
            if kind == "text":
                if cell is None or inside is None:
                    continue
                if inside == "v":
                    cell["v"] += html.unescape(xml[s:e])
                elif inside == "f":
                    cell["ftext"] += html.unescape(xml[s:e])
                elif inside == "t" and in_is and not in_rph:
                    cell["pieces"].append((s, e, html.unescape(xml[s:e])))
                continue
            if kind == "other":
                continue
            if name == "row":
                if kind == "close":
                    if row:
                        yield row
                    row = []
            elif name == "c":
                if kind == "open":
                    a = {n: v for n, v, _s, _e in attr_values(xml[s:e])}
                    cell = {"start": s, "ref": a.get("r", ""), "style": a.get("s"), "type": a.get("t", "n"), "v": "",
                            "pieces": [], "formula": False, "master": False, "group": None, "rph": [], "ftext": "", "fkind": "", "fsi": None,
                            "farea": ""}
                    in_is = in_rph = False
                elif kind == "close" and cell is not None:
                    done = XlsxAdapter._finish_cell(cell, e, shared, date_styles, date1904, info, formats)
                    if done is not None:
                        row.append(done)
                    cell = None
            elif cell is not None:
                if name == "f":
                    cell["formula"] = True
                    if kind in ("open", "empty"):
                        a = {n: v for n, v, _s, _e in attr_values(xml[s:e])}
                        follower = a.get("si") if a.get("t") == "shared" and "ref" not in a else None
                        cell["fkind"], cell["fsi"], cell["farea"] = a.get("t", ""), a.get("si"), a.get("ref", "")
                        if a.get("t") == "shared" and "ref" in a:
                            cell["master"], cell["group"] = True, a.get("si")
                        elif a.get("t") == "array":
                            cell["master"] = True
                        if kind == "empty" and follower is not None:
                            info["groups"].setdefault(follower, []).append((s, e))
                        f_open = (s, follower) if kind == "open" else None
                    elif f_open is not None:
                        if f_open[1] is not None:
                            info["groups"].setdefault(f_open[1], []).append((f_open[0], e))
                        f_open = None
                    inside = "f" if kind == "open" else None
                elif name == "v":
                    inside = "v" if kind == "open" else None
                elif name == "is":
                    in_is = kind == "open"
                elif name == "rPh":
                    in_rph = kind == "open"
                    if kind == "open":
                        rph_open = s
                    elif kind == "close" and rph_open is not None and in_is:
                        cell["rph"].append((rph_open, e))
                elif name == "t":
                    inside = "t" if kind == "open" else None
        if row:
            yield row

    @staticmethod
    def _finish_cell(c: dict, end: int, shared: list[_SI], date_styles: set[int], date1904: bool, info: dict,
                     formats: dict[int, str] | None = None) -> _Cell | None:
        kind, text, si = "v", c["v"], -1
        t = c["type"]
        if t == "s":
            index = _num(c["v"].strip())
            if index is None or index >= len(shared):
                info["bad_index"] = True  # the text of this cell cannot be known: the sheet is reported
                return None
            si = index
            kind, text = "s", shared[si].text
        elif t == "inlineStr":
            kind, text = "is", "".join(p[2] for p in c["pieces"])
        elif t in ("b", "e"):
            return None
        elif t == "n" and _num(c["style"]) in date_styles:
            try:
                text = _date_text(float(c["v"]), date1904) or c["v"]
            except ValueError:
                pass
        elif t == "n" and formats and _num(c["style"]) in formats:
            # An identifier kept as a number - a phone under 0000000000 - is on the screen as the format writes
            # it. That is the string to find; the cell is masked whole like any number.
            text = numfmt.shown(c["v"], formats[_num(c["style"])]) or c["v"]
        if not text.strip():
            return None
        return _Cell(0, text, c["start"], end, c["ref"], c["style"], kind, si, c["pieces"] if kind == "is" else [],
                     c["formula"], c["master"], t == "str", c["group"], c["rph"] if kind == "is" else [],
                     c["ftext"], c["fkind"] if c["fkind"] in ("shared", "array") else "", c["fsi"], c["farea"])

    def _parse(self, src: Path) -> _Doc:
        with self._open(src) as z:
            notes = list(getattr(self, "_notes", []))
            self._check_integrity(z)
            names = z.namelist()
            by_lower = {n.lower(): n for n in names}
            inv = Inventory(kind="xlsx")
            paras: dict = {}
            units: dict[str, _Unit] = {}
            workbook = self._xml(z, self.main_part)
            if workbook is None or not _wellformed(workbook.encode("utf-8")):
                raise BrokenFile("檔案已損壞：活頁簿內部的 XML 格式錯誤")
            rels = _rels(self._xml(z, "xl/_rels/workbook.xml.rels") or "", "xl") if "xl/_rels/workbook.xml.rels" in names else {}
            date1904 = re.search(r'date1904\s*=\s*"(1|true)"', workbook) is not None
            shared_xml = self._xml(z, SHARED) if SHARED in names else None
            shared = self._shared_strings(shared_xml) if shared_xml else []
            styles = self._xml(z, "xl/styles.xml") if "xl/styles.xml" in names else None
            date_styles = self._date_styles(styles) if styles else set()
            formats = self._shown_formats(styles) if styles else {}
            si_refs: dict[int, int] = {}

            def add_unit(text: str, section: str, part: str) -> _Unit:
                uid = f"t{len(units) + 1}"
                unit = _Unit(uid, part, section, [], text, False)
                units[uid] = unit
                inv.texts.append(TextUnit(id=uid, text=text, section=section))
                return unit

            sheets = []
            for m in re.finditer(r"<sheet\b[^>]*>", workbook):
                a = {n: (v, m.start() + vs, m.start() + ve) for n, v, vs, ve in attr_values(m.group(0))}
                if "name" in a:
                    entry = rels.get(a.get("r:id", ("",))[0])
                    if entry and entry not in names:  # part names are compared without regard to case
                        entry = by_lower.get(entry.lower())
                    sheets.append((a["name"], entry, a.get("state", ("",))[0] in ("hidden", "veryHidden")))
            if not sheets:
                raise BrokenFile(self.not_valid + "：找不到任何工作表")
            sheet_xml: dict[str, str] = {}
            groups: dict[str, dict] = {}
            complete = not any(n.lower().startswith(("xl/macrosheets/", "xl/dialogsheets/")) for n in names)
            for (name, _ns, _ne), entry, is_hidden in sheets:
                xml = self._xml(z, entry) if entry in names else None
                if xml is None:
                    inv.unchecked.append(Unchecked(f"工作表：{name}", "找不到這張工作表的內容，未檢查"))
                    complete = False
                    continue
                sheet_xml[entry] = xml
                if entry.lower().startswith("xl/chartsheets/"):
                    continue  # a chart on a sheet of its own: no cells
                section = f"工作表：{name}" + ("（隱藏）" if is_hidden else "")
                info = {"bad_index": False, "groups": {}}
                for row in self._rows(xml, shared, date_styles, date1904, info, formats):
                    text = ""
                    for k, cell in enumerate(row):
                        if k:
                            text += " | "
                        cell.offset = len(text)
                        text += cell.text
                        if cell.kind == "s":
                            si_refs[cell.si] = si_refs.get(cell.si, 0) + 1
                    add_unit(text, section, entry).cells = row
                groups[entry] = info["groups"]
                if info["bad_index"]:
                    inv.unchecked.append(Unchecked(entry, "有儲存格指向不存在的字串，這張工作表的內容不完整"))
                    complete = False

            # Hidden places are numbered after everything else: two of them masked to the same text merge into
            # one unit, and that must not shift the numbers of the units that are compared one by one.
            pending: dict[tuple[str, str], list] = {}

            def add_hidden(text: str, section: str, loc: tuple) -> None:
                if text.strip():
                    pending.setdefault((section, text), []).append(loc)

            for (name, ns, ne), _entry, _hidden in sheets:
                add_hidden(name, "工作表名稱", (self.main_part, None, ns, ne, "sheetname"))

            def paragraphs(entry: str, section: str, p_tags=None, t_tags=None) -> str | None:
                xml = self._xml(z, entry)
                if xml is None:
                    return None
                paras[entry] = parse_part(xml, p_tags, t_tags)
                for pi, p in enumerate(paras[entry]):
                    if p.text.strip():
                        add_unit(p.text, section, entry).paras = [(pi, 0)]
                return xml

            metas: dict[str, _Meta] = {}

            def add_meta(label, value, person, **where):
                mid = f"m{len(metas) + 1}"
                unit = MetaUnit(id=mid, label=label, value=value, person=person)
                metas[mid] = _Meta(unit=unit, **where)
                inv.metas.append(unit)

            def matching(pattern: str) -> list[str]:
                return sorted((n for n in names if re.fullmatch(pattern, n)), key=_natural)

            self._read_props(z, names, add_meta)
            for entry in matching(r"xl/comments\d*\.xml"):
                xml = paragraphs(entry, "註解", {"comment"}, {"t": "seg"}) or ""
                for m in re.finditer(r"<author\b[^>]*>([^<]*)</author>", xml):
                    if html.unescape(m.group(1)).strip():
                        add_meta("註解作者", html.unescape(m.group(1)), True, entry=entry, start=m.start(1), end=m.end(1),
                                 whole=NEUTRAL_AUTHOR)
            for entry in matching(r"xl/threadedComments/[^/]+\.xml"):
                paragraphs(entry, "註解", {"threadedComment"}, {"text": "seg"})
            for entry in matching(r"xl/drawings/drawing\d*\.xml"):
                xml = paragraphs(entry, "圖形文字")
                for text, section, s, e, mode in list(_hidden_in_part(xml)) + list(self._extras(xml, entry)) if xml else []:
                    add_hidden(text, section, (entry, None, s, e, mode))
            for entry in matching(r"xl/drawings/vmlDrawing\d*\.vml"):
                for text, section, s, e, mode in self._legacy_shapes(self._xml(z, entry) or ""):
                    add_hidden(text, section, (entry, None, s, e, mode))
            for entry in matching(r"xl/charts/chart\d+\.xml"):
                xml = paragraphs(entry, "圖表")
                for text, section, s, e, mode in _hidden_in_part(xml) if xml else []:
                    add_hidden(text, section, (entry, None, s, e, mode))
            for text, section, s, e, mode in self._extras(workbook, "活頁簿"):
                add_hidden(text, section, (self.main_part, None, s, e, mode))
            headers: dict[tuple[str, int, int], str] = {}  # (entry, start, end) -> the raw text of a header or footer
            for number, ((_name, _ns, _ne), entry, _hidden) in enumerate(sheets, 1):
                # by position, not by name: the name can itself be masked, and the check compares section labels
                for text, section, s, e, mode in self._extras(sheet_xml.get(entry, ""), f"第 {number} 張工作表"):
                    if mode == "header":
                        raw = headers[(entry, s, e)] = sheet_xml[entry][s:e]
                        if "x" in _header(_plain(raw)[0])[2] and section not in {u.name for u in inv.unchecked}:
                            inv.unchecked.append(Unchecked(section, HF_UNCLEAR))
                    add_hidden(text, section, (entry, None, s, e, mode))
            singles = [n for n in (SHARED, "docProps/custom.xml", "xl/styles.xml") if n in names]
            for entry in matching(r"xl/(?:tables|pivotTables|persons)/[^/]+\.xml") + singles:
                for text, section, s, e, mode in self._extras(self._xml(z, entry) or "", entry):
                    add_hidden(text, section, (entry, None, s, e, mode))
            if any(n.lower().startswith("xl/revisions/") for n in names):
                inv.notes.append("活頁簿留有追蹤修訂的紀錄（儲存格的舊值與修改者）：輸出檔會把這份紀錄整個移除")
            seen = 0
            pivot = matching(r"xl/pivotCache/[^/]+\.xml")
            for entry in pivot:
                xml = self._xml(z, entry) or ""
                for text, section, s, e, mode in self._extras(xml, entry):
                    add_hidden(text, section, (entry, None, s, e, mode))
                for kind, tag, s, e in iter_tokens(xml):
                    if kind not in ("open", "empty"):
                        continue
                    for aname, value, vs, ve in attr_values(xml[s:e]) if tag in ("s", "pivotCacheDefinition") else ():
                        if tag == "s" and aname == "v" and len(value.strip()) >= 2:
                            seen += 1
                            if seen <= PIVOT_CAP:
                                add_hidden(value, "樞紐分析快取", (entry, None, s + vs, s + ve, "attr"))
                        elif tag == "pivotCacheDefinition" and aname == "refreshedBy" and value.strip():
                            add_meta("樞紐分析表更新者", value, True, entry=entry, start=s + vs, end=s + ve, whole=NEUTRAL_AUTHOR)
            if seen > PIVOT_CAP:
                inv.unchecked.append(Unchecked("xl/pivotCache", f"樞紐分析快取內容過多，只檢查了前 {PIVOT_CAP} 筆"))
            if pivot:
                inv.notes.append("活頁簿含樞紐分析表：有內容被遮蔽時，樞紐分析表另存的資料會清除，開啟檔案時會自動重新整理")
            self._scan_links(z, names, add_hidden)
            self._scan_custom(z, names, inv, add_hidden)
            nested_pictures = self._scan_nested(z, names, inv, add_hidden)
            for (section, text), locs in pending.items():
                add_unit(text, section, "").locs = locs

            media_map: dict[str, str] = {}
            for name in sorted((n for n in names if n.startswith("xl/media/") and not n.endswith("/")), key=_natural):
                iid = f"img{len(inv.images) + 1}"
                media_map[iid] = name
                try:
                    fmt, w, h, frames = imaging.image_info(z.read(name))
                    ok = fmt in imaging.WRITABLE and frames == 1
                except Exception:
                    w, h, ok = 0, 0, False
                inv.images.append(ImageUnit(id=iid, name=name, width=w, height=h, supported=ok, areas=[(0, 0, w, h)] if ok else []))
                if not ok:
                    inv.unchecked.append(Unchecked(name, "不支援的圖片格式，未檢查"))
            self._list_nested_pictures(inv, media_map, nested_pictures)
            for name in names:
                low = name.lower()
                if re.fullmatch(r"xl/externallinks/[^/]+\.xml", low):
                    inv.unchecked.append(Unchecked(name, "外部活頁簿連結的快取資料，未檢查"))
                elif low == "xl/connections.xml":
                    inv.unchecked.append(Unchecked(name, "資料連線設定，未檢查"))
                elif low.endswith("vbaproject.bin"):
                    inv.unchecked.append(Unchecked(name, "巨集，未檢查"))
                elif low.startswith("xl/embeddings/") and not low.endswith(NESTED_OOXML) and not name.endswith("/"):
                    inv.unchecked.append(Unchecked(name, "內嵌物件，未檢查"))
            tables: dict[str, dict] = {}
            for (sheet_name, _ns, _ne), entry, _hidden in sheets:
                folder, _, file = (entry or "").rpartition("/")
                rel_name = f"{folder}/_rels/{file}.rels"
                for target in (_rels(self._xml(z, rel_name) or "", folder).values() if rel_name in names else ()):
                    if not target.lower().startswith("xl/tables/") or target not in names:
                        continue
                    xml = self._xml(z, target) or ""
                    head = re.search(r"<table\b[^>]*>", xml)
                    a = {k: v for k, v, _s, _e in attr_values(head.group(0))} if head else {}
                    box = xlsx_refs.area(a.get("ref", ""))
                    if box and a.get("displayName"):
                        tables[a["displayName"]] = {
                            "entry": entry, "sheet": sheet_name, "c1": box[0], "r1": box[1], "c2": box[2], "r2": box[3],
                            "columns": [next((v for k, v, _s, _e in attr_values(m.group(0)) if k == "name"), "")
                                        for m in re.finditer(r"<tableColumn\b[^>]*>", xml)]}
            listed = {u.name for u in inv.unchecked}
            for name, reason in notes:
                if name not in listed:
                    inv.unchecked.append(Unchecked(name, reason))
                    listed.add(name)
            for name in names:  # whatever is left is something this module does not read: say so by name
                if not (name.endswith("/") or name in listed or name in sheet_xml or KNOWN.fullmatch(name.lower())):
                    inv.unchecked.append(Unchecked(name, "這個部分的內容未檢查"))
        return _Doc(inv=inv, paras=paras, units=units, metas=metas, media=media_map,
                    extra={"shared": shared, "si_refs": si_refs, "sheets": [n for (n, _s, _e), _entry, _h in sheets], "headers": headers,
                           "groups": groups, "complete": complete, "pivot": bool(pivot), "tables": tables,
                           "entries": {entry: name for (name, _s, _e), entry, _h in sheets if entry},
                           "defined": {html.unescape(m.group(1)): html.unescape(m.group(2)) for m in re.finditer(
                               r'<definedName\b[^>]*?\bname="([^"]*)"[^>]*>([^<]*)</definedName>', workbook)},
                           "names": {m.group(1) for m in re.finditer(r'<definedName\b[^>]*?\bname="([^"]*)"', workbook)}
                           | {u.text for u in units.values() if u.section == "已定義名稱"}})

    @staticmethod
    def _extras(xml: str, where: str):
        """Text of a part that is not in cells: header/footer, drop-down choices, text literals written into
        formulas, and attributes a person typed into (filters, prompts, names). Yields (text, section, start,
        end, mode). A header or footer is its printed text (_header), mode "header"; that text can be empty
        when all it holds are format codes."""
        stack: list[str] = []
        for kind, name, s, e in iter_tokens(xml):
            if kind == "text":
                if not stack:
                    continue
                top = stack[-1]
                if top in HEADER_FOOTER:
                    if xml[s:e].strip():
                        yield _header(_plain(xml[s:e])[0])[0], f"頁首頁尾：{where}", s, e, "header"
                elif top in ("formula1", "formula2") and "dataValidation" in stack and '"' in html.unescape(xml[s:e]):
                    yield html.unescape(xml[s:e]), HIDDEN + "下拉選單", s, e, "text"
                elif top in FORMULA_ELEMS:
                    for text, ls, le in _literals(xml[s:e]):
                        if len(text.strip()) >= 2:
                            yield text, HIDDEN + "公式裡的文字", s + ls, s + le, "text"
                elif top == "t" and "rPh" in stack and html.unescape(xml[s:e]).strip():
                    yield html.unescape(xml[s:e]), HIDDEN + "注音標示", s, e, "text"  # spells out how the cell's text is read
            elif kind in ("open", "empty"):
                wanted = CONTENT_ATTRS.get(name)
                if wanted and not (name == "cellStyle" and BUILTIN_STYLE.search(xml[s:e])):
                    for aname, value, vs, ve in attr_values(xml[s:e]):
                        if aname in wanted and value.strip():
                            section = ("連結說明" if "link" in name else "作者帳號" if name == "person"
                                       else COLUMN_NAMES if name == "tableColumn" and aname == "name" else "設定文字")
                            yield value, HIDDEN + section, s + vs, s + ve, "attr"
                if name == "numFmt":  # text written into a number format ("承辦人："0): read apart from its codes
                    for aname, value, vs, ve in attr_values(xml[s:e]):
                        if aname == "formatCode":
                            plain, index = _plain(xml[s + vs:s + ve])
                            for a, b in numfmt.literals(plain):
                                if len(plain[a:b].strip()) >= 2:
                                    yield plain[a:b], HIDDEN + "數字格式", s + vs + index[a], s + vs + index[b], "attr"
                if name in ("definedName", "table"):  # identifiers that formulas use: renamed, not masked in place
                    for aname, value, vs, ve in attr_values(xml[s:e]):
                        if (aname == "name" or (name == "table" and aname == "displayName")) and len(value.strip()) >= 2 \
                                and not value.startswith("_xlnm.") and not DEFAULT_TABLE_NAME.fullmatch(value):
                            yield value, "已定義名稱", s + vs, s + ve, "defname"
                if kind == "open":
                    stack.append(name)
            elif kind == "close":
                while stack and stack[-1] != name:
                    stack.pop()
                if stack:
                    stack.pop()

    @staticmethod
    def _legacy_shapes(xml: str):
        """Legacy (VML) shapes - form buttons, old comment boxes: the text typed into them and their descriptions."""
        stack: list[str] = []
        for kind, name, s, e in iter_tokens(xml):
            if kind == "text":
                if "v:textbox" in stack and html.unescape(xml[s:e]).strip():
                    yield html.unescape(xml[s:e]), HIDDEN + "圖形文字", s, e, "text"
            elif kind in ("open", "empty"):
                for aname, value, vs, ve in attr_values(xml[s:e]):
                    if aname in ("alt", "o:title") and value.strip():
                        yield value, HIDDEN + "圖片說明", s + vs, s + ve, "attr"
                if kind == "open":
                    stack.append(name)
            elif kind == "close":
                while stack and stack[-1] != name:
                    stack.pop()
                if stack:
                    stack.pop()

    # ---- writing -------------------------------------------------------------------------

    @staticmethod
    def _inline(cell: _Cell, text: str) -> str:
        ref = f' r="{cell.ref}"' if cell.ref else ""
        style = f' s="{cell.style}"' if cell.style else ""
        return f'<c{ref}{style} t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>'

    def _cell_replacements(self, doc: _Doc, plan: MaskPlan, full: str, half: str):
        """Replacements per zip entry for masked cells, and whether any formula was replaced by a value."""
        reps: dict[str, list] = {}
        shared_uses: dict[int, list] = {}
        formulas_removed = False
        for uid, spans in plan.text.items():
            unit = doc.units.get(uid)
            if unit is None or not unit.cells or not spans:
                continue
            marks = bytearray(len(unit.text))
            for s, e in spans:
                for i in range(max(0, s), min(len(unit.text), e)):
                    marks[i] = 1
            for cell in unit.cells:
                m = bytes(marks[cell.offset:cell.offset + len(cell.text)])
                if not any(m):
                    continue
                if cell.kind == "s":
                    shared_uses.setdefault(cell.si, []).append((unit.part, cell, m))
                elif cell.kind == "is":
                    pos = 0
                    for s, e, text in cell.pieces:
                        if any(m[pos:pos + len(text)]):
                            new = "".join(mask_chars(c, full, half) if m[pos + i] else c for i, c in enumerate(text))
                            reps.setdefault(unit.part, []).append((s, e, escape(new)))
                        pos += len(text)
                    reps.setdefault(unit.part, []).extend((s, e, "") for s, e in cell.rph)  # the reading spells it out
                else:
                    new = ("".join(mask_chars(c, full, half) if m[i] else c for i, c in enumerate(cell.text)) if cell.partial
                           else mask_chars(cell.text, full, half))
                    reps.setdefault(unit.part, []).append((cell.start, cell.end, self._inline(cell, new)))
                    formulas_removed = formulas_removed or cell.formula
                    if cell.group is not None:
                        # The formula the rest of a filled-down group refers to goes with this cell: the others
                        # drop their reference and keep the value they last showed.
                        reps[unit.part].extend((s, e, "") for s, e in doc.extra["groups"].get(unit.part, {}).get(cell.group, []))
        shared, refs = doc.extra["shared"], doc.extra["si_refs"]
        for si, uses in shared_uses.items():
            if len(uses) == refs.get(si, 0):
                # Every cell showing this string is masked: edit the string itself, so no unreferenced copy of
                # the original is left behind. Cells masked to different extents get the union of their masks.
                m, pos = bytes(max(col) for col in zip(*(u[2] for u in uses))), 0
                for s, e, text in shared[si].pieces:
                    if any(m[pos:pos + len(text)]):
                        new = "".join(mask_chars(c, full, half) if m[pos + i] else c for i, c in enumerate(text))
                        reps.setdefault(SHARED, []).append((s, e, escape(new)))
                    pos += len(text)
                reps.setdefault(SHARED, []).extend((s, e, "") for s, e in shared[si].phonetic)
            else:  # other cells keep the string: give each masked cell its own text
                for part, cell, m in uses:
                    new = "".join(mask_chars(c, full, half) if m[i] else c for i, c in enumerate(cell.text))
                    reps.setdefault(part, []).append((cell.start, cell.end, self._inline(cell, new)))
        if doc.extra["complete"]:  # a string no cell shows any more is an old value left behind: empty it
            for si, item in enumerate(shared):
                if si not in refs:
                    reps.setdefault(SHARED, []).extend((s, e, "") for s, e, _text in item.pieces)
                    reps[SHARED].extend((s, e, "") for s, e in item.phonetic)
        return reps, formulas_removed

    @staticmethod
    def _marks(unit: _Unit, spans) -> bytearray:
        marks = bytearray(len(unit.text))
        for s, e in spans:
            for i in range(max(0, s), min(len(unit.text), e)):
                marks[i] = 1
        return marks

    def _hidden_replacements(self, doc: _Doc, plan: MaskPlan, full: str, half: str):
        """As for any hidden place, except a header or footer: its unit is the printed text, so each masked
        character is written where it stands in the raw text, between the format codes."""
        top, nested = DocxAdapter._hidden_replacements(doc, plan, full, half)
        for uid, spans in plan.text.items():
            unit = doc.units.get(uid)
            if unit is None or not spans or not any(loc[4] == "header" for loc in unit.locs):
                continue
            marks = self._marks(unit, spans)
            changes = [(i, mask_chars(c, full, half)) for i, c in enumerate(unit.text) if marks[i] and not c.isspace()]
            for entry, _inner, s, e, mode in unit.locs:
                raw = doc.extra["headers"].get((entry, s, e))  # read from a sheet; elsewhere it is written whole
                if mode == "header" and raw is not None:
                    top[entry] = [r for r in top.get(entry, []) if (r[0], r[1]) != (s, e)] + _header_writes(raw, s, changes)
        return top, nested

    def _sheet_renames(self, doc: _Doc, plan: MaskPlan, full: str):
        """Old sheet name -> new one, and the replacements that write it into the workbook."""
        renames: dict[str, str] = {}
        reps: list = []
        taken = set(doc.extra["sheets"])
        for uid, spans in plan.text.items():
            unit = doc.units.get(uid)
            if unit is None or not spans or not any(loc[4] == "sheetname" for loc in unit.locs):
                continue
            old, marks = unit.text, self._marks(unit, spans)
            # sheet names may not contain * ? : / \ [ ], so every masked character becomes the full-width mask
            new = "".join((c if c.isspace() else full) if marks[i] else c for i, c in enumerate(old))
            new = SHEET_NAME_FORBIDDEN.sub(full, new)[:31]
            base, n = new, 2
            while new in taken and new != old:
                new = f"{base[:28]} {n}"
                n += 1
            taken.add(new)
            renames[old] = new
            for entry, _inner, s, e, mode in unit.locs:
                if mode == "sheetname":
                    reps.append((s, e, escape(new, {'"': "&quot;"})))
        return renames, reps

    def _name_renames(self, doc: _Doc, plan: MaskPlan):
        """Old defined name -> new one, and where it is written. A defined name is an identifier that formulas
        use, so its masked characters become underscores (mask characters are not allowed in a name)."""
        renames: dict[str, str] = {}
        reps: dict[str, list] = {}
        taken = set(doc.extra["names"])
        for uid, spans in plan.text.items():
            unit = doc.units.get(uid)
            if unit is None or not spans or not any(loc[4] == "defname" for loc in unit.locs):
                continue
            old, marks = unit.text, self._marks(unit, spans)
            new = "".join("_" if marks[i] else c for i, c in enumerate(old))
            base, n = new, 2
            while new in taken and new != old:
                new = f"{base}{n}"
                n += 1
            taken.add(new)
            renames[old] = new
            for entry, _inner, s, e, mode in unit.locs:
                if mode == "defname":
                    reps.setdefault(entry, []).append((s, e, escape(new, {'"': "&quot;"})))
        return renames, reps

    @staticmethod
    def _rename_refs(name: str, xml: str, renames: dict[str, str]) -> str:
        """Formulas, defined names, chart ranges and the title list refer to sheets by name. A reference that
        names the first and last of several sheets (First:Last!A1) follows either being renamed; it is written
        again in quotes as a whole. A reference into another workbook is left alone."""
        def ref(m) -> str:
            if m.group("q"):
                if "[" in m.group("body") or "]" in m.group("body"):
                    return m.group(0)
                parts, quote = html.unescape(m.group("body")).replace("''", "'").split(":"), m.group("q")
            else:
                if m.group("book"):
                    return m.group(0)
                parts, quote = [p for p in (m.group("a"), m.group("b")) if p is not None], "'"
            if len(parts) > 2 or not any(p in renames for p in parts):
                return m.group(0)
            new = ":".join(renames.get(p, p) for p in parts).replace("'", "''")  # inside quotes an apostrophe is written twice
            return escape(f"'{new}'").replace("'", quote) + "!"

        xml = SHEET_REF.sub(ref, xml)
        for old, new in renames.items():
            eo, en = escape(old), escape(new)
            if name == "docProps/app.xml":
                xml = xml.replace(f">{eo}<", f">{en}<")
            if name.startswith("xl/pivotCache/"):
                xml = xml.replace(f'sheet="{escape(old, {chr(34): "&quot;"})}"', f'sheet="{escape(new, {chr(34): "&quot;"})}"')
        return xml

    @staticmethod
    def _distinct_styles(before: str, after: str) -> str:
        """Cell styles are told apart by name. Two names masked alike (王小明 and 陳美玲 both ■■■) are kept
        apart by a number after the second; a name the masking left as it was is not touched."""
        def names(xml):
            out = []
            for m in CELL_STYLE.finditer(xml):
                out += [(value, m.start() + vs, m.start() + ve) for aname, value, vs, ve in attr_values(m.group(0)) if aname == "name"]
            return out

        old, new = names(before), names(after)
        if len(old) != len(new):
            return after
        used = {n for (o, _s, _e), (n, _ns, _ne) in zip(old, new) if o == n}
        reps = []
        for (o, _s, _e), (n, s, e) in zip(old, new):
            if o == n:
                continue
            final, k = n, 2
            while final in used:
                final, k = f"{n} {k}", k + 1
            used.add(final)
            if final != n:
                reps.append((s, e, escape(final, {'"': "&quot;", "'": "&apos;"})))
        return splice(after, reps)

    @staticmethod
    def _in_formulas(xml: str, change) -> str:
        """`change` applied to every formula in the part, outside its text literals."""
        reps, stack = [], []
        for kind, name, s, e in iter_tokens(xml):
            if kind == "text":
                if stack and stack[-1] in FORMULA_ELEMS:
                    plain = html.unescape(xml[s:e])
                    new = _outside_literals(plain, change)
                    if new != plain:
                        reps.append((s, e, escape(new)))
            elif kind == "open":
                stack.append(name)
            elif kind == "close":
                while stack and stack[-1] != name:
                    stack.pop()
                if stack:
                    stack.pop()
        return splice(xml, reps)

    def _column_renames(self, doc: _Doc, plan: MaskPlan, full: str, half: str) -> dict[str, str]:
        """Old table column name -> masked one. The header cell and the column definition are masked through
        their own units; formulas name the column too (Table[column]) and have to follow."""
        out: dict[str, str] = {}
        for uid, spans in plan.text.items():
            unit = doc.units.get(uid)
            if unit is not None and spans and unit.section == HIDDEN + COLUMN_NAMES:
                marks = self._marks(unit, spans)
                out[unit.text] = "".join(mask_chars(c, full, half) if marks[i] else c for i, c in enumerate(unit.text))
        return out

    def _fixups(self, name: str, xml: str, doc: _Doc, renames: dict, names: dict, formulas_removed: bool, masked: bool,
                columns: dict | None = None) -> str:
        """Changes that follow from the masking rather than being the masking itself, and side copies that are
        always removed."""
        low = name.lower()
        if renames:
            xml = self._rename_refs(name, xml, renames)
        if names:
            pattern = re.compile(r"(?<![\w.])(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")(?![\w.(])")
            xml = self._in_formulas(xml, lambda text: pattern.sub(lambda m: names[m.group(0)], text))
        if columns:
            pattern = re.compile(r"(?<=[\[@])(?:" + "|".join(re.escape(c) for c in sorted(columns, key=len, reverse=True)) + r")(?=\])")
            xml = self._in_formulas(xml, lambda text: pattern.sub(lambda m: columns[m.group(0)], text))
        if low.endswith(".rels"):
            xml = re.sub(r'<Relationship\b[^>]*Type="[^"]*/(?:printerSettings|revisionHeaders|revisionLog|usernames)"[^>]*/>', "", xml)
            if formulas_removed and name == "xl/_rels/workbook.xml.rels":
                xml = re.sub(r"<Relationship\b[^>]*calcChain[^>]*/>", "", xml)
            if masked and low.startswith("xl/pivotcache/_rels/"):
                xml = re.sub(r"<Relationship\b[^>]*pivotCacheRecords[^>]*/>", "", xml)
        elif name == "[Content_Types].xml":
            xml = re.sub(r'<Override\b[^>]*PartName="/xl/revisions/[^>]*/>', "", xml)
            if formulas_removed:
                xml = re.sub(r"<Override\b[^>]*calcChain[^>]*/>", "", xml)
            if masked:
                xml = re.sub(r"<Override\b[^>]*pivotCacheRecords[^>]*/>", "", xml)
        elif name == self.main_part:
            # where the file was last saved: a folder path that usually holds an account name
            xml = re.sub(r'(<(?:[\w.\-]+:)?absPath\b[^>]*?\burl=")[^"]*', r"\1", xml)
        elif low.startswith(("xl/worksheets/", "xl/chartsheets/", "xl/dialogsheets/", "xl/macrosheets/")):
            xml = re.sub(r'(<pageSetup\b[^>]*?)\s+r:id="[^"]*"', r"\1", xml)
        elif masked and re.fullmatch(r"xl/pivotcache/pivotcachedefinition\d*\.xml", low):
            # The cache is a second copy of the source data. Its records are dropped (see apply) and the values
            # it lists per field are blanked; the program rebuilds both from the masked sheet when the file opens.
            def root(m):
                tag = re.sub(r'\s+(?:r:id|saveData|refreshOnLoad)="[^"]*"', "", m.group(0)[:-1])
                return tag + ' saveData="0" refreshOnLoad="1">'
            xml = re.sub(r"<pivotCacheDefinition\b[^>]*>", root, xml, count=1)
            xml = re.sub(r'(<n\b[^>]*?\bv=")[^"]*', r"\g<1>0", xml)
            xml = re.sub(r'(<d\b[^>]*?\bv=")[^"]*', r"\g<1>1900-01-01T00:00:00", xml)
            xml = re.sub(r'\b(minValue|maxValue)="[^"]*"', r'\1="0"', xml)
            xml = re.sub(r'\b(minDate|maxDate)="[^"]*"', r'\1="1900-01-01T00:00:00"', xml)
        elif masked and re.fullmatch(r"xl/charts/chart\d+\.xml", low):
            # numbers a chart keeps of the cells it draws: redrawn from the sheet when the file opens
            xml = re.sub(r"<c:numCache>.*?</c:numCache>",
                         lambda m: re.sub(r"<c:v>[^<]*</c:v>", "<c:v>0</c:v>", m.group(0)), xml, flags=re.S)
        return xml

    def _generic(self, name: str, xml: str, strings: list[str], full: str, half: str, partial: dict | None = None) -> str:
        """On top of the inherited search: the attributes that hold typed text, the text literals inside
        formulas (never the rest of a formula), and the printed text of headers and footers (never their
        format codes)."""
        xml = super()._generic(name, xml, strings, full, half, partial)
        pattern, mask = searcher(strings, full, half, partial)
        if pattern is None or name.lower().endswith((".rels", ".vml")):
            return xml
        pivot, reps, stack = name.startswith("xl/pivotCache/"), [], []
        for kind, tag, s, e in iter_tokens(xml):
            if kind == "text":
                if stack and stack[-1] in FORMULA_ELEMS:
                    plain = html.unescape(xml[s:e])
                    new = LITERAL.sub(lambda m: '"' + pattern.sub(mask, m.group(1)) + '"', plain)
                    if new != plain:
                        reps.append((s, e, escape(new)))
                elif stack and stack[-1] in HEADER_FOOTER:
                    text = _header(_plain(xml[s:e])[0])[0]
                    changes = [(m.start() + k, ch) for m in pattern.finditer(text)
                               for k, ch in enumerate(mask(m)) if ch != m.group(0)[k]]
                    reps += _header_writes(xml[s:e], s, changes)
            elif kind in ("open", "empty"):
                wanted = CONTENT_ATTRS.get(tag) or (PIVOT_ITEM if pivot and tag == "s" else None)
                for aname, value, vs, ve in attr_values(xml[s:e]) if wanted else ():
                    if aname in wanted:
                        new = pattern.sub(mask, value)
                        if new != value:
                            reps.append((s + vs, s + ve, escape(new, {'"': "&quot;", "'": "&apos;"})))
                for aname, _value, vs, ve in attr_values(xml[s:e]) if tag == "numFmt" else ():
                    if aname == "formatCode":  # only the text written into the format, never its codes
                        plain, index = _plain(xml[s + vs:s + ve])
                        for a, b in numfmt.literals(plain):
                            new = pattern.sub(mask, plain[a:b])
                            if new != plain[a:b]:
                                reps.append((s + vs + index[a], s + vs + index[b], escape(new, {'"': "&quot;", "'": "&apos;"})))
                if kind == "open":
                    stack.append(tag)
            elif kind == "close":
                while stack and stack[-1] != tag:
                    stack.pop()
                if stack:
                    stack.pop()
        return splice(xml, reps)

    def _residue_hits(self, name: str, xml: str, strings: list[str]) -> set[str]:
        """Formulas are searched inside their text literals only (a masked 35 is not "found" in $B$35), positions
        not at all, headers and footers in their printed text and wherever a string runs over a format code, and
        the attributes that hold typed text on top of the general ones."""
        pattern = compile_strings(strings)
        found: set[str] = set()
        if pattern is None:
            return found
        pivot, stack = name.startswith("xl/pivotCache/"), []
        for kind, tag, s, e in iter_tokens(xml):
            if kind == "text":
                top = stack[-1] if stack else ""
                if top in POSITION_ELEMS:
                    continue
                if top in HEADER_FOOTER:
                    found |= _header_hits(xml[s:e], pattern, strings)
                    continue
                plain = html.unescape(xml[s:e])
                if top in FORMULA_ELEMS:
                    plain = "\n".join(m.group(1) for m in LITERAL.finditer(plain))
                found.update(pattern.findall(plain))
            elif kind in ("open", "empty"):
                wanted = self.residue_attrs | CONTENT_ATTRS.get(tag, set())
                if pivot and tag == "s":
                    wanted = wanted | PIVOT_ITEM
                if tag in ("definedName", "table"):
                    wanted = wanted | {"name", "displayName"}
                for aname, value, _vs, _ve in attr_values(xml[s:e]):
                    if aname in wanted:
                        found.update(pattern.findall(value))
                    elif tag == "numFmt" and aname == "formatCode":
                        found.update(pattern.findall("\n".join(value[a:b] for a, b in numfmt.literals(value))))
                if kind == "open":
                    stack.append(tag)
            elif kind == "close":
                while stack and stack[-1] != tag:
                    stack.pop()
                if stack:
                    stack.pop()
        return found

    def apply(self, src: Path, dst: Path, plan: MaskPlan, settings: Settings) -> None:
        doc = self._load(src)
        full, half = settings.mask_full, settings.mask_half
        masked = bool(plan.search()) or any(plan.text.values())
        meta_reps, _authors = self._meta_replacements(doc, plan, full, half)
        text_reps = self._text_replacements(doc, plan, full, half)
        hidden_top, hidden_nested = self._hidden_replacements(doc, plan, full, half)
        cell_reps, formulas_removed = self._cell_replacements(doc, plan, full, half)
        renames, sheet_reps = self._sheet_renames(doc, plan, full)
        names, name_reps = self._name_renames(doc, plan)
        columns = self._column_renames(doc, plan, full, half)
        for entry, reps in name_reps.items():  # a defined name is renamed, not masked in place
            spans = {(s, e) for s, e, _new in reps}
            hidden_top[entry] = [r for r in hidden_top.get(entry, []) if (r[0], r[1]) not in spans] + reps
        boxes = {doc.media[i]: b for i, b in plan.boxes.items() if i in doc.media and b}
        nested_boxes = self._nested_boxes(boxes)
        with self._open(src) as zin, zipfile.ZipFile(dst, "w") as zout:
            for info in zin.infolist():
                name = info.filename
                low = name.lower()
                if (name.startswith("docProps/thumbnail") or low.startswith(("xl/printersettings/", "xl/revisions/"))
                        or (formulas_removed and name == "xl/calcChain.xml")  # Excel rebuilds the calculation chain
                        or (masked and re.fullmatch(r"xl/pivotcache/(?:_rels/)?pivotcacherecords\d*\.xml(?:\.rels)?", low))):
                    continue
                data = zin.read(info)
                if name in boxes:
                    data = imaging.paint_boxes(data, boxes[name])
                elif "/media/" in name:
                    data = imaging.strip_metadata(data)
                elif _is_xml(name):
                    reps = (text_reps.get(name, []) + meta_reps.get(name, []) + hidden_top.get(name, []) + cell_reps.get(name, [])
                            + (sheet_reps if name == self.main_part else []))
                    # In this order: the planned replacements (their positions refer to the original text), then
                    # what follows from them, then the search for any other copy of the masked strings.
                    xml = self._rewrite_xml(name, data, [], full, half, reps, {}).decode("utf-8")
                    xml = self._fixups(name, xml, doc, renames, names, formulas_removed, masked, columns)
                    xml = self._generic(name, xml, plan.search(), full, half, plan.partial)
                    if name == "xl/styles.xml":
                        xml = self._distinct_styles(data.decode("utf-8"), xml)
                    data = xml.encode("utf-8")
                elif low.endswith(NESTED_OOXML):
                    data = self._rewrite_nested(data, plan.search(), full, half, hidden_nested.get(name, {}), plan.partial,
                                                nested_boxes.get(name))
                out = zipfile.ZipInfo(name, date_time=info.date_time)
                out.compress_type = info.compress_type
                out.external_attr = info.external_attr
                out.create_system = info.create_system
                zout.writestr(out, data)

    # ---- codes over a masked file ---------------------------------------------------------

    def _code_cells(self, unit: _Unit, wanted, jobs: dict, reps: dict, shared: dict) -> list[tuple[int, int, str]]:
        done = []
        for cell in unit.cells:
            lo, hi = cell.offset, cell.offset + len(cell.text)
            inside = [(s, e, code) for s, e, code in wanted if lo <= s < e <= hi]
            if not inside or cell.kind == "v":  # a masked cell is text; a number here was not masked
                continue
            local = sorted((s - lo, e - lo, code) for s, e, code in inside)
            if cell.kind == "is":
                jobs.setdefault(unit.part, []).append((list(cell.pieces), local))
                reps.setdefault(unit.part, []).extend((s, e, "") for s, e in cell.rph)
            else:
                shared.setdefault(cell.si, []).append((unit.part, cell, local))
            done += inside
        return done

    def _code_shared(self, doc: _Doc, shared: dict, jobs: dict, reps: dict) -> None:
        strings, refs = doc.extra["shared"], doc.extra["si_refs"]
        for si, uses in shared.items():
            if len(uses) == refs.get(si, 0) and all(u[2] == uses[0][2] for u in uses):
                # Every cell showing this string takes the same codes: the string itself is written again.
                jobs.setdefault(SHARED, []).append((list(strings[si].pieces), uses[0][2]))
                reps.setdefault(SHARED, []).extend((s, e, "") for s, e in strings[si].phonetic)
                continue
            for part, cell, local in uses:  # other cells keep the string: each of these gets its own text
                new, pos = [], 0
                for s, e, code in local:
                    new += [cell.text[pos:s], code]
                    pos = e
                reps.setdefault(part, []).append((cell.start, cell.end, self._inline(cell, "".join(new) + cell.text[pos:])))

    # ---- cells computed from masked cells --------------------------------------------------

    def derived(self, src: Path, text_plan: dict) -> list[tuple[str, int, int, str]]:
        """Formula cells that read a masked cell, directly or through other formulas, and are not masked
        themselves: (unit, start, end, the masked cell it was traced back to). They can give the masked value
        back (salary x 12), so the reviewer is shown them; nothing is masked on this account."""
        doc = self._load(src)
        entry_of = {name: entry for entry, name in doc.extra["entries"].items()}
        order = doc.extra["sheets"]  # tab order: what a reference over several sheets takes in
        cells: dict[tuple, tuple] = {}
        masked: set[tuple] = set()
        formulas: dict[tuple, tuple[str, int, int]] = {}  # cell -> (formula text, rows down, columns right)
        masters: dict[tuple, tuple] = {}
        followers: list[tuple] = []
        for unit in doc.units.values():
            if not unit.cells:
                continue
            marks = self._marks(unit, text_plan.get(unit.id, ()))
            for cell in unit.cells:
                at = xlsx_refs.position(cell.ref)
                if at is None:
                    continue
                key = (unit.part, at[0], at[1])
                cells[key] = (unit, cell)
                if any(marks[cell.offset:cell.offset + len(cell.text)]):
                    masked.add(key)
                if cell.ftext:
                    formulas[key] = (cell.ftext, 0, 0)
                    if cell.fkind == "shared":
                        masters[(unit.part, cell.fsi)] = (key, cell.ftext)
                    elif cell.fkind == "array" and (box := xlsx_refs.area(cell.farea)):
                        followers.append((key, box))
                elif cell.formula and cell.fkind == "shared":
                    followers.append((key, cell.fsi))
        if not masked:
            return []
        for key, what in followers:
            if isinstance(what, tuple):  # the other cells of an array formula show results of the same formula
                for other in cells:
                    if other[0] == key[0] and what[0] <= other[1] <= what[2] and what[1] <= other[2] <= what[3]:
                        formulas.setdefault(other, formulas[key])
            elif (key[0], what) in masters:  # a filled-down formula, moved to this cell
                master, text = masters[(key[0], what)]
                formulas[key] = (text, key[2] - master[2], key[1] - master[1])
        named = {name: xlsx_refs.reads(text) for name, text in doc.extra["defined"].items()}
        name_pattern = re.compile(r"(?<![\w.])(?:" + "|".join(re.escape(n) for n in sorted(named, key=len, reverse=True))
                                  + r")(?![\w.(\[])") if named else None
        tables = doc.extra["tables"]
        single: dict[tuple, list] = {}  # cell -> formulas reading exactly it
        ranged: dict[tuple, list] = {}  # (entry, column) -> (first row, last row, formula)
        wide: dict[str, list] = {}  # entry -> (first column, last column, first row, last row, formula)
        for key, (text, down, right) in formulas.items():
            areas = xlsx_refs.reads(text, down, right)
            if tables and "[" in text:
                areas += xlsx_refs.structured(text, tables, key)
            if name_pattern is not None:
                bare = xlsx_refs.LITERAL.sub('""', text)
                for m in name_pattern.finditer(bare):
                    areas += named[m.group(0)]
            for sheet, c1, r1, c2, r2 in areas:
                for entry in [entry_of[n] for n in xlsx_refs.sheets_between(sheet, order) if n in entry_of] if sheet else [key[0]]:
                    if (c1, r1) == (c2, r2):
                        single.setdefault((entry, c1, r1), []).append(key)
                    elif c2 - c1 < 64:
                        for c in range(c1, c2 + 1):
                            ranged.setdefault((entry, c), []).append((r1, r2, key))
                    else:
                        wide.setdefault(entry, []).append((c1, c2, r1, r2, key))
        origin: dict[tuple, tuple] = {}
        queue = [(key, key) for key in sorted(masked)]
        while queue:
            at, root = queue.pop()
            readers = list(single.get(at, ()))
            readers += [f for r1, r2, f in ranged.get((at[0], at[1]), ()) if r1 <= at[2] <= r2]
            readers += [f for c1, c2, r1, r2, f in wide.get(at[0], ()) if c1 <= at[1] <= c2 and r1 <= at[2] <= r2]
            for f in readers:
                if f not in origin and f not in masked:
                    origin[f] = root
                    queue.append((f, root))
        out = []
        for key, root in origin.items():
            unit, cell = cells[key]
            out.append((unit.id, cell.offset, cell.offset + len(cell.text), xlsx_refs.column_letters(root[1]) + str(root[2])))
        return sorted(out)

    # ---- checking ------------------------------------------------------------------------

    @staticmethod
    def _separator_flags(doc: _Doc, uid: str, start: int, end: int) -> list[bool]:
        unit = doc.units[uid]
        if not unit.cells:
            return DocxAdapter._separator_flags(doc, uid, start, end)
        flags = [True] * len(unit.text)  # the " | " between cells has nothing behind it
        for cell in unit.cells:
            for i in range(cell.offset, cell.offset + len(cell.text)):
                flags[i] = False
        return flags[start:end]
