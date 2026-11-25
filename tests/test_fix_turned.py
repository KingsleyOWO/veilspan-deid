"""Text that does not run left to right on its page - a table heading turned a quarter, a slanted stamp line -
is taken out whole (review of 2026-11-23). The redaction box is pulled in across the line, not along it: pulled
in along a turned line it let the first and the last character stay in the file under the black box."""
import pymupdf
import pytest

from deid.findings import FindingStore
from deid.formats.base import adapter_for

ID = "A123456789"


def turned_pdf(tmp_path, **how):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 80), "地址欄位如右", fontsize=14, fontname="china-t")
    page.insert_text((260, 600), ID, fontsize=14, fontname="helv", **how)
    path = tmp_path / "turned.pdf"
    doc.save(path)
    doc.close()
    return path


@pytest.mark.parametrize("how", [{"rotate": 90}, {"rotate": 270}, {"rotate": 180}, {"rotate": 0},
                                 {"morph": (pymupdf.Point(260, 600), pymupdf.Matrix(30))},
                                 {"morph": (pymupdf.Point(260, 600), pymupdf.Matrix(-75))}],
                         ids=["quarter turn", "three quarters", "upside down", "upright", "slanted", "steep"])
def test_no_character_of_a_masked_string_stays_in_the_text_layer(tmp_path, settings, how):
    src = turned_pdf(tmp_path, **how)
    ad = adapter_for(src, settings)
    store = FindingStore(ad.parse(src), lambda u, s, e: ad.locate(src, u, s, e))
    assert store.add_text(ID, "身分證字號", "user") is not None
    plan, dst = store.plan(), tmp_path / "out.pdf"
    ad.apply(src, dst, plan, settings)
    with pymupdf.open(dst) as doc:
        left = doc[0].get_text()
    assert not set(left) & set(ID) and "地址欄位如右" in left
    assert ad.residue(dst, plan.strings) == [] and ad.verify(src, dst, plan, settings) == []


def test_two_turned_lines_set_close_keep_the_one_that_was_not_masked(tmp_path, settings):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((260, 600), ID, fontsize=14, fontname="helv", rotate=90)
    # the next column, at a line spacing of 1.3 - what upright lines are set at without losing their neighbours
    page.insert_text((278, 600), "B987654321", fontsize=14, fontname="helv", rotate=90)
    src = tmp_path / "columns.pdf"
    doc.save(src)
    doc.close()
    ad = adapter_for(src, settings)
    store = FindingStore(ad.parse(src), lambda u, s, e: ad.locate(src, u, s, e))
    store.add_text(ID, "身分證字號", "user")
    plan, dst = store.plan(), tmp_path / "out.pdf"
    ad.apply(src, dst, plan, settings)
    with pymupdf.open(dst) as out:
        left = out[0].get_text()
    assert "B987654321" in left and "A1" not in left and ad.verify(src, dst, plan, settings) == []


# ---- a page that is cropped and turned ----------------------------------------------------------------------

CROPS = [(100, 200, 500, 700), (0, 0, 595, 700), (100, 0, 595, 842), (40, 70, 560, 790)]


def cropped_turned_pdf(tmp_path, crop, rotation):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((150, 300), "簽收人 王大明", fontsize=14, fontname="china-t")
    page.insert_text((150, 400), "其他內容不動", fontsize=14, fontname="china-t")
    page.set_cropbox(pymupdf.Rect(*crop))
    page.set_rotation(rotation)
    path = tmp_path / "cropped.pdf"
    doc.save(path)
    doc.close()
    return path


def picture(ad, path) -> bytes:
    with pymupdf.open(path) as doc:
        return doc[0].get_pixmap(matrix=pymupdf.Matrix(ad.zoom, ad.zoom), alpha=False).tobytes("png")


@pytest.mark.parametrize("crop", CROPS)
@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_the_black_box_is_where_the_text_was_on_a_page_that_is_cropped_and_turned(tmp_path, settings, crop, rotation):
    from deid import imaging
    src = cropped_turned_pdf(tmp_path, crop, rotation)
    ad = adapter_for(src, settings)
    store = FindingStore(ad.parse(src), lambda u, s, e: ad.locate(src, u, s, e))
    found = store.add_text("王大明", "姓名", "user")
    (_image, box), = found.occurrences[0].page_boxes  # where the review screen shows it
    plan, dst = store.plan(), tmp_path / "out.pdf"
    ad.apply(src, dst, plan, settings)
    after = picture(ad, dst)
    assert imaging.is_black(after, box)
    assert ad.verify(src, dst, plan, settings) == [] and ad.residue(dst, plan.strings) == []
    with pymupdf.open(dst) as doc:
        assert doc[0].rotation == rotation and "王大明" not in doc[0].get_text() and "其他內容不動" in doc[0].get_text()
    # nothing else on the page went black: the picture differs from the original only around the box
    import io
    from PIL import Image, ImageChops
    diff = ImageChops.difference(Image.open(io.BytesIO(picture(ad, src))).convert("L"), Image.open(io.BytesIO(after)).convert("L")).getbbox()
    assert diff is not None and diff[0] >= box[0] - 3 and diff[1] >= box[1] - 3 and diff[2] <= box[2] + 3 and diff[3] <= box[3] + 3


@pytest.mark.parametrize("rotation", [90, 270])
def test_a_box_drawn_on_the_picture_of_a_cropped_and_turned_page_is_black_there(tmp_path, settings, rotation):
    from deid import imaging
    src = cropped_turned_pdf(tmp_path, (100, 200, 500, 700), rotation)
    ad = adapter_for(src, settings)
    store = FindingStore(ad.parse(src), lambda u, s, e: ad.locate(src, u, s, e))
    box = (300.0, 250.0, 520.0, 380.0)  # in the pixels of the page picture, as the review screen sends it
    store.add_region("pg1", box, "其他", "user")
    plan, dst = store.plan(), tmp_path / "out.pdf"
    ad.apply(src, dst, plan, settings)
    assert imaging.is_black(picture(ad, dst), box) and ad.verify(src, dst, plan, settings) == []
