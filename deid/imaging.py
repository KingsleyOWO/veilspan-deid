"""Image work: OCR with per-character boxes, face detection, pixel masking, vision classification."""
import base64
import io
import json
import os
import struct
import threading

import numpy as np
from PIL import Image, ImageDraw, ImageOps

from deid.config import Settings
from deid.model import Box, OcrLine

Image.MAX_IMAGE_PIXELS = 400_000_000  # scans can be large; the upload size limit bounds the input

OCR_MAX_SIDE = 4000
FACE_MAX_SIDE = 1280
VISION_MAX_SIDE = 1280
WRITABLE = {"PNG", "JPEG", "BMP", "TIFF", "GIF"}

VISION_KEYS = {"id_document": "證件", "signature_or_seal": "簽名印章", "handwriting": "手寫個資", "medical_record": "病歷"}
VISION_PROMPT = """判斷這張圖片是否含有下列內容，只輸出 JSON 物件，不要其他文字：
{"id_document": 是否為身分證、健保卡、護照、駕照、學生證、員工證等證件的影像,
 "signature_or_seal": 是否有手寫簽名或個人印章,
 "handwriting": 是否有手寫的個人資料（姓名、電話、地址等）,
 "medical_record": 是否為病歷、檢驗報告、處方箋、診斷書,
 "note": 不超過 15 字的說明}
各欄位填 true 或 false。軟體畫面截圖、簡報、產品照、活動照片、標誌，全部填 false。"""


def open_image(data: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(data))
    im.load()
    return im


# A photo can be stored sideways with a flag telling viewers to turn it. Word and browsers honour the flag,
# so everything a person sees or draws on — sizes, previews, OCR boxes, masks — uses the displayed
# orientation; only `paint_boxes` deals with the stored one.
_TURN = {2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180, 4: Image.Transpose.FLIP_TOP_BOTTOM,
         5: Image.Transpose.TRANSPOSE, 6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
         8: Image.Transpose.ROTATE_90}
_TURN_BACK = {Image.Transpose.ROTATE_270: Image.Transpose.ROTATE_90, Image.Transpose.ROTATE_90: Image.Transpose.ROTATE_270}


def orientation(im: Image.Image) -> int:
    """The EXIF orientation of a JPEG (1 = stored the way it is shown)."""
    if im.format != "JPEG":
        return 1
    try:
        value = int(im.getexif().get(0x0112) or 1)
    except Exception:
        return 1
    return value if value in _TURN else 1


def open_upright(data: bytes) -> Image.Image:
    """The picture the way it is displayed."""
    im = open_image(data)
    turn = _TURN.get(orientation(im))
    return im.transpose(turn) if turn is not None else im


def image_info(data: bytes) -> tuple[str, int, int, int]:
    """(format, displayed width, displayed height, frame count). Raises if Pillow cannot read the data."""
    im = open_image(data)
    w, h = (im.height, im.width) if orientation(im) >= 5 else (im.width, im.height)
    return im.format or "", w, h, getattr(im, "n_frames", 1)


_FOLD = {c: c - 0xFEE0 for r in (range(0xFF10, 0xFF1A), range(0xFF21, 0xFF3B), range(0xFF41, 0xFF5B)) for c in r}


def fold_width(text: str) -> str:
    """Full-width digits and Latin letters as their ordinary forms; same length, so positions still line up.
    The recogniser returns full-width digits now and then, mostly for vertical text."""
    return text.translate(_FOLD)


def even_split(text: str, box: Box) -> list[Box]:
    """One box per character, dividing the line evenly along its long side (top to bottom for vertical text)."""
    x0, y0, x1, y1 = box
    n = max(1, len(text))
    if len(text) > 1 and (y1 - y0) > 1.5 * (x1 - x0):
        step = (y1 - y0) / n
        return [(x0, y0 + k * step, x1, y0 + (k + 1) * step) for k in range(len(text))]
    step = (x1 - x0) / n
    return [(x0 + k * step, y0, x0 + (k + 1) * step, y1) for k in range(len(text))]


def _rgb(im: Image.Image) -> Image.Image:
    if im.mode in ("RGBA", "LA", "P") and ("transparency" in im.info or im.mode != "P"):
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, "white")
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    return im.convert("RGB")


def _shrunk(im: Image.Image, max_side: int) -> tuple[Image.Image, float]:
    scale = min(1.0, max_side / max(im.size))
    if scale < 1.0:
        im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)
    return im, scale


def _aabb(points) -> Box:
    pts = np.asarray(points, dtype=float).reshape(-1, 2)
    return (float(pts[:, 0].min()), float(pts[:, 1].min()), float(pts[:, 0].max()), float(pts[:, 1].max()))


