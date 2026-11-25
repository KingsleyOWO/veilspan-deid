"""Spreadsheet fixes after the review of 2026-11-23: headers and footers read with their format codes kept apart
from the words, references that name several sheets, formulas kept in extension lists, cell style names and
custom sort lists. Every workbook here is hand-built and every name and number in it is made up."""
import io
import re
import zipfile
from xml.parsers import expat
from xml.sax.saxutils import escape

import pytest

from deid import codefile, codes
from deid.findings import FindingStore
from deid.formats import xlsx as xlsx_module
from deid.formats.xlsx import XlsxAdapter
from deid.formats.xmltok import mask_chars
from deid.pipeline import build_context
from deid.restore import restore
from tests.fakes import FakeFaces, FakeOcr, keyword_llm

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
X14 = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main"
XM = "http://schemas.microsoft.com/office/excel/2006/main"
HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
NAME, ID, MAIL, PHONE = "John Smith", "A123456789", "wang.xm@example.com", "0912-345-678"


def sheet(rows: str, extra: str = "", ns: str = "") -> str:
    return HEAD + f'<worksheet xmlns="{MAIN}" xmlns:r="{REL}"{ns}><sheetData>{rows}</sheetData>{extra}</worksheet>'


def sst(*strings: str) -> str:
    return HEAD + f'<sst xmlns="{MAIN}">' + "".join(f"<si><t>{escape(x)}</t></si>" for x in strings) + "</sst>"


def cell(ref: str, index: int) -> str:
    return f'<c r="{ref}" t="s"><v>{index}</v></c>'


def rels(*items) -> str:
    return (HEAD + f'<Relationships xmlns="{PKG}">'
            + "".join(f'<Relationship Id="{i}" Type="{REL}/{t}" Target="{target}"/>' for i, t, target in items) + "</Relationships>")


