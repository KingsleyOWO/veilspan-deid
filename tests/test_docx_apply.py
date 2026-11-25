import io
import zipfile
from xml.dom import minidom

import pytest
from PIL import Image

from deid.formats.docx import DocxAdapter
from deid.imaging import is_black
from deid.model import MaskPlan
from tests.builders import make_docx, text_image


def spans(inv, needle, *, which=None):
    """MaskPlan.text entries for every occurrence of needle (or only the which-th unit that contains it)."""
    out, n = {}, 0
    for t in inv.texts:
        i = t.text.find(needle)
        if i < 0:
            continue
        if which is None or which == n:
            out[t.id] = [(i, i + len(needle))]
        n += 1
    return out


def entries(path):
    with zipfile.ZipFile(path) as z:
        return {i.filename: z.read(i) for i in z.infolist()}, [i.filename for i in z.infolist()]


def all_text(path):
    return [t.text for t in DocxAdapter().parse(path).texts]


@pytest.fixture
def rich(tmp_path):
    img = text_image(["登入者：王小明"])
    jpg = io.BytesIO()
    exif = Image.Exif()
    exif[0x013B] = "Photographer"
    Image.new("RGB", (120, 80), "white").save(jpg, "JPEG", exif=exif)
    return make_docx(tmp_path / "a.docx", ["會議紀錄", ["承辦：王", "小", "明 先生"], "A&B 公司的 <王小明>"],
                     table=[["承辦人", "王小明", "分機 2301"]], header="頁首 王小明", footnote="註腳 王小明",
                     comment=("審閱者甲", "註解提到王小明"), deleted="被刪除的王小明", deleted_author="修訂者乙",
                     images=[("image1.png", img), ("image2.jpg", jpg.getvalue())], image_alt="王小明的截圖",
                     creator="林志豪", last_modified_by="陳美玲", title="王小明的報告",
                     hyperlink=("寄信給 a@example.com", "mailto:a@example.com"), thumbnail=img,
                     embedded_xlsx_strings=["王小明", "其他"], textbox="方塊裡的王小明",
                     field_instr=' HYPERLINK "mailto:a@example.com" ')


def apply(src, tmp_path, settings, plan):
    dst = tmp_path / "out.docx"
    DocxAdapter().apply(src, dst, plan, settings)
    return dst


def test_name_split_across_runs_is_masked_and_everything_else_is_byte_identical(rich, tmp_path, settings):
    inv = DocxAdapter().parse(rich)
    dst = apply(rich, tmp_path, settings, MaskPlan(text=spans(inv, "王小明", which=0)))
    assert "承辦：■■■ 先生" in all_text(dst)
    before, order_before = entries(rich)
    after, order_after = entries(dst)
    assert [n for n in order_before if not n.startswith("docProps/thumbnail")] == order_after
    changed = [n for n in order_after if before[n] != after[n]]
    assert sorted(changed) == ["_rels/.rels", "word/document.xml", "word/media/image2.jpg"]
    doc = after["word/document.xml"].decode("utf-8")
    assert "<w:t>■</w:t>" in doc and "承辦：■" in doc


def test_only_the_requested_occurrence_changes(rich, tmp_path, settings):
    inv = DocxAdapter().parse(rich)
    dst = apply(rich, tmp_path, settings, MaskPlan(text=spans(inv, "王小明", which=0)))
    texts = all_text(dst)
    assert "承辦人 | 王小明 | 分機 2301" in texts and "頁首 王小明" in texts


