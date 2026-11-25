"""Third tier of the review of 2026-11-23, the parts about the list and the code version: what the list hides
changes nothing; a match in a picture the reviewer switched off is the reviewer's choice, not a leftover; the
line of explanation at the top of the coded text is not restored. All names and numbers are made up."""
import random

from deid.codes import NOTICE, NOTICE_SAME, build
from deid.findings import FindingStore
from deid.model import ImageUnit, Inventory, TextUnit
from deid.restore import restore
from tests.fakes import ocr_line


def store_for(text: str, *adds) -> FindingStore:
    store = FindingStore(Inventory(kind="docx", texts=[TextUnit("t1", text, "內文")]))
    for string, type_, source in adds:
        store.add_text(string, type_, source)
    return store


def without(store: FindingStore, ids) -> FindingStore:
    other = FindingStore.from_json(store.inv, store.to_json())
    other.findings = [f for f in other.findings if f.id not in ids]
    return other


def body(coded) -> list[str]:
    return coded.text[len(NOTICE):].strip("\n").split("\n")


# ---- what the list hides changes nothing ---------------------------------------------------------------------

def test_two_strings_of_the_agent_that_fill_each_others_gap_are_not_both_hidden():
    store = store_for("王小明與陳美玲", ("王小明", "姓名", "model"), ("陳美玲", "姓名", "model"),
                      ("王小明與", "其他", "agent"), ("與陳美玲", "其他", "agent"))
    hidden = store.redundant()
    assert store.plan().text == {"t1": [(0, 7)]}  # 與 is masked: the agent asked for it
    assert without(store, hidden).plan().text == store.plan().text  # and an entry on the list says so
    assert len(hidden) == 1


def test_a_string_of_the_agent_inside_what_the_list_masks_is_still_hidden():
    store = store_for("信箱 wang.xm@example.com", ("wang.xm@example.com", "電子郵件", "rule"), ("wang.xm", "其他識別碼", "agent"))
    assert [store.get(i).text for i in store.redundant()] == ["wang.xm"]
    store = store_for("王小明與陳美玲", ("王小明與陳美玲", "其他", "user"), ("王小明與", "其他", "agent"), ("與陳美玲", "其他", "agent"))
    assert sorted(store.get(i).text for i in store.redundant()) == ["王小明與", "與陳美玲"]


def test_the_masked_ranges_are_the_same_with_and_without_what_the_list_hides():
    rng = random.Random(20261124)
    pieces = ["王小明", "陳美玲", "林志豪", "與", "的", "先生", "，", "電話", "0912345678", " "]
    for _ in range(300):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(3, 9)))
        store = FindingStore(Inventory(kind="docx", texts=[TextUnit("t1", text, "內文")]))
        for _ in range(rng.randint(1, 6)):
            start = rng.randrange(len(text))
            string = text[start:start + rng.randint(1, 6)].strip()
            if string:
                store.add_text(string, rng.choice(["姓名", "其他", "電話"]), rng.choice(["agent", "agent", "model", "user"]))
        hidden = store.redundant()
        assert without(store, hidden).plan().text == store.plan().text, (text, [(f.text, f.type, f.source) for f in store.findings])


def test_a_hidden_string_does_not_name_the_code():
    inv = Inventory(kind="docx", texts=[TextUnit("t1", "備註：王小明的病歷另附", "內文")])
    store = FindingStore(inv)
    store.add_text("王小明的病歷", "其他", "user")
    store.add_text("王小明", "姓名", "agent")
    hidden = store.redundant()
    assert [store.get(i).text for i in hidden] == ["王小明"]
    coded, bare = build(inv, store), build(inv, without(store, hidden))
    assert body(coded) == body(bare) == ["【內文】", "備註：[個資1]另附"]
    assert coded.table == bare.table == {"[個資1]": "王小明的病歷"}


# ---- a match in a picture that the reviewer switched off -----------------------------------------------------

def picture(*lines) -> ImageUnit:
    read = [ocr_line(text, y=10 + 40 * n) for n, text in enumerate(lines)]
    return ImageUnit("img1", "word/media/image1.png", 800, 400, True, areas=[(0, 0, 800, 400)], ocr=read)


