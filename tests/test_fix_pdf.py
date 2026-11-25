"""Fixes for the PDF and picture-text findings of the 2026-11-23 review. Each test failed before its fix
(the ones marked as guards passed already and pin behaviour the fixes must keep). Everything here is invented data."""
import io
import shutil
import subprocess

import pymupdf
import pytest
from PIL import Image, ImageDraw

from deid import imaging
from deid.findings import FindingStore, find_in_ocr
from deid.formats.pdf import PdfAdapter
from deid.imaging import even_split
from deid.model import MaskPlan, OcrLine
from tests.builders import cjk_font, make_pdf

ZOOM = 200 / 72


def leaks(path, needles) -> set:
    """An independent look for each needle in the finished file: every object, every decoded stream, the text
    layer without any clipping (ActualText used and ignored), and poppler's text where poppler is installed."""
    found = set()
    with pymupdf.open(path) as doc:
        blobs = []
        for x in range(1, doc.xref_length()):
            try:
                blobs.append(doc.xref_object(x, compressed=False).encode("latin-1", "replace"))
                if doc.xref_is_stream(x):
                    blobs.append(doc.xref_stream(x) or b"")
            except Exception:
                pass
        blobs.append(doc.xref_object(-1).encode("latin-1", "replace"))
        text = ""
        for page in doc:
            for flags in (0, pymupdf.TEXT_IGNORE_ACTUALTEXT):
                text += page.get_text("text", flags=flags, clip=pymupdf.INFINITE_RECT())
            for w in page.widgets() or []:
                text += f"{w.field_value} {w.choice_values}"
        text += str(doc.metadata) + (doc.get_xml_metadata() or "")
    if shutil.which("pdftotext"):
        text += subprocess.run(["pdftotext", str(path), "-"], capture_output=True, text=True, timeout=60).stdout
    for n in needles:
        forms = [n.encode("utf-8"), n.encode("utf-16-be"), n.encode("utf-16-be").hex().encode(),
                 n.encode("utf-16-be").hex().upper().encode()]
        if n in text or any(f in b for b in blobs for f in forms):
            found.add(n)
    return found


def opens(path) -> bool:
    with pymupdf.open(path) as doc:
        for page in doc:
            page.get_pixmap(dpi=30)
    if shutil.which("pdfinfo"):
        run = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True, timeout=60)
        return run.returncode == 0 and "Error" not in run.stderr
    return True


def plan_for(ad, src, strings=(), metas=(), boxes=None):
    inv = ad.parse(src)
    store = FindingStore(inv, lambda u, s, e: ad.locate(src, u, s, e))
    for s in strings:
        assert store.add_text(s, "姓名", "user") is not None, s
    for mid in metas:
        store.add_meta(mid, on=True)
    for image_id, box in boxes or []:
        store.add_region(image_id, box, "人臉", "user")
    return inv, store.plan()


# ---- P4: picture text — a short line inside the box of a slanted long line is still read --------------------

def _line(text: str, chars: list) -> OcrLine:
    return OcrLine(text=text, box=(min(c[0] for c in chars), min(c[1] for c in chars), max(c[2] for c in chars),
                                   max(c[3] for c in chars)), chars=chars)


def _slanted(text: str, x: float = 36, y: float = 225, step: float = 52, rise: float = 4.3, size: float = 48) -> OcrLine:
    """A long line photographed at a slant: left end low, right end high, one box per character."""
    return _line(text, [(x + k * step, y - k * rise, x + k * step + size, y - k * rise + size) for k in range(len(text))])


def _straight(text: str, box) -> OcrLine:
    return OcrLine(text=text, box=box, chars=even_split(text, box))


def test_short_line_in_the_empty_corner_of_a_slanted_line_is_kept():
    long_ = _slanted("本公司於十一月二十三日召開年度預算審查會議")
    short = _straight("承辦王小明", (55.0, 145.0, 244.0, 190.0))
    x0, y0, x1, y1 = long_.box
    assert x0 <= short.box[0] and short.box[2] <= x1 and y0 <= short.box[1] and short.box[3] <= y1  # inside its box
    kept = imaging._dedupe([long_, short])
    assert sorted(ln.text for ln in kept) == sorted([long_.text, short.text])


def test_short_line_with_words_the_slanted_line_also_has_is_kept_when_elsewhere():
    long_ = _slanted("會議由王小明主持並決議通過年度預算審查")
    short = _straight("王小明", (55.0, 145.0, 175.0, 190.0))  # a signature in the corner, not a second reading
    assert len(imaging._dedupe([long_, short])) == 2


