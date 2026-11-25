"""A number that a number format shows differently is read the way it is shown (review of 2026-11-23): a phone
stored as 912345678 with the format 0000000000 is 0912345678 on the screen, and that is what has to be found
and masked. All names and numbers are made up."""
import zipfile
from xml.sax.saxutils import quoteattr

import pytest

from deid import codefile, codes, patterns
from deid.findings import FindingStore
from deid.formats import numfmt
from deid.formats.base import adapter_for
from deid.pipeline import build_context
from deid.restore import restore
from tests.builders import make_xlsx
from tests.fakes import FakeFaces, FakeOcr, keyword_llm


@pytest.mark.parametrize("value, code, shown", [
    ("912345678", "0000000000", "0912345678"),
    ("912345678", "0000-000-000", "0912-345-678"),
    ("912345678", r"0000\-000\-000", "0912-345-678"),
    ("223456789", '"A"000000000', "A223456789"),
    ("223456789", '"身分證 "0', "身分證 223456789"),
    ("9.12345678E8", "0000000000", "0912345678"),
    ("912345678.0", "0000000000", "0912345678"),
    ("912345678", "[$-404]0000000000", "0912345678"),
    ("912345678", "[Red]0000000000", "0912345678"),
    ("106", "000", "106"),
    ("12345", "000", "12345"),               # more digits than places: none is dropped
    ("12345678", "000-0000", "1234-5678"),
    ("27654321", "[<=99999999]####-####;(0#) ####-####", "2765-4321"),
    ("227654321", "[<=99999999]####-####;(0#) ####-####", "(02) 2765-4321"),
    ("912345678", "[>99999999]0000-000-000;000-0000", "0912-345-678"),
    ("7654321", "[>99999999]0000-000-000;000-0000", "765-4321"),
    ("-5", "000", "-005"),
    ("5", "000;(000)", "005"),
    ("-5", "000;(000)", "(005)"),
    ("0", "000;(000);\"零\"0", "零0"),
    ("7", "?00", " 07"),
])
def test_a_whole_number_is_shown_as_its_format_writes_it(value, code, shown):
    assert numfmt.shown(value, code) == shown


@pytest.mark.parametrize("value, code", [
    ("1234567", "#,##0"),        # an amount keeps the spelling it is stored in
    ("1234.5", "0.00"),
    ("0.25", "0%"),
    ("1234567", "0.00E+00"),
    ("3", "# ?/?"),
    ("3.5", "0000"),             # not a whole number
    ("912345678", "General"),
    ("912345678", "@"),
    ("912345678", '"保密"'),      # no digit place: the format hides the number, and the cell still holds it
    ("912345678", ";;;"),
    ("912345678", "[DBNum1]0"),  # written in other numerals
    ("912345678", "0_);(0)"),
    ("45000", "yyyy/m/d"),
    ("abc", "0000"),
    ("912345678", ""),
])
def test_a_format_this_does_not_know_leaves_the_stored_spelling(value, code):
    assert numfmt.shown(value, code) is None


def with_format(path, code, refs):
    """Give cells of the first sheet a custom number format."""
    with zipfile.ZipFile(path) as z:
        parts = {i.filename: z.read(i).decode("utf-8") for i in z.infolist()}
    styles, sheet = parts["xl/styles.xml"], parts["xl/worksheets/sheet1.xml"]
    xfs = '<cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="14" applyNumberFormat="1"/></cellXfs>'
    assert "<fonts" in styles and xfs in styles
    styles = styles.replace("<fonts", f'<numFmts count="1"><numFmt numFmtId="164" formatCode={quoteattr(code)}/></numFmts><fonts', 1)
    parts["xl/styles.xml"] = styles.replace(xfs, xfs.replace('count="2"', 'count="3"').replace(
        "</cellXfs>", '<xf numFmtId="164" applyNumberFormat="1"/></cellXfs>'))
    for ref in refs:
        assert f'<c r="{ref}">' in sheet
        sheet = sheet.replace(f'<c r="{ref}">', f'<c r="{ref}" s="2">')
    parts["xl/worksheets/sheet1.xml"] = sheet
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in parts.items():
            z.writestr(name, text)
    return path


def phone_book(tmp_path, code="0000000000"):
    src = make_xlsx(tmp_path / "a.xlsx", [{"name": "名單", "rows": [["姓名", "電話"], ["王小明", "0912345678"],
                                                                 ["陳美玲", 912345678], ["林志豪", ("numformula", "B3+0", 912345678)]]}])
    return with_format(src, code, ["B3", "B4"])


def everything(path) -> str:
    with zipfile.ZipFile(path) as z:
        return "\n".join(z.read(n).decode("utf-8", "replace") for n in z.namelist())


def test_a_phone_stored_as_a_number_is_read_with_its_leading_zero(tmp_path, settings):
    src = phone_book(tmp_path)
    inv = adapter_for(src, settings).parse(src)
    rows = {t.text.split(" | ")[0]: t.text for t in inv.texts if " | " in t.text}
    assert rows["陳美玲"] == "陳美玲 | 0912345678" and rows["林志豪"] == "林志豪 | 0912345678"
    assert "0912345678" in [c.text for c in patterns.candidates(rows["陳美玲"])]


def test_masking_the_number_as_shown_masks_the_number_cells(tmp_path, settings):
    src = phone_book(tmp_path)
    ad = adapter_for(src, settings)
    store = FindingStore(ad.parse(src), lambda u, s, e: ad.locate(src, u, s, e))
    assert len(store.add_text("0912345678", "電話", "user").occurrences) == 3
    plan, dst = store.plan(), tmp_path / "out.xlsx"
    ad.apply(src, dst, plan, settings)
    assert ad.residue(dst, plan.strings) == [] and ad.verify(src, dst, plan, settings) == []
    assert "912345678" not in everything(dst)
    left = [t.text for t in ad.parse(dst).texts if " | " in t.text]
    assert left[0] == "姓名 | 電話" and all("9" not in row for row in left[1:])


