"""The code version of a Word or Excel file in its own format: the masked file, with a code written over each
masked stretch. All names and numbers are made up."""
import zipfile

from deid import codefile, codes, replyfile
from deid.formats.putback import put_back
from deid.pipeline import build_context
from deid.restore import restore
from tests.builders import make_docx, make_pdf, make_xlsx, text_image
from tests.fakes import FakeFaces, FakeOcr, keyword_llm

NAME, PHONE = "王小明", "0912-345-678"


def context(src, tmp_path, settings, strings=((NAME, "姓名"), (PHONE, "電話"))):
    ctx = build_context(src, tmp_path / "work", settings, llm=keyword_llm({}), ocr=FakeOcr(), faces=FakeFaces())
    ctx.load()
    for text, kind in strings:
        ctx.store.add_text(text, kind, "user")
    return ctx


def made(ctx, tmp_path):
    coded = codes.build(ctx.inv, ctx.store)
    dst = tmp_path / ("coded" + ctx.src.suffix)
    return coded, codefile.make(ctx, coded, dst), dst


def read(path_or_bytes, name):
    data = path_or_bytes if isinstance(path_or_bytes, bytes) else path_or_bytes.read_bytes()
    return replyfile.read(data, name).text


def test_a_word_file_comes_out_as_a_word_file_with_codes_where_the_strings_were(tmp_path, settings):
    src = make_docx(tmp_path / "a.docx", [f"承辦人 {NAME}，電話 {PHONE}", ["王小", "明 先生表示會處理"], "一般內容"],
                    table=[["姓名", "電話"], [NAME, PHONE]], header=f"{NAME}的報告")
    ctx = context(src, tmp_path, settings)
    coded, out, dst = made(ctx, tmp_path)
    assert out.ok and out.placed == 6 and out.left == 0 and out.notes == []
    text = read(dst, "a.docx")
    assert NAME not in text and PHONE not in text
    assert "承辦人 [姓名1]，電話 [電話1]" in text and "一般內容" in text
    assert "[姓名1] 先生表示會處理" in text  # a string cut over two runs is one code, and the space after it stays
    assert "[姓名1] | [電話1]" in text and "[姓名1]的報告" in text
    assert restore(text, coded.table).text == read(src, "a.docx")


def test_the_coded_file_put_back_reads_as_the_original(tmp_path, settings):
    src = make_docx(tmp_path / "a.docx", [f"承辦人 {NAME}，電話 {PHONE}", ["王小", "明 先生表示會處理"]], footer=f"聯絡 {PHONE}")
    coded, out, dst = made(context(src, tmp_path, settings), tmp_path)
    assert out.ok
    assert read(put_back(dst.read_bytes(), ".docx", coded.table), "a.docx") == read(src, "a.docx")


def test_a_sheet_comes_out_as_a_sheet_whatever_kind_of_cell_held_the_string(tmp_path, settings):
    src = make_xlsx(tmp_path / "a.xlsx", [{"name": "名冊", "rows": [
        ["姓名", "電話", "備註"],
        [NAME, 912345678, ("inline", f"請洽 {NAME}")],  # a shared string, a number, an inline string
        [NAME, "0987654321", "一般內容"],                # the same shared string in a second cell
        ["陳美玲", ("date", 28916), ("formula", 'A2&"先生"', f"{NAME}先生")]]}])
    ctx = context(src, tmp_path, settings, [(NAME, "姓名"), ("912345678", "電話"), ("陳美玲", "姓名")])
    birthday = next(t for t in ctx.inv.texts if "陳美玲" in t.text).text.split(" | ")[1]
    ctx.store.add_text(birthday, "出生年月日", "user")
    coded, out, dst = made(ctx, tmp_path)
    assert out.ok and out.left == 0
    text = read(dst, "a.xlsx")
    assert NAME not in text and "912345678" not in text and "陳美玲" not in text and birthday not in text
    assert "[姓名1] | [電話1] | 請洽 [姓名1]" in text
    assert "[姓名1] | 0987654321 | 一般內容" in text
    assert "[姓名2] | [出生年月日1] | [姓名1]先生" in text
    assert read(put_back(dst.read_bytes(), ".xlsx", coded.table), "a.xlsx") == read(src, "a.xlsx")


