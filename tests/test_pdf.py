import io

import numpy as np
import pymupdf
import pytest
from PIL import Image

from deid.formats.base import BrokenFile, adapter_for
from deid.formats.pdf import PdfAdapter
from deid.imaging import is_black
from deid.model import MaskPlan, OcrLine
from tests.builders import make_pdf, text_image

ZOOM = 200 / 72


def spans(inv, needle):
    out = {}
    for t in inv.texts:
        i = t.text.find(needle)
        if i >= 0:
            out[t.id] = [(i, i + len(needle))]
    return out


def page_text(path, n=0):
    with pymupdf.open(path) as doc:
        return doc[n].get_text()


def render(path, n=0, dpi=200):
    with pymupdf.open(path) as doc:
        return doc[n].get_pixmap(dpi=dpi).tobytes("png")


@pytest.fixture
def text_pdf(tmp_path):
    return make_pdf(tmp_path / "a.pdf", [{"lines": ["第一行不相關的內容", "聯絡人 王小明 先生 電話 0912-345-678", "第三行也不相關"],
                                         "annot": "註解提到王小明"}], author="林志豪", title="測試文件")


def test_adapter_is_chosen_by_extension(tmp_path):
    assert isinstance(adapter_for(tmp_path / "x.PDF"), PdfAdapter)


def test_text_blocks_pages_and_metadata_become_units(text_pdf):
    inv = PdfAdapter().parse(text_pdf)
    assert inv.kind == "pdf"
    body = [t for t in inv.texts if t.section == "第 1 頁"]
    assert any("聯絡人 王小明 先生" in t.text for t in body) and all(t.page == 0 for t in body)
    assert [t.text for t in inv.texts if t.section == "註解"] == ["註解提到王小明"]
    assert {(m.label, m.value, m.person) for m in inv.metas} == {("作者", "林志豪", True), ("標題", "測試文件", False)}
    [pg] = inv.images
    assert (pg.id, pg.page, pg.supported, pg.needs_scan, pg.areas) == ("pg1", 0, True, False, [])
    assert abs(pg.width - 595 * ZOOM) <= 1 and abs(pg.height - 842 * ZOOM) <= 1


def test_page_image_is_a_png_of_the_declared_size(text_pdf):
    ad = PdfAdapter()
    inv = ad.parse(text_pdf)
    im = Image.open(io.BytesIO(ad.image_bytes(text_pdf, "pg1")))
    assert im.format == "PNG" and im.size == (inv.images[0].width, inv.images[0].height)


def test_only_the_matched_words_are_removed(text_pdf, tmp_path, settings):
    ad = PdfAdapter()
    inv = ad.parse(text_pdf)
    plan = MaskPlan(text={**spans(inv, "王小明"), **{k: v for k, v in spans(inv, "0912-345-678").items()}},
                    strings=["王小明", "0912-345-678"], meta={m.id: [(0, len(m.value))] for m in inv.metas if m.person})
    body = next(t for t in inv.texts if "聯絡人" in t.text)
    plan.text[body.id] = [(body.text.index("王小明"), body.text.index("王小明") + 3),
                          (body.text.index("0912"), body.text.index("0912") + 12)]
    dst = tmp_path / "out.pdf"
    ad.apply(text_pdf, dst, plan, settings)
    text = page_text(dst)
    assert "王小明" not in text and "0912-345-678" not in text
    assert "聯絡人" in text and "先生" in text and "電話" in text
    assert "第一行不相關的內容" in text and "第三行也不相關" in text
    assert ad.residue(dst, plan.strings) == []
    assert ad.verify(text_pdf, dst, plan, settings) == []
    with pymupdf.open(dst) as doc:
        assert doc.metadata["author"] == "" and doc.metadata["title"] == "測試文件"
        assert not list(doc[0].annots())
    assert b"\xe6\x9e\x97\xe5\xbf\x97\xe8\xb1\xaa" not in dst.read_bytes()


def test_masked_area_is_painted_black_where_the_text_was(text_pdf, tmp_path, settings):
    ad = PdfAdapter()
    inv = ad.parse(text_pdf)
    body = next(t for t in inv.texts if "聯絡人" in t.text)
    s = body.text.index("王小明")
    [(page_id, box)] = ad.locate(text_pdf, body.id, s, s + 3)
    assert page_id == "pg1"
    dst = tmp_path / "out.pdf"
    ad.apply(text_pdf, dst, MaskPlan(text={body.id: [(s, s + 3)]}), settings)
    out = render(dst)
    assert is_black(out, box)
    assert not is_black(render(text_pdf), box)


