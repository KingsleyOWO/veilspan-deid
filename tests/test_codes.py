"""The code version of a document: personal data replaced by codes, and the table that maps them back.
All names and numbers are made up."""
import pytest

from deid.codes import NOTICE, NOTICE_SAME, build
from deid.findings import FindingStore
from deid.formats.docx import DocxAdapter
from deid.formats.pdf import PdfAdapter
from deid.formats.xlsx import XlsxAdapter
from deid.model import ImageUnit, Inventory, MetaUnit, TextUnit
from deid.restore import restore
from tests.builders import make_docx, make_pdf, make_xlsx
from tests.fakes import ocr_line


def inventory(**kw):
    return Inventory(kind="docx", texts=[
        TextUnit("t1", "承辦人 王小明，電話 0912-345-678", "內文"),
        TextUnit("t2", "王小明表示小明會處理，請洽陳美玲", "內文"),
        TextUnit("t3", "王小明的報告", "頁首"),
        TextUnit("t4", "王小明的截圖", "隱藏內容：圖片說明"),
        TextUnit("t5", "方塊 王小明", "內文", mirror=True)],
        metas=[MetaUnit("m1", "作者", "王小明", True)], **kw)


def body(coded):
    assert coded.text.startswith(NOTICE + "\n")
    return coded.text[len(NOTICE) + 1:].strip("\n").split("\n")


def test_each_string_gets_one_code_everywhere_and_the_table_maps_it_back():
    store = FindingStore(inventory())
    store.add_text("王小明", "姓名", "model")
    store.add_text("陳美玲", "姓名", "model")
    store.add_text("0912-345-678", "電話", "rule")
    coded = build(store.inv, store)
    assert body(coded) == ["【內文】", "承辦人 [姓名1]，電話 [電話1]", "[姓名1]表示小明會處理，請洽[姓名2]", "", "【頁首】", "[姓名1]的報告"]
    assert coded.table == {"[姓名1]": "王小明", "[電話1]": "0912-345-678", "[姓名2]": "陳美玲"}
    assert coded.residue == [] and coded.blanked == 0


def test_hidden_content_properties_and_mirror_copies_are_not_part_of_the_text():
    store = FindingStore(inventory())
    coded = build(store.inv, store)
    assert "截圖" not in coded.text and "方塊" not in coded.text and "作者" not in coded.text
    assert coded.table == {}


def test_the_notice_asks_for_codes_to_be_kept_as_they_are():
    assert "[姓名1]" in NOTICE and "原樣保留" in NOTICE and "\n" not in NOTICE


def test_overlapping_findings_become_one_code_for_the_whole_stretch():
    inv = Inventory(kind="docx", texts=[TextUnit("t1", "信箱 lin.demo@example.com 與 王小明先生", "內文")])
    store = FindingStore(inv)
    store.add_text("lin.demo@example.com", "電子郵件", "rule")
    store.add_text("lin.demo", "其他", "agent")
    store.add_text("王小明", "姓名", "model")
    store.add_text("小明先生", "其他", "user")  # overlaps the name without containing it
    coded = build(inv, store)
    assert body(coded) == ["【內文】", "信箱 [電子郵件1] 與 [姓名1]"]
    assert coded.table == {"[電子郵件1]": "lin.demo@example.com", "[姓名1]": "王小明先生"}


def test_partial_style_is_ignored_codes_always_stand_for_the_whole_string():
    store = FindingStore(inventory())
    f = store.add_text("王小明", "姓名", "model")
    store.set_style(f.id, "partial")
    coded = build(store.inv, store)
    assert "[姓名1]的報告" in coded.text and "王" not in coded.text
    assert f.style == "partial"  # the stored choice is left alone


def test_a_place_switched_off_stays_readable_and_is_reported_as_left_by_choice():
    store = FindingStore(inventory())
    f = store.add_text("王小明", "姓名", "model")
    store.set_on(f.id, False, occurrence=0)
    coded = build(store.inv, store)
    assert "承辦人 王小明，電話" in coded.text and "[姓名1]表示" in coded.text
    assert coded.residue == [] and any("王小明" in n for n in coded.notes)


