"""Regression tests for the smaller problems found during acceptance and review. Each one failed before its fix."""
import io

import pymupdf
from PIL import Image

from deid.formats.pdf import PdfAdapter
from deid.model import MaskPlan


def jpeg_with_metadata(size=(200, 100), *, orientation=None, color="white") -> bytes:
    im = Image.new("RGB", size, color)
    exif = Image.Exif()
    exif[0x010F] = "MakerSecret"
    exif[0x013B] = "ArtistWangXiaoming"
    if orientation:
        exif[0x0112] = orientation
    out = io.BytesIO()
    im.save(out, "JPEG", exif=exif.tobytes(), comment=b"CommentWangXiaoming", quality=95)
    return out.getvalue()


def stream_bytes(path) -> bytes:
    doc = pymupdf.open(path)
    try:
        return b"".join(doc.xref_stream_raw(x) or b"" for x in range(1, doc.xref_length()) if doc.xref_is_stream(x))
    finally:
        doc.close()


def all_layers_text(path) -> str:
    """Text of page 1 with every optional layer made visible."""
    doc = pymupdf.open(path)
    doc.xref_set_key(doc.pdf_catalog(), "OCProperties", "null")
    data = doc.tobytes()
    doc.close()
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        return doc[0].get_text()
    finally:
        doc.close()


# ---- PDF: places that kept a copy of the page ---------------------------------------------------

def pdf_with_thumbnail(path):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Contact Wang Xiaoming today", fontsize=12)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(0.3, 0.3))
    xref = doc.get_new_xref()
    doc.update_object(xref, f"<</Type/XObject/Subtype/Image/Width {pix.width}/Height {pix.height}"
                            "/ColorSpace/DeviceRGB/BitsPerComponent 8>>")
    doc.update_stream(xref, pix.samples)
    doc.xref_set_key(page.xref, "Thumb", f"{xref} 0 R")
    doc.save(path)
    doc.close()
    return path


def test_page_thumbnail_is_removed_because_it_shows_the_unmasked_page(tmp_path, settings):
    src = pdf_with_thumbnail(tmp_path / "thumb.pdf")
    adapter = PdfAdapter()
    inv = adapter.parse(src)
    start = inv.texts[0].text.index("Wang")
    plan = MaskPlan(text={inv.texts[0].id: [(start, start + 13)]}, meta={}, boxes={}, strings=["Wang Xiaoming"])
    out = tmp_path / "out.pdf"
    adapter.apply(src, out, plan, settings)
    doc = pymupdf.open(out)
    try:
        assert doc.xref_get_key(doc[0].xref, "Thumb")[0] == "null"
        images = [x for x in range(1, doc.xref_length()) if doc.xref_get_key(x, "Subtype")[1] == "/Image"]
        assert images == []
    finally:
        doc.close()


def glyph_positions(path) -> dict:
    doc = pymupdf.open(path)
    try:
        return {c["c"]: round(c["bbox"][0], 1) for b in doc[0].get_text("rawdict")["blocks"] if b["type"] == 0
                for line in b["lines"] for span in line["spans"] for c in span["chars"]}
    finally:
        doc.close()


def test_text_after_a_masked_word_stays_where_it_was(tmp_path, settings):
    # One library release moved everything after a removed word one glyph to the left, under the mask.
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 100), "abcd WXYZ efgh", fontsize=12)
    src = tmp_path / "line.pdf"
    doc.save(src)
    doc.close()
    adapter = PdfAdapter()
    inv = adapter.parse(src)
    plan = MaskPlan(text={inv.texts[0].id: [(5, 9)]}, meta={}, boxes={}, strings=["WXYZ"])
    out = tmp_path / "out.pdf"
    adapter.apply(src, out, plan, settings)
    before, after = glyph_positions(src), glyph_positions(out)
    assert "W" not in after
    assert {c: after[c] for c in "abcdefgh"} == {c: before[c] for c in "abcdefgh"}
    assert adapter.verify(src, out, plan, settings) == []


def pdf_with_hidden_layer(path):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Visible report text", fontsize=12)
    layer = doc.add_ocg("draft notes", on=False)
    page.insert_text((72, 200), "Reviewer Lin Meihua B223456781", fontsize=12, oc=layer)
    doc.save(path)
    doc.close()
    return path


def test_text_on_a_hidden_layer_is_checked_like_visible_text(tmp_path):
    src = pdf_with_hidden_layer(tmp_path / "layers.pdf")
    texts = [t.text for t in PdfAdapter().parse(src).texts]
    assert any("Lin Meihua" in t for t in texts)


