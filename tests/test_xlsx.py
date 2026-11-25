import io
import zipfile
from xml.parsers import expat

import pytest

from deid.findings import FindingStore
from deid.formats.base import SUPPORTED, BrokenFile, adapter_for
from deid.formats.xlsx import XlsxAdapter
from deid.imaging import is_black
from deid.model import MaskPlan
from deid.pipeline import analyze, apply_and_verify, build_context
from tests.builders import make_docx, make_xlsx, text_image
from tests.fakes import FakeFaces, FakeOcr, keyword_llm, ocr_line

ROWS = [["姓名", "電話", "生日", "薪資", "備註"],
        ["王小明", "0912-345-678", ("date", 32996), 68000, ("formula", 'A2&"先生"', "王小明先生")],
        [("rich",), ("inline", "陳美玲"), None, ("numformula", "SUM(D2:D2)", 68000), "王小明"],
        ["一般", "王小明", None, 2301, None]]


@pytest.fixture
def book(tmp_path):
    return make_xlsx(tmp_path / "名單.xlsx", [{"name": "名單", "rows": ROWS},
                                             {"name": "王小明的資料", "rows": [["隱藏表", "林志豪"]], "state": "hidden"}],
                     creator="林志豪", comment=("審閱者甲", "請王小明確認", "A2"), image=text_image(["登入者：王小明"], size=(600, 200)),
                     header="承辦人 王小明", hyperlink=("寄信給承辦人", "mailto:wang@example.com"),
                     defined_name=("名單範圍", "'王小明的資料'!$A$1:$B$1"), calc_chain=True, rich=("張", "惠如"),
                     validation="王小明,陳美玲", pivot_items=["王小明", "陳美玲"], thumbnail=text_image(["x"], size=(60, 40)))


def entries(path):
    with zipfile.ZipFile(path) as z:
        return {i.filename: z.read(i) for i in z.infolist()}


def texts(inv, prefix):
    return [t.text for t in inv.texts if t.section.startswith(prefix)]


def spans(inv, needle, section_prefix=""):
    out = {}
    for t in inv.texts:
        if not t.section.startswith(section_prefix):
            continue
        found, i = [], t.text.find(needle)
        while i >= 0:
            found.append((i, i + len(needle)))
            i = t.text.find(needle, i + len(needle))
        if found:
            out[t.id] = found
    return out


def apply(src, tmp_path, settings, plan):
    dst = tmp_path / "out.xlsx"
    XlsxAdapter().apply(src, dst, plan, settings)
    return dst


def test_excel_is_a_supported_format(tmp_path):
    assert ".xlsx" in SUPPORTED and ".xlsm" in SUPPORTED
    assert isinstance(adapter_for(tmp_path / "a.XLSX"), XlsxAdapter)


def test_each_row_is_a_unit_with_cells_joined_and_dates_readable(book):
    inv = XlsxAdapter().parse(book)
    assert inv.kind == "xlsx"
    rows = texts(inv, "工作表：名單")
    assert rows[0] == "姓名 | 電話 | 生日 | 薪資 | 備註"
    assert rows[1] == "王小明 | 0912-345-678 | 1990/5/3 | 68000 | 王小明先生"
    assert rows[2] == "張惠如 | 陳美玲 | 68000 | 王小明"
    assert texts(inv, "工作表：王小明的資料（隱藏）") == ["隱藏表 | 林志豪"]


def test_other_places_holding_text_are_units_too(book):
    inv = XlsxAdapter().parse(book)
    assert "王小明的資料" in texts(inv, "工作表名稱") and "名單" in texts(inv, "工作表名稱")
    assert texts(inv, "註解") == ["請王小明確認"]
    assert any("承辦人 王小明" in t for t in texts(inv, "頁首頁尾"))
    hidden = texts(inv, "隱藏內容")
    assert "mailto:wang@example.com" in hidden and "寄信給承辦人" in hidden and "王小明的截圖" in hidden
    assert any("王小明,陳美玲" in t for t in hidden)
    assert {"王小明", "陳美玲"} <= set(texts(inv, "樞紐分析快取"))
    metas = {(m.label, m.value, m.person) for m in inv.metas}
    assert ("作者", "林志豪", True) in metas and ("註解作者", "審閱者甲", True) in metas
    [img] = inv.images
    assert img.supported and (img.width, img.height) == (600, 200)
    assert XlsxAdapter().parse(book) == inv