class OcrEngine:
    """RapidOCR (PP-OCRv6, CPU). One engine per thread, since the underlying sessions are not shared safely."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._local = threading.local()

    def _engine(self):
        if not hasattr(self._local, "engine"):
            from rapidocr import OCRVersion, RapidOCR
            # Several engines run side by side (one per worker thread); give each a share of the cores
            # instead of letting every one of them claim all of them.
            threads = max(1, (os.cpu_count() or 4) // max(1, self.settings.ocr_workers))
            self._local.engine = RapidOCR(params={"Det.ocr_version": OCRVersion.PPOCRV6,
                                                  "Rec.ocr_version": OCRVersion.PPOCRV6,
                                                  "Global.log_level": "error",
                                                  "EngineConfig.onnxruntime.intra_op_num_threads": threads,
                                                  "EngineConfig.onnxruntime.inter_op_num_threads": 1})
        return self._local.engine

    def read(self, data: bytes) -> list[OcrLine]:
        im = _rgb(open_upright(data))
        lines: list[OcrLine] = []
        for tile, ox, oy in _tiles(im):
            lines += [_shift(ln, ox, oy) for ln in self._read_tile(tile)]
        return _dedupe(lines)

    def _read_tile(self, im: Image.Image) -> list[OcrLine]:
        im, scale = _shrunk(im, OCR_MAX_SIDE)
        arr = np.ascontiguousarray(np.asarray(im)[:, :, ::-1])
        res = self._engine()(arr, return_word_box=True, return_single_char_box=True)
        if res is None or res.txts is None:
            return []
        words = res.word_results or ()
        gray = None
        lines = []
        for i, text in enumerate(res.txts):
            if not text:
                continue
            text = fold_width(text)
            box = _aabb(res.boxes[i])
            chars = _word_boxes(text, words[i] if i < len(words) else (), box)
            if chars is None:  # the recogniser merged or dropped a glyph: fall back to an even split of the line
                chars = even_split(text, box)
            thick = _thickness(res.boxes[i])
            if thick:
                if gray is None:
                    gray = np.asarray(im.convert("L"), dtype=np.int16)
                chars = _grow_to_ink(gray, text, box, chars, thick)
            lines.append(OcrLine(text=text, box=tuple(v / scale for v in box),
                                 chars=[tuple(v / scale for v in c) for c in chars]))
        return lines


def _vertical(text: str, box: Box) -> bool:
    return len(text) > 1 and (box[3] - box[1]) > 1.5 * (box[2] - box[0])


def _word_boxes(text: str, words, box: Box) -> list[Box] | None:
    """One box per character from the recogniser's own boxes. It gives none for a space, which takes the gap between
    its neighbours (an even split of a line with a space in it put the boxes tens of pixels off once the line mixed
    full-width and half-width characters). None when its boxes do not line up with the text."""
    if not all(len(w) >= 3 for w in words):
        return None
    if len(words) == len(text):
        return [_aabb(w[2]) for w in words]
    solid = [k for k, c in enumerate(text) if not c.isspace()]
    if len(words) != len(solid) or any(fold_width(str(w[0])) != text[k] for w, k in zip(words, solid)):
        return None
    chars: list = [None] * len(text)
    for w, k in zip(words, solid):
        chars[k] = _aabb(w[2])
    down = _vertical(text, box)
    lo, hi = (1, 3) if down else (0, 2)
    for k, c in enumerate(text):
        if c.isspace():
            before = next((chars[j] for j in range(k - 1, -1, -1) if not text[j].isspace()), None)
            after = next((chars[j] for j in range(k + 1, len(text)) if not text[j].isspace()), None)
            a = before[hi] if before else box[lo]
            b = after[lo] if after else box[hi]
            a, b = min(a, b), max(a, b)
            chars[k] = (box[0], a, box[2], b) if down else (a, box[1], b, box[3])
    return chars


def _thickness(quad) -> float | None:
    """The thickness of a detected line that lies level or upright, give or take 10 degrees. None for a line more
    slanted than that: its character boxes are the rectangles around turned ones and are left as they are."""
    pts = np.asarray(quad, dtype=float).reshape(-1, 2)
    if len(pts) != 4:
        return None
    edges = [pts[(k + 1) % 4] - pts[k] for k in range(4)]
    lengths = [float(np.hypot(*e)) for e in edges]
    k = 0 if lengths[0] + lengths[2] >= lengths[1] + lengths[3] else 1  # one of the long sides
    turn = abs(float(np.degrees(np.arctan2(edges[k][1], edges[k][0])))) % 90
    if min(turn, 90 - turn) > 10:
        return None
    return (lengths[k + 1] + lengths[(k + 3) % 4]) / 2


INK_GROW = 0.25  # how far a character box may grow towards its ink, as a share of the line's thickness


def _grow_to_ink(gray: np.ndarray, text: str, box: Box, chars: list[Box], thick: float) -> list[Box]:
    """The recogniser places each character's box around where it saw the character in its own reduced copy of the
    line. Measured against the real ink, a box can start or end inside the glyph by up to nearly a quarter of the
    line's height (the review saw 3 pixels of 王 outside its box). So each box grows, along the line, over the ink
    that belongs to it: a connected blot of ink lying within the box widened by INK_GROW of the line's thickness on
    each side, or lying mostly inside the box. Boxes never shrink and never grow by more than that much, so a
    neighbour can lose at most a sliver: a detached stroke beside the box, or the part of a glyph touching it.
    Everything downstream (the review screen, the painted output, the check that it is black) uses these boxes."""
    import cv2
    h, w = gray.shape
    x0, y0 = max(0, int(box[0])), max(0, int(box[1]))
    x1, y1 = min(w, int(np.ceil(box[2]))), min(h, int(np.ceil(box[3])))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return chars
    down = _vertical(text, box)
    region = gray[y0:y1, x0:x1]
    if down:
        region, off, spans = region.T, y0, [(c[1] - y0, c[3] - y0) for c in chars]
    else:
        off, spans = x0, [(c[0] - x0, c[2] - x0) for c in chars]
    dev = np.abs(region - int(np.median(region)))  # dark text on light ground and light text on dark alike
    peak = int(dev.max())
    if peak < 48:
        return chars  # hardly any contrast: ink cannot be told from the ground
    ink = (dev > max(24, 0.35 * peak)).astype(np.uint8)
    count, _labels, stats, _centres = cv2.connectedComponentsWithStats(ink, connectivity=8)
    reach = INK_GROW * thick
    grown = [list(s) for s in spans]
    for j in range(1, count):
        lo, hi = int(stats[j, 0]), int(stats[j, 0] + stats[j, 2])
        for k, (a, b) in enumerate(spans):
            if text[k].isspace() or hi <= a - reach or lo >= b + reach:
                continue
            if (a - reach <= lo and hi <= b + reach) or min(hi, b) - max(lo, a) >= 0.5 * (hi - lo):
                grown[k][0] = min(grown[k][0], max(lo, a - reach))
                grown[k][1] = max(grown[k][1], min(hi, b + reach))
    out = []
    for c, (a, b) in zip(chars, grown):
        out.append((c[0], a + off, c[2], b + off) if down else (a + off, c[1], b + off, c[3]))
    return out


def _tiles(im: Image.Image):
    """Elongated images (long screenshots, banners) defeat the text detector's resizing, so they are read
    as overlapping tiles along the long side. Everything else is one tile."""
    w, h = im.size
    short, long_ = min(w, h), max(w, h)
    if long_ <= 3 * short:
        yield im, 0, 0
        return
    size = max(2 * short, 1024)
    overlap = min(short, size // 2)
    pos = 0
    while True:
        end = min(long_, pos + size)
        box = (pos, 0, end, h) if w >= h else (0, pos, w, end)
        yield im.crop(box), box[0], box[1]
        if end >= long_:
            return
        pos = end - overlap


def _shift(ln: OcrLine, dx: float, dy: float) -> OcrLine:
    if not dx and not dy:
        return ln
    mv = lambda b: (b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy)  # noqa: E731
    return OcrLine(text=ln.text, box=mv(ln.box), chars=[mv(c) for c in ln.chars])


def _overlap(a: Box, b: Box) -> float:
    iw, ih = min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])
    return iw * ih if iw > 0 and ih > 0 else 0.0


def _read_again(ln: OcrLine, k: OcrLine) -> bool:
    """Whether `ln` is a second reading of (part of) `k`: most of its characters are the same character at the
    same place in `k`. Lying inside k's box is not enough: the box of a slanted line is mostly empty, and a
    short line in that empty corner is a line of its own."""
    pairs = [(c, b) for c, b in zip(ln.text, ln.chars) if not c.isspace()]
    if not pairs:
        return True
    hits = 0
    for c, b in pairs:
        area = max(1e-6, (b[2] - b[0]) * (b[3] - b[1]))
        for c2, b2 in zip(k.text, k.chars):
            if c2 == c and _overlap(b, b2) >= 0.5 * min(area, max(1e-6, (b2[2] - b2[0]) * (b2[3] - b2[1]))):
                hits += 1
                break
    return hits >= 0.8 * len(pairs)


def _dedupe(lines: list[OcrLine]) -> list[OcrLine]:
    """Tiles overlap, so the same line can be read twice. Keep the longer reading."""
    kept: list[OcrLine] = []
    for ln in sorted(lines, key=lambda l: -len(l.text)):
        area = max(1e-6, (ln.box[2] - ln.box[0]) * (ln.box[3] - ln.box[1]))
        if not any(_overlap(ln.box, k.box) / area >= 0.8 and _read_again(ln, k) for k in kept):
            kept.append(ln)
    return sorted(kept, key=lambda l: (l.box[1], l.box[0]))


class FaceDetector:
    """OpenCV YuNet. Boxes are grown by 15% so the mask covers the whole face."""
    GROW = 0.15

    def __init__(self, settings: Settings):
        self.settings = settings
        self._local = threading.local()

    def _detector(self, size: tuple[int, int]):
        import cv2
        if not hasattr(self._local, "det"):
            cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
            self._local.det = cv2.FaceDetectorYN.create(str(self.settings.face_model), "", size,
                                                        float(self.settings.face_score), 0.3, 5000)
        self._local.det.setInputSize(size)
        return self._local.det

    def detect(self, data: bytes) -> list[Box]:
        full = _rgb(open_upright(data))
        im, scale = _shrunk(full, FACE_MAX_SIDE)
        arr = np.ascontiguousarray(np.asarray(im)[:, :, ::-1])
        _, faces = self._detector((im.width, im.height)).detect(arr)
        out = []
        for f in faces if faces is not None else []:
            x, y, w, h = (float(v) / scale for v in f[:4])
            gx, gy = w * self.GROW, h * self.GROW
            out.append((max(0.0, x - gx), max(0.0, y - gy), min(float(full.width), x + w + gx),
                        min(float(full.height), y + h + gy)))
        return out


def _clamp(box: Box, w: int, h: int) -> tuple[int, int, int, int] | None:
    x0, y0, x1, y1 = box
    x0, y0, x1, y1 = max(0, int(x0)), max(0, int(y0)), min(w, int(np.ceil(x1))), min(h, int(np.ceil(y1)))
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


def _opaque(im: Image.Image, index: int) -> bool:
    t = im.info.get("transparency")
    if isinstance(t, int):
        return index != t
    if isinstance(t, (bytes, bytearray)):
        return index >= len(t) or t[index] == 255
    return True


def _black_for(im: Image.Image):
    if im.mode == "P":
        pal = im.getpalette() or []
        triples = [tuple(pal[i:i + 3]) for i in range(0, len(pal), 3)]
        for i, colour in enumerate(triples):  # a black entry that is also the transparent one would paint a hole
            if colour == (0, 0, 0) and _opaque(im, i):
                return i
        if len(triples) < 256:
            im.putpalette(pal + [0, 0, 0])
            return len(triples)  # entries past the end of the transparency table are opaque
        solid = [i for i in range(len(triples)) if _opaque(im, i)] or list(range(len(triples)))
        return min(solid, key=lambda i: sum(triples[i]))
    return {"1": 0, "L": 0, "I": 0, "F": 0, "I;16": 0, "LA": (0, 255), "RGB": (0, 0, 0), "RGBA": (0, 0, 0, 255),
            "CMYK": (0, 0, 0, 255), "YCbCr": (0, 128, 128)}.get(im.mode, 0)


def _save(im: Image.Image, fmt: str, source: Image.Image) -> bytes:
    out = io.BytesIO()
    kw = {}
    if fmt == "JPEG":
        kw = {"quality": 90, "subsampling": "keep"} if source.format == "JPEG" and im.mode == source.mode else {"quality": 90}
        if "icc_profile" in source.info:
            kw["icc_profile"] = source.info["icc_profile"]
        if "dpi" in source.info:
            kw["dpi"] = source.info["dpi"]
        if orientation(source) != 1:
            kw["exif"] = _orientation_exif(orientation(source))[4:]  # keep it displayed the same way round
    elif fmt == "PNG":
        if "icc_profile" in source.info:
            kw["icc_profile"] = source.info["icc_profile"]
        if "dpi" in source.info:
            kw["dpi"] = source.info["dpi"]
        if im.mode == "P" and "transparency" in source.info:
            kw["transparency"] = source.info["transparency"]
    elif fmt == "GIF" and "transparency" in source.info:
        kw["transparency"] = source.info["transparency"]
    try:
        im.save(out, fmt, **kw)
    except (ValueError, OSError):
        out = io.BytesIO()
        kw.pop("subsampling", None)
        im.save(out, fmt, **kw)
    return out.getvalue()


def paint_boxes(data: bytes, boxes: list[Box]) -> bytes:
    """Black out the boxes. Same format, colour mode and size; embedded metadata is not carried over."""
    src = open_image(data)
    fmt = src.format
    if fmt not in WRITABLE:
        raise ValueError(f"cannot write image format {fmt}")
    turn = _TURN.get(orientation(src))
    im = src.transpose(turn) if turn is not None else src.copy()  # boxes are in displayed coordinates
    if im.mode == "P" and "transparency" in src.info:
        im.info["transparency"] = src.info["transparency"]
    black = _black_for(im)
    draw = ImageDraw.Draw(im)
    for box in boxes:
        c = _clamp(box, im.width, im.height)
        if c:
            draw.rectangle((c[0], c[1], c[2] - 1, c[3] - 1), fill=black)
    if turn is not None:
        im = im.transpose(_TURN_BACK.get(turn, turn))  # back to the stored orientation
    return strip_metadata(_save(im, fmt, src))


# ---- embedded metadata ---------------------------------------------------------------------------
# Pictures carry text of their own: camera EXIF (owner, GPS, serial numbers), XMP, IPTC, comments,
# editor-private blocks, thumbnails of the uncropped picture. None of it is shown for review, so it is
# removed by cutting those blocks out of the file; the picture data itself is not re-encoded.
# Every format the adapters treat as a supported picture (WRITABLE, one frame) is handled. A file is read the
# way decoders read it: bytes they skip (a stray byte between two JPEG blocks, junk after the last PNG block,
# bytes no GIF block starts with) are dropped rather than taken as a reason to give up. BMP and TIFF are rebuilt
# from the parts needed to show them. Only when that cannot be done is the picture decoded and written again,
# without loss, in its own format (never a JPEG). Kept on purpose: the orientation (a sideways photo still shows
# upright), colour profiles (ICC, needed to show the colours right; the text inside a profile describes the
# profile and stays with it), resolution, transparency and animation.

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_KEEP = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"cHRM", b"gAMA", b"iCCP", b"sBIT", b"sRGB", b"cICP", b"pHYs",
            b"bKGD", b"hIST", b"acTL", b"fcTL", b"fdAT"}  # sPLT, a suggested palette, carries a free-text name
GIF_APPS = (b"NETSCAPE2.0", b"ANIMEXTS1.0")  # animation loop counts: not metadata
TIFF_PREFIXES = (b"II\x2a\x00", b"MM\x00\x2a", b"II\x00\x2a", b"MM\x2a\x00", b"II\x2b\x00", b"MM\x00\x2b")  # as Pillow
# TIFF tags needed to decode and show the picture (TIFF 6.0 and its JPEG and colour-profile extensions). All
# numbers, except the JPEG tables and the ICC profile. The strip and tile locations are rewritten, not copied.
TIFF_KEEP = {254, 255, 256, 257, 258, 259, 262, 263, 264, 265, 266, 274, 277, 278, 280, 281, 282, 283, 284, 290, 291,
             292, 293, 296, 301, 317, 318, 319, 320, 321, 322, 323, 332, 334, 336, 338, 339, 340, 341, 342, 347, 529,
             530, 531, 532, 34675}
TIFF_BYTES = {347, 34675}
_TIFF_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8}
_TIFF_INT = {1: "B", 3: "H", 4: "I", 13: "I", 16: "Q", 18: "Q"}
_BMP_LINKED, _BMP_EMBEDDED, _BMP_SRGB = b"KNIL", b"DEBM", b"BGRs"  # 'LINK', 'MBED', 'sRGB' as a BMP stores them


def _orientation_exif(orientation: int) -> bytes:
    """An EXIF block holding nothing but the orientation, so a sideways photo still displays upright."""
    tiff = b"MM\x00\x2a\x00\x00\x00\x08\x00\x01\x01\x12\x00\x03\x00\x00\x00\x01" + bytes([0, orientation, 0, 0]) + bytes(4)
    body = b"Exif\x00\x00" + tiff
    return b"\xff\xe1" + (len(body) + 2).to_bytes(2, "big") + body


def _exif_orientation(payload: bytes) -> int:
    try:
        exif = Image.Exif()
        exif.load(payload)
        value = int(exif.get(0x0112) or 1)
    except Exception:
        return 1
    return value if 2 <= value <= 8 else 1


def _jpeg_segment(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


def _next_marker(data: bytes, i: int) -> int:
    """Where the next marker starts, found the way decoders find it: bytes other than 0xFF are skipped, and so are
    fill bytes (FF FF) and a stuffed zero (FF 00). -1 when there is none."""
    n = len(data)
    while True:
        k = data.find(b"\xff", i)
        if k < 0 or k + 1 >= n:
            return -1
        if data[k + 1] == 0xFF:
            i = k + 1
        elif data[k + 1] == 0x00:
            i = k + 2
        else:
            return k


def _scan_end(data: bytes, k: int) -> int:
    """The end of the compressed data that starts at k: the next marker other than a restart marker."""
    n = len(data)
    while True:
        k = data.find(b"\xff", k)
        if k < 0 or k + 1 >= n:
            return n
        if data[k + 1] == 0x00 or 0xD0 <= data[k + 1] <= 0xD7:
            k += 2
        elif data[k + 1] == 0xFF:
            k += 1
        else:
            return k


def _strip_jpeg(data: bytes, keep_orientation: bool = True) -> tuple[bytes, bool]:
    """(the JPEG without metadata, whether every byte of it was accounted for)."""
    n = len(data)
    head, body = bytearray(b"\xff\xd8"), bytearray()
    orientation, exif_seen, scanned, found, i = 1, False, False, False, 2
    while True:
        k = _next_marker(data, i)
        if k < 0:
            if not found:
                return data, False  # nothing after the start marker reads as JPEG
            if scanned:
                body += b"\xff\xd9"  # cut short in the picture data: decoders stop there too
            break
        found = True
        marker = data[k + 1]
        if marker == 0xD9:
            body += b"\xff\xd9"  # anything after the end marker (a second picture, a trailer) is dropped
            break
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:  # markers without a length
            body += data[k:k + 2]
            i = k + 2
            continue
        length = int.from_bytes(data[k + 2:k + 4], "big") if k + 4 <= n else n
        end = k + 2 + length
        if length < 2:  # decoders give up here; what follows cannot be read
            return bytes(head + body) + data[k:], False
        if end > n:  # cut short inside a block: a metadata block goes, anything else is kept as it is
            if not (0xE0 <= marker <= 0xEF or marker == 0xFE):
                body += data[k:]
            elif scanned:
                body += b"\xff\xd9"  # the picture data ended at this block: without an end marker some decoders refuse
            break
        if marker == 0xDA:  # a scan: compressed data runs to the next real marker
            stop = _scan_end(data, end)
            body += data[k:stop]
            scanned, i = True, stop
            continue
        payload = data[k + 4:end]
        if marker == 0xE0 and payload[:5] == b"JFIF\x00" and not body and len(head) == 2:
            if len(payload) >= 14:  # JFIF header stays first, without the thumbnail it may carry
                head += _jpeg_segment(0xE0, payload[:12] + b"\x00\x00")
        elif marker == 0xE1 and payload[:6] == b"Exif\x00\x00":
            if not exif_seen and not scanned:  # viewers use the first EXIF block before the picture data
                orientation = _exif_orientation(payload)
            exif_seen = True
        elif marker == 0xE2 and payload[:12] == b"ICC_PROFILE\x00":
            body += data[k:end]  # colour profile: needed to show the picture correctly
        elif marker == 0xEE:
            if payload[:5] == b"Adobe" and len(payload) >= 12:
                body += _jpeg_segment(0xEE, payload[:12])  # the colour transform flag, and nothing after it
        elif not (0xE0 <= marker <= 0xEF or marker == 0xFE):
            body += data[k:end]  # every table and frame header
        i = end
    if keep_orientation and orientation != 1:
        head += _orientation_exif(orientation)
    return bytes(head + body), True


def _strip_png(data: bytes) -> tuple[bytes, bool]:
    out = bytearray(PNG_SIGNATURE)
    i, n, pixels, ended = 8, len(data), False, False
    while i < n:
        length = int.from_bytes(data[i:i + 4], "big")
        kind = data[i + 4:i + 8]
        end = i + 12 + length
        if len(kind) < 4 or not kind.isalpha() or end > n:
            if not pixels:
                return bytes(out) + data[i:], False  # broken before the picture data: the rest cannot be read
            break  # broken after it: decoders stop here, like at the end chunk
        if kind in PNG_KEEP:
            out += data[i:end]
            pixels = pixels or kind == b"IDAT"
        i = end
        if kind == b"IEND":
            ended = True
            break  # anything after the end chunk is dropped
    if not ended:
        if not pixels:
            return bytes(out), False
        out += b"\x00\x00\x00\x00IEND\xaeB`\x82"
    return bytes(out), True


def _strip_gif(data: bytes) -> tuple[bytes, bool]:
    n = len(data)
    if n < 13:
        return data, False
    i = 13 + (3 * (2 << (data[10] & 7)) if data[10] & 0x80 else 0)
    if i > n:
        return data, False
    out = bytearray(data[:i])

    def blocks(k: int) -> int:
        while k < n and data[k]:
            k += data[k] + 1
        return k + 1

    while i < n:
        kind = data[i]
        if kind == 0x3B:
            break
        if kind == 0x2C:
            if i + 10 > n:
                break
            j = blocks(i + 11 + (3 * (2 << (data[i + 9] & 7)) if data[i + 9] & 0x80 else 0))
            out += data[i:j]
            if j > n:
                return bytes(out), True  # picture data cut short: kept as it is
            i = j
        elif kind == 0x21 and i + 1 < n:
            j = blocks(i + 2)
            if j > n:
                break
            label = data[i + 1]
            # Kept: the frame's delay and transparency, and animation loop counts. Comments, application data
            # (XMP among it), the rarely shown plain-text blocks and unknown kinds go.
            if label == 0xF9 or (label == 0xFF and data[i + 3:i + 14] in GIF_APPS):
                out += data[i:j]
            i = j
        else:
            i += 1  # a byte that starts no block: Pillow skips it, so this does too
    out += b"\x3b"  # anything after the trailer is dropped
    return bytes(out), True


def _strip_bmp(data: bytes) -> tuple[bytes, bool] | None:
    """Rebuilt from its header, colour table and pixel array (and an embedded colour profile), so bytes between
    or after them and a linked profile's file name do not survive. None for a layout this does not cover."""
    if len(data) < 26:
        return None
    off = int.from_bytes(data[10:14], "little")
    size = int.from_bytes(data[14:18], "little")
    if size not in (12, 40, 52, 56, 108, 124) or 14 + size > len(data):
        return None
    head = bytearray(data[14:14 + size])
    if size == 12:
        w, h, _planes, bpp = struct.unpack("<HHHH", head[4:12])
        compression, image_size, colours, entry = 0, 0, 0, 3
    else:
        w, h, _planes, bpp, compression, image_size, _x, _y, colours = struct.unpack("<iiHHIIiiI", head[4:36])
        entry = 4
    if w <= 0 or h == 0 or bpp not in (1, 2, 4, 8, 16, 24, 32):
        return None
    masks = {3: 12, 6: 16}.get(compression, 0) if size == 40 else 0
    if bpp <= 8:
        colours = min(colours or 1 << bpp, 1 << bpp)
        if size >= 40 and int.from_bytes(head[32:36], "little") > colours:
            head[32:36] = colours.to_bytes(4, "little")
    else:
        colours = 0  # a colour table beside true-colour pixels is only a hint for old displays
        if size >= 40:
            head[32:36] = bytes(4)
    table_at = 14 + size
    table = data[table_at:table_at + masks + colours * entry]
    if compression in (0, 3, 6):
        length = (w * bpp + 31) // 32 * 4 * abs(h)
    elif compression in (1, 2) and image_size:  # run-length encoded: the header says how long
        length = image_size
    else:
        return None  # JPEG or PNG inside a BMP: not a picture Pillow reads
    if off < table_at + len(table) or off + length > len(data):
        return None
    pixels = data[off:off + length]
    profile = b""
    if size == 124:
        kind = bytes(head[56:60])
        at, count = struct.unpack("<II", head[112:120])
        if kind == _BMP_EMBEDDED and count and 14 + at + count <= len(data):
            profile = data[14 + at:14 + at + count]
            head[112:116] = (size + len(table) + len(pixels)).to_bytes(4, "little")
        else:
            if kind in (_BMP_EMBEDDED, _BMP_LINKED):
                head[56:60] = _BMP_SRGB  # a linked profile is a file name, often with a user name in its path
            head[112:120] = bytes(8)
        head[120:124] = bytes(4)  # reserved
    body = bytes(head) + table + pixels + profile
    return b"BM" + struct.pack("<IHHI", 14 + len(body), 0, 0, 14 + size + len(table)) + body, True


