"""The text of a file a cloud model handed back (Word, PDF, Excel), so that the codes in it can be turned back
into the strings (deid.restore).

The file is read by the same parsers that read a document for detection, and laid out like the coded text
that went out: one paragraph a line, a row of a table joined by " | ". Pictures are not read: text misread
by OCR would not match its code. What a reader of the file does not see (properties, hidden content) is
left out, as it is from the coded text.

A Word or Excel file is also given back in its own format with the strings put back in place
(deid.formats.putback), and read once more to see that it says what the restored text says. A PDF is not: its
text sits at fixed positions, and a string of another length would be squeezed or run over what follows.

The parsers take a path, so what they read is written to a scratch folder and removed as soon as it is read:
the upload, which holds codes only, and the restored file, which holds the strings, for the moment it is
checked. `sweep` removes what a crash left behind."""
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from deid.codes import _exported, section_heading
from deid.config import Settings
from deid.formats.base import BrokenFile, adapter_for
from deid.formats.putback import put_back
from deid.model import Inventory
from deid.restore import restore

PREFIX = "deid-reply-"
PDF_NOTE = "PDF 只能還原成文字：PDF 裡文字的位置是固定的，換回長度不同的原文會跑版"


class NoText(ValueError):
    """Nothing to restore: the file has no text a parser can read (a scan, or pictures only)."""


@dataclass
class Reply:
    text: str
    notes: list[str] = field(default_factory=list)  # what was not read, for the person restoring


def layout(inv: Inventory, *, lang: str = "zh-TW") -> str:
    """Titles (a page, a sheet, a header) only when the file has more than one section."""
    units = [t for t in inv.texts if _exported(t)]
    titled = len({t.section for t in units}) > 1
    lines: list[str] = []
    section = None
    for t in units:
        if titled and t.section != section:
            if lines:
                lines.append("")
            lines.append(section_heading(t.section, lang))
            section = t.section
        lines.append(t.text)
    return "\n".join(lines)


def read(data: bytes, filename: str, settings: Settings | None = None, *, lang: str = "zh-TW") -> Reply:
    """Raises UnsupportedFormat, BrokenFile (deid.formats.base) or NoText."""
    ext = Path(filename).suffix.lower()
    adapter = adapter_for(Path("reply" + ext), settings)  # refuses other kinds before anything is written
    with tempfile.TemporaryDirectory(prefix=PREFIX) as folder:
        path = Path(folder) / ("reply" + ext)
        path.write_bytes(data)
        inv = adapter.parse(path)
    text = layout(inv, lang=lang)
    if not text.strip():
        raise NoText
    notes = []
    if inv.kind == "pdf":  # every page of a PDF is a picture to the parser: only those with something to scan count
        pages = sum(1 for im in inv.images if im.needs_scan)
        if pages:
            notes.append(f"有 {pages} 頁含有圖片或是掃描頁，圖片裡的文字沒有讀取")
    elif inv.images:
        notes.append(f"檔案裡有 {len(inv.images)} 張圖片，圖片裡的文字沒有讀取")
    if inv.unchecked:
        notes.append(f"檔案裡有 {len(inv.unchecked)} 處內容無法讀取（例如內嵌的檔案）")
    return Reply(text=text, notes=notes)


@dataclass
class Restored:
    text: str
    replaced: int
    unknown: list[str]
    unused: list[str]
    notes: list[str]
    ext: str
    file: bytes | None = None  # the reply in its own format with the strings put back (Word, Excel)


def restore_file(data: bytes, filename: str, table: dict[str, str], settings: Settings | None = None,
                 *, lang: str = "zh-TW") -> Restored:
    """The restored text of a reply file and, for Word and Excel, the restored file. Raises what `read` raises."""
    ext = Path(filename).suffix.lower()
    reply = read(data, filename, settings, lang=lang)
    done = restore(reply.text, table)
    out = Restored(done.text, done.replaced, done.unknown, done.unused, list(reply.notes), ext)
    if ext == ".pdf":
        out.notes.append(PDF_NOTE)
    elif done.replaced:
        try:
            made = put_back(data, ext, table)
            again = read(made, filename, settings, lang=lang)
        except (BrokenFile, NoText, ValueError, KeyError, OSError):
            out.notes.append("沒辦法產生還原後的檔案，請使用下面的文字")
            return out
        out.file = made
        left = restore(again.text, table).replaced
        if left:
            out.notes.append(f"還原後的檔案裡還有 {left} 處代號沒有換回（例如圖表的標籤、公式算出來的文字）。這些地方請對照下面的文字")
        elif again.text != done.text:
            out.notes.append("還原後的檔案和下面的文字不完全相同，請以下面的文字為準")
    return out


def sweep() -> None:
    """Remove scratch folders a crash left behind. For when the service starts: nothing is being read then."""
    for folder in Path(tempfile.gettempdir()).glob(PREFIX + "*"):
        shutil.rmtree(folder, ignore_errors=True)