def test_a_match_in_a_picture_the_reviewer_switched_off_is_a_choice_not_a_leftover():
    inv = Inventory(kind="docx", texts=[TextUnit("t1", "承辦人 王小明", "內文")], images=[picture("登入者：王小明")])
    store = FindingStore(inv)
    f = store.add_text("王小明", "姓名", "model")
    assert f is not None
    store.set_on(f.id, False, occurrence=next(i for i, o in enumerate(f.occurrences) if o.kind == "box"))
    coded = build(inv, store)
    assert body(coded) == ["【內文】", "承辦人 [姓名1]", "", "【圖片 1 的文字】", "登入者：王小明"]
    assert coded.residue == []
    assert coded.notes == ["代號版文字: 「王小明」"]  # shown to the reviewer as left readable


def test_a_string_still_readable_where_nothing_was_switched_off_is_a_leftover():
    inv = Inventory(kind="docx", texts=[TextUnit("t1", "承辦人 王小明", "內文")], images=[picture("登入者：王小明")])
    store = FindingStore(inv)
    store.add_text("王小明", "姓名", "model")
    inv.images[0].ocr.append(ocr_line("審核：王小明", y=90))  # read later: no box for it
    assert build(inv, store).residue == ["代號版文字: 「王小明」"]


def test_switching_off_in_one_picture_does_not_excuse_another_picture():
    second = ImageUnit("img2", "word/media/image2.png", 800, 400, True, areas=[(0, 0, 800, 400)], ocr=[ocr_line("審核：王小明")])
    inv = Inventory(kind="docx", texts=[TextUnit("t1", "承辦人 王小明", "內文")], images=[picture("登入者：王小明"), second])
    store = FindingStore(inv)
    f = store.add_text("王小明", "姓名", "model")
    assert f is not None
    first = next(i for i, o in enumerate(f.occurrences) if o.kind == "box" and o.unit == "img1")
    store.set_on(f.id, False, occurrence=first)
    coded = build(inv, store)
    assert coded.residue == [] and coded.notes == ["代號版文字: 「王小明」"]  # the second picture's match is coded
    second.ocr.append(ocr_line("核定：王小明", y=90))  # ... and a line of it nobody boxed is a leftover
    assert build(inv, store).residue == ["代號版文字: 「王小明」"]


# ---- the line of explanation at the top of the coded text ----------------------------------------------------

TABLE = {"[姓名1]": "王小明", "[姓名1b]": "小明"}


def test_the_explanation_at_the_top_of_the_coded_text_is_not_part_of_what_is_restored():
    done = restore(NOTICE + "\n\n【內文】\n承辦人 [姓名1]\n", TABLE)
    assert done.text == "【內文】\n承辦人 王小明\n"
    assert done.replaced == 1 and done.unknown == []  # [電話2] in the explanation is no unknown code
    assert done.unused == ["[姓名1b]"]


def test_the_second_line_of_explanation_goes_too():
    done = restore(NOTICE + "\n" + NOTICE_SAME + "\n\n承辦人 [姓名1]，又稱 [姓名1b]", TABLE)
    assert done.text == "承辦人 王小明，又稱 小明" and done.replaced == 2 and done.unknown == [] and done.unused == []


def test_the_explanation_is_recognised_when_a_model_rewrote_its_brackets_or_spaces():
    rewritten = NOTICE.replace("[姓名1]", "\\[姓名1\\]").replace("[電話2]", "【電話 2】").replace("下文中 ", "下文中")
    done = restore("好的，以下是整理後的內容：\n" + rewritten + "\n承辦人 [姓名1]", TABLE)
    assert done.text == "好的，以下是整理後的內容：\n承辦人 王小明" and done.unknown == []


def test_a_sentence_that_only_resembles_the_explanation_is_restored_like_any_other():
    done = restore("（說明：下文中 [姓名1] 是承辦人。）\n[電話2] 請保留", TABLE)
    assert done.text == "（說明：下文中 王小明 是承辦人。）\n[電話2] 請保留" and done.unknown == ["[電話2]"]


def test_a_coded_text_goes_back_to_the_document_text():
    inv = Inventory(kind="docx", texts=[TextUnit("t1", "承辦人 王小明，電話 0912-345-678", "內文")])
    store = FindingStore(inv)
    store.add_text("王小明", "姓名", "model")
    store.add_text("0912-345-678", "電話", "rule")
    coded = build(inv, store)
    done = restore(coded.text, coded.table)
    assert done.text == "【內文】\n承辦人 王小明，電話 0912-345-678\n" and done.unknown == [] and done.unused == []