def test_text_on_a_hidden_layer_is_removed_and_the_layer_stays_hidden(tmp_path, settings):
    src = pdf_with_hidden_layer(tmp_path / "layers.pdf")
    adapter = PdfAdapter()
    inv = adapter.parse(src)
    unit = next(t for t in inv.texts if "Lin Meihua" in t.text)
    start = unit.text.index("Lin Meihua")
    plan = MaskPlan(text={unit.id: [(start, start + 10)]}, meta={}, boxes={}, strings=["Lin Meihua"])
    out = tmp_path / "out.pdf"
    adapter.apply(src, out, plan, settings)

    assert "Lin Meihua" not in all_layers_text(out)
    assert "B223456781" in all_layers_text(out)  # only what was asked for is removed
    assert adapter.residue(out, ["Lin Meihua"]) == []
    assert adapter.verify(src, out, plan, settings) == []
    doc = pymupdf.open(out)
    try:
        assert [g["on"] for g in doc.get_ocgs().values()] == [False]
        assert "B223456781" not in doc[0].get_text()  # still hidden for a reader
    finally:
        doc.close()


def test_residue_check_looks_inside_hidden_layers(tmp_path, settings):
    src = pdf_with_hidden_layer(tmp_path / "layers.pdf")
    adapter = PdfAdapter()
    out = tmp_path / "out.pdf"
    adapter.apply(src, out, MaskPlan(text={}, meta={}, boxes={}, strings=[]), settings)
    assert any("Lin Meihua" in line for line in adapter.residue(out, ["Lin Meihua"]))


def test_photo_metadata_inside_a_pdf_is_removed_without_touching_the_picture(tmp_path, settings):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(pymupdf.Rect(72, 300, 272, 400), stream=jpeg_with_metadata(color=(200, 30, 30)))
    src = tmp_path / "photo.pdf"
    doc.save(src)
    doc.close()
    assert b"ArtistWangXiaoming" in stream_bytes(src)

    adapter = PdfAdapter()
    out = tmp_path / "out.pdf"
    adapter.apply(src, out, MaskPlan(text={}, meta={}, boxes={}, strings=[]), settings)
    data = stream_bytes(out)
    for secret in (b"ArtistWangXiaoming", b"CommentWangXiaoming", b"MakerSecret"):
        assert secret not in data
    before, after = pymupdf.open(src), pymupdf.open(out)
    try:
        assert before[0].get_pixmap().samples == after[0].get_pixmap().samples
    finally:
        before.close()
        after.close()


def test_scripts_in_a_pdf_are_listed_as_not_checked(tmp_path):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 100), "plain page", fontsize=12)
    doc.xref_set_key(doc.pdf_catalog(), "OpenAction", "<</S/JavaScript/JS(app.alert\\('Wang Xiaoming'\\))>>")
    src = tmp_path / "script.pdf"
    doc.save(src)
    doc.close()
    unchecked = PdfAdapter().parse(src).unchecked
    assert any("JavaScript" in u.name or "JavaScript" in u.reason for u in unchecked)


def test_a_pdf_without_scripts_lists_nothing(tmp_path):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 100), "plain page about JavaScript", fontsize=12)
    src = tmp_path / "plain.pdf"
    doc.save(src)
    doc.close()
    assert PdfAdapter().parse(src).unchecked == []


# ---- pictures: embedded metadata ----------------------------------------------------------------

from PIL import PngImagePlugin  # noqa: E402

from deid import imaging  # noqa: E402


def png_with_text() -> bytes:
    info = PngImagePlugin.PngInfo()
    info.add_text("Author", "PngAuthorWangXiaoming")
    info.add_itxt("Description", "PngNoteLinMeihua")
    out = io.BytesIO()
    Image.new("RGB", (40, 30), (10, 120, 200)).save(out, "PNG", pnginfo=info, dpi=(96, 96))
    return out.getvalue()


def pixels(data: bytes):
    im = Image.open(io.BytesIO(data))
    return im.mode, im.size, im.tobytes()


def test_jpeg_metadata_is_cut_out_and_the_picture_data_is_untouched():
    src = jpeg_with_metadata()
    out = imaging.strip_metadata(src)
    for secret in (b"ArtistWangXiaoming", b"CommentWangXiaoming", b"MakerSecret", b"Exif"):
        assert secret not in out
    assert pixels(out) == pixels(src)  # no re-encoding: decoded pixels are identical
    assert imaging.strip_metadata(out) is out


def test_xmp_only_jpeg_is_cleaned():
    plain = io.BytesIO()
    Image.new("RGB", (32, 32), "white").save(plain, "JPEG")
    xmp = b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta><dc:creator>XmpWangXiaoming</dc:creator></x:xmpmeta>"
    data = plain.getvalue()
    src = data[:2] + b"\xff\xe1" + (len(xmp) + 2).to_bytes(2, "big") + xmp + data[2:]
    assert Image.open(io.BytesIO(src)).size == (32, 32)
    out = imaging.strip_metadata(src)
    assert b"XmpWangXiaoming" not in out
    assert pixels(out) == pixels(src)


