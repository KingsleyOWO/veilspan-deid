"""Word (.docx): a zip of XML parts. Text is masked by splicing the raw XML so everything else stays as it was."""
import html
import io
import posixpath
import re
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from xml.parsers import expat
from xml.sax.saxutils import escape

from deid import imaging
from deid.config import Settings
from deid.formats.base import BrokenFile
from deid.formats.xmltok import (TEXT_KINDS, attr_values, compile_strings, find_in_text_nodes, iter_tokens, mask_chars,
                                 node_text, replace_in_text_nodes, respell, searcher, splice)
from deid.model import Box, ImageUnit, Inventory, MaskPlan, MetaUnit, OcrLine, TextUnit, Unchecked

P_TAGS = {"w:p", "a:p"}
T_TAGS = {"w:t": "seg", "w:delText": "del", "a:t": "seg"}
SEPARATORS = {"w:tab": "\t", "w:br": " ", "w:cr": " ", "w:noBreakHyphen": "-"}
IMAGE_REFS = {"a:blip": ("r:embed", "r:link"), "v:imagedata": ("r:id",), "asvg:svgBlip": ("r:embed",)}

# Text nodes and attributes that carry user-visible or user-entered text. Anything else (widths, ids,
# numeric caches, dates) is never rewritten, so a masked string like "2301" cannot corrupt the layout.
GENERIC_ELEMENTS = {"w:t", "w:delText", "w:instrText", "w:delInstrText", "a:t", "t", "vt:lpwstr",
                    "dc:creator", "dc:title", "dc:subject", "dc:description", "cp:lastModifiedBy", "cp:keywords",
                    "cp:category", "Manager", "Company"}
# A chart value (c:v) is text under these: cached cell text, typed labels, a series name typed in place (c:tx),
# the labels of a multi-level axis, the cell text shown as data labels. Under anything else it is a number.
CHART_TEXT = {"c:strCache", "c:strLit", "c:multiLvlStrCache", "c:tx", "c15:dlblRangeCache"}
GENERIC_UNDER = {"c:v": CHART_TEXT}
GENERIC_ATTRS = {"descr", "title", "w:tooltip"}
RESIDUE_ATTRS = GENERIC_ATTRS | {"w:author", "w15:author", "w15:userId"}
# Word only: a simple field's instruction, a VML shape's alt text and image title. Masked and checked on whatever
# element they stand; read on a simple field and on VML elements (WORD_ATTR_TAGS), the only ones that have them.
WORD_ATTRS = {"w:instr", "alt", "o:title"}
# Word only, on one element each (the same name holds a size or a switch elsewhere): drop-down choices of a content
# control, smart tag and custom XML properties, document variables, a picture link's tooltip, what a legacy
# form field shows in the status bar or on F1, its choices; and (see _word_places) a text form field's default.
WORD_ELEMENT_ATTRS = {"w:listItem": {"w:displayText", "w:value"}, "w:attr": {"w:val"}, "w:docVar": {"w:val"},
                      "a:hlinkClick": {"tooltip"}, "a:hlinkHover": {"tooltip"}, "w:statusText": {"w:val"},
                      "w:helpText": {"w:val"}, "w:listEntry": {"w:val"}}
WORD_ATTR_TAGS = set(WORD_ELEMENT_ATTRS) | {"w:default", "w:fldSimple"}  # and every VML element (v:...)
ATTR_SECTIONS = {"w:instr": "功能變數", "alt": "圖片說明", "o:title": "圖片說明", "w:listItem": "下拉選單",
                 "w:attr": "智慧標籤", "w:docVar": "文件變數", "a:hlinkClick": "連結說明", "a:hlinkHover": "連結說明",
                 "w:statusText": "表單欄位", "w:helpText": "表單欄位", "w:listEntry": "表單欄位", "w:default": "表單欄位"}
INSTR = {"w:instrText", "w:delInstrText"}
CHUNK = "匯入內容"  # what Word brings in where a w:altChunk stands: shown in the document like the body
CHUNK_KINDS = {"text/html": "html", "application/xhtml+xml": "html", "text/plain": "text"}
CHUNK_EXTS = {"htm": "html", "html": "html", "xhtml": "html", "txt": "text"}
CHARSET = re.compile(r"""(?:charset|encoding)\s*=\s*["']?\s*([\w.:\-]+)""", re.I)
HTML_BLOCKS = {"address", "article", "aside", "blockquote", "body", "br", "caption", "center", "dd", "div", "dl", "dt",
               "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "head", "header",
               "hr", "html", "legend", "li", "main", "nav", "ol", "option", "p", "pre", "section", "table", "tbody", "td",
               "tfoot", "th", "thead", "title", "tr", "ul"}
HTML_RAW = {"script", "style"}
BOM = chr(0xFEFF)  # a byte order mark opening a UTF-8 file: not text
HTML_TEXT_ATTRS = {"alt", "title", "value", "placeholder", "label", "summary", "abbr"}  # read, masked and checked
HTML_ATTRS = HTML_TEXT_ATTRS | {"href", "src", "content"}  # masked and checked
NESTED_OOXML = (".xlsx", ".xlsm", ".docx", ".docm", ".pptx", ".pptm")
MAX_XML_BYTES = 300 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024  # everything in the package, unpacked, embedded files included
NEUTRAL_AUTHOR = "－"

HIDDEN = "隱藏內容："
FIELD_WORTH = re.compile(r"HYPERLINK|INCLUDE|LINK|@|[^\x00-\x7f]|[\\/]Users[\\/]", re.I)  # field codes that can carry data
DEFAULT_NAME = re.compile(
    r"(picture|image|img|dsc|photo|screenshot|chart|graphic|object|shape|group|rectangle|line|straight.*|text ?box|"
    r"圖片|图片|圖表|圖形|群組|矩形|直線|文字方塊|物件|螢幕擷取畫面|未命名|_goback|_toc|_hlk|_ref|_hlt|ole_link)[\s_\-]*[\d\-_ ()]*"
    r"(\.(png|jpe?g|gif|bmp|tiff?|emf|wmf|svg))?", re.I)
NAMED = {"pic:cNvPr": "name", "wp:docPr": "name", "xdr:cNvPr": "name", "w:bookmarkStart": "w:name"}
NESTED_TEXT = {"t", "w:t", "a:t"}
NESTED_CAP = 500  # distinct strings read from one embedded file
# An embedded file is read by the paragraph - in a workbook, by the string (si, is) - so that text cut over
# several runs is one string. Its file properties are read too: an author named nowhere else is still in the file.
NESTED_P = {"w:p", "a:p", "si", "is"}
NESTED_T = {"w:t": "seg", "w:delText": "del", "a:t": "seg", "t": "seg"}
NESTED_PROPS = {"dc:creator", "cp:lastModifiedBy", "dc:title", "dc:subject", "dc:description", "cp:keywords",
                "cp:category", "Manager", "Company"}
NESTED, NESTED_PROPERTY = "內嵌檔案", "內嵌檔案屬性"
CUSTOM_CAP = 200
NUMERIC_ONLY = re.compile(r"[0-9A-Fa-f\s.,:/+()\-]*")
BINARY_MIN_BYTES = 6


def _hidden_in_part(xml: str):
    """Text that a reader of the document does not see but that travels with the file: alt text, object and
    bookmark names, chart label caches. Yields (text, section, start, end, mode). Field codes and what else only
    Word parts hold: _word_hidden."""
    stack: list[str] = []
    for kind, name, s, e in iter_tokens(xml):
        if kind in TEXT_KINDS:
            if not stack:
                continue
            if stack[-1] == "c:v" and any(a in stack for a in CHART_TEXT):
                text = node_text(xml, kind, s, e)
                if text.strip():
                    yield text, "圖表", s, e, "text"
        elif kind in ("open", "empty"):
            tag = xml[s:e]
            if name in NAMED or "descr=" in tag or "title=" in tag or "w:tooltip=" in tag:
                for aname, value, vs, ve in attr_values(tag):
                    if not value.strip():
                        continue
                    if aname in GENERIC_ATTRS:
                        yield value, HIDDEN + "圖片說明", s + vs, s + ve, "attr"
                    elif NAMED.get(name) == aname and not DEFAULT_NAME.fullmatch(value.strip()):
                        yield value, HIDDEN + ("書籤" if name == "w:bookmarkStart" else "物件名稱"), s + vs, s + ve, "attr"
            if kind == "open":
                stack.append(name)
        elif kind == "close":
            while stack and stack[-1] != name:
                stack.pop()
            if stack:
                stack.pop()


