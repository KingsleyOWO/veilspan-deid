import json

import pytest

from deid.formats.base import BrokenFile, adapter_for
from deid.imaging import is_black
from deid.llm import LLMError
from deid.pipeline import analyze, apply_and_verify, build_context
from tests.builders import make_docx, text_image
from tests.fakes import FakeFaces, FakeLLM, FakeOcr, keyword_llm, ocr_line, reply

NAMES = {"王小明": "姓名", "0912-345-678": "電話"}


@pytest.fixture
def doc(tmp_path):
    return make_docx(tmp_path / "a.docx", ["承辦人 王小明 負責本案", "一般內容"], header="頁首 王小明",
                     images=[("image1.png", text_image(["登入者"], size=(800, 200)))], creator="林志豪", company="範例公司",
                     extra_media=[("image2.emf", b"\x01\x00\x00\x00junk")])


def context(doc, tmp_path, settings, llm=None, **kw):
    kw.setdefault("ocr", FakeOcr([ocr_line("登入者：王小明"), ocr_line("電話0912-345-678", y=60)]))
    kw.setdefault("faces", FakeFaces([(600.0, 20.0, 700.0, 120.0)]))
    return build_context(doc, tmp_path / "work", settings, llm=llm or keyword_llm(NAMES), **kw)


def by_text(ctx):
    return {f.text: f for f in ctx.store.findings if f.kind == "text"}


def test_standard_flow_covers_every_unit_and_finds_text_image_and_metadata_items(doc, tmp_path, settings):
    ctx = context(doc, tmp_path, settings)
    analyze(ctx, use_agent=False)
    assert ctx.unchecked_units() == []
    found = by_text(ctx)
    name = found["王小明"]
    assert {o.kind for o in name.occurrences} == {"text", "box"} and name.source == "model" and name.on
    assert [o.kind for o in found["0912-345-678"].occurrences] == ["box"]
    metas = {f.reason: f.on for f in ctx.store.findings if f.kind == "meta"}
    assert metas == {"作者": True, "公司": False}
    faces = [f for f in ctx.store.findings if f.type == "人臉"]
    assert len(faces) == 1 and faces[0].source == "face" and faces[0].on is True


def test_results_are_saved_and_previews_written(doc, tmp_path, settings):
    ctx = context(doc, tmp_path, settings)
    analyze(ctx, use_agent=False)
    work = tmp_path / "work"
    assert json.loads((work / "findings.json").read_text(encoding="utf-8"))["findings"]
    inv = json.loads((work / "inventory.json").read_text(encoding="utf-8"))
    assert inv["images"][0]["ocr"][0]["text"] == "登入者：王小明"
    assert (work / "assets" / "img1.png").is_file()
    restored = build_context(doc, work, settings, llm=keyword_llm({}), restore=True)
    assert restored.store.plan() == ctx.store.plan() and restored.warnings == ctx.warnings


def test_unsupported_image_is_reported_as_a_warning(doc, tmp_path, settings):
    ctx = context(doc, tmp_path, settings)
    analyze(ctx, use_agent=False)
    assert any("image2.emf" in w for w in ctx.warnings)


def test_apply_masks_text_image_and_metadata_and_verifies_clean(doc, tmp_path, settings):
    ctx = context(doc, tmp_path, settings)
    analyze(ctx, use_agent=False)
    result = apply_and_verify(ctx)
    assert result.ok and result.residue == [] and result.problems == []
    out = tmp_path / "work" / "output.docx"
    ad = adapter_for(out)
    inv = ad.parse(out)
    assert not any("王小明" in t.text for t in inv.texts) and any("承辦人 ■■■ 負責本案" == t.text for t in inv.texts)
    assert not any(m.label == "作者" for m in inv.metas)
    box = next(o.box for o in by_text(ctx)["王小明"].occurrences if o.kind == "box")
    assert is_black(ad.image_bytes(out, "img1"), box)
    assert result.stats["boxes"] >= 3 and result.stats["text_spans"] == 2