def test_sideways_photo_keeps_its_orientation_and_nothing_else():
    out = imaging.strip_metadata(jpeg_with_metadata(orientation=6))
    exif = Image.open(io.BytesIO(out)).getexif()
    assert dict(exif) == {0x0112: 6}


def test_data_after_the_end_of_a_jpeg_is_dropped():
    src = jpeg_with_metadata() + b"TRAILER-WangXiaoming"
    assert b"TRAILER" not in imaging.strip_metadata(src)


def test_png_text_blocks_are_removed_and_resolution_is_kept():
    src = png_with_text()
    assert b"PngAuthorWangXiaoming" in src
    out = imaging.strip_metadata(src)
    assert b"PngAuthorWangXiaoming" not in out and b"PngNoteLinMeihua" not in out
    assert pixels(out) == pixels(src)
    assert Image.open(io.BytesIO(out)).info.get("dpi") is not None
    assert imaging.strip_metadata(out) is out


def test_gif_comment_is_removed_and_animation_survives():
    frames = [Image.new("RGB", (20, 20), c).convert("P") for c in ("red", "green", "blue")]
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], loop=0, duration=80, comment=b"GifNoteWangXiaoming")
    src = buf.getvalue()
    assert b"GifNoteWangXiaoming" in src
    out = imaging.strip_metadata(src)
    assert b"GifNoteWangXiaoming" not in out
    im = Image.open(io.BytesIO(out))
    assert im.n_frames == 3 and im.info.get("loop") == 0


def test_files_that_are_not_understood_are_returned_as_they_are():
    for data in (b"", b"\xff\xd8\xff", b"\xff\xd8 not really a jpeg", imaging.PNG_SIGNATURE + b"\x00\x00", b"GIF89a", b"BM123456"):
        assert imaging.strip_metadata(data) is data


def test_painting_a_picture_does_not_carry_its_metadata_over():
    out = imaging.paint_boxes(jpeg_with_metadata(), [(10, 10, 60, 40)])
    assert b"CommentWangXiaoming" not in out and b"ArtistWangXiaoming" not in out
    assert imaging.is_black(out, (10, 10, 60, 40))


import zipfile  # noqa: E402

from deid.formats.docx import DocxAdapter  # noqa: E402
from deid.formats.xlsx import XlsxAdapter  # noqa: E402
from tests.builders import make_docx, make_xlsx  # noqa: E402

EMPTY = MaskPlan(text={}, meta={}, boxes={}, strings=[])
SECRETS = (b"ArtistWangXiaoming", b"CommentWangXiaoming", b"PngAuthorWangXiaoming", b"PngNoteLinMeihua")


def media(path) -> bytes:
    with zipfile.ZipFile(path) as z:
        return b"".join(z.read(n) for n in z.namelist() if "/media/" in n)


def test_word_pictures_lose_their_metadata_even_when_nothing_in_them_is_masked(tmp_path, settings):
    src = make_docx(tmp_path / "a.docx", ["報告內容"], images=[("image1.png", png_with_text()), ("image2.jpg", jpeg_with_metadata())])
    assert all(s in media(src) for s in SECRETS)
    adapter = DocxAdapter()
    out = tmp_path / "out.docx"
    adapter.apply(src, out, EMPTY, settings)
    assert not any(s in media(out) for s in SECRETS)
    assert adapter.verify(src, out, EMPTY, settings) == []


def test_verification_reports_a_picture_that_still_carries_metadata(tmp_path, settings):
    src = make_docx(tmp_path / "a.docx", ["報告內容"], images=[("image1.png", png_with_text())])
    problems = DocxAdapter().verify(src, src, EMPTY, settings)  # "output" that was never cleaned
    assert any("image1.png" in p for p in problems)


def test_excel_pictures_lose_their_metadata(tmp_path, settings):
    src = make_xlsx(tmp_path / "a.xlsx", [{"name": "名冊", "rows": [["項目"]]}], image=png_with_text())
    assert b"PngAuthorWangXiaoming" in media(src)
    adapter = XlsxAdapter()
    out = tmp_path / "out.xlsx"
    adapter.apply(src, out, EMPTY, settings)
    assert b"PngAuthorWangXiaoming" not in media(out)
    assert adapter.verify(src, out, EMPTY, settings) == []


# ---- pictures: sideways photos, odd palettes, vertical and full-width text ----------------------

from PIL import ImageOps  # noqa: E402

from deid.findings import find_in_ocr  # noqa: E402
from tests.fakes import ocr_line  # noqa: E402


def sideways_photo() -> bytes:
    """Stored 200x100 with a dark block at the stored top-left; flagged 'rotate to display' (shown 100x200)."""
    im = Image.new("RGB", (200, 100), "white")
    im.paste((90, 90, 90), (0, 0, 60, 40))
    exif = Image.Exif()
    exif[0x0112] = 6
    exif[0x013B] = "ArtistWangXiaoming"
    out = io.BytesIO()
    im.save(out, "JPEG", exif=exif.tobytes(), quality=95)
    return out.getvalue()


