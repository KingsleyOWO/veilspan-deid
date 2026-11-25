"""A cell computed from a masked cell can give the masked value back (salary x 12). Such cells are listed as
suggestions that are off: the owner chose to be shown them rather than have them masked automatically."""
from deid.findings import FindingStore
from deid.formats.xlsx import XlsxAdapter
from deid.pipeline import analyze, apply_and_verify, build_context
from tests.fakes import FakeFaces, FakeOcr, keyword_llm
from tests.test_polish import api, upload, wait_for  # noqa: F401  (api is a fixture)
from tests.test_xlsx_review import HEAD, MAIN, out, pack, rels, s, sheet, sst


def n(ref, value):
    return f'<c r="{ref}"><v>{value}</v></c>'


def f(ref, formula, value, attrs=""):
    return f'<c r="{ref}"><f{attrs}>{formula}</f><v>{value}</v></c>'


def derived(src, words):
    """The texts of the cells suggested after masking `words`, with the cell each one was traced back to."""
    ad = XlsxAdapter()
    inv = ad.parse(src)
    store = FindingStore(inv)
    for w in words:
        store.add_text(w, "財務情況", "user")
    return sorted((inv.text(uid).text[a:b], origin) for uid, a, b, origin in ad.derived(src, store.plan().text))


HEADER = f'<row r="1">{s("A1", 0)}{s("B1", 1)}{s("C1", 2)}</row>'
NAMES = sst("姓名", "薪資", "年薪", "王小明", "陳美玲")


def test_cells_computed_from_a_masked_cell_are_found_directly_through_another_formula_and_through_a_range(tmp_path):
    rows = (HEADER + f'<row r="2">{s("A2", 3)}{n("B2", 68000)}{f("C2", "B2*12", 816000)}{f("D2", "C2+1", 816001)}'
            f'{f("E2", "SUM(B2:B3)", 140000)}{f("F2", "LEN(A1)+LOG10(100)", 4)}{f("G2", "LEN(&quot;B2&quot;)", 2)}</row>'
            f'<row r="3">{s("A3", 4)}{n("B3", 72000)}{f("C3", "B3*12", 864000)}</row>')
    src = pack(tmp_path / "a.xlsx", {"名單": sheet(rows)}, NAMES)
    assert derived(src, ["68000"]) == [("140000", "B2"), ("816000", "B2"), ("816001", "B2")]
    assert derived(src, []) == []


MASTER = ' t="shared" ref="C2:C3" si="0"'


def test_a_filled_down_formula_is_followed_cell_by_cell(tmp_path):
    rows = (HEADER + f'<row r="2">{s("A2", 3)}{n("B2", 68000)}{f("C2", "B2*12", 816000, MASTER)}</row>'
            f'<row r="3">{s("A3", 4)}{n("B3", 72000)}<c r="C3"><f t="shared" si="0"/><v>864000</v></c>'
            f'{f("D3", "$B$2+B$2+$B2", 1)}</row>')
    src = pack(tmp_path / "b.xlsx", {"名單": sheet(rows)}, NAMES)
    assert derived(src, ["72000"]) == [("864000", "B3")]


def test_other_sheets_names_whole_columns_and_tables_are_followed(tmp_path):
    rows = (HEADER + f'<row r="2">{s("A2", 3)}{n("B2", 68000)}{f("C2", "[@薪資]*12", 816000)}</row>'
            f'<row r="3">{s("A3", 4)}{n("B3", 72000)}{f("C3", "[@薪資]*12", 864000)}{f("E3", "SUM(表格甲[薪資])", 140000)}</row>')
    other = (f'<row r="1">{f("A1", "名單!$B$2*2", 136000)}{f("B1", "SUM(薪資範圍)", 140001)}{f("C1", "SUM(名單!B:B)", 140002)}'
             f"{f('D1', chr(39) + '名單' + chr(39) + '!B3+1', 72001)}{f('E1', 'A1/2', 68001)}</row>")
    table = (HEAD + f'<table xmlns="{MAIN}" id="1" name="表格甲" displayName="表格甲" ref="A1:C3"><tableColumns count="3">'
             '<tableColumn id="1" name="姓名"/><tableColumn id="2" name="薪資"/><tableColumn id="3" name="年薪"/></tableColumns></table>')
    src = pack(tmp_path / "c.xlsx", {"名單": sheet(rows, '<tableParts count="1"><tablePart r:id="rIdT"/></tableParts>'), "統計": sheet(other)},
               NAMES, {"xl/tables/table1.xml": table, "xl/worksheets/_rels/sheet1.xml.rels": rels(("rIdT", "table", "../tables/table1.xml"))},
               wb_extra='<definedNames><definedName name="薪資範圍">名單!$B$2:$B$3</definedName></definedNames>')
    assert derived(src, ["68000"]) == [("136000", "B2"), ("140000", "B2"), ("140001", "B2"), ("140002", "B2"), ("68001", "B2"), ("816000", "B2")]


