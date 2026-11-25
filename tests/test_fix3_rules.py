"""Fixes from the review of 2026-11-23, third round: a year range after a word that only holds a phone keyword, a
phone number followed by more digits, a restore file saved again by Notepad, and the counts given for the coded
file. All names and numbers are made up."""
import codecs
import json
import random
import zipfile

import pytest

from deid import codefile, codes
from deid.patterns import candidates, is_safety_net
from deid.pipeline import build_context
from deid.restorefile import NeedsPassword, NotRestoreFile, WrongKey, open_, pack
from tests.builders import make_docx
from tests.builders import make_encrypted_restore_file as encrypted
from tests.fakes import FakeFaces, FakeOcr, keyword_llm


def net(text):
    return [(c.text, c.kind) for c in candidates(text) if is_safety_net(c, text)]


def phones(text):
    return [c.text for c in candidates(text) if c.kind in ("電話", "手機")]


def cards(text):
    return [(c.text, c.verified) for c in candidates(text) if c.kind == "信用卡號"]


# ---- A1: a keyword inside another word does not make a phone number ----------------------------

@pytest.mark.parametrize("text", [
    "Hotel 2024-2025 season", "會議在 Grand Hotel 2025 2026 年度", "Intel 8086 8088 處理器",  # tel inside a word
    "HOTEL 2024-2025 價目表", "Motel 2024 2025 房價", "Pastel 1999-2000 系列",
    "電話費 2024-2025 年度預算", "傳真機 1999-2000 年採購",  # the keyword begins a word for a thing
    "電話卡 2024-2025 發行量", "電話簿 2024-2025 年版", "電話亭 2024-2025 汰換", "電話機 2024-2025 年採購",
    "傳真紙 2024-2025 年度採購", "傳真費 2024-2025", "專線費 2024-2025 年度", "總機構 2024-2025 年度申報",
    "傳真機型號 1234-5678",
])
def test_a_year_range_after_a_word_that_only_holds_a_phone_keyword_is_not_a_phone_number(text):
    assert phones(text) == [] and net(text) == []


@pytest.mark.parametrize("text, number", [
    ("電話：2345-6789", "2345-6789"), ("電話 2345 6789", "2345 6789"), ("聯絡電話(O)：2345-6789", "2345-6789"),
    ("電話號碼：2345-6789", "2345-6789"), ("客服專線 2345-6789", "2345-6789"), ("TEL: 2345-6789", "2345-6789"),
    ("Tel.2345-6789", "2345-6789"), ("Fax 2345-6789", "2345-6789"), ("傳真：2345-6789", "2345-6789"),
    # a label for the number made from a word for a thing: 號 or a colon follows it
    ("傳真機號碼：2345-6789", "2345-6789"), ("傳真機：2345-6789", "2345-6789"), ("電話機號 2345-6789", "2345-6789"),
    # Telefax ends in fax; a keyword after a Chinese character, a bracket or a slash; Tel followed by a capital
    ("Telefax 2345-6789", "2345-6789"), ("TeleFax 2345-6789", "2345-6789"), ("公司Tel：2345-6789", "2345-6789"),
    ("（TEL）2345-6789", "2345-6789"), ("TEL/FAX: 2345-6789", "2345-6789"), ("Tel/Fax 2345 6789", "2345 6789"),
    ("TelNo: 2345-6789", "2345-6789"), ("tel:2345-6789", "2345-6789"), ("FAX：2345-6789", "2345-6789"),
    # 電話 followed by a word that is not a thing still announces the number
    ("電話/傳真：2345-6789", "2345-6789"), ("請電話洽詢 2345-6789", "2345-6789"), ("電話為 2345-6789", "2345-6789"),
    ("請電話聯絡 2345 6789", "2345 6789"), ("總機 2345-6789", "2345-6789"), ("熱線：2345-6789", "2345-6789"),
    # a word left out does not hide the keyword that comes after it
    ("電話費、傳真 2345-6789", "2345-6789"), ("Hotel 電話 2345-6789", "2345-6789"), ("傳真機/電話：2345-6789", "2345-6789"),
])
def test_a_number_written_as_a_phone_number_is_still_one(text, number):
    assert phones(text) == [number] and (number, "電話") in net(text)


# ---- A2: the phone number after 電話 is kept when more digits follow --------------------------

@pytest.mark.parametrize("text, number, run", [
    ("電話 2345-6789 20241124001", "2345-6789", "2345-6789 20241124001"),
    ("電話：2345 6789 2024 1124 起改號", "2345 6789", "2345 6789 2024 1124"),
    ("TEL 2345-6789 1234 5678 9012", "2345-6789", "2345-6789 1234 5678"),
    ("傳真 2345 6789 0001 2345 6789 01", "2345 6789", "2345 6789 0001 2345"),
])
def test_eight_digits_after_a_phone_keyword_are_masked_when_more_digits_follow(text, number, run):
    assert (number, "電話") in net(text)
    assert run in [t for t, _verified in cards(text)]  # the longer run is still offered for the model to judge