def test_all_hidden_places_are_masked_and_no_residue_remains(rich, tmp_path, settings):
    inv = DocxAdapter().parse(rich)
    plan = MaskPlan(text={**spans(inv, "王小明"), **spans(inv, "a@example.com")}, strings=["王小明", "a@example.com"])
    dst = apply(rich, tmp_path, settings, plan)
    joined = "\n".join(all_text(dst))
    assert "王小明" not in joined and "a@example.com" not in joined
    assert "頁首 ■■■" in joined and "註腳 ■■■" in joined and "註解提到■■■" in joined and "被刪除的■■■" in joined
    assert "方塊裡的■■■" in joined and "A&B 公司的 <■■■>" in joined
    assert DocxAdapter().residue(dst, plan.strings) == []
    after, _ = entries(dst)
    rels = after["word/_rels/document.xml.rels"].decode("utf-8")
    assert "mailto:a@example.com" not in rels and 'Target="about:blank"' in rels
    doc = after["word/document.xml"].decode("utf-8")
    assert "王小明的截圖" not in doc and 'descr="■■■的截圖"' in doc
    assert "mailto:*************" in doc
    inner = zipfile.ZipFile(io.BytesIO(after["word/embeddings/Workbook1.xlsx"]))
    shared = inner.read("xl/sharedStrings.xml").decode("utf-8")
    assert "王小明" not in shared and "<t>■■■</t>" in shared and "<t>其他</t>" in shared
    assert inner.namelist()[0] == "[Content_Types].xml"


def test_table_widths_survive_when_a_masked_string_equals_an_attribute_value(rich, tmp_path, settings):
    inv = DocxAdapter().parse(rich)
    plan = MaskPlan(text=spans(inv, "2301"), strings=["2301"])
    dst = apply(rich, tmp_path, settings, plan)
    after, _ = entries(dst)
    doc = after["word/document.xml"].decode("utf-8")
    assert 'w:w="2301"' in doc and "分機 ****" in "\n".join(all_text(dst))
    inner = zipfile.ZipFile(io.BytesIO(after["word/embeddings/Workbook1.xlsx"]))
    assert "<v>2301</v>" in inner.read("xl/worksheets/sheet1.xml").decode("utf-8")


def test_every_xml_part_of_the_output_is_wellformed(rich, tmp_path, settings):
    inv = DocxAdapter().parse(rich)
    plan = MaskPlan(text={**spans(inv, "王小明"), **spans(inv, "A&B")}, strings=["王小明", "A&B"],
                    meta={m.id: [(0, len(m.value))] for m in inv.metas})
    dst = apply(rich, tmp_path, settings, plan)
    with zipfile.ZipFile(dst) as z:
        assert z.testzip() is None
        for n in z.namelist():
            if n.endswith((".xml", ".rels")):
                minidom.parseString(z.read(n))
    assert "*** 公司的 <■■■>" in all_text(dst)


def test_metadata_is_cleared_and_authors_are_neutralised(rich, tmp_path, settings):
    inv = DocxAdapter().parse(rich)
    wanted = {m.id: [(0, len(m.value))] for m in inv.metas if m.person}
    title = next(m for m in inv.metas if m.label == "標題")
    wanted[title.id] = [(0, 3)]
    dst = apply(rich, tmp_path, settings, MaskPlan(meta=wanted))
    metas = {(m.label, m.value) for m in DocxAdapter().parse(dst).metas}
    assert not any(label in ("作者", "最後修改者") for label, _ in metas)
    assert ("標題", "■■■的報告") in metas
    assert {v for label, v in metas if label == "註解／修訂作者"} == {"－"}
    after, _ = entries(dst)
    assert b"<dc:creator></dc:creator>" in after["docProps/core.xml"]
    comments = after["word/comments.xml"].decode("utf-8")
    assert "審閱者甲" not in comments and 'w:initials=""' in comments
    assert "修訂者乙" not in after["word/document.xml"].decode("utf-8")


def test_thumbnail_is_removed_and_package_relationships_stay_valid(rich, tmp_path, settings):
    dst = apply(rich, tmp_path, settings, MaskPlan())
    after, order = entries(dst)
    assert not any(n.startswith("docProps/thumbnail") for n in order)
    rels = after["_rels/.rels"].decode("utf-8")
    minidom.parseString(rels)
    assert "thumbnail" not in rels and "officeDocument" in rels


