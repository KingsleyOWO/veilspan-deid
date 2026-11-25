"""A string a PDF page breaks over two lines (a mailbox in a narrow table cell) is the same string (review of
2026-11-23). The reader puts a space where a line ends, because between two words there is one; whether there
is one inside "exam|ple.com" cannot be known, so matching takes both readings. All names and numbers are made up."""
import pymupdf

from deid import codes
from deid.detect import detect_lines
from deid.findings import FindingStore
from deid.formats.base import adapter_for
from deid.pipeline import build_context
from tests.fakes import FakeFaces, FakeLLM, FakeOcr, keyword_llm, reply

MAIL, PHONE, ID = "flora.wang@example.com", "0912-345-678", "A123456789"
BROKEN = ("聯絡：flora.wang@exam", "ple.com", "電話：0912-345-", "678", "身分證：A12345", "6789")


def wrapped_pdf(tmp_path, first_line=f"聯絡信箱 {MAIL} 電話 {PHONE} 身分證 {ID}"):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    if first_line:
        page.insert_text((72, 80), first_line, fontsize=11, fontname="china-t")
    for i, part in enumerate(BROKEN):  # a narrow cell further down: the same data, wrapped by the layout
        page.insert_text((72, 200 + 14 * i), part, fontsize=11, fontname="china-t")
    path = tmp_path / "wrapped.pdf"
    doc.save(path)
    doc.close()
    return path


def store_for(src, settings):
    ad = adapter_for(src, settings)
    inv = ad.parse(src)
    return ad, inv, FindingStore(inv, lambda u, s, e: ad.locate(src, u, s, e))


def page_text(path) -> str:
    with pymupdf.open(path) as doc:
        return doc[0].get_text()


def test_the_page_text_still_reads_with_a_space_where_a_line_ends(tmp_path, settings):
    _ad, inv, _store = store_for(wrapped_pdf(tmp_path), settings)
    cell = next(t for t in inv.texts if "exam" in t.text and MAIL not in t.text)
    assert cell.text == "聯絡：flora.wang@exam ple.com 電話：0912-345- 678 身分證：A12345 6789"
    assert [cell.text[i] for i in cell.breaks] == [" "] * 5


def test_a_string_is_found_where_the_page_breaks_it_over_two_lines(tmp_path, settings):
    _ad, inv, store = store_for(wrapped_pdf(tmp_path), settings)
    cell = next(t for t in inv.texts if "exam" in t.text and MAIL not in t.text)
    for string, broken in ((MAIL, "flora.wang@exam ple.com"), (PHONE, "0912-345- 678"), (ID, "A12345 6789")):
        places = [(o.unit, cell.text[o.start:o.end]) for o in store.add_text(string, "其他", "user").occurrences if o.unit == cell.id]
        assert places == [(cell.id, broken)]
    assert len(store.add_text("聯絡", "其他", "user").occurrences) == 2  # an ordinary string is not found twice over


def test_the_rules_find_what_is_broken_over_two_lines(tmp_path, settings):
    src = wrapped_pdf(tmp_path, first_line="")
    ctx = build_context(src, tmp_path / "work", settings, llm=keyword_llm({}), ocr=FakeOcr(), faces=FakeFaces())
    ctx.load()
    ctx.add_safety_net()
    assert {MAIL, PHONE, ID} <= {f.text for f in ctx.store.findings if f.on}


def test_masking_takes_the_broken_copy_along_and_the_check_passes(tmp_path, settings):
    src = wrapped_pdf(tmp_path)
    ad, _inv, store = store_for(src, settings)
    for string in (MAIL, PHONE, ID):
        assert len(store.add_text(string, "其他", "user").occurrences) == 2
    plan, dst = store.plan(), tmp_path / "out.pdf"
    ad.apply(src, dst, plan, settings)
    left = page_text(dst)
    assert not any(piece in left for piece in ("exam", "ple.com", "0912", "678", "A12345", "6789"))
    assert "聯絡" in left and "電話" in left and "身分證" in left
    assert ad.residue(dst, plan.strings) == [] and ad.verify(src, dst, plan, settings) == []


def test_residue_sees_a_string_broken_over_two_lines(tmp_path, settings):
    src = wrapped_pdf(tmp_path, first_line="")
    assert adapter_for(src, settings).residue(src, [MAIL])


def test_a_broken_copy_is_masked_whole_under_a_style_that_keeps_characters(tmp_path, settings):
    src = wrapped_pdf(tmp_path)
    _ad, inv, store = store_for(src, settings)
    cell = next(t for t in inv.texts if "exam" in t.text and MAIL not in t.text)
    found = store.add_text(MAIL, "電子郵件", "user")
    store.set_style(found.id, "partial")
    at = cell.text.index("flora")
    assert store.plan().text[cell.id] == [(at, at + len("flora.wang@exam ple.com"))]


def test_the_model_may_name_a_broken_string_whole():
    line = "聯絡：flora.wang@exam ple.com"
    llm = FakeLLM(lambda messages, tools: reply([{"text": MAIL, "type": "電子郵件"}]))
    assert detect_lines(llm, [line]).items == []  # not in the text as it is sent
    assert detect_lines(llm, [line], also=["聯絡：flora.wang@example.com"]).items == [(MAIL, "電子郵件")]


def test_what_the_model_names_whole_is_masked_where_it_is_broken(tmp_path, settings):
    src = wrapped_pdf(tmp_path, first_line="")
    llm = FakeLLM(lambda messages, tools: reply([{"text": "flora.wang", "type": "姓名"}]))
    settings.use_agent = False
    ctx = build_context(src, tmp_path / "work", settings, llm=llm, ocr=FakeOcr(), faces=FakeFaces())
    ctx.load()
    ctx.scan_text()
    assert "flora.wang" in {f.text for f in ctx.store.findings}
    llm.handler = lambda messages, tools: reply([{"text": "A123456789", "type": "身分證字號"}])
    ctx.scan_text()
    found = next(f for f in ctx.store.findings if f.text == "A123456789")
    assert [ctx.inv.text(o.unit).text[o.start:o.end] for o in found.occurrences] == ["A12345 6789"]


def test_the_coded_text_gives_both_copies_one_code_that_restores_to_the_string(tmp_path, settings):
    src = wrapped_pdf(tmp_path)
    _ad, inv, store = store_for(src, settings)
    store.add_text(MAIL, "電子郵件", "user")
    coded = codes.build(inv, store)
    assert coded.text.count("[電子郵件1]") == 2 and coded.table == {"[電子郵件1]": MAIL} and coded.residue == []


def test_a_string_reported_with_the_line_end_space_joins_the_one_found_without_it(tmp_path, settings):
    # The rules name the mailbox whole; the model, reading the page text, names it with the space the reader put in.
    # Both are the same place: one entry on the list, not two.
    _ad, inv, store = store_for(wrapped_pdf(tmp_path, first_line=""), settings)
    whole = store.add_text(MAIL, "電子郵件", "rule")
    spaced = store.add_text("flora.wang@exam ple.com", "電子郵件", "model")
    assert spaced is whole and len(store.findings) == 1 and len(whole.occurrences) == 1
    assert store.add_text("A12345 6789", "身分證字號", "model") is not None and len(store.findings) == 2  # nothing to join
