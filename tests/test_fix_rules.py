"""Fixes from the review of 2026-11-23: phone numbers followed by more digits, customer numbers at the end of a
link, a backslash before a code, and the codes of text read from pictures. All names and numbers are made up."""
import io
import zipfile

import pytest

from deid import replyfile
from deid.agent import tools
from deid.codes import NOTICE, build
from deid.findings import FindingStore
from deid.formats.putback import put_back
from deid.model import ImageUnit, Inventory, TextUnit
from deid.patterns import candidates, is_safety_net
from deid.pipeline import analyze, build_context
from deid.restore import restore
from tests.builders import make_docx, text_image
from tests.fakes import FakeFaces, FakeOcr, keyword_llm, ocr_line


def net(text):
    return [(c.text, c.kind) for c in candidates(text) if is_safety_net(c, text)]


def phones(text):
    return [c.text for c in candidates(text) if c.kind in ("電話", "手機")]


def cards(text):
    return [(c.text, c.verified) for c in candidates(text) if c.kind == "信用卡號"]


# ---- R1: a phone number followed by a space and more digits ------------------------------------

@pytest.mark.parametrize("text, number, kind", [
    ("手機 0912-345-678 2026/03/12 起停用", "0912-345-678", "手機"),
    ("聯絡電話 0912 345 678 1234 號信箱", "0912 345 678", "手機"),
    ("總機 02-2345-6789 1234 5678", "02-2345-6789", "電話"),
    ("客服電話 02-2345-6789 2026/01/05 起改號", "02-2345-6789", "電話"),
    ("國際電話 +886-2-2345-6789 1234 5678", "+886-2-2345-6789", "電話"),
    ("手機 0912345678 1234 號", "0912345678", "手機"),
])
def test_a_phone_number_followed_by_a_space_and_more_digits_is_still_masked(text, number, kind):
    assert (number, kind) in net(text)
    assert cards(text) == []  # the run that takes in the date or the amount is not offered as a card number


@pytest.mark.parametrize("text, number", [
    ("手機 0912-345-678 2029/03/12 起停用", "0912-345-678"),  # 09123456782029 passes the card checksum by chance
    ("客服電話 02-2345-6789 2024/01/05 起改號", "02-2345-6789"),  # 02234567892024 too
])
def test_a_phone_number_is_kept_even_when_the_run_with_the_next_digits_passes_the_card_checksum(text, number):
    assert number in [t for t, _kind in net(text)]
    assert cards(text) == []


@pytest.mark.parametrize("text, number", [
    ("信用卡 4111 1111 1111 1111 到期", "4111 1111 1111 1111"),
    ("信用卡 4111-1111-1111-1111 到期", "4111-1111-1111-1111"),
    ("電話卡號：4111 1111 1111 1111", "4111 1111 1111 1111"),  # 電話 before eight digits: still the card, not a phone
    ("卡號 4111111111111111", "4111111111111111"),
])
def test_a_real_card_number_is_still_a_card_and_holds_no_phone_number(text, number):
    assert phones(text) == []
    assert cards(text) == [(number, True)] and (number, "信用卡號") in net(text)


@pytest.mark.parametrize("text", ["訂單 1234 5678 9012 3456", "序號 0912 3456 7890 1234", "帳號 0123 4567 8901 2345",
                                  "傳真機序號 1234 5678 9012 3456", "電話費帳號 4311 2222 3333 4444",
                                  "訂單編號 4311-2222-3333-4444", "帳號 0123-456-789012", "會議日期 2026/03/12 至 2026/03/15",
                                  "1999-2000 年度預算", "2024-2025 年度", "版本 10.0912.345.678", "金額 1 234 567 890 123"])
def test_long_numbers_dates_and_versions_still_hold_no_phone_number(text):
    assert phones(text) == []


def test_a_card_like_run_without_a_phone_number_is_still_offered_unverified():
    assert cards("訂單 1234 5678 9012 3456 已出貨") == [("1234 5678 9012 3456", False)]
    assert cards("序號 0912 3456 7890 1234") == [("0912 3456 7890 1234", False)]