def test_a_code_never_collides_with_text_the_document_already_has():
    inv = Inventory(kind="docx", texts=[TextUnit("t1", "範例代號 [姓名1] 與 [姓名 2]，承辦人 王小明、陳美玲", "內文")])
    store = FindingStore(inv)
    store.add_text("王小明", "姓名", "model")
    store.add_text("陳美玲", "姓名", "model")
    coded = build(inv, store)
    assert coded.table == {"[姓名3]": "王小明", "[姓名4]": "陳美玲"}


def test_english_export_preserves_existing_codes_and_document_text():
    inv = Inventory(kind="xlsx", texts=[
        TextUnit("t1", "Reference [NAME1] | Alex Morgan | alex@example.com", "工作表：聯絡人"),
    ])
    store = FindingStore(inv)
    store.add_text("Alex Morgan", "姓名", "user")
    store.add_text("alex@example.com", "電子郵件", "user")
    coded = build(inv, store, lang="en")
    assert "[Worksheet: 聯絡人]\nReference [NAME1] | [NAME2] | [EMAIL1]" in coded.text
    result = restore("Please ask [NAME2] to email [EMAIL1]. Reference [NAME1].", coded.table)
    assert result.text == "Please ask Alex Morgan to email alex@example.com. Reference [NAME1]."
    assert result.unknown == ["[NAME1]"]


def test_types_without_a_handy_name_get_a_readable_label():
    inv = Inventory(kind="docx", texts=[TextUnit("t1", "員編 EMP-00123，備註 特殊需求", "內文")])
    store = FindingStore(inv)
    store.add_text("EMP-00123", "其他識別碼", "model")
    store.add_text("特殊需求", "其他", "user")
    assert build(inv, store).table == {"[識別碼1]": "EMP-00123", "[個資1]": "特殊需求"}


def test_text_read_from_pictures_is_included_and_coded_by_what_covers_it():
    image = ImageUnit("img1", "word/media/image1.png", 800, 300, True, areas=[(0, 0, 800, 300)],
                      ocr=[ocr_line("登入者：王小明"), ocr_line("使用者 王小朋 已登出", y=60), ocr_line("部門 資訊室", y=110),
                           ocr_line("照片旁的字", y=200)])
    store = FindingStore(inventory(images=[image]))
    store.add_text("王小明", "姓名", "model")
    store.add_region("img1", (0, 190, 800, 240), "人臉", "user")  # covers the last line: nothing to restore it from
    coded = build(store.inv, store)
    lines = body(coded)
    at = lines.index("【圖片 1 的文字】")
    # the near-match (王小朋) is coded as what the picture says there: it may be another person
    assert lines[at + 1:] == ["登入者：[姓名1]", "使用者 [姓名2] 已登出", "部門 資訊室", "[已遮蔽]"]
    assert coded.blanked == 1 and coded.table == {"[姓名1]": "王小明", "[姓名2]": "王小朋"}
    english = build(store.inv, store, lang="en")
    assert "[Image 1 text]" in english.text and "[REDACTED]" in english.text and english.blanked == 1


def test_an_active_string_still_readable_in_the_text_is_residue():
    """A name the picture reader split over two boxes is not matched there, so it is not coded: it must be reported."""
    line = ocr_line("經辦 王小明")
    image = ImageUnit("img1", "word/media/image1.png", 800, 300, True, ocr=[line])
    inv = Inventory(kind="docx", texts=[TextUnit("t1", "承辦人 王小明", "內文")], images=[image])
    store = FindingStore(inv)
    f = store.add_text("王小明", "姓名", "model")
    f.occurrences = [o for o in f.occurrences if o.kind != "box"]  # as if the match in the picture had been missed
    coded = build(inv, store)
    assert any("王小明" in r for r in coded.residue)


def test_a_masked_word_that_is_part_of_a_code_label_is_not_mistaken_for_residue():
    inv = Inventory(kind="docx", texts=[TextUnit("t1", "欄位 姓名：王小明", "內文")])
    store = FindingStore(inv)
    store.add_text("王小明", "姓名", "model")
    store.add_text("姓名", "其他", "user")
    coded = build(inv, store)
    assert body(coded) == ["【內文】", "欄位 [個資1]：[姓名1]"] and coded.residue == []


