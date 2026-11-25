"""Third tier of the review of 2026-11-23, pictures: what an independent check of the new metadata stripper found.
A cleaned picture must show what the original showed, whatever is wrong with the file: where the careful way of
taking metadata out does not manage that, the picture is written again from its pixels instead; and nothing a
damaged picture does may stop the work. All content is made up."""
import io
import random
import struct
import warnings

import pytest
from PIL import Image, ImageDraw

from deid import imaging
from deid.formats.base import adapter_for
from tests.builders import make_docx

PLANT = "Wang-Xiaoming-PLANTED"


def photo(size=(96, 64)) -> Image.Image:
    im = Image.new("RGB", size, (120, 170, 210))
    draw = ImageDraw.Draw(im)
    draw.ellipse((20, 10, 70, 55), fill=(250, 220, 190))
    draw.line((0, 0, size[0], size[1]), fill=(20, 40, 60), width=3)
    return im


def jpeg(**kw) -> bytes:
    exif = Image.Exif()
    exif[0x013B] = PLANT
    out = io.BytesIO()
    photo().save(out, "JPEG", exif=exif.tobytes(), comment=PLANT.encode(), quality=90, **kw)
    return out.getvalue()


def frames(data: bytes):
    """(mode, size, pixels) of every frame, or None when Pillow cannot decode all of it."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            im = Image.open(io.BytesIO(data))
            out = []
            for k in range(getattr(im, "n_frames", 1)):
                im.seek(k)
                im.load()
                out.append((im.mode, im.size, im.tobytes()))
            return out
        except Exception:
            return None


def scan_start(data: bytes) -> int:
    """Where the compressed picture data of a JPEG begins (after the first scan header)."""
    i = 2
    while True:
        marker, length = data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
        i += 2 + length
        if marker == 0xDA:
            return i


def test_a_jpeg_with_a_marker_in_the_middle_of_its_picture_data_still_opens_after_cleaning():
    good = jpeg()
    at = scan_start(good) + 40
    # four bytes of damage that read as the start of a metadata block running past the end of the file
    damaged = good[:at] + b"\xff\xe9\xd1\xb2" + good[at + 4:]
    before = frames(damaged)
    assert before is not None, "the damaged picture is one Pillow still opens"
    out = imaging.strip_metadata(damaged)
    assert PLANT.encode() not in out
    assert frames(out) == before


def test_a_jpeg_that_cannot_be_cleaned_block_by_block_is_written_again_from_its_pixels(monkeypatch):
    good = jpeg()
    before = Image.open(io.BytesIO(good)).convert("RGB")
    monkeypatch.setitem(imaging._STRIP, "JPEG", lambda data: (data[:300], True))  # a cleaner that loses the picture
    out = imaging.strip_metadata(good)
    after = Image.open(io.BytesIO(out)).convert("RGB")
    assert PLANT.encode() not in out and after.size == before.size
    gaps = [abs(a - b) for a, b in zip(before.tobytes(), after.tobytes())]
    # written again with the picture's own tables: under one level in 255 on average (measured 0.85 on this
    # picture of hard colour edges, 0.01 on a photo-like one), up to 15 at an edge
    assert sum(gaps) / len(gaps) < 1.5 and max(gaps) <= 24, (sum(gaps) / len(gaps), max(gaps))
    assert not imaging.metadata_unknown(good)


def test_a_sideways_jpeg_written_again_still_displays_upright(monkeypatch):
    exif = Image.Exif()
    exif[0x0112] = 6
    buf = io.BytesIO()
    photo().save(buf, "JPEG", exif=exif.tobytes(), comment=PLANT.encode())
    monkeypatch.setitem(imaging._STRIP, "JPEG", lambda data: (data[:300], True))
    out = imaging.strip_metadata(buf.getvalue())
    assert imaging.image_info(out)[1:3] == (64, 96) and PLANT.encode() not in out


def tiff_with_a_second_directory_without_a_width() -> bytes:
    buf = io.BytesIO()
    photo((32, 24)).save(buf, "TIFF", save_all=True, append_images=[photo((32, 24))])
    data = bytearray(buf.getvalue())
    order = "<" if data[:2] == b"II" else ">"
    first = struct.unpack(order + "I", data[4:8])[0]
    count = struct.unpack(order + "H", data[first:first + 2])[0]
    second = struct.unpack(order + "I", data[first + 2 + 12 * count:first + 6 + 12 * count])[0]
    count = struct.unpack(order + "H", data[second:second + 2])[0]
    for k in range(count):
        at = second + 2 + 12 * k
        if struct.unpack(order + "H", data[at:at + 2])[0] == 256:  # ImageWidth
            data[at:at + 2] = struct.pack(order + "H", 65000)
    return bytes(data)


def test_a_tiff_whose_second_page_has_no_size_does_not_stop_the_cleaning():
    damaged = tiff_with_a_second_directory_without_a_width()
    with pytest.raises(TypeError):  # what Pillow does with it, and what used to come straight through
        Image.open(io.BytesIO(damaged)).n_frames
    out = imaging.strip_metadata(damaged)
    assert isinstance(out, bytes)
    assert isinstance(imaging.metadata_unknown(damaged), bool)


def test_a_document_with_such_a_tiff_is_still_read_and_masked(tmp_path, settings):
    src = make_docx(tmp_path / "a.docx", ["承辦人 王小明"], images=[("image1.tiff", tiff_with_a_second_directory_without_a_width())])
    ad = adapter_for(src, settings)
    inv = ad.parse(src)
    from deid.findings import FindingStore
    store = FindingStore(inv, lambda u, s, e: ad.locate(src, u, s, e))
    assert store.add_text("王小明", "姓名", "user") is not None
    plan, dst = store.plan(), tmp_path / "out.docx"
    ad.apply(src, dst, plan, settings)
    assert ad.residue(dst, plan.strings) == []
    assert isinstance(ad.verify(src, dst, plan, settings), list)
    listed = [im.name for im in inv.images if not im.supported] + [u.name for u in inv.unchecked]
    assert any("image1.tiff" in name for name in listed)  # nobody can vouch for it: the reviewer is told


def base_pictures() -> list[bytes]:
    out = [jpeg(), jpeg(progressive=True)]
    for fmt, kw in (("PNG", {}), ("GIF", {"comment": PLANT.encode()}), ("BMP", {}),
                    ("TIFF", {"compression": "tiff_lzw"}), ("TIFF", {"compression": "raw"})):
        buf = io.BytesIO()
        (photo().convert("P") if fmt == "GIF" else photo()).save(buf, fmt, **kw)
        out.append(buf.getvalue())
    return out


def test_whatever_is_wrong_with_a_picture_cleaning_it_neither_fails_nor_changes_what_it_shows():
    rng = random.Random(20261124)
    shown = 0
    for base in base_pictures():
        for _ in range(120):
            data = bytearray(base)
            how = rng.choice(("flip", "flip", "cut", "insert", "delete", "zero"))
            at = rng.randrange(len(data))
            if how == "flip":
                for _k in range(rng.randint(1, 4)):
                    data[rng.randrange(len(data))] = rng.randrange(256)
            elif how == "cut":
                del data[at:]
            elif how == "insert":
                data[at:at] = bytes(rng.randrange(256) for _ in range(rng.randint(1, 8)))
            elif how == "delete":
                del data[at:at + rng.randint(1, 8)]
            else:
                data[at:at + 8] = bytes(8)
            data = bytes(data)
            before = frames(data)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                out = imaging.strip_metadata(data)  # must not raise
                assert imaging.strip_metadata(out) is out  # and its own output is clean to it
            if before is not None:
                shown += 1
                assert frames(out) == before, (imaging._kind(base), how, at)
                if len(before) == 1:  # a picture the adapters call supported is always cleaned for certain
                    assert not imaging.metadata_unknown(data), (imaging._kind(base), how, at)
    assert shown > 150  # enough of the damaged copies still opened for the comparison to mean something
