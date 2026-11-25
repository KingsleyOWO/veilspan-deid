import io
import json

import pytest
from PIL import Image

from deid import imaging
from deid.imaging import (FaceDetector, OcrEngine, classify_image, is_black, paint_boxes, strip_metadata, to_browser_png)
from tests.builders import text_image
from tests.fakes import FakeLLM

BOX = (40, 30, 160, 70)


def make(fmt, mode, size=(300, 120)):
    im = Image.new("RGB", size, (200, 220, 240))
    if mode == "P":
        im = im.convert("P", palette=Image.ADAPTIVE, colors=16)
        im.info["transparency"] = 0
        out = io.BytesIO()
        im.save(out, fmt, transparency=0)
        return out.getvalue()
    out = io.BytesIO()
    im.convert(mode).save(out, fmt)
    return out.getvalue()


@pytest.mark.parametrize("fmt,mode", [("PNG", "RGB"), ("PNG", "P"), ("PNG", "RGBA"), ("JPEG", "RGB"), ("JPEG", "CMYK"),
                                      ("BMP", "RGB"), ("TIFF", "RGB"), ("GIF", "P")])
def test_paint_keeps_format_mode_and_size_and_blackens_the_box(fmt, mode):
    data = make(fmt, mode)
    before = Image.open(io.BytesIO(data))
    out = paint_boxes(data, [BOX])
    after = Image.open(io.BytesIO(out))
    assert (after.format, after.mode, after.size) == (before.format, before.mode, before.size)
    assert is_black(out, BOX)
    tol = 12 if fmt == "JPEG" else 0
    a, b = before.convert("RGB").getpixel((250, 100)), after.convert("RGB").getpixel((250, 100))
    assert all(abs(x - y) <= tol for x, y in zip(a, b))
    assert not is_black(out, (200, 80, 290, 110))


def test_paint_clamps_boxes_that_stick_out_of_the_image():
    out = paint_boxes(make("PNG", "RGB"), [(-20, -20, 50, 50), (280, 100, 400, 300)])
    assert is_black(out, (0, 0, 50, 50)) and is_black(out, (280, 100, 300, 120))


def test_painted_jpeg_carries_no_exif():
    im = Image.new("RGB", (200, 100), "white")
    exif = Image.Exif()
    exif[0x010F] = "CameraMaker"
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif)
    assert Image.open(io.BytesIO(buf.getvalue())).getexif()
    assert not Image.open(io.BytesIO(paint_boxes(buf.getvalue(), [(1, 1, 20, 20)]))).getexif()


def test_strip_metadata_removes_gps_and_leaves_clean_files_untouched():
    im = Image.new("RGB", (200, 100), "white")
    exif = Image.Exif()
    exif[0x013B] = "Artist Name"
    exif.get_ifd(0x8825)[1] = "N"
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif)
    stripped = strip_metadata(buf.getvalue())
    again = Image.open(io.BytesIO(stripped))
    assert not again.getexif() and again.size == (200, 100) and again.format == "JPEG"
    clean = make("JPEG", "RGB")
    assert strip_metadata(clean) is clean
    png = make("PNG", "RGB")
    assert strip_metadata(png) is png


def test_browser_png_converts_anything_pillow_reads():
    out = to_browser_png(make("TIFF", "RGB"))
    assert Image.open(io.BytesIO(out)).format == "PNG"


def test_face_detector_finds_nothing_on_blank_image(settings):
    assert FaceDetector(settings).detect(make("PNG", "RGB", (640, 480))) == []


def test_classify_maps_true_flags_to_labels():
    seen = {}

    def handler(messages, tools):
        seen["content"] = messages[-1]["content"]
        return {"role": "assistant", "content": json.dumps({"id_document": True, "signature_or_seal": False,
                                                           "handwriting": "true", "medical_record": True, "note": "x"})}

    labels = classify_image(FakeLLM(handler), make("PNG", "RGB", (3000, 200)))
    assert labels == ["證件", "病歷"]
    url = next(p for p in seen["content"] if p["type"] == "image_url")["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    import base64
    sent = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert max(sent.size) == 1280
    assert any(p["type"] == "text" and "只輸出 JSON 物件" in p["text"] for p in seen["content"])


def test_classify_returns_nothing_on_unparseable_reply():
    assert classify_image(FakeLLM(lambda m, t: {"role": "assistant", "content": "看不出來"}), make("PNG", "RGB")) == []


@pytest.mark.ocr
def test_ocr_reads_text_with_one_box_per_character(settings):
    lines = OcrEngine(settings).read(text_image(["登入者：王小明 0912-345-678"]))
    hit = next(ln for ln in lines if "王小明" in ln.text)
    assert len(hit.chars) == len(hit.text)
    i = hit.text.index("王小明")
    x0, y0, x1, y1 = hit.chars[i]
    assert x0 < x1 and y0 < y1 and hit.box[0] <= x0 and x1 <= hit.box[2] + 1
    assert hit.chars[i][0] < hit.chars[i + 2][0]


@pytest.mark.ocr
def test_ocr_downscales_huge_images_and_reports_original_coordinates(settings):
    data = text_image(["王小明"], size=(5000, 300), font_size=90, origin=(4400, 60))
    lines = OcrEngine(settings).read(data)
    hit = next(ln for ln in lines if "王小明" in ln.text)
    assert 4300 < hit.box[0] < hit.box[2] <= 5000


@pytest.mark.ocr
def test_ocr_reads_long_screenshots_by_tiling(settings):
    data = text_image(["第一行 王小明", "x", "x", "x", "x", "x", "x", "x", "x", "x", "x", "x", "x", "x", "x", "x", "x", "x", "x", "x",
                       "x", "x", "x", "x", "x", "x", "x", "x", "x", "x", "x", "x", "x", "x", "x", "x", "x", "x", "x", "x",
                       "x", "x", "x", "x", "x", "x", "x", "x", "x", "最後一行 陳美玲"], size=(600, 2800))
    lines = OcrEngine(settings).read(data)
    texts = [ln.text for ln in lines]
    assert sum("王小明" in t for t in texts) == 1 and sum("陳美玲" in t for t in texts) == 1
    last = next(ln for ln in lines if "陳美玲" in ln.text)
    assert last.box[1] > 2600
