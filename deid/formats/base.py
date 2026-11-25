"""What every file format adapter provides. Adapters are stateless: unit ids are stable across parses."""
from pathlib import Path
from typing import Protocol

from deid.config import Settings
from deid.model import Box, Inventory, MaskPlan, OcrLine


class UnsupportedFormat(Exception):
    """The file type is not handled in this version."""


class BrokenFile(Exception):
    """The file cannot be read (corrupt, encrypted, wrong type). The message is shown to the user."""


class Adapter(Protocol):
    kind: str

    def parse(self, src: Path) -> Inventory: ...
    def image_bytes(self, src: Path, image_id: str) -> bytes: ...
    def filter_ocr(self, src: Path, image_id: str, lines: list[OcrLine]) -> list[OcrLine]: ...
    def locate(self, src: Path, unit_id: str, start: int, end: int) -> list[tuple[str, Box]]: ...
    def view(self, src: Path, inv: Inventory) -> list[dict]: ...
    def apply(self, src: Path, dst: Path, plan: MaskPlan, settings: Settings) -> None: ...
    def residue(self, dst: Path, strings: list[str]) -> list[str]: ...
    def verify(self, src: Path, dst: Path, plan: MaskPlan, settings: Settings) -> list[str]: ...


SUPPORTED = (".docx", ".pdf", ".xlsx", ".xlsm")


def adapter_for(path: Path, settings: Settings | None = None) -> Adapter:
    ext = Path(path).suffix.lower()
    if ext == ".docx":
        from deid.formats.docx import DocxAdapter
        return DocxAdapter()
    if ext == ".pdf":
        from deid.formats.pdf import PdfAdapter
        return PdfAdapter(dpi=settings.pdf_dpi) if settings else PdfAdapter()
    if ext in (".xlsx", ".xlsm"):
        from deid.formats.xlsx import XlsxAdapter
        return XlsxAdapter()
    raise UnsupportedFormat(ext or "(無副檔名)")
