"""Regression tests for the findings of the whole-branch review. Each one failed before its fix."""
import inspect
import io
import json
import zipfile

import httpx
import pymupdf
import pytest
from fastapi.testclient import TestClient

from deid.agent import tools
from deid.agent.loop import run_agent
from deid.findings import FindingStore
from deid.formats.base import BrokenFile
from deid.formats.docx import DocxAdapter
from deid.formats.pdf import PdfAdapter
from deid.jobs import JobStore
from deid.llm import LLMClient
from deid.model import ImageUnit, Inventory, MaskPlan, TextUnit
from deid.pipeline import analyze, apply_and_verify, build_context
from deid.web.app import create_app
from tests.builders import make_docx, make_pdf, text_image
from tests.fakes import FakeFaces, FakeLLM, FakeOcr, keyword_llm, ocr_line, reply, tool_call


def units(inv, section_prefix):
    return [t.text for t in inv.texts if t.section.startswith(section_prefix)]


def entries(path):
    with zipfile.ZipFile(path) as z:
        return {i.filename: z.read(i) for i in z.infolist()}


def ctx_for(doc, tmp_path, settings, table, **kw):
    return build_context(doc, tmp_path / "work", settings, llm=keyword_llm(table), ocr=kw.get("ocr", FakeOcr()), faces=FakeFaces())


# ---- F1 (C1): text that lives only in hidden places is checked like any other text -----------

@pytest.fixture
def hidden_doc(tmp_path):
    return make_docx(tmp_path / "h.docx", ["一般內容，沒有個資"], images=[("image1.png", text_image(["x"], size=(300, 120)))],
                     image_alt="王小明的大頭照", image_name="陳美玲身分證.jpg", hyperlink=("聯絡我們", "mailto:secret@example.com"),
                     field_instr=' HYPERLINK "mailto:other@example.com" ', embedded_xlsx_strings=["林志豪", "A123456789"],
                     custom_xml_text="擁有者 張惠如", chart_labels=["李大同", "第一季"], bookmark="吳小華的備忘",
                     people=[("審閱者甲", "reviewer@example.com")])


def test_hidden_places_become_text_units(hidden_doc):
    inv = DocxAdapter().parse(hidden_doc)
    hidden = units(inv, "隱藏內容")
    assert "王小明的大頭照" in hidden and "陳美玲身分證.jpg" in hidden and "吳小華的備忘" in hidden
    assert "mailto:secret@example.com" in hidden
    assert any("other@example.com" in t for t in hidden)
    assert "擁有者 張惠如" in hidden and "reviewer@example.com" in hidden
    assert {"林志豪", "A123456789"} <= set(units(inv, "內嵌檔案"))
    assert "李大同" in units(inv, "圖表")
    assert "Picture 1" not in hidden  # default object names are not worth showing


def test_personal_data_found_only_in_hidden_places_is_masked_and_verified(hidden_doc, tmp_path, settings):
    names = {"王小明": "姓名", "陳美玲": "姓名", "secret@example.com": "電子郵件", "other@example.com": "電子郵件",
             "林志豪": "姓名", "張惠如": "姓名", "李大同": "姓名", "吳小華": "姓名", "reviewer@example.com": "電子郵件"}
    ctx = ctx_for(hidden_doc, tmp_path, settings, names)
    analyze(ctx, use_agent=False)
    found = {f.text for f in ctx.store.findings if f.kind == "text" and f.on}
    assert set(names) | {"A123456789"} <= found  # the id comes from the checksum safety net
    result = apply_and_verify(ctx)
    assert result.ok, (result.residue, result.problems)
    out = tmp_path / "work" / "output.docx"
    after = entries(out)
    blob = b"\n".join(v for k, v in after.items() if k.endswith((".xml", ".rels"))).decode("utf-8")
    inner = zipfile.ZipFile(io.BytesIO(after["word/embeddings/Workbook1.xlsx"])).read("xl/sharedStrings.xml").decode("utf-8")
    for s in list(names) + ["A123456789"]:
        assert s not in blob and s not in inner, s
    assert 'Target="about:blank"' in after["word/_rels/document.xml.rels"].decode("utf-8")
    assert "<c:v>第一季</c:v>" in after["word/charts/chart1.xml"].decode("utf-8")
    assert DocxAdapter().residue(out, list(names)) == []


