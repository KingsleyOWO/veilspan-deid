"""Word text that was neither read nor masked (review of 2026-11-23): field instructions cut over runs or kept
in an attribute, parts that were not read (the SmartArt drawing copy, the glossary, imported alt chunks),
attributes that hold typed text, CDATA sections, and chart text typed in place. Every document here is
hand-built and every name and number is made up."""
import codecs
import html
import re
import zipfile
from xml.dom import minidom
from xml.sax.saxutils import escape

from deid import codefile, codes
from deid.findings import FindingStore
from deid.formats.docx import HIDDEN, DocxAdapter
from deid.formats.xlsx import XlsxAdapter
from deid.formats.xmltok import find_in_text_nodes, iter_tokens, replace_in_text_nodes
from deid.pipeline import build_context
from tests.builders import make_xlsx
from tests.fakes import FakeFaces, FakeOcr, keyword_llm

NAME, PHONE, ID, EMAIL = "王小明", "0912-345-678", "A123456789", "someone@example.com"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
      f'xmlns:r="{REL}" '
      'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
      'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
      'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
      'xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office"')
CHART_NS = ('xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            f'xmlns:r="{REL}"')


def para(*texts: str) -> str:
    return "<w:p>" + "".join(f'<w:r><w:t xml:space="preserve">{escape(t)}</w:t></w:r>' for t in texts) + "</w:p>"


def word(path, body: str, *, rels=(), parts=(), defaults=()):
    """A small .docx around `body`. rels: (id, type, target[, external]) of the document part; parts: extra
    entries (name, str | bytes); defaults: extra (extension, content type) pairs."""
    types = [("rels", "application/vnd.openxmlformats-package.relationships+xml"), ("xml", "application/xml"),
             ("png", "image/png"), *defaults]
    rel_xml = HEAD + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    for rid, typ, target, *ext in rels:
        typ = typ if typ.startswith("http") else f"{REL}/{typ}"
        mode = ' TargetMode="External"' if ext and ext[0] else ""
        rel_xml += f'<Relationship Id="{rid}" Type="{typ}" Target="{escape(target)}"{mode}/>'
    rel_xml += "</Relationships>"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", HEAD + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   + "".join(f'<Default Extension="{e}" ContentType="{t}"/>' for e, t in types)
                   + '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                     'wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels", HEAD + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   f'<Relationship Id="rId1" Type="{REL}/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", HEAD + f"<w:document {NS}><w:body>{body}<w:sectPr/></w:body></w:document>")
        z.writestr("word/_rels/document.xml.rels", rel_xml)
        for name, data in parts:
            z.writestr(name, data)
    return path


def mask(src, tmp_path, settings, words, adapter=None):
    """Mask every place the strings are found, as the pipeline does. Returns (inventory, plan, output, residue,
    problems)."""
    ad = adapter or DocxAdapter()
    inv = ad.parse(src)
    store = FindingStore(inv)
    for w in words:
        assert store.add_text(w, "姓名", "user") is not None, w
    plan = store.plan()
    dst = tmp_path / ("out" + src.suffix)
    ad.apply(src, dst, plan, settings)
    return inv, plan, dst, ad.residue(dst, plan.strings), ad.verify(src, dst, plan, settings)


def read(path, name: str) -> str:
    with zipfile.ZipFile(path) as z:
        return z.read(name).decode("utf-8")


def leaks(path, strings) -> list[tuple[str, str]]:
    """Independent of the adapter: each entry's bytes, and its text with tags taken out and CDATA and entities
    resolved (so a string cut over runs or nodes is seen too)."""
    out = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            raw = z.read(name)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = ""
            text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", lambda m: escape(m.group(1)), text, flags=re.S)
            stripped = html.unescape(re.sub(r"<[^>]*>", "", text))
            for s in strings:
                if s.encode("utf-8") in raw or escape(s).encode("utf-8") in raw or s in stripped:
                    out.append((name, s))
    return out


def wellformed(path) -> None:
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            if n.endswith((".xml", ".rels")):
                minidom.parseString(z.read(n))


def section(inv, label: str) -> list[str]:
    return [t.text for t in inv.texts if t.section == label]


# ---- W1: field instructions --------------------------------------------------------------------

def field(*instr: str, result: str = "寫信給承辦人") -> str:
    """A complex field whose instruction is cut over one run per item."""
    return ('<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            + "".join(f'<w:r><w:instrText xml:space="preserve">{escape(i)}</w:instrText></w:r>' for i in instr)
            + f'<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>{escape(result)}</w:t></w:r>'
            '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')


SPLIT_LINK = field(' HYPERLINK "mailto:some', 'one@example.com" ')


def instructions(path) -> str:
    return "".join(html.unescape(t) for t in re.findall(r"<w:instrText[^>]*>([^<]*)</w:instrText>", read(path, "word/document.xml")))


def test_a_field_instruction_cut_over_two_runs_is_read_as_one(tmp_path):
    src = word(tmp_path / "a.docx", para("內文") + SPLIT_LINK)
    inv = DocxAdapter().parse(src)
    assert section(inv, HIDDEN + "功能變數") == [' HYPERLINK "mailto:someone@example.com" ']


def test_an_address_found_only_in_a_cut_instruction_is_masked_and_checked(tmp_path, settings):
    src = word(tmp_path / "a.docx", para("內文沒有個資") + SPLIT_LINK)
    _inv, _plan, dst, residue, problems = mask(src, tmp_path, settings, [EMAIL])
    assert residue == [] and problems == []
    assert leaks(dst, [EMAIL]) == []
    assert instructions(dst) == ' HYPERLINK "mailto:' + "*" * len(EMAIL) + '" '
    assert "寫信給承辦人" in read(dst, "word/document.xml")


def test_residue_sees_an_address_cut_over_two_instruction_runs(tmp_path):
    src = word(tmp_path / "a.docx", SPLIT_LINK)
    assert any("word/document.xml" in r for r in DocxAdapter().residue(src, [EMAIL]))


def test_a_number_cut_over_the_instruction_of_a_field_not_shown_is_masked_where_it_is_masked(tmp_path, settings):
    # a QUOTE field is not listed (it rarely holds data), but a string masked elsewhere is masked in it too
    src = word(tmp_path / "a.docx", para(f"電話 {PHONE}") + field(" QUOTE 0912-34", "5-678 ", result=PHONE))
    _inv, _plan, dst, residue, problems = mask(src, tmp_path, settings, [PHONE])
    assert residue == [] and problems == []
    assert leaks(dst, [PHONE]) == []
    assert instructions(dst) == " QUOTE ************ "


def test_the_instruction_around_a_nested_field_is_read_and_masked(tmp_path, settings):
    nested = ('<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> IF </w:instrText></w:r>'
              '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> MERGEFIELD 性別 </w:instrText></w:r>'
              '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>男</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r>'
              '<w:r><w:instrText xml:space="preserve"> = "男" "王小</w:instrText></w:r>'
              '<w:r><w:instrText xml:space="preserve">明 先生" "" </w:instrText></w:r>'
              '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>收件人</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')
    src = word(tmp_path / "a.docx", para("通知") + nested)
    inv = DocxAdapter().parse(src)
    assert sorted(section(inv, HIDDEN + "功能變數")) == sorted([' IF  = "男" "王小明 先生" "" ', " MERGEFIELD 性別 "])
    _inv, _plan, dst, residue, problems = mask(src, tmp_path, settings, [NAME])
    assert residue == [] and problems == [] and leaks(dst, [NAME]) == []
    assert "MERGEFIELD 性別" in instructions(dst)


SIMPLE = (f'<w:p><w:fldSimple w:instr=" HYPERLINK &quot;tel:{PHONE}&quot; "><w:r><w:t>撥打電話</w:t></w:r></w:fldSimple></w:p>'
          f'<w:p><w:fldSimple w:instr=" HYPERLINK &quot;mailto:{EMAIL}&quot; "><w:r><w:t>寫信</w:t></w:r></w:fldSimple></w:p>'
          '<w:p><w:fldSimple w:instr=" PAGE "><w:r><w:t>1</w:t></w:r></w:fldSimple></w:p>')


def test_the_instruction_of_a_simple_field_is_read(tmp_path):
    inv = DocxAdapter().parse(word(tmp_path / "a.docx", para("內文") + SIMPLE))
    assert sorted(section(inv, HIDDEN + "功能變數")) == sorted([f' HYPERLINK "tel:{PHONE}" ', f' HYPERLINK "mailto:{EMAIL}" '])


def test_the_instruction_of_a_simple_field_is_masked_and_checked(tmp_path, settings):
    src = word(tmp_path / "a.docx", para("內文") + SIMPLE)
    _inv, _plan, dst, residue, problems = mask(src, tmp_path, settings, [PHONE, EMAIL])
    assert residue == [] and problems == []
    assert leaks(dst, [PHONE, EMAIL]) == []
    doc = read(dst, "word/document.xml")
    assert 'w:instr=" HYPERLINK &quot;tel:************&quot; "' in doc and 'w:instr=" PAGE "' in doc
    wellformed(dst)


def test_residue_reads_the_instruction_of_a_simple_field(tmp_path):
    src = word(tmp_path / "a.docx", SIMPLE)
    assert any("word/document.xml" in r for r in DocxAdapter().residue(src, [PHONE]))


# ---- W2: parts that were not read ----------------------------------------------------------------

DGM_NS = ('xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram" '
          'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"')
DSP_NS = ('xmlns:dsp="http://schemas.microsoft.com/office/drawing/2008/diagram" '
          'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"')
CUT_NAME = '<a:r><a:t>組長 王小</a:t></a:r><a:r><a:rPr b="1"/><a:t>明</a:t></a:r>'


def smartart(tmp_path, drawing_runs=CUT_NAME):
    data = (HEAD + f'<dgm:dataModel {DGM_NS}><dgm:ptLst><dgm:pt modelId="{{1}}"><dgm:t><a:bodyPr/><a:p>{CUT_NAME}</a:p>'
            '</dgm:t></dgm:pt></dgm:ptLst></dgm:dataModel>')
    drawing = (HEAD + f'<dsp:drawing {DSP_NS}><dsp:spTree><dsp:sp><dsp:txBody><a:bodyPr/><a:p>{drawing_runs}</a:p>'
               '</dsp:txBody></dsp:sp></dsp:spTree></dsp:drawing>')
    return word(tmp_path / "s.docx", para("組織圖如下"),
                rels=[("rIdDm", "diagramData", "diagrams/data1.xml"),
                      ("rIdDd", "http://schemas.microsoft.com/office/2007/relationships/diagramDrawing", "diagrams/drawing1.xml")],
                parts=[("word/diagrams/data1.xml", data), ("word/diagrams/drawing1.xml", drawing)])


def test_the_drawing_copy_of_a_smartart_graphic_is_read_as_a_copy(tmp_path):
    inv = DocxAdapter().parse(smartart(tmp_path))
    copies = [t for t in inv.texts if t.text == "組長 王小明"]
    assert [(t.section, t.mirror) for t in copies] == [("圖表", False), ("圖表", True)]


def test_a_drawing_copy_that_says_something_else_is_shown(tmp_path):
    inv = DocxAdapter().parse(smartart(tmp_path, '<a:r><a:t>組長 陳美玲</a:t></a:r>'))
    other = [t for t in inv.texts if t.text == "組長 陳美玲"]
    assert [(t.section, t.mirror) for t in other] == [("圖表", False)]


def test_the_drawing_copy_of_a_smartart_graphic_is_masked_and_checked(tmp_path, settings):
    src = smartart(tmp_path)
    _inv, _plan, dst, residue, problems = mask(src, tmp_path, settings, [NAME])
    assert residue == [] and problems == []
    assert leaks(dst, [NAME]) == []
    assert re.findall(r"<a:t>([^<]*)</a:t>", read(dst, "word/diagrams/drawing1.xml")) == ["組長 ■■", "■"]


def test_residue_reads_the_drawing_copy_of_a_smartart_graphic(tmp_path):
    src = smartart(tmp_path)
    assert any("word/diagrams/drawing1.xml" in r for r in DocxAdapter().residue(src, [NAME]))


def glossary_doc(tmp_path, extra=()):
    glossary = (HEAD + f'<w:glossaryDocument {NS}><w:docParts><w:docPart><w:docPartPr><w:name w:val="簽名檔"/></w:docPartPr>'
                f'<w:docPartBody>{para("聯絡人 王小", f"明 {PHONE}")}</w:docPartBody></w:docPart></w:docParts></w:glossaryDocument>')
    return word(tmp_path / "g.docx", para("內文"), rels=[("rIdGl", "glossaryDocument", "glossary/document.xml")],
                parts=[("word/glossary/document.xml", glossary), *extra])


def test_the_glossary_is_read(tmp_path):
    inv = DocxAdapter().parse(glossary_doc(tmp_path))
    assert section(inv, HIDDEN + "建置組塊") == [f"聯絡人 王小明 {PHONE}"]


def test_the_glossary_is_masked_and_checked(tmp_path, settings):
    src = glossary_doc(tmp_path)
    _inv, _plan, dst, residue, problems = mask(src, tmp_path, settings, [NAME, PHONE])
    assert residue == [] and problems == []
    assert leaks(dst, [NAME, PHONE]) == []
    assert DocxAdapter().residue(src, [NAME]) != []


def test_a_file_in_the_glossary_that_is_not_read_is_reported(tmp_path):
    inv = DocxAdapter().parse(glossary_doc(tmp_path, [("word/glossary/media/image1.png", b"\x89PNG\r\n\x1a\nxx")]))
    assert "word/glossary/media/image1.png" in {u.name for u in inv.unchecked}


HTML_CHUNK = ('<html><head><meta charset="utf-8"><title>申請表</title></head><body>'
              f'<p>申請人：王<b>小明</b></p><p>手機：{PHONE}</p>'
              f'<p><a href="mailto:{EMAIL}">寫信</a> A&amp;B 公司</p></body></html>')


def chunk_doc(tmp_path, data: bytes, target="afchunk.htm", ctype="text/html"):
    ext = target.rsplit(".", 1)[1]
    return word(tmp_path / "c.docx", para("以下為申請人資料") + '<w:altChunk r:id="rIdChunk"/>' + para("（以上）"),
                rels=[("rIdChunk", "aFChunk", target)], defaults=[(ext, ctype)], parts=[(f"word/{target}", data)])


def test_an_html_alt_chunk_is_read(tmp_path):
    inv = DocxAdapter().parse(chunk_doc(tmp_path, HTML_CHUNK.encode("utf-8")))
    shown = section(inv, "匯入內容")
    assert "申請人：王小明" in shown and f"手機：{PHONE}" in shown and "寫信 A&B 公司" in shown
    assert f"mailto:{EMAIL}" in [t.text for t in inv.texts if t.section.startswith(HIDDEN)]
    assert not inv.unchecked


def test_an_html_alt_chunk_is_masked_and_checked(tmp_path, settings):
    src = chunk_doc(tmp_path, HTML_CHUNK.encode("utf-8"))
    _inv, _plan, dst, residue, problems = mask(src, tmp_path, settings, [NAME, PHONE, EMAIL])
    assert residue == [] and problems == []
    assert leaks(dst, [NAME, PHONE, EMAIL]) == []
    out = read(dst, "word/afchunk.htm")
    assert "<p>申請人：■<b>■■</b></p>" in out and "<p>手機：************</p>" in out
    assert 'href="mailto:*******************"' in out and "A&amp;B 公司" in out
    assert [t.text for t in DocxAdapter().parse(dst).texts if t.section == "匯入內容"][1:3] == ["申請人：■■■", "手機：************"]


def test_residue_reads_an_html_alt_chunk(tmp_path):
    src = chunk_doc(tmp_path, HTML_CHUNK.encode("utf-8"))
    assert any("word/afchunk.htm" in r for r in DocxAdapter().residue(src, [NAME]))


def test_what_a_page_saved_by_word_keeps_in_a_comment_is_read_and_masked(tmp_path, settings):
    page = ('<html><head><!--[if gte mso 9]><xml><o:DocumentProperties><o:Author>王小明</o:Author><o:Company>A123456789'
            '</o:Company></o:DocumentProperties></xml><![endif]--></head><body><p>申請表</p></body></html>')
    src = chunk_doc(tmp_path, page.encode("utf-8"))
    inv = DocxAdapter().parse(src)
    assert any("<o:Author>王小明</o:Author>" in t for t in section(inv, HIDDEN + "HTML 註解"))
    _inv, _plan, dst, residue, problems = mask(src, tmp_path, settings, [NAME, ID])
    assert residue == [] and problems == []
    assert leaks(dst, [NAME, ID]) == []
    # the markup stays markup; the page does not say it is UTF-8, so the mask goes in as character references
    assert "<o:Author>&#9632;&#9632;&#9632;</o:Author><o:Company>**********</o:Company>" in read(dst, "word/afchunk.htm")
    assert DocxAdapter().residue(src, [ID]) != []


def test_a_plain_text_alt_chunk_is_read_and_masked(tmp_path, settings):
    src = chunk_doc(tmp_path, f"申請人：{NAME}\r\n手機：{PHONE} <備註> A&B\r\n".encode("utf-8"), "chunk1.txt", "text/plain")
    inv = DocxAdapter().parse(src)
    assert section(inv, "匯入內容") == [f"申請人：{NAME}", f"手機：{PHONE} <備註> A&B"]
    _inv, _plan, dst, residue, problems = mask(src, tmp_path, settings, [NAME, PHONE])
    assert residue == [] and problems == []
    with zipfile.ZipFile(dst) as z:
        assert z.read("word/chunk1.txt") == "申請人：■■■\r\n手機：************ <備註> A&B\r\n".encode("utf-8")


def test_a_byte_order_mark_opening_an_alt_chunk_is_not_text_and_stays(tmp_path, settings):
    src = chunk_doc(tmp_path, codecs.BOM_UTF8 + f"{NAME} 的申請\n".encode("utf-8"), "chunk1.txt", "text/plain")
    assert section(DocxAdapter().parse(src), "匯入內容") == [f"{NAME} 的申請"]
    _inv, _plan, dst, residue, problems = mask(src, tmp_path, settings, [NAME])
    assert residue == [] and problems == []
    with zipfile.ZipFile(dst) as z:
        assert z.read("word/chunk1.txt") == codecs.BOM_UTF8 + "■■■ 的申請\n".encode("utf-8")


def test_an_alt_chunk_that_cannot_be_read_is_reported_as_not_checked(tmp_path):
    rtf = chunk_doc(tmp_path, b"{" + b"\\rtf1\\ansi " + "王小明".encode("big5").hex().encode() + b" 0912-345-678}",
                    "chunk.rtf", "application/rtf")
    assert "word/chunk.rtf" in {u.name for u in DocxAdapter().parse(rtf).unchecked}
    big5 = chunk_doc(tmp_path, f"<html><body><p>申請人：{NAME}</p></body></html>".encode("big5"))
    assert "word/afchunk.htm" in {u.name for u in DocxAdapter().parse(big5).unchecked}


def test_the_coded_file_keeps_an_alt_chunk_masked(tmp_path, settings):
    src = word(tmp_path / "c.docx", para(f"承辦人 {NAME}") + '<w:altChunk r:id="rIdChunk"/>',
               rels=[("rIdChunk", "aFChunk", "afchunk.htm")], defaults=[("htm", "text/html")],
               parts=[("word/afchunk.htm", HTML_CHUNK.encode("utf-8"))])
    ctx = build_context(src, tmp_path / "work", settings, llm=keyword_llm({}), ocr=FakeOcr(), faces=FakeFaces())
    ctx.load()
    ctx.store.add_text(NAME, "姓名", "user")
    coded = codes.build(ctx.inv, ctx.store)
    out = codefile.make(ctx, coded, tmp_path / "coded.docx")
    assert out.ok and out.left == 1
    assert "承辦人 [姓名1]" in read(tmp_path / "coded.docx", "word/document.xml")
    chunk = read(tmp_path / "coded.docx", "word/afchunk.htm")
    assert "申請人：■<b>■■</b>" in chunk and NAME not in chunk


# ---- W3: attributes that hold typed text, and CDATA ------------------------------------------------

ATTRS_BODY = (para("內文沒有個資")
              + '<w:sdt><w:sdtPr><w:dropDownList><w:listItem w:displayText="王小明" w:value="A123456789"/>'
                '<w:listItem w:displayText="（請選擇）" w:value="0"/></w:dropDownList></w:sdtPr>'
                '<w:sdtContent><w:p><w:r><w:t>（請選擇）</w:t></w:r></w:p></w:sdtContent></w:sdt>'
              + '<w:p><w:smartTag w:uri="urn:schemas-microsoft-com:office:smarttags" w:element="PersonName"><w:smartTagPr>'
                '<w:attr w:name="ProductID" w:val="0912-345-678"/></w:smartTagPr><w:r><w:t>某人</w:t></w:r></w:smartTag></w:p>'
              + '<w:p><w:r><w:pict><v:shape id="s1" alt="陳美玲的照片" style="width:10pt;height:10pt">'
                '<v:imagedata r:id="rIdImg" o:title="林志豪"/></v:shape></w:pict></w:r></w:p>'
              + '<w:p><w:r><w:drawing><wp:inline><wp:extent cx="1" cy="1"/><wp:docPr id="5" name="Picture 5">'
                '<a:hlinkClick r:id="rIdWeb" tooltip="someone@example.com 的個人頁"/></wp:docPr></wp:inline></w:drawing></w:r></w:p>'
              + '<w:p><w:r><w:fldChar w:fldCharType="begin"><w:ffData><w:name w:val="Text1"/><w:statusText w:type="text" '
                'w:val="承辦人張惠如"/><w:textInput><w:default w:val="李大同"/></w:textInput></w:ffData></w:fldChar></w:r>'
                '<w:r><w:instrText xml:space="preserve"> FORMTEXT </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r>'
                '<w:r><w:t>（未填）</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')
SETTINGS = HEAD + f'<w:settings {NS}><w:docVars><w:docVar w:name="contact" w:val="吳小華"/></w:docVars></w:settings>'
ATTR_VALUES = {"王小明": HIDDEN + "下拉選單", ID: HIDDEN + "下拉選單", PHONE: HIDDEN + "智慧標籤",
               "陳美玲的照片": HIDDEN + "圖片說明", "林志豪": HIDDEN + "圖片說明", f"{EMAIL} 的個人頁": HIDDEN + "連結說明",
               "承辦人張惠如": HIDDEN + "表單欄位", "李大同": HIDDEN + "表單欄位", "吳小華": HIDDEN + "文件變數"}
ATTR_STRINGS = ["王小明", ID, PHONE, "陳美玲", "林志豪", EMAIL, "張惠如", "李大同", "吳小華"]


def attrs_doc(tmp_path):
    return word(tmp_path / "t.docx", ATTRS_BODY,
                rels=[("rIdImg", "image", "media/image1.png"), ("rIdWeb", "hyperlink", "https://example.com/", True),
                      ("rIdSet", "settings", "settings.xml")],
                parts=[("word/settings.xml", SETTINGS)])


def test_attributes_that_hold_typed_text_are_read(tmp_path):
    inv = DocxAdapter().parse(attrs_doc(tmp_path))
    listed = {(t.text, t.section) for t in inv.texts}
    for value, label in ATTR_VALUES.items():
        assert (value, label) in listed, value
    assert not any(t.text in ("0", "Text1", "ProductID") for t in inv.texts if t.section.startswith(HIDDEN))


def test_attributes_that_hold_typed_text_are_masked_and_checked(tmp_path, settings):
    src = attrs_doc(tmp_path)
    _inv, _plan, dst, residue, problems = mask(src, tmp_path, settings, ATTR_STRINGS)
    assert residue == [] and problems == []
    assert leaks(dst, ATTR_STRINGS) == []
    doc = read(dst, "word/document.xml")
    assert 'w:displayText="■■■" w:value="**********"' in doc and 'w:value="0"' in doc and 'w:val="Text1"' in doc
    assert 'w:val="■■■"' in read(dst, "word/settings.xml")
    wellformed(dst)


def test_residue_reads_attributes_that_hold_typed_text(tmp_path):
    src = attrs_doc(tmp_path)
    for s in ATTR_STRINGS:
        assert DocxAdapter().residue(src, [s]), s


CDATA_BODY = para("申請人資料如下") + f'<w:p><w:r><w:t><![CDATA[申請人 {NAME} A&B <備註>]]></w:t></w:r></w:p>'


def test_text_in_a_cdata_section_is_read(tmp_path):
    inv = DocxAdapter().parse(word(tmp_path / "k.docx", CDATA_BODY))
    assert f"申請人 {NAME} A&B <備註>" in section(inv, "內文")


def test_text_in_a_cdata_section_is_masked_and_checked(tmp_path, settings):
    src = word(tmp_path / "k.docx", CDATA_BODY)
    _inv, _plan, dst, residue, problems = mask(src, tmp_path, settings, [NAME])
    assert residue == [] and problems == []
    assert leaks(dst, [NAME]) == []
    wellformed(dst)
    assert "申請人 ■■■ A&B <備註>" in section(DocxAdapter().parse(dst), "內文")


def test_residue_reads_a_cdata_section(tmp_path):
    src = word(tmp_path / "k.docx", CDATA_BODY)
    assert any("word/document.xml" in r for r in DocxAdapter().residue(src, [NAME]))


def test_the_tokenizer_reads_and_rewrites_cdata_as_text():
    xml = f"<a><t><![CDATA[x {NAME} & y]]></t><u>{NAME}</u></a>"
    assert [k for k, *_ in iter_tokens(xml)] == ["open", "open", "cdata", "close", "open", "text", "close", "close"]
    assert find_in_text_nodes(xml, [NAME], set()) == {NAME}
    out = replace_in_text_nodes(xml, [NAME], {"t"}, set(), "■", "*")
    assert out == f"<a><t>x ■■■ &amp; y</t><u>{NAME}</u></a>"
    assert replace_in_text_nodes(xml, ["陳美玲"], None, set(), "■", "*") == xml


# ---- W4: chart text typed in place --------------------------------------------------------------

CHART = (HEAD + f'<c:chartSpace {CHART_NS}><c:chart><c:plotArea><c:layout/>'
         '<c:barChart><c:barDir val="col"/><c:grouping val="clustered"/><c:ser><c:idx val="0"/><c:order val="0"/>'
         '<c:tx><c:v>林志豪的業績</c:v></c:tx>'
         '<c:cat><c:multiLvlStrRef><c:f>名單!$A$2:$B$3</c:f><c:multiLvlStrCache><c:ptCount val="2"/>'
         '<c:lvl><c:pt idx="0"><c:v>張惠如</c:v></c:pt><c:pt idx="1"><c:v>王小明</c:v></c:pt></c:lvl>'
         '<c:lvl><c:pt idx="0"><c:v>北區</c:v></c:pt></c:lvl></c:multiLvlStrCache></c:multiLvlStrRef></c:cat>'
         '<c:val><c:numRef><c:f>名單!$C$2:$C$3</c:f><c:numCache><c:formatCode>General</c:formatCode><c:ptCount val="2"/>'
         '<c:pt idx="0"><c:v>10</c:v></c:pt><c:pt idx="1"><c:v>20</c:v></c:pt></c:numCache></c:numRef></c:val>'
         # data labels taken from cells (Excel 2013 and later)
         '<c:extLst><c:ext uri="{02D57815-91ED-43cb-92C2-25804820EDAC}" '
         'xmlns:c15="http://schemas.microsoft.com/office/drawing/2012/chart"><c15:datalabelsRange><c15:f>名單!$D$2:$D$3</c15:f>'
         '<c15:dlblRangeCache><c:ptCount val="2"/><c:pt idx="0"><c:v>陳美玲負責</c:v></c:pt></c15:dlblRangeCache>'
         '</c15:datalabelsRange></c:ext></c:extLst></c:ser>'
         '<c:axId val="1"/><c:axId val="2"/></c:barChart><c:catAx><c:axId val="1"/><c:crossAx val="2"/></c:catAx>'
         '<c:valAx><c:axId val="2"/><c:crossAx val="1"/></c:valAx></c:plotArea></c:chart></c:chartSpace>')
CHART_STRINGS = ["林志豪", "張惠如", "王小明", "陳美玲"]


def test_a_typed_series_name_and_multi_level_labels_of_a_word_chart_are_read_masked_and_checked(tmp_path, settings):
    src = word(tmp_path / "ch.docx", para("業績圖"), rels=[("rIdC", "chart", "charts/chart1.xml")],
               parts=[("word/charts/chart1.xml", CHART)])
    inv = DocxAdapter().parse(src)
    assert {"林志豪的業績", "張惠如", "王小明", "北區", "陳美玲負責"} <= set(section(inv, "圖表"))
    _inv, _plan, dst, residue, problems = mask(src, tmp_path, settings, CHART_STRINGS)
    assert residue == [] and problems == []
    assert leaks(dst, CHART_STRINGS) == []
    chart = read(dst, "word/charts/chart1.xml")
    assert "<c:v>■■■的業績</c:v>" in chart and "<c:v>北區</c:v>" in chart and "<c:v>20</c:v>" in chart


def excel_with_chart(path):
    make_xlsx(path, [{"name": "名單", "rows": [["姓名", "區", "業績"], ["張惠如", "北區", 10], ["王小明", "北區", 20]]}])
    with zipfile.ZipFile(path) as z:
        parts = {i.filename: z.read(i) for i in z.infolist()}
    drawing = (HEAD + '<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
               f'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="{REL}"><xdr:twoCellAnchor>'
               '<xdr:from><xdr:col>4</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>1</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>'
               '<xdr:to><xdr:col>10</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>15</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>'
               '<xdr:graphicFrame macro=""><xdr:nvGraphicFramePr><xdr:cNvPr id="2" name="Chart 1"/><xdr:cNvGraphicFramePr/>'
               '</xdr:nvGraphicFramePr><xdr:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/></xdr:xfrm><a:graphic>'
               '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart"><c:chart '
               'xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" r:id="rId1"/></a:graphicData></a:graphic>'
               '</xdr:graphicFrame><xdr:clientData/></xdr:twoCellAnchor></xdr:wsDr>')
    rels = HEAD + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{}</Relationships>'
    parts["xl/charts/chart1.xml"] = CHART.encode()
    parts["xl/drawings/drawing1.xml"] = drawing.encode()
    parts["xl/drawings/_rels/drawing1.xml.rels"] = rels.format(
        f'<Relationship Id="rId1" Type="{REL}/chart" Target="../charts/chart1.xml"/>').encode()
    parts["xl/worksheets/_rels/sheet1.xml.rels"] = rels.format(
        f'<Relationship Id="rIdD" Type="{REL}/drawing" Target="../drawings/drawing1.xml"/>').encode()
    parts["xl/worksheets/sheet1.xml"] = parts["xl/worksheets/sheet1.xml"].replace(b"</sheetData>", b'</sheetData><drawing r:id="rIdD"/>')
    parts["[Content_Types].xml"] = parts["[Content_Types].xml"].replace(b"</Types>", (
        b'<Override PartName="/xl/charts/chart1.xml" ContentType="application/vnd.openxmlformats-officedocument.drawingml.chart+xml"/>'
        b'<Override PartName="/xl/drawings/drawing1.xml" ContentType="application/vnd.openxmlformats-officedocument.drawing+xml"/></Types>'))
    with zipfile.ZipFile(path, "w") as z:
        for n, d in parts.items():
            z.writestr(n, d)
    return path


def test_a_typed_series_name_and_multi_level_labels_of_an_excel_chart_are_read_masked_and_checked(tmp_path, settings):
    src = excel_with_chart(tmp_path / "ch.xlsx")
    inv = XlsxAdapter().parse(src)
    assert {"林志豪的業績", "陳美玲負責"} <= {t.text for t in inv.texts}
    _inv, _plan, dst, residue, problems = mask(src, tmp_path, settings, CHART_STRINGS, XlsxAdapter())
    assert residue == [] and problems == []
    assert leaks(dst, CHART_STRINGS) == []
    assert "<c:v>■■■的業績</c:v>" in read(dst, "xl/charts/chart1.xml")