def test_text_cells_are_masked_in_place_and_the_file_stays_valid(book, tmp_path, settings):
    ad = XlsxAdapter()
    inv = ad.parse(book)
    plan = MaskPlan(text=spans(inv, "王小明"), strings=["王小明"])
    dst = apply(book, tmp_path, settings, plan)
    out = ad.parse(dst)
    rows = texts(out, "工作表：名單")
    assert rows[1] == "■■■ | 0912-345-678 | 1990/5/3 | 68000 | ■■■先生"
    assert rows[2] == "張惠如 | 陳美玲 | 68000 | ■■■" and rows[3] == "一般 | ■■■ | 2301"
    assert texts(out, "註解") == ["請■■■確認"]
    after = entries(dst)
    for name, data in after.items():
        if name.endswith((".xml", ".rels")):
            expat.ParserCreate().Parse(data, True)
    blob = b"\n".join(v for k, v in after.items() if k.endswith((".xml", ".rels"))).decode("utf-8")
    assert "王小明" not in blob
    assert ad.residue(dst, ["王小明"]) == [] and ad.verify(book, dst, plan, settings) == []
    assert not any(n.startswith("docProps/thumbnail") for n in after)


def test_masked_formula_cell_becomes_a_static_value_and_the_calc_chain_is_dropped(book, tmp_path, settings):
    ad = XlsxAdapter()
    inv = ad.parse(book)
    dst = apply(book, tmp_path, settings, MaskPlan(text=spans(inv, "王小明先生", "工作表：名單")))
    after = entries(dst)
    sheet = after["xl/worksheets/sheet1.xml"].decode("utf-8")
    assert 'A2&amp;"先生"' not in sheet and "A2&" not in sheet
    assert "SUM(D2:D2)" in sheet  # formulas that were not masked stay
    assert "xl/calcChain.xml" not in after
    assert "calcChain" not in after["xl/_rels/workbook.xml.rels"].decode("utf-8")
    assert "calcChain" not in after["[Content_Types].xml"].decode("utf-8")


def test_number_and_date_cells_are_masked_whole(book, tmp_path, settings):
    ad = XlsxAdapter()
    inv = ad.parse(book)
    row = next(t for t in inv.texts if t.text.startswith("王小明 | 0912"))
    start = row.text.index("1990")
    plan = MaskPlan(text={row.id: [(start, start + 4), (row.text.index("68000"), row.text.index("68000") + 5)]})
    dst = apply(book, tmp_path, settings, plan)
    out_row = next(t for t in ad.parse(dst).texts if t.text.startswith("王小明 | 0912"))
    assert out_row.text == "王小明 | 0912-345-678 | ******** | ***** | 王小明先生"
    assert ad.verify(book, dst, plan, settings) == []
    sheet = entries(dst)["xl/worksheets/sheet1.xml"].decode("utf-8")
    assert "<v>32996</v>" not in sheet and "<v>68000</v>" in sheet  # the other 68000 (row 3) is untouched


def test_one_cell_of_a_shared_string_can_be_masked_without_touching_the_others(book, tmp_path, settings):
    ad = XlsxAdapter()
    inv = ad.parse(book)
    target = next(t for t in inv.texts if t.text == "一般 | 王小明 | 2301")
    plan = MaskPlan(text={target.id: [(5, 8)]})
    dst = apply(book, tmp_path, settings, plan)
    rows = texts(ad.parse(dst), "工作表：名單")
    assert rows[3] == "一般 | ■■■ | 2301" and rows[1].startswith("王小明 | ") and rows[2].endswith("| 王小明")
    assert ad.verify(book, dst, plan, settings) == []


def test_rich_text_shared_string_is_masked_across_its_runs(book, tmp_path, settings):
    ad = XlsxAdapter()
    inv = ad.parse(book)
    dst = apply(book, tmp_path, settings, MaskPlan(text=spans(inv, "張惠如"), strings=["張惠如"]))
    shared = entries(dst)["xl/sharedStrings.xml"].decode("utf-8")
    assert "<r><t>■</t></r>" in shared and "惠如" not in shared and "<b/>" in shared
    assert ad.residue(dst, ["張惠如"]) == []


