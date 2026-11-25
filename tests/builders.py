"""Synthetic documents for tests. Everything here is invented data."""
import glob
import io
import zipfile
from functools import lru_cache
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import pymupdf
from PIL import Image, ImageDraw, ImageFont

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
NS = (f'{W} {R} xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
      'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
      'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
      'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
      'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
      'xmlns:v="urn:schemas-microsoft-com:vml"')
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


def make_encrypted_restore_file(table: dict, password: str, **header) -> bytes:
    """A restore file as it was written until 2026-11-19: the table encrypted with a password (scrypt,
    AES-256-GCM). The program no longer writes this format and still opens it. `header` overrides fields."""
    import base64
    import json
    import os

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    salt, nonce = os.urandom(16), os.urandom(12)
    head = {"format": "deid-restore", "v": 1, "kdf": "scrypt", "n": 2 ** 15, "r": 8, "p": 1,
            "salt": base64.b64encode(salt).decode("ascii"), "nonce": base64.b64encode(nonce).decode("ascii")}
    key = Scrypt(salt=salt, length=32, n=head["n"], r=head["r"], p=head["p"]).derive(password.encode("utf-8"))
    body = json.dumps({"created": "2026-11-18T10:00:00", "codes": table}, ensure_ascii=False).encode("utf-8")
    bound = json.dumps(head, sort_keys=True, separators=(",", ":")).encode("ascii")
    sealed = AESGCM(key).encrypt(nonce, body, bound)
    return json.dumps({**head, **header, "data": base64.b64encode(sealed).decode("ascii")}).encode("ascii")


@lru_cache
def cjk_font(size: int = 34):
    for pat in ("/usr/share/fonts/**/*CJK*", "/usr/share/fonts/**/*cjk*", "/usr/share/fonts/**/*.ttc"):
        hits = sorted(glob.glob(pat, recursive=True))
        if hits:
            return ImageFont.truetype(hits[0], size)
    return ImageFont.load_default()


def text_image(lines, *, size=(1000, 300), fmt="PNG", mode="RGB", font_size=34, origin=(30, 30)) -> bytes:
    im = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(im)
    for i, line in enumerate(lines):
        d.text((origin[0], origin[1] + i * int(font_size * 1.6)), line, font=cjk_font(font_size), fill="black")
    if mode != "RGB":
        im = im.convert(mode)
    out = io.BytesIO()
    im.save(out, fmt)
    return out.getvalue()