@pytest.mark.parametrize("text, number", [
    ("傳真 卡號 4111 1111 1111 1111", "4111 1111 1111 1111"),
    ("電話 卡號：4111-1111-1111-1111", "4111-1111-1111-1111"),
    ("TEL 卡號 5500 0000 0000 0004 到期", "5500 0000 0000 0004"),
    ("電話卡號：4111 1111 1111 1111", "4111 1111 1111 1111"),
])
def test_a_card_number_with_卡_before_it_after_a_phone_keyword_is_masked_whole(text, number):
    assert phones(text) == []
    assert (number, True) in cards(text) and (number, "信用卡號") in net(text)


@pytest.mark.parametrize("text, number", [
    ("卡號 4111 1111 1111 1111 12/28", "4111 1111 1111 1111"),
    ("卡號：4111111111111111 12/28 到期", "4111111111111111"),
    ("信用卡 4111-1111-1111-1111 2028/12", "4111-1111-1111-1111"),
    ("卡號 3782 822463 10005 12/28", "3782 822463 10005"),
    ("卡號: 4766 5169 0529 404 234 元", "4766 5169 0529 404"),  # 0529 404 234 reads as a landline number
])
def test_a_card_number_followed_by_its_expiry_date_or_an_amount_is_still_found(text, number):
    assert (number, True) in cards(text) and (number, "信用卡號") in net(text)


def check_digit(body: str) -> str:
    """The digit that makes `body` + it pass the card checksum (Luhn), computed here on its own."""
    total = 0
    for i, ch in enumerate(reversed(body)):
        d = int(ch) * (2 if i % 2 == 0 else 1)
        total += d - 9 if d > 9 else d
    return str((10 - total % 10) % 10)


BEFORE = ["", "電話", "電話：", "聯絡電話 ", "TEL ", "Tel.", "Fax ", "傳真：", "總機", "專線 ", "傳真機", "電話費",
          "電話 2345-6789 ", "TEL 2345 6789，", "手機 0912-345-678 ", "(02)2345-6789 ", "王小明 ", "信用", "Hotel ",
          "2024-2025 ", "A123456789 ", "訂單 1234 5678 9012 3456 "]
BETWEEN = ["", "：", ": ", " ", "為 ", "（末碼）"]
AFTER = ["", "。", " 到期", "，有效期限 12/28", " 12/28", " 2028/12 到期", " 234 元", " 電話 2345-6789", " 2345 6789",
         "\n電話：2345-6789", " TEL 2345-6789", "，聯絡電話 02-2345-6789", " 0912-345-678", "、王小明", " 1234 5678 9012"]


def test_a_card_number_after_卡號_is_masked_whole_whatever_stands_around_it():
    rng = random.Random(20261124)
    for _ in range(4000):
        length = rng.randint(13, 19)
        body = rng.choice("3456") + "".join(rng.choice("0123456789") for _ in range(length - 2))
        digits = body + check_digit(body)
        style = rng.choice(["plain", "space", "dash", "amex"])
        if style == "amex" and length == 15:
            number = " ".join([digits[:4], digits[4:10], digits[10:]])
        elif style == "plain":
            number = digits
        else:
            number = ("-" if style == "dash" else " ").join(digits[i:i + 4] for i in range(0, length, 4))
        head = "".join(rng.choice(BEFORE) for _ in range(rng.randint(0, 2))) + "卡號" + rng.choice(BETWEEN)
        text = head + number + rng.choice(AFTER)
        start, end = len(head), len(head) + len(number)
        assert any(c.kind == "信用卡號" and is_safety_net(c, text) and c.start <= start and end <= c.end
                   for c in candidates(text)), text


# ---- A3: a restore file saved again by Notepad ---------------------------------------------------

TABLE = {"[姓名1]": "王小明", "[電話1]": "0912-345-678"}
PASSWORD = "正確的密碼 horse-42"


def utf8_mark(data):
    return codecs.BOM_UTF8 + data


def notepad_unicode(data):  # a mark, then UTF-16 little-endian, with Windows line ends
    return codecs.BOM_UTF16_LE + data.decode("utf-8").replace("\n", "\r\n").encode("utf-16-le")


def utf16_big_endian(data):
    return codecs.BOM_UTF16_BE + data.decode("utf-8").encode("utf-16-be")


SAVED_AGAIN = [utf8_mark, notepad_unicode, utf16_big_endian]


@pytest.mark.parametrize("saved", SAVED_AGAIN)
def test_a_restore_file_saved_again_by_notepad_opens_with_the_same_table(saved):
    assert open_(saved(pack(TABLE))) == TABLE