def test_the_same_line_read_twice_is_still_kept_once():
    first = _straight("聯絡人王小明電話0912345678", (100.0, 500.0, 900.0, 540.0))
    again = _straight("聯絡人王小明電話0912345678", (102.0, 501.0, 901.0, 541.0))
    cut = _line("聯絡人王小明", first.chars[:6])  # the tile edge cut the line: same characters, same places
    kept = imaging._dedupe([cut, again, first])
    assert [ln.text for ln in kept] == ["聯絡人王小明電話0912345678"]


@pytest.mark.ocr
def test_ocr_reads_a_short_line_in_the_corner_of_a_slanted_line(settings):
    im = Image.new("RGB", (1250, 430), "white")
    layer = Image.new("RGBA", (1300, 70), (255, 255, 255, 0))
    ImageDraw.Draw(layer).text((0, 5), "本公司於十一月二十三日召開年度預算審查會議並決議通過", font=cjk_font(40), fill="black")
    rot = layer.rotate(4, expand=True, resample=Image.BICUBIC)
    im.paste(rot, (40, 120), rot)
    ImageDraw.Draw(im).text((60, 152), "承辦王小明", font=cjk_font(36), fill="black")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    lines = imaging.OcrEngine(settings).read(buf.getvalue())
    assert find_in_ocr(lines, "王小明")


# ---- P3: every document-information entry, and XMP anywhere in the file --------------------------------------

def _png(size, colour="red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "PNG")
    return buf.getvalue()


def _info_pdf(path):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "承辦人 王小明", fontsize=14, fontname="china-t")
    doc.set_metadata({"author": "王小明", "title": "報告", "creator": "王小明的電腦", "producer": "Example Writer 1.0"})
    ix = int(doc.xref_get_key(-1, "Info")[1].split()[0])
    doc.xref_set_key(ix, "Company", pymupdf.get_pdf_str("王小明事務所"))
    doc.xref_set_key(ix, "SourceFile", pymupdf.get_pdf_str("C:\\Users\\wang.xm\\Desktop\\王小明-履歷.docx"))
    doc.save(path)
    doc.close()
    return path


def _info(path) -> dict:
    with pymupdf.open(path) as doc:
        ix = int(doc.xref_get_key(-1, "Info")[1].split()[0])
        return {k: doc.xref_get_key(ix, k)[1] for k in doc.xref_get_keys(ix) if doc.xref_get_key(ix, k)[0] == "string"}


def test_every_document_information_entry_is_shown_for_review(tmp_path):
    src = _info_pdf(tmp_path / "info.pdf")
    metas = {m.label: (m.value, m.person) for m in PdfAdapter().parse(src).metas}
    assert metas["作者"] == ("王小明", True) and metas["標題"] == ("報告", False)
    assert metas["建立程式"] == ("王小明的電腦", False) and metas["PDF 產生程式"] == ("Example Writer 1.0", False)
    assert metas["自訂屬性：Company"] == ("王小明事務所", False)
    assert metas["自訂屬性：SourceFile"][0].endswith("王小明-履歷.docx")


def test_document_information_entries_lose_masked_names_and_switched_on_entries(tmp_path, settings):
    src = _info_pdf(tmp_path / "info.pdf")
    ad = PdfAdapter()
    on = [m.id for m in ad.parse(src).metas if m.label in ("作者", "自訂屬性：SourceFile")]
    _inv, plan = plan_for(ad, src, strings=["王小明"], metas=on)
    dst = tmp_path / "out.pdf"
    ad.apply(src, dst, plan, settings)
    info = _info(dst)
    assert "Author" not in info and "SourceFile" not in info  # switched on: emptied
    assert info["Creator"] == "■■■的電腦" and info["Company"] == "■■■事務所"  # off, but the masked name goes
    assert info["Producer"] == "Example Writer 1.0" and info["Title"] == "報告"  # nothing to mask: kept
    assert leaks(dst, ["王小明", "wang.xm"]) == set()
    assert ad.residue(dst, plan.strings) == [] and ad.verify(src, dst, plan, settings) == []
    assert opens(dst)


def test_residue_and_verify_see_custom_document_information(tmp_path, settings):
    src = _info_pdf(tmp_path / "info.pdf")
    ad = PdfAdapter()
    assert any("Company" in r for r in ad.residue(src, ["王小明事務所"]))
    company = next(m for m in ad.parse(src).metas if m.label == "自訂屬性：Company")
    plan = MaskPlan(meta={company.id: [(0, len(company.value))]})
    assert any("Company" in p for p in ad.verify(src, src, plan, settings))  # an "output" that still has it