def test_name_broken_across_two_lines_is_joined_and_both_parts_removed(tmp_path, settings):
    src = make_pdf(tmp_path / "wrap.pdf", [{"lines": ["本案的承辦人員為王小", "明先生負責後續聯繫"], "leading": 17}])
    ad = PdfAdapter()
    inv = ad.parse(src)
    unit = next(t for t in inv.texts if "王小明" in t.text)
    s = unit.text.index("王小明")
    boxes = ad.locate(src, unit.id, s, s + 3)
    assert len(boxes) == 2 and boxes[0][1][1] < boxes[1][1][1]
    dst = tmp_path / "out.pdf"
    ad.apply(src, dst, MaskPlan(text={unit.id: [(s, s + 3)]}, strings=["王小明"]), settings)
    text = page_text(dst)
    assert "王小" not in text and "明先生" not in text and "承辦人員為" in text and "先生負責後續聯繫" in text
    assert ad.residue(dst, ["王小明"]) == []


def test_latin_lines_are_joined_with_a_space(tmp_path):
    src = make_pdf(tmp_path / "en.pdf", [{"lines": ["Contact person: John", "Smith, phone 555"], "leading": 17}])
    assert any("John Smith" in t.text for t in PdfAdapter().parse(src).texts)


def test_box_on_an_image_page_blacks_out_the_pixels(tmp_path, settings):
    src = make_pdf(tmp_path / "scan.pdf", [{"image": text_image(["掃描頁 王小明 的資料"], size=(1190, 1684))}])
    ad = PdfAdapter()
    inv = ad.parse(src)
    [pg] = inv.images
    assert pg.needs_scan and len(pg.areas) == 1 and pg.areas[0][2] - pg.areas[0][0] > 1500
    box = (200.0, 60.0, 700.0, 160.0)
    dst = tmp_path / "out.pdf"
    plan = MaskPlan(boxes={"pg1": [box]})
    ad.apply(src, dst, plan, settings)
    out = render(dst)
    assert is_black(out, box) and not is_black(out, (200, 400, 700, 500))
    assert ad.verify(src, dst, plan, settings) == []
    with pymupdf.open(dst) as doc:  # the pixels are gone from the stored image, not just covered
        xref = doc[0].get_images()[0][0]
        stored = Image.open(io.BytesIO(doc.extract_image(xref)["image"])).convert("L")
        sx = stored.width / pg.width
        region = np.asarray(stored.crop((int(260 * sx), int(80 * sx), int(640 * sx), int(140 * sx))))
        assert region.min() == region.max()  # no glyph pixels left under the box
    with pymupdf.open(src) as doc:
        before = Image.open(io.BytesIO(doc.extract_image(doc[0].get_images()[0][0])["image"])).convert("L")
        assert np.asarray(before.crop((int(260 * sx), int(80 * sx), int(640 * sx), int(140 * sx)))).min() < 100


def test_box_on_a_rotated_page_lands_where_it_was_drawn(tmp_path, settings):
    src = make_pdf(tmp_path / "rot.pdf", [{"lines": ["旋轉頁面上的王小明"], "image": text_image(["x"], size=(600, 300)),
                                          "image_rect": (50, 400, 350, 550), "rotate": 90}])
    ad = PdfAdapter()
    inv = ad.parse(src)
    [pg] = inv.images
    assert pg.width > pg.height  # the page is shown rotated
    box = (300.0, 200.0, 700.0, 420.0)
    dst = tmp_path / "out.pdf"
    ad.apply(src, dst, MaskPlan(boxes={"pg1": [box]}), settings)
    assert is_black(render(dst), box)
    unit = next(t for t in inv.texts if "王小明" in t.text)
    s = unit.text.index("王小明")
    [(_, tbox)] = ad.locate(src, unit.id, s, s + 3)
    dst2 = tmp_path / "out2.pdf"
    ad.apply(src, dst2, MaskPlan(text={unit.id: [(s, s + 3)]}), settings)
    assert is_black(render(dst2), tbox) and "王小明" not in page_text(dst2)
    area = pg.areas[0]
    assert area[3] - area[1] > area[2] - area[0]  # the wide image appears tall once the page is rotated