@pytest.mark.parametrize("make", ["docx", "xlsx", "pdf"])
def test_each_format_gives_text_in_reading_order_with_its_sections(make, tmp_path):
    if make == "docx":
        src = make_docx(tmp_path / "a.docx", ["承辦人 王小明", "一般內容"], header="王小明的報告", creator="王小明")
        ad, expected = DocxAdapter(), ["【內文】", "承辦人 [姓名1]", "一般內容", "", "【頁首】", "[姓名1]的報告"]
    elif make == "xlsx":
        src = make_xlsx(tmp_path / "a.xlsx", [{"name": "名單", "rows": [["姓名", "部門"], ["王小明", "資訊室"]]},
                                             {"name": "備用", "rows": [["王小明", 3]], "state": "hidden"}])
        ad, expected = XlsxAdapter(), ["【工作表：名單】", "姓名 | 部門", "[姓名1] | 資訊室", "", "【工作表：備用（隱藏）】", "[姓名1] | 3"]
    else:
        src = make_pdf(tmp_path / "a.pdf", [{"lines": ["承辦人 王小明"]}, {"lines": ["第二頁 王小明"], "annot": "請王小明確認"}])
        ad, expected = PdfAdapter(), ["【第 1 頁】", "承辦人 [姓名1]", "", "【第 2 頁】", "第二頁 [姓名1]", "", "【註解】", "請[姓名1]確認"]
    inv = ad.parse(src)
    store = FindingStore(inv)
    store.add_text("王小明", "姓名", "model")
    coded = build(inv, store)
    assert body(coded) == expected
    assert coded.table == {"[姓名1]": "王小明"} and coded.residue == []


def test_a_masked_sheet_name_is_coded_in_the_title_of_its_section(tmp_path):
    src = make_xlsx(tmp_path / "a.xlsx", [{"name": "王小明的資料", "rows": [["備註", "無"]]}, {"name": "名單", "rows": [["王小明", "資訊室"]]}])
    ad = XlsxAdapter()
    inv = ad.parse(src)
    store = FindingStore(inv)
    store.add_text("王小明", "姓名", "model")
    coded = build(inv, store)
    assert body(coded) == ["【工作表：[姓名1]的資料】", "備註 | 無", "", "【工作表：名單】", "[姓名1] | 資訊室"]
    assert coded.residue == []


# ---- review fix: content a reader cannot see is not part of the text ---------------------------

def test_embedded_files_defined_names_and_pivot_caches_are_left_out(tmp_path):
    doc = make_docx(tmp_path / "a.docx", ["承辦人 王小明"], embedded_xlsx_strings=["內嵌的陳美玲", "內嵌資料"], chart_labels=["第一季"])
    inv = DocxAdapter().parse(doc)
    assert any(t.section.startswith("內嵌檔案") for t in inv.texts)
    text = build(inv, FindingStore(inv)).text
    assert "內嵌" not in text and "第一季" in text  # a chart's labels are on the page; the workbook behind it is not
    book = make_xlsx(tmp_path / "a.xlsx", [{"name": "名單", "rows": [["王小明", "資訊室"]]}],
                     defined_name=("名單範圍", "'名單'!$A$1:$B$1"), pivot_items=["樞紐裡的王小明"])
    inv = XlsxAdapter().parse(book)
    assert {"已定義名稱", "樞紐分析快取"} <= {t.section for t in inv.texts}
    text = build(inv, FindingStore(inv)).text
    assert "名單範圍" not in text and "樞紐" not in text and "王小明 | 資訊室" in text


# ---- names of one person share a number ---------------------------------------------------

def grouped(text, *names, extra=(), images=()):
    """A one-paragraph document whose `names` are name findings linked as one person."""
    inv = Inventory(kind="docx", texts=[TextUnit("t1", text, "內文")], images=list(images))
    store = FindingStore(inv)
    found = [store.add_text(n, "姓名", "model") for n in names]
    for other, kind in extra:
        store.add_text(other, kind, "model")
    store.link([f.id for f in found])
    return inv, store


def lines_after_notices(coded):
    assert coded.text.startswith(NOTICE + "\n" + NOTICE_SAME + "\n\n")
    return coded.text[len(NOTICE) + len(NOTICE_SAME) + 3:].strip("\n").split("\n")


