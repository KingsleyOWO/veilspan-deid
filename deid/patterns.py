"""Pattern candidates. They never decide masking alone, except the safety net: checksum-verified ids, and the
kinds the owner decided are always masked (phone numbers, mailboxes, customer or member numbers in a link).
The model is not asked to judge these: asking it to list an organisation's contacts cost it personal names."""
import re
from dataclasses import dataclass

_LETTER = dict(zip("ABCDEFGHJKLMNPQRSTUVXYWZIO", range(10, 36)))
_ID = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-D1289]\d{8}(?![A-Za-z0-9])")
_CARD = re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])")
_MOBILE = re.compile(r"(?<![\d-])(?:\+886[- ]?9|09)\d{2}[- ]?\d{3}[- ]?\d{3}(?![\d-])")
# A landline as it is written: area code in brackets or followed by a separator, or +886. Nine or ten digits.
_LANDLINE = re.compile(r"(?<![\d\-.])(?:\+886[\s\-]?\d{1,2}[\s\-]?|\(0\d{1,3}\)\s?|0\d{1,3}[\s\-])\d{3,4}[\s\-]?\d{3,4}(?![\d\-.])")
# A number without its area code is a phone number only when the text says so, with the keyword as a word of its
# own. In English it is not the end of a longer word (Hotel, Intel; Telefax is a keyword itself). In Chinese it does
# not begin a word for a thing: 電話費, 電話卡, 電話簿, 電話亭, 傳真紙, 總機構, and 電話機 or 傳真機 unless 號 or a
# colon makes them a label for a number (傳真機號碼：, 傳真機：). Nothing else is left out: a missed phone number is
# worse than a masked year range.
_LOCAL = re.compile(r"(?:(?:電話|總機|專線|傳真|熱線)(?![費卡簿亭紙構]|機(?![號:：]))"
                    r"|(?<![A-Za-z])(?i:telefax|tel|fax))"
                    r"[^\d\n]{0,4}?(?<![\d\-])(\d{4}[\s\-]\d{4})(?![\d\-.])")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_URL = re.compile(r"(?i)(?:https?://|www\.)[^\s<>\"'，。、；）】」]+")
# A query parameter named for a customer or member: cust, CustomerNo, customer_id, MemberNo, member-code ...
# Its value ends at the next parameter, at the end of the link, or where the sentence takes over: Chinese text
# written straight after the link, or punctuation that closes the sentence or a bracket and ends the link.
_CUSTOMER = re.compile(r"(?i)[?&](?:customer|cust|member)(?:[_-]?(?:no|id|num|number|code))?=([A-Za-z0-9_-]{2,40})"
                       r"(?=$|[&#]|[\u3000-\u9fff\uf900-\ufaff\uff00-\uffef]|[).,;:!?\]]+$)")

ALWAYS = {"電話": "電話號碼一律遮蔽", "手機": "電話號碼一律遮蔽", "電子郵件": "電子郵件信箱一律遮蔽",
          "客戶編號": "網址參數是客戶或會員編號"}  # kind -> reason shown


@dataclass
class Candidate:
    text: str
    kind: str
    start: int
    end: int
    verified: bool


def _tw_checksum(s: str) -> bool:
    first = _LETTER[s[0]]
    second = int(s[1]) if s[1].isdigit() else _LETTER[s[1]] % 10
    digits = [first // 10, first % 10, second] + [int(c) for c in s[2:]]
    return sum(d * w for d, w in zip(digits, (1, 9, 8, 7, 6, 5, 4, 3, 2, 1, 1))) % 10 == 0


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _card_run(run: str) -> str:
    """A card-like run that fails the checksum can be a card number followed by a date or an amount
    (卡號 4111 1111 1111 1111 12/28). Then the longest part of it that ends at a space or a dash, holds 13 digits or
    more and passes is the card number; failing that, the run as it is."""
    if _luhn(re.sub(r"\D", "", run)):
        return run
    for m in reversed(list(re.finditer(r"[ -]", run))):
        digits = re.sub(r"\D", "", run[:m.start()])
        if len(digits) >= 13 and _luhn(digits):
            return run[:m.start()]
    return run


def candidates(text: str) -> list[Candidate]:
    out: list[Candidate] = []
    for m in _ID.finditer(text):
        if _tw_checksum(m.group()):
            kind = "身分證字號" if m.group()[1] in "12" else "居留證號"
            out.append(Candidate(m.group(), kind, m.start(), m.end(), True))
    cards = []
    for m in _CARD.finditer(text):
        run = _card_run(m.group())
        digits = re.sub(r"\D", "", run)
        if 13 <= len(digits) <= 19:
            cards.append(Candidate(run, "信用卡號", m.start(), m.start() + len(run), _luhn(digits)))
    # A card-like run does not hide a phone number written with its area code: a card is grouped in fours (or
    # 4-6-5) and never holds one, so the run is the phone number followed by a date or an amount, whether or not
    # its digits happen to pass the checksum, and it is no card number. A run the safety net masks as a card number
    # stays when the phone number only begins inside it (卡號 4766 5169 0529 404 234 元: the last group of the card
    # and the amount after it read as an area code and a number); both are masked then, and nothing is lost.
    taken = [(c.start, c.end) for c in out]
    for m in _MOBILE.finditer(text):
        if not any(s <= m.start() and m.end() <= e for s, e in taken):
            out.append(Candidate(m.group(), "手機", m.start(), m.end(), False))
            taken.append((m.start(), m.end()))
    for m in _LANDLINE.finditer(text):
        digits = re.sub(r"\D", "", m.group()).removeprefix("886")
        if 8 <= len(digits) <= 10 and not any(s <= m.start() and m.end() <= e or m.start() <= s and e <= m.end() for s, e in taken):
            out.append(Candidate(m.group().strip(), "電話", m.start(), m.start() + len(m.group().strip()), False))
            taken.append((m.start(), m.end()))
    phones = [(c.start, c.end) for c in out if c.kind in ("手機", "電話")]
    cards = [c for c in cards if not any(s < c.end and c.start < e and (s <= c.start or not is_safety_net(c, text))
                                         for s, e in phones)]
    out += cards
    # Eight digits after 電話 that run on into more digits are still the phone number, unless the run is a card
    # number the safety net masks: then the whole run is masked, these digits with it. Any other card-like run is
    # offered beside the phone number for the model to judge.
    for m in _LOCAL.finditer(text):
        start, end = m.span(1)
        if any(s <= start < e for s, e in taken):
            continue
        if any(c.start < end and start < c.end and is_safety_net(c, text) for c in cards):
            continue
        out.append(Candidate(m.group(1), "電話", start, end, False))
    for m in _EMAIL.finditer(text):
        out.append(Candidate(m.group(), "電子郵件", m.start(), m.end(), False))
    for url in _URL.finditer(text):
        for m in _CUSTOMER.finditer(url.group()):
            out.append(Candidate(m.group(1), "客戶編號", url.start() + m.start(1), url.start() + m.end(1), False))
    return sorted(out, key=lambda c: c.start)


def is_safety_net(c: Candidate, text: str) -> bool:
    """True when the candidate should be masked even if the model did not list it."""
    if c.kind in ALWAYS:
        return True
    if not c.verified:
        return False
    if c.kind == "信用卡號":
        return "卡" in text[max(0, c.start - 12): c.start]
    return c.kind in ("身分證字號", "居留證號")
