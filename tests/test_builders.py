import io
import zipfile
from xml.dom import minidom

import pymupdf
from PIL import Image

from tests.builders import make_docx, make_pdf, text_image


def test_make_docx_writes_wellformed_parts(tmp_path):
    img = text_image(["登入者：王小明"])
    p = make_docx(tmp_path / "a.docx", ["第一段", ["王", "小", "明"]], table=[["承辦人", "王小明"]], header="頁首文字",
                  footer="頁尾文字", footnote="註腳文字", comment=("審閱者", "註解文字"), deleted="被刪除的字",
                  images=[("image1.png", img)], creator="林志豪", last_modified_by="陳美玲",
                  hyperlink=("寄信", "mailto:a@example.com"), thumbnail=img, embedded_xlsx_strings=["王小明"])
    with zipfile.ZipFile(p) as z:
        names = z.namelist()
        assert names[0] == "[Content_Types].xml"
        for n in names:
            if n.endswith((".xml", ".rels")):
                minidom.parseString(z.read(n))
        doc = z.read("word/document.xml").decode("utf-8")
        assert "<w:t>王</w:t>" in doc and "<w:t>小</w:t>" in doc
        assert "word/media/image1.png" in names and "word/embeddings/Workbook1.xlsx" in names
        assert any(n.startswith("docProps/thumbnail") for n in names)
        inner = zipfile.ZipFile(io.BytesIO(z.read("word/embeddings/Workbook1.xlsx")))
        assert "王小明" in inner.read("xl/sharedStrings.xml").decode("utf-8")


def test_make_pdf_has_requested_pages_and_text(tmp_path):
    p = make_pdf(tmp_path / "a.pdf", [{"lines": ["聯絡人 王小明", "電話 0912-345-678"]}, {"image": text_image(["掃描頁"]), "rotate": 90}],
                 author="林志豪")
    doc = pymupdf.open(p)
    assert doc.page_count == 2
    assert "王小明" in doc[0].get_text()
    assert doc[1].rotation == 90 and len(doc[1].get_images()) == 1
    assert doc.metadata["author"] == "林志豪"


def test_text_image_honours_format_and_mode():
    im = Image.open(io.BytesIO(text_image(["abc"], size=(300, 100), fmt="JPEG", mode="CMYK")))
    assert (im.format, im.mode, im.size) == ("JPEG", "CMYK", (300, 100))
