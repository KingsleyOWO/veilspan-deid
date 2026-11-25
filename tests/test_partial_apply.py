"""Partial masking through the three writers and their checks. All names and numbers are made up."""
import re

import pymupdf
import pytest

from deid.findings import FindingStore
from deid.formats.docx import DocxAdapter
from deid.formats.pdf import PdfAdapter
from deid.formats.xlsx import XlsxAdapter
from deid.formats.xmltok import mask_string, replace_in_text_nodes
from deid.model import MaskPlan
from tests.builders import make_docx, make_pdf, make_xlsx

NAME = {"王小明": [(1, 2)]}


def partial_plan(adapter, path, items):
    """items: (text, type) pairs, each added everywhere and switched to the partial style."""
    store = FindingStore(adapter.parse(path))
    for text, kind in items:
        f = store.add_text(text, kind, "user")
        assert f is not None, text
        store.set_style(f.id, "partial")
    return store.plan()


# ---- the shared string masker -------------------------------------------------------------------

def test_a_matched_string_is_masked_whole_unless_it_is_listed_as_partial():
    assert mask_string("王小明", "■", "*") == "■■■"
    assert mask_string("王小明", "■", "*", NAME) == "王■明"
    assert mask_string("陳美玲", "■", "*", NAME) == "■■■"


def test_text_nodes_and_attributes_follow_the_partial_ranges():
    xml = '<a t="王小明的圖"><t>王小明與陳美玲</t></a>'
    out = replace_in_text_nodes(xml, ["王小明", "陳美玲"], None, {"t"}, "■", "*", partial=NAME)
    assert out == '<a t="王■明的圖"><t>王■明與■■■</t></a>'


def test_the_generic_search_of_each_format_follows_the_partial_ranges():
    assert DocxAdapter()._generic("word/other.xml", "<w:t>王小明</w:t>", ["王小明"], "■", "*", NAME) == "<w:t>王■明</w:t>"
    sheet = '<worksheet><sheetData><row><c><f>COUNTIF(A:A,"王小明")</f></c></row></sheetData></worksheet>'
    assert '"王■明"' in XlsxAdapter()._generic("xl/worksheets/sheet1.xml", sheet, ["王小明"], "■", "*", NAME)


# ---- Word ---------------------------------------------------------------------------------------

def test_word_text_headers_properties_and_hidden_places_all_show_the_same_partial_form(tmp_path, settings):
    src = make_docx(tmp_path / "a.docx", ["承辦人王小明已簽收", ["聯絡 王小", "明 0912-345-678"]], header="王小明的報告",
                    creator="王小明", bookmark="王小明的備忘", custom_xml_text="擁有者 王小明")
    ad = DocxAdapter()
    plan = partial_plan(ad, src, [("王小明", "姓名"), ("0912-345-678", "電話")])
    dst = tmp_path / "out.docx"
    ad.apply(src, dst, plan, settings)
    after = ad.parse(dst)
    texts = [t.text for t in after.texts]
    assert "承辦人王■明已簽收" in texts and "聯絡 王■明 0912-***-678" in texts and "王■明的報告" in texts
    assert "王■明的備忘" in texts and "擁有者 王■明" in texts
    assert [m.value for m in after.metas if m.label == "作者"] == ["王■明"]
    assert not any("王小明" in t or "■■■" in t for t in texts)
    assert ad.verify(src, dst, plan, settings) == [] and ad.residue(dst, plan.strings) == []


def test_word_check_reports_a_character_that_should_be_masked_and_is_not(tmp_path, settings):
    src = make_docx(tmp_path / "a.docx", ["承辦人王小明已簽收"])
    ad = DocxAdapter()
    plan = partial_plan(ad, src, [("王小明", "姓名")])
    dst = tmp_path / "out.docx"
    ad.apply(src, dst, MaskPlan(), settings)
    assert any("遮蔽未生效" in p for p in ad.verify(src, dst, plan, settings))


def test_word_check_reports_kept_characters_that_no_longer_match_the_original(tmp_path, settings):
    src = make_docx(tmp_path / "a.docx", ["甲乙王小明丙丁"])
    ad = DocxAdapter()
    plan = partial_plan(ad, src, [("王小明", "姓名")])
    wrong = make_docx(tmp_path / "wrong.docx", ["甲戊王■明丙丁"])  # masked where planned, but another character changed
    assert any("與原檔不一致" in p for p in ad.verify(src, wrong, plan, settings))
    more = make_docx(tmp_path / "more.docx", ["甲乙■■■丙丁"])  # masking more than planned is not an error
    assert ad.verify(src, more, plan, settings) == []


# ---- Excel --------------------------------------------------------------------------------------

