"""An Office file embedded in the document (review of 2026-11-23): its text is read by the paragraph, so a name
Word cut over two runs is found, masked and checked; its pictures are checked like the document's own; its
thumbnail goes; what is embedded in it in turn is reported. All names and numbers are made up."""
import io
import re
import zipfile

from deid import imaging
from deid.findings import FindingStore
from deid.formats.base import adapter_for
from deid.formats.docx import DocxAdapter
from deid.model import MaskPlan
from tests.builders import make_docx, make_xlsx, text_image

NAME = "王小明"
EMBEDDED = "word/embeddings/Microsoft_Word_Document.docx"


def inner_docx(tmp_path, paragraphs=(["承辦人 王小", "明 負責"], "其他內容"), **kw) -> bytes:
    return make_docx(tmp_path / "inner.docx", list(paragraphs), **kw).read_bytes()


def outer_docx(tmp_path, inner: bytes, paragraphs=("附件如下",), **kw):
    src = make_docx(tmp_path / "outer.docx", list(paragraphs), **kw)
    with zipfile.ZipFile(src, "a") as z:
        z.writestr(EMBEDDED, inner)
    return src


def inner_zip(path, name=EMBEDDED) -> zipfile.ZipFile:
    with zipfile.ZipFile(path) as z:
        return zipfile.ZipFile(io.BytesIO(z.read(name)))


def runs(xml: str) -> list[str]:
    return re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml)


def masked(src, tmp_path, settings, strings=(NAME,), boxes=None):
    ad = adapter_for(src, settings)
    store = FindingStore(ad.parse(src), lambda u, s, e: ad.locate(src, u, s, e))
    for s in strings:
        assert store.add_text(s, "姓名", "user") is not None
    for image_id, box in (boxes or {}).items():
        store.add_region(image_id, box, "其他", "user")
    plan, dst = store.plan(), tmp_path / ("out" + src.suffix)
    ad.apply(src, dst, plan, settings)
    return ad, plan, dst


# ---- text ---------------------------------------------------------------------------------------------------

def test_a_name_cut_over_two_runs_in_an_embedded_document_is_read_as_one(tmp_path, settings):
    src = outer_docx(tmp_path, inner_docx(tmp_path))
    texts = [(t.section, t.text) for t in adapter_for(src, settings).parse(src).texts]
    assert ("內嵌檔案", "承辦人 王小明 負責") in texts and ("內嵌檔案", "其他內容") in texts


def test_a_name_cut_over_two_runs_in_an_embedded_document_is_masked_and_checked(tmp_path, settings):
    src = outer_docx(tmp_path, inner_docx(tmp_path))
    ad, plan, dst = masked(src, tmp_path, settings)
    with inner_zip(dst) as z:
        assert runs(z.read("word/document.xml").decode("utf-8")) == ["承辦人 ■■", "■ 負責", "其他內容"]
    assert ad.residue(dst, plan.strings) == [] and ad.verify(src, dst, plan, settings) == []


def test_residue_sees_a_name_cut_over_two_runs_in_an_embedded_document(tmp_path, settings):
    src = outer_docx(tmp_path, inner_docx(tmp_path))
    found = adapter_for(src, settings).residue(src, [NAME])
    assert len(found) == 1 and found[0].startswith(EMBEDDED + "!word/document.xml")


def test_the_search_for_other_copies_goes_over_run_boundaries_in_an_embedded_document(tmp_path):
    out = DocxAdapter()._rewrite_nested(inner_docx(tmp_path), [NAME], "■", "*")
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        assert runs(z.read("word/document.xml").decode("utf-8")) == ["承辦人 ■■", "■ 負責", "其他內容"]


def test_a_name_cut_over_two_runs_in_a_document_embedded_in_a_workbook_is_masked(tmp_path, settings):
    src = make_xlsx(tmp_path / "a.xlsx", [{"name": "名單", "rows": [["附件"]]}])
    with zipfile.ZipFile(src, "a") as z:
        z.writestr("xl/embeddings/Microsoft_Word_Document.docx", inner_docx(tmp_path))
    ad, plan, dst = masked(src, tmp_path, settings)
    with inner_zip(dst, "xl/embeddings/Microsoft_Word_Document.docx") as z:
        assert runs(z.read("word/document.xml").decode("utf-8")) == ["承辦人 ■■", "■ 負責", "其他內容"]
    assert ad.residue(dst, plan.strings) == [] and ad.verify(src, dst, plan, settings) == []


