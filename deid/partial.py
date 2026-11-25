"""Partial masking: which characters of a finding are masked when its style is "partial".

A rule either fits a string or it does not. When it does not (too short, a form the rule cannot read, or
nothing would end up masked) the whole string is masked: showing less is always the safe side."""
import re

from deid.formats.xmltok import mask_chars

Span = tuple[int, int]

PARTIAL_TYPES = ("姓名", "電話", "電子郵件", "身分證字號", "護照號碼", "其他識別碼", "地址", "出生年月日")
IDENTIFIERS = ("身分證字號", "護照號碼", "其他識別碼")
TITLES = ("先生", "小姐", "女士", "太太", "老師")  # not personal data: kept, the rule applies to what is before
NAME_JOINERS = " \t.-·‧・'’,，"
_AREA = "區鄉鎮市縣"
ADDRESS = re.compile(rf"\s*(?:\d{{3}}(?:-?\d{{2,3}})?\s*)?(?:[^{_AREA}\s\d]{{1,3}}?[市縣])?(?:[^{_AREA}\s\d]{{1,3}}?(?:[鎮市]區|[區鄉鎮市]))?")
CJK_NUMERALS = "〇零一二三四五六七八九十廿卅百元正"
PHONE_WORDS = "（）－轉分機"  # besides digits, spaces and ASCII punctuation: what may stay readable around a number
DATE_WORDS = "年月日號民國西元前出生"
EMAIL = re.compile(r"\s*([^\s@,;，；（(<>]+)@([A-Za-z0-9][A-Za-z0-9.\-]*)")


def _han(c: str) -> bool:
    return "㐀" <= c <= "鿿" or "豈" <= c <= "﫿"


def _latin(c: str) -> bool:
    return c.isascii() and c.isalpha()


def _runs(text: str, test) -> list[Span]:
    out, start = [], None
    for i, c in enumerate(text + "\0"):
        if i < len(text) and test(c):
            start = i if start is None else start
        elif start is not None:
            out.append((start, i))
            start = None
    return out


def _spans(flags: list[bool]) -> list[Span]:
    return _runs("".join("x" if f else " " for f in flags), lambda c: c == "x")


def _only(text: str, flags: list[bool], own, words: str = "") -> list[bool]:
    """A rule decides about the item itself (`own` characters). Whatever else is in the string - a name the
    model returned glued to a number - is masked, except spaces, ASCII punctuation and the few `words`."""
    return [f or not (own(c) or c.isspace() or (c.isascii() and not c.isalnum()) or c in words) for f, c in zip(flags, text)]


def _name(text: str) -> list[bool] | None:
    if text.strip() in TITLES:
        return None
    body = len(text)
    for title in TITLES:
        if text.endswith(title) and len(text.strip()) > len(title):
            body = len(text) - len(title)
            break
    # Anything that is not a letter of a name or a joiner between its parts (a digit, a symbol) is never shown.
    flags = [i < body and not (_han(c) or _latin(c) or c in NAME_JOINERS) for i, c in enumerate(text)]
    for s, e in _runs(text[:body], _han):
        for i in range(s + 1, e - 1 if e - s > 2 else e):
            flags[i] = True
        if e - s == 1:
            flags[s] = True
    for s, e in _runs(text[:body], _latin):
        for i in range(s + 1, e):
            flags[i] = True
        if e - s == 1 and ((s and text[s - 1].isdigit()) or (e < len(text) and text[e].isdigit())):
            flags[s] = True  # a lone letter next to a digit is the start of a number, not an initial
    return flags


def _keep_ends(text: str, counts, head: int, tail: int) -> list[bool]:
    at = [i for i, c in enumerate(text) if counts(c)]
    flags = [False] * len(text)
    for i in at[head:len(at) - tail]:
        flags[i] = True
    return flags