def test_xmp_of_the_document_pages_and_pictures_does_not_reach_the_output(tmp_path, settings):
    """Guard: XMP is not shown for review, so none of it may be left anywhere in the output."""
    xmp = ('<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
           'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description rdf:about="" '
           'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator><rdf:Seq><rdf:li>林志豪</rdf:li></rdf:Seq>'
           '</dc:creator></rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>')
    src = tmp_path / "xmp.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "內容", fontname="china-t")
    page.insert_image(pymupdf.Rect(72, 200, 172, 260), stream=_png((40, 20)))
    doc.set_xml_metadata(xmp)
    for xref in (page.xref, page.get_images()[0][0]):
        meta = doc.get_new_xref()
        doc.update_object(meta, "<</Type/Metadata/Subtype/XML>>")
        doc.update_stream(meta, xmp.encode("utf-8"), compress=True)
        doc.xref_set_key(xref, "Metadata", f"{meta} 0 R")
    doc.save(src)
    doc.close()
    ad = PdfAdapter()
    assert any("XMP" in r for r in ad.residue(src, ["林志豪"]))
    dst = tmp_path / "out.pdf"
    ad.apply(src, dst, MaskPlan(), settings)
    assert leaks(dst, ["林志豪"]) == set()


# ---- P1: form data kept beside the field value ---------------------------------------------------------------

XFA_DATA = ('<xfa:datasets xmlns:xfa="http://www.xfa.org/schema/xfa-data/1.0/"><xfa:data><form1>'
            '<applicant>王小明</applicant><phone>0912345678</phone><reviewer>陳美玲</reviewer><extra>陳大文</extra>'
            '</form1></xfa:data></xfa:datasets>')


def _add_xfa(doc, data=XFA_DATA):
    x = doc.get_new_xref()
    doc.update_object(x, "<<>>")
    doc.update_stream(x, data.encode("utf-8"), compress=True)
    cat = doc.pdf_catalog()
    acro = doc.xref_get_key(cat, "AcroForm")
    if acro[0] == "xref":
        doc.xref_set_key(int(acro[1].split()[0]), "XFA", f"[(datasets) {x} 0 R]")
    else:
        doc.xref_set_key(cat, "AcroForm/XFA", f"[(datasets) {x} 0 R]")


def _widget(page, kind, name, rect, value=None, choices=None, flags=0):
    w = pymupdf.Widget()
    w.field_type, w.field_name = kind, name
    if choices:
        w.choice_values = choices
    if value is not None:
        w.field_value = value
    w.field_flags |= flags
    w.rect = pymupdf.Rect(rect)
    page.add_widget(w)


def _form_pdf(path):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 80), "申請表", fontsize=16, fontname="china-t")
    page.insert_text((72, 120), "聯絡人 王小明 電話 0912345678", fontsize=12, fontname="china-t")
    _widget(page, pymupdf.PDF_WIDGET_TYPE_TEXT, "applicant", (72, 150, 300, 174), "王小明")
    _widget(page, pymupdf.PDF_WIDGET_TYPE_TEXT, "note", (320, 150, 520, 174), "無")
    _widget(page, pymupdf.PDF_WIDGET_TYPE_COMBOBOX, "reviewer", (72, 200, 300, 224), "陳美玲", ["陳美玲", "林志豪", "王小明"])
    _widget(page, pymupdf.PDF_WIDGET_TYPE_LISTBOX, "cc", (72, 250, 300, 320), "張惠如", ["張惠如", "王小明", "李大同"],
            pymupdf.PDF_CH_FIELD_IS_MULTI_SELECT)
    doc.save(path)
    doc.close()
    doc = pymupdf.open(path)
    for w in doc[0].widgets():
        if w.field_name == "applicant":  # a rich-text value as Acrobat writes it, and a default value
            doc.xref_set_key(w.xref, "RV", pymupdf.get_pdf_str('<?xml version="1.0"?><body xmlns="http://www.w3.org/1999/'
                                                               'xhtml"><p>王小明</p></body>'))
            doc.xref_set_key(w.xref, "Ff", str(1 << 25))
            doc.xref_set_key(w.xref, "DV", pymupdf.get_pdf_str("王小明"))
        if w.field_name == "note":
            doc.xref_set_key(w.xref, "DV", pymupdf.get_pdf_str("林志豪"))
        if w.field_name == "cc":
            doc.xref_set_key(w.xref, "V", "[" + pymupdf.get_pdf_str("張惠如") + pymupdf.get_pdf_str("王小明") + "]")
    _add_xfa(doc)
    doc.save(path.with_suffix(".x.pdf"))
    doc.close()
    return path.with_suffix(".x.pdf")