def test_switched_off_items_are_left_alone(doc, tmp_path, settings):
    ctx = context(doc, tmp_path, settings)
    analyze(ctx, use_agent=False)
    ctx.store.set_on(by_text(ctx)["王小明"].id, False)
    assert apply_and_verify(ctx).ok
    inv = adapter_for(tmp_path / "work" / "output.docx").parse(tmp_path / "work" / "output.docx")
    assert any("承辦人 王小明 負責本案" == t.text for t in inv.texts)


def test_checksum_valid_id_is_masked_even_when_the_model_misses_it(tmp_path, settings):
    doc = make_docx(tmp_path / "b.docx", ["報名資料 A123456789 已收到", "型號 A123-4567"])
    ctx = build_context(doc, tmp_path / "work", settings, llm=keyword_llm({}), ocr=FakeOcr(), faces=FakeFaces())
    analyze(ctx, use_agent=False)
    [f] = [f for f in ctx.store.findings if f.kind == "text"]
    assert (f.text, f.source, f.on, f.type) == ("A123456789", "rule", True, "身分證字號")


def test_id_number_inside_an_image_is_caught_by_the_safety_net(tmp_path, settings):
    doc = make_docx(tmp_path / "c.docx", ["內文"], images=[("image1.png", text_image(["x"], size=(400, 200)))])
    ctx = build_context(doc, tmp_path / "work", settings, llm=keyword_llm({}),
                        ocr=FakeOcr([ocr_line("身分證A123456789")]), faces=FakeFaces())
    analyze(ctx, use_agent=False)
    [f] = [f for f in ctx.store.findings if f.kind == "text"]
    assert f.source == "rule" and [o.kind for o in f.occurrences] == ["box"]


def test_vision_flag_becomes_a_whole_image_suggestion_that_is_off(tmp_path, settings):
    def handler(messages, tools):
        if isinstance(messages[-1]["content"], list):
            return {"role": "assistant", "content": '{"id_document": true, "signature_or_seal": false, "handwriting": false, "medical_record": false}'}
        return reply([])

    doc = make_docx(tmp_path / "d.docx", ["內文"], images=[("image1.png", text_image(["x"], size=(400, 200))),
                                                         ("image2.png", text_image(["x"], size=(40, 40)))])
    llm = FakeLLM(handler)
    ctx = build_context(doc, tmp_path / "work", settings, llm=llm, ocr=FakeOcr(), faces=FakeFaces())
    analyze(ctx, use_agent=False)
    flags = [f for f in ctx.store.findings if f.source == "vision"]
    assert [(f.type, f.on, f.occurrences[0].unit, f.occurrences[0].box) for f in flags] == [("證件", False, "img1", (0.0, 0.0, 400.0, 200.0))]
    vision_calls = [c for c in llm.calls if isinstance(c["messages"][-1]["content"], list)]
    assert len(vision_calls) == 1  # the 40px icon is below the size threshold


def test_failed_chunk_is_surfaced_as_a_warning(tmp_path, settings):
    doc = make_docx(tmp_path / "e.docx", ["只有一行"])
    ctx = build_context(doc, tmp_path / "work", settings, llm=FakeLLM(lambda m, t: {"role": "assistant", "content": "??"}),
                        ocr=FakeOcr(), faces=FakeFaces())
    analyze(ctx, use_agent=False)
    assert any("無法解析" in w for w in ctx.warnings)


def test_endpoint_failure_aborts_without_saving_partial_results(doc, tmp_path, settings):
    def boom(messages, tools):
        raise LLMError("down")

    ctx = context(doc, tmp_path, settings, llm=FakeLLM(boom))
    with pytest.raises(LLMError):
        analyze(ctx, use_agent=False)
    assert not (tmp_path / "work" / "findings.json").exists()