@pytest.mark.parametrize("saved", SAVED_AGAIN)
def test_an_encrypted_restore_file_saved_again_by_notepad_still_opens_with_its_password(saved):
    data = saved(encrypted(TABLE, PASSWORD))
    assert open_(data, PASSWORD) == TABLE
    with pytest.raises(NeedsPassword):
        open_(data)
    with pytest.raises(WrongKey):
        open_(data, PASSWORD + "x")


@pytest.mark.parametrize("data", [
    codecs.BOM_UTF8, codecs.BOM_UTF8 + b"hello", codecs.BOM_UTF16_LE, "hello".encode("utf-16"), "{}".encode("utf-16"),
    codecs.BOM_UTF16_LE + b"{\x00}",  # an odd number of bytes
    codecs.BOM_UTF16_LE + b"\x00\xd8{\x00}\x00",  # half of a character
    codecs.BOM_UTF8 + json.dumps({"format": "something-else", "codes": {}}).encode(),
    json.dumps({"format": "deid-restore", "v": 2, "codes": ["[姓名1]"]}).encode("utf-16"),
    codecs.BOM_UTF8 + codecs.BOM_UTF8 + pack(TABLE),  # a mark twice: not what any editor writes
])
def test_what_is_not_a_restore_file_is_still_told_apart_whatever_its_encoding(data):
    for password in ("", PASSWORD):
        with pytest.raises(NotRestoreFile):
            open_(data, password)


def test_a_version_written_as_true_is_not_taken_for_version_1():
    data = json.dumps({**json.loads(encrypted(TABLE, PASSWORD)), "v": True}).encode()
    with pytest.raises(NotRestoreFile):
        open_(data, PASSWORD)


# ---- A4: the counts given for the coded file ------------------------------------------------------

def coded_file(src, tmp_path, settings, strings):
    ctx = build_context(src, tmp_path / "work", settings, llm=keyword_llm({}), ocr=FakeOcr(), faces=FakeFaces())
    ctx.load()
    for text, kind in strings:
        ctx.store.add_text(text, kind, "user")
    coded = codes.build(ctx.inv, ctx.store)
    dst = tmp_path / ("coded" + src.suffix)
    return coded, codefile.make(ctx, coded, dst), dst


def test_two_masked_chart_labels_are_two_places_without_a_code(tmp_path, settings):
    src = make_docx(tmp_path / "a.docx", ["圖表如下"], chart_labels=["王小明", "陳美玲"])
    _coded, out, _dst = coded_file(src, tmp_path, settings, [("王小明", "姓名"), ("陳美玲", "姓名")])
    assert out.ok and out.placed == 0 and out.left == 2
    assert out.notes == ["代號版檔案裡有 2 處是遮蔽、沒有寫成代號，這些地方還原不回來。需要還原時請使用代號版文字"]


def test_chart_labels_that_mask_alike_are_counted_one_by_one_and_a_readable_one_is_not(tmp_path, settings):
    # In the masked file 王明 and 陳美 are one label (■■), and every label after them moves up one place.
    src = make_docx(tmp_path / "a.docx", ["圖表如下"], chart_labels=["王明", "陳美", "李大同", "林志豪"])
    _coded, out, _dst = coded_file(src, tmp_path, settings, [("王明", "姓名"), ("陳美", "姓名"), ("林志豪", "姓名")])
    assert out.ok and out.left == 3


def test_a_text_box_is_one_place_though_the_file_holds_it_twice(tmp_path, settings):
    src = make_docx(tmp_path / "a.docx", ["一般內容"], textbox="方塊 王小明")
    _coded, out, dst = coded_file(src, tmp_path, settings, [("王小明", "姓名")])
    assert out.ok and out.placed == 1 and out.left == 0
    with zipfile.ZipFile(dst) as z:
        body = z.read("word/document.xml").decode("utf-8")
    assert body.count("方塊 [姓名1]") == 2 and "王小明" not in body  # the second copy is still written


def test_a_place_in_a_text_box_that_takes_no_code_is_counted_once(tmp_path, settings):
    src = make_docx(tmp_path / "a.docx", ["一般內容"], textbox="方塊 王小明＠經理")
    with zipfile.ZipFile(src) as z:
        parts = {i.filename: z.read(i) for i in z.infolist()}
    tab = '</w:t><w:tab/><w:t xml:space="preserve">'
    parts["word/document.xml"] = parts["word/document.xml"].decode("utf-8").replace("＠", tab).encode("utf-8")
    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)
    _coded, out, _dst = coded_file(src, tmp_path, settings, [("王小明\t經理", "其他")])  # runs over the tab
    assert out.ok and out.placed == 0 and out.left == 1