def test_form_option_lists_selections_and_default_values_are_read(tmp_path):
    src = _form_pdf(tmp_path / "form.pdf")
    texts = [(t.section, t.text) for t in PdfAdapter().parse(src).texts if t.section != "第 1 頁"]
    values = [t for s, t in texts if s == "表單欄位"]
    assert sorted(values) == sorted(["王小明", "無", "陳美玲", "張惠如", "王小明"])  # both selections of the list box
    options = [t for s, t in texts if s == "表單選項"]
    assert sorted(options) == sorted(["陳美玲", "林志豪", "王小明", "張惠如", "王小明", "李大同"])
    assert ("隱藏內容：表單欄位預設值", "林志豪") in texts  # differs from the value, so it is a place of its own


def test_form_side_values_do_not_keep_a_masked_name(tmp_path, settings):
    src = _form_pdf(tmp_path / "form.pdf")
    ad = PdfAdapter()
    names = ["王小明", "陳美玲", "張惠如", "林志豪", "0912345678"]
    _inv, plan = plan_for(ad, src, strings=names)
    dst = tmp_path / "out.pdf"
    ad.apply(src, dst, plan, settings)
    assert leaks(dst, names + ["陳大文"]) == set()  # the form's XFA copy is gone, not just its known values
    assert ad.residue(dst, plan.strings) == [] and ad.verify(src, dst, plan, settings) == []
    with pymupdf.open(dst) as doc:
        boxes = {w.field_name: w for w in doc[0].widgets()}
        assert boxes["cc"].choice_values == ["■■■", "■■■", "李大同"]  # unmasked options stay
        assert doc.xref_get_key(boxes["cc"].xref, "V")[1].count("FEFF25A025A025A0") == 2
        assert doc.xref_get_key(boxes["applicant"].xref, "RV")[0] == "null"
        assert boxes["note"].field_value == "無"
    assert opens(dst)


def test_choices_with_export_values_are_masked_in_both(tmp_path, settings):
    """Guard for the [export value, shown text] form of a choice list."""
    src = tmp_path / "pairs.pdf"
    doc = pymupdf.open()
    _widget(doc.new_page(), pymupdf.PDF_WIDGET_TYPE_COMBOBOX, "reviewer", (72, 200, 300, 224), "chen",
            [["chen", "陳美玲"], ["lin", "林志豪"]])
    doc.save(src)
    doc.close()
    ad = PdfAdapter()
    texts = [(t.section, t.text) for t in ad.parse(src).texts if t.section != "第 1 頁"]
    assert ("表單選項", "陳美玲") in texts and ("隱藏內容：表單選項的匯出值", "chen") in texts
    _inv, plan = plan_for(ad, src, strings=["陳美玲"])
    dst = tmp_path / "out.pdf"
    ad.apply(src, dst, plan, settings)
    with pymupdf.open(dst) as doc:
        assert [w.choice_values for w in doc[0].widgets()] == [[("chen", "■■■"), ("lin", "林志豪")]]
    assert leaks(dst, ["陳美玲"]) == set() and opens(dst)


def test_residue_sees_form_side_values(tmp_path):
    src = _form_pdf(tmp_path / "form.pdf")
    ad = PdfAdapter()
    assert any("表單" in r for r in ad.residue(src, ["林志豪"]))
    assert any("XFA" in r for r in ad.residue(src, ["陳大文"]))


def test_a_form_that_exists_only_as_xfa_is_reported(tmp_path, settings):
    src = tmp_path / "xfa.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Please wait...", fontsize=14)
    doc.xref_set_key(doc.pdf_catalog(), "AcroForm", "<</Fields []>>")
    _add_xfa(doc)
    doc.save(src)
    doc.close()
    ad = PdfAdapter()
    assert any("XFA" in u.name for u in ad.parse(src).unchecked)
    dst = tmp_path / "out.pdf"
    ad.apply(src, dst, MaskPlan(), settings)
    assert leaks(dst, ["王小明", "陳大文"]) == set() and opens(dst)


def test_a_plain_form_lists_nothing_unchecked(tmp_path):
    """Guard: an ordinary form is read field by field and needs no warning."""
    src = tmp_path / "plain.pdf"
    doc = pymupdf.open()
    _widget(doc.new_page(), pymupdf.PDF_WIDGET_TYPE_TEXT, "name", (72, 100, 300, 124), "王小明")
    doc.save(src)
    doc.close()
    inv = PdfAdapter().parse(src)
    assert inv.unchecked == [] and inv.notes == []