# ---- R2: a customer or member number at the end of a link --------------------------------------

@pytest.mark.parametrize("text, number", [
    ("https://shop.example.com/q?cust=AB1234 請查詢", "AB1234"),
    ("請至https://shop.example.com/q?cust=AB1234查詢", "AB1234"),
    ("(https://shop.example.com/q?member_no=M12345)", "M12345"),
    ("see https://shop.example.com/q?member_no=M12345.", "M12345"),
    ("https://shop.example.com/q?member_no=M12345, thanks", "M12345"),
    ("https://shop.example.com/q?member_no=M12345。", "M12345"),
    ("[會員頁](https://shop.example.com/q?x=1&MemberNo=K8P2Q)", "K8P2Q"),
    ("點 https://shop.example.com/q?member_no=M12345！", "M12345"),
    ("網址 https://shop.example.com/q?CustomerNo=90412；請洽", "90412"),
])
def test_a_customer_number_at_the_end_of_a_link_is_found_whatever_follows_the_link(text, number):
    assert [c.text for c in candidates(text) if c.kind == "客戶編號"] == [number]
    assert (number, "客戶編號") in net(text)


@pytest.mark.parametrize("text", [
    "https://shop.example.com/q?cust=AB12.34 請查詢",  # a value with a dot in it is not cut short
    "https://shop.example.com/q?member_no=M12.345)",
    "https://shop.example.com/q?cust=AB1234.html",
    "https://shop.example.com/q?cust=AB1234;jsessionid=9",
    "https://shop.example.com/q?cust=AB1234%E6%9F%A5",
    "https://intra.example.org.tw/page?id=2031040077查詢",
    "https://intra.example.org.tw/page?ProjectNo=2031040077）",
    "https://intra.example.org.tw/page?customer_service=on.",
    "請見 https://www.example.com/download/v1.2.3/setup.exe。",
    "https://www.example.com/news/2026/03/12/story.html, 2024-2025 年度",
    "客戶編號 CustomerNo=90412 請洽櫃台",  # not in a link
])
def test_nothing_else_in_a_link_becomes_a_customer_number(text):
    assert [c.text for c in candidates(text) if c.kind == "客戶編號"] == []


MEMBER = "會員專區：請至https://shop.example.com/q?member_no=M12345查詢點數"
DOC_LINK = "會議紀錄請見 https://intra.example.org.tw/meeting/view.aspx?id=2031040077 。"


def context(doc, tmp_path, settings, table=None, ocr=None):
    return build_context(doc, tmp_path / "work", settings, llm=keyword_llm(table or {}), ocr=ocr or FakeOcr(), faces=FakeFaces())


def test_a_member_number_in_a_link_that_the_agent_reports_is_on_and_a_document_id_stays_a_suggestion(tmp_path, settings):
    ctx = context(make_docx(tmp_path / "a.docx", [MEMBER, DOC_LINK]), tmp_path, settings)
    ctx.load()
    items = [{"text": "M12345", "type": "其他識別碼", "reason": "會員編號"},
             {"text": "2031040077", "type": "其他識別碼", "reason": "連結中的文件 id"}]
    member, doc_id = tools.call(ctx, "add_findings", {"items": items})["results"]
    assert member["on"] is True and "note" not in member
    assert doc_id["on"] is False and "網址" in doc_id["note"]


def test_a_member_number_in_a_link_read_from_a_picture_is_masked_when_the_model_reports_it_first(tmp_path, settings):
    doc = make_docx(tmp_path / "m.docx", ["內文", DOC_LINK], images=[("image1.png", text_image(["x"], size=(400, 200)))])
    ctx = context(doc, tmp_path, settings, {"M12345": "其他識別碼", "2031040077": "其他識別碼"}, ocr=FakeOcr([ocr_line(MEMBER)]))
    analyze(ctx, use_agent=False)
    found = {f.text: f for f in ctx.store.findings if f.kind == "text"}
    assert found["M12345"].on is True and any(o.kind == "box" and o.on for o in found["M12345"].occurrences)
    assert found["2031040077"].on is False and "網址" in found["2031040077"].reason