def test_image_boxes_are_painted_and_exif_is_stripped_from_other_jpegs(rich, tmp_path, settings):
    inv = DocxAdapter().parse(rich)
    png = next(i for i in inv.images if i.name.endswith("image1.png"))
    dst = apply(rich, tmp_path, settings, MaskPlan(boxes={png.id: [(100, 20, 400, 90)]}))
    after, _ = entries(dst)
    assert is_black(after["word/media/image1.png"], (100, 20, 400, 90))
    assert Image.open(io.BytesIO(after["word/media/image1.png"])).size == (1000, 300)
    assert not Image.open(io.BytesIO(after["word/media/image2.jpg"])).getexif()


def test_residue_reports_strings_left_in_parts_the_adapter_does_not_edit(tmp_path, settings):
    src = make_docx(tmp_path / "c.docx", ["內文 王小明"])
    with zipfile.ZipFile(src, "a") as z:  # a part no rule knows about (custom XML items are edited since the review fixes)
        z.writestr("word/vendorExtension.xml", '<?xml version="1.0"?><ext xmlns="urn:example:vendor"><owner>擁有者 王小明</owner></ext>')
    inv = DocxAdapter().parse(src)
    plan = MaskPlan(text=spans(inv, "王小明"), strings=["王小明"])
    dst = apply(src, tmp_path, settings, plan)
    left = DocxAdapter().residue(dst, plan.strings)
    assert left and all("word/vendorExtension.xml" in r for r in left) and all("擁有者" not in r for r in left)


def test_custom_xml_items_are_masked_like_other_hidden_text(tmp_path, settings):
    src = make_docx(tmp_path / "c2.docx", ["內文 王小明"], custom_xml_text="擁有者 王小明")
    inv = DocxAdapter().parse(src)
    plan = MaskPlan(text=spans(inv, "王小明"), strings=["王小明"])
    dst = apply(src, tmp_path, settings, plan)
    assert DocxAdapter().residue(dst, plan.strings) == []
    assert "擁有者 ■■■" in entries(dst)[0]["customXml/item1.xml"].decode("utf-8")


def test_residue_sees_strings_split_across_runs(tmp_path):
    src = make_docx(tmp_path / "d.docx", [["王", "小", "明"]])
    assert any("word/document.xml" in r for r in DocxAdapter().residue(src, ["王小明"]))
    assert DocxAdapter().residue(src, ["陳美玲"]) == []


def test_overlapping_and_adjacent_spans_in_one_unit(tmp_path, settings):
    src = make_docx(tmp_path / "e.docx", ["王小明0912345678"])
    inv = DocxAdapter().parse(src)
    dst = apply(src, tmp_path, settings, MaskPlan(text={inv.texts[0].id: [(0, 3), (3, 13)]}))
    assert all_text(dst) == ["■■■**********"]


def test_people_part_is_emptied_when_an_author_is_masked(tmp_path, settings):
    src = make_docx(tmp_path / "f.docx", ["內文"], comment=("審閱者甲", "註解"))
    people = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w15:people xmlns:w15="http://schemas.microsoft.com/'
              'office/word/2012/wordml"><w15:person w15:author="審閱者甲"><w15:presenceInfo w15:providerId="AD" '
              'w15:userId="a@example.com"/></w15:person></w15:people>')
    with zipfile.ZipFile(src, "a") as z:
        z.writestr("word/people.xml", people)
    inv = DocxAdapter().parse(src)
    author = next(m for m in inv.metas if m.label == "註解／修訂作者")
    dst = apply(src, tmp_path, settings, MaskPlan(meta={author.id: [(0, len(author.value))]}))
    after, _ = entries(dst)
    out = after["word/people.xml"].decode("utf-8")
    minidom.parseString(out)
    assert "審閱者甲" not in out and "a@example.com" not in out and "w15:people" in out