# ---- P2: a font without a Unicode map: the text layer cannot be trusted, so the page is read as a picture ------

def _font_file() -> str:
    path = getattr(cjk_font(), "path", "")
    if not path:
        pytest.skip("no CJK font installed")
    return path


def _pdf_without_unicode_map(path, broken, readable=()):
    """Lines in `broken` use an embedded font whose /ToUnicode map is removed (older report generators write such
    files): the glyphs show, but the text layer reads as nonsense. Lines in `readable` use a normal font."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    for i, line in enumerate(broken):
        page.insert_text((72, 100 + 40 * i), line, fontsize=14, fontname="F0", fontfile=_font_file())
    for i, line in enumerate(readable):
        page.insert_text((72, 400 + 40 * i), line, fontsize=14, fontname="china-t")
    doc.save(path.with_suffix(".tmp.pdf"))
    doc.close()
    doc = pymupdf.open(path.with_suffix(".tmp.pdf"))
    for x in range(1, doc.xref_length()):  # the built-in font of the readable lines reads through its own encoding
        if "ToUnicode" in doc.xref_get_keys(x):
            doc.xref_set_key(x, "ToUnicode", "null")
    doc.save(path, garbage=3)
    doc.close()
    return path


def _px(rect) -> tuple:
    return tuple(v * ZOOM for v in rect)


def test_page_whose_font_has_no_unicode_map_is_read_from_the_picture(tmp_path):
    src = _pdf_without_unicode_map(tmp_path / "nomap.pdf", ["姓名：王小明　電話：0912345678", "地址：台北市中山區民生東路一段1號"])
    ad = PdfAdapter()
    inv = ad.parse(src)
    [pg] = inv.images
    assert pg.needs_scan  # read by OCR like a page without a text layer
    assert not [t for t in inv.texts if t.section == "第 1 頁"]  # no nonsense text sent for detection
    assert any("第 1 頁" in n and "影像辨識" in n for n in inv.notes)
    line = OcrLine("姓名：王小明電話：0912345678", _px((72, 86, 330, 104)), even_split("姓名：王小明電話：0912345678",
                                                                                   _px((72, 86, 330, 104))))
    assert ad.filter_ocr(src, "pg1", [line]) == [line]  # not taken for a repeat of the text layer


def test_a_box_from_the_picture_removes_the_glyphs_of_such_a_page(tmp_path, settings):
    """Guard: once such a page is read as a picture, a box from OCR takes the glyphs out of the file."""
    src = _pdf_without_unicode_map(tmp_path / "nomap.pdf", ["姓名：王小明　電話：0912345678"])
    ad = PdfAdapter()
    name = (72 + 3 * 14, 86, 72 + 6 * 14, 104)  # where 王小明 is drawn
    box = _px(name)
    plan = MaskPlan(boxes={"pg1": [box]})
    dst = tmp_path / "out.pdf"
    ad.apply(src, dst, plan, settings)

    def glyphs(path):
        with pymupdf.open(path) as doc:
            raw = doc[0].get_text("rawdict", clip=pymupdf.Rect(name[0] + 2, name[1] + 2, name[2] - 2, name[3] - 2))
            return sum(len(s["chars"]) for b in raw["blocks"] for ln in b.get("lines", []) for s in ln["spans"])

    assert glyphs(src) >= 3 and glyphs(dst) == 0
    assert ad.verify(src, dst, plan, settings) == [] and opens(dst)


def test_mixed_page_keeps_the_readable_text_and_reads_the_rest_from_the_picture(tmp_path):
    src = _pdf_without_unicode_map(tmp_path / "mixed.pdf", ["姓名：王小明"], readable=["聯絡電話 0912-345-678"])
    ad = PdfAdapter()
    inv = ad.parse(src)
    assert [t.text for t in inv.texts if t.section == "第 1 頁"] == ["聯絡電話 0912-345-678"]
    assert inv.images[0].needs_scan
    unit = inv.texts[0]
    [(_, readable_box)] = ad.locate(src, unit.id, 0, len(unit.text))
    again = OcrLine("聯絡電話0912-345-678", readable_box, even_split("聯絡電話0912-345-678", readable_box))
    broken = OcrLine("姓名：王小明", _px((72, 86, 160, 104)), even_split("姓名：王小明", _px((72, 86, 160, 104))))
    assert ad.filter_ocr(src, "pg1", [again, broken]) == [broken]


def test_ordinary_chinese_and_latin_pages_are_not_flagged(tmp_path):
    """Guard: a normal text layer is trusted as before, embedded fonts included."""
    src = make_pdf(tmp_path / "plain.pdf", [{"lines": ["聯絡人 王小明 先生", "第二行內容"]}, {"lines": ["第二頁"]}])
    doc = pymupdf.open(src)
    page = doc.new_page()
    page.insert_text((72, 100), "Contact: John Smith, phone 555-0100", fontsize=12, fontname="helv")
    page.insert_text((72, 140), "Times line", fontsize=12, fontname="tiro")
    page = doc.new_page()
    page.insert_text((72, 100), "嵌入字型 王小明", fontsize=14, fontname="F1", fontfile=_font_file())
    doc.save(tmp_path / "plain2.pdf")
    doc.close()
    inv = PdfAdapter().parse(tmp_path / "plain2.pdf")
    assert [im.needs_scan for im in inv.images] == [False, False, False, False]
    assert inv.notes == [] and inv.unchecked == []
    assert any(t.text.split() == ["嵌入字型", "王小明"] for t in inv.texts)  # the font maps its space to U+00A0


# ---- P5c: text placed outside the page --------------------------------------------------------------------------

def _outside_pdf(path, crop=None):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "看得到的內容", fontsize=14, fontname="china-t")
    page.insert_text((72, 900), "頁外 李大同 A123456789", fontsize=14, fontname="china-t")
    page.insert_text((-200, 300), "左外 王小明", fontsize=14, fontname="china-t")
    page.insert_text((72, 700), "裁切外 陳美玲", fontsize=14, fontname="china-t")
    if crop:
        page.set_cropbox(pymupdf.Rect(crop))
    doc.save(path)
    doc.close()
    return path


def test_text_outside_the_page_is_read_and_shown_as_its_own_text(tmp_path):
    src = _outside_pdf(tmp_path / "out.pdf", crop=(0, 0, 595, 600))
    ad = PdfAdapter()
    inv = ad.parse(src)
    outside = [t for t in inv.texts if t.section == "隱藏內容：第 1 頁頁面外的文字"]  # not seen: kept out of the code text
    assert sorted(t.text for t in outside) == sorted(["頁外 李大同 A123456789", "左外 王小明", "裁切外 陳美玲"])
    assert [t.text for t in inv.texts if t.section == "第 1 頁"] == ["看得到的內容"]
    blocks = ad.view(src, inv)
    assert all({"type": "text", "unit": t.id, "section": t.section} in blocks for t in outside)  # listed for review
    assert ad.locate(src, outside[0].id, 0, 2) == []  # nowhere on the page picture
    assert any("李大同" in r for r in ad.residue(src, ["李大同"]))


def test_text_outside_the_page_is_masked(tmp_path, settings):
    src = _outside_pdf(tmp_path / "out.pdf")
    ad = PdfAdapter()
    names = ["李大同", "A123456789", "王小明", "陳美玲"]
    _inv, plan = plan_for(ad, src, strings=names)
    dst = tmp_path / "masked.pdf"
    ad.apply(src, dst, plan, settings)
    assert leaks(dst, names) == set()
    with pymupdf.open(dst) as doc:
        text = doc[0].get_text("text", flags=0, clip=pymupdf.INFINITE_RECT())
    assert "看得到的內容" in text and "頁外" in text and "左外" in text
    assert ad.residue(dst, plan.strings) == [] and ad.verify(src, dst, plan, settings) == []
    assert opens(dst)


# ---- P5a: a box drawn over an image stamp ----------------------------------------------------------------------

STAMP = (300, 600, 480, 680)


def _stamp_pdf(tmp_path, rotate=0):
    seal = Image.new("RGB", (360, 160), "white")
    d = ImageDraw.Draw(seal)
    d.rectangle((4, 4, 355, 155), outline="red", width=6)
    d.text((30, 45), "核准 王小明", font=cjk_font(48), fill="red")
    seal.save(tmp_path / "seal.png")
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "本案經審核通過。", fontsize=14, fontname="china-t")
    page.add_stamp_annot(pymupdf.Rect(STAMP), stamp=str(tmp_path / "seal.png"))
    page.add_text_annot((72, 300), "另一則備註")  # away from any box
    if rotate:
        page.set_rotation(rotate)
    doc.save(tmp_path / "stamp.pdf")
    doc.close()
    return tmp_path / "stamp.pdf"


def _reds(png: bytes, box) -> int:
    im = Image.open(io.BytesIO(png)).convert("RGB").crop(tuple(int(v) for v in box))
    return sum(1 for r, g, b in im.get_flattened_data() if r > 180 and g < 90 and b < 90)


@pytest.mark.parametrize("rotate", [0, 90])
def test_a_box_over_an_image_stamp_blacks_out_the_stamp_there(tmp_path, settings, rotate):
    src = _stamp_pdf(tmp_path, rotate)
    ad = PdfAdapter()
    ad.parse(src)
    with pymupdf.open(src) as doc:
        page = doc[0]
        left, right = ad._to_px(page, (300, 600, 390, 680)), ad._to_px(page, (395, 610, 470, 670))
    dst = tmp_path / "out.pdf"
    plan = MaskPlan(boxes={"pg1": [left]})
    ad.apply(src, dst, plan, settings)
    with pymupdf.open(dst) as doc:
        png = doc[0].get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM), alpha=False).tobytes("png")
        notes = [a.type[1] for a in doc[0].annots()]
        stored = [Image.open(io.BytesIO(doc.extract_image(x[0])["image"])).convert("RGB") for x in doc.get_page_images(0)]
    assert imaging.is_black(png, left)
    assert _reds(png, right) > 500  # the rest of the stamp is still there
    assert notes == ["Text"]  # the note away from the box is untouched
    for im in stored:  # the pixels under the box are gone from the picture, not just covered
        assert sum(1 for r, g, b in im.crop((0, 0, im.width // 2 - 8, im.height)).get_flattened_data()
                   if r > 180 and g < 90 and b < 90) == 0
    assert ad.verify(src, dst, plan, settings) == [] and opens(dst)


def test_a_box_over_an_opaque_form_field_blacks_it_out(tmp_path, settings):
    src = tmp_path / "field.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    w = pymupdf.Widget()
    w.field_type, w.field_name, w.field_value = pymupdf.PDF_WIDGET_TYPE_TEXT, "sign", "核准"
    w.fill_color = (1, 0, 0)
    w.rect = pymupdf.Rect(300, 600, 480, 680)
    page.add_widget(w)
    doc.save(src)
    doc.close()
    ad = PdfAdapter()
    ad.parse(src)
    box = _px((300, 600, 390, 680))
    plan = MaskPlan(boxes={"pg1": [box]})
    dst = tmp_path / "out.pdf"
    ad.apply(src, dst, plan, settings)
    with pymupdf.open(dst) as doc:
        png = doc[0].get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM), alpha=False).tobytes("png")
    assert imaging.is_black(png, box) and _reds(png, _px((400, 610, 470, 670))) > 500
    assert ad.verify(src, dst, plan, settings) == [] and opens(dst)


def test_a_box_beside_annotations_leaves_them_alone(tmp_path, settings):
    """Guard: only what shows through a box changes; a highlight over masked text still reads black there."""
    src = make_pdf(tmp_path / "h.pdf", [{"lines": ["聯絡人 王小明 先生"], "annot": "備註文字"}])
    with pymupdf.open(src) as doc:
        doc[0].add_highlight_annot(doc[0].search_for("王小明")[0])
        doc.save(tmp_path / "h2.pdf")
    src = tmp_path / "h2.pdf"
    ad = PdfAdapter()
    inv = ad.parse(src)
    unit = next(t for t in inv.texts if "王小明" in t.text)
    s = unit.text.index("王小明")
    plan = MaskPlan(text={unit.id: [(s, s + 3)]})
    dst = tmp_path / "out.pdf"
    ad.apply(src, dst, plan, settings)
    with pymupdf.open(dst) as doc:
        assert sorted(a.type[1] for a in doc[0].annots()) == ["Highlight", "Text"]
    assert ad.verify(src, dst, plan, settings) == []


# ---- P5b: tagged PDF — alternate text and replacement text ------------------------------------------------------

def _hex(s: str) -> str:
    return "<FEFF" + s.encode("utf-16-be").hex().upper() + ">"


def _tagged_pdf(path, inline_alt=False):
    """Marked content with /ActualText around the first line (as generators write it), another line with
    replacement text of its own, and a structure tree whose Figure has /Alt naming a person."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "承辦人：王小明", fontsize=14, fontname="china-t")
    page.insert_text((72, 140), "其他說明文字", fontsize=14, fontname="china-t")
    page.insert_text((72, 180), "無關的一行", fontsize=14, fontname="china-t")
    page.clean_contents()
    xref = page.get_contents()[0]
    cont = doc.xref_stream(xref).decode("latin-1")
    first = cont.index("BT")
    end = cont.index("ET", first) + 2
    second = cont.index("BT", end)
    end2 = cont.index("ET", second) + 2
    figure = f"/Figure <</MCID 2 /Alt {_hex('林志豪的簽名')}>> BDC EMC\n" if inline_alt else ""
    cont = (cont[:first] + f"/Span <</MCID 0 /ActualText {_hex('承辦人：王小明')}>> BDC\n" + cont[first:end] + "\nEMC\n"
            + cont[end:second] + f"/Span <</MCID 1 /ActualText {_hex('其他說明文字')}>> BDC\n" + cont[second:end2]
            + "\nEMC\n" + figure + cont[end2:])
    doc.update_stream(xref, cont.encode("latin-1"))
    root, fig, para = doc.get_new_xref(), doc.get_new_xref(), doc.get_new_xref()
    doc.update_object(fig, f"<</Type/StructElem/S/Figure/P {root} 0 R/Alt {_hex('王小明的大頭照')}/Pg {page.xref} 0 R>>")
    doc.update_object(para, f"<</Type/StructElem/S/P/P {root} 0 R/T {_hex('段落')}/Pg {page.xref} 0 R/K 0>>")
    doc.update_object(root, f"<</Type/StructTreeRoot/K [{fig} 0 R {para} 0 R]>>")
    cat = doc.pdf_catalog()
    doc.xref_set_key(cat, "StructTreeRoot", f"{root} 0 R")
    doc.xref_set_key(cat, "MarkInfo", "<</Marked true>>")
    doc.save(path)
    doc.close()
    return path