def test_ocr_lines_that_duplicate_the_text_layer_are_dropped(tmp_path):
    src = make_pdf(tmp_path / "ocrpdf.pdf", [{"image": text_image(["x"], size=(1190, 1684)), "hidden": ["隱形文字層 王小明"]}])
    ad = PdfAdapter()
    inv = ad.parse(src)
    unit = next(t for t in inv.texts if "王小明" in t.text)
    [(_, box)] = ad.locate(src, unit.id, 0, len(unit.text))
    dup = OcrLine("隱形文字層王小明", box, [box] * 8)
    other = OcrLine("只在影像裡", (100.0, 1500.0, 500.0, 1560.0), [(100.0, 1500.0, 500.0, 1560.0)] * 5)
    assert ad.filter_ocr(src, "pg1", [dup, other]) == [other]


def test_hidden_text_layer_is_removed_together_with_the_pixels(tmp_path, settings):
    src = make_pdf(tmp_path / "ocrpdf.pdf", [{"image": text_image(["x"], size=(1190, 1684)), "hidden": ["隱形文字層 王小明"]}])
    ad = PdfAdapter()
    inv = ad.parse(src)
    unit = next(t for t in inv.texts if "王小明" in t.text)
    s = unit.text.index("王小明")
    dst = tmp_path / "out.pdf"
    ad.apply(src, dst, MaskPlan(text={unit.id: [(s, s + 3)]}, strings=["王小明"]), settings)
    assert "王小明" not in page_text(dst) and "隱形文字層" in page_text(dst)
    [(_, box)] = ad.locate(src, unit.id, s, s + 3)
    assert is_black(render(dst), box)


def test_bookmarks_and_links_are_cleaned(tmp_path, settings):
    src = tmp_path / "toc.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "contact", fontsize=14)
    page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 80, 200, 110), "uri": "mailto:wang@example.com"})
    doc.set_toc([[1, "王小明的章節", 1]])
    doc.save(src)
    doc.close()
    ad = PdfAdapter()
    inv = ad.parse(src)
    mark = next(t for t in inv.texts if t.section == "書籤")
    assert mark.text == "王小明的章節"
    dst = tmp_path / "out.pdf"
    ad.apply(src, dst, MaskPlan(text={mark.id: [(0, 3)]}, strings=["王小明", "wang@example.com"]), settings)
    with pymupdf.open(dst) as out:
        assert out.get_toc()[0][1] == "■■■的章節" and out[0].get_links() == []
    assert ad.residue(dst, ["王小明", "wang@example.com"]) == []


def test_residue_reports_page_and_string_without_context(text_pdf):
    left = PdfAdapter().residue(text_pdf, ["王小明", "不存在"])
    assert left and all("王小明" in r for r in left) and any("第 1 頁" in r for r in left)
    assert not any("聯絡人" in r for r in left)


def test_password_protected_pdf_is_reported(tmp_path):
    src = make_pdf(tmp_path / "locked.pdf", [{"lines": ["x"]}], password="pw")
    with pytest.raises(BrokenFile) as e:
        PdfAdapter().parse(src)
    assert "密碼" in str(e.value)


def test_non_pdf_content_is_reported(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf at all")
    with pytest.raises(BrokenFile):
        PdfAdapter().parse(bad)


def test_embedded_files_are_listed_as_unchecked(tmp_path):
    src = tmp_path / "emb.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.embfile_add("附件.txt", b"hello")
    doc.save(src)
    doc.close()
    assert any("附件.txt" in u.name for u in PdfAdapter().parse(src).unchecked)


def test_parsing_twice_gives_the_same_ids(text_pdf):
    assert PdfAdapter().parse(text_pdf) == PdfAdapter().parse(text_pdf)


def test_view_lists_pages_then_loose_text(text_pdf):
    ad = PdfAdapter()
    inv = ad.parse(text_pdf)
    view = ad.view(text_pdf, inv)
    assert view[0] == {"type": "page", "image": "pg1", "page": 1}
    note = next(t for t in inv.texts if t.section == "註解")
    assert {"type": "text", "unit": note.id, "section": "註解"} in view