def _tiff_ifd(tags: dict, at: int, e: str) -> bytes:
    """One directory written at offset `at`: entries in tag order, longer values after them."""
    extra_at = at + 2 + 12 * len(tags) + 4
    head, extra = bytearray(struct.pack(e + "H", len(tags))), bytearray()
    for tag in sorted(tags):
        kind, count, raw = tags[tag]
        if len(raw) <= 4:
            head += struct.pack(e + "HHI", tag, kind, count) + raw.ljust(4, b"\x00")
        else:
            head += struct.pack(e + "HHII", tag, kind, count, extra_at + len(extra))
            extra += raw + b"\x00" * (len(raw) % 2)
    return bytes(head + bytes(4) + extra)


def _strip_tiff(data: bytes) -> tuple[bytes, bool] | None:
    """Rebuilt as a classic TIFF holding, for each page, the tags needed to show it and its strips or tiles.
    Gone: descriptions, names, dates, EXIF and GPS directories, XMP, IPTC, Photoshop data, thumbnails in sub-
    directories and any byte no kept tag points to. None for a layout this does not cover."""
    e = "<" if data[:2] == b"II" else ">"
    big = struct.unpack(e + "H", data[2:4])[0] == 43
    word, count_fmt, entry_size, value_size = ("Q", "Q", 20, 8) if big else ("I", "H", 12, 4)
    at = struct.unpack(e + word, data[8:16] if big else data[4:8])[0]
    pages, seen = [], set()
    while at:
        if at in seen or len(seen) >= 4096:
            return None
        seen.add(at)
        n = struct.unpack(e + count_fmt, data[at:at + struct.calcsize(count_fmt)])[0]
        pos = at + struct.calcsize(count_fmt)
        tags = {}
        for k in range(n):
            ent = data[pos + k * entry_size:pos + (k + 1) * entry_size]
            tag, kind = struct.unpack(e + "HH", ent[:4])
            count = struct.unpack(e + word, ent[4:4 + value_size])[0]
            if kind not in _TIFF_SIZE:
                if tag in TIFF_KEEP or tag in (273, 279, 324, 325):
                    return None
                continue
            length = _TIFF_SIZE[kind] * count
            if length <= value_size:
                raw = ent[4 + value_size:4 + value_size + length]
            else:
                where = struct.unpack(e + word, ent[4 + value_size:])[0]
                raw = data[where:where + length]
            if len(raw) != length:
                return None
            tags[tag] = (kind, count, raw)
        nxt = pos + n * entry_size
        at = struct.unpack(e + word, data[nxt:nxt + value_size])[0]
        pages.append(tags)

    def numbers(item) -> list[int]:
        kind, count, raw = item
        if kind not in _TIFF_INT:
            raise ValueError("not a list of offsets")
        return list(struct.unpack(e + _TIFF_INT[kind] * count, raw))

    out = bytearray((b"II*\x00" if e == "<" else b"MM\x00*") + bytes(4))
    link = 4
    for tags in pages:
        where, sizes = (324, 325) if 324 in tags else (273, 279)
        if where not in tags or sizes not in tags:
            return None
        offsets, counts = numbers(tags[where]), numbers(tags[sizes])
        if len(offsets) != len(counts) or any(o + c > len(data) for o, c in zip(offsets, counts)):
            return None
        compression = numbers(tags[259])[0] if 259 in tags else 1
        if compression == 6:
            return None  # old-style JPEG: its tables and picture sit wherever other tags point
        blocks = [data[o:o + c] for o, c in zip(offsets, counts)]
        kept = {}
        for tag, (kind, count, raw) in tags.items():
            if tag not in TIFF_KEEP or (kind in (2, 7) and tag not in TIFF_BYTES):
                continue
            if kind in (16, 17, 18):  # 64-bit numbers of a BigTIFF: the copy is a classic TIFF
                values = struct.unpack(e + ("q" if kind == 17 else "Q") * count, raw)
                if any(not 0 <= v < 2 ** 32 for v in values):
                    return None
                kind, raw = 4, struct.pack(e + "I" * count, *values)
            kept[tag] = (kind, count, raw)
        if compression == 7:  # each strip, and the shared tables, is a JPEG stream that can carry blocks of its own
            blocks = [_strip_jpeg(b, keep_orientation=False)[0] if b[:2] == b"\xff\xd8" else b for b in blocks]
            if 347 in kept and kept[347][2][:2] == b"\xff\xd8":
                tables = _strip_jpeg(kept[347][2], keep_orientation=False)[0]
                kept[347] = (kept[347][0], len(tables), tables)
        placed = []
        for block in blocks:
            out += b"\x00" * (len(out) % 2)
            placed.append(len(out))
            out += block
        kept[where] = (4, len(placed), struct.pack(e + "I" * len(placed), *placed))
        kept[sizes] = (4, len(blocks), struct.pack(e + "I" * len(blocks), *(len(b) for b in blocks)))
        out += b"\x00" * (len(out) % 2)
        struct.pack_into(e + "I", out, link, len(out))
        link = len(out) + 2 + 12 * len(kept)
        out += _tiff_ifd(kept, len(out), e)
    if len(out) >= 2 ** 32:
        return None
    return bytes(out), True