@pytest.fixture
def book(tmp_path):
    rows = [["姓名", "電話", "生日", "備註"],
            ["王小明", "0912-345-678", ("date", 32996), ("formula", 'A2&"先生"', "王小明先生")],
            ["王小明", ("inline", "陳美玲"), None, "聯絡王小明"],
            ["林志豪", "王小明", None, None]]
    return make_xlsx(tmp_path / "名單.xlsx", [{"name": "名單", "rows": rows}, {"name": "王小明的資料", "rows": [["備用", "無"]]}],
                     creator="王小明", comment=("審閱者甲", "請王小明確認", "A2"), header="承辦人 王小明",
                     validation="王小明,陳美玲")


def test_excel_text_cells_shared_strings_and_names_show_the_partial_form(book, tmp_path, settings):
    ad = XlsxAdapter()
    plan = partial_plan(ad, book, [("王小明", "姓名"), ("0912-345-678", "電話")])
    dst = tmp_path / "out.xlsx"
    ad.apply(book, dst, plan, settings)
    after = ad.parse(dst)
    joined = "\n".join(t.text for t in after.texts)
    assert "王■明" in joined and "0912-***-678" in joined and "聯絡王■明" in joined and "請王■明確認" in joined
    assert "王■明先生" in joined and "承辦人 王■明" in joined
    assert "王小明" not in joined and "■■■" not in joined and "陳美玲" in joined and "林志豪" in joined
    assert ad.verify(book, dst, plan, settings) == [] and ad.residue(dst, plan.strings) == []


def test_excel_date_cells_are_still_masked_whole(book, tmp_path, settings):
    ad = XlsxAdapter()
    row = next(t for t in ad.parse(book).texts if "0912-345-678" in t.text)
    date = re.search(r"\d{4}/\d{1,2}/\d{1,2}", row.text).group(0)
    plan = partial_plan(ad, book, [(date, "出生年月日")])
    assert plan.text[row.id] != [(row.text.index(date), row.text.index(date) + len(date))]  # only month and day are planned
    dst = tmp_path / "out.xlsx"
    ad.apply(book, dst, plan, settings)
    after = next(t for t in ad.parse(dst).texts if "0912-345-678" in t.text)
    assert date[:4] not in after.text and "*" * len(date) in after.text
    assert ad.verify(book, dst, plan, settings) == []


# ---- PDF ----------------------------------------------------------------------------------------

def test_pdf_page_text_loses_only_the_masked_characters(tmp_path, settings):
    src = make_pdf(tmp_path / "a.pdf", [{"lines": ["承辦人 王小明 到場", "電話 0912-345-678 已確認"]}])
    ad = PdfAdapter()
    plan = partial_plan(ad, src, [("王小明", "姓名"), ("0912-345-678", "電話")])
    dst = tmp_path / "out.pdf"
    ad.apply(src, dst, plan, settings)
    with pymupdf.open(dst) as doc:
        text = re.sub(r"\s+", "", doc[0].get_text())
    assert "承辦人王明到場" in text and "電話0912--678已確認" in text
    assert ad.verify(src, dst, plan, settings) == [] and ad.residue(dst, plan.strings) == []


def test_pdf_bookmarks_form_fields_and_properties_show_the_partial_form(tmp_path, settings):
    src = make_pdf(tmp_path / "f.pdf", [{"lines": ["申請表"], "widget": ("applicant", "王小明")}], author="王小明", title="王小明的申請")
    doc = pymupdf.open(src)
    doc.set_toc([[1, "王小明的章節", 1]])
    doc.saveIncr()
    doc.close()
    ad = PdfAdapter()
    plan = partial_plan(ad, src, [("王小明", "姓名")])
    dst = tmp_path / "out.pdf"
    ad.apply(src, dst, plan, settings)
    with pymupdf.open(dst) as out:
        assert out.get_toc()[0][1] == "王■明的章節"
        assert [w.field_value for w in out[0].widgets()] == ["王■明"]
        assert out.metadata["author"] == "王■明" and out.metadata["title"] == "王■明的申請"
    assert ad.verify(src, dst, plan, settings) == [] and ad.residue(dst, plan.strings) == []


def test_pdf_strings_found_only_by_search_follow_the_partial_ranges(tmp_path, settings):
    """A plan that names the string but no place (a copy the parser did not turn into a unit)."""
    src = make_pdf(tmp_path / "f.pdf", [{"lines": ["申請表"], "widget": ("applicant", "王小明")}], title="王小明的申請")
    doc = pymupdf.open(src)
    doc.set_toc([[1, "王小明的章節", 1]])
    doc.saveIncr()
    doc.close()
    ad = PdfAdapter()
    dst = tmp_path / "out.pdf"
    ad.apply(src, dst, MaskPlan(strings=["王小明"], partial=NAME), settings)
    with pymupdf.open(dst) as out:
        assert out.get_toc()[0][1] == "王■明的章節"
        assert [w.field_value for w in out[0].widgets()] == ["王■明"]
        assert out.metadata["title"] == "王■明的申請"


