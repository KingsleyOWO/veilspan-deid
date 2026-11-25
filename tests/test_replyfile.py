"""A file a cloud model handed back (Word, PDF, Excel) is read as text, so that the codes in it can be turned
back into the strings. All names and numbers are made up."""
import tempfile

import pytest

from deid import replyfile
from deid.formats.base import BrokenFile, UnsupportedFormat
from deid.restore import restore
from tests.builders import make_docx, make_pdf, make_xlsx, text_image

TABLE = {"[姓名1]": "王小明", "[電話1]": "0912-345-678"}


def test_a_word_reply_is_read_one_paragraph_a_line_without_a_title(tmp_path):
    data = make_docx(tmp_path / "a.docx", ["已通知 [姓名1]。", ["請回電 [電", "話1]。"]]).read_bytes()
    reply = replyfile.read(data, "回覆.DOCX")
    assert reply.text == "已通知 [姓名1]。\n請回電 [電話1]。"  # a code split over two runs is one code
    assert reply.notes == []
    assert restore(reply.text, TABLE).text == "已通知 王小明。\n請回電 0912-345-678。"


def test_sections_get_titles_when_there_is_more_than_one(tmp_path):
    data = make_pdf(tmp_path / "a.pdf", [{"lines": ["已通知 [姓名1]"]}, {"lines": ["請回電 [電話1]"]}]).read_bytes()
    assert replyfile.read(data, "a.pdf").text == "【第 1 頁】\n已通知 [姓名1]\n\n【第 2 頁】\n請回電 [電話1]"


def test_a_table_of_a_word_reply_and_its_header_are_read(tmp_path):
    data = make_docx(tmp_path / "a.docx", ["名單如下"], table=[["姓名", "電話"], ["[姓名1]", "[電話1]"]], header="[姓名1] 的案件").read_bytes()
    text = replyfile.read(data, "a.docx").text
    assert "【" in text and "名單如下" in text
    assert restore(text, TABLE).replaced == 3


def test_a_code_that_a_pdf_broke_across_two_lines_is_still_put_back(tmp_path):
    data = make_pdf(tmp_path / "a.pdf", [{"lines": ["本案已通知 [姓", "名1] 到場，電話 [電話", "1]"], "leading": 16}]).read_bytes()
    done = restore(replyfile.read(data, "a.pdf").text, TABLE)
    assert done.replaced == 2 and "王小明" in done.text and "0912-345-678" in done.text and done.unknown == []


def test_a_sheet_is_read_one_row_a_line(tmp_path):
    data = make_xlsx(tmp_path / "a.xlsx", [{"name": "名冊", "rows": [["姓名", "電話"], ["[姓名1]", "[電話1]"]]}]).read_bytes()
    text = replyfile.read(data, "a.xlsx").text
    assert "[姓名1] | [電話1]" in text


def test_pictures_are_counted_and_not_read(tmp_path):
    data = make_docx(tmp_path / "a.docx", ["已通知 [姓名1]。"], images=[("image1.png", text_image(["[電話1]"], size=(400, 200)))]).read_bytes()
    reply = replyfile.read(data, "a.docx")
    assert reply.text == "已通知 [姓名1]。"
    assert reply.notes == ["檔案裡有 1 張圖片，圖片裡的文字沒有讀取"]


def test_a_page_of_a_pdf_is_not_a_picture_but_a_scanned_page_is_said_to_be_unread(tmp_path):
    plain = make_pdf(tmp_path / "a.pdf", [{"lines": ["已通知 [姓名1]"]}]).read_bytes()
    assert replyfile.read(plain, "a.pdf").notes == []
    mixed = make_pdf(tmp_path / "b.pdf", [{"lines": ["已通知 [姓名1]"]}, {"image": text_image(["[電話1]"], size=(600, 800))}]).read_bytes()
    assert replyfile.read(mixed, "b.pdf").notes == ["有 1 頁含有圖片或是掃描頁，圖片裡的文字沒有讀取"]


def test_hidden_content_is_not_part_of_the_text(tmp_path):
    data = make_docx(tmp_path / "a.docx", ["已通知 [姓名1]。"], images=[("image1.png", text_image(["x"], size=(400, 200)))],
                     image_alt="[電話1] 的照片", creator="[姓名1]").read_bytes()
    assert "[電話1]" not in replyfile.read(data, "a.docx").text


def test_a_file_without_text_is_refused(tmp_path):
    scan = make_pdf(tmp_path / "a.pdf", [{"image": text_image(["[姓名1]"], size=(600, 800))}]).read_bytes()
    with pytest.raises(replyfile.NoText):
        replyfile.read(scan, "a.pdf")


def test_other_kinds_of_file_and_broken_files_are_refused():
    with pytest.raises(UnsupportedFormat):
        replyfile.read("hello [姓名1]".encode("utf-8"), "a.rtf")
    with pytest.raises(BrokenFile):
        replyfile.read(b"this is not a zip", "a.docx")
    with pytest.raises(BrokenFile):
        replyfile.read(b"\xd0\xcf\x11\xe0" + b"\0" * 600, "a.docx")  # an old .doc under a new name


def test_the_file_is_gone_from_disk_after_reading_even_when_reading_fails(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    replyfile.read(make_docx(tmp_path / "a.docx", ["已通知 [姓名1]。"]).read_bytes(), "a.docx")
    assert list(scratch.iterdir()) == []
    with pytest.raises(BrokenFile):
        replyfile.read(b"this is not a zip", "a.docx")
    assert list(scratch.iterdir()) == []