def _run(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r>' if " " in text else f"<w:r><w:t>{escape(text)}</w:t></w:r>"


def _para(content) -> str:
    runs = [content] if isinstance(content, str) else list(content)
    return "<w:p>" + "".join(_run(r) for r in runs) + "</w:p>"


def _drawing(rid: str, n: int, alt: str, name: str | None = None) -> str:
    name = quoteattr(name or f"Picture {n}")
    return (f'<w:p><w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="3000000" cy="900000"/>'
            f'<wp:docPr id="{n}" name={name} descr={quoteattr(alt)}/><a:graphic><a:graphicData '
            f'uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic><pic:nvPicPr><pic:cNvPr id="{n}" '
            f'name={name}/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill><a:blip r:embed="{rid}"/><a:stretch>'
            f'<a:fillRect/></a:stretch></pic:blipFill><pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="3000000" '
            f'cy="900000"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>'
            f'</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>')


def _xlsx(strings) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", HEAD + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/></Types>')
        z.writestr("xl/sharedStrings.xml", HEAD + '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   + "".join(f"<si><t>{escape(s)}</t></si>" for s in strings) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", HEAD + '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   '<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>2301</v></c></row></sheetData></worksheet>')
    return out.getvalue()


def make_docx(path, paragraphs, *, table=None, header=None, footer=None, footnote=None, comment=None, deleted=None,
              deleted_author="修訂者", images=(), image_alt="", creator="", last_modified_by="", manager="", company="",
              title="", hyperlink=None, thumbnail=None, embedded_xlsx_strings=None, textbox=None, custom_xml_text=None,
              field_instr=None, ole_bin=None, extra_media=(), image_name=None, chart_labels=None, bookmark=None,
              people=None) -> Path:
    """Write a small but valid .docx. A paragraph given as a list is split into one run per item."""
    rels = []  # (id, type, target, external)
    body = []
    overrides = ['<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                 'wordprocessingml.document.main+xml"/>',
                 '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>',
                 '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>']
    files: list[tuple[str, bytes]] = []

    first = True
    for p in paragraphs:
        xml = _para(p)
        if first:
            extra = ""
            if footnote is not None:
                extra += '<w:r><w:footnoteReference w:id="2"/></w:r>'
            if comment is not None:
                extra += '<w:r><w:commentReference w:id="0"/></w:r>'
            xml = xml[:-len("</w:p>")] + extra + "</w:p>"
            first = False
        body.append(xml)
    if table:
        rows = "".join("<w:tr>" + "".join(f"<w:tc><w:tcPr><w:tcW w:w=\"2301\" w:type=\"dxa\"/></w:tcPr>{_para(c)}</w:tc>"
                                          for c in row) + "</w:tr>" for row in table)
        body.append(f"<w:tbl><w:tblPr><w:tblW w:w=\"0\" w:type=\"auto\"/></w:tblPr>{rows}</w:tbl>")
    if deleted is not None:
        body.append(f'<w:p><w:del w:id="9" w:author={quoteattr(deleted_author)} w:date="2026-01-01T00:00:00Z"><w:r>'
                    f"<w:delText>{escape(deleted)}</w:delText></w:r></w:del></w:p>")
    if hyperlink is not None:
        rels.append(("rIdLink", f"{REL}/hyperlink", hyperlink[1], True))
        body.append(f'<w:p><w:hyperlink r:id="rIdLink">{_run(hyperlink[0])}</w:hyperlink></w:p>')
    if field_instr is not None:
        body.append('<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve">'
                    f"{escape(field_instr)}</w:instrText></w:r><w:r><w:fldChar w:fldCharType=\"end\"/></w:r></w:p>")
    if textbox is not None:
        inner = _para(textbox)
        body.append('<w:p><w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><wp:inline><wp:extent cx="1" cy="1"/>'
                    '<wp:docPr id="77" name="TextBox 77"/><a:graphic><a:graphicData uri="http://schemas.microsoft.com/office/'
                    f'word/2010/wordprocessingShape"><wps:wsp><wps:txbx><w:txbxContent>{inner}</w:txbxContent></wps:txbx>'
                    '<wps:bodyPr/></wps:wsp></a:graphicData></a:graphic></wp:inline></w:drawing></mc:Choice><mc:Fallback>'
                    f'<w:pict><v:shape><v:textbox><w:txbxContent>{inner}</w:txbxContent></v:textbox></v:shape></w:pict>'
                    '</mc:Fallback></mc:AlternateContent></w:r></w:p>')
    exts = {"rels": "application/vnd.openxmlformats-package.relationships+xml", "xml": "application/xml"}
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "emf": "image/x-emf",
            "bmp": "image/bmp", "tiff": "image/tiff", "bin": "application/vnd.openxmlformats-officedocument.oleObject",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
    for n, (name, data) in enumerate(images, 1):
        rels.append((f"rIdImg{n}", f"{REL}/image", f"media/{name}", False))
        body.append(_drawing(f"rIdImg{n}", n, image_alt, image_name))
        files.append((f"word/media/{name}", data))
        exts[name.rsplit(".", 1)[-1].lower()] = mime[name.rsplit(".", 1)[-1].lower()]
    for name, data in extra_media:  # present in the package but not referenced from the body
        files.append((f"word/media/{name}", data))
        exts[name.rsplit(".", 1)[-1].lower()] = mime[name.rsplit(".", 1)[-1].lower()]
    if bookmark is not None:
        body.append(f'<w:p><w:bookmarkStart w:id="5" w:name={quoteattr(bookmark)}/><w:r><w:t>書籤處</w:t></w:r><w:bookmarkEnd w:id="5"/></w:p>')
    if chart_labels is not None:
        rels.append(("rIdChart", f"{REL}/chart", "charts/chart1.xml", False))
        pts = "".join(f'<c:pt idx="{i}"><c:v>{escape(v)}</c:v></c:pt>' for i, v in enumerate(chart_labels))
        nums = "".join(f'<c:pt idx="{i}"><c:v>{2301 + i}</c:v></c:pt>' for i in range(len(chart_labels)))
        files.append(("word/charts/chart1.xml", (HEAD + '<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
                      'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><c:chart><c:plotArea><c:barChart><c:ser>'
                      f'<c:cat><c:strRef><c:f>Sheet1!$A$2:$A$9</c:f><c:strCache>{pts}</c:strCache></c:strRef></c:cat>'
                      f'<c:val><c:numRef><c:f>Sheet1!$B$2:$B$9</c:f><c:numCache>{nums}</c:numCache></c:numRef></c:val>'
                      '</c:ser></c:barChart></c:plotArea></c:chart></c:chartSpace>').encode()))
        overrides.append('<Override PartName="/word/charts/chart1.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                         'drawingml.chart+xml"/>')
    if people is not None:
        files.append(("word/people.xml", (HEAD + '<w15:people xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml">'
                      + "".join(f'<w15:person w15:author={quoteattr(a)}><w15:presenceInfo w15:providerId="AD" w15:userId={quoteattr(u)}/>'
                                '</w15:person>' for a, u in people) + "</w15:people>").encode()))
    if embedded_xlsx_strings is not None:
        rels.append(("rIdXlsx", f"{REL}/package", "embeddings/Workbook1.xlsx", False))
        files.append(("word/embeddings/Workbook1.xlsx", _xlsx(embedded_xlsx_strings)))
        exts["xlsx"] = mime["xlsx"]
    if ole_bin is not None:
        rels.append(("rIdOle", f"{REL}/oleObject", "embeddings/oleObject1.bin", False))
        files.append(("word/embeddings/oleObject1.bin", ole_bin))
        exts["bin"] = mime["bin"]

    sect = ""
    if header is not None:
        rels.append(("rIdHdr", f"{REL}/header", "header1.xml", False))
        files.append(("word/header1.xml", (HEAD + f"<w:hdr {NS}>{_para(header)}</w:hdr>").encode()))
        overrides.append('<Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                         'wordprocessingml.header+xml"/>')
        sect += '<w:headerReference w:type="default" r:id="rIdHdr"/>'
    if footer is not None:
        rels.append(("rIdFtr", f"{REL}/footer", "footer1.xml", False))
        files.append(("word/footer1.xml", (HEAD + f"<w:ftr {NS}>{_para(footer)}</w:ftr>").encode()))
        overrides.append('<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                         'wordprocessingml.footer+xml"/>')
        sect += '<w:footerReference w:type="default" r:id="rIdFtr"/>'
    if footnote is not None:
        rels.append(("rIdFn", f"{REL}/footnotes", "footnotes.xml", False))
        files.append(("word/footnotes.xml", (HEAD + f'<w:footnotes {NS}><w:footnote w:type="separator" w:id="0"><w:p><w:r>'
                      '<w:separator/></w:r></w:p></w:footnote><w:footnote w:type="continuationSeparator" w:id="1"><w:p><w:r>'
                      f'<w:continuationSeparator/></w:r></w:p></w:footnote><w:footnote w:id="2">{_para(footnote)}'
                      "</w:footnote></w:footnotes>").encode()))
        overrides.append('<Override PartName="/word/footnotes.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                         'wordprocessingml.footnotes+xml"/>')
    if comment is not None:
        rels.append(("rIdCm", f"{REL}/comments", "comments.xml", False))
        files.append(("word/comments.xml", (HEAD + f'<w:comments {NS}><w:comment w:id="0" w:author={quoteattr(comment[0])} '
                      f'w:date="2026-01-01T00:00:00Z" w:initials={quoteattr(comment[0][:1])}>{_para(comment[1])}'
                      "</w:comment></w:comments>").encode()))
        overrides.append('<Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                         'wordprocessingml.comments+xml"/>')
    document = (HEAD + f"<w:document {NS}><w:body>{''.join(body)}<w:sectPr>{sect}<w:pgSz w:w=\"11906\" w:h=\"16838\"/>"
                "</w:sectPr></w:body></w:document>")
    root_rels = [("rId1", f"{REL}/officeDocument", "word/document.xml"),
                 ("rId2", "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties", "docProps/core.xml"),
                 ("rId3", f"{REL}/extended-properties", "docProps/app.xml")]
    if thumbnail is not None:
        root_rels.append(("rId4", "http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail",
                          "docProps/thumbnail.png"))
        files.append(("docProps/thumbnail.png", thumbnail))
        exts["png"] = mime["png"]
    if custom_xml_text is not None:
        files.append(("customXml/item1.xml", (HEAD + f'<root xmlns="urn:example:custom"><owner>{escape(custom_xml_text)}</owner></root>').encode()))
    core = (HEAD + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f"<dc:title>{escape(title)}</dc:title><dc:creator>{escape(creator)}</dc:creator>"
            f"<cp:lastModifiedBy>{escape(last_modified_by)}</cp:lastModifiedBy><cp:revision>2</cp:revision>"
            '<dcterms:created xsi:type="dcterms:W3CDTF">2026-01-01T00:00:00Z</dcterms:created></cp:coreProperties>')
    app = (HEAD + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
           'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><Application>Test</Application>'
           f"<Manager>{escape(manager)}</Manager><Company>{escape(company)}</Company></Properties>")
    types = (HEAD + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             + "".join(f'<Default Extension="{e}" ContentType="{t}"/>' for e, t in exts.items()) + "".join(overrides) + "</Types>")

    def rels_xml(items):
        out = HEAD + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        for item in items:
            rid, typ, target = item[:3]
            mode = ' TargetMode="External"' if len(item) > 3 and item[3] else ""
            out += f'<Relationship Id="{rid}" Type="{typ}" Target={quoteattr(target)}{mode}/>'
        return out + "</Relationships>"

    path = Path(path)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", types)
        z.writestr("_rels/.rels", rels_xml(root_rels))
        z.writestr("word/document.xml", document)
        z.writestr("word/_rels/document.xml.rels", rels_xml(rels))
        z.writestr("docProps/core.xml", core)
        z.writestr("docProps/app.xml", app)
        for name, data in files:
            stored = name.startswith("word/media/")
            z.writestr(zipfile.ZipInfo(name), data, zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED)
    return path


def make_pdf(path, pages, *, author="", title="", password=None, xmp=None) -> Path:
    """pages: dicts with optional keys lines (list[str]), image (bytes, fills the page), rotate (int),
    hidden (list[str], invisible text layer), fontsize, annot (str), widget ((name, value)), link (uri),
    cropbox (rect). Keyword xmp sets an XMP packet."""
    doc = pymupdf.open()
    for spec in pages:
        page = doc.new_page(width=595, height=842)
        if spec.get("image"):
            page.insert_image(spec.get("image_rect") or page.rect, stream=spec["image"])
        size = spec.get("fontsize", 14)
        for i, line in enumerate(spec.get("lines", [])):
            page.insert_text((72, 100 + i * spec.get("leading", size * 2)), line, fontsize=size, fontname="china-t")
        for i, line in enumerate(spec.get("hidden", [])):
            page.insert_text((72, 100 + i * size * 2), line, fontsize=size, fontname="china-t", render_mode=3)
        if spec.get("annot"):
            page.add_text_annot((300, 60), spec["annot"])
        if spec.get("widget"):
            w = pymupdf.Widget()
            w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
            w.field_name, w.field_value = spec["widget"]
            w.rect = pymupdf.Rect(72, 400, 300, 424)
            page.add_widget(w)
        if spec.get("link"):
            page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 300, 220, 320), "uri": spec["link"]})
        if spec.get("cropbox"):
            page.set_cropbox(pymupdf.Rect(*spec["cropbox"]))
        if spec.get("rotate"):
            page.set_rotation(spec["rotate"])
    doc.set_metadata({"author": author, "title": title})
    if xmp:
        doc.set_xml_metadata(xmp)
    path = Path(path)
    if password:
        doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=password, owner_pw=password)
    else:
        doc.save(path)
    doc.close()
    return path


def _col(n: int) -> str:
    out = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def make_xlsx(path, sheets, *, creator="", comment=None, image=None, header=None, hyperlink=None, defined_name=None,
              calc_chain=False, rich=None, validation=None, pivot_items=None, thumbnail=None) -> Path:
    """Write a small valid .xlsx. `sheets` is a list of dicts: name, rows (list of lists), state ("hidden").
    Cell values: str -> shared string; int/float -> number; ("date", serial) -> number with a date style;
    ("formula", "A1&B1", "cached text") -> string formula; ("numformula", "SUM(A1:A2)", 3) -> numeric formula;
    ("inline", "text") -> inline string. `rich` = (text_a, text_b) adds a two-run shared string at index 0 use via
    ("rich",). comment = (author, text, "A1") on the first sheet."""
    shared: list[str] = []
    rich_xml = ""
    if rich:
        rich_xml = f"<si><r><t>{escape(rich[0])}</t></r><r><rPr><b/></rPr><t>{escape(rich[1])}</t></r></si>"

    def sidx(text):
        if text not in shared:
            shared.append(text)
        return shared.index(text) + (1 if rich else 0)

    sheet_xml = []
    formulas = []
    for si, sheet in enumerate(sheets):
        rows = ""
        for r, row in enumerate(sheet.get("rows", []), 1):
            cells = ""
            for c, v in enumerate(row):
                ref = f"{_col(c)}{r}"
                if v is None or v == "":
                    continue
                if isinstance(v, str):
                    cells += f'<c r="{ref}" t="s"><v>{sidx(v)}</v></c>'
                elif isinstance(v, (int, float)):
                    cells += f'<c r="{ref}"><v>{v}</v></c>'
                elif v[0] == "date":
                    cells += f'<c r="{ref}" s="1"><v>{v[1]}</v></c>'
                elif v[0] == "formula":
                    cells += f'<c r="{ref}" t="str"><f>{escape(v[1])}</f><v>{escape(v[2])}</v></c>'
                    formulas.append((ref, si + 1))
                elif v[0] == "numformula":
                    cells += f'<c r="{ref}"><f>{escape(v[1])}</f><v>{v[2]}</v></c>'
                    formulas.append((ref, si + 1))
                elif v[0] == "inline":
                    cells += f'<c r="{ref}" t="inlineStr"><is><t>{escape(v[1])}</t></is></c>'
                elif v[0] == "rich":
                    cells += f'<c r="{ref}" t="s"><v>0</v></c>'
            rows += f'<row r="{r}">{cells}</row>'
        extra = ""
        if validation and si == 0:
            extra += (f'<dataValidations count="1"><dataValidation type="list" sqref="Z1"><formula1>&quot;{escape(validation)}&quot;'
                      "</formula1></dataValidation></dataValidations>")
        if hyperlink and si == 0:
            extra += f'<hyperlinks><hyperlink ref="A1" r:id="rIdLink" display={quoteattr(hyperlink[0])}/></hyperlinks>'
        if header and si == 0:
            extra += f"<headerFooter><oddHeader>&amp;C{escape(header)}</oddHeader></headerFooter>"
        if image and si == 0:
            extra += '<drawing r:id="rIdDraw"/>'
        if comment and si == 0:
            extra += '<legacyDrawing r:id="rIdVml"/>'
        sheet_xml.append(HEAD + '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                         f'xmlns:r="{REL}"><sheetData>{rows}</sheetData>{extra}</worksheet>')

    files: list[tuple[str, bytes]] = []
    types = ['<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
             '<Default Extension="xml" ContentType="application/xml"/>', '<Default Extension="png" ContentType="image/png"/>',
             '<Default Extension="vml" ContentType="application/vnd.openxmlformats-officedocument.vmlDrawing"/>',
             '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
             '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>',
             '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
             '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>',
             '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>']
    wb_rels = [("rIdSS", f"{REL}/sharedStrings", "sharedStrings.xml"), ("rIdSt", f"{REL}/styles", "styles.xml")]
    sheet_tags = ""
    for i, sheet in enumerate(sheets, 1):
        state = f' state="{sheet["state"]}"' if sheet.get("state") else ""
        sheet_tags += f'<sheet name={quoteattr(sheet["name"])} sheetId="{i}"{state} r:id="rId{i}"/>'
        wb_rels.append((f"rId{i}", f"{REL}/worksheet", f"worksheets/sheet{i}.xml"))
        types.append(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                     'spreadsheetml.worksheet+xml"/>')
        files.append((f"xl/worksheets/sheet{i}.xml", sheet_xml[i - 1].encode()))
    names = f"<definedNames><definedName name={quoteattr(defined_name[0])}>{escape(defined_name[1])}</definedName></definedNames>" if defined_name else ""
    workbook = (HEAD + f'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="{REL}">'
                f"<sheets>{sheet_tags}</sheets>{names}</workbook>")
    sst = (HEAD + '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' + rich_xml
           + "".join(f"<si><t xml:space=\"preserve\">{escape(t)}</t></si>" for t in shared) + "</sst>")
    styles = (HEAD + '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="1"><font/></fonts>'
              '<fills count="1"><fill/></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs>'
              '<cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="14" applyNumberFormat="1"/></cellXfs></styleSheet>')
    sheet1_rels = []
    if hyperlink:
        sheet1_rels.append(("rIdLink", f"{REL}/hyperlink", hyperlink[1], True))
    if image:
        sheet1_rels.append(("rIdDraw", f"{REL}/drawing", "../drawings/drawing1.xml", False))
        files.append(("xl/media/image1.png", image))
        files.append(("xl/drawings/drawing1.xml", (HEAD + '<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/'
                      f'spreadsheetDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="{REL}">'
                      '<xdr:oneCellAnchor><xdr:from><xdr:col>3</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>3</xdr:row><xdr:rowOff>0'
                      '</xdr:rowOff></xdr:from><xdr:ext cx="3000000" cy="900000"/><xdr:pic><xdr:nvPicPr><xdr:cNvPr id="2" '
                      'name="Picture 1" descr="王小明的截圖"/><xdr:cNvPicPr/></xdr:nvPicPr><xdr:blipFill><a:blip r:embed="rIdImg"/>'
                      '<a:stretch><a:fillRect/></a:stretch></xdr:blipFill><xdr:spPr><a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
                      "</xdr:spPr></xdr:pic><xdr:clientData/></xdr:oneCellAnchor></xdr:wsDr>").encode()))
        files.append(("xl/drawings/_rels/drawing1.xml.rels", None))
        types.append('<Override PartName="/xl/drawings/drawing1.xml" ContentType="application/vnd.openxmlformats-officedocument.drawing+xml"/>')
    if comment:
        sheet1_rels.append(("rIdCm", f"{REL}/comments", "../comments1.xml", False))
        sheet1_rels.append(("rIdVml", f"{REL}/vmlDrawing", "../drawings/vmlDrawing1.vml", False))
        files.append(("xl/comments1.xml", (HEAD + '<comments xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><authors>'
                      f"<author>{escape(comment[0])}</author></authors><commentList><comment ref=\"{comment[2]}\" authorId=\"0\"><text>"
                      f"<r><t>{escape(comment[1][:2])}</t></r><r><t>{escape(comment[1][2:])}</t></r></text></comment></commentList></comments>").encode()))
        files.append(("xl/drawings/vmlDrawing1.vml", b'<xml xmlns:v="urn:schemas-microsoft-com:vml"></xml>'))
        types.append('<Override PartName="/xl/comments1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.comments+xml"/>')
    if calc_chain:
        wb_rels.append(("rIdCalc", f"{REL}/calcChain", "calcChain.xml"))
        types.append('<Override PartName="/xl/calcChain.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.calcChain+xml"/>')
        files.append(("xl/calcChain.xml", (HEAD + '<calcChain xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                      + "".join(f'<c r="{ref}" i="{i}"/>' for ref, i in formulas) + "</calcChain>").encode()))
    if pivot_items:
        files.append(("xl/pivotCache/pivotCacheDefinition1.xml", (HEAD + '<pivotCacheDefinition xmlns="http://schemas.openxmlformats.org/'
                      'spreadsheetml/2006/main"><cacheFields count="1"><cacheField name="姓名"><sharedItems>'
                      + "".join(f"<s v={quoteattr(v)}/>" for v in pivot_items) + '<n v="2301"/></sharedItems></cacheField></cacheFields>'
                      "</pivotCacheDefinition>").encode()))
    root_rels = [("rId1", f"{REL}/officeDocument", "xl/workbook.xml"),
                 ("rId2", "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties", "docProps/core.xml"),
                 ("rId3", f"{REL}/extended-properties", "docProps/app.xml")]
    if thumbnail:
        root_rels.append(("rId4", "http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail", "docProps/thumbnail.png"))
        files.append(("docProps/thumbnail.png", thumbnail))
    core = (HEAD + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            f'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator>{escape(creator)}</dc:creator></cp:coreProperties>')
    app = (HEAD + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
           'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><Application>Test</Application>'
           f'<TitlesOfParts><vt:vector size="{len(sheets)}" baseType="lpstr">'
           + "".join(f"<vt:lpstr>{escape(s_['name'])}</vt:lpstr>" for s_ in sheets) + "</vt:vector></TitlesOfParts></Properties>")

    def rels_xml(items):
        out = HEAD + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        for item in items:
            mode = ' TargetMode="External"' if len(item) > 3 and item[3] else ""
            out += f'<Relationship Id="{item[0]}" Type="{item[1]}" Target={quoteattr(item[2])}{mode}/>'
        return out + "</Relationships>"

    path = Path(path)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", HEAD + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   + "".join(types) + "</Types>")
        z.writestr("_rels/.rels", rels_xml(root_rels))
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", rels_xml(wb_rels))
        z.writestr("xl/sharedStrings.xml", sst)
        z.writestr("xl/styles.xml", styles)
        z.writestr("docProps/core.xml", core)
        z.writestr("docProps/app.xml", app)
        if sheet1_rels:
            z.writestr("xl/worksheets/_rels/sheet1.xml.rels", rels_xml(sheet1_rels))
        for name, data in files:
            if name == "xl/drawings/_rels/drawing1.xml.rels":
                data = rels_xml([("rIdImg", f"{REL}/image", "../media/image1.png")]).encode()
            z.writestr(name, data)
    return path