def shown(data: bytes) -> Image.Image:
    return ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("L")


def test_a_sideways_photo_is_measured_and_previewed_the_way_it_is_displayed():
    src = sideways_photo()
    assert imaging.image_info(src)[1:3] == (100, 200)
    preview = Image.open(io.BytesIO(imaging.to_browser_png(src)))
    assert preview.size == (100, 200)
    assert preview.convert("L").tobytes() == shown(src).tobytes()


def test_a_mask_drawn_on_a_sideways_photo_lands_where_it_was_drawn():
    src = sideways_photo()
    box = (10, 120, 70, 180)  # in displayed coordinates: lower part of the upright picture
    out = imaging.paint_boxes(src, [box])
    assert Image.open(io.BytesIO(out)).getexif().get(0x0112) == 6  # still displayed the same way round
    assert b"ArtistWangXiaoming" not in out
    after = shown(out)
    assert after.size == (100, 200)
    assert max(after.crop((14, 124, 66, 176)).tobytes()) < 16
    assert min(after.crop((10, 60, 90, 110)).tobytes()) > 230  # the rest is untouched
    assert imaging.is_black(out, box)


def test_a_palette_picture_whose_only_black_is_transparent_gets_an_opaque_mask():
    im = Image.new("P", (60, 40), 1)
    im.putpalette([0, 0, 0, 255, 255, 255, 255, 0, 0])
    out = io.BytesIO()
    im.save(out, "PNG", transparency=0)
    painted = imaging.paint_boxes(out.getvalue(), [(10, 10, 40, 30)])
    assert imaging.is_black(painted, (10, 10, 40, 30))
    assert Image.open(io.BytesIO(painted)).mode == "P"


def test_a_tall_line_without_per_character_boxes_is_split_top_to_bottom():
    boxes = imaging.even_split("王小明", (100.0, 0.0, 140.0, 120.0))
    assert boxes == [(100.0, 0.0, 140.0, 40.0), (100.0, 40.0, 140.0, 80.0), (100.0, 80.0, 140.0, 120.0)]
    wide = imaging.even_split("王小明", (0.0, 0.0, 120.0, 40.0))
    assert wide == [(0.0, 0.0, 40.0, 40.0), (40.0, 0.0, 80.0, 40.0), (80.0, 0.0, 120.0, 40.0)]


def test_full_width_digits_read_from_a_picture_match_the_ordinary_number():
    assert imaging.fold_width("電話０９１２Ａｂ，") == "電話0912Ab，"
    line = ocr_line("電話０９１２３４５６７８")
    hits = find_in_ocr([line], "0912345678")
    assert [fuzzy for _box, fuzzy in hits] == [False]
    assert hits[0][0][0] < line.chars[2][0] + 0.1 and hits[0][0][2] > line.chars[-1][2] - 0.1


def test_hidden_layers_are_mentioned_to_the_reviewer(tmp_path):
    notes = PdfAdapter().parse(pdf_with_hidden_layer(tmp_path / "layers.pdf")).notes
    assert len(notes) == 1 and "隱藏圖層" in notes[0]
    assert PdfAdapter().parse(pdf_with_thumbnail(tmp_path / "plain.pdf")).notes == []


from deid.pipeline import analyze, apply_and_verify, build_context  # noqa: E402
from tests.fakes import FakeFaces, FakeLLM, FakeOcr, keyword_llm, reply, tool_call  # noqa: E402


def context(doc, tmp_path, settings, table=None, **kw):
    return build_context(doc, tmp_path / "work", settings, llm=kw.get("llm") or keyword_llm(table or {}),
                         ocr=kw.get("ocr", FakeOcr()), faces=kw.get("faces", FakeFaces()))


def test_the_review_screen_gets_a_sideways_photo_the_right_way_up(tmp_path, settings):
    src = make_docx(tmp_path / "a.docx", ["活動照片"], images=[("image1.jpg", sideways_photo())])
    ctx = context(src, tmp_path, settings)
    ctx.load()
    preview = Image.open(io.BytesIO(ctx.image_data("img1")))
    assert preview.size == (100, 200) == (ctx.inv.images[0].width, ctx.inv.images[0].height)
    assert not preview.getexif().get(0x0112)


def test_a_face_box_on_a_sideways_photo_is_masked_where_it_was_found(tmp_path, settings):
    settings.use_agent = False
    src = make_docx(tmp_path / "a.docx", ["活動照片"], images=[("image1.jpg", sideways_photo())])
    box = (10.0, 120.0, 70.0, 180.0)  # found on the upright picture
    ctx = context(src, tmp_path, settings, faces=FakeFaces([box]))
    analyze(ctx, use_agent=False)
    result = apply_and_verify(ctx)
    assert result.ok, result.problems
    with zipfile.ZipFile(tmp_path / "work" / "output.docx") as z:
        out = z.read("word/media/image1.jpg")
    assert max(shown(out).crop((14, 124, 66, 176)).tobytes()) < 16
    assert Image.open(io.BytesIO(out)).getexif().get(0x0112) == 6