def test_a_member_number_in_a_link_in_the_text_is_masked_when_the_model_reports_it(tmp_path, settings):
    ctx = context(make_docx(tmp_path / "a.docx", [MEMBER]), tmp_path, settings, {"M12345": "其他識別碼"})
    analyze(ctx, use_agent=False)
    found = {f.text: f for f in ctx.store.findings if f.kind == "text"}
    assert found["M12345"].on is True and found["M12345"].type == "其他識別碼"


# ---- R3: a backslash that stands before a code ---------------------------------------------------

PATHS = {"[帳號密碼1]": "wangxm", "[姓名1]": "王小明"}


def test_a_backslash_before_a_code_is_part_of_the_text_and_stays():
    r = restore("檔案位置 C:\\Users\\[帳號密碼1]\\Desktop\\報告.docx；共用 \\\\fs01\\[姓名1]\\專案", PATHS)
    assert r.text == "檔案位置 C:\\Users\\wangxm\\Desktop\\報告.docx；共用 \\\\fs01\\王小明\\專案"
    assert r.replaced == 2


def test_a_code_a_model_escaped_for_markdown_is_still_put_back_without_the_backslashes():
    r = restore("\\[姓名1\\] 與 [姓名1\\] 與 ［姓名１\\］ 與 \\[帳號密碼1\\]", PATHS)
    assert r.text == "王小明 與 王小明 與 王小明 與 wangxm" and r.replaced == 4


def test_a_path_with_a_code_survives_the_round_trip_through_the_code_version():
    for line, found in [("檔案位置 C:\\Users\\wangxm\\Desktop\\報告.docx", [("wangxm", "帳號密碼")]),
                        ("共用資料夾 \\\\fs01\\王小明\\專案", [("王小明", "姓名")]),
                        ("跳脫字元 \\[王小明\\] 測試", [("王小明", "姓名")])]:
        inv = Inventory(kind="docx", texts=[TextUnit("t1", line, "內文")])
        store = FindingStore(inv)
        for text, kind in found:
            store.add_text(text, kind, "model")
        coded = build(inv, store)
        body = coded.text[len(NOTICE):].strip("\n").split("\n")[1]
        assert found[0][0] not in body
        assert restore(body, coded.table).text == line


def test_a_backslash_before_a_code_stays_when_the_strings_are_put_back_into_a_word_file(tmp_path):
    src = make_docx(tmp_path / "a.docx", ["檔案位置 C:\\Users\\[帳號密碼1]\\Desktop", ["共用 \\\\fs01\\", "[姓名1]", "\\專案"],
                                          "跳脫 \\[姓名1\\] 完"]).read_bytes()
    out = put_back(src, ".docx", PATHS)
    assert replyfile.read(out, "a.docx").text.split("\n")[-3:] == [
        "檔案位置 C:\\Users\\wangxm\\Desktop", "共用 \\\\fs01\\王小明\\專案", "跳脫 王小明 完"]
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        assert "[姓名1]" not in z.read("word/document.xml").decode("utf-8")


# ---- R4: codes for text read from pictures -------------------------------------------------------

def picture(*lines):
    return ImageUnit("img1", "word/media/image1.png", 800, 100 * len(lines), True, areas=[(0, 0, 800, 100 * len(lines))],
                     ocr=[ocr_line(text, y=10 + 60 * n) for n, text in enumerate(lines)])


def coded_lines(coded):
    return coded.text[len(NOTICE):].strip("\n").split("\n")


def picture_part(coded):
    lines = coded_lines(coded)
    return lines[lines.index("【圖片 1 的文字】") + 1:]


@pytest.mark.parametrize("order", [("王小明", "王小華"), ("王小華", "王小明")])
def test_an_exact_match_in_a_picture_wins_over_a_near_match_whatever_the_order(order):
    inv = Inventory(kind="docx", texts=[TextUnit("t1", "出席：王小明、王小華", "內文")], images=[picture("承辦：王小華")])
    store = FindingStore(inv)
    for name in order:
        store.add_text(name, "姓名", "model")
    coded = build(inv, store)
    (line,) = picture_part(coded)
    assert line.startswith("承辦：[") and coded.table[line[3:]] == "王小華"
    assert restore("\n".join(coded_lines(coded)), coded.table).text.split("\n")[-1] == "承辦：王小華"