def test_the_author_of_an_embedded_document_is_shown(tmp_path, settings):
    src = outer_docx(tmp_path, inner_docx(tmp_path, creator="林志豪"))
    inv = adapter_for(src, settings).parse(src)
    assert any(t.section.startswith("內嵌檔案") and t.text == "林志豪" for t in inv.texts)
    ad, plan, dst = masked(src, tmp_path, settings, strings=("林志豪",))
    with inner_zip(dst) as z:
        assert "林志豪" not in z.read("docProps/core.xml").decode("utf-8")
    assert ad.residue(dst, plan.strings) == [] and ad.verify(src, dst, plan, settings) == []


# ---- pictures and thumbnails --------------------------------------------------------------------------------

def test_a_picture_inside_an_embedded_document_is_listed_and_read(tmp_path, settings):
    picture = text_image(["身分證 A123456789"], size=(900, 200))
    src = outer_docx(tmp_path, inner_docx(tmp_path, images=[("image1.png", picture)]))
    ad = adapter_for(src, settings)
    inv = ad.parse(src)
    inside = [im for im in inv.images if im.name == EMBEDDED + "!word/media/image1.png"]
    assert len(inside) == 1 and inside[0].supported and inside[0].needs_scan
    assert ad.image_bytes(src, inside[0].id) == picture
    assert not [u for u in inv.unchecked if "media" in u.name]


def test_a_box_on_a_picture_inside_an_embedded_document_is_painted_and_checked(tmp_path, settings):
    picture = text_image(["身分證 A123456789"], size=(900, 200))
    src = outer_docx(tmp_path, inner_docx(tmp_path, images=[("image1.png", picture)]), images=[("image1.png", picture)])
    ad = adapter_for(src, settings)
    inside = next(im for im in ad.parse(src).images if "!" in im.name)
    box = (20.0, 20.0, 600.0, 120.0)
    ad, plan, dst = masked(src, tmp_path, settings, boxes={inside.id: box})
    with inner_zip(dst) as z:
        assert imaging.is_black(z.read("word/media/image1.png"), box)
    with zipfile.ZipFile(dst) as z:
        assert not imaging.is_black(z.read("word/media/image1.png"), box)  # the document's own picture was not asked for
    assert ad.verify(src, dst, plan, settings) == []


def test_verify_notices_a_box_inside_an_embedded_document_that_was_not_painted(tmp_path, settings):
    picture = text_image(["身分證 A123456789"], size=(900, 200))
    src = outer_docx(tmp_path, inner_docx(tmp_path, images=[("image1.png", picture)]))
    ad = adapter_for(src, settings)
    inside = next(im for im in ad.parse(src).images if "!" in im.name)
    plan = MaskPlan(boxes={inside.id: [(20.0, 20.0, 600.0, 120.0)]})
    assert any("未塗黑" in p for p in ad.verify(src, src, plan, settings))  # the "output" is the untouched file


def test_the_thumbnail_of_an_embedded_document_is_removed(tmp_path, settings):
    thumb = text_image(["承辦人 王小明"], size=(600, 200))
    src = outer_docx(tmp_path, inner_docx(tmp_path, paragraphs=("其他內容",), thumbnail=thumb))
    ad, plan, dst = masked(src, tmp_path, settings, strings=("附件如下",))
    with inner_zip(dst) as z:
        assert not [n for n in z.namelist() if "thumbnail" in n.lower()]
        assert "thumbnail" not in z.read("_rels/.rels").decode("utf-8")
    assert ad.verify(src, dst, plan, settings) == []


def test_verify_notices_a_thumbnail_left_in_an_embedded_document(tmp_path, settings):
    thumb = text_image(["承辦人 王小明"], size=(600, 200))
    src = outer_docx(tmp_path, inner_docx(tmp_path, paragraphs=("其他內容",), thumbnail=thumb))
    assert any("縮圖" in p for p in adapter_for(src, settings).verify(src, src, MaskPlan(), settings))


def test_what_is_embedded_inside_an_embedded_document_is_reported(tmp_path, settings):
    inner = inner_docx(tmp_path, embedded_xlsx_strings=["王小明"], ole_bin=b"\xd0\xcf\x11\xe0" + b"\0" * 64)
    src = outer_docx(tmp_path, inner)
    unchecked = {u.name for u in adapter_for(src, settings).parse(src).unchecked}
    assert {EMBEDDED + "!word/embeddings/Workbook1.xlsx", EMBEDDED + "!word/embeddings/oleObject1.bin"} <= unchecked