def test_pdf_form_field_shown_on_the_page_passes_the_check_when_masked_whole(tmp_path, settings):
    """The field's value is also page text. Masking both redraws the field with mask characters inside the
    redacted box, which the check used to report as text left behind."""
    src = make_pdf(tmp_path / "f.pdf", [{"lines": ["申請表"], "widget": ("applicant", "王小明")}])
    ad = PdfAdapter()
    store = FindingStore(ad.parse(src))
    store.add_text("王小明", "姓名", "user")
    plan = store.plan()
    assert len(plan.text) == 2  # the page text and the field
    dst = tmp_path / "out.pdf"
    ad.apply(src, dst, plan, settings)
    assert ad.verify(src, dst, plan, settings) == [] and ad.residue(dst, plan.strings) == []
    ad.apply(src, dst, MaskPlan(), settings)  # nothing masked: the same check must still catch it
    assert any("仍有文字" in p for p in ad.verify(src, dst, plan, settings))


# ---- the whole flow -----------------------------------------------------------------------------

def test_a_string_left_readable_inside_a_partially_masked_one_is_a_note_not_residue(tmp_path, settings):
    from deid.pipeline import analyze, apply_and_verify, build_context
    from tests.fakes import FakeFaces, FakeOcr, keyword_llm
    doc = make_docx(tmp_path / "p.docx", ["信箱 lin.demo@example.com"])
    ctx = build_context(doc, tmp_path / "work", settings, llm=keyword_llm({}), ocr=FakeOcr(), faces=FakeFaces())
    analyze(ctx, use_agent=False)
    box = next(f for f in ctx.store.findings if f.text == "lin.demo@example.com")
    ctx.store.add_text("example.com", box.type, "user")  # same type, inside the mailbox: the mailbox decides
    ctx.store.set_style(box.id, "partial")
    result = apply_and_verify(ctx)
    assert [t.text for t in DocxAdapter().parse(ctx.workdir / "output.docx").texts] == ["信箱 l******o@example.com"]
    assert result.ok and result.residue == []
    assert any("example.com" in n for n in result.notes)


# ---- review fix: a string readable inside a longer partial one is still masked where it stands alone ----

def test_a_string_kept_inside_a_partial_mailbox_is_masked_in_parts_only_the_search_reaches(tmp_path, settings):
    import zipfile
    from deid.pipeline import analyze, apply_and_verify, build_context
    from tests.fakes import FakeFaces, FakeOcr, keyword_llm
    doc = make_docx(tmp_path / "p.docx", ["信箱 liming@wang-family.example", "個人網站 wang-family.example"])
    glossary = ('<?xml version="1.0" encoding="UTF-8"?><w:glossaryDocument xmlns:w="http://schemas.openxmlformats.org/'
                'wordprocessingml/2006/main"><w:docParts><w:docPart><w:docPartBody><w:p><w:r><w:t>備用 wang-family.example'
                '</w:t></w:r><w:r><w:t>；liming@wang-family.example</w:t></w:r></w:p></w:docPartBody></w:docPart></w:docParts>'
                '</w:glossaryDocument>')
    with zipfile.ZipFile(doc, "a") as z:  # a part the parser does not turn into units: only the string search edits it
        z.writestr("word/glossary/document.xml", glossary)
    ctx = build_context(doc, tmp_path / "work", settings, llm=keyword_llm({}), ocr=FakeOcr(), faces=FakeFaces())
    analyze(ctx, use_agent=False)
    box = next(f for f in ctx.store.findings if f.text == "liming@wang-family.example")
    ctx.store.add_text("wang-family.example", box.type, "user")
    ctx.store.set_style(box.id, "partial")
    result = apply_and_verify(ctx)
    out = ctx.workdir / "output.docx"
    assert [t.text for t in DocxAdapter().parse(out).texts][:2] == ["信箱 l****g@wang-family.example", "個人網站 *******************"]
    with zipfile.ZipFile(out) as z:
        part = z.read("word/glossary/document.xml").decode("utf-8")
    assert "備用 *******************<" in part and "；l****g@wang-family.example<" in part
    assert result.ok and result.residue == []
    kept = [n for n in result.notes if "wang-family.example" in n]
    assert kept and all(n.startswith("遮一半保留：") for n in kept)  # said as what it is, not as the reviewer's choice


# ---- 2026-11-19: inside a mailbox the mailbox decides ---------------------------------------------