# ---- text the model could not judge is reported as not checked, by name ---------------------------

from deid.detect import detect_lines  # noqa: E402

GARBAGE = {"role": "assistant", "content": "抱歉，我無法處理這段內容"}


def broken_on(marker: str, table: dict | None = None) -> FakeLLM:
    """Detector double that answers with prose instead of JSON whenever the text contains `marker`."""
    good = keyword_llm(table or {})

    def handler(messages, tools):
        user = messages[-1]["content"]
        text = user if isinstance(user, str) else " ".join(p.get("text", "") for p in user if isinstance(p, dict))
        return GARBAGE if marker in text else good.handler(messages, tools)
    return FakeLLM(handler)


def test_detection_names_the_lines_it_could_not_judge():
    res = detect_lines(broken_on("亂碼"), ["承辦人王小明", "這行是亂碼段落", "一般內容"], chunk_chars=2500, workers=1)
    assert res.failed_lines == ["這行是亂碼段落"]
    assert res.failed_chunks == 1


def test_a_paragraph_the_model_could_not_judge_is_not_counted_as_checked(tmp_path, settings):
    src = make_docx(tmp_path / "a.docx", ["承辦人王小明", "這行是亂碼段落 林美華", "一般內容"])
    ctx = context(src, tmp_path, settings, llm=broken_on("亂碼", {"王小明": "姓名"}))
    analyze(ctx, use_agent=False)
    bad = next(t.id for t in ctx.inv.texts if "亂碼" in t.text)
    assert bad not in ctx.covered
    assert [f.text for f in ctx.store.findings if f.kind == "text"] == ["王小明"]  # the rest was still checked
    items = ctx.unread_items()
    assert len(items) == 1 and "這行是亂碼段落" in items[0]["name"] and "內文" in items[0]["name"]
    assert any("沒有經過個資偵測" in w for w in ctx.warnings)


def test_units_that_were_not_checked_survive_a_reload(tmp_path, settings):
    src = make_docx(tmp_path / "a.docx", ["承辦人王小明", "這行是亂碼段落 林美華"])
    ctx = context(src, tmp_path, settings, llm=broken_on("亂碼", {"王小明": "姓名"}))
    analyze(ctx, use_agent=False)
    again = build_context(src, tmp_path / "work", settings, llm=keyword_llm({}), ocr=FakeOcr(), faces=FakeFaces(), restore=True)
    assert again.unread_items() == ctx.unread_items() != []


def test_a_picture_whose_text_the_model_could_not_judge_is_named(tmp_path, settings):
    src = make_docx(tmp_path / "a.docx", ["說明"], images=[("image1.png", png_with_text())])
    ctx = context(src, tmp_path, settings, llm=broken_on("亂碼"), ocr=FakeOcr([ocr_line("亂碼截圖 林美華")]))
    analyze(ctx, use_agent=False)
    assert "img1" not in ctx.covered
    assert any("image1.png" in item["name"] for item in ctx.unread_items())


def test_the_agent_cannot_finish_until_it_has_read_what_detection_could_not_judge(tmp_path, settings):
    src = make_docx(tmp_path / "a.docx", ["承辦人王小明", "這行是亂碼段落 林美華"])
    detector = broken_on("亂碼", {"王小明": "姓名"})
    script = iter([tool_call("scan_text", {}), tool_call("finish", {"summary": "done"}),
                   tool_call("read_text", {"unit_ids": ["t2"]}),
                   tool_call("add_findings", {"items": [{"text": "林美華", "type": "姓名", "reason": "人名"}]}),
                   tool_call("finish", {"summary": "done"})])

    def handler(messages, tools):
        return next(script) if tools else detector.handler(messages, tools)

    ctx = context(src, tmp_path, settings, llm=FakeLLM(handler))
    analyze(ctx, use_agent=True)
    refused = [e for e in ctx.log if e["tool"] == "finish"]
    assert len(refused) == 2 and "t2" in refused[0]["result"] and '"ok": true' in refused[1]["result"]
    scan = next(e for e in ctx.log if e["tool"] == "scan_text")
    assert "t2" in scan["result"] and "read_text" in scan["result"]
    assert ctx.unread_items() == []  # the agent read it itself, so it was checked after all
    assert {f.text for f in ctx.store.findings if f.kind == "text"} == {"王小明", "林美華"}


# ---- the same, seen through the web API -----------------------------------------------------------

import time  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from deid.jobs import JobStore  # noqa: E402
from deid.web.app import create_app  # noqa: E402