def test_hidden_occurrence_can_be_left_alone(hidden_doc, tmp_path, settings):
    ctx = ctx_for(hidden_doc, tmp_path, settings, {"王小明": "姓名"})
    analyze(ctx, use_agent=False)
    f = next(f for f in ctx.store.findings if f.text == "王小明")
    ctx.store.set_on(f.id, False, occurrence=0)
    assert apply_and_verify(ctx).ok
    assert "王小明的大頭照" in entries(tmp_path / "work" / "output.docx")["word/document.xml"].decode("utf-8")


def test_pdf_link_addresses_are_units_and_masked_links_are_removed(tmp_path, settings):
    src = make_pdf(tmp_path / "l.pdf", [{"lines": ["聯絡我們"], "link": "mailto:secret@example.com"}])
    ad = PdfAdapter()
    inv = ad.parse(src)
    link = next(t for t in inv.texts if t.section == "連結位址")
    assert link.text == "mailto:secret@example.com"
    dst = tmp_path / "out.pdf"
    ad.apply(src, dst, MaskPlan(text={link.id: [(7, 25)]}), settings)
    with pymupdf.open(dst) as doc:
        assert doc[0].get_links() == []
    assert b"secret@example.com" not in dst.read_bytes()


# ---- F2 (C2): switching off an image match must not stop hidden copies being masked ---------

def test_switching_off_an_image_match_keeps_whole_document_replacement():
    inv = Inventory(kind="docx", texts=[TextUnit("t1", "承辦人 王小明", "內文")],
                    images=[ImageUnit("img1", "word/media/image1.png", 400, 200, True, areas=[(0, 0, 400, 200)],
                                      ocr=[ocr_line("登入者 王小朋")])])
    store = FindingStore(inv)
    f = store.add_text("王小明", "姓名", "model")
    fuzzy = next(i for i, o in enumerate(f.occurrences) if o.fuzzy)
    store.set_on(f.id, False, occurrence=fuzzy)
    plan = store.plan()
    assert plan.strings == ["王小明"] and "img1" not in plan.boxes


def test_partially_masked_string_is_reported_as_a_note_not_passed_in_silence(tmp_path, settings):
    doc = make_docx(tmp_path / "p.docx", ["承辦人 王小明", "王小明 再次出現"])
    with zipfile.ZipFile(doc, "a") as z:  # a copy somewhere the program does not edit
        z.writestr("customXml/itemProps1.xml", '<?xml version="1.0"?><ds:datastoreItem xmlns:ds="urn:x" ds:owner="王小明"/>')
    ctx = ctx_for(doc, tmp_path, settings, {"王小明": "姓名"})
    analyze(ctx, use_agent=False)
    f = next(f for f in ctx.store.findings if f.text == "王小明")
    ctx.store.set_on(f.id, False, occurrence=1)
    result = apply_and_verify(ctx)
    assert result.ok and result.residue == []
    assert any("王小明" in n and "customXml/itemProps1.xml" in n for n in result.notes)


# ---- F3 (I1): PDF form fields ---------------------------------------------------------------

def test_pdf_form_field_value_is_cleared(tmp_path, settings):
    src = make_pdf(tmp_path / "f.pdf", [{"lines": ["申請表"], "widget": ("applicant", "王小明")}])
    ad = PdfAdapter()
    inv = ad.parse(src)
    field = next(t for t in inv.texts if t.section == "表單欄位")
    assert field.text == "王小明"
    plan = MaskPlan(text={field.id: [(0, 3)]}, strings=["王小明"])
    dst = tmp_path / "out.pdf"
    ad.apply(src, dst, plan, settings)
    with pymupdf.open(dst) as doc:
        values = [w.field_value for w in doc[0].widgets()]
    assert not any("王小明" in (v or "") for v in values)
    assert ad.residue(dst, ["王小明"]) == [] and ad.verify(src, dst, plan, settings) == []


# ---- F4 (I2): a single-place add inside a text box also covers its compatibility copy -------

def test_single_place_in_a_text_box_also_masks_the_fallback_copy(tmp_path, settings):
    doc = make_docx(tmp_path / "t.docx", ["內文"], textbox="方塊裡的王小明")
    ad = DocxAdapter()
    inv = ad.parse(doc)
    visible = next(t for t in inv.texts if t.text == "方塊裡的王小明" and not t.mirror)
    store = FindingStore(inv)
    store.add_text("王小明", "姓名", "user", only=(visible.id, 4, 7))
    plan = store.plan()
    mirror = next(t for t in inv.texts if t.mirror)
    assert plan.text[mirror.id] == [(4, 7)]
    dst = tmp_path / "out.docx"
    ad.apply(doc, dst, plan, settings)
    assert "王小明" not in entries(dst)["word/document.xml"].decode("utf-8")


