"""The strings are put back into the file itself (Word, Excel): only the codes change, the rest of the file
stays as it was. All names and numbers are made up."""
import io
import zipfile

from deid import replyfile
from deid.formats.putback import put_back
from tests.builders import make_docx, make_xlsx

TABLE = {"[姓名1]": "王小明", "[電話1]": "0912-345-678", "[個資1]": "A&B <公司>"}


def entry(data: bytes, name: str) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read(name).decode("utf-8")


def text_of(data: bytes, name: str) -> str:
    return replyfile.read(data, name).text


def test_codes_in_a_word_file_become_the_strings_and_nothing_else_changes(tmp_path):
    src = make_docx(tmp_path / "a.docx", ["已通知 [姓名1]。", "這一段沒有代號，[保留] 原樣。"], table=[["姓名", "電話"], ["【姓名1】", "[ 電話 １ ]"]],
                    header="[姓名1] 的案件", footnote="見 [電話1]", comment=("審閱者", "[姓名1] 已確認")).read_bytes()
    out = put_back(src, ".docx", TABLE)
    assert text_of(out, "a.docx") == text_of(src, "a.docx").replace("[姓名1]", "王小明").replace("【姓名1】", "王小明") \
        .replace("[電話1]", "0912-345-678").replace("[ 電話 １ ]", "0912-345-678")
    assert "王小明" in text_of(out, "a.docx") and "[保留]" in text_of(out, "a.docx")
    with zipfile.ZipFile(io.BytesIO(src)) as a, zipfile.ZipFile(io.BytesIO(out)) as b:
        assert a.namelist() == b.namelist()
        changed = [n for n in a.namelist() if a.read(n) != b.read(n)]
    assert sorted(changed) == ["word/comments.xml", "word/document.xml", "word/footnotes.xml", "word/header1.xml"]
    before, after = entry(src, "word/document.xml"), entry(out, "word/document.xml")
    assert after.count("<w:tbl>") == before.count("<w:tbl>") == 1 and after.count("<w:p>") == before.count("<w:p>")
    assert "這一段沒有代號，[保留] 原樣。" in after


def test_a_code_split_over_runs_goes_into_the_first_run_and_spaces_around_it_survive(tmp_path):
    src = make_docx(tmp_path / "a.docx", [["請 [姓", "名", "1] 回電，或找 [電話", "1]"]]).read_bytes()
    out = put_back(src, ".docx", TABLE)
    assert text_of(out, "a.docx") == "請 王小明 回電，或找 0912-345-678"
    xml = entry(out, "word/document.xml")
    assert xml.count("<w:r>") == entry(src, "word/document.xml").count("<w:r>")  # no run is added or removed
    assert '<w:t xml:space="preserve"> 回電，或找 0912-345-678</w:t>' in xml  # the space that now leads a run is kept


def test_a_string_with_markup_characters_is_escaped(tmp_path):
    src = make_docx(tmp_path / "a.docx", ["來自 [個資1] 的通知"]).read_bytes()
    out = put_back(src, ".docx", TABLE)
    assert "來自 A&amp;B &lt;公司&gt; 的通知" in entry(out, "word/document.xml")
    assert text_of(out, "a.docx") == "來自 A&B <公司> 的通知"


def test_codes_the_table_does_not_have_stay(tmp_path):
    src = make_docx(tmp_path / "a.docx", ["請 [姓名9] 回電 [電話1]"]).read_bytes()
    assert text_of(put_back(src, ".docx", TABLE), "a.docx") == "請 [姓名9] 回電 0912-345-678"


def test_a_file_without_codes_comes_back_byte_for_byte(tmp_path):
    src = make_docx(tmp_path / "a.docx", ["沒有代號"]).read_bytes()
    out = put_back(src, ".docx", TABLE)
    with zipfile.ZipFile(io.BytesIO(src)) as a, zipfile.ZipFile(io.BytesIO(out)) as b:
        assert [(n, a.read(n)) for n in a.namelist()] == [(n, b.read(n)) for n in b.namelist()]