def make_client(settings, llm, **kw):
    settings.use_agent = kw.pop("use_agent", False)

    def factory(src, workdir, s, progress=None, restore=False):
        return build_context(src, workdir, s, llm=llm, progress=progress, restore=restore,
                             ocr=kw.get("ocr", FakeOcr()), faces=FakeFaces())

    store = JobStore(settings, context_factory=factory)
    store.start()
    client = TestClient(create_app(settings, store))
    client.store = store
    return client


@pytest.fixture
def api(settings):
    clients = []

    def start(llm=None, **kw):
        clients.append(make_client(settings, llm or keyword_llm({"王小明": "姓名"}), **kw))
        return clients[-1]

    yield start
    for c in clients:
        c.store.stop()


def upload(client, data, name="報告.docx", **kw):
    r = client.post("/api/jobs", files={"file": (name, io.BytesIO(data), "application/octet-stream")}, **kw)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def wait_for(client, job_id, statuses=("review", "failed"), seconds=20):
    end = time.time() + seconds
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job.get("status") in statuses:
            return job
        time.sleep(0.02)
    raise AssertionError("job did not reach " + "/".join(statuses))


def test_the_review_screen_lists_what_the_model_could_not_judge(tmp_path, api):
    client = api(broken_on("亂碼", {"王小明": "姓名"}))
    job = upload(client, make_docx(tmp_path / "a.docx", ["承辦人王小明", "這行是亂碼段落 林美華"]).read_bytes())
    assert wait_for(client, job)["status"] == "review"
    data = client.get(f"/api/jobs/{job}/review").json()
    assert any("這行是亂碼段落" in u["name"] and "沒有經過個資偵測" in u["reason"] for u in data["unchecked"])
    report = client.get(f"/api/jobs/{job}/report").json()
    assert any("這行是亂碼段落" in u["name"] for u in report["unchecked"])


# ---- things that could stall or bloat the service -------------------------------------------------

import asyncio  # noqa: E402
import sys  # noqa: E402

import httpx  # noqa: E402

from deid import llm as llm_module  # noqa: E402
from deid.agent import sandbox, tools  # noqa: E402
from deid.findings import FindingStore  # noqa: E402
from deid.formats.docx import parse_part  # noqa: E402
from deid.llm import LLMClient  # noqa: E402
from deid.model import Inventory, TextUnit  # noqa: E402


def test_a_runaway_search_pattern_is_stopped_and_reported(tmp_path, settings):
    src = make_docx(tmp_path / "a.docx", ["a" * 45 + "b", "王小明"])
    ctx = context(src, tmp_path, settings)
    ctx.load()
    started = time.time()
    result = tools.call(ctx, "search", {"query": "(a|aa)+$", "regex": True})
    assert time.time() - started < 10
    assert "error" in result and "逾時" in result["error"]
    assert tools.call(ctx, "search", {"query": "王.明", "regex": True})["total"] == 1  # ordinary patterns still work


def slow_llm(seconds: float, table=None) -> FakeLLM:
    good = keyword_llm(table or {})

    def handler(messages, tools_):
        time.sleep(seconds)
        return good.handler(messages, tools_)
    return FakeLLM(handler)


def test_a_job_that_runs_past_the_time_limit_is_stopped_with_a_clear_message(tmp_path, settings, api):
    settings.job_timeout_min = 0.002  # about a tenth of a second
    client = api(slow_llm(0.4))
    job = upload(client, make_docx(tmp_path / "a.docx", ["承辦人王小明"]).read_bytes())
    done = wait_for(client, job)
    assert done["status"] == "failed" and "時間" in done["error"]


def test_deleting_a_job_while_it_is_processed_leaves_nothing_behind(tmp_path, settings, api):
    client = api(slow_llm(0.5))
    job = upload(client, make_docx(tmp_path / "a.docx", ["承辦人王小明", "第二段"]).read_bytes())
    wait_for(client, job, statuses=("processing",))
    assert client.delete(f"/api/jobs/{job}").status_code == 200
    folder = client.store.root / job
    end = time.time() + 10
    while time.time() < end and folder.exists():
        time.sleep(0.05)
    time.sleep(1.5)  # longer than the model call that was in flight: the worker has moved on by now
    assert not folder.exists()
    assert client.get(f"/api/jobs/{job}").status_code == 404


def test_script_output_is_capped_while_it_is_being_read():
    code = "import sys\nfor _ in range(500): sys.stdout.write('x' * 100_000)\nprint('END')"
    res = sandbox.run_bounded([sys.executable, "-c", code], b"", timeout=60)
    assert res.returncode == 0 and not res.timed_out
    assert res.truncated and len(res.data) <= sandbox.CAPTURE_LIMIT


def test_a_script_that_never_ends_is_stopped():
    started = time.time()
    res = sandbox.run_bounded([sys.executable, "-c", "import time\nprint('start', flush=True)\ntime.sleep(60)"], b"", timeout=1)
    assert res.timed_out and time.time() - started < 10
    assert b"start" in res.data


