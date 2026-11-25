"""What a spreadsheet's number format makes of a whole number, for the formats people use to write identifiers:
digit places and literal characters, nothing else. A phone stored as 912345678 with the format 0000000000 is
0912345678 on the screen, and an ID stored as 223456789 with "A"000000000 is A223456789; that is the string a
person, a rule or the model looks for.

Deliberately narrow. Amounts (grouping, decimals, percent, exponents, fractions), dates and anything this does
not recognise give None, and the caller keeps the stored spelling: guessing would show text that is not in the
workbook. A format with no digit place gives None as well - it hides the number on the screen, but the number
is still in the cell and has to stay readable here."""
import re
import unicodedata
from decimal import Decimal, InvalidOperation

_AS_IS = set(" -+():!^&'~{}<>=")  # shown as written without quotes
_COLOUR = re.compile(r"black|blue|cyan|green|magenta|red|white|yellow|color\s*\d+", re.I)
_CONDITION = re.compile(r"\[(<=|>=|<>|<|>|=)\s*(-?\d+(?:\.\d+)?)\]")
_COMPARE = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
            "=": lambda a, b: a == b, "<>": lambda a, b: a != b}
_PAD = {"0": "0", "#": "", "?": " "}


def _sections(code: str) -> list[str] | None:
    out, buf, i = [], [], 0
    while i < len(code):
        c = code[i]
        if c == '"':
            j = code.find('"', i + 1)
            if j < 0:
                return None
            buf.append(code[i:j + 1])
            i = j + 1
        elif c == "\\":
            buf.append(code[i:i + 2])
            i += 2
        elif c == "[":
            j = code.find("]", i)
            if j < 0:
                return None
            buf.append(code[i:j + 1])
            i = j + 1
        elif c == ";":
            out.append("".join(buf))
            buf = []
            i += 1
        else:
            buf.append(c)
            i += 1
    return out + ["".join(buf)]


def _tokens(section: str) -> list[tuple[bool, str]] | None:
    """(is a digit place, text) for each piece of one section; None when it holds anything else."""
    out, i = [], 0
    while i < len(section):
        c = section[i]
        if c == '"':
            j = section.find('"', i + 1)
            out.append((False, section[i + 1:j]))
            i = j + 1
        elif c == "\\":
            if i + 1 >= len(section):
                return None
            out.append((False, section[i + 1]))
            i += 2
        elif c == "[":
            j = section.find("]", i)
            body = section[i + 1:j]
            if body.startswith("$"):  # [$NT$-404]: a currency sign to show and a locale; [$-404]: a locale only
                out.append((False, body[1:].split("-")[0]))
            elif not (_COLOUR.fullmatch(body) or _CONDITION.fullmatch(section[i:j + 1])):
                return None  # other numerals, elapsed time, ...
            i = j + 1
        elif c in _PAD:
            out.append((True, c))
            i += 1
        elif c in _AS_IS or ord(c) > 127:
            out.append((False, c))
            i += 1
        else:
            return None
    return out


def shown(value: str, code: str) -> str | None:
    """The text a cell holding the number `value` shows under the format `code`, or None (see above)."""
    try:
        number = Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        return None
    if not number.is_finite() or number != number.to_integral_value():
        return None
    whole = int(number)
    sections = _sections(code or "")
    if not sections:
        return None
    conditions = [_CONDITION.search(s) for s in sections]
    sign = ""
    if any(conditions):
        if whole < 0:
            return None
        section = next((s for s, c in zip(sections, conditions)
                        if c is None or _COMPARE[c.group(1)](number, Decimal(c.group(2)))), None)
    elif len(sections) == 1:
        section, sign = sections[0], "-" if whole < 0 else ""
    elif len(sections) == 2:
        section = sections[0] if whole >= 0 else sections[1]
    else:
        section = sections[0] if whole > 0 else sections[1] if whole < 0 else sections[2]
    tokens = _tokens(section) if section is not None else None
    places = [i for i, (place, _) in enumerate(tokens or []) if place]
    if not places:
        return None
    digits = str(abs(whole)) if whole else ""
    out = [text for _, text in tokens]
    at = len(digits) - 1
    for i in reversed(places):
        out[i] = digits[at] if at >= 0 else _PAD[tokens[i][1]]
        at -= 1
    if at >= 0:  # more digits than places: the first place takes the rest
        out[places[0]] = digits[:at + 1] + out[places[0]]
    return sign + "".join(out)


def literals(code: str) -> list[tuple[int, int]]:
    """The stretches of a format that are text a person typed, shown as written: what stands between quotes, and
    runs of wide characters written without them (yyyy年m月d日). Not the codes, not what stands in brackets (a
    colour, a locale, a currency sign), not a single character behind a backslash. A name written into a format
    is in the workbook like any other text; these are the places to read it and to mask it without touching the
    codes around it."""
    out: list[tuple[int, int]] = []
    i, run = 0, None
    while i <= len(code):
        c = code[i] if i < len(code) else ""
        wide = bool(c) and c not in '"\\[' and unicodedata.east_asian_width(c) in "WFA"
        if wide:
            run = i if run is None else run
            i += 1
            continue
        if run is not None:
            out.append((run, i))
            run = None
        if c == '"':
            end = code.find('"', i + 1)
            end = len(code) if end < 0 else end
            if end > i + 1:
                out.append((i + 1, end))
            i = end + 1
        elif c == "\\":
            i += 2
        elif c == "[":
            end = code.find("]", i)
            i = len(code) + 1 if end < 0 else end + 1
        else:
            i += 1
    return out