def _same_picture(before: bytes, after: bytes) -> bool:
    """Whether a rebuilt file shows the same pixels as the original, every frame. True when the original does not
    decode: there is nothing to compare, and the rebuild only copied its picture data across."""
    try:
        old = open_image(before)
    except Exception:
        return True
    try:
        new = open_image(after)
        frames = getattr(old, "n_frames", 1)
        if getattr(new, "n_frames", 1) != frames:
            return False
        for k in range(frames):
            old.seek(k)
            new.seek(k)
            if (old.mode, old.size) != (new.mode, new.size) or old.tobytes() != new.tobytes():
                return False
        return True
    except Exception:
        return False


def _reencode(data: bytes) -> bytes | None:
    """Last resort for a supported picture that could not be cleaned block by block (its structure could not be
    followed, or what came out did not show the same picture): decoded and written again in its own format with
    only its pixels, colour profile, resolution, transparency and orientation. PNG, GIF, BMP and TIFF are written
    without loss. A JPEG is written with its own quantisation tables, which costs next to nothing; only grey and
    RGB ones (a CMYK JPEG written by Pillow does not read the same elsewhere). Not for a picture of more than one
    frame. None when it cannot be done; whatever the file makes Pillow raise ends here."""
    try:
        im = open_image(data)
        fmt = im.format
        if fmt not in WRITABLE or getattr(im, "n_frames", 1) != 1:
            return None
        out = io.BytesIO()
        if fmt == "JPEG":
            if im.mode not in ("L", "RGB"):
                return None
            turn = orientation(im)
            im.load()
            kw = {key: im.info[key] for key in ("icc_profile", "dpi") if key in im.info}
            if turn != 1:
                kw["exif"] = _orientation_exif(turn)[4:]  # the block without its marker and length
            im.save(out, "JPEG", quality="keep", subsampling="keep", comment=b"", **kw)
            return out.getvalue()
        plain = im.copy()  # no tag directory: the TIFF writer would copy XMP, IPTC and Photoshop data from it
        plain.info = {}
        kw = {key: im.info[key] for key in ("icc_profile", "dpi", "transparency") if key in im.info}
        if fmt == "TIFF":
            kw["compression"] = "tiff_adobe_deflate"
            if im.tag_v2.get(274) in _TURN:
                kw["tiffinfo"] = {274: im.tag_v2[274]}
        plain.save(out, fmt, **kw)
        return out.getvalue()
    except Exception:
        return None


