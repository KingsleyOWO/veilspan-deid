"""Token-level XML editing. The raw text is spliced in place so untouched bytes stay exactly as they were;
no XML library rewrites namespaces or formatting."""
import html
import re
import unicodedata
from typing import Iterator
from xml.sax.saxutils import escape

TOKEN = re.compile(r"<!--.*?-->|<\?.*?\?>|<!\[CDATA\[.*?\]\]>|<![^>]*>|<[^>]+>|[^<]+", re.S)
TAGNAME = re.compile(r"</?([A-Za-z_][\w:.\-]*)")
ATTR = re.compile(r"""([A-Za-z_][\w:.\-]*)\s*=\s*("([^"]*)"|'([^']*)')""")
CDATA_OPEN, CDATA_CLOSE = "<![CDATA[", "]]>"
TEXT_KINDS = ("text", "cdata")


def mask_chars(s: str, full: str, half: str) -> str:
    """Same length, whitespace kept; wide characters get `full`, the rest get `half`."""
    return "".join(c if c.isspace() else (full if unicodedata.east_asian_width(c) in "WFA" else half) for c in s)


def mask_string(s: str, full: str, half: str, partial: dict[str, list[tuple[int, int]]] | None = None) -> str:
    """Mask one string found by search: all of it, or only the ranges `partial` lists for that string."""
    spans = partial.get(s) if partial else None
    if spans is None:
        return mask_chars(s, full, half)
    flags = bytearray(len(s))
    for a, b in spans:
        for i in range(max(0, a), min(len(s), b)):
            flags[i] = 1
    return "".join(mask_chars(c, full, half) if flags[i] else c for i, c in enumerate(s))


def searcher(strings: list[str], full: str, half: str, partial: dict[str, list[tuple[int, int]]] | None = None):
    """(pattern, replace) for masking `strings` wherever a search meets them.

    A string masked in part is also recognised in its masked form and left as it is. Without that, a shorter
    string lying in the readable part (the domain of l**n@domain) would be found there and masked again, and
    the only way to avoid it would be not to search for the shorter string at all, leaving its other copies."""
    shown = {mask_string(s, full, half, partial) for s in (partial or {})} - set(strings)
    pattern = compile_strings(list(strings) + sorted(shown))

    def replace(m) -> str:
        return m.group(0) if m.group(0) in shown else mask_string(m.group(0), full, half, partial)

    return pattern, replace


def iter_tokens(xml: str) -> Iterator[tuple[str, str, int, int]]:
    """Yields (kind, name, start, end); kind is text | cdata | open | close | empty | other. A CDATA section is
    text written as it stands; its span is the whole section, markers included (see node_text)."""
    for m in TOKEN.finditer(xml):
        tok = m.group(0)
        if tok[0] != "<":
            yield "text", "", m.start(), m.end()
        elif tok.startswith(CDATA_OPEN):
            yield "cdata", "", m.start(), m.end()
        elif tok[1] in "!?":
            yield "other", "", m.start(), m.end()
        else:
            name = TAGNAME.match(tok)
            if not name:
                yield "other", "", m.start(), m.end()
            elif tok[1] == "/":
                yield "close", name.group(1), m.start(), m.end()
            elif tok.endswith("/>"):
                yield "empty", name.group(1), m.start(), m.end()
            else:
                yield "open", name.group(1), m.start(), m.end()


def node_text(xml: str, kind: str, start: int, end: int) -> str:
    """The text a text token holds: entities resolved, or the content of a CDATA section as it stands. Writing
    escape(new) over the token's whole span leaves an ordinary text node in either case, so writers need not
    tell the two apart."""
    if kind == "cdata":
        return xml[start + len(CDATA_OPEN):end - len(CDATA_CLOSE)]
    return html.unescape(xml[start:end])


def splice(xml: str, reps: list[tuple[int, int, str]]) -> str:
    """Write the replacements (start, end, new text) into the string in one pass. Where two overlap, the one
    that starts first - the outer one - wins, so a piece inside an element that is replaced whole is dropped."""
    if not reps:
        return xml
    out, pos = [], 0
    for s, e, new in sorted(reps, key=lambda r: (r[0], -r[1])):
        if s < pos:
            continue
        out.append(xml[pos:s])
        out.append(new)
        pos = e
    out.append(xml[pos:])
    return "".join(out)


OPEN_TAG = re.compile(r"<([\w:.-]+)[^<>]*>\Z")
KEEP_SPACE = {"w:t", "w:delText", "t"}  # elements that drop a leading or trailing space unless told to keep it