def _phone(text: str) -> list[bool] | None:
    n = sum(c.isdigit() for c in text)
    if n < 8:
        return None
    # The last three and up to four at the front stay, and at least three digits in between are masked.
    return _only(text, _keep_ends(text, str.isdigit, min(4, n - 6), 3), str.isdigit, PHONE_WORDS)


def _email(text: str) -> list[bool] | None:
    m = EMAIL.match(text)  # one address at the start; anything after it is not shown
    if not m:
        return None
    first, at = m.start(1), m.end(1)
    # Before the @ the first and the last character stay (only the first of two, none of one); the domain stays.
    kept = {first} if at - first > 1 else set()
    if at - first > 2:
        kept.add(at - 1)
    return [not (i < first or i in kept or at <= i < m.end()) for i in range(len(text))]


def _identifier(text: str) -> list[bool] | None:
    def own(c: str) -> bool:
        return c.isascii() and c.isalnum()

    if sum(own(c) for c in text) < 6:
        return None
    return _only(text, _keep_ends(text, own, 1, 3), own)


def _address(text: str) -> list[bool] | None:
    m = ADDRESS.match(text)
    if not m or not any(c in _AREA for c in m.group(0)):
        return None
    return [i >= m.end() for i in range(len(text))]


def _birth(text: str) -> list[bool] | None:
    flags = [False] * len(text)
    runs = _runs(text, str.isdigit)
    year = text.find("年")

    def mask(spans):
        for s, e in spans:
            for i in range(s, e):
                flags[i] = True
        return _only(text, flags, str.isdigit, DATE_WORDS)

    if not runs:
        if year <= 0:
            return None
        return _only(text, [i > year and c in CJK_NUMERALS for i, c in enumerate(text)], lambda c: c in CJK_NUMERALS, DATE_WORDS)
    if year > 0:
        if not any(e <= year for _s, e in runs):
            return None
        return mask([r for r in runs if r[0] > year])
    sizes = [e - s for s, e in runs]
    if len(runs) >= 3:
        if sizes[0] == 4 or (sizes[0] in (2, 3) and sizes[-1] <= 2):
            return mask(runs[1:])
        if sizes[-1] == 4:
            return mask(runs[:-1])
        return None
    if len(runs) == 2:
        return mask(runs[1:]) if sizes[0] == 4 else None
    if sizes[0] in (6, 7, 8):  # written without separators: the last four are month and day
        s, e = runs[0]
        return mask([(e - 4, e)])
    return None


RULES = {"姓名": _name, "電話": _phone, "電子郵件": _email, "地址": _address, "出生年月日": _birth,
         **{kind: _identifier for kind in IDENTIFIERS}}


def mask_spans(kind: str, text: str) -> list[Span]:
    """The ranges of `text` that are masked under the partial style; the whole string when no rule fits."""
    if not text:
        return []
    whole = [(0, len(text))]
    rule = RULES.get(kind)
    flags = rule(text) if rule else None
    if not flags:
        return whole
    hidden = [f and not c.isspace() for f, c in zip(flags, text)]
    shown = [not f and not c.isspace() for f, c in zip(flags, text)]
    if not any(hidden) or not any(shown):
        return whole
    return _spans(flags)


def applies(kind: str, text: str) -> bool:
    """True when the partial style would leave some of this string readable."""
    return bool(text) and mask_spans(kind, text) != [(0, len(text))]


def spans_for(kind: str, text: str, style: str) -> list[Span]:
    if style == "partial":
        return mask_spans(kind, text)
    return [(0, len(text))] if text else []


def masked(text: str, spans: list[Span], full: str, half: str) -> str:
    flags = [False] * len(text)
    for s, e in spans:
        for i in range(max(0, s), min(len(text), e)):
            flags[i] = True
    return "".join(mask_chars(c, full, half) if flags[i] else c for i, c in enumerate(text))


def preview(kind: str, text: str, style: str, full: str, half: str) -> str:
    """What the string looks like once masked."""
    return masked(text, spans_for(kind, text, style), full, half)
