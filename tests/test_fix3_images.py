"""Pictures: what a camera or editor wrote into them, and the black box over a string read from one.
Everything here is invented data. Each test failed before its fix unless it says otherwise."""
import io
import struct
import zipfile
import zlib

import numpy as np
import pymupdf
import pytest
from PIL import Image, ImageCms, ImageDraw, PngImagePlugin, TiffImagePlugin

from deid import imaging
from deid.findings import find_in_ocr
from deid.formats.pdf import PdfAdapter
from deid.formats.xlsx import XlsxAdapter
from deid.model import MaskPlan
from tests.builders import cjk_font, make_xlsx, text_image

PLANTED = ("WangXiaoming", "LinZhihao")  # every test hides one of these in the picture's metadata
EMPTY = MaskPlan(text={}, meta={}, boxes={}, strings=[])
SRGB = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()

# Tags of TIFF 6.0 a picture needs to be decoded and shown the right way (spec, sections 8-23): nothing else may stay.
TIFF_STRUCTURE = {254, 256, 257, 258, 259, 262, 266, 273, 274, 277, 278, 279, 282, 283, 284, 296, 317, 320, 322, 323,
                  324, 325, 338, 339, 347, 530, 531, 532, 34675}


# ---- independent checks ----------------------------------------------------------------------------

def leaks(data: bytes) -> list[str]:
    """Planted strings found anywhere in the raw bytes, in the encodings metadata uses."""
    return [s for s in PLANTED if any(f in data for f in (s.encode(), s.encode("utf-16-le"), s.encode("utf-16-be")))]


def pillow_metadata(data: bytes) -> list:
    """What Pillow reports as metadata in a picture (orientation and the TIFF structure tags excepted)."""
    im = Image.open(io.BytesIO(data))
    found = []
    exif = im.getexif()
    for tag in exif:
        if tag != 0x0112 and not (im.format == "TIFF" and tag in TIFF_STRUCTURE):
            found.append(("exif", tag))
    for ifd in (0x8769, 0x8825, 0xA005):
        if exif.get_ifd(ifd):
            found.append(("ifd", ifd))
    if im.format == "TIFF":
        found += [("tiff", t) for t in im.tag_v2 if t not in TIFF_STRUCTURE]
    blocked = {"exif", "comment", "xmp", "XML:com.adobe.xmp", "photoshop", "iptc", "extension", "thumbnail"}
    found += [("info", k) for k in im.info if k in blocked]
    if im.format == "PNG":
        im.load()
        found += [("text", k) for k in getattr(im, "text", {})]
    return found


def pixels(data: bytes) -> list:
    im = Image.open(io.BytesIO(data))
    frames = []
    for k in range(getattr(im, "n_frames", 1)):
        im.seek(k)
        frames.append((im.mode, im.size, im.tobytes()))
    return frames


def assert_clean(src: bytes, out: bytes, *, same_pixels=True):
    assert leaks(src), "the test picture must carry the planted strings"
    assert out is not src
    assert not leaks(out)
    assert not pillow_metadata(out)
    if same_pixels:
        assert pixels(out) == pixels(src)
    assert imaging.strip_metadata(out) is out  # what the adapters' check relies on


