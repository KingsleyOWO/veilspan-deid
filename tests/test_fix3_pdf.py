"""Third tier of the review of 2026-11-23, PDF: lines set close together. A redaction takes every glyph it
reaches far enough into, so the box of a masked line is pulled in across the line; where the lines stand closer
than their own height it is pulled in as far as the neighbours need, and the check asks the question the
redaction answers (is a glyph of the masked stretch still there), not whether any glyph box touches the range.
All names and numbers are made up."""
import pymupdf
import pytest

from deid.findings import FindingStore
from deid.formats.base import adapter_for

NAME = "王小明"
ABOVE, LINE, BELOW = "會議時間為下午", "聯絡人王小明先生", "會議地點在三樓"


def lines_pdf(tmp_path, leading: float, size: float = 14, **how):
    """Three lines `leading` apart; with rotate=90 they are three columns."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    for n, text in enumerate((ABOVE, LINE, BELOW)):
        at = (200 + n * leading, 600) if how.get("rotate") == 90 else (72, 200 + n * leading)
        page.insert_text(at, text, fontsize=size, fontname="china-t", **how)
    path = tmp_path / f"lines_{leading}.pdf"
    doc.save(path)
    doc.close()
    return path


def masked(src, tmp_path, settings, strings=(NAME,)):
    ad = adapter_for(src, settings)
    store = FindingStore(ad.parse(src), lambda u, s, e: ad.locate(src, u, s, e))
    for s in strings:
        assert store.add_text(s, "姓名", "user") is not None
    plan, dst = store.plan(), tmp_path / "out.pdf"
    ad.apply(src, dst, plan, settings)
    with pymupdf.open(dst) as doc:
        left = doc[0].get_text()
    return ad, plan, dst, "".join(left.split())


@pytest.mark.parametrize("leading", [14, 15, 16.8, 20])
def test_lines_set_solid_raise_no_false_alarm(tmp_path, settings, leading):
    src = lines_pdf(tmp_path, leading)
    ad, plan, dst, left = masked(src, tmp_path, settings)
    assert left == ABOVE + "聯絡人先生" + BELOW
    assert ad.residue(dst, plan.strings) == [] and ad.verify(src, dst, plan, settings) == []


@pytest.mark.parametrize("leading", [11, 12, 13])
def test_lines_set_closer_than_their_size_keep_their_neighbours(tmp_path, settings, leading):
    src = lines_pdf(tmp_path, leading)
    ad, plan, dst, left = masked(src, tmp_path, settings)
    assert left == ABOVE + "聯絡人先生" + BELOW  # the masked line lost its name and nothing else lost anything
    assert ad.residue(dst, plan.strings) == [] and ad.verify(src, dst, plan, settings) == []


@pytest.mark.parametrize("leading", [12, 14])
def test_columns_of_turned_text_set_close_keep_their_neighbours(tmp_path, settings, leading):
    src = lines_pdf(tmp_path, leading, rotate=90)
    ad, plan, dst, left = masked(src, tmp_path, settings)
    assert sorted(left) == sorted(ABOVE + "聯絡人先生" + BELOW)
    assert ad.residue(dst, plan.strings) == [] and ad.verify(src, dst, plan, settings) == []


def test_a_small_line_under_a_large_masked_one_is_kept(tmp_path, settings):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 200), "王小明", fontsize=28, fontname="china-t")
    page.insert_text((72, 209), "資訊室專員", fontsize=8, fontname="china-t")  # a caption set right under the name
    src = tmp_path / "caption.pdf"
    doc.save(src)
    doc.close()
    ad, plan, dst, left = masked(src, tmp_path, settings)
    assert left == "資訊室專員" and ad.verify(src, dst, plan, settings) == []


def test_two_masked_lines_set_close_both_go(tmp_path, settings):
    src = lines_pdf(tmp_path, 12)
    ad, plan, dst, left = masked(src, tmp_path, settings, strings=(NAME, "會議地點"))
    assert left == ABOVE + "聯絡人先生" + "在三樓"
    assert ad.residue(dst, plan.strings) == [] and ad.verify(src, dst, plan, settings) == []


def test_text_left_in_a_masked_range_is_still_reported(tmp_path, settings):
    src = lines_pdf(tmp_path, 14)
    ad = adapter_for(src, settings)
    store = FindingStore(ad.parse(src), lambda u, s, e: ad.locate(src, u, s, e))
    store.add_text(NAME, "姓名", "user")
    problems = ad.verify(src, src, store.plan(), settings)  # the "output" is the untouched file
    assert problems == ["第 1 頁的遮蔽範圍內仍有文字"]


def test_a_redaction_takes_a_glyph_once_it_reaches_past_the_outer_tenth_of_its_box():
    """What the library does, measured: the pulling in and the check both go by it. If this fails after an
    upgrade, measure again and set TAKEN in deid/formats/pdf.py to what the library now does."""
    def left(y0: float, y1: float) -> str:
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), LINE, fontsize=14, fontname="china-t")  # glyph boxes from y 86 to 102.8
        page.add_redact_annot(pymupdf.Rect(60, y0, 400, y1), fill=(0, 0, 0), cross_out=False)
        page.apply_redactions()
        return page.get_text().strip()

    assert left(70, 87.2) == LINE and left(70, 88.2) == ""  # 1.2 and 2.2 of 16.8 from above
    assert left(101.6, 120) == LINE and left(100.6, 120) == ""  # and from below


def test_the_check_decodes_a_page_once_however_many_boxes_it_holds(tmp_path, settings, monkeypatch):
    """Each masked place used to decode the whole page picture again: a page with a few hundred of them took
    most of a minute to check."""
    from deid import imaging
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    for n in range(8):
        page.insert_text((72, 100 + n * 30), f"承辦人王小明 第{n}列", fontsize=14, fontname="china-t")
    src = tmp_path / "many.pdf"
    doc.save(src)
    doc.close()
    ad, plan, dst, left = masked(src, tmp_path, settings)
    assert NAME not in left and sum(len(v) for v in plan.text.values()) == 8
    decoded = []
    real = imaging.open_upright
    monkeypatch.setattr(imaging, "open_upright", lambda data: decoded.append(1) or real(data))
    assert ad.verify(src, dst, plan, settings) == []
    assert len(decoded) == 1
    # and it still sees a box that is not black
    assert "第 1 頁的遮蔽框未塗黑" in ad.verify(src, src, plan, settings) or "第 1 頁的遮蔽範圍內仍有文字" in ad.verify(src, src, plan, settings)
