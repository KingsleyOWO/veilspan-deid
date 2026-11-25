"""The code version of a Word or Excel file in the file's own format, beside the coded text (deid.codes).

It is made in two steps, and the first is the masking the other function does: the document is masked by the
writers that are checked for it - hidden content, file properties, pictures and all - and that masked file is
checked the way a masked file is. Only then is a code written over each masked stretch a reader sees. A
stretch that cannot take its code (it runs over two cells, or over a tab) stays masked, and nothing is written
but codes: the coded file cannot show a string the masked file would not. It can show less than the coded
text does: what stays masked is counted, and the person is told that nothing restores it.

The file is then read again: every text unit has to say what the masked file said, with the codes that were
written in place of the mask and nothing else changed.

A PDF has no coded file: its text sits at fixed positions, and a code of another length would be squeezed or
run over what follows."""
import logging
from dataclasses import dataclass, field
from pathlib import Path

from deid.codes import CodedText, _exported
from deid.formats.xmltok import mask_chars

log = logging.getLogger("deid.codefile")

KINDS = (".docx", ".xlsx", ".xlsm")
PDF_NOTE = "PDF 只有代號版文字：PDF 裡文字的位置是固定的，換成長度不同的代號會跑版"
FAILED = "代號版檔案沒有通過檢查，沒有提供。請使用代號版文字"
BROKEN = "沒辦法產生代號版檔案，請使用代號版文字"
PICTURES = "圖片裡遮住的地方在代號版檔案裡是塗黑的，還原不回來"


@dataclass
class CodedFile:
    ok: bool = False  # the file was written and reads as it should
    placed: int = 0  # stretches a reader sees that are a code in the file (a text box's second copy not counted)
    left: int = 0  # places a reader sees that are masked in the file and have no code: nothing restores them
    notes: list[str] = field(default_factory=list)  # for the person who will send the file out


def _spelled(text: str, written: list[tuple[int, int, str]]) -> str:
    out, pos = [], 0
    for s, e, code in sorted(written):
        out += [text[pos:s], code]
        pos = e
    return "".join(out) + text[pos:]


def _masked(original: str, masked: str, spans: list[tuple[int, int]], written: list[tuple[int, int, str]],
            full: str, half: str) -> int:
    """How many places of one unit are masked and were given no code. Counted from the masked file itself, so
    that what the masking covers beyond the stretches of the text (a number or date cell is masked whole) counts
    as well. Read again, though, the masked file can show two places as one unit: chart labels are listed by
    their text, two labels masked alike become one, and every label listed after them moves up one number. When
    the unit under this unit's number is not this unit masked, this unit's own stretches are counted instead. A
    unit with nothing to mask has no masked place. Blanks inside a masked place do not split it."""
    if not spans:
        return 0
    if len(masked) == len(original) and all(a == b or b == mask_chars(a, full, half) for a, b in zip(original, masked)):
        hidden = {i for i, (a, b) in enumerate(zip(original, masked)) if a != b}
    else:
        hidden = {i for s, e in spans for i in range(s, e)}
    coded = {i for s, e, _code in written for i in range(s, e)}
    places, inside = 0, False
    for i, was in enumerate(original):
        if i in hidden and i not in coded:
            places += not inside
            inside = True
        elif not was.isspace():
            inside = False
    return places


def make(ctx, coded: CodedText, dst: Path) -> CodedFile:
    """Write `dst`, the document of `ctx` with the codes of `coded`. Nothing is written when the document is
    of a kind that has no coded file, or when a check fails: the notes say which."""
    ext = ctx.src.suffix.lower()
    if ext not in KINDS:
        return CodedFile(notes=[PDF_NOTE] if ext == ".pdf" else [])
    masked = dst.with_name(dst.stem + ".masked.tmp" + ext)
    tmp = dst.with_name(dst.stem + ".tmp" + ext)
    try:
        plan = ctx.store.plan(whole=True)
        ctx.adapter.apply(ctx.src, masked, plan, ctx.settings)
        if ctx.adapter.residue(masked, plan.strings) or ctx.adapter.verify(ctx.src, masked, plan, ctx.settings):
            return CodedFile(notes=[FAILED])
        written = ctx.adapter.write_codes(masked, tmp, coded.places)
        reader = type(ctx.adapter)()  # its own: the job's adapter keeps the source it has read
        before = {t.id: t.text for t in reader.parse(masked).texts}
        after = {t.id: t.text for t in reader.parse(tmp).texts}
        if after != {uid: _spelled(text, written.get(uid, [])) for uid, text in before.items()}:
            return CodedFile(notes=[FAILED])
        tmp.replace(dst)
    except Exception as e:  # noqa: BLE001 - whatever goes wrong here costs the file only: the coded text is made already
        log.error("coded file not made: %s", type(e).__name__)  # the class only: a message may quote content
        return CodedFile(notes=[BROKEN])
    finally:
        masked.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)
    # What a reader sees: the second copy of a text box is written like the first but is not another place.
    full, half = ctx.settings.mask_full, ctx.settings.mask_half
    left = sum(_masked(t.text, before.get(t.id, ""), plan.text.get(t.id, []), written.get(t.id, []), full, half)
               for t in ctx.inv.texts if _exported(t))
    copies = {t.id for t in ctx.inv.texts if t.mirror}
    out = CodedFile(ok=True, placed=sum(len(v) for uid, v in written.items() if uid not in copies), left=left)
    if left:
        out.notes.append(f"代號版檔案裡有 {left} 處是遮蔽、沒有寫成代號，這些地方還原不回來。需要還原時請使用代號版文字")
    if any(plan.boxes.values()):
        out.notes.append(PICTURES)
    return out