def _word_places(xml: str):
    """One pass over a Word part for what only Word parts hold. Yields
    ("field", pieces): the instruction of a complex field as the text nodes it is made of, [(start, end, text)] in
      order. Word cuts an instruction over runs as it pleases (an edit, a change of formatting), and a field can
      sit inside another's instruction; the nodes of a nested field are its own.
    ("attr", attribute, value, start, end, section): an attribute that holds text in a Word part on top of
      GENERIC_ATTRS: WORD_ATTRS on a simple field or a VML element, WORD_ELEMENT_ATTRS, a text form field's
      default."""
    fields: list[list | None] = []  # per open field: its instruction while it is read, None after
    stack: list[str] = []
    for kind, name, s, e in iter_tokens(xml):
        if kind in TEXT_KINDS:
            if stack and stack[-1] in INSTR:
                piece = (s, e, node_text(xml, kind, s, e))
                if fields and fields[-1] is not None:
                    fields[-1].append(piece)
                else:
                    yield "field", [piece]  # outside any field (or in a field's result): read on its own
        elif kind in ("open", "empty"):
            if name == "w:fldChar":
                what = next((v for a, v, _s, _e in attr_values(xml[s:e]) if a == "w:fldCharType"), "")
                if what == "begin":
                    fields.append([])
                elif what in ("separate", "end") and fields:
                    if fields[-1]:
                        yield "field", fields[-1]
                    if what == "end":
                        fields.pop()
                    else:
                        fields[-1] = None
            elif name in WORD_ATTR_TAGS or name.startswith("v:"):
                parent = stack[-1] if stack else ""
                wanted = WORD_ELEMENT_ATTRS.get(name) or ({"w:val"} if name == "w:default" and parent == "w:textInput" else ())
                for aname, value, vs, ve in attr_values(xml[s:e]):
                    if aname in wanted or aname in WORD_ATTRS:
                        label = ATTR_SECTIONS.get(aname) or ATTR_SECTIONS[name]
                        if name == "w:attr" and "w:customXmlPr" in stack:
                            label = "自訂資料"
                        yield "attr", aname, value, s + vs, s + ve, label
            if kind == "open":
                stack.append(name)
        elif kind == "close":
            while stack and stack[-1] != name:
                stack.pop()
            if stack:
                stack.pop()
    for pieces in fields:  # never closed
        if pieces:
            yield "field", pieces


def _word_hidden(xml: str):
    """Hidden text that only Word parts hold (see _word_places), as (text, section, start, end, mode). An
    instruction cut over several text nodes gives one row per node, its mode ("text", a, b) naming the
    characters of the whole instruction that node holds."""
    for place in _word_places(xml):
        if place[0] == "attr":
            _kind, aname, value, s, e, label = place
            if len(value.strip()) >= 2 and (aname != "w:instr" or FIELD_WORTH.search(value)):
                yield value, HIDDEN + label, s, e, "attr"
            continue
        pieces = place[1]
        text = "".join(p[2] for p in pieces)
        if not FIELD_WORTH.search(text):
            continue
        if len(pieces) == 1:
            yield text, HIDDEN + "功能變數", pieces[0][0], pieces[0][1], "text"
            continue
        pos = 0
        for s, e, piece in pieces:
            yield text, HIDDEN + "功能變數", s, e, ("text", pos, pos + len(piece))
            pos += len(piece)


def _word_generic(xml: str, strings: list[str], full: str, half: str, partial: dict | None) -> str:
    """What the general search cannot do in a Word part: attributes that hold text on one element only, and
    field instructions cut over several runs, searched as one string."""
    pattern, sub = searcher(strings, full, half, partial)
    if pattern is None:
        return xml
    reps: list[tuple[int, int, str]] = []
    for place in _word_places(xml):
        if place[0] == "attr":
            _kind, aname, value, s, e, _label = place
            new = pattern.sub(sub, value)
            if new != value and aname not in WORD_ATTRS:  # those the general search has done
                reps.append((s, e, escape(new, {'"': "&quot;", "'": "&apos;"})))
            continue
        pieces = place[1]
        if len(pieces) < 2:
            continue  # one node: the general search has been through it
        text = "".join(p[2] for p in pieces)
        new = pattern.sub(sub, text)  # masking keeps the length, so each node keeps its share
        pos = 0
        for s, e, piece in pieces:
            if new[pos:pos + len(piece)] != piece:
                reps.append((s, e, escape(new[pos:pos + len(piece)])))
            pos += len(piece)
    return splice(xml, reps)


def _word_hits(xml: str, pattern) -> set[str]:
    """Which strings are still in what _word_places finds: the attributes, and field instructions read whole."""
    found: set[str] = set()
    for place in _word_places(xml):
        found.update(pattern.findall(place[2] if place[0] == "attr" else "".join(p[2] for p in place[1])))
    return found


def _html_paras(text: str) -> list["_Para"]:
    """An HTML page as paragraphs: the text between block-level tags, with what inline tags (<b>, <span>, <a>)
    cut joined in; the cells of a table row are one row, as in a Word table. Script and style are not text."""
    paras: list[_Para] = []
    cur, raw, rows, rstack = None, 0, 0, []
    for kind, name, s, e in iter_tokens(text):
        if kind in TEXT_KINDS:
            if raw:
                continue
            if s == 0 and text.startswith(BOM):
                s = 1
            if s >= e:
                continue
            if cur is None:
                cur = _Para(row=rstack[-1] if rstack else None, mirror=False)
                paras.append(cur)
            cur.pieces.append(("seg", s, e, node_text(text, kind, s, e)))
        elif kind in ("open", "close", "empty"):
            low = name.lower()
            if low in HTML_RAW and kind != "empty":
                raw = max(0, raw + (1 if kind == "open" else -1))
            if low == "tr" and kind == "open":
                rows += 1
                rstack.append(rows)
            elif low == "tr" and kind == "close" and rstack:
                rstack.pop()
            if low in HTML_BLOCKS or low in HTML_RAW:
                cur = None
    for p in paras:
        p.text = "".join(x[3] for x in p.pieces)
    return paras


def _plain_paras(text: str) -> list["_Para"]:
    """Plain text as paragraphs: one per line."""
    paras = []
    for m in re.finditer(r"[^\r\n]+", text):
        s, e = m.start(), m.end()
        if s == 0 and text.startswith(BOM):
            s = 1
        if s < e:
            paras.append(_Para(row=None, mirror=False, pieces=[("seg", s, e, text[s:e])], text=text[s:e]))
    return paras


def _comment(text: str, kind: str, s: int, e: int) -> tuple[int, int] | None:
    """The span of what an HTML comment holds, if the token is one."""
    return (s + 4, e - 3) if kind == "other" and text.startswith("<!--", s) and text.startswith("-->", e - 3) else None


def _html_hidden(text: str):
    """What an HTML page holds besides its text: link addresses, alt text, titles, form values, the page's named
    meta data, and comments (a page saved by Word keeps its author and company in one). Yields (text, section,
    start, end, mode)."""
    for kind, name, s, e in iter_tokens(text):
        inner = _comment(text, kind, s, e)
        if inner and len(text[inner[0]:inner[1]].strip()) >= 2:
            yield text[inner[0]:inner[1]], HIDDEN + "HTML 註解", inner[0], inner[1], "htmlcomment"
        if kind not in ("open", "empty"):
            continue
        low, tag = name.lower(), text[s:e]
        for aname, value, vs, ve in attr_values(tag):
            a = aname.lower()
            if not value.strip():
                continue
            if a == "href" and low in ("a", "area") and not value.startswith("#"):
                yield value, HIDDEN + "連結位址", s + vs, s + ve, "htmlattr"
            elif a == "alt":
                yield value, HIDDEN + "圖片說明", s + vs, s + ve, "htmlattr"
            elif a in HTML_TEXT_ATTRS or (a == "content" and low == "meta" and re.search(r"\bname\s*=", tag, re.I)):
                yield value, HIDDEN + "設定文字", s + vs, s + ve, "htmlattr"


def _html_out(value: str, refs: bool) -> str:
    """What is written into an HTML page that does not say it is UTF-8: characters beyond ASCII as character
    references, which read the same whatever character set the program that opens it assumes."""
    return value.encode("ascii", "xmlcharrefreplace").decode("ascii") if refs else value


def _mask_html(text: str, strings: list[str], full: str, half: str, partial: dict | None, refs: bool) -> str:
    """The general search in an HTML page: every text node and comment, and the attributes that hold text or
    addresses."""
    pattern, sub = searcher(strings, full, half, partial)
    if pattern is None:
        return text
    reps = []
    for kind, _name, s, e in iter_tokens(text):
        inner = _comment(text, kind, s, e)
        if inner:  # written as it stands: Word reads the markup inside a conditional comment
            new = pattern.sub(sub, text[inner[0]:inner[1]])
            if new != text[inner[0]:inner[1]]:
                reps.append((inner[0], inner[1], _html_out(new, refs)))
        elif kind in TEXT_KINDS:
            plain = node_text(text, kind, s, e)
            new = pattern.sub(sub, plain)
            if new != plain:
                reps.append((s, e, _html_out(escape(new), refs)))
        elif kind in ("open", "empty"):
            for aname, value, vs, ve in attr_values(text[s:e]):
                if aname.lower() in HTML_ATTRS:
                    new = pattern.sub(sub, value)
                    if new != value:
                        reps.append((s + vs, s + ve, _html_out(escape(new, {'"': "&quot;", "'": "&#39;"}), refs)))
    return splice(text, reps)


def _chunk_hits(kind: str, text: str, pattern) -> set[str]:
    """Which strings an imported page still holds: in its text (as one string per paragraph too) and in the
    attributes the masking goes through."""
    if kind == "text":
        return set(pattern.findall(text))
    found: set[str] = set()
    for k, _name, s, e in iter_tokens(text):
        inner = _comment(text, k, s, e)
        if inner:
            found.update(pattern.findall(text[inner[0]:inner[1]]))
        elif k in TEXT_KINDS:
            found.update(pattern.findall(node_text(text, k, s, e)))
        elif k in ("open", "empty"):
            for aname, value, _vs, _ve in attr_values(text[s:e]):
                if aname.lower() in HTML_ATTRS:
                    found.update(pattern.findall(value))
    for p in _html_paras(text):
        found.update(pattern.findall(p.text))
    return found


