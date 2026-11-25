"""Which cells a spreadsheet formula reads. Used to find the cells computed from a masked cell; what is not
understood (INDIRECT, OFFSET, references into other workbooks) is simply not followed."""
import re

MAX_ROW, MAX_COL = 1_048_576, 16_384
LITERAL = re.compile(r'"(?:[^"]|"")*"')
_NAME = r"[^\s!'\"()+\-*/^&=<>,;:\[\]{}%@#]+"
# a sheet, or the first and last of several (a 3-D reference: First:Last!A1, 'First:Last'!A1)
_SHEET = rf"(?:'((?:[^']|'')+)'|({_NAME}(?::{_NAME})?))!"
_CELL = r"(\$?)([A-Za-z]{1,3})(\$?)([1-9]\d{0,6})"
# an optional sheet, then a cell or a range of cells, whole columns (B:D) or whole rows (2:5)
REF = re.compile(rf"(?<![\w.$'\]])(?:{_SHEET})?(?:{_CELL}(?::{_CELL})?|(\$?)([A-Za-z]{{1,3}}):(\$?)([A-Za-z]{{1,3}})"
                 rf"|(\$?)([1-9]\d{{0,6}}):(\$?)([1-9]\d{{0,6}}))(?![\w.(\[!])")
_ADDRESS = re.compile(r"\$?([A-Za-z]{1,3})\$?([1-9]\d{0,6})")


def column_number(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + ord(ch) - 64
    return n


def column_letters(n: int) -> str:
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def position(address: str) -> tuple[int, int] | None:
    """(column, row) of an address such as "C5"."""
    m = _ADDRESS.fullmatch(address or "")
    return (column_number(m.group(1)), int(m.group(2))) if m else None


def area(text: str) -> tuple[int, int, int, int] | None:
    """(first column, first row, last column, last row) of "A1:C3" or "B2"."""
    first, _, last = text.partition(":")
    a, b = position(first), position(last or first)
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])) if a and b else None


def reads(formula: str, down: int = 0, right: int = 0) -> list[tuple[str | None, int, int, int, int]]:
    """Every area the formula names, as (sheet or None for its own sheet, first column, first row, last column,
    last row). `down` and `right` move the relative parts, for a cell that repeats a filled-down formula. A
    3-D reference gives "First:Last" as its sheet (a sheet's name cannot hold a colon): see `sheets_between`."""
    out = []
    for m in REF.finditer(LITERAL.sub('""', formula)):
        sheet = m.group(1).replace("''", "'") if m.group(1) else m.group(2)

        def col(fixed, letters):
            return column_number(letters) + (0 if fixed else right)

        def row(fixed, digits):
            return int(digits) + (0 if fixed else down)

        if m.group(4):
            c1, r1 = col(m.group(3), m.group(4)), row(m.group(5), m.group(6))
            c2, r2 = (col(m.group(7), m.group(8)), row(m.group(9), m.group(10))) if m.group(8) else (c1, r1)
        elif m.group(12):
            c1, c2, r1, r2 = col(m.group(11), m.group(12)), col(m.group(13), m.group(14)), 1, MAX_ROW
        else:
            r1, r2, c1, c2 = row(m.group(15), m.group(16)), row(m.group(17), m.group(18)), 1, MAX_COL
        out.append((sheet, min(c1, c2), min(r1, r2), max(c1, c2), max(r1, r2)))
    return out


def sheets_between(sheet: str, order: list[str]) -> list[str]:
    """The sheets a reference names: one, or for "First:Last" every sheet from the one to the other in tab
    order. Empty when a name is not one of `order` (another workbook)."""
    if sheet in order:
        return [sheet]
    first, colon, last = sheet.partition(":")
    if not colon or first not in order or last not in order:
        return []
    a, b = sorted((order.index(first), order.index(last)))
    return order[a:b + 1]


def structured(formula: str, tables: dict, here: tuple[str, int, int]) -> list[tuple[str | None, int, int, int, int]]:
    """Areas named through a table: Table[column], Table[[#This Row],[column]], [@column]. `tables` maps a table
    name to a dict (entry, sheet, c1, r1, c2, r2, columns); `here` is (entry, column, row) of the formula's cell."""
    out, text, i = [], LITERAL.sub('""', formula), 0
    while (start := text.find("[", i)) >= 0:
        depth, end = 0, start
        while end < len(text):
            depth += (text[end] == "[") - (text[end] == "]")
            if depth == 0:
                break
            end += 1
        inner, i = text[start + 1:end], end + 1
        before = re.search(_NAME + r"$", text[:start])
        if before:
            table = tables.get(before.group(0))
        elif inner.isdigit():
            continue  # [1]Sheet!A1: another workbook
        else:  # no name: the table the cell itself sits in
            table = next((t for t in tables.values() if t["entry"] == here[0] and t["c1"] <= here[1] <= t["c2"]
                          and t["r1"] <= here[2] <= t["r2"]), None)
        if table is None:
            continue
        this_row = "@" in inner or "#This Row" in inner
        r1, r2 = (here[2], here[2]) if this_row else (table["r1"], table["r2"])
        named = [n for n, name in enumerate(table["columns"]) if name and name in inner]
        for n in named or range(len(table["columns"])):
            out.append((table["sheet"], table["c1"] + n, r1, table["c1"] + n, r2))
    return out