# ---- F5 (I3): residue looks everywhere ------------------------------------------------------

def test_residue_finds_a_name_left_in_any_attribute_but_ignores_layout_numbers(tmp_path):
    doc = make_docx(tmp_path / "a.docx", ["內文"], table=[["分機", "2301"]])
    data = entries(doc)
    xml = data["word/document.xml"].decode("utf-8").replace("<w:body>", '<w:body><w:p w:customNote="王小明經手"><w:r><w:t>x</w:t></w:r></w:p>', 1)
    with zipfile.ZipFile(doc, "w") as z:
        for name, blob in data.items():
            z.writestr(name, xml.encode("utf-8") if name == "word/document.xml" else blob)
    ad = DocxAdapter()
    assert any("word/document.xml" in r for r in ad.residue(doc, ["王小明"]))
    assert ad.residue(doc, ["9999"]) == []
    assert not any("w:w" in r for r in ad.residue(doc, ["2301"])) and len(ad.residue(doc, ["2301"])) == 1  # the cell text only


def test_residue_finds_a_name_inside_a_binary_embedded_object(tmp_path):
    doc = make_docx(tmp_path / "b.docx", ["內文"], ole_bin=b"\xd0\xcf\x11\xe0" + "所有人 王小明".encode("utf-16-le") + b"\x00" * 20)
    assert any("oleObject1.bin" in r for r in DocxAdapter().residue(doc, ["王小明"]))


# ---- F6 (I4): PDF XMP and cropped pages -----------------------------------------------------

XMP = ('<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
       'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description rdf:about="" '
       'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator><rdf:Seq><rdf:li>林志豪</rdf:li></rdf:Seq></dc:creator>'
       '</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>')


def test_pdf_output_never_carries_xmp(tmp_path, settings):
    src = make_pdf(tmp_path / "x.pdf", [{"lines": ["內容"]}], xmp=XMP)
    assert "林志豪".encode("utf-8") in src.read_bytes()
    dst = tmp_path / "out.pdf"
    PdfAdapter().apply(src, dst, MaskPlan(), settings)
    with pymupdf.open(dst) as doc:
        assert (doc.get_xml_metadata() or "") == ""
    assert "林志豪".encode("utf-8") not in dst.read_bytes()


def test_pdf_page_with_content_outside_the_crop_is_listed_as_unchecked(tmp_path):
    src = make_pdf(tmp_path / "c.pdf", [{"lines": ["可見內容"], "cropbox": (0, 0, 595, 300)}, {"lines": ["一般頁"]}])
    names = [u.name for u in PdfAdapter().parse(src).unchecked]
    assert any("第 1 頁" in n for n in names) and not any("第 2 頁" in n for n in names)


# ---- F7 (I5): the model client ignores proxy settings ----------------------------------------

