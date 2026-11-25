"""Putting the original strings back into the file itself, for a Word or Excel file a cloud model handed back:
every code the table knows becomes its string where it stands, and nothing else in the file changes.

Both formats are zip packages of XML. The text of a paragraph (a shared string, a cell's own text, a comment)
can be cut into several runs, and so can a code. The paragraph is read as one string, the codes are found by
the same pattern the text restore uses, and each string is written into the run its code began in; the rest
of the code is taken out of the runs it ran through. Runs are neither added nor removed, so the formatting
of the first run applies.

Everything happens in memory. Whether the result is right is checked by the caller, who reads it again
(deid.replyfile)."""
import io
import zipfile

from deid.codes import CODE, canonical
from deid.formats.docx import parse_part
from deid.formats.xlsx import SHARED, XlsxAdapter
from deid.formats.xmltok import respell, splice

SHEET = (".xlsx", ".xlsm")
BRACKETS = tuple(b.encode("utf-8") for b in "[［【〔")  # a part without one of these holds no code
GAP = "\0"  # a tab or a line break inside a paragraph: no code runs across one
SHEET_PARAGRAPHS, SHEET_TEXT = {"is", "comment", "a:p"}, {"t": "seg", "a:t": "seg"}


def _paragraph(xml: str, pieces: list[tuple[int | None, int | None, str]], table: dict[str, str]) -> list[tuple[int, int, str]]:
    """Replacements for one paragraph. `pieces` are its text tokens in order as (start, end, text) in the raw
    XML; a separator between them has no position."""
    whole = "".join(GAP * len(text) if s is None else text for s, _e, text in pieces)
    subs = []
    for m in CODE.finditer(whole):
        string = table.get(canonical(m.group(1), m.group(2), m.group(3)))
        if string is not None:
            subs.append((m.start(), m.end(), string))
    return respell(xml, pieces, subs) if subs else []


def _part(name: str, xml: str, ext: str, table: dict[str, str]) -> str:
    reps: list[tuple[int, int, str]] = []
    if ext in SHEET and name == SHARED:
        for item in XlsxAdapter._shared_strings(xml):
            changed = _paragraph(xml, list(item.pieces), table)
            if changed:
                reps += changed
                reps += [(s, e, "") for s, e in item.phonetic]  # the reading spelled out the code
    else:
        paragraphs = parse_part(xml, SHEET_PARAGRAPHS, SHEET_TEXT) if ext in SHEET else parse_part(xml)
        for p in paragraphs:
            reps += _paragraph(xml, [(None, None, text) if kind == "sep" else (s, e, text) for kind, s, e, text in p.pieces], table)
    return splice(xml, reps)


def put_back(data: bytes, ext: str, table: dict[str, str]) -> bytes:
    """The package with every code of `table` turned back into its string. `ext` is .docx, .xlsx or .xlsm."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin, zipfile.ZipFile(out, "w") as zout:
        for info in zin.infolist():
            body = zin.read(info)
            if info.filename.lower().endswith(".xml") and any(b in body for b in BRACKETS):
                try:
                    xml = body.decode("utf-8")
                except UnicodeDecodeError:
                    xml = None
                if xml is not None:
                    new = _part(info.filename, xml, ext, table)
                    if new != xml:
                        body = new.encode("utf-8")
            copy = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            copy.compress_type = info.compress_type
            copy.external_attr = info.external_attr
            copy.create_system = info.create_system
            zout.writestr(copy, body)
    return out.getvalue()