def test_broken_file_raises_before_anything_else(tmp_path, settings):
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"nope")
    ctx = build_context(bad, tmp_path / "work", settings, llm=keyword_llm({}), ocr=FakeOcr(), faces=FakeFaces())
    with pytest.raises(BrokenFile):
        analyze(ctx, use_agent=False)


def test_progress_is_reported_and_ends_at_one(doc, tmp_path, settings):
    seen = []
    ctx = context(doc, tmp_path, settings, progress=lambda stage, frac: seen.append((stage, frac)))
    analyze(ctx, use_agent=False)
    assert seen[-1][1] == 1.0 and all(0 <= f <= 1 for _, f in seen) and len({s for s, _ in seen}) >= 3


def test_document_without_any_findings_still_produces_a_valid_output(tmp_path, settings):
    doc = make_docx(tmp_path / "f.docx", ["沒有個資的內容"])
    ctx = build_context(doc, tmp_path / "work", settings, llm=keyword_llm({}), ocr=FakeOcr(), faces=FakeFaces())
    analyze(ctx, use_agent=False)
    result = apply_and_verify(ctx)
    assert result.ok and adapter_for(tmp_path / "work" / "output.docx").parse(tmp_path / "work" / "output.docx").texts[0].text == "沒有個資的內容"


def test_image_text_is_sent_to_the_model_one_image_at_a_time(tmp_path, settings):
    """Mixed into one request, names drown in interface text from other screenshots (seen on real files)."""
    doc = make_docx(tmp_path / "g.docx", ["內文"], images=[("image1.png", text_image(["x"], size=(400, 200))),
                                                         ("image2.png", text_image(["x"], size=(500, 200)))])
    ocr = FakeOcr(by_size={(400, 200): [ocr_line("第一張 王小明")], (500, 200): [ocr_line("第二張 陳美玲")]})
    llm = keyword_llm({"王小明": "姓名", "陳美玲": "姓名"})
    ctx = build_context(doc, tmp_path / "work", settings, llm=llm, ocr=ocr, faces=FakeFaces())
    analyze(ctx, use_agent=False)
    texts = [c["messages"][-1]["content"] for c in llm.calls if isinstance(c["messages"][-1]["content"], str)]
    assert "第一張 王小明" in texts and "第二張 陳美玲" in texts
    assert not any("第一張" in t and "第二張" in t for t in texts)
    assert {f.text for f in ctx.store.findings if f.kind == "text"} == {"王小明", "陳美玲"}


def test_pdf_goes_through_the_whole_flow(tmp_path, settings):
    from tests.builders import make_pdf
    src = make_pdf(tmp_path / "a.pdf", [{"lines": ["聯絡人 王小明 先生", "一般內容"]},
                                        {"image": text_image(["x"], size=(1190, 1684))}], author="林志豪")
    ocr = FakeOcr([ocr_line("掃描頁上的王小明", x=200, y=300, w=40, h=48)])
    ctx = build_context(src, tmp_path / "work", settings, llm=keyword_llm({"王小明": "姓名"}), ocr=ocr, faces=FakeFaces())
    analyze(ctx, use_agent=False)
    assert ocr.calls == 1  # only the page that has an image is read by OCR
    name = next(f for f in ctx.store.findings if f.text == "王小明")
    kinds = sorted((o.kind, o.unit) for o in name.occurrences)
    assert kinds == [("box", "pg2"), ("text", next(t.id for t in ctx.inv.texts if "王小明" in t.text))]
    text_occ = next(o for o in name.occurrences if o.kind == "text")
    assert text_occ.page_boxes and text_occ.page_boxes[0][0] == "pg1"
    assert (tmp_path / "work" / "assets" / "pg1.png").is_file() and (tmp_path / "work" / "assets" / "pg2.png").is_file()
    result = apply_and_verify(ctx)
    assert result.ok, (result.residue, result.problems)
    import pymupdf
    with pymupdf.open(tmp_path / "work" / "output.pdf") as doc:
        assert "王小明" not in doc[0].get_text() and "聯絡人" in doc[0].get_text() and doc.metadata["author"] == ""
        box = next(o.box for o in name.occurrences if o.kind == "box")
        assert is_black(doc[1].get_pixmap(dpi=200).tobytes("png"), box)