def photo(size=(96, 64), mode="RGB") -> Image.Image:
    im = Image.new("RGB", size, (120, 170, 210))
    d = ImageDraw.Draw(im)
    for x in range(0, size[0], 6):
        d.line((x, 0, x, size[1]), fill=(x * 2 % 256, 90, 160))
    d.ellipse((size[0] // 4, size[1] // 6, size[0] * 3 // 4, size[1] * 5 // 6), fill=(250, 210, 180))
    return im.convert(mode)


def save(im: Image.Image, fmt: str, **kw) -> bytes:
    out = io.BytesIO()
    im.save(out, fmt, **kw)
    return out.getvalue()


def camera_exif(orientation=None) -> bytes:
    exif = Image.Exif()
    exif[0x013B] = "WangXiaoming"  # Artist
    exif[0x010E] = "home of WangXiaoming"  # ImageDescription
    exif[0x8825] = {1: "N", 2: (25.0, 2.0, 30.0), 3: "E", 4: (121.0, 33.0, 5.0)}  # GPS
    if orientation:
        exif[0x0112] = orientation
    return exif.tobytes()


def jpeg_segments(data: bytes) -> list[tuple[int, int, int]]:
    """(marker, start, end) of each segment up to and including the first scan header."""
    out, i = [], 2
    while data[i] == 0xFF:
        marker, length = data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
        out.append((marker, i, i + 2 + length))
        if marker == 0xDA:
            break
        i += 2 + length
    return out


def segment(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


def camera_jpeg(**kw) -> bytes:
    return save(photo(), "JPEG", exif=camera_exif(kw.pop("orientation", None)), quality=90, **kw)


def after_segment(data: bytes, marker: int, insert: bytes) -> bytes:
    end = next(e for m, _s, e in jpeg_segments(data) if m == marker)
    return data[:end] + insert + data[end:]


# ---- JPEG --------------------------------------------------------------------------------------------

def test_jpeg_with_a_stray_byte_between_segments_loses_its_metadata():
    """The review's case: one padding byte after the EXIF block. Pillow and viewers skip it; the stripper gave up."""
    src = after_segment(camera_jpeg(), 0xE1, b"\x00")
    assert imaging.image_info(src)[0] == "JPEG"
    assert Image.open(io.BytesIO(src)).getexif().get_ifd(0x8825)
    out = imaging.strip_metadata(src)
    assert_clean(src, out)


def test_jpeg_with_text_and_fill_bytes_between_segments_loses_them():
    src = after_segment(camera_jpeg(), 0xDB, b"LinZhihao was here\xff\xff")  # decoders skip both
    assert Image.open(io.BytesIO(src)).size == (96, 64)
    assert_clean(src, imaging.strip_metadata(src))


def test_jpeg_thumbnail_inside_the_jfif_header_is_dropped():
    data = save(photo(), "JPEG", quality=90, dpi=(150, 150))
    m, s, e = jpeg_segments(data)[0]
    assert m == 0xE0 and data[s + 4:s + 9] == b"JFIF\x00"
    thumb = b"WangXiaoming" * 2  # a 4 x 2 RGB thumbnail
    jfif = data[s + 4:s + 16] + bytes([4, 2]) + thumb
    src = data[:s] + segment(0xE0, jfif) + data[e:]
    assert Image.open(io.BytesIO(src)).info["dpi"] == (150, 150)
    out = imaging.strip_metadata(src)
    assert out is not src and not leaks(out)
    assert pixels(out) == pixels(src)
    assert Image.open(io.BytesIO(out)).info["dpi"] == (150, 150)  # the density is not metadata: kept
    assert imaging.strip_metadata(out) is out


def test_jpeg_keeps_the_orientation_viewers_use():
    """Viewers and Pillow take the first EXIF block, and none after the picture data starts; so must the copy."""
    data = save(photo(), "JPEG", quality=90)
    first = segment(0xE1, camera_exif(orientation=6))
    second = segment(0xE1, camera_exif(orientation=3))
    two = data[:2] + first + second + data[2:]
    assert Image.open(io.BytesIO(two)).getexif()[0x0112] == 6
    out = imaging.strip_metadata(two)
    assert dict(Image.open(io.BytesIO(out)).getexif()) == {0x0112: 6} and not leaks(out)

    late = after_segment(data, 0xDA, b"")  # unchanged picture; now put an EXIF block after the scan
    eoi = late.rindex(b"\xff\xd9")
    late = late[:eoi] + segment(0xE1, camera_exif(orientation=8)) + late[eoi:]
    assert Image.open(io.BytesIO(late)).getexif().get(0x0112) is None
    out = imaging.strip_metadata(late)
    assert Image.open(io.BytesIO(out)).getexif().get(0x0112) in (None, 1) and not leaks(out)
    assert pixels(out) == pixels(late)


def test_jpeg_adobe_block_keeps_only_the_colour_transform():
    data = save(photo(mode="CMYK"), "JPEG", quality=90)
    m, s, e = next(seg for seg in jpeg_segments(data) if seg[0] == 0xEE)
    payload = data[s + 4:e]
    assert payload[:5] == b"Adobe"
    src = data[:s] + segment(0xEE, payload + b"WangXiaoming") + data[e:]
    before = Image.open(io.BytesIO(src))
    assert before.info.get("adobe_transform") is not None
    out = imaging.strip_metadata(src)
    assert out is not src and not leaks(out)
    assert pixels(out) == pixels(src)
    assert Image.open(io.BytesIO(out)).info.get("adobe_transform") == before.info["adobe_transform"]


def test_jpeg_cut_short_after_its_picture_data_still_loses_its_metadata():
    """It does not open (Pillow: truncated); the adapters list it as not checked. What it carries goes anyway."""
    data = camera_jpeg()
    src = data[:len(data) - 40]
    with pytest.raises(OSError):
        Image.open(io.BytesIO(src)).load()
    out = imaging.strip_metadata(src)
    assert leaks(src) and not leaks(out)
    assert imaging.strip_metadata(out) is out


# ---- PNG ---------------------------------------------------------------------------------------------

def png_chunk(kind: bytes, body: bytes) -> bytes:
    return len(body).to_bytes(4, "big") + kind + body + zlib.crc32(kind + body).to_bytes(4, "big")


def camera_png(**kw) -> bytes:
    info = PngImagePlugin.PngInfo()
    info.add_text("Author", "WangXiaoming")
    info.add_text("Comment", "LinZhihao", zip=True)
    info.add_itxt("Description", "WangXiaoming at home")
    return save(photo(), "PNG", pnginfo=info, dpi=(96, 96), exif=camera_exif(), **kw)


def test_png_without_an_end_chunk_loses_its_text():
    src = camera_png()
    assert src.endswith(png_chunk(b"IEND", b""))
    src = src[:-12]
    assert Image.open(io.BytesIO(src)).size == (96, 64)
    assert_clean(src, imaging.strip_metadata(src))


def test_png_with_junk_where_the_end_chunk_should_be_loses_its_text():
    src = camera_png()[:-12] + b"\x00\x00\x00\x09junk LinZhihao"
    assert Image.open(io.BytesIO(src)).size == (96, 64)
    assert_clean(src, imaging.strip_metadata(src))


def test_png_suggested_palette_name_is_dropped():
    data = save(photo(), "PNG")
    at = data.index(b"IDAT") - 4
    splt = png_chunk(b"sPLT", b"WangXiaoming\x00\x08" + bytes([10, 20, 30, 255, 0, 1]))
    src = data[:at] + splt + data[at:]
    assert Image.open(io.BytesIO(src)).size == (96, 64)
    assert_clean(src, imaging.strip_metadata(src))


# ---- GIF ---------------------------------------------------------------------------------------------

def camera_gif() -> bytes:
    return save(photo(mode="P"), "GIF", comment=b"WangXiaoming")


def test_gif_without_a_trailer_loses_its_comment():
    src = camera_gif()
    assert src.endswith(b"\x3b")
    src = src[:-1]
    assert Image.open(io.BytesIO(src)).size == (96, 64)
    assert_clean(src, imaging.strip_metadata(src))


def test_gif_stray_bytes_between_blocks_are_dropped():
    data = save(photo(mode="P"), "GIF")
    at = data.index(b"\x2c", 13 + 3 * (2 << (data[10] & 7)))
    src = data[:at] + b"LinZhihao" + data[at:]  # Pillow skips bytes that start no block
    assert pixels(src) == pixels(data)
    assert_clean(src, imaging.strip_metadata(src))


def test_gif_plain_text_and_unknown_extensions_are_dropped():
    data = save(photo(mode="P"), "GIF")
    at = data.index(b"\x2c", 13 + 3 * (2 << (data[10] & 7)))
    plain = b"\x21\x01\x0c" + bytes(12) + b"\x0cWangXiaoming\x00"
    unknown = b"\x21\x99\x09LinZhihao\x00"
    src = data[:at] + plain + unknown + data[at:]
    assert pixels(src) == pixels(data)
    assert_clean(src, imaging.strip_metadata(src))


# ---- BMP ---------------------------------------------------------------------------------------------

def bmp_v5(cs_type: bytes, profile: bytes = b"") -> bytes:
    """A 24-bit bottom-up BMP with a BITMAPV5HEADER; the profile (linked file name or embedded ICC) follows the pixels."""
    w, h = 6, 4
    stride = (w * 3 + 3) // 4 * 4
    rows = b"".join(b"".join(bytes([x * 40, y * 60, 200]) for x in range(w)).ljust(stride, b"\0") for y in range(h))
    off = 14 + 124
    prof_at = 124 + len(rows)  # from the start of the info header
    head = struct.pack("<IiiHHIIiiII", 124, w, h, 1, 24, 0, len(rows), 2835, 2835, 0, 0)
    head += bytes(16) + cs_type + bytes(36) + bytes(12) + struct.pack("<IIII", 4, prof_at if profile else 0, len(profile), 0)
    assert len(head) == 124
    body = head + rows + profile
    return b"BM" + struct.pack("<IHHI", 14 + len(body), 0, 0, off) + body


def test_bmp_with_data_after_the_picture_loses_it():
    data = save(photo(), "BMP")
    src = data + b"WangXiaoming"
    assert Image.open(io.BytesIO(src)).size == (96, 64)
    assert_clean(src, imaging.strip_metadata(src))


def test_bmp_with_a_gap_before_the_pixels_loses_it():
    data = save(photo(), "BMP")
    off = int.from_bytes(data[10:14], "little")
    gap = b"LinZhihao gap"
    src = bytearray(data[:off] + gap + data[off:])
    src[10:14] = (off + len(gap)).to_bytes(4, "little")
    src[2:6] = len(src).to_bytes(4, "little")
    src = bytes(src)
    assert pixels(src) == pixels(data)
    assert_clean(src, imaging.strip_metadata(src))


def test_bmp_with_an_os2_header_is_written_again_without_its_extra_fields():
    """A 64-byte OS/2 header: Pillow reads it, the rebuild does not cover it, so the last resort writes the picture
    again. BMP is written without loss: the pixels are the same."""
    data = save(photo(), "BMP")
    head = bytearray(data[14:54] + b"WangXiaoming".ljust(24, b"\x00"))
    head[0:4] = (64).to_bytes(4, "little")
    src = b"BM" + struct.pack("<IHHI", 14 + 64 + len(data) - 54, 0, 0, 14 + 64) + bytes(head) + data[54:]
    assert pixels(src) == pixels(data)
    assert_clean(src, imaging.strip_metadata(src))


def test_bmp_linked_profile_path_is_dropped_and_an_embedded_profile_kept():
    linked = bmp_v5(b"KNIL", b"C:\\Users\\WangXiaoming\\screen.icc\x00")  # 'LINK', stored little-endian
    assert Image.open(io.BytesIO(linked)).size == (6, 4)
    out = imaging.strip_metadata(linked)
    assert_clean(linked, out)
    embedded = bmp_v5(b"DEBM", SRGB)  # 'MBED'
    out = imaging.strip_metadata(embedded)
    assert SRGB in out and pixels(out) == pixels(embedded)


# ---- TIFF --------------------------------------------------------------------------------------------

def tiff_tags() -> TiffImagePlugin.ImageFileDirectory_v2:
    info = TiffImagePlugin.ImageFileDirectory_v2()
    info[315] = "WangXiaoming"  # Artist
    info[270] = "home of WangXiaoming"  # ImageDescription
    info[271] = "LinZhihao Camera"  # Make
    info[305] = "LinZhihao Editor"  # Software
    info[306] = "2026:11:24 10:00:00"  # DateTime
    info[33432] = "(c) WangXiaoming"  # Copyright
    info[700] = b"<x:xmpmeta><dc:creator>WangXiaoming</dc:creator></x:xmpmeta>"  # XMP
    return info


@pytest.mark.parametrize("mode,compression", [("RGB", None), ("RGB", "tiff_lzw"), ("RGB", "tiff_adobe_deflate"),
                                              ("RGB", "packbits"), ("RGB", "jpeg"), ("L", None), ("1", "group4"),
                                              ("P", "tiff_lzw"), ("RGBA", None), ("CMYK", None), ("I;16B", None)])
def test_tiff_tags_are_removed_and_the_picture_is_untouched(mode, compression):
    """The review's second case, in the colour modes and compressions Pillow writes (I;16B is big-endian)."""
    kw = {"compression": compression} if compression else {}
    src = save(photo(mode=mode), "TIFF", tiffinfo=tiff_tags(), dpi=(300, 300), **kw)
    assert imaging.image_info(src)[0] == "TIFF"
    out = imaging.strip_metadata(src)
    assert_clean(src, out)
    assert Image.open(io.BytesIO(out)).info.get("dpi") == (300, 300)
    if compression:
        assert Image.open(io.BytesIO(out)).info["compression"] == compression


def test_tiff_keeps_its_colour_profile_and_orientation():
    info = tiff_tags()
    info[274] = 6
    src = save(photo(), "TIFF", tiffinfo=info, icc_profile=SRGB)
    out = imaging.strip_metadata(src)
    assert_clean(src, out)
    im = Image.open(io.BytesIO(out))
    assert im.info.get("icc_profile") == SRGB and im.tag_v2.get(274) == 6


def tiff_ifd(tags: dict, at: int, e: str) -> bytes:
    n = len(tags)
    extra_at = at + 2 + 12 * n + 4
    head, extra = bytearray(struct.pack(e + "H", n)), bytearray()
    for tag in sorted(tags):
        typ, val = tags[tag]
        if typ in (2, 7):
            raw, count = bytes(val), len(val)
        elif typ == 5:
            raw, count = struct.pack(e + "%dI" % len(val), *val), len(val) // 2
        else:
            raw, count = struct.pack(e + {1: "B", 3: "H", 4: "I", 13: "I"}[typ] * len(val), *val), len(val)
        if len(raw) <= 4:
            head += struct.pack(e + "HHI", tag, typ, count) + raw.ljust(4, b"\0")
        else:
            head += struct.pack(e + "HHII", tag, typ, count, extra_at + len(extra))
            extra += raw + (b"\0" if len(raw) % 2 else b"")
    return bytes(head + bytes(4) + extra)


def build_tiff(pages: list[dict], order="<", gap=b"", trailer=b"") -> bytes:
    """A classic TIFF put together by hand, for layouts Pillow does not write: header, `gap` (referenced by nothing),
    the picture data, then the directories at the end. A page: {"tags": {tag: (type, values)}, "blocks": [bytes],
    "tiled": bool, "sub": {tag: page}} where `sub` are directories a tag points to (EXIF, GPS, SubIFDs)."""
    e = order
    out = bytearray((b"II*\x00" if e == "<" else b"MM\x00*") + bytes(4)) + gap

    def place(page) -> dict:
        nonlocal out
        offs = []
        for blk in page.get("blocks", []):
            out += b"\0" * (len(out) % 2)
            offs.append(len(out))
            out += blk
        tags = dict(page["tags"])
        if page.get("blocks"):
            tags[324 if page.get("tiled") else 273] = (4, offs)
            tags[325 if page.get("tiled") else 279] = (4, [len(b) for b in page["blocks"]])
        for tag, sub in page.get("sub", {}).items():
            sub_tags = place(sub)
            out += b"\0" * (len(out) % 2)
            at = len(out)
            out += tiff_ifd(sub_tags, at, e)
            tags[tag] = (4, [at])
        return tags

    link = 4
    for page in pages:
        tags = place(page)
        out += b"\0" * (len(out) % 2)
        at = len(out)
        struct.pack_into(e + "I", out, link, at)
        out += tiff_ifd(tags, at, e)
        link = at + 2 + 12 * len(tags)
    return bytes(out + trailer)


def rgb_tags(w, h, extra=None) -> dict:
    tags = {256: (3, [w]), 257: (3, [h]), 258: (3, [8, 8, 8]), 259: (3, [1]), 262: (3, [2]), 277: (3, [3]),
            278: (3, [h]), 284: (3, [1])}
    tags.update(extra or {})
    return tags


def test_tiff_with_its_directory_at_the_end_tiles_and_orphaned_bytes_loses_them():
    """Directories after the pixels, tiles instead of strips, bytes nothing points to (an editor's leftover copy of
    the old tags), data after the end, a big-endian file, and the EXIF and GPS directories."""
    w, h, t = 40, 24, 16
    im = photo((w, h))
    tiles = []
    for ty in range(0, h, t):
        for tx in range(0, w, t):
            tiles.append(im.crop((tx, ty, tx + t, ty + t)).tobytes())  # crop pads past the edge with black
    exif = {"tags": {0xA430: (2, b"WangXiaoming\x00")}}  # CameraOwnerName
    gps = {"tags": {1: (2, b"N\x00"), 0x1B: (7, b"ASCII\x00\x00\x00LinZhihao")}}  # GPSProcessingMethod
    page = {"tags": rgb_tags(w, h, {322: (3, [t]), 323: (3, [t]), 315: (2, b"WangXiaoming\x00")}),
            "blocks": tiles, "tiled": True, "sub": {34665: exif, 34853: gps}}
    for order in ("<", ">"):
        src = build_tiff([page], order, gap=b"Artist=LinZhihao\x00", trailer=b"WangXiaoming")
        before = Image.open(io.BytesIO(src))
        assert before.size == (w, h) and before.getexif().get_ifd(0x8825)
        assert pixels(src)[0][2] == im.tobytes()
        assert_clean(src, imaging.strip_metadata(src))


def test_tiff_thumbnail_in_a_sub_directory_is_dropped():
    w, h = 32, 20
    thumb = {"tags": rgb_tags(4, 2, {254: (4, [1])}), "blocks": [b"WangXiaomingWangXiaoming"]}  # 4 x 2 RGB
    src = build_tiff([{"tags": rgb_tags(w, h), "blocks": [photo((w, h)).tobytes()], "sub": {330: thumb}}])
    assert Image.open(io.BytesIO(src)).size == (w, h)
    assert_clean(src, imaging.strip_metadata(src))


def test_tiff_jpeg_strip_with_a_comment_is_cleaned():
    """A JPEG-compressed TIFF whose strip carries a comment block of its own."""
    w, h = 48, 32
    jpeg = save(photo((w, h), "L"), "JPEG", quality=90)
    strip = jpeg[:2] + segment(0xFE, b"WangXiaoming") + jpeg[2:]
    tags = {256: (3, [w]), 257: (3, [h]), 258: (3, [8]), 259: (3, [7]), 262: (3, [1]), 277: (3, [1]), 278: (3, [h])}
    src = build_tiff([{"tags": tags, "blocks": [strip]}])
    assert Image.open(io.BytesIO(src)).size == (w, h)
    assert_clean(src, imaging.strip_metadata(src))


def test_tiff_without_strip_sizes_is_written_again_without_loss():
    """No StripByteCounts: Pillow still reads it; the rebuild needs them, so the picture is written again, compressed
    without loss."""
    w, h = 32, 20
    raw = photo((w, h)).tobytes()
    out = bytearray(b"II*\x00" + bytes(4)) + raw
    at = len(out)
    struct.pack_into("<I", out, 4, at)
    out += tiff_ifd(rgb_tags(w, h, {315: (2, b"WangXiaoming\x00"), 273: (4, [8])}), at, "<")
    src = bytes(out)
    assert pixels(src)[0][2] == raw
    assert_clean(src, imaging.strip_metadata(src))


def test_multi_page_tiff_keeps_every_page_without_metadata():
    """Not a supported picture (more than one frame: listed as not checked), but its pages are not lost either."""
    src = save(photo(), "TIFF", save_all=True, append_images=[photo().rotate(90)], tiffinfo=tiff_tags())
    assert Image.open(io.BytesIO(src)).n_frames == 2
    assert_clean(src, imaging.strip_metadata(src))


def test_bigtiff_loses_its_tags():
    src = save(photo(), "TIFF", big_tiff=True, tiffinfo=tiff_tags())
    assert imaging.image_info(src)[0] == "TIFF"
    assert_clean(src, imaging.strip_metadata(src))


# ---- what the callers rely on ----------------------------------------------------------------------

def test_nothing_to_remove_gives_back_the_same_object():
    """The identity part held before too (BMP and TIFF were not touched at all); it guards that the rebuilds leave a
    clean picture alone. metadata_unknown is new."""
    for data in (save(photo(), "JPEG"), save(photo(), "PNG"), save(photo(mode="P"), "GIF"), save(photo(), "BMP"),
                 imaging.strip_metadata(save(photo(), "TIFF", tiffinfo=tiff_tags()))):
        assert imaging.strip_metadata(data) is data
        assert not imaging.metadata_unknown(data)


def test_a_picture_it_cannot_read_through_is_flagged():
    """Neither the structure can be followed nor the picture decoded: it comes back as it is, and says so."""
    data = save(photo(), "PNG")
    at = data.index(b"IDAT") - 4
    broken = data[:at] + b"??" + png_chunk(b"tEXt", b"Author\x00WangXiaoming") + data[at:]
    with pytest.raises(Exception):
        Image.open(io.BytesIO(broken)).load()
    assert imaging.metadata_unknown(broken)
    for clean in (data, imaging.strip_metadata(camera_jpeg()), b"\x01\x00\x00\x00 an EMF picture"):
        assert not imaging.metadata_unknown(clean)


def review_workbook(tmp_path):
    """The review's workbook: a padded camera JPEG and a TIFF with tags next to an ordinary sheet."""
    src = make_xlsx(tmp_path / "k.xlsx", [{"name": "名單", "rows": [["姓名"], ["王小明"]]}])
    padded = after_segment(camera_jpeg(), 0xE1, b"\x00")
    tif = save(photo(), "TIFF", tiffinfo=tiff_tags())
    with zipfile.ZipFile(src, "a") as z:
        z.writestr("xl/media/image2.jpeg", padded)
        z.writestr("xl/media/image4.tiff", tif)
    return src


def test_workbook_pictures_come_out_without_metadata(tmp_path, settings):
    src = review_workbook(tmp_path)
    inv = XlsxAdapter().parse(src)
    assert {(i.name, i.supported) for i in inv.images} >= {("xl/media/image2.jpeg", True), ("xl/media/image4.tiff", True)}
    out = tmp_path / "out.xlsx"
    XlsxAdapter().apply(src, out, EMPTY, settings)
    with zipfile.ZipFile(out) as z:
        for name in ("xl/media/image2.jpeg", "xl/media/image4.tiff"):
            data = z.read(name)
            assert not leaks(data) and not pillow_metadata(data), name
    assert XlsxAdapter().verify(src, out, EMPTY, settings) == []


def test_the_check_notices_metadata_left_in_a_padded_jpeg_or_a_tiff(tmp_path, settings):
    """The verification re-strips each output picture; it used to call these two clean."""
    src = review_workbook(tmp_path)
    problems = XlsxAdapter().verify(src, src, EMPTY, settings)
    assert any("image2.jpeg" in p for p in problems) and any("image4.tiff" in p for p in problems)


def test_pdf_photo_with_a_stray_byte_loses_its_metadata(tmp_path, settings):
    padded = after_segment(camera_jpeg(), 0xE1, b"\x00")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(pymupdf.Rect(50, 50, 290, 210), stream=padded)
    src = tmp_path / "photo.pdf"
    doc.save(src)
    doc.close()

    def photos(path):
        d = pymupdf.open(path)
        try:
            return [d.xref_stream_raw(x) for x in range(1, d.xref_length())
                    if d.xref_is_stream(x) and "DCTDecode" in (d.xref_get_key(x, "Filter")[1] or "")]
        finally:
            d.close()

    assert any(leaks(p) for p in photos(src))
    out = tmp_path / "out.pdf"
    PdfAdapter().apply(src, out, EMPTY, settings)
    kept = photos(out)
    assert kept and not any(leaks(p) for p in kept) and not any(pillow_metadata(p) for p in kept)


# ---- the black box over a string read from a picture ---------------------------------------------

def ink_masks(line: str, font_size: int, size, origin=(30, 30)) -> list:
    """Each character's own ink in a picture made by text_image (one line), rendered alone where text_image puts it."""
    font = cjk_font(font_size)
    masks = []
    for k, ch in enumerate(line):
        if ch.isspace():
            masks.append(None)
            continue
        one = Image.new("L", size, 255)
        ImageDraw.Draw(one).text((origin[0] + font.getlength(line[:k]), origin[1]), ch, font=font, fill=0)
        masks.append(np.asarray(one) < 160)
    return masks


def painted_area(lines, needle: str, size) -> np.ndarray:
    """Where the black boxes for `needle` go: the boxes the review screen shows, as paint_boxes rounds them."""
    hits = find_in_ocr(lines, needle)
    assert hits, f"{needle} not read"
    area = np.zeros((size[1], size[0]), dtype=bool)
    for box, _near in hits:
        c = imaging._clamp(box, size[0], size[1])
        area[c[1]:c[3], c[0]:c[2]] = True
    return area


def check_box(settings, line: str, needle: str, font_size: int, size=(1000, 200)):
    data = text_image([line], size=size, font_size=font_size)
    masks = ink_masks(line, font_size, size)
    picture = np.asarray(Image.open(io.BytesIO(data)).convert("L")) < 160
    start = line.index(needle)
    own = np.zeros_like(picture)
    for m in masks[start:start + len(needle)]:
        if m is not None:
            own |= m
    assert (own & picture).sum() >= 0.95 * own.sum()  # the rendering above is where the picture has its ink
    area = painted_area(imaging.OcrEngine(settings).read(data), needle, size)
    left = own & ~area
    assert not left.any(), f"{needle} at {font_size}px: {int(left.sum())} pixels of ink outside the box"
    covered = [float((m & area).sum()) / m.sum() for k, m in enumerate(masks)
               if m is not None and not start <= k < start + len(needle)]
    return covered


@pytest.mark.ocr
@pytest.mark.parametrize("font_size", [34, 48, 72])
def test_box_over_a_name_read_from_a_picture_covers_its_first_stroke(settings, font_size):
    """The review's case: a sliver of the first character (王) stood outside the box. The recogniser's own box for
    each character can start inside the glyph."""
    for line in ("姓名：王小明", "登入者：王小明"):
        covered = check_box(settings, line, "王小明", font_size)
        assert max(covered) < 0.35  # a neighbour may lose a sliver, never most of itself


@pytest.mark.ocr
@pytest.mark.parametrize("font_size", [26, 34])
def test_box_over_a_name_in_a_line_with_spaces_is_where_the_name_is(settings, font_size):
    """A space in the line made the boxes fall back to an even split of the whole line: half-width letters and digits
    pulled the boxes of the name tens of pixels away from it."""
    covered = check_box(settings, "聯絡人 陳美玲 A123456789", "陳美玲", font_size)
    assert max(covered) < 0.35
