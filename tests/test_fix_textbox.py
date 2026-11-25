"""The second copy of a text box (mc:Fallback, what an older Word shows) follows the copy the reviewer sees, by
where it stands in the file and not by having the same text (review of 2026-11-23): in a table cell the visible
copy is part of a row, and two text boxes can say the same thing. All names are made up."""
import re
import zipfile

from deid.findings import FindingStore
from deid.formats.base import adapter_for
from deid.model import inventory_from_json, to_json
from tests.builders import make_docx

NAME = "王小明"
BOX = re.compile(r"<w:p>(?:(?!<w:p>).)*?<mc:AlternateContent>.*?</mc:AlternateContent>.*?</w:p>", re.S)


def rewritten(path, change):
    """The same document with its body XML passed through `change`."""
    with zipfile.ZipFile(path) as z:
        parts = {i.filename: z.read(i) for i in z.infolist()}
    xml = parts["word/document.xml"].decode("utf-8")
    assert len(BOX.findall(xml)) == 1
    parts["word/document.xml"] = change(xml).encode("utf-8")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)
    return path


def in_a_table_cell(xml: str) -> str:
    cell = '<w:tc><w:p><w:r><w:t>承辦人</w:t></w:r></w:p></w:tc>'
    return BOX.sub(lambda m: f"<w:tbl><w:tr>{cell}<w:tc>{m.group(0)}</w:tc></w:tr></w:tbl>", xml)


def twice(xml: str) -> str:
    return BOX.sub(lambda m: m.group(0) + m.group(0), xml)


def copies(path) -> list[tuple[list[str], list[str]]]:
    """(what the visible copy says, what the second copy says) for each text box of the output."""
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    runs = lambda part: re.findall(r"<w:t[^>]*>([^<]*)</w:t>", part)  # noqa: E731
    return [(runs(c), runs(f)) for c, f in re.findall(r"<mc:Choice\b.*?>(.*?)</mc:Choice><mc:Fallback>(.*?)</mc:Fallback>", xml, re.S)]


def one_place(src, tmp_path, settings, nth=0, strip_links=False):
    ad = adapter_for(src, settings)
    inv = ad.parse(src)
    if strip_links:  # an inventory saved before the copies were linked by position
        saved = to_json(inv)
        for t in saved["texts"]:
            t.pop("copies", None)
        inv = inventory_from_json(saved)
    store = FindingStore(inv, lambda u, s, e: ad.locate(src, u, s, e))
    visible = [t for t in inv.texts if not t.mirror and "方塊" in t.text][nth]
    at = visible.text.index(NAME)
    finding = store.add_text(NAME, "姓名", "user", only=(visible.id, at, at + len(NAME)))
    plan, dst = store.plan(), tmp_path / "out.docx"
    ad.apply(src, dst, plan, settings)
    return ad, inv, finding, plan, dst


def test_one_place_in_a_text_box_inside_a_table_cell_takes_the_second_copy_along(tmp_path, settings):
    src = rewritten(make_docx(tmp_path / "a.docx", [f"另見 {NAME}"], textbox=f"方塊 {NAME}"), in_a_table_cell)
    ad, inv, finding, plan, dst = one_place(src, tmp_path, settings)
    mirrors = {t.id for t in inv.texts if t.mirror}
    assert [o.unit in mirrors for o in finding.occurrences] == [False, True]
    assert copies(dst) == [(["方塊 ■■■"], ["方塊 ■■■"])]
    assert ad.verify(src, dst, plan, settings) == []


def test_one_place_in_one_of_two_text_boxes_that_say_the_same_leaves_the_other_box_alone(tmp_path, settings):
    src = rewritten(make_docx(tmp_path / "a.docx", ["內文"], textbox=f"方塊 {NAME}"), twice)
    ad, _inv, finding, plan, dst = one_place(src, tmp_path, settings, nth=1)
    assert len(finding.occurrences) == 2
    assert copies(dst) == [(["方塊 王小明"], ["方塊 王小明"]), (["方塊 ■■■"], ["方塊 ■■■"])]
    assert ad.verify(src, dst, plan, settings) == []


def test_one_place_in_a_text_box_outside_a_table_still_takes_the_second_copy_along(tmp_path, settings):
    src = make_docx(tmp_path / "a.docx", [f"另見 {NAME}"], textbox=f"方塊 {NAME}")
    ad, _inv, _finding, plan, dst = one_place(src, tmp_path, settings)
    assert copies(dst) == [(["方塊 ■■■"], ["方塊 ■■■"])]
    assert ad.verify(src, dst, plan, settings) == []


def test_an_inventory_saved_without_the_links_still_pairs_copies_that_say_the_same(tmp_path, settings):
    src = make_docx(tmp_path / "a.docx", [f"另見 {NAME}"], textbox=f"方塊 {NAME}")
    _ad, _inv, finding, _plan, dst = one_place(src, tmp_path, settings, strip_links=True)
    assert len(finding.occurrences) == 2 and copies(dst) == [(["方塊 ■■■"], ["方塊 ■■■"])]