def test_a_phone_a_mailbox_and_a_customer_number_in_a_link_are_masked_even_when_the_model_lists_none(tmp_path, settings):
    doc = make_docx(tmp_path / "m.docx", ["請來信 it-service@hospital.example.org.tw，或電洽總機 (02)2592-6998 轉 2301", "內文"],
                    images=[("image1.png", text_image(["x"], size=(400, 200)))])
    link = "預約連結：https://www.clinic.example.com.tw/order/?CustomerNo=90412"
    ctx = build_context(doc, tmp_path / "work", settings, llm=keyword_llm({}), ocr=FakeOcr([ocr_line(link)]), faces=FakeFaces())
    analyze(ctx, use_agent=False)
    found = {f.text: f for f in ctx.store.findings if f.kind == "text"}
    assert set(found) == {"it-service@hospital.example.org.tw", "(02)2592-6998", "90412"}
    assert {(f.type, f.source, f.on) for f in found.values()} == {("電子郵件", "rule", True), ("電話", "rule", True), ("其他識別碼", "rule", True)}
    assert "檢查碼" not in found["90412"].reason + found["it-service@hospital.example.org.tw"].reason


# ---- which names are one person: only asked for the code function -------------------------

def same_person_model(names):
    from tests.fakes import routed, tool_call
    script = [tool_call("link", {"texts": ["王小明", "小明"], "reason": "同一段"}), tool_call("finish", {})]
    return routed({"指的是同一個人": lambda messages, tools: script.pop(0)}, table=names)


def test_only_a_code_job_asks_who_is_the_same_person(tmp_path, settings):
    from deid.agent import samename
    settings.link_names = True
    names = {"王小明": "姓名", "小明": "姓名"}
    doc = make_docx(tmp_path / "n.docx", ["王小明到場。", "小明另提供資料。"])

    def run(mode, work):
        llm = same_person_model(names)
        ctx = build_context(doc, tmp_path / work, settings, llm=llm, ocr=FakeOcr(), faces=FakeFaces())
        analyze(ctx, use_agent=False, mode=mode)
        return ctx, [c for c in llm.calls if c["messages"][0]["content"] == samename.SYSTEM]

    ctx, asked = run("mask", "w1")
    assert asked == [] and "names" not in ctx.outcome and ctx.store.groups() == {}
    ctx, asked = run("code", "w2")
    assert len(asked) == 2 and ctx.outcome["names"] == {"candidates": 2, "groups": 1, "finished": True, "reason": None}
    again = build_context(doc, tmp_path / "w2", settings, llm=keyword_llm(names), ocr=FakeOcr(), faces=FakeFaces(), restore=True)
    assert [f.text for f in again.store.groups()["g1"]] == ["王小明", "小明"] and again.outcome["names"]["groups"] == 1


def test_the_same_person_step_can_be_switched_off(tmp_path, settings):
    settings.link_names = False
    doc = make_docx(tmp_path / "n.docx", ["王小明到場。", "小明另提供資料。"])
    llm = same_person_model({"王小明": "姓名", "小明": "姓名"})
    ctx = build_context(doc, tmp_path / "w", settings, llm=llm, ocr=FakeOcr(), faces=FakeFaces())
    analyze(ctx, use_agent=False, mode="code")
    assert ctx.store.groups() == {} and all(c["tools"] is None for c in llm.calls)


def test_a_mask_job_sends_the_model_exactly_what_it_sent_before(doc, tmp_path, settings):
    def requests(work, on):
        settings.link_names = on
        llm = keyword_llm(NAMES)
        analyze(context(doc, tmp_path / work, settings, llm=llm), use_agent=False, mode="mask")
        return sorted(json.dumps(c, ensure_ascii=False, sort_keys=True) for c in llm.calls)

    assert requests("off", False) == requests("on", True)
