"""PDF: text is removed with real redactions (the glyphs leave the file, image pixels under them are
blacked out) and the file is rewritten in full so nothing old survives in unused objects.

Coordinates: PyMuPDF reports text and image positions in the page's unrotated space. What the user sees,
and what OCR reads, is the rendered page; `_to_px` / `_from_px` convert between the two."""
import html
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from deid import imaging
from deid.config import Settings
from deid.formats.base import BrokenFile
from deid.formats.xmltok import mask_chars, searcher
from deid.model import (Box, ImageUnit, Inventory, MaskPlan, MetaUnit, OcrLine, TextUnit, Unchecked, inventory_from_json,
                        to_json, tight)

SHRINK = 0.12  # redaction boxes are pulled in vertically so tight line spacing does not take the neighbours
# A redaction takes a glyph once it reaches past the outer tenth of the glyph's box, on any side (measured with
# PyMuPDF 1.28; tests/test_fix3_pdf.py holds the measurement). Glyph boxes are about 1.2 times the font size
# high, so lines set solid already overlap, and lines set tighter overlap by more than SHRINK clears.
TAKEN = 0.1
CLEAR = 0.3  # points kept between a redaction and the part of a neighbouring glyph that would get it taken
MIN_AREA_PX = 8
RAW_MIN_BYTES = 6  # shorter byte patterns turn up by chance inside compressed streams
# Document information: the usual entries by name; any other text entry (a maker's /Company, /SourceFile, ...) is
# shown as a custom property. Dates and the trapping flag carry nothing personal.
INFO_FIELDS = {"Author": ("作者", True), "Title": ("標題", False), "Subject": ("主旨", False), "Keywords": ("關鍵字", False),
               "Creator": ("建立程式", False), "Producer": ("PDF 產生程式", False)}
INFO_SKIP = {"CreationDate", "ModDate", "Trapped"}
INFO_NAMES = {"Author": "author", "Title": "title", "Subject": "subject", "Keywords": "keywords", "Creator": "creator",
              "Producer": "producer"}  # the library's names, for a file whose information is not an object of its own
RICH_TEXT_FLAG = 1 << 25  # text field flag: the value also exists as rich text (/RV)
# A glyph whose font gives no way to know its character (no /ToUnicode map, no usable encoding) comes out as
# U+FFFD, not as its raw code: the library's default turns such codes into random characters that look like text.
UNREADABLE = "\ufffd"
# Text placed outside the page box is not shown, but it is in the file and any text tool reads it: read all of it.
TEXT_FLAGS = pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_CID_FOR_UNKNOWN_UNICODE & ~pymupdf.TEXT_MEDIABOX_CLIP
CHECK_FLAGS = pymupdf.TEXTFLAGS_TEXT & ~pymupdf.TEXT_MEDIABOX_CLIP
GLYPH_KINDS = ("page", "offpage")  # units whose characters are glyphs on a page, masked by redaction
HIDDEN = "隱藏內容："  # section prefix of text a reader of the page does not see (as for Word files)
# Tagged PDF: text a structure element carries for readers that cannot see the page (alternate description,
# replacement text, abbreviation expansion, title).
STRUCT_KEYS = ("Alt", "ActualText", "E", "T")
STRUCT_SECTIONS = {"Alt": HIDDEN + "替代文字", "ActualText": HIDDEN + "替代文字", "E": HIDDEN + "替代文字",
                   "T": HIDDEN + "結構標題"}
MARKED_KEYS = (b"ActualText", b"Alt")  # the same, in the properties of marked content on a page
_SPACE = frozenset(b" \t\r\n\x0c\x00")
_DELIMS = _SPACE | frozenset(b"()<>[]{}/%")
_INLINE_IMAGE_END = re.compile(rb"\sEI(?=[\s/\[<(%]|$)")
MARKER = 0xE000  # private-use characters stand in for replacement text while its place on the page is measured
WIDGET_KINDS = ("widget", "widget_opt", "widget_dv", "widget_rv")


# ---- PDF strings ---------------------------------------------------------------------------------

_ESCAPES = {ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("b"): b"\b", ord("f"): b"\f", ord("("): b"(",
            ord(")"): b")", ord("\\"): b"\\"}


def _text_of(raw: bytes) -> str:
    """A PDF text string's bytes as text (UTF-16 or UTF-8 with a byte-order mark, else the 8-bit encoding)."""
    if raw[:2] == b"\xfe\xff":
        return raw[2:].decode("utf-16-be", "replace")
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", "replace")
    return raw.decode("latin-1")


def _read_string(data: bytes, i: int) -> tuple[bytes, int]:
    """The literal `(...)` or hex `<...>` string that starts at data[i], and the position after it."""
    if data[i] == ord("<"):
        j = data.index(b">", i)
        digits = re.sub(rb"\s", b"", data[i + 1:j])
        return bytes.fromhex((digits + b"0" * (len(digits) % 2)).decode("ascii")), j + 1
    out, depth, j, n = bytearray(), 1, i + 1, len(data)
    while j < n:
        c = data[j]
        if c == ord("\\") and j + 1 < n:
            nxt = data[j + 1]
            if nxt in _ESCAPES:
                out += _ESCAPES[nxt]
                j += 2
            elif ord("0") <= nxt <= ord("7"):
                k = j + 1
                while k < min(n, j + 4) and ord("0") <= data[k] <= ord("7"):
                    k += 1
                out.append(int(data[j + 1:k], 8) & 0xFF)
                j = k
            elif nxt in (ord("\r"), ord("\n")):  # a line continuation
                j += 3 if data[j + 1:j + 3] == b"\r\n" else 2
            else:
                out.append(nxt)
                j += 2
            continue
        if c == ord("("):
            depth += 1
        elif c == ord(")"):
            depth -= 1
            if depth == 0:
                return bytes(out), j + 1
        out.append(c)
        j += 1
    raise ValueError("unterminated string")


def _strings_in(raw: str) -> list[str]:
    """The text strings in an array as the library prints it, e.g. a multiple-choice field value."""
    data, out, i = raw.encode("latin-1", "replace"), [], 0
    while i < len(data):
        c = data[i]
        if c == ord("(") or (c == ord("<") and data[i + 1:i + 2] != b"<"):
            try:
                s, i = _read_string(data, i)
            except ValueError:
                break
            out.append(_text_of(s))
        else:
            i += 2 if data[i:i + 2] in (b"<<", b">>") else 1
    return out


def _pdf_array(items: list) -> str:
    return "[" + "".join(_pdf_array(x) if isinstance(x, (list, tuple)) else pymupdf.get_pdf_str(x) for x in items) + "]"


# ---- form fields ---------------------------------------------------------------------------------

def _field_entry(doc, xref: int, key: str) -> tuple[int, tuple[str, str]]:
    """(object holding it, (type, value)) of a form-field entry, looked up from the widget through its parents."""
    seen = set()
    while xref > 0 and xref not in seen:
        seen.add(xref)
        got = doc.xref_get_key(xref, key)
        if got[0] != "null":
            return xref, got
        parent = doc.xref_get_key(xref, "Parent")
        xref = int(parent[1].split()[0]) if parent[0] == "xref" else 0
    return 0, ("null", "null")