def call_asgi(app, scope_headers, method="POST", path="/api/jobs"):
    """Send request headers only. Reading the body is an error: the point is to refuse before reading."""
    sent = []

    async def receive():
        raise AssertionError("the request body was read")

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "path": path,
             "raw_path": path.encode(), "query_string": b"", "scheme": "http", "server": ("test", 80),
             "client": ("127.0.0.1", 1), "root_path": "", "headers": scope_headers}
    asyncio.run(app(scope, receive, send))
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def test_an_oversized_upload_is_refused_before_its_body_is_read(settings):
    settings.max_upload_mb = 1
    app = create_app(settings, JobStore(settings))
    form = (b"content-type", b"multipart/form-data; boundary=x")
    assert call_asgi(app, [(b"content-length", str(50 * 1024 * 1024).encode()), form]) == 413
    assert call_asgi(app, [form]) == 411  # no declared size: cannot be bounded up front


def test_a_busy_endpoint_is_retried(settings):
    answers = iter([httpx.Response(429), httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "[]"}}]})])
    naps = []
    client = LLMClient(settings, transport=httpx.MockTransport(lambda request: next(answers)), sleep=naps.append)
    assert client.chat([{"role": "user", "content": "x"}])["content"] == "[]"
    assert len(naps) == 1


def test_clients_share_one_connection_pool_and_open_it_only_when_used(settings, monkeypatch):
    made = []
    real = httpx.Client
    monkeypatch.setattr(llm_module, "_shared", None, raising=False)
    monkeypatch.setattr(llm_module.httpx, "Client", lambda *a, **k: made.append(1) or real(*a, **k))
    clients = [LLMClient(settings, sleep=lambda s: None) for _ in range(5)]
    assert made == []
    for c in clients[:2]:
        with pytest.raises(llm_module.LLMError):
            c.chat([{"role": "user", "content": "x"}])  # nothing listens on the test address
    assert made == [1]


# ---- small corrections ----------------------------------------------------------------------------

def store_for(*texts) -> FindingStore:
    return FindingStore(Inventory(kind="docx", texts=[TextUnit(id=f"t{i}", text=t, section="內文") for i, t in enumerate(texts, 1)]))


def test_a_single_place_must_be_given_with_real_positions():
    store = store_for("承辦人王小明")
    assert store.add_text("王小明", "姓名", "user", only=("t1", -3, 6)) is None
    assert store.add_text("王小明", "姓名", "user", only=("t1", 3, 99)) is None
    assert store.add_text("王小明", "姓名", "user", only=("t1", 3, 6)) is not None


def test_switching_a_finding_back_on_keeps_the_places_that_were_switched_off():
    store = store_for("王小明 與 王小明", "王小明")
    f = store.add_text("王小明", "姓名", "model")
    store.set_on(f.id, False, occurrence=1)
    store.set_on(f.id, False)
    store.set_on(f.id, True)
    assert [o.on for o in f.occurrences] == [True, False, True]


def test_switching_on_a_finding_with_every_place_off_turns_them_all_on():
    store = store_for("王小明 與 王小明")
    f = store.add_text("王小明", "姓名", "model")
    store.set_on(f.id, False, occurrence=0)
    store.set_on(f.id, False, occurrence=1)
    assert f.on is False
    store.set_on(f.id, True)
    assert [o.on for o in f.occurrences] == [True, True]


def test_a_tab_stop_definition_is_not_a_tab_character():
    xml = ('<w:p><w:pPr><w:tabs><w:tab w:val="left" w:pos="720"/></w:tabs></w:pPr>'
           '<w:r><w:t>王小明</w:t></w:r><w:r><w:tab/><w:t>經理</w:t></w:r></w:p>')
    assert parse_part(xml)[0].text == "王小明\t經理"


def test_a_quote_in_the_uploaded_file_name_is_shown_as_a_quote(tmp_path, api):
    client = api()
    job = upload(client, make_docx(tmp_path / "a.docx", ["內容"]).read_bytes(), name='報"告.docx')
    assert client.get(f"/api/jobs/{job}").json()["filename"] == '報"告.docx'


def test_single_place_positions_are_validated_by_the_api(tmp_path, api):
    client = api()
    job = upload(client, make_docx(tmp_path / "a.docx", ["承辦人王小明"]).read_bytes())
    assert wait_for(client, job)["status"] == "review"
    unit = next(iter(client.get(f"/api/jobs/{job}/review").json()["units"]))
    r = client.post(f"/api/jobs/{job}/findings", json={"text": "王小明", "type": "姓名", "all": False, "unit": unit, "start": -3, "end": 6})
    assert r.status_code == 422


# ---- the agent must not over-mask: whole screenshots, bare job titles ------------------------------