def test_names_of_one_person_share_a_number_and_each_code_maps_to_its_own_string():
    store = FindingStore(inventory())
    a, b = store.add_text("王小明", "姓名", "model"), store.add_text("小明", "姓名", "model")
    store.add_text("陳美玲", "姓名", "model")
    store.link([a.id, b.id])
    coded = build(store.inv, store)
    assert "[姓名1]表示[姓名1b]會處理，請洽[姓名2]" in coded.text
    assert coded.table == {"[姓名1]": "王小明", "[姓名1b]": "小明", "[姓名2]": "陳美玲"}
    assert coded.text.split("\n")[1] == NOTICE_SAME and coded.residue == []
    back = restore("\n".join(lines_after_notices(coded)), coded.table)  # the notices quote example codes
    assert "王小明表示小明會處理，請洽陳美玲" in back.text and back.unknown == [] and back.unused == []


def test_without_groups_the_text_is_exactly_what_it_was():
    store = FindingStore(inventory())
    store.add_text("王小明", "姓名", "model")
    store.add_text("小明", "姓名", "model")
    coded = build(store.inv, store)
    assert NOTICE_SAME not in coded.text and coded.text.startswith(NOTICE + "\n\n【內文】")
    assert coded.table == {"[姓名1]": "王小明", "[姓名2]": "小明"}


def test_the_longest_writing_has_no_letter_even_when_it_comes_later():
    inv, store = grouped("小明到場。王小明簽名。", "王小明", "小明")
    coded = build(inv, store)
    assert lines_after_notices(coded) == ["【內文】", "[姓名1b]到場。[姓名1]簽名。"]
    assert coded.table == {"[姓名1]": "王小明", "[姓名1b]": "小明"}


def test_overlapping_names_of_one_group_get_a_letter_of_that_group():
    inv, store = grouped("王小明先生到場。王小明簽名。", "王小明", "小明先生")
    coded = build(inv, store)
    assert lines_after_notices(coded) == ["【內文】", "[姓名1]到場。[姓名1b]簽名。"]
    assert coded.table == {"[姓名1]": "王小明先生", "[姓名1b]": "王小明"}


def test_a_name_glued_to_another_kind_keeps_its_own_number():
    inv, store = grouped("王小明0912-345-678，王小明與小明", "王小明", "小明", extra=[("0912-345-678", "電話")])
    coded = build(inv, store)
    assert lines_after_notices(coded) == ["【內文】", "[電話1]，[姓名1]與[姓名1b]"]
    assert coded.table == {"[電話1]": "王小明0912-345-678", "[姓名1]": "王小明", "[姓名1b]": "小明"}


def test_text_that_already_reads_like_a_lettered_code_moves_the_number():
    inv, store = grouped("範例 [姓名1b] 與 ［姓名２Ｃ］，王小明與小明", "王小明", "小明")
    assert build(inv, store).table == {"[姓名3]": "王小明", "[姓名3b]": "小明"}


def test_a_group_of_more_than_26_writings_overflows_to_new_numbers():
    names = [f"林{a}{b}" for a in "甲乙丙丁戊己庚" for b in "子丑寅卯"]
    assert len(names) == 28
    inv, store = grouped("、".join(names), *names)
    coded = build(inv, store)
    assert len(coded.table) == 28 and set(coded.table.values()) == set(names)
    shared = [code for code in coded.table if code.startswith("[姓名1") and not code[4].isdigit()]
    assert len(shared) == 26 and coded.table["[姓名1]"] == names[0] and coded.table["[姓名1z]"] == names[25]
    assert {coded.table["[姓名2]"], coded.table["[姓名3]"]} == set(names[26:])
    assert restore(coded.text, coded.table).text.endswith("、".join(names) + "\n")


def test_a_grouped_name_read_from_a_picture_uses_its_group_code():
    image = ImageUnit("img1", "word/media/image1.png", 800, 100, True, areas=[(0, 0, 800, 100)], ocr=[ocr_line("登入者：小明")])
    inv, store = grouped("王小明到場", "王小明", "小明", images=[image])
    coded = build(inv, store)
    assert lines_after_notices(coded) == ["【內文】", "[姓名1]到場", "", "【圖片 1 的文字】", "登入者：[姓名1b]"]