def _kind(data: bytes) -> str | None:
    if data[:2] == b"\xff\xd8":
        return "JPEG"
    if data[:8] == PNG_SIGNATURE:
        return "PNG"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "GIF"
    if data[:4] in TIFF_PREFIXES:
        return "TIFF"
    if data[:2] == b"BM":
        return "BMP"
    return None


_STRIP = {"JPEG": _strip_jpeg, "PNG": _strip_png, "GIF": _strip_gif, "BMP": _strip_bmp, "TIFF": _strip_tiff}


def _lossless(kind: str, data: bytes) -> tuple[bytes, bool] | None:
    """The picture cleaned block by block, or None when that failed or changed what the picture shows. A damaged
    file can read one way to a decoder and another way here (bytes inside the picture data that look like the
    start of a metadata block), so every result that differs from the input is decoded and compared, whatever
    the format; whatever goes wrong on the way counts as failed, and the picture is written again instead."""
    try:
        result = _STRIP[kind](data)
        if result is not None and result[0] != data and not _same_picture(data, result[0]):
            return None
        return result
    except Exception:
        return None


def _clean(data: bytes) -> tuple[bytes, bool]:
    """(the picture without embedded metadata, whether that could be done completely). Never raises: a picture
    this cannot handle comes back as it was, marked as not done."""
    try:
        return _cleaned(data)
    except Exception:
        return data, False