def _struct_alt(path) -> list:
    with pymupdf.open(path) as doc:
        return [doc.xref_get_key(x, "Alt")[1] for x in range(1, doc.xref_length())
                if doc.xref_get_key(x, "Alt")[0] == "string"]


def test_alternate_text_in_the_structure_tree_is_read(tmp_path):
    src = _tagged_pdf(tmp_path / "tagged.pdf")
    ad = PdfAdapter()
    texts = [(t.section, t.text) for t in ad.parse(src).texts]
    assert ("隱藏內容：替代文字", "王小明的大頭照") in texts
    assert ("隱藏內容：結構標題", "段落") in texts
    assert any("替代文字" in r for r in ad.residue(src, ["王小明"]))


def test_masking_reaches_alternate_and_replacement_text(tmp_path, settings):
    src = _tagged_pdf(tmp_path / "tagged.pdf")
    ad = PdfAdapter()
    _inv, plan = plan_for(ad, src, strings=["王小明"])
    dst = tmp_path / "out.pdf"
    ad.apply(src, dst, plan, settings)
    assert leaks(dst, ["王小明"]) == set()
    assert _struct_alt(dst) == ["■■■的大頭照"]
    with pymupdf.open(dst) as doc:
        text = doc[0].get_text()
        stream = b"".join(doc.xref_stream(x) for x in doc[0].get_contents())
    assert "承辦人：" in text and "其他說明文字" in text
    assert stream.count(b"/ActualText") == 1  # the untouched line keeps its replacement text
    assert ad.residue(dst, plan.strings) == [] and ad.verify(src, dst, plan, settings) == []
    assert opens(dst)