def _field_values(doc, widget) -> tuple[list[str], bool]:
    """The field's value(s), and whether it holds a list (the selections of a multiple-choice list box)."""
    kind, raw = _field_entry(doc, widget.xref, "V")[1]
    if kind == "array":
        return _strings_in(raw), True
    value = widget.field_value
    return ([value] if isinstance(value, str) else []), False


def _field_options(widget) -> list[list[str]]:
    """The choices of a list or combo box: [shown text] or [shown text, export value] each."""
    out = []
    for entry in widget.choice_values or []:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            out.append([str(entry[1]), str(entry[0])])
        elif isinstance(entry, str):
            out.append([entry])
    return out


def _rich_text(doc, kind: str, raw: str) -> str:
    """The words of a rich-text value (XHTML), as a string or a stream."""
    if kind == "xref":
        try:
            raw = _text_of(doc.xref_stream(int(raw.split()[0])) or b"")
        except Exception:
            return ""
    elif kind != "string":
        return ""
    return html.unescape(re.sub(r"<[^>]*>", "", raw))


def _same(a: str, b: str) -> bool:
    return " ".join(a.split()) == " ".join(b.split())


def _xfa_streams(doc) -> list[int]:
    kind, value = doc.xref_get_key(doc.pdf_catalog(), "AcroForm/XFA")
    return [int(x) for x in re.findall(r"(\d+)\s+\d+\s+R", value)] if kind in ("array", "xref") else []


def _drop_xfa(doc) -> None:
    """XFA keeps a second copy of every form value, which the fields themselves already hold."""
    cat = doc.pdf_catalog()
    if doc.xref_get_key(cat, "AcroForm/XFA")[0] != "null":
        kind, value = doc.xref_get_key(cat, "AcroForm")
        if kind == "xref":
            doc.xref_set_key(int(value.split()[0]), "XFA", "null")
        else:
            doc.xref_set_key(cat, "AcroForm/XFA", "null")
    if doc.xref_get_key(cat, "NeedsRendering")[0] != "null":
        doc.xref_set_key(cat, "NeedsRendering", "null")


# ---- tagged PDF ----------------------------------------------------------------------------------

def _struct_texts(doc) -> list[tuple[int, str, str]]:
    """(object, key, text) for the text entries of every structure element."""
    out = []
    for x in range(1, doc.xref_length()):
        try:
            keys = doc.xref_get_keys(x)
        except Exception:
            continue
        if "S" not in keys or "P" not in keys or "Subtype" in keys or "Rect" in keys or \
                doc.xref_get_key(x, "Type")[1] not in ("null", "/StructElem"):
            continue
        for key in STRUCT_KEYS:
            if key in keys:
                kind, value = doc.xref_get_key(x, key)
                if kind == "string" and value.strip():
                    out.append((x, key, value))
    return out


def _marked_entries(data: bytes) -> list[tuple[int, int, int, bytes, bytes, int]]:
    """(key start, value start, value end, key, value bytes, string bytes before it) of each /ActualText or /Alt
    string inside a dictionary of a content stream, i.e. in the properties of marked content, and a last entry
    (len(data), ..., b"", b"", string bytes in all). Strings, comments and inline image data are stepped over, so the
    same bytes inside them are not taken for entries; the string bytes bound how many glyphs can be drawn."""
    out, i, n, depth, shown = [], 0, len(data), 0, 0
    try:
        while i < n:
            c = data[i]
            if c in _SPACE:
                i += 1
            elif c == 0x25:  # % comment
                while i < n and data[i] not in (10, 13):
                    i += 1
            elif c == 0x28:  # ( literal string
                s, i = _read_string(data, i)
                shown += len(s)
            elif c == 0x3C:  # << dictionary, or <hex string>
                if data[i + 1:i + 2] == b"<":
                    depth, i = depth + 1, i + 2
                else:
                    s, i = _read_string(data, i)
                    shown += len(s)
            elif c == 0x3E:
                if data[i + 1:i + 2] == b">":
                    depth = max(0, depth - 1)
                i += 2 if data[i + 1:i + 2] == b">" else 1
            elif c in b"[]{}":
                i += 1
            elif c == 0x2F:  # /name
                j = i + 1
                while j < n and data[j] not in _DELIMS:
                    j += 1
                if depth and data[i + 1:j] in MARKED_KEYS:
                    k = j
                    while k < n and data[k] in _SPACE:
                        k += 1
                    if k < n and (data[k] == 0x28 or (data[k] == 0x3C and data[k + 1:k + 2] != b"<")):
                        value, end = _read_string(data, k)
                        out.append((i, k, end, data[i + 1:j], value, shown))
                        j = end
                i = j
            else:  # an operator or a number
                j = i
                while j < n and data[j] not in _DELIMS:
                    j += 1
                if data[i:j] == b"ID":  # inline image: binary data up to EI
                    m = _INLINE_IMAGE_END.search(data, j + 1)
                    j = m.end() if m else n
                i = max(j, i + 1)
    except (ValueError, IndexError):
        shown += n - i  # a broken stream: what was found so far is all that can be found; the rest may be glyphs
    return out + [(n, n, n, b"", b"", shown)]


def _hex_text(text: str) -> bytes:
    return b"<FEFF" + text.encode("utf-16-be").hex().upper().encode("ascii") + b">"


def _wide(c: str) -> bool:
    return unicodedata.east_asian_width(c) in "WF"


def _shown(page, bbox) -> bool:
    """Whether a box (unrotated page space, origin at the crop box corner) reaches into the visible page."""
    x0, y0, x1, y1 = bbox
    return x1 >= 0 and y1 >= 0 and x0 <= page.cropbox.width and y0 <= page.cropbox.height


def _pages(numbers: list[int]) -> str:
    shown = "、".join(str(n) for n in numbers[:10])
    return f"第 {shown} 頁" if len(numbers) <= 10 else f"第 {shown}… 頁等 {len(numbers)} 頁"


class _LineRect(pymupdf.Rect):
    """The box of a stretch of one text line. `across` is the axis that runs across the line: "y" for text
    running left to right, "x" for a line turned a quarter, None for one that slants. A redaction is pulled in
    on that axis only (_shrunk): pulled in along the line it would leave the first and last character in.
    `glyphs` are the boxes of the characters the stretch is made of; `pulled`, when set, is the box to redact
    instead of the usual one: pulled in further, to spare the lines beside it (_spare)."""

    def __init__(self, rect, across, glyphs=()):
        super().__init__(rect)
        self.across = across
        self.glyphs = list(glyphs)
        self.pulled = None


def _core(glyph) -> tuple[float, float, float, float]:
    """The part of a glyph's box a redaction has to reach for the glyph to be taken (see TAKEN). Boxes here are
    plain (x0, y0, x1, y1): a page can hold thousands of glyphs and each is looked at for every masked stretch."""
    dx, dy = (glyph[2] - glyph[0]) * TAKEN, (glyph[3] - glyph[1]) * TAKEN
    return glyph[0] + dx, glyph[1] + dy, glyph[2] - dx, glyph[3] - dy