def respell(xml: str, pieces: list[tuple[int | None, int | None, str]], subs: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """Replacements that write other strings over stretches of one paragraph. `pieces` are its text tokens in
    order as (start, end, text) in the raw XML; a separator between them (a tab, a line break) has no position.
    `subs` are (start, end, string) in the paragraph's text. The string goes into the token its stretch begins
    in and the rest of the stretch is taken out of the tokens it ran through: tokens are neither added nor
    removed, so the formatting of the first one applies."""
    emit: dict[int, str] = {}
    for s, e, string in subs:
        emit[s] = string
        for i in range(s + 1, e):
            emit[i] = ""
    reps, pos = [], 0
    for s, e, text in pieces:
        new = "".join(emit.get(pos + i, c) for i, c in enumerate(text))
        pos += len(text)
        if s is None or new == text:
            continue
        reps.append((s, e, escape(new)))
        if new != new.strip():
            at = xml.rfind("<", 0, s)
            tag = OPEN_TAG.match(xml, at, s)
            if tag and tag.group(1) in KEEP_SPACE and "xml:space" not in tag.group(0):
                reps.append((at, s, tag.group(0)[:-1] + ' xml:space="preserve">'))
    return reps


def token_pattern(s: str) -> str:
    """Regex for one string that will not match inside a longer number or a longer Latin word: "522" must
    not hit "0912-345-522" or "15223", "Iris" must not hit "Irish"."""
    head = tail = ""
    if s[0].isascii() and s[0].isdigit():
        head = r"(?<!\d)(?<!\d[\-.])"
    elif s[0].isascii() and s[0].isalpha():
        head = r"(?<![A-Za-z])"
    if s[-1].isascii() and s[-1].isdigit():
        tail = r"(?!\d)(?![\-.]\d)"
    elif s[-1].isascii() and s[-1].isalpha():
        tail = r"(?![A-Za-z])"
    return head + re.escape(s) + tail


def compile_strings(strings: list[str]) -> re.Pattern | None:
    strings = sorted({s for s in strings if s}, key=len, reverse=True)
    return re.compile("|".join(token_pattern(s) for s in strings)) if strings else None


def attr_values(tag: str) -> Iterator[tuple[str, str, int, int]]:
    """(name, unescaped value, value start, value end) for each attribute in a tag; offsets exclude quotes."""
    for m in ATTR.finditer(tag):
        g = 3 if m.group(3) is not None else 4
        yield m.group(1), html.unescape(m.group(g)), m.start(g), m.end(g)


def is_target(stack: list[str], elements, prefixes, under) -> bool:
    if not stack:
        return False
    name = stack[-1]
    if name in under:
        return any(a in stack for a in under[name])
    if elements is None:
        return True
    return name in elements or (bool(prefixes) and name.startswith(prefixes))


def replace_in_text_nodes(xml: str, strings: list[str], elements: set[str] | None, attrs: set[str], full: str, half: str,
                          *, prefixes: tuple[str, ...] = (), under: dict[str, set[str]] | None = None,
                          partial: dict[str, list[tuple[int, int]]] | None = None) -> str:
    """Mask every occurrence of `strings` inside text nodes of the chosen elements and inside the chosen
    attributes. elements=None means all text nodes; `under` restricts an element to given ancestors;
    `partial` lists the strings that keep some characters (see mask_string)."""
    pattern, sub = searcher(strings, full, half, partial)
    if pattern is None:
        return xml
    under = under or {}
    reps: list[tuple[int, int, str]] = []
    stack: list[str] = []
    for kind, name, s, e in iter_tokens(xml):
        if kind in TEXT_KINDS:
            if is_target(stack, elements, prefixes, under):
                plain = node_text(xml, kind, s, e)
                new = pattern.sub(sub, plain)
                if new != plain:
                    reps.append((s, e, escape(new)))
        elif kind in ("open", "empty"):
            if attrs:
                tag = xml[s:e]
                for aname, value, vs, ve in attr_values(tag):
                    if aname in attrs:
                        new = pattern.sub(sub, value)
                        if new != value:
                            reps.append((s + vs, s + ve, escape(new, {'"': "&quot;", "'": "&apos;"})))
            if kind == "open":
                stack.append(name)
        elif kind == "close":
            while stack and stack[-1] != name:
                stack.pop()
            if stack:
                stack.pop()
    return splice(xml, reps)


def find_in_text_nodes(xml: str, strings: list[str], attrs: set[str]) -> set[str]:
    """Which of `strings` still occur in any text node or in the given attributes."""
    pattern = compile_strings(strings)
    found: set[str] = set()
    if pattern is None:
        return found
    for kind, _name, s, e in iter_tokens(xml):
        if kind in TEXT_KINDS:
            found.update(pattern.findall(node_text(xml, kind, s, e)))
        elif kind in ("open", "empty") and attrs:
            for aname, value, _vs, _ve in attr_values(xml[s:e]):
                if aname in attrs:
                    found.update(pattern.findall(value))
    return found
