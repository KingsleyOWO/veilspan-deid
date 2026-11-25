"""Findings: what will be masked. A text finding is one string applied to every place it occurs —
body text, text read from images, file properties — and each place can be switched off on its own."""
import re
import unicodedata
from typing import Callable

from deid.formats.xmltok import token_pattern
from deid.imaging import fold_width

from deid.partial import PARTIAL_TYPES, applies, mask_spans, masked
from deid.model import Box, Finding, Inventory, MaskPlan, Occurrence, OcrLine, finding_from_json, tight, to_json

META_TYPE = "檔案屬性"
AGENT_CANCEL = "代理取消："  # how the reason starts when the agent withdrew a string only it had listed
AGENT_DOUBT = "代理存疑："  # ... and when it objected to one that detection or a rule found: that one stays masked
NAME_TYPE = "姓名"
OCR_PAD = 2.0


def find_in_text(haystack: str, needle: str) -> list[tuple[int, int]]:
    """Non-overlapping matches, left to right. Numbers and Latin words match only as whole tokens."""
    if not needle:
        return []
    return [(m.start(), m.end()) for m in re.finditer(token_pattern(needle), haystack)]


def _cjk(c: str) -> bool:
    return unicodedata.east_asian_width(c) in "WF" and c.isalpha()


def _union(boxes: list[Box]) -> Box:
    return (min(b[0] for b in boxes) - OCR_PAD, min(b[1] for b in boxes) - OCR_PAD,
            max(b[2] for b in boxes) + OCR_PAD, max(b[3] for b in boxes) + OCR_PAD)


def find_in_ocr(lines: list[OcrLine], needle: str) -> list[tuple[Box, bool]]:
    """Exact matches, plus near matches for strings of three or more characters where exactly one CJK
    character differs (the recogniser sometimes returns a look-alike or simplified glyph)."""
    needle = fold_width(needle)
    variants = [needle]
    squeezed = "".join(needle.split())
    if squeezed and squeezed != needle:
        variants.append(squeezed)
    out: list[tuple[Box, bool]] = []
    for line in lines:
        line_text = fold_width(line.text)
        taken: list[tuple[int, int]] = []
        for v in variants:
            for s, e in find_in_text(line_text, v):
                if not any(s < te and ts < e for ts, te in taken):
                    taken.append((s, e))
                    out.append((_union(line.chars[s:e]), False))
        n = len(needle)
        if n < 3:
            continue
        for s in range(0, len(line_text) - n + 1):
            if any(s < te and ts < s + n for ts, te in taken):
                continue
            window = line_text[s:s + n]
            diff = [i for i in range(n) if window[i] != needle[i]]
            if len(diff) == 1 and _cjk(needle[diff[0]]) and _cjk(window[diff[0]]):
                taken.append((s, s + n))
                out.append((_union(line.chars[s:s + n]), True))
    return out


STYLES = ("full", "partial", "custom")
MAILBOX = "電子郵件"


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