def test_a_near_match_in_a_picture_gets_a_code_of_its_own_for_what_the_picture_says():
    inv = Inventory(kind="docx", texts=[TextUnit("t1", "出席：王小明", "內文")], images=[picture("承辦：王小華")])
    store = FindingStore(inv)
    f = store.add_text("王小明", "姓名", "model")
    assert [o.fuzzy for o in f.occurrences if o.kind == "box"] == [True]  # the picture is still covered when masking
    coded = build(inv, store)
    assert coded_lines(coded)[1] == "出席：[姓名1]" and picture_part(coded) == ["承辦：[姓名2]"]
    assert coded.table == {"[姓名1]": "王小明", "[姓名2]": "王小華"}
    assert restore("\n".join(coded_lines(coded)), coded.table).text.split("\n")[-1] == "承辦：王小華"
    assert coded.residue == []


def test_a_near_match_does_not_join_the_group_of_the_name_it_resembles():
    inv = Inventory(kind="docx", texts=[TextUnit("t1", "王小明到場，小明簽名", "內文")], images=[picture("承辦：王小華")])
    store = FindingStore(inv)
    a = store.add_text("王小明", "姓名", "model")
    b = store.add_text("小明", "姓名", "model")
    store.link([a.id, b.id])
    coded = build(inv, store)
    (line,) = picture_part(coded)
    assert coded.table[line[3:]] == "王小華" and line[3:] not in ("[姓名1]", "[姓名1b]", "[姓名1c]")
    assert "[姓名1c]" not in coded.table


@pytest.mark.parametrize("order", [("小明", "王小明"), ("王小明", "小明"), ("小明先生", "王小明"), ("王小明", "小明先生")])
def test_overlapping_findings_in_a_picture_become_one_code_as_in_the_text(order):
    inv = Inventory(kind="docx", texts=[TextUnit("t1", "簽核：王小明先生", "內文")], images=[picture("簽核：王小明先生")])
    store = FindingStore(inv)
    for name in order:
        store.add_text(name, "姓名", "model")
    coded = build(inv, store)
    lines = coded_lines(coded)
    text_line, (picture_line,) = lines[1], picture_part(coded)
    assert picture_line == text_line  # the same stretch gets the same single code
    assert picture_line.count("[") == 1
    back = restore("\n".join(lines), coded.table).text.split("\n")
    assert back[1] == back[-1] == "簽核：王小明先生"


def test_a_name_written_twice_in_a_row_in_a_picture_comes_back_twice():
    inv = Inventory(kind="docx", texts=[TextUnit("t1", "王小明、陳美玲", "內文")], images=[picture("王小明王小明、王小明陳美玲")])
    store = FindingStore(inv)
    store.add_text("王小明", "姓名", "model")
    store.add_text("陳美玲", "姓名", "model")
    coded = build(inv, store)
    assert picture_part(coded) == ["[姓名1][姓名1]、[姓名1][姓名2]"]
    assert restore("\n".join(coded_lines(coded)), coded.table).text.split("\n")[-1] == "王小明王小明、王小明陳美玲"


def test_a_face_box_over_part_of_a_line_blanks_only_what_no_string_covers():
    image = ImageUnit("img1", "word/media/image1.png", 800, 100, True, areas=[(0, 0, 800, 100)], ocr=[ocr_line("簽核：王小明 照片")])
    inv = Inventory(kind="docx", texts=[TextUnit("t1", "王小明", "內文")], images=[image])
    store = FindingStore(inv)
    store.add_text("王小明", "姓名", "model")
    store.add_region("img1", (10 + 6 * 20, 0, 10 + 9 * 20, 100), "人臉", "user")  # over 照片 and the space before it
    store.add_region("img1", (10 + 2 * 20, 0, 10 + 5 * 20, 100), "人臉", "user")  # over the name as well
    coded = build(inv, store)
    assert picture_part(coded) == ["簽核[已遮蔽][姓名1][已遮蔽]"]