def test_codes_in_a_sheet_become_the_strings(tmp_path):
    rows = [["姓名", "電話", "次數"], ["[姓名1]", "【電話1】", 3], [("inline", "轉告 [姓名1]"), ("rich",), ("date", 45000)]]
    src = make_xlsx(tmp_path / "a.xlsx", [{"name": "名冊", "rows": rows}], rich=("請 [姓", "名1] 回電"),
                    comment=("審閱者", "[電話1] 已確認", "A1")).read_bytes()
    out = put_back(src, ".xlsx", TABLE)
    text = text_of(out, "a.xlsx")
    assert "王小明 | 0912-345-678 | 3" in text and "轉告 王小明 | 請 王小明 回電" in text and "0912-345-678 已確認" in text
    assert "[姓名1]" not in text and "電話1" not in text
    with zipfile.ZipFile(io.BytesIO(src)) as a, zipfile.ZipFile(io.BytesIO(out)) as b:
        assert a.namelist() == b.namelist()
        changed = sorted(n for n in a.namelist() if a.read(n) != b.read(n))
    assert changed == ["xl/comments1.xml", "xl/sharedStrings.xml", "xl/worksheets/sheet1.xml"]
    sheet = entry(out, "xl/worksheets/sheet1.xml")
    assert "<v>3</v>" in sheet and "<v>45000</v>" in sheet  # numbers and dates are not touched


def test_the_restored_file_comes_with_the_text_and_is_checked_against_it(tmp_path):
    src = make_docx(tmp_path / "a.docx", ["已通知 [姓名1]，請回電 [電話1]，副本 [姓名9]。"]).read_bytes()
    done = replyfile.restore_file(src, "回覆.docx", TABLE)
    assert done.text == "已通知 王小明，請回電 0912-345-678，副本 [姓名9]。"
    assert (done.replaced, done.unknown, done.unused, done.notes) == (2, ["[姓名9]"], ["[個資1]"], [])
    assert done.ext == ".docx" and text_of(done.file, "a.docx") == done.text


def test_a_pdf_gets_the_text_only_and_says_so(tmp_path):
    from tests.builders import make_pdf
    src = make_pdf(tmp_path / "a.pdf", [{"lines": ["已通知 [姓名1]"]}]).read_bytes()
    done = replyfile.restore_file(src, "a.pdf", TABLE)
    assert done.text == "已通知 王小明" and done.file is None and done.ext == ".pdf"
    assert done.notes == ["PDF 只能還原成文字：PDF 裡文字的位置是固定的，換回長度不同的原文會跑版"]


def test_a_code_the_writer_cannot_reach_is_reported_and_the_file_is_still_given(tmp_path):
    rows = [["[姓名1]", ("formula", 'A1&"先生"', "[姓名1]先生")]]
    src = make_xlsx(tmp_path / "a.xlsx", [{"name": "名冊", "rows": rows}]).read_bytes()
    done = replyfile.restore_file(src, "a.xlsx", TABLE)
    assert "王小明 | 王小明先生" in done.text  # the text has it
    assert done.file is not None and "[姓名1]先生" in text_of(done.file, "a.xlsx")
    assert done.notes == ["還原後的檔案裡還有 1 處代號沒有換回（例如圖表的標籤、公式算出來的文字）。這些地方請對照下面的文字"]


def test_no_restored_file_is_left_on_disk(tmp_path, monkeypatch):
    import tempfile
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    replyfile.restore_file(make_docx(tmp_path / "a.docx", ["已通知 [姓名1]。"]).read_bytes(), "a.docx", TABLE)
    assert list(scratch.iterdir()) == []
    (scratch / "deid-reply-left-behind").mkdir()
    (scratch / "deid-reply-left-behind" / "restored.docx").write_bytes(b"x")
    (scratch / "something-else").mkdir()
    replyfile.sweep()
    assert [p.name for p in scratch.iterdir()] == ["something-else"]