def screenshot_ctx(tmp_path, settings):
    src = make_docx(tmp_path / "a.docx", ["系統畫面如下", "總經理 王小明 核定", "本案由執行長主持"],
                    images=[("image1.png", png_with_text()), ("image2.png", png_with_text())])
    ctx = context(src, tmp_path, settings, {"王小明": "姓名"},
                  ocr=FakeOcr(by_size={(40, 30): [ocr_line("登入者：王小明")]}))
    ctx.load()
    ctx.scan_text()
    ctx.scan_images(["img1"])
    return ctx


def test_the_agent_cannot_black_out_a_whole_screenshot_for_text_that_is_already_masked_in_place(tmp_path, settings):
    ctx = screenshot_ctx(tmp_path, settings)
    res = tools.call(ctx, "add_image_mask", {"image_id": "img1", "reason": "圖片含承辦人姓名，整頁遮蔽"})
    assert res.get("ok") is False and "依位置" in res["error"]
    assert not [f for f in ctx.store.findings if f.kind == "region" and f.source == "agent"]


def test_a_whole_image_mask_is_still_accepted_for_what_only_looking_can_tell(tmp_path, settings):
    ctx = screenshot_ctx(tmp_path, settings)
    as_id = tools.call(ctx, "add_image_mask", {"image_id": "img1", "type": "證件", "reason": "身分證影本"})
    no_text = tools.call(ctx, "add_image_mask", {"image_id": "img2", "reason": "手繪的住家位置圖"})
    assert as_id.get("ok") is True and no_text.get("ok") is True
    assert len([f for f in ctx.store.findings if f.kind == "region" and f.source == "agent" and f.on]) == 2


def test_a_bare_job_title_added_by_the_agent_is_kept_as_a_suggestion_only(tmp_path, settings):
    ctx = screenshot_ctx(tmp_path, settings)
    res = tools.call(ctx, "add_findings", {"items": [{"text": "執行長", "type": "其他", "reason": "受文者職稱"},
                                                     {"text": "總經理 王小明", "type": "姓名", "reason": "職稱加姓名"}]})
    title, named = res["results"]
    assert title["accepted"] and title["on"] is False and "職稱" in title["note"]
    assert ctx.store.get(title["id"]).on is False
    assert named["accepted"] and ctx.store.get(named["id"]).on is True


def test_a_bare_job_title_reported_by_detection_is_kept_as_a_suggestion_only(tmp_path, settings):
    src = make_docx(tmp_path / "a.docx", ["本案由執行長主持", "承辦人王小明"])
    ctx = context(src, tmp_path, settings, {"執行長": "教育職業", "王小明": "姓名"})
    analyze(ctx, use_agent=False)
    by_text = {f.text: f for f in ctx.store.findings if f.kind == "text"}
    assert by_text["執行長"].on is False and "職稱" in by_text["執行長"].reason
    assert by_text["王小明"].on is True


LINK_DOC = ["會議紀錄請見 https://intra.example.org.tw/meeting/view.aspx?id=2031040077 。",
            "申請表 https://intra.example.org.tw/forms/apply.aspx?guid=0a1b2c3d-1111-4222-8333-444455556666",
            "個人頁 https://intra.example.org.tw/users/wang.xiaoming ，案件編號 77120045 另見 https://intra.example.org.tw/c?id=77120045"]


def test_a_code_the_agent_found_only_inside_links_is_kept_as_a_suggestion_only(tmp_path, settings):
    ctx = context(make_docx(tmp_path / "a.docx", LINK_DOC), tmp_path, settings)
    ctx.load()
    items = [{"text": "2031040077", "type": "其他識別碼", "reason": "連結中的文件 id"},
             {"text": "https://intra.example.org.tw/forms/apply.aspx?guid=0a1b2c3d-1111-4222-8333-444455556666", "type": "其他", "reason": "guid"},
             {"text": "77120045", "type": "其他識別碼", "reason": "案件編號"},
             {"text": "wang.xiaoming", "type": "姓名", "reason": "網址裡的帳號"}]
    doc_id, whole_link, case_number, account = tools.call(ctx, "add_findings", {"items": items})["results"]
    assert doc_id["on"] is False and "網址" in doc_id["note"] and whole_link["on"] is False
    assert case_number["on"] is True  # also written outside a link
    assert account["on"] is True  # a person's name or account is masked wherever it stands


def test_a_code_detection_found_only_inside_links_is_kept_as_a_suggestion_only(tmp_path, settings):
    ctx = context(make_docx(tmp_path / "a.docx", LINK_DOC), tmp_path, settings, {"2031040077": "其他識別碼", "77120045": "其他識別碼"})
    analyze(ctx, use_agent=False)
    by_text = {f.text: f for f in ctx.store.findings if f.kind == "text"}
    assert by_text["2031040077"].on is False and "網址" in by_text["2031040077"].reason
    assert by_text["77120045"].on is True