def test_replacement_text_does_not_bring_back_a_name_masked_in_one_place(tmp_path, settings):
    src = _tagged_pdf(tmp_path / "tagged.pdf")
    ad = PdfAdapter()
    inv = ad.parse(src)
    unit = next(t for t in inv.texts if t.text.startswith("承辦人"))
    s = unit.text.index("王小明")
    plan = MaskPlan(text={unit.id: [(s, s + 3)]})  # only this place: the name is not searched for elsewhere
    dst = tmp_path / "out.pdf"
    ad.apply(src, dst, plan, settings)
    with pymupdf.open(dst) as doc:
        assert "王小明" not in doc[0].get_text() and "王小明" not in doc[0].get_text("rawdict").__repr__()
    assert ad.verify(src, dst, plan, settings) == []


def test_alternate_text_on_marked_content_is_read_and_masked(tmp_path, settings):
    src = _tagged_pdf(tmp_path / "tagged.pdf", inline_alt=True)
    ad = PdfAdapter()
    inv = ad.parse(src)
    assert any(t.section == "隱藏內容：替代文字" and t.text == "林志豪的簽名" for t in inv.texts)
    _inv, plan = plan_for(ad, src, strings=["林志豪"])
    dst = tmp_path / "out.pdf"
    ad.apply(src, dst, plan, settings)
    assert leaks(dst, ["林志豪"]) == set()
    assert ad.residue(dst, plan.strings) == [] and opens(dst)