def _nested_units(xml: str):
    """What one part of an embedded file holds, as (text, section, [(start, end, mode)]): each paragraph with
    the text nodes it is made of - mode "text" for a node that holds all of it, ("text", a, b) for one that holds
    characters a..b - then text nodes that stand in no paragraph, and the file's properties."""
    inside = set()
    for p in parse_part(xml, NESTED_P, NESTED_T):
        nodes = [(s, e, text) for kind, s, e, text in p.pieces if kind != "sep"]
        inside.update((s, e) for s, e, _text in nodes)
        if len(p.text.strip()) < 2:
            continue
        if len(nodes) == 1 and nodes[0][2] == p.text:
            yield p.text, NESTED, [(nodes[0][0], nodes[0][1], "text")]
            continue
        locs, pos = [], 0
        for kind, s, e, text in p.pieces:
            if kind != "sep":
                locs.append((s, e, ("text", pos, pos + len(text))))
            pos += len(text)
        yield p.text, NESTED, locs
    for text, s, e in _text_nodes(xml, NESTED_TEXT):
        if (s, e) not in inside:
            yield text, NESTED, [(s, e, "text")]
    for text, s, e in _text_nodes(xml, NESTED_PROPS):
        yield text, NESTED_PROPERTY, [(s, e, "text")]


def _mask_joined(xml: str, strings: list[str], full: str, half: str, partial: dict | None) -> str:
    """The search for the masked strings over each paragraph of an embedded file read as one string: what the
    search through single text nodes cannot find. Masking keeps the length, so each node keeps its share."""
    pattern, sub = searcher(strings, full, half, partial)
    if pattern is None:
        return xml
    reps: list[tuple[int, int, str]] = []
    for p in parse_part(xml, NESTED_P, NESTED_T):
        if sum(1 for piece in p.pieces if piece[0] != "sep") < 2:
            continue
        new = pattern.sub(sub, p.text)
        if new == p.text:
            continue
        pos = 0
        for kind, s, e, text in p.pieces:
            if kind != "sep" and new[pos:pos + len(text)] != text:
                reps.append((s, e, escape(new[pos:pos + len(text)])))
            pos += len(text)
    return splice(xml, reps)


def _read_entry(z: zipfile.ZipFile, name: str) -> bytes:
    """An entry of the package, or - named outer!inner - an entry of a file embedded in it."""
    if name in z.namelist():
        return z.read(name)
    outer, _bang, inner = name.partition("!")
    with zipfile.ZipFile(io.BytesIO(z.read(outer))) as nested:
        return nested.read(inner)


def _text_nodes(xml: str, parents: set[str] | None):
    """(text, start, end) of non-blank text nodes, optionally only those directly under the given elements."""
    stack: list[str] = []
    for kind, name, s, e in iter_tokens(xml):
        if kind in TEXT_KINDS:
            if stack and (parents is None or stack[-1] in parents or
                          (stack[-1] == "c:v" and any(a in stack for a in CHART_TEXT))):
                text = node_text(xml, kind, s, e)
                if len(text.strip()) >= 2:
                    yield text, s, e
        elif kind == "open":
            stack.append(name)
        elif kind == "close":
            while stack and stack[-1] != name:
                stack.pop()
            if stack:
                stack.pop()


def _wellformed(data: bytes) -> bool:
    try:
        expat.ParserCreate().Parse(data, True)
        return True
    except expat.ExpatError:
        return False


CORE_FIELDS = [("dc:creator", "作者", True), ("cp:lastModifiedBy", "最後修改者", True), ("dc:title", "標題", False),
               ("dc:subject", "主旨", False), ("cp:keywords", "關鍵字", False), ("dc:description", "描述", False),
               ("cp:category", "類別", False), ("cp:contentStatus", "狀態", False)]
APP_FIELDS = [("Manager", "主管", True), ("Company", "公司", False), ("HyperlinkBase", "超連結基底路徑", False)]
AUTHOR_TAG = re.compile(r"<[^<>]*\bw:author\s*=\s*\"[^\"]*\"[^<>]*>")
RELATIONSHIP = re.compile(r"<Relationship\b[^>]*>")
THUMB_REL = re.compile(r"<Relationship\b[^>]*Target=\"/?docProps/thumbnail[^\"]*\"[^>]*/>")


def _natural(name: str):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name)]


def _section(name: str) -> str | None:
    if name == "word/document.xml":
        return "內文"
    m = re.fullmatch(r"word/(header|footer|footnotes|endnotes|comments)\d*\.xml", name)
    if m:
        return {"header": "頁首", "footer": "頁尾", "footnotes": "註腳", "endnotes": "章節附註", "comments": "註解"}[m.group(1)]
    # a SmartArt graphic: its data, and the drawing of it that Word keeps beside (the same text again)
    if re.fullmatch(r"word/charts/[^/]+\.xml", name) or re.fullmatch(r"word/diagrams/(?:data|drawing)[^/]*\.xml", name):
        return "圖表"
    # building blocks and AutoText saved with the document: not shown in it, but they travel with the file
    if re.fullmatch(r"word/glossary/(?:document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml", name):
        return HIDDEN + "建置組塊"
    return None


_ORDER = {"內文": 0, CHUNK: 1, "頁首": 2, "頁尾": 3, "註腳": 4, "章節附註": 5, "註解": 6, "圖表": 7, HIDDEN + "建置組塊": 8}
DIAGRAM_DATA = re.compile(r"word/diagrams/data[^/]*\.xml")
DIAGRAM_DRAWING = re.compile(r"word/diagrams/drawing[^/]*\.xml")


@dataclass
class _Para:
    row: int | None
    mirror: bool
    pieces: list = field(default_factory=list)  # (kind, start, end, text); kind: seg | del | sep
    rids: list = field(default_factory=list)
    text: str = ""


@dataclass
class _Unit:
    id: str
    part: str
    section: str
    paras: list  # (paragraph index within the part, offset of that paragraph inside the unit text)
    text: str
    mirror: bool
    # Hidden places: every spot holding exactly this text, as (zip entry, entry inside an embedded file or None,
    # start, end, mode); mode is text | attr | htmlattr | htmlcomment | target and decides how the replacement is written, or
    # ("text", a, b) for one text node of several that hold the text together: characters a..b of it.
    locs: list = field(default_factory=list)
    cells: list = field(default_factory=list)  # spreadsheet rows: the cells this unit's text is made of


@dataclass
class _Meta:
    unit: MetaUnit
    entry: str | None = None  # element-valued property: zip entry and span of its text node
    start: int = 0
    end: int = 0
    author: str | None = None  # revision/comment author: every w:author attribute with this value
    whole: str = ""  # what replaces the value when all of it is masked


@dataclass
class _Doc:
    inv: Inventory
    paras: dict  # part name -> list[_Para]
    units: dict  # unit id -> _Unit
    metas: dict  # meta id -> _Meta
    media: dict  # image id -> zip entry name
    extra: dict = field(default_factory=dict)  # format-specific data (spreadsheets: shared strings)


def _link_copies(plist: list["_Para"], made: list) -> None:
    """Tell each compatibility copy (mc:Fallback) what it repeats. The copy follows the content it stands in
    for, paragraph by paragraph in the same order, so a run of n copied paragraphs repeats the n paragraphs just
    before it. Going by position rather than by text keeps two text boxes that say the same thing apart, and
    finds the visible copy when that is only one cell of a table row's unit. `made` are the part's units with
    the TextUnit each was listed as."""
    twin: dict[int, int] = {}
    start = None
    for pi in range(len(plist) + 1):
        copied = pi < len(plist) and plist[pi].mirror
        if copied and start is None:
            start = pi
        elif not copied and start is not None:
            n = pi - start
            for k in range(start, pi):
                if k - n >= 0 and not plist[k - n].mirror and plist[k - n].text == plist[k].text:
                    twin[k] = k - n
            start = None
    where = {pi: (unit.id, offset) for unit, _listed in made if not unit.mirror for pi, offset in unit.paras}
    for unit, listed in made:
        if unit.mirror:
            listed.copies = [[offset, len(plist[pi].text), *where[twin[pi]]]
                             for pi, offset in unit.paras if pi in twin and twin[pi] in where]


def parse_part(xml: str, p_tags=None, t_tags=None) -> list[_Para]:
    """Paragraphs in document order with the raw-XML span of every text token."""
    p_tags = P_TAGS if p_tags is None else p_tags
    t_tags = T_TAGS if t_tags is None else t_tags
    paras: list[_Para] = []
    pstack: list[int] = []
    rstack: list[int] = []
    rows = 0
    fallback = 0
    tab_stops = 0
    in_t = None
    for kind, name, s, e in iter_tokens(xml):
        if kind in TEXT_KINDS:
            if in_t and pstack:
                paras[pstack[-1]].pieces.append((in_t, s, e, node_text(xml, kind, s, e)))
            continue
        if kind == "other":
            continue
        opening = kind in ("open", "empty")
        if name in p_tags:
            if kind == "open":
                paras.append(_Para(row=rstack[-1] if rstack else None, mirror=fallback > 0))
                pstack.append(len(paras) - 1)
            elif kind == "close" and pstack:
                pstack.pop()
        elif name == "w:tr":
            if kind == "open":
                rows += 1
                rstack.append(rows)
            elif kind == "close" and rstack:
                rstack.pop()
        elif name in t_tags:
            in_t = t_tags[name] if kind == "open" else None
        elif name == "w:tabs":  # tab stop definitions: <w:tab> in here sets a position, it is not a character
            tab_stops += 1 if kind == "open" else (-1 if kind == "close" else 0)
        elif name in SEPARATORS:
            if opening and pstack and not tab_stops:
                paras[pstack[-1]].pieces.append(("sep", 0, 0, SEPARATORS[name]))
        elif name == "mc:Fallback":
            fallback += 1 if kind == "open" else (-1 if kind == "close" else 0)
        elif name in IMAGE_REFS and opening and pstack:
            for aname, value, _vs, _ve in attr_values(xml[s:e]):
                if aname in IMAGE_REFS[name]:
                    paras[pstack[-1]].rids.append(value)
    for p in paras:
        p.text = "".join(x[3] for x in p.pieces)
    return paras