def _meets(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _reached(glyph, rect) -> bool:
    """True when a redaction of `rect` takes this glyph. A glyph whose box is flat has no part to reach: it
    counts as soon as the rectangle touches it."""
    if glyph[2] > glyph[0] and glyph[3] > glyph[1]:
        return _meets(_core(glyph), rect)
    return glyph[0] <= rect[2] and rect[0] <= glyph[2] and glyph[1] <= rect[3] and rect[1] <= glyph[3]


def _glyphs(page) -> list[tuple[str, tuple]]:
    """Every character the page shows that is not white space, with its box."""
    return [(c["c"], tuple(c["bbox"]))
            for block in page.get_text("rawdict", flags=CHECK_FLAGS, clip=pymupdf.INFINITE_RECT())["blocks"]
            for line in block.get("lines", []) for span in line["spans"] for c in span["chars"] if c["c"].strip()]


@dataclass
class _Unit:
    # page | offpage (text placed outside the page) | annot | widget | widget_opt | widget_dv | widget_rv | link | toc |
    # struct (text of a structure element) | mc_alt (alternate text of marked content)
    kind: str
    page: int = -1
    chars: list = field(default_factory=list)  # page, offpage: (Rect | None, line number) per character of the text
    # page, offpage: line number -> the axis across a line that does not run left to right: "x" for one turned a
    # quarter (or written top to bottom), None for one that slants
    across: dict = field(default_factory=dict)
    xref: int = 0
    index: int = 0


@dataclass
class _Doc:
    inv: Inventory
    units: dict
    metas: dict  # meta id -> ("info", document-information key) | ("annot_author", name)


SCRIPT_KEYS = ("/JavaScript", "/JS")


def _has_scripts(doc) -> bool:
    for xref in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(xref, compressed=True)
        except Exception:
            continue
        if any(key + c in obj for key in SCRIPT_KEYS for c in "/ (<[\n\r\t"):
            return True
    return False


def _info_xref(doc) -> int:
    kind, value = doc.xref_get_key(-1, "Info")
    if kind == "xref":
        try:
            return int(value.split()[0])
        except (ValueError, IndexError):
            return 0
    return 0


def _info_entries(doc) -> list[tuple[str, str]]:
    """(key, text) for every non-blank text entry of the document information, the usual ones first."""
    ix = _info_xref(doc)
    if not ix:
        md = doc.metadata or {}
        return [(key, md[name]) for key, name in INFO_NAMES.items() if (md.get(name) or "").strip()]
    keys = doc.xref_get_keys(ix)
    out = []
    for key in [k for k in INFO_FIELDS if k in keys] + [k for k in keys if k not in INFO_FIELDS and k not in INFO_SKIP]:
        kind, value = doc.xref_get_key(ix, key)
        if kind == "string" and value.strip():
            out.append((key, value))
    return out


def _write_info(doc, changes: dict[str, str]) -> None:
    if not changes:
        return
    ix = _info_xref(doc)
    if ix:
        for key, value in changes.items():
            doc.xref_set_key(ix, key, pymupdf.get_pdf_str(value) if value else "null")
        return
    md = dict(doc.metadata or {})
    md.update({INFO_NAMES[k]: v for k, v in changes.items() if k in INFO_NAMES})
    doc.set_metadata({k: v for k, v in md.items() if k in (*INFO_NAMES.values(), "creationDate", "modDate", "trapped")})


def _drop_side_copies(doc) -> None:
    """Remove what keeps a second copy of content outside the page: page thumbnails (a picture of the
    page as it was), per-object XMP and editor-private data, and the metadata inside embedded photos."""
    for xref in range(1, doc.xref_length()):
        try:
            keys = doc.xref_get_keys(xref)
        except Exception:
            continue
        for key in ("Thumb", "Metadata", "PieceInfo"):
            if key in keys:
                doc.xref_set_key(xref, key, "null")
        if "Filter" in keys and doc.xref_is_stream(xref) and doc.xref_get_key(xref, "Filter")[1] in ("/DCTDecode", "[/DCTDecode]"):
            raw = doc.xref_stream_raw(xref)
            clean = imaging.strip_metadata(raw)
            if clean is not raw:
                doc.update_stream(xref, clean, compress=False)  # this drops the filter entry: put it back
                doc.xref_set_key(xref, "Filter", "/DCTDecode")


class PdfAdapter:
    kind = "pdf"

    def __init__(self, dpi: int = 200):
        self.dpi = dpi
        self.zoom = dpi / 72
        self._cache: dict = {}
        self._visible: dict = {}  # file key -> bytes of the copy with every layer visible

    # ---- helpers -------------------------------------------------------------------------

    @staticmethod
    def _usable(doc: pymupdf.Document) -> pymupdf.Document:
        if not doc.is_pdf:
            doc.close()
            raise BrokenFile("檔案不是有效的 PDF")
        if doc.needs_pass and not doc.authenticate(""):
            doc.close()
            raise BrokenFile("PDF 有密碼保護，請移除密碼後再上傳")
        if doc.page_count == 0:
            doc.close()
            raise BrokenFile("PDF 沒有任何頁面")
        return doc

    def _open_as_is(self, src: Path) -> pymupdf.Document:
        """The file as it is, optional layers in whatever state the author left them."""
        try:
            doc = pymupdf.open(src, filetype="pdf")
        except Exception:
            raise BrokenFile("檔案不是有效的 PDF") from None
        return self._usable(doc)

    def _open(self, src: Path) -> pymupdf.Document:
        """The file with every optional layer visible. Content on a switched-off layer is still in the
        file, so it is read, shown and checked like everything else. Never saved: `apply` works on the
        file as it is and the layers keep their state."""
        doc = self._open_as_is(src)
        try:
            layered = bool(doc.get_ocgs())
        except Exception:
            layered = False
        if not layered:
            return doc
        st = Path(src).stat()
        key = (str(src), st.st_mtime_ns, st.st_size)
        if key not in self._visible:
            # Dropping the layer table is the one way that reliably shows everything: switching layers on
            # through the library leaves content governed by usage rules or membership policies hidden.
            doc.xref_set_key(doc.pdf_catalog(), "OCProperties", "null")
            self._visible = {key: doc.tobytes()}
        doc.close()
        try:
            return self._usable(pymupdf.open(stream=self._visible[key], filetype="pdf"))
        except BrokenFile:
            raise
        except Exception:
            raise BrokenFile("檔案不是有效的 PDF") from None

    def _to_px(self, page, rect) -> Box:
        r = pymupdf.Rect(rect) * page.rotation_matrix
        r.normalize()
        return (r.x0 * self.zoom, r.y0 * self.zoom, r.x1 * self.zoom, r.y1 * self.zoom)

    def _from_px(self, page, box: Box) -> pymupdf.Rect:
        r = pymupdf.Rect(box[0] / self.zoom, box[1] / self.zoom, box[2] / self.zoom, box[3] / self.zoom) * page.derotation_matrix
        r.normalize()
        return r

    def _size(self, page) -> tuple[int, int]:
        r = (page.rect * pymupdf.Matrix(self.zoom, self.zoom)).irect
        return r.width, r.height

    def _load(self, src: Path) -> _Doc:
        src = Path(src)
        st = src.stat()
        key = (str(src), st.st_mtime_ns, st.st_size)
        if key not in self._cache:
            self._cache.clear()
            self._cache[key] = self._parse(src)
        return self._cache[key]

    @staticmethod
    def _line_rects(chars: list, start: int, end: int, across: dict | None = None) -> list[pymupdf.Rect]:
        """One rectangle per text line for the characters in [start, end). `across` (a unit's) says which lines
        do not run left to right."""
        out: dict[int, pymupdf.Rect] = {}
        glyphs: dict[int, list] = {}
        for rect, line in chars[start:end]:
            if rect is None:
                continue
            if line in out:
                out[line] |= rect
            else:
                out[line] = pymupdf.Rect(rect)
            glyphs.setdefault(line, []).append(rect)
        return [_LineRect(out[k], (across or {}).get(k, "y"), glyphs[k]) for k in sorted(out)]

    # ---- reading -------------------------------------------------------------------------

    def _parse(self, src: Path) -> _Doc:
        doc = self._open(src)
        try:
            inv = Inventory(kind="pdf")
            units: dict[str, _Unit] = {}
            metas: dict[str, tuple] = {}

            def add_text(text: str, section: str, page: int | None, unit: _Unit, breaks=()):
                uid = f"t{len(units) + 1}"
                units[uid] = unit
                inv.texts.append(TextUnit(id=uid, text=text, section=section, page=page, breaks=list(breaks)))

            authors: list[str] = []
            widgets = 0
            unreadable_pages: list[int] = []
            for n, page in enumerate(doc):
                line_no = 0
                has_text = unreadable = unreadable_outside = False
                for block in page.get_text("rawdict", flags=TEXT_FLAGS, clip=pymupdf.INFINITE_RECT())["blocks"]:
                    if block.get("type") != 0:
                        continue
                    parts = {True: ("", [], [], {}), False: ("", [], [], {})}  # lines on the page, lines outside it
                    for line in block["lines"]:
                        glyphs = [(c["c"], pymupdf.Rect(c["bbox"])) for span in line["spans"] for c in span["chars"]]
                        if not glyphs:
                            continue
                        line_no += 1
                        shown = _shown(page, line["bbox"])
                        text, chars, breaks, across = parts[shown]
                        dx, dy = line.get("dir") or (1.0, 0.0)
                        if line.get("wmode") == 1 or abs(dx) < 0.1:
                            across[line_no] = "x"  # turned a quarter, or written top to bottom
                        elif abs(dy) >= 0.1:
                            across[line_no] = None  # slanted
                        if text and not (_wide(text[-1]) and _wide(glyphs[0][0])) and not text[-1].isspace():
                            # Between two words a line end stands for a space; inside a string the layout broke
                            # (a mailbox in a narrow cell) it stands for nothing. Noted, so a search takes both.
                            breaks.append(len(text))
                            text += " "
                            chars.append((None, line_no))
                        for c, rect in glyphs:
                            text += c
                            chars += [(rect, line_no)] * len(c)
                        parts[shown] = (text, chars, breaks, across)
                    text, chars, breaks, across = parts[True]
                    unreadable = unreadable or UNREADABLE in text
                    if text.replace(UNREADABLE, "").strip():  # glyphs nobody can read are not sent for detection
                        has_text = True
                        add_text(text, f"第 {n + 1} 頁", n, _Unit("page", page=n, chars=chars, across=across), breaks)
                    text, chars, breaks, across = parts[False]
                    unreadable_outside = unreadable_outside or UNREADABLE in text
                    if text.replace(UNREADABLE, "").strip():  # not on the page picture: listed as text of its own
                        add_text(text, f"{HIDDEN}第 {n + 1} 頁頁面外的文字", n, _Unit("offpage", page=n, chars=chars, across=across), breaks)
                if unreadable_outside:  # no picture to read it from
                    inv.unchecked.append(Unchecked(f"第 {n + 1} 頁頁面外的文字", "字型缺少字碼對照，無法讀出，未檢查"))
                for x in page.get_contents():  # alternate text given to marked content on the page itself
                    data = doc.xref_stream(x) or b""
                    for _s, _v, _e, key, value, _n in (_marked_entries(data) if b"/Alt" in data else []):
                        if key == b"Alt" and _text_of(value).strip():
                            add_text(_text_of(value), STRUCT_SECTIONS["Alt"], n, _Unit("mc_alt", page=n))
                for annot in page.annots() or []:
                    info = annot.info
                    if (info.get("content") or "").strip():
                        add_text(info["content"], "註解", n, _Unit("annot", page=n, xref=annot.xref))
                    name = (info.get("title") or "").strip()
                    if name and name not in authors:
                        authors.append(name)
                    if annot.type[0] == pymupdf.PDF_ANNOT_FILE_ATTACHMENT:
                        inv.unchecked.append(Unchecked(f"第 {n + 1} 頁的附件註解", "附件，未檢查"))
                for widget in page.widgets() or []:
                    widgets += 1
                    values, _many = _field_values(doc, widget)
                    for i, value in enumerate(values):
                        if value.strip():
                            add_text(value, "表單欄位", n, _Unit("widget", page=n, xref=widget.xref, index=i))
                    for i, texts in enumerate(_field_options(widget)):  # a reader can open the list and see them all
                        for j, text in enumerate(texts):  # [shown text, export value]: only the first is seen
                            if text.strip() and not (j and text == texts[0]):
                                add_text(text, f"{HIDDEN}表單選項的匯出值" if j else "表單選項", n,
                                         _Unit("widget_opt", page=n, xref=widget.xref, index=2 * i + j))
                    kind, raw = _field_entry(doc, widget.xref, "DV")[1]  # what a form reset puts back
                    defaults = _strings_in(raw) if kind == "array" else [raw] if kind == "string" else []
                    if defaults != values:
                        for i, text in enumerate(defaults):
                            if text.strip():
                                add_text(text, f"{HIDDEN}表單欄位預設值", n, _Unit("widget_dv", page=n, xref=widget.xref, index=i))
                    rich = _rich_text(doc, *_field_entry(doc, widget.xref, "RV")[1])
                    if rich.strip() and not _same(rich, values[0] if values else ""):
                        add_text(rich, f"{HIDDEN}表單欄位的格式化內容", n, _Unit("widget_rv", page=n, xref=widget.xref))
                for i, link in enumerate(page.get_links()):
                    target = link.get("uri") or link.get("file") or ""
                    if target.strip():  # the address behind a link is not visible on the page
                        add_text(target, "連結位址", n, _Unit("link", page=n, index=i))
                crop, media = page.cropbox, page.mediabox
                if crop.width < media.width - 1 or crop.height < media.height - 1:
                    inv.unchecked.append(Unchecked(f"第 {n + 1} 頁裁切範圍外的內容",
                                                   "頁面有裁切，範圍外的圖片未檢查（範圍外的文字有列出檢查）"))
                w, h = self._size(page)
                areas = []
                for info in page.get_image_info():
                    box = self._to_px(page, info["bbox"])
                    box = (max(0.0, box[0]), max(0.0, box[1]), min(float(w), box[2]), min(float(h), box[3]))
                    if box[2] - box[0] >= MIN_AREA_PX and box[3] - box[1] >= MIN_AREA_PX and box not in areas:
                        areas.append(box)
                # A page with no text layer at all may be a scan drawn some other way: read it too. So is a page with
                # glyphs the text layer cannot name: what they say is only known from the picture.
                if unreadable:
                    unreadable_pages.append(n + 1)
                inv.images.append(ImageUnit(id=f"pg{n + 1}", name=f"第 {n + 1} 頁", width=w, height=h, supported=True, page=n,
                                            areas=areas, needs_scan=bool(areas) or not has_text or unreadable))
            if unreadable_pages:
                inv.notes.append(f"{_pages(unreadable_pages)}有文字的字型缺少字碼對照，無法從文字層讀出，"
                                 "這些文字改用影像辨識檢查，遮蔽時以方框塗黑。")
            for i, entry in enumerate(doc.get_toc(simple=True)):
                if str(entry[1]).strip():
                    add_text(str(entry[1]), "書籤", None, _Unit("toc", index=i))
            for x, key, value in _struct_texts(doc):  # what a screen reader says instead of (or about) the page
                add_text(value, STRUCT_SECTIONS[key], None, _Unit("struct", xref=x, index=STRUCT_KEYS.index(key)))

            def add_meta(label, value, person, where):
                mid = f"m{len(metas) + 1}"
                metas[mid] = where
                inv.metas.append(MetaUnit(id=mid, label=label, value=value, person=person))

            for key, value in _info_entries(doc):
                label, person = INFO_FIELDS.get(key, (f"自訂屬性：{key}", False))
                add_meta(label, value, person, ("info", key))
            for name in authors:
                add_meta("註解作者", name, True, ("annot_author", name))
            raw = self._open_as_is(src)
            try:
                hidden = sum(1 for g in raw.get_ocgs().values() if not g.get("on"))
            except Exception:
                hidden = 0
            finally:
                raw.close()
            if hidden:
                inv.notes.append(f"這份 PDF 有 {hidden} 個隱藏圖層。隱藏的內容也在檔案裡，所以一併檢查，並顯示在檢查畫面上；"
                                 "下載的檔案中這些圖層仍維持隱藏。")
            if _has_scripts(doc):
                inv.unchecked.append(Unchecked("JavaScript", "文件內含指令碼，內容未檢查"))
            if _xfa_streams(doc) and not widgets:  # with fields beside it, XFA only repeats what they hold
                inv.unchecked.append(Unchecked("XFA 表單", "這種表單的欄位內容無法讀取，未檢查；下載的檔案會移除這份表單資料"))
            for i in range(doc.embfile_count()):
                name = str(doc.embfile_info(i).get("filename") or i)
                try:  # the library hands back UTF-8 names decoded as Latin-1
                    name = name.encode("latin-1").decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
                inv.unchecked.append(Unchecked(f"內嵌檔案：{name}", "附件，未檢查"))
            return _Doc(inv=inv, units=units, metas=metas)
        finally:
            doc.close()

    def parse(self, src: Path) -> Inventory:
        return inventory_from_json(to_json(self._load(src).inv))

    def image_bytes(self, src: Path, image_id: str) -> bytes:
        doc = self._open(src)
        try:
            return doc[int(image_id[2:]) - 1].get_pixmap(matrix=pymupdf.Matrix(self.zoom, self.zoom), alpha=False).tobytes("png")
        finally:
            doc.close()

    def _page_char_boxes(self, src: Path, page_no: int) -> list[Box]:
        parsed = self._load(src)
        texts = {t.id: t.text for t in parsed.inv.texts}
        doc = self._open(src)
        try:
            page = doc[page_no]
            return [self._to_px(page, rect) for uid, u in parsed.units.items() if u.kind == "page" and u.page == page_no
                    for (rect, _), c in zip(u.chars, texts[uid]) if rect is not None and c != UNREADABLE]
        finally:
            doc.close()

    def filter_ocr(self, src: Path, image_id: str, lines: list[OcrLine]) -> list[OcrLine]:
        """Drop OCR lines that only re-read text the page already has as a text layer."""
        chars = self._page_char_boxes(src, int(image_id[2:]) - 1)
        if not chars:
            return lines
        kept = []
        for ln in lines:
            x0, y0, x1, y1 = ln.box
            area = max(1e-6, (x1 - x0) * (y1 - y0))
            covered = 0.0
            for c in chars:
                iw, ih = min(x1, c[2]) - max(x0, c[0]), min(y1, c[3]) - max(y0, c[1])
                if iw > 0 and ih > 0:
                    covered += iw * ih
            if covered / area < 0.5:
                kept.append(ln)
        return kept

    def locate(self, src: Path, unit_id: str, start: int, end: int) -> list[tuple[str, Box]]:
        unit = self._load(src).units.get(unit_id)
        if unit is None or unit.kind != "page":
            return []
        doc = self._open(src)
        try:
            page = doc[unit.page]
            return [(f"pg{unit.page + 1}", self._to_px(page, r)) for r in self._line_rects(unit.chars, start, end)]
        finally:
            doc.close()

    def view(self, src: Path, inv: Inventory) -> list[dict]:
        blocks = [{"type": "page", "image": im.id, "page": (im.page or 0) + 1} for im in inv.images]
        units = self._load(src).units
        blocks += [{"type": "text", "unit": t.id, "section": t.section} for t in inv.texts
                   if t.id in units and units[t.id].kind != "page"]
        return blocks

    # ---- writing -------------------------------------------------------------------------

    def _redactions(self, parsed: _Doc, plan: MaskPlan, doc) -> dict[int, list[pymupdf.Rect]]:
        """Full (unshrunk) rectangles to redact, per page, in unrotated page coordinates."""
        out: dict[int, list[pymupdf.Rect]] = {}
        going: dict[str, set[int]] = {}
        for uid, spans in plan.text.items():
            unit = parsed.units.get(uid)
            if unit is None or unit.kind not in GLYPH_KINDS:
                continue
            for s, e in spans:
                out.setdefault(unit.page, []).extend(self._line_rects(unit.chars, s, e, unit.across))
                going.setdefault(uid, set()).update(range(s, e))
        for n, rects in out.items():
            staying = [tuple(rect) for uid, unit in parsed.units.items() if unit.kind in GLYPH_KINDS and unit.page == n
                       for i, (rect, _line) in enumerate(unit.chars) if rect is not None and i not in going.get(uid, ())]
            staying = [(g, _core(g)) for g in staying if g[2] > g[0] and g[3] > g[1]]
            for rect in rects:
                self._spare(rect, staying)
        for image_id, boxes in plan.boxes.items():
            n = int(image_id[2:]) - 1
            if 0 <= n < doc.page_count:
                out.setdefault(n, []).extend(self._from_px(doc[n], b) for b in boxes)
        return out

    @staticmethod
    def _spare(rect: _LineRect, staying: list) -> None:
        """Pull the box of a masked stretch in further where the usual amount would still take glyphs of the
        lines beside it: lines set closer than their glyph boxes are high, a caption right under a large name.
        Only as far as every glyph of the stretch itself is still taken; where that cannot be had, the usual
        box stands and the neighbours go, as they always did. `staying` holds (box, core) of every glyph on the
        page that is not being masked."""
        if rect.across not in ("x", "y") or rect.is_empty:
            return
        lo, hi = (1, 3) if rect.across == "y" else (0, 2)  # the sides that are pulled in
        box = tuple(PdfAdapter._shrunk(rect))
        low, high, middle = box[lo], box[hi], (rect[lo] + rect[hi]) / 2
        changed = False
        for glyph, core in staying:
            if not _meets(core, box):
                continue
            centre = (glyph[lo] + glyph[hi]) / 2
            if rect[lo] <= centre <= rect[hi] and core[lo] < middle < core[hi]:
                continue  # its box lies over the stretch itself: no pulling in spares it
            if centre < middle:
                low, changed = max(low, core[hi] + CLEAR), True
            else:
                high, changed = min(high, core[lo] - CLEAR), True
        if not changed or high - low < CLEAR:
            return
        pulled = list(box)
        pulled[lo], pulled[hi] = low, high
        if all(_reached(tuple(g), pulled) for g in rect.glyphs):
            rect.pulled = pymupdf.Rect(pulled)

    @staticmethod
    def _shrunk(rect: pymupdf.Rect) -> pymupdf.Rect:
        """The rectangle pulled in across its text line, so that the lines beside it keep their glyphs."""
        if getattr(rect, "pulled", None) is not None:
            return pymupdf.Rect(rect.pulled)
        across = getattr(rect, "across", "y")
        if across == "x":
            dx = rect.width * SHRINK
            return pymupdf.Rect(rect.x0 + dx, rect.y0, rect.x1 - dx, rect.y1)
        if across is None:  # a slanted line: its box is not a strip along it, there is no side to pull in
            return pymupdf.Rect(rect)
        dy = rect.height * SHRINK
        return pymupdf.Rect(rect.x0, rect.y0 + dy, rect.x1, rect.y1 - dy)

    def _marked_content(self, doc, n: int, rects: list, mask_alt, pattern, found) -> None:
        """Replacement text (/ActualText) stands in for the glyphs it wraps when text is copied or read aloud;
        alternate text (/Alt) describes them. Where a redaction will take glyphs away, their replacement text would
        give them back, so it is removed there and the glyphs left speak for themselves. Elsewhere both are masked
        like any other text."""
        page = doc[n]
        streams = []
        for x in page.get_contents():
            data = doc.xref_stream(x) or b""
            entries = _marked_entries(data) if b"/ActualText" in data or b"/Alt" in data else []
            if len(entries) > 1:  # more than the closing count
                streams.append((x, data, entries))
        actual = [(si, ei) for si, (_x, _d, entries) in enumerate(streams)
                  for ei, e in enumerate(entries) if e[3] == b"ActualText"]
        touched = self._touched(doc, page, streams, actual, rects) if rects and actual else set()
        for si, (x, data, entries) in enumerate(streams):
            out = bytearray(data)
            for ei in range(len(entries) - 2, -1, -1):
                start, vstart, vend, key, value, _shown = entries[ei]
                text = _text_of(value)
                if key == b"ActualText" and (si, ei) in touched:
                    del out[start:vend]
                    continue
                new = mask_alt(text) if key == b"Alt" else pattern.sub(found, text) if pattern else text
                if new != text:
                    out[vstart:vend] = _hex_text(new)
            if bytes(out) != data:
                doc.update_stream(x, bytes(out))
        kind, value = doc.xref_get_key(page.xref, "Resources/Properties")  # property lists named from the content
        for xr in (int(v) for v in re.findall(r"(\d+)\s+\d+\s+R", value)) if kind == "dict" else ():
            keys = doc.xref_get_keys(xr)
            for key in ("ActualText", "Alt"):
                got = doc.xref_get_key(xr, key) if key in keys else ("null", "")
                if got[0] != "string":
                    continue
                if key == "ActualText" and rects:  # where its glyphs are is not measured: removed to be safe
                    doc.xref_set_key(xr, key, "null")
                    continue
                new = mask_alt(got[1]) if key == "Alt" else pattern.sub(found, got[1]) if pattern else got[1]
                if new != got[1]:
                    doc.xref_set_key(xr, key, pymupdf.get_pdf_str(new))

    @staticmethod
    def _touched(doc, page, streams, actual, rects) -> set:
        """Which replacement texts wrap glyphs that a redaction rectangle meets. Each is swapped for a private-use
        character for a moment; the text layer then shows where that character's glyphs are."""
        last = 6399  # replacement texts past this many on one page share a marker and count as met
        marker = {pos: chr(MARKER + min(i, last)) for i, pos in enumerate(actual)}
        for si, (x, data, entries) in enumerate(streams):
            # The text layer gives the n-th character of a replacement text the place of the n-th glyph it wraps,
            # so a marker is repeated at least as often as there are glyphs: no more than the string bytes drawn
            # before the next replacement text.
            ats = [ei for ei, e in enumerate(entries) if e[3] == b"ActualText"] + [len(entries) - 1]
            out = bytearray(data)
            for k in range(len(ats) - 2, -1, -1):
                start, vstart, vend, _key, _value, shown = entries[ats[k]]
                out[vstart:vend] = _hex_text(marker[(si, ats[k])] * (entries[ats[k + 1]][5] - shown + 1))
            doc.update_stream(x, bytes(out))
        seen: dict[str, pymupdf.Rect] = {}
        try:
            for block in page.get_text("rawdict", flags=TEXT_FLAGS, clip=pymupdf.INFINITE_RECT())["blocks"]:
                for line in block.get("lines", []):
                    for c in (c for span in line["spans"] for c in span["chars"]):
                        if c["c"] and MARKER <= ord(c["c"][0]) <= MARKER + last:
                            box = pymupdf.Rect(c["bbox"])
                            seen[c["c"][0]] = seen[c["c"][0]] | box if c["c"][0] in seen else box
        finally:
            for x, data, _entries in streams:
                doc.update_stream(x, data)

        def meets(a, b) -> bool:  # touching counts: a glyph box can be flat
            return a.x0 <= b.x1 and b.x0 <= a.x1 and a.y0 <= b.y1 and b.y0 <= a.y1

        hit = {ch for ch, box in seen.items() if any(meets(box, r) for r in rects)} | {chr(MARKER + last)}
        return {pos for pos, ch in marker.items() if ch in hit}

    def _redact(self, page, rects: list, from_text: int, only: set | None = None) -> None:
        """Redact the rectangles (the first `from_text` come from text and are shrunk), then paint them black."""
        chosen = [(i, r) for i, r in enumerate(rects)
                  if (only is None or i in only) and not r.is_empty and not r.is_infinite]
        # The rectangles are in the page's unrotated coordinates, and the page is worked on unrotated: on a page
        # that is both turned and cropped the library takes the right glyphs out but paints the black fill and
        # anything drawn after it somewhere else (measured with PyMuPDF 1.28: off by what the crop cuts from the
        # left and the bottom). Unrotated it paints where it redacts; the rotation is put back afterwards.
        turned = page.rotation
        if turned:
            page.set_rotation(0)
        try:
            for i, rect in chosen:
                target = self._shrunk(rect) if i < from_text else rect
                if not target.is_empty:
                    page.add_redact_annot(target, fill=(0, 0, 0), cross_out=False)
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS)
            shape = page.new_shape()  # cover the slivers the shrunk redaction left above and below
            for _i, rect in chosen:
                shape.draw_rect(rect)
            shape.finish(color=None, fill=(0, 0, 0), width=0)
            shape.commit(overlay=True)
        finally:
            if turned:
                page.set_rotation(turned)

    def _uncover(self, doc, n: int, rects: list, from_text: int) -> None:
        """Annotations are drawn over the page, so over its black boxes too. One that still shows through a box
        (an image stamp, a note icon) is made part of the page content where it is, and the boxes it meets are
        redacted again: its pixels and glyphs under them leave the file like the page's own."""
        page = doc[n]
        skip = (pymupdf.PDF_ANNOT_LINK, pymupdf.PDF_ANNOT_POPUP, pymupdf.PDF_ANNOT_REDACT)

        def drawn(p):  # (key, object, is a form field) for everything drawn over the page
            where = lambda o: tuple(round(v, 2) for v in o.rect)  # noqa: E731
            return ([((a.type[0], where(a)), a, False) for a in p.annots() or [] if a.type[0] not in skip]
                    + [(("field", where(w)), w, True) for w in p.widgets() or []])

        candidates = [c for c in drawn(page) if any(c[1].rect.intersects(r) for r in rects)]
        if not candidates:
            return
        seen = imaging.for_black_check(page.get_pixmap(matrix=pymupdf.Matrix(self.zoom, self.zoom), alpha=False).tobytes("png"))
        failing = [r for r in rects if _shown(page, r) and not imaging.is_black(seen, self._to_px(page, r))]
        targets = [c for c in candidates if any(c[1].rect.intersects(r) for r in failing)]
        if not targets:
            return
        keys = {c[0] for c in targets}
        again = {i for i, r in enumerate(rects) if any(c[1].rect.intersects(r) for c in targets)}
        tmp = pymupdf.open()
        try:
            tmp.insert_pdf(doc, from_page=n, to_page=n, links=False, annots=True, widgets=True)
            copy = tmp[0]
            for k, obj, field_ in drawn(copy):
                if k in keys:
                    continue
                if field_:
                    copy.delete_widget(obj)
                else:
                    copy.delete_annot(obj)
            for x in copy.get_contents():
                tmp.update_stream(x, b"")
            tmp.bake(annots=True, widgets=True)  # now the page holds nothing but their pictures
            turn = page.rotation
            page.set_rotation(0)  # same box, same unrotated space: the copy lands exactly where the originals were
            copy.set_rotation(0)
            try:
                page.show_pdf_page(page.rect, tmp, 0, overlay=True)
            except ValueError:
                pass  # they draw nothing after all: removing them below is all it takes
            finally:
                page.set_rotation(turn)
        finally:
            tmp.close()
        for _k, obj, field_ in targets:  # objects are not used after this: a deleted one cannot be read
            if field_:
                page.delete_widget(obj)
            else:
                page.delete_annot(obj)
        self._redact(page, rects, from_text, only=again)

    @staticmethod
    def _mask_field(doc, widget, spans, masked, pattern) -> None:
        """Mask a form field everywhere it keeps text: its value or selections, its list of choices, its default
        value, and its rich-text copy; then draw the field again from what is left."""
        xref = widget.xref
        values, many = _field_values(doc, widget)
        new_values = [masked(v, spans("widget", i)) for i, v in enumerate(values)]
        options = _field_options(widget)
        new_options = []
        for i, texts in enumerate(options):
            shown = masked(texts[0], spans("widget_opt", 2 * i))
            if len(texts) == 1:
                new_options.append(shown)
            else:  # stored as [export value, shown text]
                export = shown if texts[1] == texts[0] else masked(texts[1], spans("widget_opt", 2 * i + 1))
                new_options.append([export, shown])
        old_options = [t[0] if len(t) == 1 else [t[1], t[0]] for t in options]
        if new_options != old_options:
            doc.xref_set_key(_field_entry(doc, xref, "Opt")[0] or xref, "Opt", _pdf_array(new_options))
        holder, (kind, raw) = _field_entry(doc, xref, "DV")
        if kind in ("string", "array"):
            defaults = _strings_in(raw) if kind == "array" else [raw]
            new_defaults = (new_values if defaults == values  # a reset puts the value back: masked the same way
                            else [masked(d, spans("widget_dv", i)) for i, d in enumerate(defaults)])
            if new_defaults != defaults:
                doc.xref_set_key(holder, "DV", _pdf_array(new_defaults) if kind == "array"
                                 else pymupdf.get_pdf_str(new_defaults[0]))
        holder, (kind, raw) = _field_entry(doc, xref, "RV")
        if kind != "null":
            rich = _rich_text(doc, kind, raw)
            if new_values != values or spans("widget_rv") or (pattern and pattern.search(rich)):
                doc.xref_set_key(holder, "RV", "null")  # the plain value is what is left; viewers show it instead
                fh, (fk, fv) = _field_entry(doc, xref, "Ff")
                if fk == "int" and int(fv) & RICH_TEXT_FLAG:
                    doc.xref_set_key(fh, "Ff", str(int(fv) & ~RICH_TEXT_FLAG))
        if new_values == values and new_options == old_options:
            return
        v_holder = _field_entry(doc, xref, "V")[0]
        if not many:
            if widget.choice_values is not None:
                widget.choice_values = new_options  # drawn from these, so they must be the masked ones
            if values:
                widget.field_value = new_values[0] or " "  # this library ignores an empty value and keeps the old one
            widget.update()
            if values and v_holder and v_holder != xref:  # the value lives on a parent field: mask it there too
                doc.xref_set_key(v_holder, "V", pymupdf.get_pdf_str(new_values[0] or " "))
            return
        doc.xref_set_key(v_holder or xref, "V", _pdf_array(new_values))
        try:  # the library cannot set a list value, so the field is redrawn from its entries directly
            annot = widget._annot.this
            pymupdf.mupdf.pdf_dirty_annot(annot)
            pymupdf.mupdf.pdf_update_annot(annot)
        except Exception:
            doc.xref_set_key(xref, "AP", "null")  # no stale picture of the old choices: viewers draw it from the values
            doc.xref_set_key(doc.pdf_catalog(), "AcroForm/NeedAppearances", "true")

    def apply(self, src: Path, dst: Path, plan: MaskPlan, settings: Settings) -> None:
        parsed = self._load(src)
        full, half = settings.mask_full, settings.mask_half
        pattern, found = searcher(plan.search(), full, half, plan.partial)  # for strings met by search
        doc = self._open_as_is(src)

        def masked(text: str, spans) -> str:
            marks = bytearray(len(text))
            for s, e in spans:
                for i in range(max(0, s), min(len(text), e)):
                    marks[i] = 1
            new = "".join(mask_chars(c, full, half) if marks[i] else c for i, c in enumerate(text))
            return pattern.sub(found, new) if pattern else new

        try:
            text_boxes = {uid for uid in plan.text if uid in parsed.units and parsed.units[uid].kind in GLYPH_KINDS}
            redactions = self._redactions(parsed, plan, doc)
            from_text = {n: sum(len(self._line_rects(parsed.units[u].chars, s, e)) for u in text_boxes
                                if parsed.units[u].page == n for s, e in plan.text[u]) for n in redactions}
            texts = {t.id: t.text for t in parsed.inv.texts}
            alt_spans: dict[tuple, list] = {}
            for uid, u in parsed.units.items():
                if u.kind == "mc_alt" and plan.text.get(uid):
                    alt_spans.setdefault((u.page, texts[uid]), []).extend(plan.text[uid])
            for n in range(doc.page_count):  # before the redactions, which rewrite the page content
                self._marked_content(doc, n, redactions.get(n, []), lambda t, n=n: masked(t, alt_spans.get((n, t), [])),
                                     pattern, found)
            for n, rects in redactions.items():
                self._redact(doc[n], rects, from_text[n])
            struct_spans = {(u.xref, u.index): plan.text[uid] for uid, u in parsed.units.items()
                            if u.kind == "struct" and plan.text.get(uid)}
            for x, key, value in _struct_texts(doc):
                new = masked(value, struct_spans.get((x, STRUCT_KEYS.index(key)), []))
                if new != value:
                    doc.xref_set_key(x, key, pymupdf.get_pdf_str(new))

            masked_authors = {where[1] for mid, where in parsed.metas.items()
                              if where[0] == "annot_author" and plan.meta.get(mid)}
            drop_annots = {(u.page, u.xref) for uid, u in parsed.units.items() if u.kind == "annot" and plan.text.get(uid)}
            field_spans = {(u.page, u.xref, u.kind, u.index): plan.text[uid] for uid, u in parsed.units.items()
                           if u.kind in WIDGET_KINDS and plan.text.get(uid)}

            drop_links = {(u.page, u.index) for uid, u in parsed.units.items() if u.kind == "link" and plan.text.get(uid)}
            for n, page in enumerate(doc):
                for annot in list(page.annots() or []):
                    info = annot.info
                    if (n, annot.xref) in drop_annots or (pattern and pattern.search(info.get("content") or "")):
                        page.delete_annot(annot)
                    elif (info.get("title") or "").strip() in masked_authors:
                        annot.set_info(title="")
                        annot.update()
                for widget in list(page.widgets() or []):
                    self._mask_field(doc, widget, lambda kind, i=0, k=(n, widget.xref): field_spans.get((*k, kind, i), []),
                                     masked, pattern)
                for i, link in enumerate(page.get_links()):
                    target = link.get("uri") or link.get("file") or ""
                    if (n, i) in drop_links or (pattern and pattern.search(target)):
                        page.delete_link(link)
            for n, rects in redactions.items():  # after the fields are drawn again from their masked values
                self._uncover(doc, n, rects, from_text[n])

            toc = doc.get_toc(simple=True)
            changed = False
            for uid, spans in plan.text.items():
                unit = parsed.units.get(uid)
                if unit and unit.kind == "toc" and unit.index < len(toc):
                    title = str(toc[unit.index][1])
                    marks = bytearray(len(title))
                    for s, e in spans:
                        for i in range(max(0, s), min(len(title), e)):
                            marks[i] = 1
                    toc[unit.index][1] = "".join(mask_chars(c, full, half) if marks[i] else c for i, c in enumerate(title))
                    changed = True
            if pattern:
                for entry in toc:
                    new = pattern.sub(found, str(entry[1]))
                    if new != entry[1]:
                        entry[1], changed = new, True
            if changed:
                doc.set_toc(toc)

            info = dict(_info_entries(doc))
            changes: dict[str, str] = {}
            for mid, spans in plan.meta.items():
                where = parsed.metas.get(mid)
                if not where or where[0] != "info" or not spans or where[1] not in info:
                    continue
                value = info[where[1]]
                marks = bytearray(len(value))
                for s, e in spans:
                    for i in range(max(0, s), min(len(value), e)):
                        marks[i] = 1
                whole = all(marks[i] or c.isspace() for i, c in enumerate(value))
                changes[where[1]] = "" if whole else "".join(mask_chars(c, full, half) if marks[i] else c
                                                             for i, c in enumerate(value))
            if pattern:
                for key, value in info.items():
                    now = changes.get(key, value)
                    new = pattern.sub(found, now)
                    if new != now:
                        changes[key] = new
            _write_info(doc, changes)
            _drop_xfa(doc)
            # XMP repeats (and sometimes alone holds) author and title, and is not shown for review: never keep it.
            doc.del_xml_metadata()
            _drop_side_copies(doc)
            doc.save(dst, garbage=4, deflate=True, clean=True)
        finally:
            doc.close()

    # ---- checking ------------------------------------------------------------------------

    def residue(self, dst: Path, strings: list[str]) -> list[str]:
        strings = [s for s in strings if s]
        if not strings:
            return []
        found: list[str] = []

        def report(where: str, s: str):
            line = f"{where}: 「{s}」"
            if line not in found:
                found.append(line)

        parsed = PdfAdapter(self.dpi)._parse(dst)
        for t in parsed.inv.texts:
            joined = tight(t)  # a string still there, broken over two lines
            for s in strings:
                if s in t.text or (joined and s in joined[0]):
                    report(t.section, s)
        for m in parsed.inv.metas:
            for s in strings:
                if s in m.value:
                    report(f"檔案屬性「{m.label}」", s)
        doc = self._open(dst)
        try:
            xmp = doc.get_xml_metadata() or ""
            xfa = ""
            for x in _xfa_streams(doc):
                try:
                    xfa += html.unescape((doc.xref_stream(x) or b"").decode("utf-8", "replace"))
                except Exception:
                    continue
            for s in strings:
                if s in xfa:
                    report("XFA 表單資料", s)
            for n, page in enumerate(doc):
                for link in page.get_links():
                    for s in strings:
                        if s in (link.get("uri") or ""):
                            report(f"第 {n + 1} 頁的連結", s)
        finally:
            doc.close()
        raw = Path(dst).read_bytes()
        for s in strings:
            if s in xmp:
                report("XMP 屬性", s)
            encodings = [e for e in (s.encode("utf-8"), s.encode("utf-16-be")) if len(e) >= RAW_MIN_BYTES]
            if any(e in raw for e in encodings):
                report("檔案原始內容", s)
        return found

    def verify(self, src: Path, dst: Path, plan: MaskPlan, settings: Settings) -> list[str]:
        parsed = self._load(src)
        problems: list[str] = []
        try:
            out = self._open(dst)
        except BrokenFile:
            return ["輸出檔無法重新開啟"]
        doc = self._open(src)
        try:
            if out.page_count != doc.page_count:
                problems.append("輸出檔的頁數與原檔不同")
                return problems
            for n, rects in self._redactions(parsed, plan, doc).items():
                page = out[n]
                seen = imaging.for_black_check(  # decoded once for all the boxes of the page
                    page.get_pixmap(matrix=pymupdf.Matrix(self.zoom, self.zoom), alpha=False).tobytes("png"))
                now, before = _glyphs(page), None
                for rect in rects:
                    if rect.is_empty or rect.is_infinite:
                        continue
                    # The question the redaction answered: is a glyph it should have taken still there. A glyph
                    # of the next line whose box only runs into the range is not one (lines set solid).
                    inner = self._shrunk(rect)
                    here = tuple(inner)
                    left = set() if inner.is_empty else {c for c, box in now if _reached(box, here)}
                    if left and before is None:
                        before = _glyphs(doc[n])
                    # A form field is drawn again from its masked value, so mask characters may stand where the
                    # old ones were. What must not be there is anything the original had in that spot.
                    if left and left & {c for c, box in before if _reached(box, here)}:
                        problems.append(f"第 {n + 1} 頁的遮蔽範圍內仍有文字")
                        break
                    # Text placed outside the page has no picture to be black in.
                    if _shown(doc[n], rect) and not imaging.is_black(seen, self._to_px(doc[n], rect)):
                        problems.append(f"第 {n + 1} 頁的遮蔽框未塗黑")
                        break
            info = dict(_info_entries(out))
            for mid, spans in plan.meta.items():
                where = parsed.metas.get(mid)
                unit = parsed.inv.meta(mid)
                if where and where[0] == "info" and spans and unit and info.get(where[1], "") == unit.value:
                    problems.append(f"檔案屬性「{unit.label}」未清除")
        finally:
            doc.close()
            out.close()
        return problems