def _cleaned(data: bytes) -> tuple[bytes, bool]:
    kind = _kind(data)
    if kind is None:
        return data, False
    result = _lossless(kind, data)
    if result is not None and result[1]:
        return result
    again = _reencode(data)
    if again is not None:
        tidy = _lossless(kind, again)
        return (tidy[0] if tidy is not None and tidy[1] else again), True
    return (result[0] if result is not None else data), False


def strip_metadata(data: bytes) -> bytes:
    """The same picture without embedded metadata: JPEG, PNG, GIF, BMP and TIFF (every format the adapters treat as
    supported). Anything else comes back unchanged, and so does a picture of those formats that can neither be read
    through nor decoded (`metadata_unknown` says which). Returns the same object only when there is nothing to
    remove, so `strip_metadata(x) is not x` means x still carries something."""
    out, _complete = _clean(data)
    return data if out == data else out


def metadata_unknown(data: bytes) -> bool:
    """True for a JPEG, PNG, GIF, BMP or TIFF that strip_metadata could neither read through nor decode: it comes
    back with what could be cut (perhaps nothing), and nobody can say what the rest still carries. Pillow cannot
    open such a file either, so the adapters list it as not checked when they read the document; a caller that
    checks an output picture should report it the same way rather than call it clean."""
    return _kind(data) is not None and not _clean(data)[1]