def _rels(xml: str, base: str) -> dict[str, str]:
    """Relationship id -> package path, for internal targets."""
    out = {}
    for m in RELATIONSHIP.finditer(xml):
        a = {n: v for n, v, _s, _e in attr_values(m.group(0))}
        if a.get("TargetMode") == "External" or "Id" not in a or "Target" not in a:
            continue
        target = a["Target"]
        out[a["Id"]] = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(base, target))
    return out


def _element_text_spans(xml: str, tag: str) -> list[tuple[int, int]]:
    return [(m.start(1), m.end(1)) for m in re.finditer(rf"<{re.escape(tag)}\b[^>]*>([^<]*)</{re.escape(tag)}>", xml)]


def _masked_value(value: str, spans: list[tuple[int, int]], full: str, half: str, whole: str) -> str:
    marks = bytearray(len(value))
    for s, e in spans:
        for i in range(max(0, s), min(len(value), e)):
            marks[i] = 1
    if all(marks[i] or c.isspace() for i, c in enumerate(value)):
        return whole
    return "".join(mask_chars(c, full, half) if marks[i] else c for i, c in enumerate(value))


class DocxAdapter:
    kind = "docx"
    main_part = "word/document.xml"
    not_valid = "檔案不是有效的 Word 文件（.docx）"
    legacy = "檔案有密碼保護，或是舊版 .doc 格式；請移除密碼或另存為 .docx 後再上傳"
    generic_elements = GENERIC_ELEMENTS
    generic_under = GENERIC_UNDER
    generic_attrs = GENERIC_ATTRS | WORD_ATTRS
    residue_attrs = RESIDUE_ATTRS | WORD_ATTRS

    def __init__(self):
        self._cache: dict = {}

    def _joined_texts(self, name: str, xml: str):
        """Texts that are split across several XML text nodes and must be searched as one string (field
        instructions: _residue_hits)."""
        return (p.text for p in parse_part(xml)) if _section(name) else ()

    # ---- reading -------------------------------------------------------------------------

    def _open(self, src: Path) -> zipfile.ZipFile:
        try:
            with open(src, "rb") as f:
                head = f.read(8)
        except OSError:
            raise BrokenFile("無法讀取檔案") from None
        if head.startswith(b"\xd0\xcf\x11\xe0"):
            raise BrokenFile(self.legacy)
        try:
            z = zipfile.ZipFile(src)
        except (zipfile.BadZipFile, OSError):
            raise BrokenFile(self.not_valid) from None
        if self.main_part not in z.namelist():
            z.close()
            raise BrokenFile(self.not_valid)
        return z

    @staticmethod
    def _xml(z: zipfile.ZipFile, name: str) -> str | None:
        info = z.getinfo(name)
        if info.file_size > MAX_XML_BYTES:
            raise BrokenFile("文件內部的 XML 過大，無法處理")
        try:
            return z.read(info).decode("utf-8")
        except UnicodeDecodeError:
            return None
        except (zipfile.BadZipFile, OSError, RuntimeError):
            raise BrokenFile("檔案已損壞，無法讀取") from None

    @staticmethod
    def _check_integrity(z: zipfile.ZipFile) -> None:
        try:
            damaged = z.testzip()
        except (zipfile.BadZipFile, zlib.error, OSError, RuntimeError, EOFError):
            damaged = "?"
        if damaged:
            raise BrokenFile("檔案已損壞，無法完整讀取，請重新取得檔案後再上傳")
        # Embedded Office files are unpacked too (to read and to mask them): bound what they unpack to.
        total = 0
        for info in z.infolist():
            total += info.file_size
            if info.filename.lower().endswith(NESTED_OOXML):
                try:
                    with zipfile.ZipFile(io.BytesIO(z.read(info))) as inner:
                        sizes = [n.file_size for n in inner.infolist()]
                except (zipfile.BadZipFile, zlib.error, OSError, RuntimeError, EOFError):
                    sizes = []
                total += sum(sizes)
                if any(size > MAX_XML_BYTES for size in sizes):
                    raise BrokenFile("檔案解壓縮後過大，無法處理")
        if total > MAX_TOTAL_BYTES:
            raise BrokenFile("檔案解壓縮後過大，無法處理")

    def _scan_links(self, z, names, add_hidden) -> None:
        """Addresses behind links: not visible in the document, but stored in the relationship parts."""
        for name in sorted(n for n in names if n.lower().endswith(".rels")):
            xml = self._xml(z, name) or ""
            for m in RELATIONSHIP.finditer(xml):
                a = {n: (v, vs, ve) for n, v, vs, ve in attr_values(m.group(0))}
                if a.get("TargetMode", ("",))[0] == "External" and a.get("Target", ("",))[0].strip():
                    v, vs, ve = a["Target"]
                    add_hidden(v, HIDDEN + "連結位址", (name, None, m.start() + vs, m.start() + ve, "target"))

    def _scan_custom(self, z, names, inv, add_hidden) -> None:
        for name in sorted((n for n in names if re.fullmatch(r"customXml/item\d+\.xml", n)), key=_natural):
            xml = self._xml(z, name) or ""
            nodes = list(_text_nodes(xml, None))
            for text, s, e in nodes[:CUSTOM_CAP]:
                add_hidden(text, HIDDEN + "自訂資料", (name, None, s, e, "text"))
            if len(nodes) > CUSTOM_CAP:
                inv.unchecked.append(Unchecked(name, f"自訂資料過多，只檢查了前 {CUSTOM_CAP} 筆"))

    def _scan_settings(self, z, names, inv, add_hidden) -> None:
        """Document variables: values a template or a macro stored in the settings, never shown."""
        for name in sorted(n for n in names if re.fullmatch(r"word/(?:glossary/)?settings\.xml", n)):
            rows = list(_word_hidden(self._xml(z, name) or ""))
            for text, section, s, e, mode in rows[:CUSTOM_CAP]:
                add_hidden(text, section, (name, None, s, e, mode))
            if len(rows) > CUSTOM_CAP:
                inv.unchecked.append(Unchecked(name, f"文件變數過多，只檢查了前 {CUSTOM_CAP} 筆"))

    def _alt_chunks(self, z, names) -> tuple[dict[str, str], list[Unchecked]]:
        """Parts that Word brings into the document where a w:altChunk stands - an HTML page, plain text, RTF, a
        web archive, another document. Returns (part -> how it is read, the ones that cannot be read). Read are
        HTML and plain text in UTF-8: "html" (written back as UTF-8), "html-refs" (a page that does not say it
        is UTF-8: what is written into it beyond ASCII goes as character references) and "text". An embedded
        Office document is read with the other embedded files."""
        readable: dict[str, str] = {}
        unchecked: list[Unchecked] = []
        types = (self._xml(z, "[Content_Types].xml") if "[Content_Types].xml" in names else None) or ""
        overrides, defaults = {}, {}
        for m in re.finditer(r"<(Override|Default)\b[^>]*>", types):
            a = {n: v for n, v, _s, _e in attr_values(m.group(0))}
            if m.group(1) == "Override" and "PartName" in a:
                overrides[a["PartName"].lstrip("/")] = a.get("ContentType", "")
            elif "Extension" in a:
                defaults[a["Extension"].lower()] = a.get("ContentType", "")
        for rels_name in sorted(n for n in names if n.startswith("word/") and n.lower().endswith(".rels")):
            base = posixpath.dirname(posixpath.dirname(rels_name))  # word/_rels/document.xml.rels -> word
            for m in RELATIONSHIP.finditer(self._xml(z, rels_name) or ""):
                a = {n: v for n, v, _s, _e in attr_values(m.group(0))}
                if not a.get("Type", "").endswith("/aFChunk") or a.get("TargetMode") == "External" or "Target" not in a:
                    continue
                target = a["Target"]
                name = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(base, target))
                if name not in names or name in readable or any(u.name == name for u in unchecked) \
                        or name.lower().endswith(NESTED_OOXML):
                    continue
                ext = name.rsplit(".", 1)[-1].lower() if "." in posixpath.basename(name) else ""
                ctype = (overrides.get(name) or defaults.get(ext) or "").split(";")[0].strip().lower()
                kind = CHUNK_KINDS.get(ctype) if ctype else CHUNK_EXTS.get(ext)
                if kind is None:
                    unchecked.append(Unchecked(name, "匯入的內容不是 HTML 或純文字，未檢查"))
                    continue
                if z.getinfo(name).file_size > MAX_XML_BYTES:
                    unchecked.append(Unchecked(name, "匯入的內容過大，未檢查"))
                    continue
                data = z.read(name)
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    unchecked.append(Unchecked(name, "匯入的內容不是 UTF-8 編碼，未檢查"))
                    continue
                if kind == "html":
                    declared = CHARSET.search(text[:2048])
                    if declared and declared.group(1).lower() not in ("utf-8", "utf8"):
                        if not data.isascii():  # read as UTF-8, it would not be what Word shows
                            unchecked.append(Unchecked(name, "匯入的內容不是 UTF-8 編碼，未檢查"))
                            continue
                    if not declared or declared.group(1).lower() not in ("utf-8", "utf8"):
                        kind = "html-refs"
                readable[name] = kind
        return readable, unchecked

    def _scan_nested(self, z, names, inv, add_hidden) -> list[tuple[str, int, int, bool]]:
        """Text inside embedded Office files (a chart's workbook, an embedded document), read by the paragraph.
        Returns the pictures they hold, as (outer!inner, width, height, whether it can be checked), for the caller
        to list after the document's own. What is embedded in an embedded file in turn is reported, not read."""
        pictures = []
        for name in sorted((n for n in names if n.lower().endswith(NESTED_OOXML)), key=_natural):
            try:
                inner_zip = zipfile.ZipFile(io.BytesIO(z.read(name)))
            except zipfile.BadZipFile:
                inv.unchecked.append(Unchecked(name, "內嵌檔案無法開啟，未檢查"))
                continue
            seen = 0
            with inner_zip:
                for inner in sorted(inner_zip.namelist(), key=_natural):
                    low = inner.lower()
                    if inner.endswith("/"):
                        continue
                    if low.endswith(NESTED_OOXML) or "/embeddings/" in low or low.endswith("vbaproject.bin"):
                        inv.unchecked.append(Unchecked(f"{name}!{inner}", "內嵌檔案裡的內嵌物件，未檢查"))
                    elif "/media/" in low:
                        try:
                            fmt, w, h, frames = imaging.image_info(inner_zip.read(inner))
                            ok = fmt in imaging.WRITABLE and frames == 1
                        except Exception:
                            w, h, ok = 0, 0, False
                        pictures.append((f"{name}!{inner}", w, h, ok))
                for inner in inner_zip.namelist():
                    if not inner.lower().endswith(".xml") or seen > NESTED_CAP:
                        continue
                    try:
                        xml = inner_zip.read(inner).decode("utf-8")
                    except (UnicodeDecodeError, zipfile.BadZipFile, zlib.error):
                        continue
                    for text, section, locs in _nested_units(xml):
                        seen += 1
                        if seen > NESTED_CAP:
                            break
                        for s, e, mode in locs:
                            add_hidden(text, section, (name, inner, s, e, mode))
            if seen > NESTED_CAP:
                inv.unchecked.append(Unchecked(name, f"內嵌檔案內容過多，只檢查了前 {NESTED_CAP} 筆文字"))
        return pictures

    @staticmethod
    def _list_nested_pictures(inv: Inventory, media_map: dict[str, str], pictures) -> None:
        """Pictures inside embedded files are checked and masked like the document's own."""
        for name, w, h, ok in pictures:
            iid = f"img{len(inv.images) + 1}"
            media_map[iid] = name
            inv.images.append(ImageUnit(id=iid, name=name, width=w, height=h, supported=ok, areas=[(0, 0, w, h)] if ok else []))
            if not ok:
                inv.unchecked.append(Unchecked(name, "不支援的圖片格式，未檢查"))

    def _read_props(self, z, names, add_meta) -> None:
        """File properties: docProps/core.xml, app.xml and custom.xml."""
        for entry, fields_ in (("docProps/core.xml", CORE_FIELDS), ("docProps/app.xml", APP_FIELDS)):
            xml = (self._xml(z, entry) if entry in names else None) or ""
            for tag, label, person in fields_:
                for s, e in _element_text_spans(xml, tag)[:1]:
                    value = html.unescape(xml[s:e])
                    if value.strip():
                        add_meta(label, value, person, entry=entry, start=s, end=e)
        if "docProps/custom.xml" in names:
            xml = self._xml(z, "docProps/custom.xml") or ""
            for m in re.finditer(r"<property\b([^>]*)>(.*?)</property>", xml, re.S):
                pname = next((v for n, v, _s, _e in attr_values("<p " + m.group(1) + ">") if n == "name"), "")
                inner = re.search(r"<vt:lpwstr>([^<]*)</vt:lpwstr>", m.group(2))
                if inner and html.unescape(inner.group(1)).strip():
                    s = m.start(2) + inner.start(1)
                    add_meta(f"自訂屬性：{pname}", html.unescape(inner.group(1)), False, entry="docProps/custom.xml",
                             start=s, end=s + len(inner.group(1)))

    def _load(self, src: Path) -> _Doc:
        src = Path(src)
        st = src.stat()
        key = (str(src), st.st_mtime_ns, st.st_size)
        if key not in self._cache:
            self._cache.clear()
            self._cache[key] = self._parse(src)
        return self._cache[key]

    def _parse(self, src: Path) -> _Doc:
        with self._open(src) as z:
            self._check_integrity(z)
            names = z.namelist()
            inv = Inventory(kind="docx")
            paras: dict[str, list[_Para]] = {}
            units: dict[str, _Unit] = {}
            image_anchor: dict[str, str] = {}  # media entry -> text unit id
            chunks, unreadable = self._alt_chunks(z, names)
            inv.unchecked += unreadable
            chunk_text = {name: z.read(name).decode("utf-8") for name in chunks}  # _alt_chunks has checked that it decodes
            # the drawing of a SmartArt graphic after its data: what it repeats of the data is a copy
            parts = sorted([n for n in names if _section(n) and n not in chunks] + list(chunks),
                           key=lambda n: (_ORDER[CHUNK if n in chunks else _section(n) or ""], bool(DIAGRAM_DRAWING.fullmatch(n)),
                                          _natural(n)))
            diagram_texts: set[str] = set()
            for part in parts:
                if part in chunks:
                    text = chunk_text[part]
                    plist = _plain_paras(text) if chunks[part] == "text" else _html_paras(text)
                    paras[part] = plist
                    section, rels = CHUNK, {}
                else:
                    xml = self._xml(z, part)
                    if xml is None:
                        inv.unchecked.append(Unchecked(part, "無法解碼的 XML，未檢查"))
                        continue
                    if not _wellformed(xml.encode("utf-8")):
                        raise BrokenFile("檔案已損壞：文件內部的 XML 格式錯誤")
                    if part == "word/document.xml" and "<w:body" not in xml:
                        inv.unchecked.append(Unchecked(part, "非預期的 XML 結構，內文無法解析"))
                    plist = parse_part(xml)
                    paras[part] = plist
                    section = _section(part) or ""
                    if DIAGRAM_DATA.fullmatch(part):
                        diagram_texts.update(p.text for p in plist)
                    elif DIAGRAM_DRAWING.fullmatch(part):
                        for p in plist:  # a text the data does not hold is shown and checked like any other
                            p.mirror = p.mirror or p.text in diagram_texts
                    rel_name = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
                    rels = _rels(self._xml(z, rel_name) or "", posixpath.dirname(part)) if rel_name in names else {}
                last_unit, pending = None, []
                group: list[int] = []
                made: list[tuple[_Unit, TextUnit]] = []

                def flush(group=group):
                    nonlocal last_unit
                    if not group:
                        return
                    uid = f"t{len(units) + 1}"
                    offsets, text = [], ""
                    for k, pi in enumerate(group):
                        if k:
                            text += " | "
                        offsets.append((pi, len(text)))
                        text += plist[pi].text
                    unit = _Unit(uid, part, section, offsets, text, all(plist[pi].mirror for pi in group))
                    units[uid] = unit
                    inv.texts.append(TextUnit(id=uid, text=text, section=unit.section, mirror=unit.mirror))
                    made.append((unit, inv.texts[-1]))
                    if not unit.mirror or last_unit is None:
                        last_unit = uid  # images anchor to visible text, not to a compatibility copy
                    for media in pending:
                        image_anchor.setdefault(media, uid)
                    pending.clear()
                    group.clear()

                for pi, p in enumerate(plist):
                    targets = [rels[r] for r in p.rids if r in rels]
                    blank = not p.text.strip()
                    if not blank:
                        if group and (p.row is None or plist[group[-1]].row != p.row or plist[group[-1]].mirror != p.mirror):
                            flush()
                        group.append(pi)
                        if p.row is None:
                            flush()
                    for media in targets:
                        if group:
                            pending.append(media)
                        elif last_unit and blank:
                            image_anchor.setdefault(media, last_unit)
                        else:
                            pending.append(media)
                flush()
                for media in pending:
                    if last_unit:
                        image_anchor.setdefault(media, last_unit)
                _link_copies(plist, made)

            hidden_index: dict[tuple[str, str], str] = {}

            def add_hidden(text: str, section: str, loc: tuple) -> None:
                key = (section, text)
                if key not in hidden_index:
                    uid = f"t{len(units) + 1}"
                    hidden_index[key] = uid
                    units[uid] = _Unit(uid, "", section, [], text, False)
                    inv.texts.append(TextUnit(id=uid, text=text, section=section))
                units[hidden_index[key]].locs.append(loc)

            for part in parts:
                if part in chunks:
                    for text, section, s, e, mode in _html_hidden(chunk_text[part]) if chunks[part] != "text" else []:
                        add_hidden(text, section, (part, None, s, e, mode))
                    continue
                xml = self._xml(z, part)
                rows = sorted(list(_hidden_in_part(xml)) + list(_word_hidden(xml)), key=lambda r: r[2]) if xml else []
                for text, section, s, e, mode in rows:  # in the order the part holds them
                    add_hidden(text, section, (part, None, s, e, mode))
            self._scan_settings(z, names, inv, add_hidden)
            self._scan_links(z, names, add_hidden)
            self._scan_custom(z, names, inv, add_hidden)
            if "word/people.xml" in names:
                xml = self._xml(z, "word/people.xml") or ""
                for kind, _name, s, e in iter_tokens(xml):
                    if kind in ("open", "empty"):
                        for aname, value, vs, ve in attr_values(xml[s:e]):
                            if aname == "w15:userId" and value.strip():
                                add_hidden(value, HIDDEN + "作者帳號", ("word/people.xml", None, s + vs, s + ve, "attr"))
            nested_pictures = self._scan_nested(z, names, inv, add_hidden)

            media_map: dict[str, str] = {}
            for name in sorted((n for n in names if n.startswith("word/media/") and not n.endswith("/")), key=_natural):
                iid = f"img{len(inv.images) + 1}"
                media_map[iid] = name
                try:
                    fmt, w, h, frames = imaging.image_info(z.read(name))
                    ok = fmt in imaging.WRITABLE and frames == 1
                except Exception:
                    fmt, w, h, ok = "", 0, 0, False
                inv.images.append(ImageUnit(id=iid, name=name, width=w, height=h, supported=ok,
                                            anchor=image_anchor.get(name), areas=[(0, 0, w, h)] if ok else []))
                if not ok:
                    inv.unchecked.append(Unchecked(name, "不支援的圖片格式，未檢查"))
            self._list_nested_pictures(inv, media_map, nested_pictures)
            for name in names:
                if name.startswith("word/embeddings/") and not name.lower().endswith(NESTED_OOXML) and not name.endswith("/"):
                    inv.unchecked.append(Unchecked(name, "內嵌物件，未檢查"))
                elif name.lower().endswith("vbaproject.bin"):
                    inv.unchecked.append(Unchecked(name, "巨集，未檢查"))
                elif name.startswith("word/glossary/") and not name.endswith("/") and name not in chunks \
                        and not name.lower().endswith((".xml", ".rels") + NESTED_OOXML) and all(u.name != name for u in unreadable):
                    inv.unchecked.append(Unchecked(name, "建置組塊裡的檔案，未檢查"))

            metas: dict[str, _Meta] = {}

            def add_meta(label, value, person, **where):
                mid = f"m{len(metas) + 1}"
                unit = MetaUnit(id=mid, label=label, value=value, person=person)
                metas[mid] = _Meta(unit=unit, **where)
                inv.metas.append(unit)

            self._read_props(z, names, add_meta)
            authors: list[str] = []
            for name in names:
                if name.startswith("word/") and name.endswith(".xml") and name != "word/people.xml":
                    xml = self._xml(z, name) or ""
                    for m in AUTHOR_TAG.finditer(xml):
                        for aname, value, _s, _e in attr_values(m.group(0)):
                            if aname == "w:author" and value.strip() and value not in authors:
                                authors.append(value)
            if "word/people.xml" in names:  # people listed there may have no comment or revision left in the text
                for m in re.finditer(r'w15:author\s*=\s*"([^"]*)"', self._xml(z, "word/people.xml") or ""):
                    value = html.unescape(m.group(1))
                    if value.strip() and value not in authors:
                        authors.append(value)
            for a in authors:
                add_meta("註解／修訂作者", a, True, author=a)
        return _Doc(inv=inv, paras=paras, units=units, metas=metas, media=media_map, extra={"chunks": chunks})

    def parse(self, src: Path) -> Inventory:
        from deid.model import inventory_from_json, to_json
        return inventory_from_json(to_json(self._load(src).inv))  # a copy the caller may mutate

    def image_bytes(self, src: Path, image_id: str) -> bytes:
        name = self._load(src).media[image_id]
        with self._open(src) as z:
            return _read_entry(z, name)

    def filter_ocr(self, src: Path, image_id: str, lines: list[OcrLine]) -> list[OcrLine]:
        return lines

    def locate(self, src: Path, unit_id: str, start: int, end: int) -> list[tuple[str, Box]]:
        return []

    def view(self, src: Path, inv: Inventory) -> list[dict]:
        doc = self._load(src)
        by_anchor: dict[str, list[str]] = {}
        loose = []
        for im in inv.images:
            if im.anchor:
                by_anchor.setdefault(im.anchor, []).append(im.id)
            else:
                loose.append(im.id)
        blocks = []
        for t in inv.texts:
            if not t.mirror:
                block = {"type": "text", "unit": t.id, "section": t.section}
                deleted = self._deleted_ranges(doc, t.id)
                if deleted:
                    block["del"] = deleted
                blocks.append(block)
            for iid in by_anchor.get(t.id, []):
                blocks.append({"type": "image", "image": iid})
        blocks += [{"type": "image", "image": iid} for iid in loose]
        return blocks

    @staticmethod
    def _deleted_ranges(doc: _Doc, unit_id: str) -> list[list[int]]:
        unit = doc.units.get(unit_id)
        out = []
        for pi, offset in unit.paras if unit else []:
            pos = offset
            for kind, _s, _e, text in doc.paras[unit.part][pi].pieces:
                if kind == "del" and text:
                    if out and out[-1][1] == pos:
                        out[-1][1] = pos + len(text)
                    else:
                        out.append([pos, pos + len(text)])
                pos += len(text)
        return out

    # ---- writing -------------------------------------------------------------------------

    def apply(self, src: Path, dst: Path, plan: MaskPlan, settings: Settings) -> None:
        doc = self._load(src)
        full, half = settings.mask_full, settings.mask_half
        meta_reps, authors = self._meta_replacements(doc, plan, full, half)
        text_reps = self._text_replacements(doc, plan, full, half)
        hidden_top, hidden_nested = self._hidden_replacements(doc, plan, full, half)
        boxes = {doc.media[i]: b for i, b in plan.boxes.items() if i in doc.media and b}
        nested_boxes = self._nested_boxes(boxes)
        chunks = doc.extra.get("chunks", {})

        with self._open(src) as zin, zipfile.ZipFile(dst, "w") as zout:
            for info in zin.infolist():
                name = info.filename
                if name.startswith("docProps/thumbnail"):
                    continue
                data = zin.read(info)
                if name in boxes:
                    data = imaging.paint_boxes(data, boxes[name])
                elif "/media/" in name:
                    data = imaging.strip_metadata(data)
                elif name in chunks:
                    data = self._rewrite_chunk(data, chunks[name], plan.search(), full, half,
                                               text_reps.get(name, []) + hidden_top.get(name, []), plan.partial)
                elif name.lower().endswith((".xml", ".rels", ".vml")):
                    data = self._rewrite_xml(name, data, plan.search(), full, half,
                                             text_reps.get(name, []) + meta_reps.get(name, []) + hidden_top.get(name, []),
                                             authors, plan.partial)
                elif name.lower().endswith(NESTED_OOXML):
                    data = self._rewrite_nested(data, plan.search(), full, half, hidden_nested.get(name, {}), plan.partial,
                                                nested_boxes.get(name))
                out = zipfile.ZipInfo(name, date_time=info.date_time)
                out.compress_type = info.compress_type
                out.external_attr = info.external_attr
                out.create_system = info.create_system
                zout.writestr(out, data)

    @staticmethod
    def _nested_boxes(boxes: dict[str, list]) -> dict[str, dict[str, list]]:
        """Embedded file -> (picture inside it -> boxes), from the boxes of pictures named outer!inner."""
        out: dict[str, dict[str, list]] = {}
        for name, b in boxes.items():
            outer, bang, inner = name.partition("!")
            if bang and outer.lower().endswith(NESTED_OOXML):
                out.setdefault(outer, {})[inner] = b
        return out

    @staticmethod
    def _meta_replacements(doc: _Doc, plan: MaskPlan, full: str, half: str):
        """(replacements per zip entry for element-valued properties, author name -> its replacement)."""
        authors = {}  # author value -> replacement
        meta_reps: dict[str, list] = {}
        for mid, spans in plan.meta.items():
            m = doc.metas.get(mid)
            if not m or not spans:
                continue
            if m.author is not None:
                authors[m.author] = _masked_value(m.unit.value, spans, full, half, NEUTRAL_AUTHOR)
            else:
                new = _masked_value(m.unit.value, spans, full, half, m.whole)
                meta_reps.setdefault(m.entry, []).append((m.start, m.end, escape(new)))
        return meta_reps, authors

    @staticmethod
    def _hidden_replacements(doc: _Doc, plan: MaskPlan, full: str, half: str):
        """Replacements for hidden places: per top-level entry, and per entry inside embedded files."""
        hidden_top: dict[str, list] = {}
        hidden_nested: dict[str, dict[str, list]] = {}
        refs = {name for name, kind in doc.extra.get("chunks", {}).items() if kind == "html-refs"}
        for uid, spans in plan.text.items():
            unit = doc.units.get(uid)
            if not unit or not unit.locs or not spans:
                continue
            marks = bytearray(len(unit.text))
            for s0, e0 in spans:
                for i in range(max(0, s0), min(len(unit.text), e0)):
                    marks[i] = 1
            new = "".join(mask_chars(c, full, half) if marks[i] else c for i, c in enumerate(unit.text))
            for entry, inner, s0, e0, mode in unit.locs:
                if mode == "sheetname":
                    continue  # renaming a sheet has to follow its references: the spreadsheet adapter does it
                if isinstance(mode, tuple):  # one of several text nodes holding the text: its share of it
                    value = escape(new[mode[1]:mode[2]])
                elif mode == "htmlattr":
                    value = escape(new, {'"': "&quot;", "'": "&#39;"})
                elif mode == "htmlcomment":  # a comment holds no entities: written as it stands
                    value = new
                else:
                    value = "about:blank" if mode == "target" else escape(new, {'"': "&quot;", "'": "&apos;"} if mode == "attr" else {})
                value = _html_out(value, entry in refs)
                target = hidden_nested.setdefault(entry, {}).setdefault(inner, []) if inner else hidden_top.setdefault(entry, [])
                target.append((s0, e0, value))
        return hidden_top, hidden_nested

    @staticmethod
    def _text_replacements(doc: _Doc, plan: MaskPlan, full: str, half: str) -> dict[str, list]:
        marks: dict[tuple[str, int], bytearray] = {}
        for uid, spans in plan.text.items():
            unit = doc.units.get(uid)
            if not unit:
                continue
            for pi, offset in unit.paras:
                length = len(doc.paras[unit.part][pi].text)
                for s, e in spans:
                    lo, hi = max(s, offset) - offset, min(e, offset + length) - offset
                    if lo < hi:
                        m = marks.setdefault((unit.part, pi), bytearray(length))
                        for i in range(lo, hi):
                            m[i] = 1
        reps: dict[str, list] = {}
        chunks = doc.extra.get("chunks", {})
        for (part, pi), m in marks.items():
            pos = 0
            for kind, s, e, text in doc.paras[part][pi].pieces:
                if kind != "sep" and any(m[pos:pos + len(text)]):
                    new = "".join(mask_chars(c, full, half) if m[pos + i] else c for i, c in enumerate(text))
                    # plain text imported as it stands has nothing to escape
                    value = new if chunks.get(part) == "text" else _html_out(escape(new), chunks.get(part) == "html-refs")
                    reps.setdefault(part, []).append((s, e, value))
                pos += len(text)
        return reps

    # ---- codes over a masked file ---------------------------------------------------------

    def write_codes(self, src: Path, dst: Path, places: dict[str, list[tuple[int, int, str]]]) -> dict[str, list[tuple[int, int, str]]]:
        """`src` is a masked file, the output of `apply`. Write `dst`: the same file with a code over each listed
        stretch (unit -> (start, end, code); positions are those of the unit's text, which masking does not
        move). Returns the stretches written. One that cannot be written whole into one paragraph or one cell -
        it runs over a tab, a line break or two cells - is left as it is, masked; so is a hidden place, and text
        imported from an alt chunk (codes are put back into XML parts only)."""
        doc = type(self)()._parse(src)
        jobs: dict[str, list] = {}  # zip entry -> (pieces, stretches) of every paragraph to spell again
        reps: dict[str, list] = {}  # zip entry -> replacements that are known without the XML
        written: dict[str, list] = {}
        shared: dict[int, list] = {}  # spreadsheets: shared string -> the cells showing it that take codes
        chunks = doc.extra.get("chunks", {})
        for uid, wanted in places.items():
            unit = doc.units.get(uid)
            if unit is None or unit.locs or not wanted or unit.part in chunks:
                continue
            done = []
            for pi, offset in unit.paras:
                pieces = [(None, None, text) if kind == "sep" else (s, e, text) for kind, s, e, text in doc.paras[unit.part][pi].pieces]
                inside = self._fitting(wanted, offset, pieces)
                if inside:
                    jobs.setdefault(unit.part, []).append((pieces, [(s - offset, e - offset, code) for s, e, code in inside]))
                    done += inside
            done += self._code_cells(unit, wanted, jobs, reps, shared)
            if done:
                written[uid] = sorted(done)
        self._code_shared(doc, shared, jobs, reps)
        with self._open(src) as zin, zipfile.ZipFile(dst, "w") as zout:
            for info in zin.infolist():
                name, data = info.filename, zin.read(info)
                if name in jobs or name in reps:
                    xml = data.decode("utf-8")
                    todo = reps.get(name, []) + [r for pieces, subs in jobs.get(name, []) for r in respell(xml, pieces, subs)]
                    data = splice(xml, todo).encode("utf-8")
                out = zipfile.ZipInfo(name, date_time=info.date_time)
                out.compress_type = info.compress_type
                out.external_attr = info.external_attr
                out.create_system = info.create_system
                zout.writestr(out, data)
        return written

    @staticmethod
    def _fitting(wanted, offset: int, pieces) -> list[tuple[int, int, str]]:
        """The stretches that lie inside this paragraph (it starts at `offset` of the unit's text) and take in
        no separator."""
        gaps, pos = set(), offset
        for s, _e, text in pieces:
            if s is None:
                gaps.update(range(pos, pos + len(text)))
            pos += len(text)
        return [(s, e, code) for s, e, code in wanted if offset <= s < e <= pos and not gaps.intersection(range(s, e))]

    def _code_cells(self, unit: _Unit, wanted, jobs: dict, reps: dict, shared: dict) -> list[tuple[int, int, str]]:
        """Spreadsheets: the stretches written into the cells of a row."""
        return []

    def _code_shared(self, doc: _Doc, shared: dict, jobs: dict, reps: dict) -> None:
        """Spreadsheets: what `_code_cells` put off until every cell was seen."""

    def _rewrite_xml(self, name: str, data: bytes, strings: list[str], full: str, half: str, reps: list,
                     authors: dict[str, str], partial: dict | None = None) -> bytes:
        try:
            xml = data.decode("utf-8")
        except UnicodeDecodeError:
            return data
        original = xml
        reps = list(reps)
        if authors and name.startswith("word/") and name != "word/people.xml":
            for m in AUTHOR_TAG.finditer(xml):
                tag = m.group(0)
                values = {n: (v, s, e) for n, v, s, e in attr_values(tag)}
                author = values.get("w:author")
                if author and author[0] in authors:
                    reps.append((m.start() + author[1], m.start() + author[2], escape(authors[author[0]], {'"': "&quot;"})))
                    if "w:initials" in values:
                        reps.append((m.start() + values["w:initials"][1], m.start() + values["w:initials"][2], ""))
        xml = splice(xml, reps)
        if authors and name == "word/people.xml":
            m = re.search(r"<(w15:people)\b[^>]*?(/?)>", xml)
            if m and not m.group(2):
                xml = xml[:m.end()] + f"</{m.group(1)}>"
        if name == "_rels/.rels":
            xml = THUMB_REL.sub("", xml)
        xml = self._generic(name, xml, strings, full, half, partial)
        return data if xml == original else xml.encode("utf-8")

    def _generic(self, name: str, xml: str, strings: list[str], full: str, half: str, partial: dict | None = None) -> str:
        """Mask the strings wherever else they occur in a part; `partial` names the ones that keep characters."""
        if not strings:
            return xml
        if name.lower().endswith(".vml"):
            return replace_in_text_nodes(xml, strings, None, self.generic_attrs, full, half, partial=partial)
        if name.lower().endswith(".rels"):
            pattern = compile_strings(strings)
            reps = []
            for m in RELATIONSHIP.finditer(xml):
                a = {n: (v, s, e) for n, v, s, e in attr_values(m.group(0))}
                if a.get("TargetMode", ("",))[0] == "External" and "Target" in a and pattern.search(a["Target"][0]):
                    reps.append((m.start() + a["Target"][1], m.start() + a["Target"][2], "about:blank"))
            for s, e, new in sorted(reps, reverse=True):
                xml = xml[:s] + new + xml[e:]
            return xml
        xml = replace_in_text_nodes(xml, strings, self.generic_elements, self.generic_attrs, full, half,
                                    under=self.generic_under, partial=partial)
        return _word_generic(xml, strings, full, half, partial) if name.startswith("word/") else xml

    @staticmethod
    def _rewrite_chunk(data: bytes, kind: str, strings: list[str], full: str, half: str, reps: list,
                       partial: dict | None = None) -> bytes:
        """An imported HTML page or plain text: planned spans first, then the search for the masked strings."""
        text = data.decode("utf-8")
        new = splice(text, reps)
        if strings and kind == "text":
            pattern, sub = searcher(strings, full, half, partial)
            new = pattern.sub(sub, new) if pattern else new
        elif strings:
            new = _mask_html(new, strings, full, half, partial, kind == "html-refs")
        return data if new == text else new.encode("utf-8")

    def _rewrite_nested(self, data: bytes, strings: list[str], full: str, half: str, reps: dict | None = None,
                        partial: dict | None = None, boxes: dict | None = None) -> bytes:
        """An embedded Office file (chart workbook, embedded document): planned spans first, then the same
        generic masking as everywhere else, by the text node and by the paragraph. Its pictures are painted or
        cleaned of what the camera wrote like the document's own, and its thumbnail goes."""
        reps, boxes = reps or {}, boxes or {}
        try:
            zin = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            return data
        changed = False
        buf = io.BytesIO()
        with zin, zipfile.ZipFile(buf, "w") as zout:
            for info in zin.infolist():
                if info.filename.startswith("docProps/thumbnail"):
                    changed = True
                    continue
                inner = zin.read(info)
                if info.filename in boxes:
                    inner, changed = imaging.paint_boxes(inner, boxes[info.filename]), True
                elif "/media/" in info.filename:
                    cleaned = imaging.strip_metadata(inner)
                    if cleaned is not inner:
                        inner, changed = cleaned, True
                elif info.filename.lower().endswith((".xml", ".rels")):
                    try:
                        xml = inner.decode("utf-8")
                        new = xml
                        for s0, e0, value in sorted(reps.get(info.filename, []), reverse=True):
                            new = new[:s0] + value + new[e0:]
                        if info.filename == "_rels/.rels":
                            new = THUMB_REL.sub("", new)
                        new = self._generic(info.filename, new, strings, full, half, partial)
                        new = _mask_joined(new, strings, full, half, partial)
                        if new != xml:
                            inner, changed = new.encode("utf-8"), True
                    except UnicodeDecodeError:
                        pass
                out = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                out.compress_type = info.compress_type
                out.external_attr = info.external_attr
                out.create_system = info.create_system
                zout.writestr(out, inner)
        return buf.getvalue() if changed else data

    # ---- checking ------------------------------------------------------------------------

    def _residue_hits(self, name: str, xml: str, strings: list[str]) -> set[str]:
        """Which strings are still present in the text nodes and text-bearing attributes of one part, and in a
        Word part's field instructions read whole."""
        found = find_in_text_nodes(xml, strings, self.residue_attrs)
        if name.startswith("word/"):
            pattern = compile_strings(strings)
            found |= _word_hits(xml, pattern) if pattern else set()
        return found

    def residue(self, dst: Path, strings: list[str]) -> list[str]:
        """Where the given strings can still be found in the output. Text nodes and text-bearing attributes are
        searched for every string; the raw XML (so any attribute at all) for strings that are not just digits,
        hex letters and punctuation, which would otherwise collide with widths and ids - except long ones holding
        a letter (an id number such as A123456789), which no such value contains; everything else as bytes."""
        strings = [s for s in strings if s]
        if not strings:
            return []
        wordy = [s for s in strings if not NUMERIC_ONLY.fullmatch(s) or (len(s) >= 9 and re.search(r"[A-Za-z]", s))]
        found: list[str] = []

        def report(where: str, hits):
            for s in sorted(hits):
                line = f"{where}: 「{s}」"
                if line not in found:
                    found.append(line)

        def scan(z: zipfile.ZipFile, prefix: str):
            pattern = compile_strings(strings)
            chunks = {} if prefix else self._alt_chunks(z, z.namelist())[0]
            for info in z.infolist():
                name = info.filename
                low = name.lower()
                if name.endswith("/"):
                    continue
                try:
                    data = z.read(info)
                except (zipfile.BadZipFile, zlib.error, OSError):
                    report(prefix + name + "（無法讀取）", [])
                    continue
                if low.endswith(NESTED_OOXML):
                    try:
                        with zipfile.ZipFile(io.BytesIO(data)) as inner:
                            scan(inner, f"{prefix}{name}!")
                        continue
                    except zipfile.BadZipFile:
                        pass  # not really a package: fall through to the byte search
                if name in chunks:  # an imported page: read as it was read for masking
                    text = data.decode("utf-8")
                    hits = _chunk_hits(chunks[name], text, pattern)
                    hits.update(s for s in wordy if s in text or escape(s, {'"': "&quot;"}) in text)
                    report(prefix + name, hits)
                    continue
                xml = None
                if low.endswith((".xml", ".rels", ".vml")):
                    try:
                        xml = data.decode("utf-8")
                    except UnicodeDecodeError:
                        xml = None
                if xml is None:
                    hits = set()
                    for s in strings:
                        for enc in ("utf-8", "utf-16-le"):
                            b = s.encode(enc)
                            if len(b) >= BINARY_MIN_BYTES and b in data:
                                hits.add(s)
                    report(prefix + name, hits)
                    continue
                hits = set()
                if low.endswith(".rels"):
                    for m in RELATIONSHIP.finditer(xml):
                        a = {n: v for n, v, _s, _e in attr_values(m.group(0))}
                        if a.get("TargetMode") == "External":
                            hits.update(pattern.findall(a.get("Target", "")))
                else:
                    hits |= self._residue_hits(name, xml, strings)
                    # a string cut over several text nodes: read whole, in an embedded file by the paragraph
                    for joined in self._joined_texts(name, xml) if not prefix else [p.text for p in parse_part(xml, NESTED_P, NESTED_T)]:
                        hits.update(pattern.findall(joined))
                for s in wordy:
                    if s in xml or escape(s, {'"': "&quot;"}) in xml:
                        hits.add(s)
                report(prefix + name, hits)

        with self._open(dst) as z:
            scan(z, "")
        return found

    def verify(self, src: Path, dst: Path, plan: MaskPlan, settings: Settings) -> list[str]:
        """Re-read the output and confirm each planned mask took effect. Messages never quote content."""
        before = self._load(src)
        after = type(self)()._parse(dst)  # raises BrokenFile if the output is not a readable document
        problems = []
        full, half = settings.mask_full, settings.mask_half
        out_text = {t.id: t.text for t in after.inv.texts}
        hidden_now = {(t.section, t.text) for t in after.inv.texts}
        for uid, spans in plan.text.items():
            unit = before.units.get(uid)
            if unit is not None and unit.locs:  # a hidden place: its old value must be gone everywhere
                if spans and (unit.section, unit.text) in hidden_now:
                    problems.append(f"{unit.section} 的遮蔽未生效")
                continue
            was = before.units[uid].text if uid in before.units else None
            now = out_text.get(uid)
            if was is None or now is None or len(now) != len(was):
                problems.append(f"文字單元 {uid} 在輸出檔中對不上")
                continue
            for s, e in spans:
                expected = "".join(c if doc_sep else mask_chars(c, full, half)
                                   for c, doc_sep in zip(was[s:e], self._separator_flags(before, uid, s, e)))
                if now[s:e] != expected:
                    problems.append(f"文字單元 {uid} 的遮蔽未生效")
                    break
            else:
                # What was to stay readable is still what the original says. A character masked beyond the plan
                # (a number cell is masked whole) is not an error; one that changed into something else is.
                if any(a != b and b != mask_chars(a, full, half) for a, b in zip(was, now)):
                    problems.append(f"文字單元 {uid} 保留的內容與原檔不一致")
        left = {(m.label, m.value) for m in after.inv.metas}
        for mid, spans in plan.meta.items():
            m = before.metas.get(mid)
            if m and spans and (m.unit.label, m.unit.value) in left:
                problems.append(f"檔案屬性「{m.unit.label}」未清除")
        if plan.boxes:
            with self._open(dst) as z:
                for iid, boxes in plan.boxes.items():
                    name = before.media.get(iid)
                    try:
                        data = _read_entry(z, name) if name else None
                    except (KeyError, zipfile.BadZipFile):
                        data = None
                    try:
                        seen = imaging.for_black_check(data) if data is not None else None  # once for all its boxes
                    except Exception:  # noqa: BLE001 - a picture that no longer opens has no black boxes
                        seen = None
                    if seen is None or not all(imaging.is_black(seen, b) for b in boxes):
                        problems.append(f"圖片 {iid} 的遮蔽框未塗黑")
        with self._open(dst) as z:
            if any(n.startswith("docProps/thumbnail") for n in z.namelist()):
                problems.append("首頁縮圖未移除")
            for name in z.namelist():
                if name.lower().endswith((".xml", ".rels")) and not _wellformed(z.read(name)):
                    problems.append(f"輸出檔的 XML 格式錯誤：{name}")
                elif "/media/" in name:
                    data = z.read(name)
                    if imaging.strip_metadata(data) is not data:
                        problems.append(f"圖片 {name} 仍帶有相機或編輯軟體寫入的資訊")
                elif name.lower().endswith(NESTED_OOXML):
                    problems += self._verify_nested(name, z.read(name))
        return problems

    @staticmethod
    def _verify_nested(name: str, data: bytes) -> list[str]:
        """An embedded file after masking: no thumbnail, no picture with what a camera wrote, XML that still parses."""
        problems = []
        try:
            nested = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            return problems  # was not a package to begin with: it is listed as not checked
        with nested:
            if any(n.startswith("docProps/thumbnail") for n in nested.namelist()):
                problems.append(f"內嵌檔案 {name} 的首頁縮圖未移除")
            for inner in nested.namelist():
                if inner.lower().endswith((".xml", ".rels")) and not _wellformed(nested.read(inner)):
                    problems.append(f"輸出檔的 XML 格式錯誤：{name}!{inner}")
                elif "/media/" in inner:
                    picture = nested.read(inner)
                    if imaging.strip_metadata(picture) is not picture:
                        problems.append(f"圖片 {name}!{inner} 仍帶有相機或編輯軟體寫入的資訊")
        return problems

    @staticmethod
    def _separator_flags(doc: _Doc, uid: str, start: int, end: int) -> list[bool]:
        """True for characters that have no XML text behind them (tabs, breaks, the ' | ' between cells)."""
        unit = doc.units[uid]
        flags = [True] * len(unit.text)
        for pi, offset in unit.paras:
            pos = offset
            for kind, _s, _e, text in doc.paras[unit.part][pi].pieces:
                if kind != "sep":
                    for i in range(pos, pos + len(text)):
                        flags[i] = False
                pos += len(text)
        return flags[start:end]