def context(src, tmp_path, settings, table):
    return build_context(src, tmp_path / "work", settings, llm=keyword_llm(table), ocr=FakeOcr(), faces=FakeFaces())


def test_the_review_lists_them_switched_off_and_masking_one_turns_its_formula_into_a_value(tmp_path, settings):
    rows = (HEADER + f'<row r="2">{s("A2", 3)}{n("B2", 68000)}{f("C2", "B2*12", 816000)}</row>'
            f'<row r="3">{s("A3", 4)}{n("B3", 72000)}{f("C3", "B3*12", 864000)}</row>')
    src = pack(tmp_path / "d.xlsx", {"名單": sheet(rows)}, NAMES)
    ctx = context(src, tmp_path, settings, {"68000": "財務情況"})
    analyze(ctx, use_agent=False)
    by_text = {x.text: x for x in ctx.store.findings if x.kind == "text"}
    assert set(by_text) == {"68000", "816000"}
    suggestion = by_text["816000"]
    assert suggestion.on is False and suggestion.source == "rule" and "B2" in suggestion.reason and len(suggestion.occurrences) == 1
    assert apply_and_verify(ctx).ok and "<v>816000</v>" in out(ctx.workdir / "output.xlsx")["xl/worksheets/sheet1.xml"]
    ctx.store.set_on(suggestion.id, True)
    assert apply_and_verify(ctx).ok
    after = out(ctx.workdir / "output.xlsx")["xl/worksheets/sheet1.xml"]
    assert "816000" not in after and "B2*12" not in after and "<f>B3*12</f>" in after


def test_a_cell_that_is_itself_masked_is_not_suggested_again(tmp_path, settings):
    rows = HEADER + f'<row r="2">{s("A2", 3)}{n("B2", 68000)}{f("C2", "B2*12", 816000)}</row>'
    ctx = context(pack(tmp_path / "e.xlsx", {"名單": sheet(rows)}, NAMES), tmp_path, settings, {"68000": "財務情況", "816000": "財務情況"})
    analyze(ctx, use_agent=False)
    assert sorted((x.text, x.on, x.source) for x in ctx.store.findings if x.kind == "text") == [("68000", True, "model"), ("816000", True, "model")]


def test_masking_a_cell_by_hand_in_the_review_brings_up_the_cells_computed_from_it(tmp_path, api):
    rows = HEADER + f'<row r="2">{s("A2", 3)}{n("B2", 68000)}{f("C2", "B2*12", 816000)}</row>'
    src = pack(tmp_path / "f.xlsx", {"名單": sheet(rows)}, NAMES)
    client = api(keyword_llm({}))
    job = upload(client, src.read_bytes(), name="名單.xlsx")
    wait_for(client, job, statuses=("review",))
    assert [x for x in client.get(f"/api/jobs/{job}/review").json()["findings"] if x.get("text") == "816000"] == []
    added = client.post(f"/api/jobs/{job}/findings", json={"kind": "text", "text": "68000", "type": "財務情況", "all": True}).json()
    assert added["derived"] == 1  # tells the page to fetch the list again
    [suggested] = [x for x in client.get(f"/api/jobs/{job}/review").json()["findings"] if x.get("text") == "816000"]
    assert suggested["on"] is False and "B2" in suggested["reason"]


def test_a_computed_value_equal_to_something_already_masked_elsewhere_is_still_only_suggested(tmp_path, settings):
    rows = (HEADER + f'<row r="2">{s("A2", 3)}{n("B2", 68000)}{f("C2", "B2*1", 68000)}</row>'
            f'<row r="3">{s("A3", 4)}{n("B3", 5)}</row>')
    src = pack(tmp_path / "g.xlsx", {"名單": sheet(rows)}, NAMES)
    ctx = context(src, tmp_path, settings, {})
    analyze(ctx, use_agent=False)
    b2 = next(t for t in ctx.inv.texts if "68000" in t.text)
    start = b2.text.index("68000")
    ctx.store.add_text("68000", "財務情況", "user", only=(b2.id, start, start + 5))  # only the salary cell
    ctx.add_derived()
    assert apply_and_verify(ctx).ok
    after = out(ctx.workdir / "output.xlsx")["xl/worksheets/sheet1.xml"]
    assert "<f>B2*1</f><v>68000</v>" in after  # the computed cell is listed, not masked
    [finding] = [x for x in ctx.store.findings if x.text == "68000"]
    assert [o.on for o in finding.occurrences] == [True, False]