def test_a_dashed_format_is_found_by_the_dashed_spelling(tmp_path, settings):
    src = phone_book(tmp_path, "0000-000-000")
    ad = adapter_for(src, settings)
    store = FindingStore(ad.parse(src), lambda u, s, e: ad.locate(src, u, s, e))
    assert len(store.add_text("0912-345-678", "電話", "user").occurrences) == 2
    plan, dst = store.plan(), tmp_path / "out.xlsx"
    ad.apply(src, dst, plan, settings)
    assert ad.residue(dst, plan.strings) == [] and ad.verify(src, dst, plan, settings) == []
    # the text cell of the first row is spelled without dashes: another string, not asked for, still there
    assert "<v>912345678</v>" not in everything(dst) and everything(dst).count("912345678") == 1


def test_the_coded_workbook_holds_a_code_and_restores_to_the_number_as_shown(tmp_path, settings):
    src = phone_book(tmp_path)
    ctx = build_context(src, tmp_path / "work", settings, llm=keyword_llm({}), ocr=FakeOcr(), faces=FakeFaces())
    ctx.load()
    ctx.store.add_text("0912345678", "電話", "user")
    coded = codes.build(ctx.inv, ctx.store)
    dst = tmp_path / "coded.xlsx"
    out = codefile.make(ctx, coded, dst)
    assert out.ok and out.left == 0 and coded.residue == []
    assert "912345678" not in coded.text and "912345678" not in everything(dst)
    assert coded.text.count("[電話1]") == 3
    assert "陳美玲 | 0912345678" in restore(coded.text, coded.table).text


# ---- text written into a number format ----------------------------------------------------------------------

@pytest.mark.parametrize("code, texts", [
    ('"王小明"0', ["王小明"]),
    ('0"號"', ["號"]),
    ('"A"0"B"', ["A", "B"]),
    ('0 "元整"', ["元整"]),
    ("yyyy年m月d日", ["年", "月", "日"]),      # wide characters written without quotes
    ("承辦人王小明0", ["承辦人王小明"]),
    ("[$NT$-404]#,##0", []),
    (r"\A000", []),
    ('#,##0.00_);[Red](#,##0.00)', []),
    ('"沒有收尾0', ["沒有收尾0"]),
])
def test_the_text_a_number_format_writes_out_is_told_from_its_codes(code, texts):
    assert [code[a:b] for a, b in numfmt.literals(code)] == texts


def format_code(path) -> str:
    """The workbook's one custom number format, as a program reading the file gets it."""
    import re
    from deid.formats.xmltok import attr_values
    with zipfile.ZipFile(path) as z:
        tag = re.search(r"<numFmt\b[^>]*>", z.read("xl/styles.xml").decode("utf-8")).group(0)
    return next(value for name, value, _s, _e in attr_values(tag) if name == "formatCode")


def test_a_name_written_into_a_number_format_is_shown_masked_and_checked(tmp_path, settings):
    src = make_xlsx(tmp_path / "a.xlsx", [{"name": "名單", "rows": [["件數", 5], ["比例", 3.5]]}])
    with_format(src, '"承辦人王小明："0.0', ["B1", "B2"])
    ad = adapter_for(src, settings)
    inv = ad.parse(src)
    assert ("隱藏內容：數字格式", "承辦人王小明：") in [(t.section, t.text) for t in inv.texts]
    store = FindingStore(inv, lambda u, s, e: ad.locate(src, u, s, e))
    assert store.add_text("王小明", "姓名", "user") is not None
    plan, dst = store.plan(), tmp_path / "out.xlsx"
    ad.apply(src, dst, plan, settings)
    assert format_code(dst) == '"承辦人■■■："0.0'
    assert ad.residue(dst, plan.strings) == [] and ad.verify(src, dst, plan, settings) == []


def test_a_name_in_a_number_format_is_masked_with_the_cell_that_shows_it(tmp_path, settings):
    src = make_xlsx(tmp_path / "a.xlsx", [{"name": "名單", "rows": [["編號", 7]]}])
    with_format(src, '"王小明"000', ["B1"])
    ad = adapter_for(src, settings)
    store = FindingStore(ad.parse(src), lambda u, s, e: ad.locate(src, u, s, e))
    found = store.add_text("王小明", "姓名", "user")
    assert len(found.occurrences) == 2  # the cell as it is shown (王小明007), and the format
    plan, dst = store.plan(), tmp_path / "out.xlsx"
    ad.apply(src, dst, plan, settings)
    assert "王小明" not in everything(dst) and format_code(dst) == '"■■■"000'
    assert ad.residue(dst, plan.strings) == [] and ad.verify(src, dst, plan, settings) == []


def test_another_copy_of_a_masked_string_in_a_number_format_is_found_by_the_search(tmp_path, settings):
    src = make_xlsx(tmp_path / "a.xlsx", [{"name": "名單", "rows": [["編號", 3.5]]}])
    with_format(src, '"王小明"0.0', ["B1"])
    ad = adapter_for(src, settings)
    xml = ad._generic("xl/styles.xml", '<styleSheet><numFmts><numFmt numFmtId="164" formatCode="&quot;王小明&quot;0.0"/></numFmts></styleSheet>',
                      ["王小明", "0.0"], "■", "*")
    assert 'formatCode="&quot;■■■&quot;0.0"' in xml  # the name in the literal; never the code beside it
    assert ad._residue_hits("xl/styles.xml", xml, ["王小明", "0.0"]) == set()
