"""What the independent review of the spreadsheet adapter found. Every workbook here is hand-built so that it
contains exactly the construct under test."""
import re
import time
import zipfile
from xml.parsers import expat

import pytest

from deid.findings import FindingStore
from deid.formats import xlsx as xlsx_module
from deid.formats.base import BrokenFile
from deid.formats.xlsx import XlsxAdapter

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


def sheet(rows: str, extra: str = "") -> str:
    return HEAD + f'<worksheet xmlns="{MAIN}" xmlns:r="{REL}"><sheetData>{rows}</sheetData>{extra}</worksheet>'


def sst(*strings: str) -> str:
    return HEAD + f'<sst xmlns="{MAIN}">' + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>"


def s(ref: str, index: int) -> str:
    return f'<c r="{ref}" t="s"><v>{index}</v></c>'


def rels(*items) -> str:
    return (HEAD + f'<Relationships xmlns="{PKG}">'
            + "".join(f'<Relationship Id="{i}" Type="{REL}/{t}" Target="{target}"/>' for i, t, target in items) + "</Relationships>")


def pack(path, sheets: dict, shared: str | None = None, parts: dict | None = None, wb_extra: str = "", types: str = ""):
    """sheets: name -> worksheet XML (str or bytes). parts: extra or replacement zip entries."""
    names = list(sheets)
    files = {
        "[Content_Types].xml": HEAD + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/><Default Extension="rels" ContentType="application/vnd.openxmlformats-'
        'package.relationships+xml"/><Default Extension="bin" ContentType="application/octet-stream"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + types + "</Types>",
        "_rels/.rels": rels(("rId1", "officeDocument", "xl/workbook.xml")),
        "xl/workbook.xml": HEAD + f'<workbook xmlns="{MAIN}" xmlns:r="{REL}">{wb_extra}<sheets>'
        + "".join(f'<sheet name="{n.replace(chr(39), "&apos;")}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, n in enumerate(names))
        + "</sheets></workbook>",
        "xl/_rels/workbook.xml.rels": rels(*[(f"rId{i + 1}", "worksheet", f"worksheets/sheet{i + 1}.xml") for i in range(len(names))],
                                           ("rIdS", "sharedStrings", "sharedStrings.xml")),
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
    """Mask every place the given strings are found; returns (inventory, plan, output, residue, verify)."""
    ad = XlsxAdapter()
    inv = ad.parse(src)
    store = FindingStore(inv)
    for w in words:
        store.add_text(w, "姓名", "user")
    for m in inv.metas:  # as the pipeline does: properties that name a person are masked without asking
        store.add_meta(m.id, on=m.person)
    plan = store.plan()
    dst = tmp_path / "out.xlsx"
    ad.apply(src, dst, plan, settings)
    return inv, plan, dst, ad.residue(dst, list(words)), ad.verify(src, dst, plan, settings)


def out(dst) -> dict:
    with zipfile.ZipFile(dst) as z:
        return {i.filename: z.read(i).decode("utf-8", "replace") for i in z.infolist()}


def everything(dst) -> str:
    return "\n".join(out(dst).values())


def wellformed(dst) -> bool:
    for name, xml in out(dst).items():
        if name.endswith((".xml", ".rels")):
            expat.ParserCreate().Parse(xml.encode("utf-8"), True)
    return True


def shown(inv) -> str:
    return "\n".join(t.text for t in inv.texts)


# ---- pivot caches ------------------------------------------------------------------------------

def pivot_book(path, many=0):
    items = "".join(f'<s v="項目{i}"/>' for i in range(many)) + '<s v="王小明"/><s v="A123456789"/>'
    definition = (HEAD + f'<pivotCacheDefinition xmlns="{MAIN}" xmlns:r="{REL}" r:id="rId1" refreshedBy="林志豪" recordCount="1">'
                  '<cacheSource type="worksheet"><worksheetSource ref="A1:C2" sheet="名單"/></cacheSource><cacheFields count="3">'
                  f'<cacheField name="姓名" numFmtId="0"><sharedItems count="{many + 2}">{items}</sharedItems></cacheField>'
                  '<cacheField name="薪資" numFmtId="0"><sharedItems containsSemiMixedTypes="0" containsString="0" containsNumber="1" '
                  'containsInteger="1" minValue="68000" maxValue="68000"><n v="68000"/></sharedItems></cacheField>'
                  '<cacheField name="生日" numFmtId="14"><sharedItems containsSemiMixedTypes="0" containsNonDate="0" containsDate="1" '
                  'containsString="0" minDate="1990-05-03T00:00:00" maxDate="1990-05-03T00:00:00"><d v="1990-05-03T00:00:00"/>'
                  "</sharedItems></cacheField></cacheFields></pivotCacheDefinition>")
    records = (HEAD + f'<pivotCacheRecords xmlns="{MAIN}" count="1"><r><x v="0"/><n v="68000"/><d v="1990-05-03T00:00:00"/></r>'
               '<r><s v="A123456789"/><n v="68000"/><d v="1990-05-03T00:00:00"/></r></pivotCacheRecords>')
    rows = (f'<row r="1">{s("A1", 0)}{s("B1", 1)}{s("C1", 2)}{s("D1", 4)}</row>'
            f'<row r="2">{s("A2", 3)}<c r="B2"><v>68000</v></c><c r="C2"><v>32996</v></c><c r="D2" t="s"><v>5</v></c></row>')
    return pack(path, {"名單": sheet(rows)}, sst("姓名", "薪資", "生日", "王小明", "證號", "A123456789"), {
        "xl/pivotCache/pivotCacheDefinition1.xml": definition,
        "xl/pivotCache/_rels/pivotCacheDefinition1.xml.rels": rels(("rId1", "pivotCacheRecords", "pivotCacheRecords1.xml")),
        "xl/pivotCache/pivotCacheRecords1.xml": records,
    }, types='<Override PartName="/xl/pivotCache/pivotCacheRecords1.xml" ContentType="application/vnd.openxmlformats-officedocument.'
             'spreadsheetml.pivotCacheRecords+xml"/>')


@pytest.mark.parametrize("many", [0, 600], ids=["small", "more-items-than-are-shown"])
def test_a_pivot_cache_keeps_no_copy_of_what_was_masked(tmp_path, settings, many):
    src = pivot_book(tmp_path / "p.xlsx", many)
    _inv, _plan, dst, residue, problems = run(src, tmp_path, settings, ["王小明", "68000", "A123456789"])
    names, text = out(dst), everything(dst)
    assert "王小明" not in text and "A123456789" not in text and "68000" not in text and "1990-05-03" not in text
    assert "林志豪" not in text  # the account that last refreshed the pivot table
    assert not [n for n in names if "pivotCacheRecords" in n] and "pivotCacheRecords" not in text
    assert 'refreshOnLoad="1"' in names["xl/pivotCache/pivotCacheDefinition1.xml"]
    assert (residue, problems) == ([], []) and wellformed(dst)


# ---- formulas ----------------------------------------------------------------------------------

def test_text_written_inside_a_formula_is_shown_and_can_be_masked(tmp_path, settings):
    rows = ('<row r="1"><c r="A1" t="str"><f>HYPERLINK("mailto:wang.xiaoming@example.com","寄信")</f><v>寄信</v></c>'
            '<c r="B1" t="b"><f>A1="王小明"</f><v>0</v></c>'
            '<c r="C1" t="e"><f>VLOOKUP("陳美玲",A:B,2,0)</f><v>#N/A</v></c>'
            '<c r="D1" t="str"><f>IF(A1="林志豪","","")</f><v></v></c></row>')
    src = pack(tmp_path / "f.xlsx", {"表": sheet(rows)})
    words = ["wang.xiaoming@example.com", "王小明", "陳美玲", "林志豪"]
    inv, _plan, dst, residue, problems = run(src, tmp_path, settings, words)
    assert all(w in shown(inv) for w in words)
    text = out(dst)["xl/worksheets/sheet1.xml"]
    assert not [w for w in words if w in text]
    assert 'VLOOKUP("■■■",A:B,2,0)' in text and 'A1="■■■"' in text and ',"寄信")' in text
    assert (residue, problems) == ([], []) and wellformed(dst)


def test_masking_a_number_leaves_cell_references_alone(tmp_path, settings):
    rows = ('<row r="1"><c r="A1"><v>2301</v></c><c r="B1"><f>SUM(C2:C2301)</f><v>5</v></c>'
            '<c r="C1"><f>B2*35+$B$35</f><v>7</v></c></row><row r="2"><c r="A2"><v>35</v></c></row>')
    extra = ('<dataValidations count="1"><dataValidation type="list" sqref="Z1"><formula1>$A$2:$A$35</formula1>'
             "</dataValidation></dataValidations>")
    src = pack(tmp_path / "n.xlsx", {"表": sheet(rows, extra)})
    _inv, _plan, dst, residue, problems = run(src, tmp_path, settings, ["2301", "35"])
    text = out(dst)["xl/worksheets/sheet1.xml"]
    assert "<f>SUM(C2:C2301)</f>" in text and "<f>B2*35+$B$35</f>" in text and "<formula1>$A$2:$A$35</formula1>" in text
    assert "<v>2301</v>" not in text and "<v>35</v>" not in text
    assert (residue, problems) == ([], [])


def test_formulas_follow_a_renamed_sheet(tmp_path, settings):
    refs = ('<row r="1"><c r="A1"><f>王小明!B1</f><v>1</v></c><c r="B1"><f>Wang!B1+1</f><v>2</v></c>'
            "<c r=\"C1\"><f>'O''Brien 王小明'!A1</f><v>3</v></c></row>")
    one = sheet('<row r="1"><c r="B1"><v>1</v></c></row>')
    app = (HEAD + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
           'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><TitlesOfParts><vt:vector size="4" '
           "baseType=\"lpstr\"><vt:lpstr>總表</vt:lpstr><vt:lpstr>王小明</vt:lpstr><vt:lpstr>Wang</vt:lpstr>"
           "<vt:lpstr>O'Brien 王小明</vt:lpstr></vt:vector></TitlesOfParts></Properties>")
    src = pack(tmp_path / "r.xlsx", {"總表": sheet(refs), "王小明": one, "Wang": one, "O'Brien 王小明": one}, parts={"docProps/app.xml": app})
    _inv, _plan, dst, residue, problems = run(src, tmp_path, settings, ["王小明", "Wang"])
    files = out(dst)
    names = [n.replace("&apos;", "'") for n in re.findall(r'<sheet name="([^"]*)"', files["xl/workbook.xml"])]
    assert names == ["總表", "■■■", "■■■■", "O'Brien ■■■"]
    text = files["xl/worksheets/sheet1.xml"]
    assert "<f>'■■■'!B1</f>" in text and "<f>'■■■■'!B1+1</f>" in text and "<f>'O''Brien ■■■'!A1</f>" in text
    assert re.findall(r"<vt:lpstr>([^<]*)</vt:lpstr>", files["docProps/app.xml"]) == names
    assert (residue, problems) == ([], [])


def test_the_first_cell_of_a_filled_down_formula_and_an_array_formula_can_be_masked(tmp_path, settings):
    rows = ('<row r="1"><c r="A1" t="str"><f t="shared" ref="A1:A3" si="0">B1&amp;"先生"</f><v>王小明先生</v></c>'
            '<c r="D1" t="str"><f t="array" ref="D1:D2">B1:B2&amp;"x"</f><v>王小明x</v></c></row>'
            '<row r="2"><c r="A2" t="str"><f t="shared" si="0"/><v>陳美玲先生</v></c><c r="D2" t="str"><v>陳美玲x</v></c></row>'
            '<row r="3"><c r="A3" t="str"><f t="shared" si="0"/><v>林志豪先生</v></c></row>')
    src = pack(tmp_path / "s.xlsx", {"表": sheet(rows)})
    _inv, _plan, dst, residue, problems = run(src, tmp_path, settings, ["王小明"])
    text = out(dst)["xl/worksheets/sheet1.xml"]
    assert "王小明" not in text and "■■■先生" in text and "■■■x" in text
    assert "<f" not in text and "陳美玲先生" in text and "林志豪先生" in text  # the rest of the group keeps its values
    assert (residue, problems) == ([], []) and wellformed(dst)


# ---- workbooks written another way ---------------------------------------------------------------

ROW = f'<row r="1">{s("A1", 0)}<c r="B1"><v>5</v></c></row>'


def prefixed(xml: str) -> str:
    """The same part with the main namespace bound to a prefix, as some libraries write it."""
    xml = re.sub(r"<(/?)(?!\?|/)([A-Za-z])", r"<\1x:\2", xml)
    return xml.replace(f'xmlns="{MAIN}"', f'xmlns:x="{MAIN}"')


def check_masked(src, tmp_path, settings):
    inv, _plan, dst, residue, problems = run(src, tmp_path, settings, ["王小明"])
    assert [t.text for t in inv.texts if t.section.startswith("工作表：")] == ["王小明 | 5"]
    after = XlsxAdapter().parse(dst)
    assert [t.text for t in after.texts if t.section.startswith("工作表：")] == ["■■■ | 5"]
    assert "王小明" not in everything(dst) and (residue, problems) == ([], []) and wellformed(dst)


def test_a_workbook_that_prefixes_every_element_is_read_and_masked(tmp_path, settings):
    wb = prefixed(HEAD + f'<workbook xmlns="{MAIN}" xmlns:r="{REL}"><sheets><sheet name="表" sheetId="1" r:id="rId1"/></sheets></workbook>')
    src = pack(tmp_path / "x.xlsx", {"表": prefixed(sheet(ROW))}, prefixed(sst("王小明")), {"xl/workbook.xml": wb})
    check_masked(src, tmp_path, settings)


def test_parts_stored_as_utf16_are_read_and_masked(tmp_path, settings):
    as16 = lambda xml: xml.replace('encoding="UTF-8"', 'encoding="UTF-16"').encode("utf-16")  # noqa: E731
    check_masked(pack(tmp_path / "u.xlsx", {"表": as16(sheet(ROW))}, as16(sst("王小明"))), tmp_path, settings)


def test_text_kept_in_a_cdata_section_is_read_and_masked(tmp_path, settings):
    check_masked(pack(tmp_path / "c.xlsx", {"表": sheet(ROW)}, sst("<![CDATA[王小明]]>")), tmp_path, settings)


def test_a_sheet_whose_file_name_differs_only_in_case_is_read(tmp_path, settings):
    src = pack(tmp_path / "k.xlsx", {"表": sheet(ROW)}, sst("王小明"),
               {"xl/_rels/workbook.xml.rels": rels(("rId1", "worksheet", "Worksheets/Sheet1.xml"), ("rIdS", "sharedStrings", "sharedStrings.xml"))})
    check_masked(src, tmp_path, settings)


def test_a_cell_pointing_past_the_string_table_is_reported_not_dropped(tmp_path):
    src = pack(tmp_path / "i.xlsx", {"表": sheet(f'<row r="1">{s("A1", 7)}<c r="B1" s="²"><v>5</v></c></row>')}, sst("王小明"))
    inv = XlsxAdapter().parse(src)
    assert [u.name for u in inv.unchecked] == ["xl/worksheets/sheet1.xml"]


def test_a_workbook_in_which_no_sheet_can_be_found_is_refused(tmp_path):
    wb = HEAD + '<workbook xmlns="urn:something-else"><sheets><tab name="表"/></sheets></workbook>'
    with pytest.raises(BrokenFile):
        XlsxAdapter().parse(pack(tmp_path / "w.xlsx", {"表": sheet(ROW)}, sst("王小明"), {"xl/workbook.xml": wb}))


def test_a_sheet_that_is_listed_but_missing_is_reported(tmp_path):
    src = pack(tmp_path / "m.xlsx", {"表": sheet(ROW), "第二張": None}, sst("王小明"))
    assert "第二張" in " ".join(u.name + u.reason for u in XlsxAdapter().parse(src).unchecked)


def test_two_entries_with_the_same_name_are_refused(tmp_path):
    src = pack(tmp_path / "d.xlsx", {"表": sheet(ROW)}, sst("王小明"))
    with zipfile.ZipFile(src, "a") as z:
        z.writestr("xl/sharedStrings.xml", sst("陳美玲"))
    with pytest.raises(BrokenFile):
        XlsxAdapter().parse(src)


def test_a_part_that_is_not_wellformed_is_refused_quickly(tmp_path):
    src = pack(tmp_path / "q.xlsx", {"表": sheet(ROW)}, HEAD + f'<sst xmlns="{MAIN}">' + "<!--" * 100_000)
    started = time.time()
    with pytest.raises(BrokenFile):
        XlsxAdapter().parse(src)
    assert time.time() - started < 5


def test_an_entry_that_unpacks_too_large_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(xlsx_module, "MAX_ENTRY_BYTES", 10_000)
    src = pack(tmp_path / "b.xlsx", {"表": sheet(ROW)}, sst("王小明"), {"xl/media/image1.png": b"\0" * 50_000})
    with pytest.raises(BrokenFile):
        XlsxAdapter().parse(src)


# ---- places that were neither read nor listed --------------------------------------------------------

def test_text_kept_in_workbook_and_sheet_settings_is_shown_and_can_be_masked(tmp_path, settings):
    wb_extra = ('<fileSharing userName="帳號甲秘密"/><definedNames><definedName name="名稱乙秘密" comment="備註丙秘密">表!$A$1</definedName>'
                "</definedNames>")
    extra = ('<autoFilter ref="A1:B2"><filterColumn colId="0"><filters><filter val="篩選丁秘密"/></filters></filterColumn>'
             '<filterColumn colId="1"><customFilters><customFilter operator="equal" val="自訂戊秘密"/></customFilters></filterColumn></autoFilter>'
             '<conditionalFormatting sqref="A1"><cfRule type="containsText" priority="1" operator="containsText" text="條件己秘密">'
             '<formula>NOT(ISERROR(SEARCH("條件己秘密",A1)))</formula></cfRule></conditionalFormatting>'
             '<dataValidations count="1"><dataValidation type="whole" sqref="A1" promptTitle="標題庚秘密" prompt="提示辛秘密" '
             'errorTitle="錯誤壬秘密" error="訊息癸秘密"><formula1>1</formula1></dataValidation></dataValidations>'
             '<scenarios><scenario name="情境子秘密" user="使用者丑秘密" comment="說明寅秘密"><inputCells r="A1" val="輸入卯秘密"/></scenario></scenarios>'
             '<tableParts count="1"><tablePart r:id="rIdT"/></tableParts>')
    table = (HEAD + f'<table xmlns="{MAIN}" id="1" name="Table1" displayName="Table1" ref="A1:B2"><tableColumns count="2">'
             '<tableColumn id="1" name="欄名辰秘密"/><tableColumn id="2" name="數量"/></tableColumns></table>')
    rows = f'<row r="1">{s("A1", 0)}{s("B1", 1)}</row><row r="2"><c r="A2"><v>1</v></c><c r="B2"><v>2</v></c></row>'
    src = pack(tmp_path / "t.xlsx", {"表": sheet(rows, extra)}, sst("欄名辰秘密", "數量"), {
        "xl/tables/table1.xml": table, "xl/worksheets/_rels/sheet1.xml.rels": rels(("rIdT", "table", "../tables/table1.xml"))}, wb_extra=wb_extra)
    secrets = [x + "秘密" for x in ("帳號甲", "名稱乙", "備註丙", "篩選丁", "自訂戊", "條件己", "標題庚", "提示辛", "錯誤壬", "訊息癸", "情境子",
                                  "使用者丑", "說明寅", "輸入卯", "欄名辰")]
    inv, _plan, dst, residue, problems = run(src, tmp_path, settings, ["秘密"])
    assert [x for x in secrets if x not in shown(inv)] == []
    assert "秘密" not in everything(dst) and (residue, problems) == ([], []) and wellformed(dst)
    assert 'name="Table1" displayName="Table1"' in out(dst)["xl/tables/table1.xml"]


def test_the_folder_the_workbook_was_saved_in_is_always_removed(tmp_path, settings):
    wb_extra = ('<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"><mc:Choice Requires="x15">'
                '<x15ac:absPath url="C:\\Users\\wang.xiaoming\\Documents\\" xmlns:x15ac="http://schemas.microsoft.com/office/spreadsheetml/'
                '2010/11/ac"/></mc:Choice></mc:AlternateContent>')
    src = pack(tmp_path / "a.xlsx", {"表": sheet(ROW)}, sst("一般"), wb_extra=wb_extra)
    _inv, _plan, dst, _residue, problems = run(src, tmp_path, settings, [])
    assert "wang.xiaoming" not in everything(dst) and problems == [] and wellformed(dst)


def test_parts_that_are_not_read_are_listed_by_name(tmp_path):
    unread = ["xl/diagrams/data1.xml", "xl/slicerCaches/slicerCache1.xml", "xl/queryTables/queryTable1.xml",
              "xl/ctrlProps/ctrlProp1.xml", "xl/activeX/activeX1.bin", "xl/charts/chartEx1.xml", "xl/richData/rdrichvalue.xml",
              "xl/macrosheets/sheet1.xml"]
    src = pack(tmp_path / "l.xlsx", {"表": sheet(ROW)}, sst("王小明"), {n: "<x>王小明</x>" for n in unread})
    listed = {u.name for u in XlsxAdapter().parse(src).unchecked}
    assert [n for n in unread if n not in listed] == []
    plain = pack(tmp_path / "plain.xlsx", {"表": sheet(ROW)}, sst("王小明"), {
        "xl/styles.xml": HEAD + f'<styleSheet xmlns="{MAIN}"/>', "xl/theme/theme1.xml": "<a/>", "xl/calcChain.xml": HEAD + f'<calcChain xmlns="{MAIN}"/>'})
    assert XlsxAdapter().parse(plain).unchecked == []


def test_printer_settings_are_dropped_together_with_what_points_at_them(tmp_path, settings):
    src = pack(tmp_path / "ps.xlsx", {"表": sheet(ROW, '<pageSetup paperSize="9" orientation="portrait" r:id="rIdP"/>')}, sst("一般"), {
        "xl/printerSettings/printerSettings1.bin": "\\\\PRINTSRV\\王小明的印表機".encode("utf-16-le"),
        "xl/worksheets/_rels/sheet1.xml.rels": rels(("rIdP", "printerSettings", "../printerSettings/printerSettings1.bin"))})
    assert XlsxAdapter().parse(src).unchecked == []
    _inv, _plan, dst, _residue, problems = run(src, tmp_path, settings, [])
    files = out(dst)
    assert not [n for n in files if "printerSettings" in n] and "printerSettings" not in "".join(files.values())
    assert '<pageSetup paperSize="9" orientation="portrait"/>' in files["xl/worksheets/sheet1.xml"] and problems == []


def test_a_string_no_cell_uses_any_more_is_emptied(tmp_path, settings):
    src = pack(tmp_path / "o.xlsx", {"表": sheet(ROW)}, sst("一般", "舊資料 林志豪"))
    _inv, _plan, dst, _residue, problems = run(src, tmp_path, settings, [])
    text = out(dst)["xl/sharedStrings.xml"]
    assert "林志豪" not in text and "一般" in text and text.count("<si>") == 2 and problems == []


def test_the_reading_guide_of_an_inline_string_goes_when_its_cell_is_masked(tmp_path, settings):
    rows = ('<row r="1"><c r="A1" t="inlineStr"><is><t>山田太郎</t><rPh sb="0" eb="4"><t>ヤマダタロウ</t></rPh><phoneticPr fontId="1"/></is></c></row>')
    _inv, _plan, dst, residue, problems = run(pack(tmp_path / "ph.xlsx", {"表": sheet(rows)}), tmp_path, settings, ["山田太郎"])
    text = out(dst)["xl/worksheets/sheet1.xml"]
    assert "ヤマダタロウ" not in text and "■■■■" in text and (residue, problems) == ([], []) and wellformed(dst)


# ---- checking ----------------------------------------------------------------------------------------

def test_an_id_number_left_in_any_attribute_is_reported(tmp_path):
    src = pack(tmp_path / "v.xlsx", {"表": sheet(ROW)}, sst("一般"), {"xl/unknown/part1.xml": '<x note="A123456789" phone="0912-345-678"/>'})
    found = XlsxAdapter().residue(src, ["A123456789"])
    assert len(found) == 1 and "part1.xml" in found[0]


DRAWING = ('<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
           'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><xdr:twoCellAnchor><xdr:from><xdr:col>1</xdr:col><xdr:row>35</xdr:row>'
           '</xdr:from><xdr:pic><xdr:nvPicPr><xdr:cNvPr id="2" name="圖片 1" descr="王小明的照片"/></xdr:nvPicPr></xdr:pic></xdr:twoCellAnchor>'
           '<xdr:twoCellAnchor><xdr:pic><xdr:nvPicPr><xdr:cNvPr id="3" name="圖片 2" descr="陳美玲的照片"/></xdr:nvPicPr></xdr:pic>'
           "</xdr:twoCellAnchor></xdr:wsDr>")
CHART = ('<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" xmlns:a="http://schemas.openxmlformats.org/drawingml/'
         '2006/main"><c:chart><c:title><c:tx><c:rich><a:p><a:r><a:t>年度報表 王小明</a:t></a:r></a:p></c:rich></c:tx></c:title><c:plotArea>'
         "<c:barChart><c:ser><c:val><c:numRef><c:f>表!$B$1:$B$2</c:f><c:numCache><c:formatCode>General</c:formatCode><c:ptCount val=\"2\"/>"
         '<c:pt idx="0"><c:v>68000</c:v></c:pt><c:pt idx="1"><c:v>32996</c:v></c:pt></c:numCache></c:numRef></c:val></c:ser></c:barChart>'
         "</c:plotArea></c:chart></c:chartSpace>")


def test_two_descriptions_masked_to_the_same_text_and_a_number_equal_to_a_position_do_not_fail_the_check(tmp_path, settings):
    rows = f'<row r="1">{s("A1", 0)}<c r="B1"><v>68000</v></c></row><row r="2"><c r="A2"><v>35</v></c><c r="B2" s="1"><v>32996</v></c></row>'
    src = pack(tmp_path / "g.xlsx", {"表": sheet(rows)}, sst("王小明"), {"xl/drawings/drawing1.xml": DRAWING, "xl/charts/chart1.xml": CHART})
    _inv, _plan, dst, residue, problems = run(src, tmp_path, settings, ["王小明", "陳美玲", "35", "68000"])
    assert (residue, problems) == ([], [])
    chart = out(dst)["xl/charts/chart1.xml"]
    assert "68000" not in chart and "32996" not in chart and "<c:f>表!$B$1:$B$2</c:f>" in chart  # cached values go, the range stays


def test_a_cell_written_without_its_address_stays_without_one(tmp_path, settings):
    src = pack(tmp_path / "nr.xlsx", {"表": sheet("<row><c><v>68000</v></c></row>")})
    _inv, _plan, dst, residue, problems = run(src, tmp_path, settings, ["68000"])
    text = out(dst)["xl/worksheets/sheet1.xml"]
    assert 'r=""' not in text and "*****" in text and (residue, problems) == ([], [])


def test_masking_every_row_of_a_large_sheet_does_not_take_minutes(tmp_path, settings):
    rows = "".join(f'<row r="{i}"><c r="A{i}" t="inlineStr"><is><t>王小明</t></is></c><c r="B{i}"><v>{i}</v></c></row>' for i in range(1, 40_001))
    src = pack(tmp_path / "big.xlsx", {"表": sheet(rows)})
    ad = XlsxAdapter()
    store = FindingStore(ad.parse(src))
    store.add_text("王小明", "姓名", "user")
    started = time.time()
    ad.apply(src, tmp_path / "big-out.xlsx", store.plan(), settings)
    assert time.time() - started < 12


def test_text_in_legacy_shapes_link_tips_and_two_more_file_properties_is_shown_and_can_be_masked(tmp_path, settings):
    vml = ('<xml xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:x="urn:schemas-microsoft-com:'
           'office:excel"><v:shape id="s1" type="#t" alt="替代甲秘密" o:title="標題乙秘密"><v:textbox><div><font>按鈕丙秘密</font></div></v:textbox>'
           "<x:ClientData ObjectType=\"Button\"><x:Anchor>1, 15, 0, 2, 3, 31, 4, 1</x:Anchor></x:ClientData></v:shape></xml>")
    drawing = ('<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" xmlns:a="http://schemas.openxmlformats.'
               'org/drawingml/2006/main"><xdr:twoCellAnchor><xdr:sp><xdr:nvSpPr><xdr:cNvPr id="2" name="矩形 1"><a:hlinkClick tooltip="提示丁秘密"/>'
               "</xdr:cNvPr></xdr:nvSpPr></xdr:sp></xdr:twoCellAnchor></xdr:wsDr>")
    app = (HEAD + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
           "<HyperlinkBase>\\\\server\\基底戊秘密\\</HyperlinkBase></Properties>")
    core = (HEAD + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/"><cp:contentStatus>狀態己秘密</cp:contentStatus></cp:coreProperties>')
    src = pack(tmp_path / "vml.xlsx", {"表": sheet(ROW)}, sst("一般"), {
        "xl/drawings/vmlDrawing1.vml": vml, "xl/drawings/drawing1.xml": drawing, "docProps/app.xml": app, "docProps/core.xml": core})
    inv = XlsxAdapter().parse(src)
    everywhere = shown(inv) + "\n".join(m.value for m in inv.metas)
    assert [x for x in ("替代甲", "標題乙", "按鈕丙", "提示丁", "基底戊", "狀態己") if x + "秘密" not in everywhere] == []
    assert "1, 15, 0" not in everywhere  # positions of a legacy shape are not content
    store = FindingStore(inv)
    store.add_text("秘密", "姓名", "user")
    for m in inv.metas:
        store.add_meta(m.id, on=True)
    plan, dst, ad = store.plan(), tmp_path / "vml-out.xlsx", XlsxAdapter()
    ad.apply(src, dst, plan, settings)
    assert "秘密" not in everything(dst) and ad.residue(dst, ["秘密"]) == [] and ad.verify(src, dst, plan, settings) == []


def test_an_embedded_file_that_unpacks_too_large_is_refused(tmp_path, monkeypatch):
    import io
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/sharedStrings.xml", "<sst>" + "x" * 200_000 + "</sst>")
    monkeypatch.setattr(xlsx_module, "MAX_XML_PART", 100_000)
    src = pack(tmp_path / "e.xlsx", {"表": sheet(ROW)}, sst("王小明"), {"xl/embeddings/Microsoft_Excel_Sheet1.xlsx": inner.getvalue()})
    with pytest.raises(BrokenFile):
        XlsxAdapter().parse(src)


def test_a_header_left_unmasked_is_noticed_even_when_its_sheet_was_renamed(tmp_path, settings):
    src = pack(tmp_path / "h.xlsx", {"王小明": sheet(ROW, "<headerFooter><oddHeader>&amp;C承辦人 王小明</oddHeader></headerFooter>")}, sst("一般"))
    ad = XlsxAdapter()
    store = FindingStore(ad.parse(src))
    store.add_text("王小明", "姓名", "user")
    plan = store.plan()
    header = next(u for u, t in ((t.id, t) for t in ad.parse(src).texts) if t.section.startswith("頁首頁尾"))
    partial = type(plan)(text={u: v for u, v in plan.text.items() if u != header}, meta={}, boxes={}, strings=[])
    dst = tmp_path / "h-out.xlsx"
    ad.apply(src, dst, partial, settings)  # the sheet is renamed, the header is not masked
    assert "承辦人 王小明" in out(dst)["xl/worksheets/sheet1.xml"]
    assert any("頁首頁尾" in problem for problem in ad.verify(src, dst, plan, settings))


def test_names_of_tables_pivot_tables_and_custom_properties_and_reading_guides_are_shown_and_can_be_masked(tmp_path, settings):
    rows = (f'<row r="1">{s("A1", 0)}{s("B1", 1)}<c r="C1"><f>SUM(王小明名單[王小明欄])</f><v>1</v></c>{s("D1", 2)}</row>'
            '<row r="2"><c r="A2"><v>1</v></c><c r="B2"><v>2</v></c></row>')
    shared = (HEAD + f'<sst xmlns="{MAIN}"><si><t>王小明欄</t></si><si><t>數量</t></si>'
              '<si><t>山田</t><rPh sb="0" eb="2"><t>ヤマダ</t></rPh></si></sst>')
    table = (HEAD + f'<table xmlns="{MAIN}" id="1" name="王小明名單" displayName="王小明名單" ref="A1:B2"><tableColumns count="2">'
             '<tableColumn id="1" name="王小明欄"/><tableColumn id="2" name="數量"/></tableColumns></table>')
    pivot = HEAD + f'<pivotTableDefinition xmlns="{MAIN}" name="王小明的樞紐" cacheId="1" dataCaption="值"/>'
    slicer = ('<slicerCacheDefinition xmlns="http://schemas.microsoft.com/office/spreadsheetml/2009/9/main" name="S"><pivotTables>'
              '<pivotTable tabId="1" name="王小明的樞紐"/></pivotTables></slicerCacheDefinition>')
    custom = (HEAD + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" '
              'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><property fmtid="{D5CDD505-2E9C-101B-9397-'
              '08002B2CF9AE}" pid="2" name="王小明的編號"><vt:i4>7</vt:i4></property></Properties>')
    src = pack(tmp_path / "names.xlsx", {"表": sheet(rows, '<tableParts count="1"><tablePart r:id="rIdT"/></tableParts>')}, shared, {
        "xl/tables/table1.xml": table, "xl/worksheets/_rels/sheet1.xml.rels": rels(("rIdT", "table", "../tables/table1.xml")),
        "xl/pivotTables/pivotTable1.xml": pivot, "xl/slicerCaches/slicerCache1.xml": slicer, "docProps/custom.xml": custom})
    inv, _plan, dst, residue, problems = run(src, tmp_path, settings, ["王小明", "ヤマダ"])
    assert [x for x in ("王小明名單", "王小明的樞紐", "王小明的編號", "ヤマダ") if x not in shown(inv)] == []
    files = out(dst)
    assert 'name="___名單" displayName="___名單"' in files["xl/tables/table1.xml"] and 'name="■■■欄"' in files["xl/tables/table1.xml"]
    assert "<f>SUM(___名單[■■■欄])</f>" in files["xl/worksheets/sheet1.xml"]  # the formula still points at the table and column
    assert 'name="■■■的樞紐"' in files["xl/pivotTables/pivotTable1.xml"] and 'name="■■■的樞紐"' in files["xl/slicerCaches/slicerCache1.xml"]
    assert 'name="■■■的編號"' in files["docProps/custom.xml"]
    assert "ヤマダ" not in files["xl/sharedStrings.xml"] and "山田" in files["xl/sharedStrings.xml"]
    assert (residue, problems) == ([], []) and wellformed(dst)


def test_the_record_of_tracked_changes_is_removed_with_everything_that_points_at_it(tmp_path, settings):
    log = (HEAD + f'<revisions xmlns="{MAIN}"><rcc rId="1" sId="1"><oc r="B1"><v>68000</v></oc><nc r="B1"><v>5</v></nc></rcc></revisions>')
    src = pack(tmp_path / "rev.xlsx", {"表": sheet(ROW)}, sst("一般"), {
        "xl/revisions/revisionHeaders.xml": HEAD + f'<headers xmlns="{MAIN}" xmlns:r="{REL}"><header guid="{{1}}" userName="王小明" r:id="rId1"/></headers>',
        "xl/revisions/revisionLog1.xml": log, "xl/revisions/userNames.xml": HEAD + f'<users xmlns="{MAIN}"><userInfo name="王小明"/></users>',
        "xl/revisions/_rels/revisionHeaders.xml.rels": rels(("rId1", "revisionLog", "revisionLog1.xml")),
        "xl/_rels/workbook.xml.rels": rels(("rId1", "worksheet", "worksheets/sheet1.xml"), ("rIdS", "sharedStrings", "sharedStrings.xml"),
                                           ("rIdH", "revisionHeaders", "revisions/revisionHeaders.xml"), ("rIdU", "usernames", "revisions/userNames.xml"))},
        types='<Override PartName="/xl/revisions/revisionHeaders.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.'
              'revisionHeaders+xml"/><Override PartName="/xl/revisions/revisionLog1.xml" ContentType="application/vnd.openxmlformats-officedocument.'
              'spreadsheetml.revisionLog+xml"/>')
    inv = XlsxAdapter().parse(src)
    assert inv.unchecked == [] and any("追蹤修訂" in note for note in inv.notes)
    _inv, _plan, dst, residue, problems = run(src, tmp_path, settings, [])
    files = out(dst)
    assert not [n for n in files if "revisions" in n] and "revision" not in "".join(files.values()) and "usernames" not in "".join(files.values())
    assert "68000" not in everything(dst) and "王小明" not in everything(dst) and problems == [] and wellformed(dst)