def test_what_a_reader_does_not_see_stays_masked_and_no_string_is_left_anywhere(tmp_path, settings):
    src = make_docx(tmp_path / "a.docx", [f"承辦人 {NAME}"], creator=NAME, hyperlink=("連結", f"https://example.com/{NAME}"),
                    bookmark=NAME)
    ctx = context(src, tmp_path, settings)
    _coded, out, dst = made(ctx, tmp_path)
    assert out.ok
    assert ctx.adapter.residue(dst, [NAME]) == []
    with zipfile.ZipFile(dst) as z:
        assert all("[姓名1]" not in z.read(n).decode("utf-8", "ignore") for n in z.namelist() if n != "word/document.xml")


def test_the_second_copy_of_a_text_box_gets_the_same_code(tmp_path, settings):
    src = make_docx(tmp_path / "a.docx", [f"承辦人 {NAME}"], textbox=f"方塊 {NAME}")
    _coded, out, dst = made(context(src, tmp_path, settings), tmp_path)
    assert out.ok and out.left == 0
    with zipfile.ZipFile(dst) as z:
        body = z.read("word/document.xml").decode("utf-8")
    assert body.count("方塊 [姓名1]") == 2 and NAME not in body


def test_a_stretch_that_runs_over_two_cells_stays_masked_and_is_counted(tmp_path, settings):
    src = make_xlsx(tmp_path / "a.xlsx", [{"name": "名冊", "rows": [["姓名", "部門"], [NAME, "資訊室"], ["陳美玲", "總務"]]}])
    ctx = context(src, tmp_path, settings, [(f"{NAME} | 資訊室", "其他"), ("陳美玲", "姓名")])
    coded, out, dst = made(ctx, tmp_path)
    assert out.ok and out.placed == 1 and out.left == 2  # the two cells of the stretch
    assert out.notes == ["代號版檔案裡有 2 處是遮蔽、沒有寫成代號，這些地方還原不回來。需要還原時請使用代號版文字"]
    text = read(dst, "a.xlsx")
    assert NAME not in text and "資訊室" not in text and "[姓名1]" in text
    assert any(code in coded.text and code not in text for code in coded.table)  # the text has it, the file does not


def test_a_pdf_has_no_coded_file(tmp_path, settings):
    src = make_pdf(tmp_path / "a.pdf", [{"lines": [f"承辦人 {NAME}"]}])
    _coded, out, dst = made(context(src, tmp_path, settings), tmp_path)
    assert not out.ok and not dst.exists() and out.notes == [codefile.PDF_NOTE]


def test_no_file_is_given_when_the_masked_file_under_it_fails_its_checks(tmp_path, settings, monkeypatch):
    src = make_docx(tmp_path / "a.docx", [f"承辦人 {NAME}"])
    ctx = context(src, tmp_path, settings)
    monkeypatch.setattr(ctx.adapter, "residue", lambda dst, strings: ["某處: 「x」"])
    _coded, out, dst = made(ctx, tmp_path)
    assert not out.ok and not dst.exists()
    assert out.notes == ["代號版檔案沒有通過檢查，沒有提供。請使用代號版文字"]


def test_nothing_but_the_coded_file_is_left_behind(tmp_path, settings):
    src = make_docx(tmp_path / "a.docx", [f"承辦人 {NAME}"])
    _coded, out, dst = made(context(src, tmp_path, settings), tmp_path)
    assert out.ok
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.docx", "coded.docx"]  # the masked file under it is gone


def test_a_document_with_nothing_to_code_still_comes_out_in_its_own_format(tmp_path, settings):
    src = make_docx(tmp_path / "a.docx", ["一般內容"])
    _coded, out, dst = made(context(src, tmp_path, settings, []), tmp_path)
    assert out.ok and out.placed == 0 and read(dst, "a.docx") == "一般內容"