def test_llm_client_does_not_route_document_text_through_environment_proxies(settings, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    from deid import llm
    assert llm._new_client().trust_env is False  # every client the module makes, shared or not
    assert LLMClient(settings, transport=httpx.MockTransport(lambda r: httpx.Response(200)))._own.trust_env is False


# ---- F8 (I6): the coverage record only counts what the model really received -----------------

def agent_ctx(tmp_path, settings, paragraphs, llm, images=()):
    doc = make_docx(tmp_path / "ag.docx", paragraphs, images=list(images))
    ctx = build_context(doc, tmp_path / "work", settings, llm=llm, ocr=FakeOcr([ocr_line("登入者")]),
                        faces=FakeFaces([(10.0, 10.0, 60.0, 60.0)]))
    ctx.load()
    ctx.write_previews()
    return ctx


def test_reading_many_short_units_never_marks_units_the_model_did_not_get(tmp_path, settings):
    ctx = agent_ctx(tmp_path, settings, [f"第{i}段" for i in range(401)], keyword_llm({}))
    ids = ctx.text_ids()
    res = tools.call(ctx, "read_text", {"unit_ids": ids})
    assert len(json.dumps(res, ensure_ascii=False)) <= 6000
    assert set(res["texts"]) | set(res["not_returned"]) == set(ids) and res["not_returned"]
    assert ctx.pending_reads == set(res["texts"]) and not (ctx.covered & set(ids))


def test_looking_at_an_image_does_not_replace_scanning_it(tmp_path, settings):
    script = [tool_call("scan_text", {}), tool_call("view_image", {"image_id": "img1"}), tool_call("finish", {"summary": "看過了"})]

    def handler(messages, tool_schemas):
        if tool_schemas is None:
            return reply([])
        return script.pop(0) if script else {"role": "assistant", "content": "…"}

    ctx = agent_ctx(tmp_path, settings, ["內文"], FakeLLM(handler), images=[("image1.png", text_image(["x"], size=(300, 200)))])
    run_agent(ctx)
    assert ctx.unchecked_units() == []
    assert [f.type for f in ctx.store.findings if f.source == "face"] == ["人臉"]  # face detection still ran


# ---- F9 (I7): request handlers do not block the event loop -----------------------------------

@pytest.fixture
def client(settings):
    settings.use_agent = False

    def factory(src, workdir, s, progress=None, restore=False):
        return build_context(src, workdir, s, llm=keyword_llm({"王小明": "姓名"}), progress=progress, restore=restore,
                             ocr=FakeOcr(), faces=FakeFaces())

    store = JobStore(settings, context_factory=factory)
    store.start()
    with TestClient(create_app(settings, store), raise_server_exceptions=False) as c:
        c.store = store
        yield c
    store.stop()


def ready(client, data, name="a.docx"):
    import time
    job_id = client.post("/api/jobs", files={"file": (name, io.BytesIO(data), "application/octet-stream")}).json()["id"]
    for _ in range(1000):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("review", "failed"):
            return job_id, job
        time.sleep(0.02)
    raise AssertionError("stuck")


def test_no_api_handler_is_a_coroutine(settings):
    app = create_app(settings, JobStore(settings))
    handlers = [r.endpoint for r in app.routes if getattr(r, "path", "").startswith("/api/")]
    assert handlers and not any(inspect.iscoroutinefunction(h) for h in handlers)


def test_malformed_request_bodies_are_client_errors(client, tmp_path):
    job_id, _ = ready(client, make_docx(tmp_path / "s.docx", ["承辦人 王小明"]).read_bytes())
    fid = client.get(f"/api/jobs/{job_id}/review").json()["findings"][0]["id"]
    r = client.patch(f"/api/jobs/{job_id}/findings/{fid}", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert client.patch(f"/api/jobs/{job_id}/findings/{fid}", json=["on"]).status_code == 422
    assert client.post(f"/api/jobs/{job_id}/findings", json="text").status_code == 422


# ---- F10 (I8): damaged input and output are named, not crashed on ----------------------------

def test_word_file_renamed_to_pdf_is_reported_as_not_a_pdf(tmp_path):
    src = make_docx(tmp_path / "real.docx", ["內文"])
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(src.read_bytes())
    with pytest.raises(BrokenFile) as e:
        PdfAdapter().parse(fake)
    assert "PDF" in str(e.value)


def test_docx_with_a_corrupt_entry_is_rejected_at_parse(tmp_path):
    src = make_docx(tmp_path / "c.docx", ["內文"], images=[("image1.png", text_image(["x"], size=(200, 100)))])
    raw = bytearray(src.read_bytes())
    with zipfile.ZipFile(src) as z:
        info = z.getinfo("word/media/image1.png")
    raw[info.header_offset + 30 + len(info.filename) + 40] ^= 0xFF  # flip a byte inside the stored image
    src.write_bytes(bytes(raw))
    with pytest.raises(BrokenFile) as e:
        DocxAdapter().parse(src)
    assert "損壞" in str(e.value)


def test_docx_with_truncated_xml_is_rejected_at_parse(tmp_path):
    src = make_docx(tmp_path / "t.docx", ["內文 王小明", "第二段"])
    data = entries(src)
    data["word/document.xml"] = data["word/document.xml"][:-60]
    with zipfile.ZipFile(src, "w") as z:
        for name, blob in data.items():
            z.writestr(name, blob)
    with pytest.raises(BrokenFile):
        DocxAdapter().parse(src)


def test_verify_reports_output_xml_that_is_not_wellformed(tmp_path, settings):
    src = make_docx(tmp_path / "v.docx", ["內文"])
    dst = tmp_path / "out.docx"
    data = entries(src)
    data["docProps/app.xml"] = data["docProps/app.xml"].replace(b"</Properties>", b"</Propertie>")
    with zipfile.ZipFile(dst, "w") as z:
        for name, blob in data.items():
            z.writestr(name, blob)
    problems = DocxAdapter().verify(src, dst, MaskPlan(), settings)
    assert any("docProps/app.xml" in p for p in problems)


def test_apply_failure_is_a_readable_error_not_a_bare_500(client, tmp_path, monkeypatch):
    job_id, _ = ready(client, make_docx(tmp_path / "s.docx", ["承辦人 王小明"]).read_bytes())

    def boom(_job_id):
        raise zipfile.BadZipFile("bad crc")

    monkeypatch.setattr(client.store, "apply", boom)
    r = client.post(f"/api/jobs/{job_id}/apply")
    assert r.status_code == 500 and "BadZipFile" in r.json()["error"]


# ---- F11 (I9): no false residue from short strings in raw PDF bytes --------------------------

def test_pdf_raw_byte_check_ignores_short_strings_but_still_catches_real_leftovers(tmp_path):
    src = make_pdf(tmp_path / "r.pdf", [{"lines": ["nothing here"]}])
    with open(src, "ab") as f:
        f.write(b"\n%KC " + "王小明".encode("utf-8") + b"\n")
    left = PdfAdapter().residue(src, ["KC", "王小明"])
    assert not any("KC" in r for r in left)
    assert any("檔案原始內容" in r and "王小明" in r for r in left)


# ---- F12: uploads are private to the service account -----------------------------------------

def test_job_files_are_not_readable_by_other_local_users(settings, tmp_path):
    store = JobStore(settings)
    job_id = store.create("a.docx", io.BytesIO(make_docx(tmp_path / "s.docx", ["內文"]).read_bytes()))
    for path in (settings.data_dir, store.root, store.root / job_id, store.root / job_id / "original.docx", store.root / job_id / "job.json"):
        assert path.stat().st_mode & 0o077 == 0, path


# ---- Short numbers (seen on a real file after the review fixes: extensions flagged as phones) ----

def test_numbers_and_latin_words_only_match_as_whole_tokens():
    from deid.findings import find_in_text
    assert find_in_text("分機 522，手機 0912-345-522，編號 15223", "522") == [(3, 6)]
    assert find_in_text("Irish 與 Iris 與 iris", "Iris") == [(8, 12)]
    assert find_in_text("王小明0912345678", "0912345678") == [(3, 13)]
    assert find_in_text("Tel0912345678。", "0912345678") == [(3, 13)]
    assert find_in_text("金額 3.522 與 522.", "522") == [(11, 14)]
    assert find_in_text("XA123456789 與 A123456789", "A123456789") == [(14, 24)]
    assert find_in_text("王小明與王小明", "王小明") == [(0, 3), (4, 7)]


def test_generic_replacement_and_residue_respect_token_boundaries():
    from deid.formats.xmltok import find_in_text_nodes, replace_in_text_nodes
    xml = "<t>分機 522，手機 0912-345-522</t>"
    assert replace_in_text_nodes(xml, ["522"], None, set(), "■", "*") == "<t>分機 ***，手機 0912-345-522</t>"
    assert find_in_text_nodes("<t>手機 0912-345-522</t>", ["522"], set()) == set()


def test_short_numbers_reported_by_the_model_are_suggestions_only(tmp_path, settings):
    doc = make_docx(tmp_path / "n.docx", ["承辦人 王小明 分機 522", "總機 2592-6998 轉 522"])
    ctx = ctx_for(doc, tmp_path, settings, {"王小明": "姓名", "522": "電話", "2592-6998": "電話"})
    analyze(ctx, use_agent=False)
    by = {f.text: f for f in ctx.store.findings if f.kind == "text"}
    assert by["王小明"].on and by["2592-6998"].on
    assert by["522"].on is False and "分機" in by["522"].reason and len(by["522"].occurrences) == 2


def test_detection_prompt_tells_the_model_not_to_list_extensions():
    from deid.detect import SYSTEM_PROMPT
    line = next(ln for ln in SYSTEM_PROMPT.splitlines() if ln.startswith("- 機關、公司"))
    assert "分機" in line