def test_sheet_name_is_renamed_and_references_follow(book, tmp_path, settings):
    ad = XlsxAdapter()
    inv = ad.parse(book)
    name_unit = next(t for t in inv.texts if t.section == "工作表名稱" and t.text == "王小明的資料")
    plan = MaskPlan(text={name_unit.id: [(0, 3)]}, strings=[])
    dst = apply(book, tmp_path, settings, plan)
    after = entries(dst)
    workbook = after["xl/workbook.xml"].decode("utf-8")
    assert 'name="■■■的資料"' in workbook and "王小明的資料" not in workbook
    assert "'■■■的資料'!$A$1:$B$1" in workbook
    assert "<vt:lpstr>■■■的資料</vt:lpstr>" in after["docProps/app.xml"].decode("utf-8")
    assert ad.verify(book, dst, plan, settings) == []


def test_hidden_places_metadata_and_images_are_handled(book, tmp_path, settings):
    ad = XlsxAdapter()
    inv = ad.parse(book)
    plan = MaskPlan(text={**spans(inv, "王小明"), **spans(inv, "wang@example.com"), **spans(inv, "陳美玲")},
                    strings=["王小明", "wang@example.com", "陳美玲"],
                    meta={m.id: [(0, len(m.value))] for m in inv.metas if m.person},
                    boxes={inv.images[0].id: [(100.0, 20.0, 400.0, 90.0)]})
    dst = apply(book, tmp_path, settings, plan)
    after = entries(dst)
    assert 'Target="about:blank"' in after["xl/worksheets/_rels/sheet1.xml.rels"].decode("utf-8")
    assert "王小明" not in after["xl/pivotCache/pivotCacheDefinition1.xml"].decode("utf-8")
    # the numbers a pivot cache keeps cannot be matched to the cells that were masked: all are blanked, and the
    # spreadsheet program rebuilds the cache from the masked sheet when the file is opened
    assert '<n v="0"/>' in after["xl/pivotCache/pivotCacheDefinition1.xml"].decode("utf-8")
    assert "王小明" not in after["xl/drawings/drawing1.xml"].decode("utf-8")
    assert is_black(after["xl/media/image1.png"], (100, 20, 400, 90))
    out = ad.parse(dst)
    assert not any(m.label == "作者" for m in out.metas) and {m.value for m in out.metas if m.label == "註解作者"} == {"－"}
    assert ad.residue(dst, plan.strings) == [] and ad.verify(book, dst, plan, settings) == []


def test_whole_flow_with_safety_net_and_image_text(book, tmp_path, settings):
    names = {"王小明": "姓名", "陳美玲": "姓名", "0912-345-678": "電話", "wang@example.com": "電子郵件", "1990/5/3": "出生年月日"}
    ctx = build_context(book, tmp_path / "work", settings, llm=keyword_llm(names), ocr=FakeOcr([ocr_line("登入者：王小明")]),
                        faces=FakeFaces())
    analyze(ctx, use_agent=False)
    found = {f.text: f for f in ctx.store.findings if f.kind == "text"}
    assert set(names) <= set(found)
    assert any(o.kind == "box" for o in found["王小明"].occurrences)
    result = apply_and_verify(ctx)
    assert result.ok, (result.residue, result.problems)
    out = tmp_path / "work" / "output.xlsx"
    rows = texts(XlsxAdapter().parse(out), "工作表：名單")
    assert rows[1] == "■■■ | ************ | ******** | 68000 | ■■■先生"


def test_view_lists_rows_then_images(book):
    ad = XlsxAdapter()
    inv = ad.parse(book)
    view = ad.view(book, inv)
    assert view[0] == {"type": "text", "unit": inv.texts[0].id, "section": "工作表：名單"}
    assert view[-1] == {"type": "image", "image": inv.images[0].id}


def test_things_that_cannot_be_checked_are_listed(tmp_path):
    src = make_xlsx(tmp_path / "x.xlsx", [{"name": "S", "rows": [["a"]]}])
    with zipfile.ZipFile(src, "a") as z:
        z.writestr("xl/externalLinks/externalLink1.xml", '<?xml version="1.0"?><externalLink xmlns="urn:x"><v>王小明</v></externalLink>')
        z.writestr("xl/vbaProject.bin", b"macro bytes")
        z.writestr("xl/connections.xml", '<?xml version="1.0"?><connections xmlns="urn:x"/>')
    names = " ".join(u.name for u in XlsxAdapter().parse(src).unchecked)
    assert "externalLink1.xml" in names and "vbaProject.bin" in names and "connections.xml" in names


