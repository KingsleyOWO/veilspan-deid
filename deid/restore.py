"""Putting the original strings back into text that carries codes (a reply from a cloud model)."""
import re
from dataclasses import dataclass, field

from deid.codes import CODE, EN_LABELS, EN_NOTICE, EN_NOTICE_SAME, LABELS, NOTICE, NOTICE_SAME, canonical
from deid.detect import TYPES


def _shape(line: str) -> str:
    """A line with its codes and its spacing taken out: what is left when a model rewrote the brackets."""
    return re.sub(r"\s+", "", CODE.sub("\0", line))


# The lines of explanation the coded text starts with. They are written for the model that reads the text and
# name codes as examples ([姓名1], [電話2]): they are not part of the document, and their examples are not codes
# to put back or to report as unknown.
_NOTICES = {_shape(line) for line in (NOTICE, NOTICE_SAME, EN_NOTICE, EN_NOTICE_SAME)}


def _without_notices(text: str) -> str:
    lines = text.split("\n")
    kept = [line for line in lines if _shape(line) not in _NOTICES]
    if len(kept) == len(lines):
        return text
    first = next((n for n, line in enumerate(lines) if _shape(line) in _NOTICES), 0)
    if not any(line.strip() for line in lines[:first]):  # the explanation was the top of the text: so is its blank line
        while kept and not kept[0].strip():
            kept.pop(0)
    return "\n".join(kept)


@dataclass
class Restored:
    text: str
    replaced: int = 0
    unknown: list[str] = field(default_factory=list)  # codes in the text that the table does not have
    unused: list[str] = field(default_factory=list)  # codes of the table that the text never mentions


def restore(text: str, table: dict[str, str]) -> Restored:
    """Replace every code the table knows, however its brackets, spaces or digits were rewritten. Anything
    else in brackets is left as it is; what looks like one of our codes but is not in the table is listed.
    The coded text's own lines of explanation are left out of the result."""
    labels = {LABELS.get(t, t) for t in TYPES} | set(EN_LABELS.values())
    labels.update(m.group(1) for code in table if (m := CODE.fullmatch(code)))
    seen: set[str] = set()
    unknown: list[str] = []
    count = 0

    def put_back(m) -> str:
        nonlocal count
        code = canonical(m.group(1), m.group(2), m.group(3))
        if code in table:
            seen.add(code)
            count += 1
            return table[code]
        if m.group(1) in labels and code not in unknown:
            unknown.append(code)
        return m.group(0)

    out = CODE.sub(put_back, _without_notices(text))
    return Restored(text=out, replaced=count, unknown=unknown, unused=[c for c in table if c not in seen])