def test_comments_and_a_string_cut_over_two_runs_of_a_cell_are_coded_too(tmp_path, settings):
    src = make_xlsx(tmp_path / "a.xlsx", [{"name": "名冊", "rows": [["承辦人", ("rich",)], ["備註", "一般內容"]]}],
                    rich=("王小", "明 主任"), comment=("審核者", f"請 {NAME} 確認", "A1"))
    coded, out, dst = made(context(src, tmp_path, settings), tmp_path)
    assert out.ok and out.placed == 2 and out.left == 0
    text = read(dst, "a.xlsx")
    assert "承辦人 | [姓名1] 主任" in text and "請 [姓名1] 確認" in text and NAME not in text
    with zipfile.ZipFile(dst) as z:
        assert "<b/>" in z.read("xl/sharedStrings.xml").decode("utf-8")  # the second run and its formatting are still there
    assert read(put_back(dst.read_bytes(), ".xlsx", coded.table), "a.xlsx") == read(src, "a.xlsx")


def test_a_place_the_reviewer_left_readable_is_readable_in_the_file_as_in_the_text(tmp_path, settings):
    src = make_xlsx(tmp_path / "a.xlsx", [{"name": "名冊", "rows": [["承辦人", NAME], ["聯絡人", NAME]]}])
    ctx = context(src, tmp_path, settings, [(NAME, "姓名")])
    [finding] = ctx.store.findings
    ctx.store.set_on(finding.id, False, occurrence=1)
    coded, out, dst = made(ctx, tmp_path)
    assert out.ok and out.placed == 1 and out.left == 0
    assert read(dst, "a.xlsx") == "承辦人 | [姓名1]\n聯絡人 | 王小明"
    assert "承辦人 | [姓名1]\n聯絡人 | 王小明" in coded.text


def test_a_covered_picture_is_said_to_be_beyond_restoring(tmp_path, settings):
    src = make_docx(tmp_path / "a.docx", [f"承辦人 {NAME}"], images=[("image1.png", text_image(["x"], size=(400, 200)))])
    ctx = context(src, tmp_path, settings)
    ctx.store.add_region(ctx.inv.images[0].id, (10, 10, 100, 60), "其他", "user")
    _coded, out, _dst = made(ctx, tmp_path)
    assert out.ok and out.notes == [codefile.PICTURES]


def test_what_the_masking_covers_beyond_the_codes_is_counted_too(tmp_path, settings):
    """A date cell is masked whole, whatever part of it was to be masked: the rest of it has no code."""
    src = make_xlsx(tmp_path / "a.xlsx", [{"name": "名冊", "rows": [["生日", ("date", 28916)], ["備註", "一般內容"]]}])
    ctx = context(src, tmp_path, settings, [])
    year = ctx.inv.texts[0].text.split(" | ")[1][:4]
    ctx.store.add_text(year, "其他", "user")
    coded, out, dst = made(ctx, tmp_path)
    assert out.ok and out.placed == 1 and out.left == 1
    assert out.notes == ["代號版檔案裡有 1 處是遮蔽、沒有寫成代號，這些地方還原不回來。需要還原時請使用代號版文字"]
    text = read(dst, "a.xlsx")
    assert year not in text and "生日 | [個資1]" in text and "一般內容" in text
    assert f"生日 | [個資1]{ctx.inv.texts[0].text.split(' | ')[1][4:]}" in coded.text  # the text keeps what the file masks


def test_an_error_nobody_foresaw_costs_the_file_and_not_the_text(tmp_path, settings, monkeypatch, caplog):
    import logging
    src = make_docx(tmp_path / "a.docx", [f"承辦人 {NAME}"])
    ctx = context(src, tmp_path, settings)

    def broken(*args):
        raise RuntimeError(f"內容 {NAME}")

    monkeypatch.setattr(ctx.adapter, "write_codes", broken)
    with caplog.at_level(logging.DEBUG, logger="deid"):
        _coded, out, dst = made(ctx, tmp_path)
    assert not out.ok and out.notes == [codefile.BROKEN]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.docx"]
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in text and NAME not in text  # the kind of error is logged, never what it says
