import pytest

from deid.formats.base import BrokenFile, UnsupportedFormat, adapter_for
from deid.formats.docx import DocxAdapter
from tests.builders import make_docx, text_image


@pytest.fixture
def doc(tmp_path):
    img = text_image(["登入者：王小明"])
    return make_docx(tmp_path / "a.docx", ["會議紀錄", ["承辦：王", "小", "明 先生"]],
                     table=[["承辦人", "王小明", "分機 2301"], ["窗口", "陳美玲", ""]],
                     header="頁首 王小明", footer="頁尾文字", footnote="註腳 陳美玲", comment=("審閱者甲", "註解提到王小明"),
                     deleted="被刪除的林先生", deleted_author="修訂者乙", images=[("image1.png", img)],
                     creator="林志豪", last_modified_by="陳美玲", manager="張主管", company="範例公司", title="測試文件",
                     textbox="方塊裡的王小明", ole_bin=b"\xd0\xcf\x11\xe0junk", extra_media=[("image2.emf", b"\x01\x00\x00\x00junk")])


def texts(inv, section=None):
    return [t.text for t in inv.texts if (section is None or t.section == section) and not t.mirror]


def test_adapter_is_chosen_by_extension(tmp_path):
    assert isinstance(adapter_for(tmp_path / "x.DOCX"), DocxAdapter)
    with pytest.raises(UnsupportedFormat):
        adapter_for(tmp_path / "x.pptx")


def test_body_paragraphs_join_runs_into_continuous_text(doc):
    inv = DocxAdapter().parse(doc)
    assert inv.kind == "docx"
    body = texts(inv, "內文")
    assert body[0] == "會議紀錄"
    assert "承辦：王小明 先生" in body


def test_table_row_becomes_one_unit_with_cells_joined(doc):
    body = texts(DocxAdapter().parse(doc), "內文")
    assert "承辦人 | 王小明 | 分機 2301" in body
    assert "窗口 | 陳美玲" in body


def test_hidden_places_are_units_with_their_own_sections(doc):
    inv = DocxAdapter().parse(doc)
    assert texts(inv, "頁首") == ["頁首 王小明"]
    assert texts(inv, "頁尾") == ["頁尾文字"]
    assert texts(inv, "註腳") == ["註腳 陳美玲"]
    assert texts(inv, "註解") == ["註解提到王小明"]
    assert "被刪除的林先生" in texts(inv, "內文")


def test_textbox_fallback_copy_is_marked_as_mirror(doc):
    inv = DocxAdapter().parse(doc)
    copies = [t for t in inv.texts if t.text == "方塊裡的王小明"]
    assert [t.mirror for t in copies] == [False, True]


def test_metadata_units_cover_properties_and_revision_authors(doc):
    metas = {(m.label, m.value): m.person for m in DocxAdapter().parse(doc).metas}
    assert metas[("作者", "林志豪")] is True and metas[("最後修改者", "陳美玲")] is True and metas[("主管", "張主管")] is True
    assert metas[("公司", "範例公司")] is False and metas[("標題", "測試文件")] is False
    assert metas[("註解／修訂作者", "審閱者甲")] is True and metas[("註解／修訂作者", "修訂者乙")] is True


def test_images_are_anchored_and_unsupported_ones_are_reported(doc):
    inv = DocxAdapter().parse(doc)
    img = next(i for i in inv.images if i.name == "word/media/image1.png")
    assert img.supported and (img.width, img.height) == (1000, 300) and img.areas == [(0, 0, 1000, 300)]
    assert inv.text(img.anchor) is not None
    emf = next(i for i in inv.images if i.name.endswith(".emf"))
    assert emf.supported is False
    names = {u.name for u in inv.unchecked}
    assert "word/media/image2.emf" in names and "word/embeddings/oleObject1.bin" in names
    assert DocxAdapter().image_bytes(doc, img.id)[:4] == b"\x89PNG"


def test_parsing_twice_gives_the_same_ids(doc):
    a, b = DocxAdapter().parse(doc), DocxAdapter().parse(doc)
    assert a == b and a.texts[0].id == "t1" and a.images[0].id == "img1" and a.metas[0].id == "m1"


def test_view_lists_blocks_in_document_order_without_mirrors(doc):
    ad = DocxAdapter()
    inv = ad.parse(doc)
    view = ad.view(doc, inv)
    units = [b["unit"] for b in view if b["type"] == "text"]
    assert units == [t.id for t in inv.texts if not t.mirror]
    img = next(i for i in inv.images if i.supported)
    pos = next(i for i, b in enumerate(view) if b.get("image") == img.id)
    assert view[pos - 1] == {"type": "text", "unit": img.anchor, "section": "內文"} or view[pos - 1]["unit"] == img.anchor
    deleted = next(b for b in view if b["type"] == "text" and inv.text(b["unit"]).text == "被刪除的林先生")
    assert deleted["del"] == [[0, 7]]


def test_file_that_is_not_a_zip_is_reported_as_broken(tmp_path):
    p = tmp_path / "bad.docx"
    p.write_bytes(b"this is not a zip")
    with pytest.raises(BrokenFile):
        DocxAdapter().parse(p)


def test_old_binary_or_encrypted_file_is_reported_as_broken(tmp_path):
    p = tmp_path / "old.docx"
    p.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 100)
    with pytest.raises(BrokenFile) as e:
        DocxAdapter().parse(p)
    assert "密碼" in str(e.value)


def test_zip_without_a_document_part_is_reported_as_broken(tmp_path):
    import zipfile
    p = tmp_path / "empty.docx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("hello.txt", "x")
    with pytest.raises(BrokenFile):
        DocxAdapter().parse(p)


def test_a_word_file_whose_embedded_workbook_unpacks_too_large_is_refused(tmp_path, monkeypatch):
    import io
    import zipfile

    from deid.formats import docx as docx_module
    from deid.formats.base import BrokenFile
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/sharedStrings.xml", "<sst>" + "x" * 200_000 + "</sst>")
    src = make_docx(tmp_path / "e.docx", ["內文"])
    with zipfile.ZipFile(src, "a") as z:
        z.writestr("word/embeddings/Microsoft_Excel_Sheet1.xlsx", inner.getvalue())
    monkeypatch.setattr(docx_module, "MAX_XML_BYTES", 100_000)
    with pytest.raises(BrokenFile):
        docx_module.DocxAdapter().parse(src)