def test_an_excel_mailbox_keeps_its_ends_when_the_account_before_the_at_is_listed_too(tmp_path, settings):
    """What happened on a real contact list: the model lists the account before the @ as an identifier of its
    own, and that entry masked the first character the mailbox was asked to keep."""
    ad = XlsxAdapter()
    book = make_xlsx(tmp_path / "通訊錄.xlsx", [{"name": "通訊錄", "rows": [["姓名", "信箱", "帳號"],
                                                                        ["王小明", "alice0609@example.com", "alice0609"]]}])
    store = FindingStore(ad.parse(book))
    box = store.add_text("alice0609@example.com", "電子郵件", "rule")
    assert store.add_text("alice0609", "其他識別碼", "agent") is not None
    store.set_style(box.id, "partial")
    plan = store.plan()
    dst = tmp_path / "out.xlsx"
    ad.apply(book, dst, plan, settings)
    joined = "\n".join(t.text for t in ad.parse(dst).texts)
    assert "a*******9@example.com" in joined  # the account by itself, in the next cell, is masked whole
    assert "alice0609" not in joined and joined.count("*********") == 1
    assert ad.verify(book, dst, plan, settings) == [] and ad.residue(dst, plan.search()) == []


# ---- 2026-11-19: custom masking, the reviewer picks the characters ---------------------------------

CODE = "王小明的專案代號AB12"
CODE_SHOWN = "王■■的專案代號A***"


def custom_plan(adapter, path, items, metas=()):
    """items: (text, ranges) text findings; metas: (value, ranges) file properties. All masked as picked."""
    store = FindingStore(adapter.parse(path))
    for text, spans in items:
        f = store.add_text(text, "其他", "user")
        assert f is not None, text
        store.set_spans(f.id, spans)
    for value, spans in metas:
        unit = next(m for m in store.inv.metas if m.value == value)
        store.set_spans(store.add_meta(unit.id, on=True).id, spans)
    return store.plan()


def test_word_shows_the_picked_characters_in_text_hidden_places_and_a_property(tmp_path, settings):
    src = make_docx(tmp_path / "c.docx", [f"備註 {CODE} 已結案", ["另見 王小明的專案", "代號AB12"]], header=f"{CODE} 報告",
                    company="大明科技股份有限公司", custom_xml_text=f"來源 {CODE}")
    ad = DocxAdapter()
    plan = custom_plan(ad, src, [(CODE, [(1, 3), (9, 12)])], [("大明科技股份有限公司", [(0, 2)])])
    dst = tmp_path / "out.docx"
    ad.apply(src, dst, plan, settings)
    after = ad.parse(dst)
    texts = [t.text for t in after.texts]
    assert f"備註 {CODE_SHOWN} 已結案" in texts and f"另見 {CODE_SHOWN}" in texts and f"{CODE_SHOWN} 報告" in texts
    assert f"來源 {CODE_SHOWN}" in texts
    assert [m.value for m in after.metas if "科技" in m.value] == ["■■科技股份有限公司"]
    assert ad.verify(src, dst, plan, settings) == [] and ad.residue(dst, plan.strings) == []


def test_excel_shows_the_picked_characters(tmp_path, settings):
    ad = XlsxAdapter()
    book = make_xlsx(tmp_path / "c.xlsx", [{"name": "清單", "rows": [["項目", "說明"], ["一", CODE], ["二", f"參考 {CODE}"]]}])
    plan = custom_plan(ad, book, [(CODE, [(1, 3), (9, 12)])])
    dst = tmp_path / "out.xlsx"
    ad.apply(book, dst, plan, settings)
    joined = "\n".join(t.text for t in ad.parse(dst).texts)
    assert joined.count(CODE_SHOWN) == 2 and f"參考 {CODE_SHOWN}" in joined and CODE not in joined
    assert ad.verify(book, dst, plan, settings) == [] and ad.residue(dst, plan.strings) == []


def test_pdf_shows_the_picked_characters_on_the_page_and_in_a_property(tmp_path, settings):
    src = make_pdf(tmp_path / "c.pdf", [{"lines": ["備註 PROJECT-AB12 已結案"]}], title="大明科技股份有限公司")
    ad = PdfAdapter()
    plan = custom_plan(ad, src, [("PROJECT-AB12", [(9, 12)])], [("大明科技股份有限公司", [(0, 2)])])
    dst = tmp_path / "out.pdf"
    ad.apply(src, dst, plan, settings)
    with pymupdf.open(dst) as doc:
        text = re.sub(r"\s+", "", doc[0].get_text())
        assert doc.metadata["title"] == "■■科技股份有限公司"
    assert "備註PROJECT-A已結案" in text and "B12" not in text
    assert ad.verify(src, dst, plan, settings) == [] and ad.residue(dst, plan.strings) == []
