"""Format-independent data: what a document is made of, and what should be masked in it."""
from dataclasses import asdict, dataclass, field

Box = tuple[float, float, float, float]  # x0, y0, x1, y1 in pixels of the image the unit refers to


@dataclass
class TextUnit:
    id: str
    text: str
    section: str
    page: int | None = None
    mirror: bool = False  # duplicate copy of another unit (e.g. mc:Fallback); hidden in the view
    # A mirror says which visible text each of its paragraphs repeats: [offset here, length, the unit, offset there].
    # Empty where that is not known (an inventory saved before this was recorded).
    copies: list = field(default_factory=list)
    # Positions of spaces that are not in the file: the reader put one where a line of a page ends, because
    # between two words there is one. Inside a string broken over two lines (exam|ple.com) there is none, and
    # which it is cannot be known, so whoever searches the text takes both readings (see `tight`).
    breaks: list = field(default_factory=list)


@dataclass
class OcrLine:
    text: str
    box: Box
    chars: list[Box]  # one box per character of `text`


@dataclass
class ImageUnit:
    id: str
    name: str
    width: int
    height: int
    supported: bool
    page: int | None = None
    anchor: str | None = None  # text unit the image sits in (docx)
    areas: list[Box] = field(default_factory=list)  # regions classified one by one by the vision check
    ocr: list[OcrLine] = field(default_factory=list)
    faces: list[Box] = field(default_factory=list)
    needs_scan: bool = True  # False for PDF pages that contain no raster image


@dataclass
class MetaUnit:
    id: str
    label: str
    value: str
    person: bool


@dataclass
class Unchecked:
    name: str
    reason: str


@dataclass
class Inventory:
    kind: str  # "docx" | "pdf"
    texts: list[TextUnit] = field(default_factory=list)
    images: list[ImageUnit] = field(default_factory=list)
    metas: list[MetaUnit] = field(default_factory=list)
    unchecked: list[Unchecked] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # things the reviewer should know that are not problems

    def text(self, unit_id: str) -> TextUnit | None:
        return next((t for t in self.texts if t.id == unit_id), None)

    def image(self, image_id: str) -> ImageUnit | None:
        return next((i for i in self.images if i.id == image_id), None)

    def meta(self, meta_id: str) -> MetaUnit | None:
        return next((m for m in self.metas if m.id == meta_id), None)


@dataclass
class Occurrence:
    unit: str
    kind: str  # "text" | "box" | "meta"
    start: int = 0
    end: int = 0
    box: Box | None = None
    page_boxes: list[tuple[str, Box]] = field(default_factory=list)  # PDF text: where it shows on page images
    on: bool = True
    fuzzy: bool = False


@dataclass
class Finding:
    id: str
    kind: str  # "text" | "region" | "meta"
    type: str
    text: str
    source: str  # model | agent | rule | face | vision | user | meta
    on: bool
    reason: str = ""
    occurrences: list[Occurrence] = field(default_factory=list)
    style: str = "full"  # "full" | "partial": keeps what deid.partial says may stay readable | "custom": masks `spans`
    spans: list[tuple[int, int]] = field(default_factory=list)  # style "custom": the ranges of `text` the reviewer masks
    group: str = ""  # names the reviewer or the model said are one person share a group; "" = on its own


@dataclass
class MaskPlan:
    text: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    meta: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    boxes: dict[str, list[Box]] = field(default_factory=dict)
    strings: list[str] = field(default_factory=list)  # masked wherever else they occur (places the screen does not show)
    partial: dict[str, list[tuple[int, int]]] = field(default_factory=dict)  # which of `strings` keep characters: the ranges masked
    lenient: list[str] = field(default_factory=list)  # masked like `strings`, but may stay readable inside a longer, partially masked one

    def search(self) -> list[str]:
        """Every string the writers look for outside the planned places."""
        return self.strings + [s for s in self.lenient if s not in self.strings]


def tight(unit) -> tuple[str, list[int]] | None:
    """A text unit read without the spaces put in at line ends (TextUnit.breaks): the text, and for each of its
    characters the position in the unit's own text. None for a unit that has no such spaces."""
    breaks = set(getattr(unit, "breaks", None) or ())
    if not breaks:
        return None
    keep = [i for i in range(len(unit.text)) if i not in breaks]
    return "".join(unit.text[i] for i in keep), keep


def to_json(obj) -> dict:
    return asdict(obj)


def _box(v) -> Box | None:
    return None if v is None else tuple(v)


def inventory_from_json(d: dict) -> Inventory:
    images = []
    for i in d.get("images", []):
        i = dict(i)
        i["areas"] = [_box(b) for b in i.get("areas", [])]
        i["faces"] = [_box(b) for b in i.get("faces", [])]
        i["ocr"] = [OcrLine(text=o["text"], box=_box(o["box"]), chars=[_box(c) for c in o["chars"]]) for o in i.get("ocr", [])]
        images.append(ImageUnit(**i))
    return Inventory(kind=d["kind"], texts=[TextUnit(**t) for t in d.get("texts", [])], images=images,
                     metas=[MetaUnit(**m) for m in d.get("metas", [])],
                     unchecked=[Unchecked(**u) for u in d.get("unchecked", [])], notes=list(d.get("notes", [])))


def finding_from_json(d: dict) -> Finding:
    d = dict(d)
    occs = []
    for o in d.get("occurrences", []):
        o = dict(o)
        o["box"] = _box(o.get("box"))
        o["page_boxes"] = [(p, _box(b)) for p, b in o.get("page_boxes", [])]
        occs.append(Occurrence(**o))
    d["occurrences"] = occs
    d["spans"] = [(int(s), int(e)) for s, e in d.get("spans") or []]
    return Finding(**d)