class FindingStore:
    def __init__(self, inv: Inventory, locate: Callable[[str, int, int], list[tuple[str, Box]]] | None = None):
        self.inv = inv
        self.locate = locate
        self.findings: list[Finding] = []
        self._next = 1
        self._mirrors = {t.id for t in inv.texts if t.mirror}
        self._tight: dict[str, tuple[str, list[int]] | None] = {}  # per unit: its text without line-end spaces
        self.styles: dict[str, str] = {}  # type -> "partial", for types switched as a whole: new findings follow

    # ---- adding --------------------------------------------------------------------------

    def _new_id(self) -> str:
        fid = f"f{self._next}"
        self._next += 1
        return fid

    def _find(self, unit, needle: str) -> list[tuple[int, int]]:
        """Where a string stands in a text unit. A line end of a page is read both ways, as a space and as
        nothing: a string the layout broke over two lines is found, the place then taking in the space the
        reader put there."""
        found = find_in_text(unit.text, needle)
        if unit.id not in self._tight:
            self._tight[unit.id] = tight(unit)
        joined = self._tight[unit.id]
        if joined:
            text, keep = joined
            for s, e in find_in_text(text, needle):
                a, b = keep[s], keep[e - 1] + 1
                if not any(x < b and a < y for x, y in found):
                    found.append((a, b))
            found.sort()
        return found

    def _text_occurrence(self, unit: str, start: int, end: int) -> Occurrence:
        boxes = self.locate(unit, start, end) if self.locate else []
        return Occurrence(unit=unit, kind="text", start=start, end=end, page_boxes=list(boxes))

    def _image_occurrences(self, text: str) -> list[Occurrence]:
        out = []
        for im in self.inv.images:
            for box, fuzzy in find_in_ocr(im.ocr, text):
                out.append(Occurrence(unit=im.id, kind="box", box=box, fuzzy=fuzzy))
        return out

    def add_text(self, text: str, type: str, source: str, *, on: bool = True, reason: str = "",
                 only: tuple[str, int, int] | None = None) -> Finding | None:
        text = text or ""
        existing = next((f for f in self.findings if f.kind == "text" and f.text == text), None)
        if only is not None:
            unit, start, end = only
            t = self.inv.text(unit)
            if t is None or not text or not 0 <= start < end <= len(t.text) or t.text[start:end] != text:
                return None
            occs = [self._text_occurrence(unit, start, end)]
            if not t.mirror:  # mc:Fallback holds the same text again; it is not shown, so it follows silently
                for m in self.inv.texts:
                    if not m.mirror or m.section != t.section:
                        continue
                    if m.copies:  # the copy of this very place, wherever the visible text stands in its unit
                        occs += [self._text_occurrence(m.id, start - there + here, end - there + here)
                                 for here, length, uid, there in m.copies if uid == unit and there <= start and end <= there + length]
                    elif m.text == t.text:  # not recorded which text it repeats: the one that says the same
                        occs.append(self._text_occurrence(m.id, start, end))
        else:
            occs = [self._text_occurrence(t.id, s, e) for t in self.inv.texts for s, e in self._find(t, text)]
            occs += self._image_occurrences(text)
            occs += [Occurrence(unit=m.id, kind="meta", start=s, end=e)
                     for m in self.inv.metas for s, e in find_in_text(m.value, text)]
            if not occs:
                return None
            if existing is None:
                # The very places of a string already listed under another spelling: one reading has the space the
                # reader put in at a line end, the other has not. One entry, not two.
                places = {(o.unit, o.start, o.end) for o in occs if o.kind == "text"}
                existing = next((g for g in self.findings if g.kind == "text" and places and places <= {
                    (o.unit, o.start, o.end) for o in g.occurrences if o.kind == "text"}), None)
        if existing is None:
            f = Finding(id=self._new_id(), kind="text", type=type, text=text, source=source, on=on, reason=reason,
                        occurrences=occs, style=self.styles.get(type, "full"))
            self.findings.append(f)
            return f
        have = {(o.unit, o.kind, o.start, o.end, o.box) for o in existing.occurrences}
        fresh = [o for o in occs if (o.unit, o.kind, o.start, o.end, o.box) not in have]
        existing.occurrences += fresh
        # Detection or a rule asks for a string that so far only the agent had listed: from here it is not the
        # agent's alone to withdraw, whichever of the two came first. One the agent had already withdrawn is
        # masked again, and its objection stays as a doubt for the person reviewing.
        takeover = on and existing.source == "agent" and source not in ("user", "agent")
        if takeover:
            existing.source = source
            if not existing.on:
                existing.reason = (AGENT_DOUBT + existing.reason[len(AGENT_CANCEL):]
                                   if existing.reason.startswith(AGENT_CANCEL) else reason)
        if (source in ("user", "agent") or takeover) and on:
            wanted = {(o.unit, o.kind, o.start, o.end, o.box) for o in occs}
            if not existing.on:
                existing.on = True
                for o in existing.occurrences:
                    o.on = only is None or (o.unit, o.kind, o.start, o.end, o.box) in wanted
            else:
                for o in existing.occurrences:
                    if (o.unit, o.kind, o.start, o.end, o.box) in wanted:
                        o.on = True
        return existing

    def add_region(self, image_id: str, box: Box, type: str, source: str, *, on: bool = True, reason: str = "") -> Finding:
        box = tuple(float(v) for v in box)
        for f in self.findings:
            if f.kind == "region" and f.type == type and f.occurrences[0].unit == image_id and f.occurrences[0].box == box:
                return f
        f = Finding(id=self._new_id(), kind="region", type=type, text="", source=source, on=on, reason=reason,
                    occurrences=[Occurrence(unit=image_id, kind="box", box=box, on=True)])
        self.findings.append(f)
        return f

    def add_meta(self, meta_id: str, *, on: bool) -> Finding:
        for f in self.findings:
            if f.kind == "meta" and f.occurrences[0].unit == meta_id:
                return f
        m = self.inv.meta(meta_id)
        if m is None:
            raise KeyError(meta_id)
        f = Finding(id=self._new_id(), kind="meta", type=META_TYPE, text=m.value, source="meta", on=on, reason=m.label,
                    occurrences=[Occurrence(unit=meta_id, kind="meta", start=0, end=len(m.value))])
        self.findings.append(f)
        return f

    def rematch(self) -> None:
        """After OCR results arrive, give existing text findings their occurrences inside images."""
        for f in self.findings:
            if f.kind != "text":
                continue
            have = {(o.unit, o.box) for o in f.occurrences if o.kind == "box"}
            for o in self._image_occurrences(f.text):
                if (o.unit, o.box) not in have:
                    f.occurrences.append(o)

    # ---- editing -------------------------------------------------------------------------

    def get(self, fid: str) -> Finding:
        for f in self.findings:
            if f.id == fid:
                return f
        raise KeyError(fid)

    def set_on(self, fid: str, on: bool, occurrence: int | None = None) -> None:
        f = self.get(fid)
        if occurrence is None:
            f.on = on
            # Places switched off one by one stay off when the finding comes back on; only a finding
            # with no place left on gets all of them back.
            if on and not any(o.on for o in f.occurrences if o.unit not in self._mirrors):
                for o in f.occurrences:
                    o.on = True
            if not on:
                self.unlink(fid)
            return
        if not 0 <= occurrence < len(f.occurrences):
            raise ValueError("no such occurrence")
        o = f.occurrences[occurrence]
        if o.unit in self._mirrors:
            raise ValueError("this occurrence follows the finding and cannot be switched alone")
        o.on = on
        if on:
            f.on = True
        elif not any(x.on for x in f.occurrences if x.unit not in self._mirrors):
            f.on = False
            self.unlink(fid)

    def set_type(self, fid: str, type: str) -> None:
        f = self.get(fid)
        f.type = type
        if f.style != "custom":  # the characters the reviewer picked do not depend on the type
            f.style = self.styles.get(type, "full") if f.kind == "text" else "full"
        if type != NAME_TYPE:
            self.unlink(fid)

    def set_style(self, fid: str, style: str) -> None:
        """Whole ("full"), keeping the characters a rule leaves ("partial": text findings of a type that has a
        rule), or the characters the reviewer picks ("custom": text and file properties). Custom starts from how
        the finding is masked at that moment; set_spans changes it."""
        f = self.get(fid)
        if style not in STYLES or (style == "partial" and (f.kind != "text" or f.type not in PARTIAL_TYPES)) \
                or (style == "custom" and not self.can_customize(f)):
            raise ValueError("this finding cannot be masked that way")
        if style != "custom":
            f.spans = []
        elif f.style != "custom":
            f.spans = self._mask_of(f)
        f.style = style

    @staticmethod
    def can_customize(f: Finding) -> bool:
        return f.kind in ("text", "meta") and bool(f.text.strip())

    def set_spans(self, fid: str, spans) -> None:
        """The reviewer's own choice of which characters of the finding are masked: ranges within its text."""
        f = self.get(fid)
        if not self.can_customize(f):
            raise ValueError("this finding cannot be masked that way")
        try:
            pairs = [(int(s), int(e)) for s, e in spans]
        except (TypeError, ValueError):
            raise ValueError("ranges are pairs of numbers") from None
        if any(not 0 <= s <= e <= len(f.text) for s, e in pairs):
            raise ValueError("a range reaches outside the text")
        tidy = _merge([(s, e) for s, e in pairs if s < e])
        if not any(not f.text[i].isspace() for s, e in tidy for i in range(s, e)):
            raise ValueError("nothing would be masked")  # switch the finding off instead
        f.style, f.spans = "custom", tidy

    def bulk(self, *, type: str | None = None, source: str | None = None, on: bool | None = None,
             style: str | None = None) -> int:
        changed = 0
        if style is not None:
            if style not in STYLES or type is None or (style == "partial" and type not in PARTIAL_TYPES):
                raise ValueError("this type cannot be masked that way")
            if style == "partial":
                self.styles[type] = style
            else:
                self.styles.pop(type, None)
            for f in self.findings:
                if f.kind == "text" and f.type == type and (source is None or f.source == source) \
                        and f.style not in (style, "custom"):  # what the reviewer picked by hand is left alone
                    f.style = style
                    changed += 1
        if on is not None:
            for f in self.findings:
                if (type is None or f.type == type) and (source is None or f.source == source) and f.on != on:
                    self.set_on(f.id, on)
                    changed += 1
        return changed

    def remove(self, fid: str) -> None:
        f = self.get(fid)
        if f.source != "user":
            raise ValueError("only manually added findings can be removed")
        self.unlink(fid)
        self.findings.remove(f)

    # ---- names of one person -------------------------------------------------------------

    def link(self, fids: list[str]) -> str:
        """Say that these names are one person: in the code version they share a number. Only active name
        findings can be linked. Linking with a member of a group joins that group; returns the group."""
        members: list[Finding] = []
        for fid in fids:
            f = self.get(fid)
            if f not in members:
                members.append(f)
        if len(members) < 2 or not all(f.kind == "text" and f.type == NAME_TYPE and f.on for f in members):
            raise ValueError("only two or more active names can be one person")
        have = {f.group for f in members if f.group}
        if len(have) > 1:
            raise ValueError("these names are in two different groups")
        if have:
            group = have.pop()
        else:
            used, n = {f.group for f in self.findings}, 1
            while f"g{n}" in used:
                n += 1
            group = f"g{n}"
        for f in members:
            f.group = group
        return group

    def unlink(self, fid: str) -> None:
        """Take a finding out of its group. One name left alone is no group."""
        f = self.get(fid)
        group, f.group = f.group, ""
        if group:
            left = [x for x in self.findings if x.group == group]
            if len(left) < 2:
                for x in left:
                    x.group = ""

    def groups(self) -> dict[str, list[Finding]]:
        out: dict[str, list[Finding]] = {}
        for f in self.findings:
            if f.group:
                out.setdefault(f.group, []).append(f)
        return out

    # ---- output --------------------------------------------------------------------------

    def _active(self, f: Finding, o: Occurrence) -> bool:
        return f.on and (o.on or o.unit in self._mirrors)

    @staticmethod
    def _partial(f: Finding) -> bool:
        return f.kind == "text" and f.style == "partial" and applies(f.type, f.text)

    def _custom(self, f: Finding) -> bool:
        return f.style == "custom" and bool(f.spans) and self.can_customize(f)

    def _mask_of(self, f: Finding) -> list[tuple[int, int]]:
        """The ranges of the finding's text that are masked under its style."""
        if self._custom(f):
            return list(f.spans)
        return mask_spans(f.type, f.text) if self._partial(f) else [(0, len(f.text))]

    @staticmethod
    def _follows(f: Finding, o: Occurrence, items: list[tuple[Finding, Occurrence]]) -> bool:
        """True when this place lies inside a longer place that decides how it is masked: one of the same type,
        or a mailbox. Inside a mailbox the type does not matter: the model lists the account before the @ as an
        identifier of its own, and adding that up would mask the characters the mailbox was asked to keep."""
        return any(g is not f and (g.type == f.type or g.type == MAILBOX) and p.start <= o.start and o.end <= p.end
                   and p.end - p.start > o.end - o.start for g, p in items)

    def _places(self) -> dict[tuple[str, str], list[tuple[Finding, Occurrence]]]:
        """(kind, unit) -> every active place in text and file properties, with its finding."""
        groups: dict[tuple[str, str], list[tuple[Finding, Occurrence]]] = {}
        for f in self.findings:
            for o in f.occurrences:
                if o.kind != "box" and self._active(f, o):
                    groups.setdefault((o.kind, o.unit), []).append((f, o))
        return groups

    def _masked_ranges(self, whole: bool = False) -> tuple[dict[tuple[str, str], list[tuple[str, int, int]]], set[str]]:
        """What will really be masked: (kind, unit) -> (finding id, start, end) for every active place in text
        and file properties, and the strings that a longer, partially masked finding leaves readable somewhere.

        A place lying inside a longer place of the same type, or inside a mailbox, follows the longer one and adds
        nothing itself: 王小明 masked as 王■明 must not be undone by 小明 masking its second character. Other
        types add up. `whole` ignores styles: every finding covers all of its text (what the code version needs)."""
        out: dict[tuple[str, str], list[tuple[str, int, int]]] = {}
        follows: set[str] = set()
        for key, items in self._places().items():
            rows = out.setdefault(key, [])
            inner = []
            for f, o in items:
                if self._follows(f, o, items):
                    inner.append((f, o))
                    continue
                size = o.end - o.start
                spans = self._mask_of(f) if not whole and size == len(f.text) else [(0, size)]
                rows.extend((f.id, o.start + s, o.start + e) for s, e in spans)
            for f, o in inner:
                if f.kind == "text" and not any(s < o.end and o.start < e for _fid, s, e in rows):
                    follows.add(f.text)  # none of its characters is masked there: the string is still readable
        return out, follows

    def _covers_everything(self, f: Finding) -> bool:
        """True when every place the string occurs is being masked, so copies in parts the screen does
        not show (alt text, field codes, embedded workbooks) may be masked as well."""
        textual = [o for o in f.occurrences if o.kind in ("text", "meta")]
        if not textual or not all(self._active(f, o) for o in textual):
            return False  # switching off a match inside an image must not change what happens to text
        have = {(o.unit, o.start, o.end) for o in textual}
        for unit in self.inv.texts:
            if any((unit.id, s, e) not in have for s, e in self._find(unit, f.text)):
                return False
        for unit in self.inv.metas:
            if any((unit.id, s, e) not in have for s, e in find_in_text(unit.value, f.text)):
                return False
        return True

    def partial_strings(self) -> list[str]:
        """Active strings with at least one textual place left unmasked: copies of these may remain by choice."""
        return [f.text for f in self.findings if f.kind == "text" and f.on and f.text
                and any(o.kind in ("text", "meta") and self._active(f, o) for o in f.occurrences)
                and not self._covers_everything(f)]

    def redundant(self) -> set[str]:
        """Strings the agent added that change nothing: every place they occur lies inside a longer place another
        finding is masking (the part of a mailbox before the @, when the mailbox is masked by rule), or inside a
        longer place that decides for it (see _follows). They stay in
        the store, so the output is the same with or without them, and are left out of what the reviewer is shown.
        Only the agent's: a detected name inside a longer mask is the entry the reviewer wants to see.

        What hides a string is what the reviewer is shown. Two of the agent's strings that fill each other's gaps
        (王小明與 and 與陳美玲 beside the names 王小明 and 陳美玲) would otherwise both go, and 與 would be masked with
        no entry on the list saying so: the first of them stays on the list."""
        ranges, _readable = self._masked_ranges()
        places = self._places()
        boxes: dict[str, list[tuple[str, Box]]] = {}
        for g in self.findings:
            for o in g.occurrences:
                if o.kind == "box" and o.box is not None and self._active(g, o):
                    boxes.setdefault(o.unit, []).append((g.id, o.box))

        def inside(f: Finding, o: Occurrence, unseen: set[str]) -> bool:
            if o.kind != "box":  # every character of it is masked by what other findings do there, or it follows one
                others = _merge([(s, e) for fid, s, e in ranges.get((o.kind, o.unit), []) if fid != f.id and fid not in unseen])
                return any(s <= o.start and o.end <= e for s, e in others) or self._follows(
                    f, o, [(g, p) for g, p in places.get((o.kind, o.unit), []) if g.id not in unseen])
            return o.box is not None and any(
                b != o.box and b[0] <= o.box[0] and b[1] <= o.box[1] and b[2] >= o.box[2] and b[3] >= o.box[3]
                for gid, b in boxes.get(o.unit, []) if gid not in unseen)

        mine = [f for f in self.findings if f.kind == "text" and f.source == "agent" and f.occurrences]
        hidden = {f.id for f in mine if all(inside(f, o, set()) for o in f.occurrences)}
        for f in mine:  # one pass is enough: taking a string back onto the list only adds to what hides the rest
            if f.id in hidden and not all(inside(f, o, hidden - {f.id}) for o in f.occurrences):
                hidden.discard(f.id)
        return hidden

    def listed(self) -> list[Finding]:
        """What the review screen and the report show."""
        hidden = self.redundant()
        return [f for f in self.findings if f.id not in hidden]

    def shown_as(self, f: Finding, full: str = "■", half: str = "*") -> str:
        """What a text finding looks like once masked; empty for regions."""
        if f.kind == "region" or not f.text:
            return ""
        return masked(f.text, self._mask_of(f), full, half)

    def report(self, full: str = "■", half: str = "*") -> list[dict]:
        return [{"id": f.id, "kind": f.kind, "type": f.type, "text": f.text, "source": f.source, "on": f.on,
                 "reason": f.reason, "count": len(f.occurrences),
                 "active": sum(1 for o in f.occurrences if o.on) if f.on else 0,
                 "style": "custom" if self._custom(f) else "partial" if self._partial(f) else "full",
                 "masked": self.shown_as(f, full, half)}
                for f in self.listed()]

    def active_boxes(self, image_id: str) -> list[tuple[Box, Finding]]:
        """The boxes that will be covered in one picture, each with the finding it comes from."""
        return [(o.box, f) for f in self.findings for o in f.occurrences
                if o.kind == "box" and o.unit == image_id and o.box is not None and self._active(f, o)]

    def masked_ranges(self, whole: bool = False) -> dict[tuple[str, str], list[tuple[str, int, int]]]:
        """(kind, unit) -> (finding id, start, end): the ranges masked in text and file properties, and by whom."""
        return self._masked_ranges(whole)[0]

    def plan(self, whole: bool = False) -> MaskPlan:
        ranges, follows = self._masked_ranges(whole)
        text = {unit: _merge([(s, e) for _fid, s, e in rows]) for (kind, unit), rows in ranges.items() if kind == "text" and rows}
        meta = {unit: _merge([(s, e) for _fid, s, e in rows]) for (kind, unit), rows in ranges.items() if kind == "meta" and rows}
        boxes: dict[str, list] = {}
        strings: list[str] = []
        partial: dict[str, list] = {}
        for f in self.findings:
            if not f.on:
                continue
            for o in f.occurrences:
                if o.kind == "box" and o.box is not None and self._active(f, o) and o.box not in boxes.setdefault(o.unit, []):
                    boxes[o.unit].append(o.box)
            if f.kind == "text" and f.text and f.text not in strings and self._covers_everything(f):
                strings.append(f.text)
                if not whole and (self._partial(f) or self._custom(f)):
                    partial[f.text] = self._mask_of(f)
        # A string the longer finding leaves readable is still masked wherever else it stands (the writers search
        # for it), but it cannot be required to be gone: it is listed apart, and where it remains is reported.
        return MaskPlan(text=text, meta=meta, boxes={u: b for u, b in boxes.items() if b},
                        strings=[s for s in strings if s not in follows], partial=partial,
                        lenient=[s for s in strings if s in follows])

    def to_json(self) -> dict:
        return {"next": self._next, "styles": dict(self.styles), "findings": [to_json(f) for f in self.findings]}

    @classmethod
    def from_json(cls, inv: Inventory, data: dict, locate=None) -> "FindingStore":
        store = cls(inv, locate)
        store.findings = [finding_from_json(d) for d in data.get("findings", [])]
        store._next = int(data.get("next", len(store.findings) + 1))
        store.styles = {str(k): str(v) for k, v in (data.get("styles") or {}).items()}
        return store