def for_black_check(data: bytes) -> Image.Image:
    """The picture as is_black looks at it. A caller with many boxes on one picture decodes it once with this:
    decoding a page picture again for every box made the check of a page with hundreds of them take a minute."""
    return _rgb(open_upright(data)).convert("L")


def is_black(data, box: Box) -> bool:
    """`data`: the picture's bytes, or what for_black_check made of them."""
    im = data if isinstance(data, Image.Image) else for_black_check(data)
    c = _clamp(box, im.width, im.height)
    if not c:
        return False
    x0, y0, x1, y1 = c
    if x1 - x0 > 6 and y1 - y0 > 6:
        x0, y0, x1, y1 = x0 + 2, y0 + 2, x1 - 2, y1 - 2
    return float(np.asarray(im.crop((x0, y0, x1, y1))).mean()) < 16


def to_browser_png(data: bytes) -> bytes:
    out = io.BytesIO()
    _rgb(open_upright(data)).save(out, "PNG", optimize=False, compress_level=3)
    return out.getvalue()


def crop_png(data: bytes, box: Box) -> bytes:
    im = _rgb(open_upright(data))
    c = _clamp(box, im.width, im.height)
    out = io.BytesIO()
    (im.crop(c) if c else im).save(out, "PNG", compress_level=3)
    return out.getvalue()


def vision_jpeg_url(data: bytes) -> str:
    im, _ = _shrunk(_rgb(open_upright(data)), VISION_MAX_SIDE)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")


def classify_image(llm, data: bytes) -> list[str]:
    """Ask the model what kind of sensitive image this is. Returns labels; empty when unsure or unparseable."""
    msg = llm.chat([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": vision_jpeg_url(data)}},
                                                 {"type": "text", "text": VISION_PROMPT}]}], max_tokens=200)
    content = msg.get("content") or ""
    try:
        flags = json.loads(content[content.index("{"): content.rindex("}") + 1])
    except ValueError:
        return []
    if not isinstance(flags, dict):
        return []
    return [label for key, label in VISION_KEYS.items() if flags.get(key) is True]