def test_files_that_are_not_workbooks_are_rejected(tmp_path):
    bad = tmp_path / "bad.xlsx"
    bad.write_bytes(b"not a zip")
    with pytest.raises(BrokenFile):
        XlsxAdapter().parse(bad)
    word = make_docx(tmp_path / "w.docx", ["內文"])
    fake = tmp_path / "fake.xlsx"
    fake.write_bytes(word.read_bytes())
    with pytest.raises(BrokenFile) as e:
        XlsxAdapter().parse(fake)
    assert "Excel" in str(e.value)
    old = tmp_path / "old.xlsx"
    old.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 64)
    with pytest.raises(BrokenFile) as e:
        XlsxAdapter().parse(old)
    assert "密碼" in str(e.value) or ".xls" in str(e.value)


def test_manual_single_cell_finding_goes_through_the_store(book, tmp_path, settings):
    ad = XlsxAdapter()
    inv = ad.parse(book)
    row = next(t for t in inv.texts if t.text == "一般 | 王小明 | 2301")
    store = FindingStore(inv)
    store.add_text("2301", "其他識別碼", "user", only=(row.id, 11, 15))
    dst = apply(book, tmp_path, settings, store.plan())
    assert texts(ad.parse(dst), "工作表：名單")[3] == "一般 | 王小明 | ****"


def test_shared_string_is_not_left_orphaned_when_its_cells_are_masked_differently(tmp_path, settings):
    src = make_xlsx(tmp_path / "o.xlsx", [{"name": "S", "rows": [["王小明先生"], ["王小明先生"]]}])
    ad = XlsxAdapter()
    inv = ad.parse(src)
    a, b = [t for t in inv.texts if t.section == "工作表：S"]
    plan = MaskPlan(text={a.id: [(0, 3)], b.id: [(0, 5)]})  # both cells masked, to different extents
    dst = apply(src, tmp_path, settings, plan)
    assert "王小明" not in entries(dst)["xl/sharedStrings.xml"].decode("utf-8")
    assert "王小明" not in entries(dst)["xl/worksheets/sheet1.xml"].decode("utf-8")


def test_text_literal_inside_a_formula_is_masked(tmp_path, settings):
    src = make_xlsx(tmp_path / "f.xlsx", [{"name": "S", "rows": [["王小明", ("numformula", 'COUNTIF(A1:A9,"王小明")', 1)]]}])
    ad = XlsxAdapter()
    inv = ad.parse(src)
    plan = MaskPlan(text=spans(inv, "王小明"), strings=["王小明"])
    dst = apply(src, tmp_path, settings, plan)
    sheet = entries(dst)["xl/worksheets/sheet1.xml"].decode("utf-8")
    assert "王小明" not in sheet and 'COUNTIF(A1:A9,"■■■")' in sheet
    assert ad.residue(dst, ["王小明"]) == []


def test_legacy_comment_drawing_is_masked_too(tmp_path, settings):
    src = make_xlsx(tmp_path / "v.xlsx", [{"name": "S", "rows": [["王小明"]]}], comment=("審閱者甲", "請王小明確認", "A1"))
    data = entries(src)
    data["xl/drawings/vmlDrawing1.vml"] = ('<xml xmlns:v="urn:schemas-microsoft-com:vml"><v:shape><v:textbox><div>請王小明確認</div>'
                                           "</v:textbox></v:shape></xml>").encode("utf-8")
    with zipfile.ZipFile(src, "w") as z:
        for name, blob in data.items():
            z.writestr(name, blob)
    ad = XlsxAdapter()
    inv = ad.parse(src)
    plan = MaskPlan(text=spans(inv, "王小明"), strings=["王小明"])
    dst = apply(src, tmp_path, settings, plan)
    assert "王小明" not in entries(dst)["xl/drawings/vmlDrawing1.vml"].decode("utf-8")
    assert ad.residue(dst, ["王小明"]) == []