def pack(path, sheets: dict, shared: str | None = None, parts: dict | None = None, wb_extra: str = ""):
    """sheets: name -> worksheet XML, in tab order. parts: extra or replacement zip entries."""
    names = list(sheets)
    files = {
        "[Content_Types].xml": HEAD + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/><Default Extension="rels" ContentType="application/vnd.openxmlformats-'
        'package.relationships+xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.sheet.main+xml"/></Types>',
        "_rels/.rels": rels(("rId1", "officeDocument", "xl/workbook.xml")),
        "xl/workbook.xml": HEAD + f'<workbook xmlns="{MAIN}" xmlns:r="{REL}"><sheets>'
        + "".join(f'<sheet name="{escape(n, {chr(39): "&apos;"})}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, n in enumerate(names))
        + f"</sheets>{wb_extra}</workbook>",
        "xl/_rels/workbook.xml.rels": rels(*[(f"rId{i + 1}", "worksheet", f"worksheets/sheet{i + 1}.xml") for i in range(len(names))],
                                           ("rIdS", "sharedStrings", "sharedStrings.xml"), ("rIdY", "styles", "styles.xml")),
    }
    for i, n in enumerate(names):
        files[f"xl/worksheets/sheet{i + 1}.xml"] = sheets[n]
    if shared is not None:
        files["xl/sharedStrings.xml"] = shared
    files.update(parts or {})
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            if data is not None:
                z.writestr(name, data)
    return path


def run(src, tmp_path, settings, words):
    """The mask function on its own: every place each word is found. Returns (inventory, store, output,
    residue, verify)."""
    ad = XlsxAdapter()
    inv = ad.parse(src)
    store = FindingStore(inv)
    for w in words:
        store.add_text(w, "姓名", "user")
    plan = store.plan()
    dst = tmp_path / "out.xlsx"
    ad.apply(src, dst, plan, settings)
    return inv, store, dst, ad.residue(dst, list(words)), ad.verify(src, dst, plan, settings)


def files(path) -> dict:
    with zipfile.ZipFile(path) as z:
        return {i.filename: z.read(i).decode("utf-8", "replace") for i in z.infolist()}


def wellformed(path) -> bool:
    for name, xml in files(path).items():
        if name.endswith((".xml", ".rels")):
            expat.ParserCreate().Parse(xml.encode("utf-8"), True)
    return True


def leaks(path, words) -> list:
    """Every entry of the package, in the spellings a word can take in XML."""
    out = []
    for name, xml in files(path).items():
        for w in words:
            if w in xml or escape(w) in xml or escape(w, {"'": "&apos;", '"': "&quot;"}) in xml:
                out.append((name, w))
    return out


def headers(xml: str) -> dict:
    return {m.group(1): m.group(2) for m in re.finditer(r"<(\w+(?:Header|Footer))>([^<]*)</\1>", xml)}


def units(inv, prefix):
    return [t for t in inv.texts if t.section.startswith(prefix)]


# ---- X1: headers and footers ---------------------------------------------------------------------

ELEMENTS = ["oddHeader", "oddFooter", "evenHeader", "evenFooter", "firstHeader", "firstFooter"]
# right after a section code, a font, a font size, a colour, a style switch
PREFIXES = ["&L", "&C", "&R", '&C&"Arial,Bold"', "&R&14", "&L&B", "&C&KFF0000", "&R&U&I", '&L&"微軟正黑體,粗體"&9']
CASES = [(p, w, ELEMENTS[(i + j) % len(ELEMENTS)]) for i, p in enumerate(PREFIXES) for j, w in enumerate([NAME, ID, MAIL, PHONE])]


def header_book(path, element: str, header: str, *, cell_text: str = "一般"):
    flag = ' differentOddEven="1"' if element.startswith("even") else ' differentFirst="1"' if element.startswith("first") else ""
    extra = f"<headerFooter{flag}><{element}>{escape(header)}</{element}></headerFooter>"
    return pack(path, {"名單": sheet(f'<row r="1">{cell("A1", 0)}</row>', extra)}, sst(cell_text))


def glue(prefix: str, word: str) -> str:
    # Excel writes a space between a font size and text that starts with a digit; otherwise the digits would
    # be read as part of the size (see test_digits_run_into_a_font_size_are_reported_not_passed)
    return prefix + (" " if prefix[-1].isdigit() and word[0].isdigit() else "") + word


@pytest.mark.parametrize("prefix,word,element", CASES, ids=[f"{e}-{p}-{w}" for p, w, e in CASES])
def test_a_string_right_after_a_format_code_is_found_and_masked_and_the_codes_stay(tmp_path, settings, prefix, word, element):
    tail = "&B 第&P頁&R&D"
    header = glue(prefix, word) + tail
    src = header_book(tmp_path / "h.xlsx", element, header)
    inv, store, dst, residue, problems = run(src, tmp_path, settings, [word])
    [unit] = units(inv, "頁首頁尾")
    assert word in unit.text and "&" not in unit.text  # the reviewer and the model see the words, not the codes
    assert [o.unit for o in store.findings[0].occurrences] == [unit.id]
    out = headers(files(dst)["xl/worksheets/sheet1.xml"])
    assert out[element] == escape(glue(prefix, word)[:-len(word)] + mask_chars(word, settings.mask_full, settings.mask_half) + tail)
    assert leaks(dst, [word]) == [] and (residue, problems) == ([], []) and wellformed(dst)


def test_what_a_header_shows_keeps_its_sections_apart_and_drops_the_codes(tmp_path):
    header = '&L&"Arial,Bold"機密&C第 &P 頁，共 &N 頁&R承辦人：王&B小明 && 同仁'
    src = header_book(tmp_path / "h.xlsx", "oddFooter", header)
    [unit] = units(XlsxAdapter().parse(src), "頁首頁尾")
    assert unit.text == "機密 | 第   頁，共   頁 | 承辦人：王小明 & 同仁"


def test_a_name_cut_by_a_style_code_and_a_literal_ampersand_are_masked_without_breaking_the_codes(tmp_path, settings):
    header = "&C承辦人 王&B小明&B && John Smith && Co"
    src = header_book(tmp_path / "h.xlsx", "oddHeader", header)
    _inv, _store, dst, residue, problems = run(src, tmp_path, settings, ["王小明", "Smith & Co"])
    out = headers(files(dst)["xl/worksheets/sheet1.xml"])["oddHeader"]
    assert out == escape("&C承辦人 ■&B■■&B && John ***** * **")  # the masked && is one masked character
    assert (residue, problems) == ([], []) and wellformed(dst)


def test_odd_even_and_first_page_headers_of_one_sheet_are_all_masked(tmp_path, settings):
    extra = ('<headerFooter differentOddEven="1" differentFirst="1">'
             + "".join(f"<{e}>{escape(h)}</{e}>" for e, h in [
                 ("oddHeader", f"&L{NAME}"), ("oddFooter", f"&R{MAIL}"), ("evenHeader", f"&C{ID}"),
                 ("evenFooter", f"&C&10 {PHONE}"), ("firstHeader", f"&R&B{NAME}&B"), ("firstFooter", f"&L{ID}&R&P")])
             + "</headerFooter>")
    src = pack(tmp_path / "h.xlsx", {"名單": sheet(f'<row r="1">{cell("A1", 0)}</row>', extra)}, sst("一般"))
    _inv, _store, dst, residue, problems = run(src, tmp_path, settings, [NAME, MAIL, ID, PHONE])
    out = headers(files(dst)["xl/worksheets/sheet1.xml"])
    assert out == {"oddHeader": "&amp;L**** *****", "oddFooter": "&amp;R" + "*" * len(MAIL), "evenHeader": "&amp;C**********",
                   "evenFooter": "&amp;C&amp;10 ************", "firstHeader": "&amp;R&amp;B**** *****&amp;B",
                   "firstFooter": "&amp;L**********&amp;R&amp;P"}
    assert leaks(dst, [NAME, MAIL, ID, PHONE]) == [] and (residue, problems) == ([], []) and wellformed(dst)


@pytest.mark.parametrize("number", [PHONE, "0912345678"])
def test_digits_run_into_a_font_size_are_reported_not_passed(tmp_path, settings, number):
    """&120912… is a font size of 120912 to the spreadsheet program: what it prints cannot be known. The
    header is listed as not fully checked, and the number left in it is residue."""
    src = header_book(tmp_path / "h.xlsx", "oddHeader", f"&C&12{number}", cell_text=number)
    inv, _store, _dst, residue, _problems = run(src, tmp_path, settings, [number])
    assert [u.reason for u in inv.unchecked if u.name.startswith("頁首頁尾")] == [xlsx_module.HF_UNCLEAR]
    assert residue == [f"xl/worksheets/sheet1.xml: 「{number}」"]


def code_context(src, tmp_path, settings, strings):
    ctx = build_context(src, tmp_path / "work", settings, llm=keyword_llm({}), ocr=FakeOcr(), faces=FakeFaces())
    ctx.load()
    for text, kind in strings:
        ctx.store.add_text(text, kind, "user")
    return ctx


def test_the_code_function_codes_a_header_string_and_the_coded_workbook_keeps_none(tmp_path, settings):
    extra = ('<headerFooter differentOddEven="1"><oddHeader>' + escape(f"&L{NAME}&C&\"Arial\"&12{ID}&R{MAIL}") + "</oddHeader>"
             + "<evenFooter>" + escape(f"&C&14 {PHONE}&R&B{NAME}") + "</evenFooter></headerFooter>")
    src = pack(tmp_path / "c.xlsx", {"名單": sheet(f'<row r="1">{cell("A1", 0)}</row>', extra)}, sst("一般"))
    words = [(NAME, "姓名"), (ID, "身分證字號"), (MAIL, "電子郵件"), (PHONE, "電話")]
    ctx = code_context(src, tmp_path, settings, words)
    coded = codes.build(ctx.inv, ctx.store)
    assert coded.residue == []
    assert not [w for w, _k in words if w in coded.text]
    lines = coded.text.splitlines()
    assert f"{coded_of(coded, NAME)} | {coded_of(coded, ID)} | {coded_of(coded, MAIL)}" in lines
    assert f"{coded_of(coded, PHONE)} | {coded_of(coded, NAME)}" in lines
    back = restore(coded.text, coded.table).text
    assert f"{NAME} | {ID} | {MAIL}" in back and f"{PHONE} | {NAME}" in back
    dst = tmp_path / "coded.xlsx"
    made = codefile.make(ctx, coded, dst)
    assert made.ok and dst.exists()
    assert leaks(dst, [w for w, _k in words]) == [] and ctx.adapter.residue(dst, [w for w, _k in words]) == [] and wellformed(dst)
    out = headers(files(dst)["xl/worksheets/sheet1.xml"])
    assert out["oddHeader"] == escape(f"&L**** *****&C&\"Arial\"&12**********&R{'*' * len(MAIL)}")  # the codes are as they were
    assert out["evenFooter"] == escape("&C&14 ************&R&B**** *****")


def coded_of(coded, original: str) -> str:
    return next(code for code, text in coded.table.items() if text == original)


# ---- X2: references that name several sheets; formulas kept in extension lists ---------------------

def test_a_reference_over_several_sheets_follows_a_renamed_sheet(tmp_path, settings):
    one = sheet('<row r="1"><c r="B1"><v>1</v></c></row>')
    refs = ('<row r="1"><c r="A1"><f>SUM(名單:王小明的資料!B1)</f><v>2</v></c>'
            "<c r=\"B1\"><f>SUM('名單:王小明的資料'!B1)</f><v>2</v></c>"
            '<c r="C1"><f>SUM(王小明的資料:總表!B1)</f><v>2</v></c>'
            "<c r=\"D1\"><f>SUM('O''Neil:王小明的資料'!B1)+王小明的資料!B1</f><v>3</v></c>"
            '<c r="E1"><f>SUM(王小明的資料!A1:王小明的資料!B1)</f><v>1</v></c></row>')
    src = pack(tmp_path / "r.xlsx", {"總表": sheet(refs), "名單": one, "O'Neil": one, "王小明的資料": one},
               wb_extra="<definedNames><definedName name=\"範圍\">'名單:王小明的資料'!$B$1</definedName></definedNames>")
    _inv, _store, dst, residue, problems = run(src, tmp_path, settings, ["王小明"])
    text = files(dst)["xl/worksheets/sheet1.xml"]
    assert re.findall(r"<f>([^<]*)</f>", text) == [
        "SUM('名單:■■■的資料'!B1)", "SUM('名單:■■■的資料'!B1)", "SUM('■■■的資料:總表'!B1)",
        "SUM('O''Neil:■■■的資料'!B1)+'■■■的資料'!B1", "SUM('■■■的資料'!A1:'■■■的資料'!B1)"]
    assert "<definedName name=\"範圍\">'名單:■■■的資料'!$B$1</definedName>" in files(dst)["xl/workbook.xml"]
    assert leaks(dst, ["王小明"]) == [] and (residue, problems) == ([], []) and wellformed(dst)


def extension_book(path):
    ns = f' xmlns:x14="{X14}" xmlns:xm="{XM}"'
    rows = f'<row r="1">{cell("A1", 0)}<c r="B1"><v>5</v></c></row>'
    ext = ('<extLst><ext uri="{78C0D931-6437-407d-A8EE-F0AAD7539E65}"><x14:conditionalFormattings><x14:conditionalFormatting>'
           '<x14:cfRule type="containsText" priority="1" operator="containsText" text="王小明" id="{00000000-0000-0000-0000-000000000001}">'
           "<xm:f>AND($A1=\"王小明\",'王小明的資料'!$B$1&gt;0,王小明的資料!$A$1&lt;&gt;\"\")</xm:f><x14:dxf/></x14:cfRule>"
           "<xm:sqref>A1:B1</xm:sqref></x14:conditionalFormatting></x14:conditionalFormattings></ext>"
           '<ext uri="{CCE6A557-97BC-4b89-ADB6-D9C93CAAB3DF}"><x14:dataValidations count="2">'
           '<x14:dataValidation type="list" prompt="請洽王小明" promptTitle="承辦人陳美玲"><x14:formula1><xm:f>王小明名單</xm:f></x14:formula1>'
           "<xm:sqref>C1</xm:sqref></x14:dataValidation>"
           '<x14:dataValidation type="list"><x14:formula1><xm:f>"陳美玲,林志豪"</xm:f></x14:formula1><xm:sqref>D1</xm:sqref>'
           "</x14:dataValidation></x14:dataValidations></ext></extLst>")
    data = sheet('<row r="1"><c r="A1"><v>1</v></c><c r="B1"><v>2</v></c></row>')
    return pack(path, {"表": sheet(rows, ext, ns), "王小明的資料": data}, sst("一般"),
                wb_extra="<definedNames><definedName name=\"王小明名單\">'王小明的資料'!$A$1:$B$1</definedName></definedNames>")


def test_formulas_in_an_extension_list_are_read_masked_and_follow_renamed_sheets_and_names(tmp_path, settings):
    src = extension_book(tmp_path / "x.xlsx")
    inv, _store, dst, residue, problems = run(src, tmp_path, settings, ["王小明", "陳美玲"])
    shown = "\n".join(t.text for t in inv.texts)
    assert "陳美玲,林志豪" in shown and "請洽王小明" in shown and "承辦人陳美玲" in shown
    assert [u.name for u in inv.unchecked] == []
    text = files(dst)["xl/worksheets/sheet1.xml"]
    assert re.findall(r"<xm:f>([^<]*)</xm:f>", text) == [
        "AND($A1=\"■■■\",'■■■的資料'!$B$1&gt;0,'■■■的資料'!$A$1&lt;&gt;\"\")", "___名單", '"■■■,林志豪"']
    assert 'text="■■■"' in text and 'prompt="請洽■■■"' in text and 'promptTitle="承辦人■■■"' in text
    assert leaks(dst, ["王小明", "陳美玲"]) == [] and (residue, problems) == ([], []) and wellformed(dst)


def test_the_cells_an_extension_formatting_covers_are_a_position_not_content(tmp_path, settings):
    rows = '<row r="1"><c r="A1"><v>2301</v></c></row>'
    ext = ('<extLst><ext uri="{78C0D931-6437-407d-A8EE-F0AAD7539E65}"><x14:conditionalFormattings><x14:conditionalFormatting>'
           '<x14:cfRule type="expression" priority="1" id="{00000000-0000-0000-0000-000000000002}"><xm:f>$A1&gt;0</xm:f><x14:dxf/>'
           "</x14:cfRule><xm:sqref>A1:A2301</xm:sqref></x14:conditionalFormatting></x14:conditionalFormattings></ext></extLst>")
    src = pack(tmp_path / "q.xlsx", {"表": sheet(rows, ext, f' xmlns:x14="{X14}" xmlns:xm="{XM}"')})
    _inv, _store, dst, residue, problems = run(src, tmp_path, settings, ["2301"])
    assert "<xm:sqref>A1:A2301</xm:sqref>" in files(dst)["xl/worksheets/sheet1.xml"] and (residue, problems) == ([], [])


def test_an_extension_namespace_bound_to_another_prefix_is_reported(tmp_path):
    xml = (sheet(f'<row r="1">{cell("A1", 0)}</row>', '<extLst><ext uri="{x}"><y:dataValidations count="1"><y:dataValidation type="list">'
                 '<y:formula1><m:f>"王小明"</m:f></y:formula1></y:dataValidation></y:dataValidations></ext></extLst>',
                 f' xmlns:y="{X14}" xmlns:m="{XM}"'))
    src = pack(tmp_path / "p.xlsx", {"表": xml}, sst("一般"))
    assert "xl/worksheets/sheet1.xml" in [u.name for u in XlsxAdapter().parse(src).unchecked]


def test_a_cell_summing_over_several_sheets_is_shown_as_computed_from_a_masked_cell(tmp_path):
    one = sheet('<row r="1"><c r="B1"><v>100</v></c></row>')
    masked = sheet('<row r="1"><c r="B1"><v>68000</v></c></row>')
    total = sheet('<row r="1"><c r="A1"><f>SUM(一月:三月!B1)</f><v>68200</v></c>'
                  "<c r=\"B1\"><f>SUM('一月:三月'!B1)</f><v>68200</v></c><c r=\"C1\"><f>SUM(一月:一月!B1)</f><v>100</v></c></row>")
    src = pack(tmp_path / "d.xlsx", {"一月": one, "二月": masked, "三月": one, "總表": total})
    ad = XlsxAdapter()
    inv = ad.parse(src)
    salary = next(t for t in inv.texts if t.section == "工作表：二月")
    found = ad.derived(src, {salary.id: [(0, 5)]})
    row = next(t for t in inv.texts if t.section == "工作表：總表")
    assert sorted((u, row.text[s:e]) for u, s, e, _origin in found) == [(row.id, "68200"), (row.id, "68200")]
    assert {origin for *_rest, origin in found} == {"B1"}


# ---- X3: cell style names, custom sort lists -----------------------------------------------------

STYLES = (HEAD + f'<styleSheet xmlns="{MAIN}"><fonts count="1"><font/></fonts><fills count="1"><fill/></fills><borders count="1"><border/>'
          '</borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="1"><xf xfId="0"/></cellXfs><cellStyles count="4">'
          '<cellStyle name="Normal" xfId="0" builtinId="0"/><cellStyle name="林志豪專用" xfId="0"/><cellStyle name="王小明" xfId="0"/>'
          '<cellStyle name="陳美玲" xfId="0"/></cellStyles></styleSheet>')


def styled_book(path):
    rows = f'<row r="1">{cell("A1", 0)}</row><row r="2">{cell("A2", 1)}</row>'
    extra = '<sortState ref="A1:A2"><sortCondition ref="A1:A2" customList="林志豪,張惠如"/></sortState>'
    table = (HEAD + f'<table xmlns="{MAIN}" id="1" name="Table1" displayName="Table1" ref="A1:A2"><autoFilter ref="A1:A2"/>'
             '<sortState ref="A2:A2"><sortCondition ref="A2:A2" customList="張惠如,林志豪"/></sortState>'
             '<tableColumns count="1"><tableColumn id="1" name="姓名"/></tableColumns></table>')
    return pack(path, {"表": sheet(rows, extra + '<tableParts count="1"><tablePart r:id="rIdT"/></tableParts>')}, sst("姓名", "一般"), {
        "xl/styles.xml": STYLES, "xl/tables/table1.xml": table,
        "xl/worksheets/_rels/sheet1.xml.rels": rels(("rIdT", "table", "../tables/table1.xml"))})


def test_custom_cell_style_names_and_custom_sort_lists_are_shown_and_masked(tmp_path, settings):
    src = styled_book(tmp_path / "s.xlsx")
    inv, _store, dst, residue, problems = run(src, tmp_path, settings, ["林志豪", "王小明", "陳美玲"])
    shown = [t.text for t in inv.texts]
    assert {"林志豪專用", "王小明", "陳美玲", "林志豪,張惠如", "張惠如,林志豪"} <= set(shown)
    assert "Normal" not in shown  # the names of the built-in styles are the program's, not anybody's
    out = files(dst)
    assert 'customList="■■■,張惠如"' in out["xl/worksheets/sheet1.xml"] and 'customList="張惠如,■■■"' in out["xl/tables/table1.xml"]
    names = re.findall(r'<cellStyle name="([^"]*)"', out["xl/styles.xml"])
    assert names[:2] == ["Normal", "■■■專用"] and len(set(names)) == 4  # two names masked alike are kept apart
    assert all("■■■" in n for n in names[1:])
    assert leaks(dst, ["林志豪", "王小明", "陳美玲"]) == [] and (residue, problems) == ([], []) and wellformed(dst)


def test_style_names_nobody_masked_are_left_as_they_were(tmp_path, settings):
    src = styled_book(tmp_path / "s.xlsx")
    _inv, _store, dst, _residue, problems = run(src, tmp_path, settings, ["張惠如"])
    assert files(dst)["xl/styles.xml"] == STYLES and problems == []
