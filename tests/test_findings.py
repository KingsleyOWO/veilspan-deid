import json
import pytest

from deid.findings import FindingStore, find_in_ocr, find_in_text
from deid.model import ImageUnit, Inventory, MetaUnit, TextUnit
from tests.fakes import ocr_line


def inventory():
    return Inventory(
        kind="docx",
        texts=[TextUnit("t1", "承辦人 | 王小明 | 分機 2301", "內文"), TextUnit("t2", "王小明表示小明會處理，王小明同意", "內文"),
               TextUnit("t3", "方塊 王小明", "內文", mirror=True)],
        images=[ImageUnit("img1", "word/media/image1.png", 800, 200, True, areas=[(0, 0, 800, 200)],
                          ocr=[ocr_line("登入者：王小明"), ocr_line("使用者 王小朋 已登出", y=60), ocr_line("小朋友", y=110)]),
                ImageUnit("img2", "word/media/image2.png", 100, 100, True, areas=[(0, 0, 100, 100)])],
        metas=[MetaUnit("m1", "作者", "王小明", True), MetaUnit("m2", "標題", "王小明的報告", False)],
    )


def test_find_in_text_is_left_to_right_and_non_overlapping():
    assert find_in_text("哈哈哈哈", "哈哈") == [(0, 2), (2, 4)]
    assert find_in_text("aaaa", "aa") == []  # Latin words match only as whole tokens (see test_review_fixes)
    assert find_in_text("王小明與王小明", "王小明") == [(0, 3), (4, 7)]
    assert find_in_text("abc", "") == []


def test_ocr_match_boxes_cover_exactly_the_matched_characters():
    line = ocr_line("登入者：王小明", x=10, y=10, w=20, h=24)
    [(box, fuzzy)] = find_in_ocr([line], "王小明")
    assert fuzzy is False and box == (10 + 4 * 20 - 2, 8, 10 + 7 * 20 + 2, 36)


def test_ocr_fuzzy_match_allows_one_wrong_cjk_character_for_three_or_more():
    lines = [ocr_line("使用者 王小朋 已登出")]
    [(box, fuzzy)] = find_in_ocr(lines, "王小明")
    assert fuzzy is True
    assert find_in_ocr([ocr_line("小朋友")], "小明") == []
    assert find_in_ocr([ocr_line("電話 0912-345-679")], "0912-345-678") == []
    assert find_in_ocr([ocr_line("王大華")], "王小明") == []


def test_ocr_match_ignores_spaces_the_recogniser_drops():
    assert len(find_in_ocr([ocr_line("王小明先生0912345678")], "0912 345 678")) == 1


def test_text_entity_is_found_in_text_units_images_and_metadata():
    store = FindingStore(inventory())
    f = store.add_text("王小明", "姓名", "model")
    kinds = [(o.unit, o.kind, o.fuzzy) for o in f.occurrences]
    assert kinds == [("t1", "text", False), ("t2", "text", False), ("t2", "text", False), ("t3", "text", False),
                     ("img1", "box", False), ("img1", "box", True), ("m1", "meta", False), ("m2", "meta", False)]
    assert (f.id, f.kind, f.on, f.source) == ("f1", "text", True, "model")
    t1 = f.occurrences[0]
    assert "承辦人 | 王小明 | 分機 2301"[t1.start:t1.end] == "王小明"


def test_unknown_text_is_rejected_and_duplicates_return_the_same_finding():
    store = FindingStore(inventory())
    assert store.add_text("陳美玲", "姓名", "agent") is None
    a = store.add_text("王小明", "姓名", "model")
    b = store.add_text("王小明", "其他", "agent")
    assert a is b and b.type == "姓名" and b.source == "model" and len(store.findings) == 1


def test_readding_a_switched_off_entity_by_user_or_agent_turns_it_back_on():
    store = FindingStore(inventory())
    f = store.add_text("王小明", "姓名", "model")
    store.set_on(f.id, False)
    assert store.add_text("王小明", "姓名", "model").on is False
    assert store.add_text("王小明", "姓名", "user").on is True


def test_nested_entities_merge_into_one_span_and_split_when_one_is_off():
    store = FindingStore(inventory())
    full = store.add_text("王小明", "姓名", "model")
    store.add_text("小明", "姓名", "agent")
    s = "王小明表示小明會處理，王小明同意".index
    assert store.plan().text["t2"] == [(0, 3), (5, 7), (11, 14)]
    assert store.plan().text["t1"] == [(6, 9)]
    store.set_on(full.id, False)
    assert store.plan().text["t2"] == [(1, 3), (5, 7), (12, 14)]
    assert store.plan().text["t1"] == [(7, 9)]
    assert s("小明") == 1


def test_adjacent_spans_are_merged():
    inv = Inventory(kind="docx", texts=[TextUnit("t1", "王小明0912345678", "內文")])
    store = FindingStore(inv)
    store.add_text("王小明", "姓名", "model")
    store.add_text("0912345678", "電話", "model")
    assert store.plan().text["t1"] == [(0, 13)]


def test_switching_off_one_occurrence_keeps_the_rest_but_drops_the_string_from_generic_replacement():
    store = FindingStore(inventory())
    f = store.add_text("王小明", "姓名", "model")
    assert store.plan().strings == ["王小明"]
    store.set_on(f.id, False, occurrence=0)
    plan = store.plan()
    assert "t1" not in plan.text and plan.text["t2"] == [(0, 3), (11, 14)]
    assert plan.strings == [] and f.on is True
    store.set_on(f.id, True)  # the finding as a whole: the place switched off by hand stays off
    assert store.plan().strings == [] and not f.occurrences[0].on
    store.set_on(f.id, True, occurrence=0)
    assert store.plan().strings == ["王小明"] and all(o.on for o in f.occurrences)


def test_mirror_occurrences_follow_the_finding_and_cannot_be_toggled_alone():
    store = FindingStore(inventory())
    f = store.add_text("王小明", "姓名", "model")
    mirror = next(i for i, o in enumerate(f.occurrences) if o.unit == "t3")
    with pytest.raises(ValueError):
        store.set_on(f.id, False, occurrence=mirror)
    assert store.plan().text["t3"] == [(3, 6)]
    store.set_on(f.id, False)
    assert "t3" not in store.plan().text


def test_single_occurrence_can_be_added_on_its_own():
    store = FindingStore(inventory())
    f = store.add_text("2301", "其他識別碼", "user", only=("t1", 15, 19))
    assert [(o.unit, o.start, o.end) for o in f.occurrences] == [("t1", 15, 19)]
    assert store.add_text("2301", "其他", "user", only=("t1", 0, 4)) is None
    assert store.plan().strings == ["2301"]  # that one place is every place: hidden copies are masked too


def test_single_occurrence_of_a_repeated_string_stays_out_of_generic_replacement():
    store = FindingStore(inventory())
    f = store.add_text("王小明", "姓名", "user", only=("t2", 0, 3))
    plan = store.plan()
    assert plan.text == {"t2": [(0, 3)]} and plan.strings == [] and len(f.occurrences) == 1


def test_regions_and_metadata_findings_reach_the_plan():
    store = FindingStore(inventory())
    face = store.add_region("img2", (10, 10, 50, 60), "人臉", "face")
    again = store.add_region("img2", (10, 10, 50, 60), "人臉", "face")
    off = store.add_region("img2", (0, 0, 100, 100), "證件", "vision", on=False)
    author = store.add_meta("m1", on=True)
    title = store.add_meta("m2", on=False)
    assert face is again and (face.kind, off.on, author.kind, author.type, author.text) == ("region", False, "meta", "檔案屬性", "王小明")
    plan = store.plan()
    assert plan.boxes == {"img2": [(10, 10, 50, 60)]} and plan.meta == {"m1": [(0, 3)]} and plan.text == {}
    assert store.add_meta("m1", on=False) is author and title.on is False


def test_image_occurrence_of_a_text_entity_lands_in_plan_boxes():
    store = FindingStore(inventory())
    store.add_text("王小明", "姓名", "model")
    assert len(store.plan().boxes["img1"]) == 2


def test_bulk_switch_by_type_and_removal_rules():
    store = FindingStore(inventory())
    store.add_region("img2", (1, 1, 5, 5), "人臉", "face")
    store.add_region("img2", (6, 6, 9, 9), "人臉", "face")
    model = store.add_text("王小明", "姓名", "model")
    mine = store.add_text("小明", "姓名", "user")
    assert store.bulk(type="人臉", on=False) == 2 and store.bulk(type="人臉", on=False) == 0
    assert all(not f.on for f in store.findings if f.type == "人臉")
    assert store.bulk(source="model", on=False) == 1
    with pytest.raises(ValueError):
        store.remove(model.id)
    store.remove(mine.id)
    assert mine not in store.findings
    with pytest.raises(KeyError):
        store.set_on("f999", True)


def test_set_type_changes_the_label():
    store = FindingStore(inventory())
    f = store.add_text("王小明", "姓名", "model")
    store.set_type(f.id, "其他")
    assert f.type == "其他"


def test_rematch_adds_image_occurrences_once_ocr_arrives():
    inv = inventory()
    inv.images[0].ocr = []
    store = FindingStore(inv)
    f = store.add_text("王小明", "姓名", "model")
    assert not any(o.kind == "box" for o in f.occurrences)
    inv.images[0].ocr = [ocr_line("登入者：王小明")]
    store.rematch()
    store.rematch()
    assert sum(o.kind == "box" for o in f.occurrences) == 1


def test_pdf_text_occurrences_carry_page_boxes_from_the_locator():
    seen = []

    def locate(unit, start, end):
        seen.append((unit, start, end))
        return [("pg1", (1.0, 2.0, 3.0, 4.0))]

    store = FindingStore(inventory(), locate=locate)
    f = store.add_text("王小明", "姓名", "model")
    assert f.occurrences[0].page_boxes == [("pg1", (1.0, 2.0, 3.0, 4.0))] and seen[0] == ("t1", 6, 9)


def test_json_roundtrip_preserves_the_plan_and_the_id_counter():
    inv = inventory()
    store = FindingStore(inv)
    f = store.add_text("王小明", "姓名", "model")
    store.set_on(f.id, False, occurrence=1)
    store.add_region("img2", (10, 10, 50, 60), "人臉", "face")
    store.add_meta("m1", on=True)
    again = FindingStore.from_json(inv, store.to_json())
    assert again.plan() == store.plan()
    assert again.add_text("小明", "姓名", "user").id == "f4"


# ---- strings the agent adds inside something already masked ----------------------------------

def mailboxes():
    return Inventory(kind="docx", texts=[TextUnit("t1", "信箱 alice0609@example.com", "內文"),
                                         TextUnit("t2", "聯絡 bob77@example.com 或找 bob77", "內文"),
                                         TextUnit("t3", "承辦 王小明先生", "內文")])


def test_an_agent_string_wholly_inside_what_another_finding_masks_is_left_out_of_the_list_but_kept():
    store = FindingStore(mailboxes())
    mail = store.add_text("alice0609@example.com", "電子郵件", "rule")
    before = store.plan()
    part = store.add_text("alice0609", "其他", "agent")
    assert store.redundant() == {part.id}
    assert [f.id for f in store.listed()] == [mail.id] and part in store.findings
    assert store.plan().text == before.text  # the output is the same with or without it
    assert [r["text"] for r in store.report()] == ["alice0609@example.com"]


def test_it_is_listed_when_one_of_its_places_is_not_masked_by_anything_else():
    store = FindingStore(mailboxes())
    store.add_text("bob77@example.com", "電子郵件", "rule")
    alone = store.add_text("bob77", "其他", "agent")  # also stands by itself at the end of t2
    assert store.redundant() == set() and alone in store.listed()


def test_it_comes_back_when_what_covered_it_is_switched_off():
    store = FindingStore(mailboxes())
    mail = store.add_text("alice0609@example.com", "電子郵件", "rule")
    part = store.add_text("alice0609", "其他", "agent")
    store.set_on(mail.id, False)
    assert store.redundant() == set() and part in store.listed()
    store.set_on(mail.id, True)
    store.set_on(mail.id, False, occurrence=0)  # the one place it covered
    assert store.redundant() == set()


def test_one_the_agent_switched_off_itself_is_left_out_too():
    store = FindingStore(mailboxes())
    store.add_text("alice0609@example.com", "電子郵件", "rule")
    part = store.add_text("alice0609", "其他", "agent", on=False)
    assert store.redundant() == {part.id}


def test_only_agent_strings_are_left_out_a_detected_name_inside_a_longer_mask_stays_listed():
    store = FindingStore(mailboxes())
    name = store.add_text("王小明", "姓名", "model")
    store.add_text("王小明先生", "姓名", "agent")
    assert store.redundant() == set() and name in store.listed()
    mine = store.add_text("alice0609", "其他", "user")
    store.add_text("alice0609@example.com", "電子郵件", "rule")
    assert mine in store.listed()


# ---- partial masking (style) ------------------------------------------------------------------

def test_findings_saved_before_styles_existed_load_as_fully_masked():
    store = FindingStore(inventory())
    store.add_text("王小明", "姓名", "model")
    data = store.to_json()
    for f in data["findings"]:
        del f["style"]
    data.pop("styles", None)
    again = FindingStore.from_json(inventory(), data)
    assert again.findings[0].style == "full"
    assert again.plan().text["t1"] == [(6, 9)]


def test_a_partial_finding_masks_only_the_characters_its_rule_hides():
    store = FindingStore(inventory())
    f = store.add_text("王小明", "姓名", "model")
    store.set_style(f.id, "partial")
    plan = store.plan()
    assert plan.text == {"t1": [(7, 8)], "t2": [(1, 2), (12, 13)], "t3": [(4, 5)]}
    assert plan.meta == {"m1": [(1, 2)], "m2": [(1, 2)]}
    assert plan.strings == ["王小明"] and plan.partial == {"王小明": [(1, 2)]}
    assert len(plan.boxes["img1"]) == 2  # text inside pictures is still covered whole


def test_only_some_types_and_only_text_findings_can_be_partial():
    store = FindingStore(inventory())
    name = store.add_text("王小明", "姓名", "model")
    other = store.add_text("分機 2301", "金融帳號", "user")
    meta = store.add_meta("m1", on=True)
    region = store.add_region("img2", (0, 0, 10, 10), "人臉", "face")
    for f in (other, meta, region):
        with pytest.raises(ValueError):
            store.set_style(f.id, "partial")
    with pytest.raises(ValueError):
        store.set_style(name.id, "half")
    store.set_style(name.id, "partial")
    store.set_type(name.id, "金融帳號")  # a type that has no partial rule: back to whole
    assert name.style == "full"


def test_a_string_the_rule_does_not_fit_is_masked_whole_even_when_partial():
    inv = Inventory(kind="docx", texts=[TextUnit("t1", "代表號 1234567", "內文")])
    store = FindingStore(inv)
    f = store.add_text("1234567", "電話", "user")
    store.set_style(f.id, "partial")
    plan = store.plan()
    assert plan.text == {"t1": [(4, 11)]} and plan.partial == {}


def test_inside_a_longer_finding_of_the_same_type_the_longer_one_decides():
    store = FindingStore(inventory())
    full_name = store.add_text("王小明", "姓名", "model")
    store.add_text("小明", "姓名", "agent")
    assert store.plan().text["t2"] == [(0, 3), (5, 7), (11, 14)]
    assert store.plan().lenient == []
    store.set_style(full_name.id, "partial")
    plan = store.plan()
    # 王■明 where the full name stands, ■■ where the given name stands alone
    assert plan.text["t2"] == [(1, 2), (5, 7), (12, 13)]
    assert plan.strings == ["王小明", "小明"] and plan.lenient == []  # ■明 is no longer the given name
    store.set_on(full_name.id, False, occurrence=1)  # the first place in t2 is left alone by the longer one
    assert store.plan().text["t2"] == [(1, 3), (5, 7), (12, 13)]


def test_findings_of_different_types_that_overlap_are_both_applied():
    store = FindingStore(inventory())
    full_name = store.add_text("王小明", "姓名", "model")
    store.add_text("小明", "其他", "user")
    store.set_style(full_name.id, "partial")
    assert store.plan().text["t2"] == [(1, 3), (5, 7), (12, 14)]


def test_a_whole_type_can_be_switched_and_later_findings_of_it_follow():
    inv = Inventory(kind="docx", texts=[TextUnit("t1", "王小明與陳美玲，電話 0912-345-678，帳號 0123-456-789012", "內文")])
    store = FindingStore(inv)
    store.add_text("王小明", "姓名", "model")
    store.add_text("0912-345-678", "電話", "rule")
    assert store.bulk(type="姓名", style="partial") == 1
    later = store.add_text("陳美玲", "姓名", "user")
    assert [f.style for f in store.findings] == ["partial", "full", "partial"]
    again = FindingStore.from_json(inv, store.to_json())
    assert again.styles == {"姓名": "partial"}
    moved = again.add_text("0123-456-789012", "金融帳號", "user")
    again.set_type(moved.id, "姓名")
    assert moved.style == "partial"
    assert again.bulk(type="姓名", style="full") == 3 and again.styles == {}
    assert later.style == "partial"  # the first store is untouched
    with pytest.raises(ValueError):
        store.bulk(type="金融帳號", style="partial")


def test_an_agent_string_is_hidden_only_when_other_findings_really_mask_all_of_it():
    inv = Inventory(kind="docx", texts=[TextUnit("t1", "編號 EMP-00123-TPE", "內文")])
    store = FindingStore(inv)
    number = store.add_text("EMP-00123-TPE", "其他識別碼", "rule")
    head = store.add_text("EMP", "其他", "agent")
    middle = store.add_text("00123", "其他", "agent")
    assert store.redundant() == {head.id, middle.id}
    store.set_style(number.id, "partial")  # E**-*****-TPE: the first letter is no longer covered
    assert store.redundant() == {middle.id}
    assert [f.id for f in store.listed()] == [number.id, head.id]
    assert store.plan().text == {"t1": [(3, 6), (7, 12)]}  # another type: it adds to what the number masks


def test_inside_a_mailbox_the_mailbox_decides_whatever_type_the_shorter_one_has():
    store = FindingStore(mailboxes())
    box = store.add_text("alice0609@example.com", "電子郵件", "rule")
    other = store.add_text("bob77@example.com", "電子郵件", "rule")
    store.add_text("alice0609", "其他識別碼", "agent")  # what the model adds: the account before the @
    store.add_text("bob77", "其他識別碼", "model")
    store.add_text("example.com", "其他", "user")
    for f in (box, other):
        store.set_style(f.id, "partial")
    plan = store.plan()
    # a*******9@example.com; b***7@example.com, and bob77 standing by itself is masked whole
    assert plan.text == {"t1": [(4, 11)], "t2": [(4, 7), (24, 29)]}
    assert plan.lenient == ["example.com"] and "alice0609" in plan.strings and "bob77" in plan.strings
    store.set_on(box.id, False)  # the mailbox is left alone: what lies inside it is on its own again
    assert store.plan().text["t1"] == [(3, 12), (13, 24)]


def test_an_agent_string_that_only_stands_inside_mailboxes_is_left_out_of_the_list():
    store = FindingStore(mailboxes())
    box = store.add_text("alice0609@example.com", "電子郵件", "rule")
    other = store.add_text("bob77@example.com", "電子郵件", "rule")
    local = store.add_text("alice0609", "其他識別碼", "agent")
    alone = store.add_text("bob77", "其他識別碼", "agent")  # also stands by itself at the end of t2
    for f in (box, other):
        store.set_style(f.id, "partial")
    assert store.redundant() == {local.id}  # it changes nothing: the list would show ********* for a*******9
    assert [f.id for f in store.listed()] == [box.id, other.id, alone.id]
    store.set_on(box.id, False)
    assert store.redundant() == set()


def test_the_report_says_how_each_entry_is_masked():
    store = FindingStore(inventory())
    f = store.add_text("王小明", "姓名", "model")
    assert [(r["style"], r["masked"]) for r in store.report()] == [("full", "■■■")]
    store.set_style(f.id, "partial")
    assert [(r["style"], r["masked"]) for r in store.report("■", "*")] == [("partial", "王■明")]


def test_a_string_the_longer_partial_finding_leaves_readable_is_not_searched_for_elsewhere():
    inv = Inventory(kind="docx", texts=[TextUnit("t1", "信箱 lin.demo@example.com", "內文")])
    store = FindingStore(inv)
    box = store.add_text("lin.demo@example.com", "電子郵件", "rule")
    store.add_text("example.com", "電子郵件", "user")
    assert store.plan().lenient == []
    store.set_style(box.id, "partial")
    plan = store.plan()
    assert plan.text == {"t1": [(4, 10)]}
    assert plan.strings == ["lin.demo@example.com"] and plan.lenient == ["example.com"]


# ---- custom masking: the reviewer picks the characters (2026-11-19) ------------------------------

def custom_store():
    inv = Inventory(kind="docx", texts=[TextUnit("t1", "備註 王小明的專案代號AB12 已結案", "內文"),
                                         TextUnit("t2", "另見 王小明的專案代號AB12", "內文")],
                    metas=[MetaUnit("m1", "公司", "大明科技股份有限公司", False)])
    return FindingStore(inv)


def test_a_custom_finding_masks_the_characters_the_reviewer_picked_everywhere_it_stands():
    store = custom_store()
    f = store.add_text("王小明的專案代號AB12", "其他", "user")
    store.set_spans(f.id, [(1, 3), (9, 12)])
    assert (f.style, f.spans) == ("custom", [(1, 3), (9, 12)])
    plan = store.plan()
    assert plan.text == {"t1": [(4, 6), (12, 15)], "t2": [(4, 6), (12, 15)]}
    assert plan.strings == [f.text] and plan.partial == {f.text: [(1, 3), (9, 12)]}
    assert store.shown_as(f, "■", "*") == "王■■的專案代號A***"
    assert [(r["style"], r["masked"]) for r in store.report("■", "*")] == [("custom", "王■■的專案代號A***")]
    again = FindingStore.from_json(store.inv, json.loads(json.dumps(store.to_json())))
    assert (again.findings[0].style, again.findings[0].spans) == ("custom", [(1, 3), (9, 12)])
    assert again.plan().text == plan.text


def test_switching_to_custom_starts_from_how_the_finding_is_masked_now():
    inv = Inventory(kind="docx", texts=[TextUnit("t1", "王小明 0912-345-678 專案甲", "內文")])
    store = FindingStore(inv)
    name, phone, other = (store.add_text(t, k, "user") for t, k in (("王小明", "姓名"), ("0912-345-678", "電話"), ("專案甲", "其他")))
    store.set_style(name.id, "partial")
    for f in (name, phone, other):
        store.set_style(f.id, "custom")
    assert (name.spans, phone.spans, other.spans) == ([(1, 2)], [(0, 12)], [(0, 3)])
    store.set_spans(phone.id, [(5, 8)])
    store.set_style(phone.id, "custom")  # already custom: what was picked stays
    assert phone.spans == [(5, 8)]
    store.set_style(phone.id, "partial")  # leaving custom forgets the picked characters
    assert (phone.style, phone.spans) == ("partial", [])
    store.set_style(phone.id, "full")
    assert store.plan().text == {"t1": [(1, 2), (4, 16), (17, 20)]}


def test_a_file_property_can_be_masked_in_part():
    store = custom_store()
    meta = store.add_meta("m1", on=True)
    assert store.plan().meta == {"m1": [(0, 10)]}
    store.set_spans(meta.id, [(0, 2)])
    assert store.plan().meta == {"m1": [(0, 2)]} and store.shown_as(meta, "■", "*") == "■■科技股份有限公司"
    assert store.report("■", "*")[0]["style"] == "custom"


def test_ranges_are_tidied_and_refused_when_they_mask_nothing_or_reach_outside():
    store = custom_store()
    f = store.add_text("王小明的專案代號AB12", "其他", "user")
    store.set_spans(f.id, [(9, 12), (1, 2), (2, 3), (1, 2)])
    assert f.spans == [(1, 3), (9, 12)]
    for bad in ([], [(3, 3)], [(-1, 2)], [(0, 13)], [(5, 2)], "x", [("a", "b")], [(1,)]):
        with pytest.raises(ValueError):
            store.set_spans(f.id, bad)
    assert f.spans == [(1, 3), (9, 12)]  # a refused change leaves what was there
    spaced = store.add_text("專案代號AB12 已結案", "其他", "user")
    with pytest.raises(ValueError):
        store.set_spans(spaced.id, [(8, 9)])  # only the space: nothing would be masked


def test_a_region_cannot_be_custom():
    store = FindingStore(inventory())
    region = store.add_region("img2", (0, 0, 10, 10), "人臉", "face")
    for change in (lambda: store.set_style(region.id, "custom"), lambda: store.set_spans(region.id, [(0, 1)])):
        with pytest.raises(ValueError):
            change()


def test_switching_a_whole_type_leaves_custom_findings_as_they_are_and_so_does_changing_the_type():
    inv = Inventory(kind="docx", texts=[TextUnit("t1", "王小明與陳美玲", "內文")])
    store = FindingStore(inv)
    a = store.add_text("王小明", "姓名", "model")
    b = store.add_text("陳美玲", "姓名", "model")
    store.set_spans(a.id, [(0, 1)])
    assert store.bulk(type="姓名", style="partial") == 1
    assert (a.style, a.spans, b.style) == ("custom", [(0, 1)], "partial")
    assert store.bulk(type="姓名", style="full") == 1 and a.style == "custom"
    store.set_type(a.id, "其他")
    assert (a.style, a.spans) == ("custom", [(0, 1)])
    assert store.plan().text == {"t1": [(0, 1), (4, 7)]}


def test_the_code_version_ignores_custom_ranges():
    store = custom_store()
    f = store.add_text("王小明的專案代號AB12", "其他", "user")
    store.set_spans(f.id, [(1, 3)])
    assert store.plan(whole=True).text == {"t1": [(3, 15)], "t2": [(3, 15)]} and store.plan(whole=True).partial == {}


# ---- names of one person ------------------------------------------------------------------

NAME_TYPE = "姓名"


def names_store():
    inv = Inventory(kind="docx", texts=[TextUnit("t1", "王小明與小明、Wang 一起，陳美玲另到，電話 0912-345-678", "內文")])
    store = FindingStore(inv)
    found = {text: store.add_text(text, NAME_TYPE, "model") for text in ("王小明", "小明", "Wang", "陳美玲")}
    found["電話"] = store.add_text("0912-345-678", "電話", "rule")
    return store, found


def test_linked_names_share_a_group_that_survives_saving():
    s, f = names_store()
    g = s.link([f["王小明"].id, f["小明"].id])
    assert g == "g1" and [x.text for x in s.groups()[g]] == ["王小明", "小明"]
    assert f["陳美玲"].group == "" and list(s.groups()) == ["g1"]
    again = FindingStore.from_json(s.inv, s.to_json())
    assert {x.text for x in again.groups()["g1"]} == {"王小明", "小明"}


def test_link_refuses_what_is_not_two_active_names():
    s, f = names_store()
    s.set_on(f["陳美玲"].id, False)
    for ids in ([f["王小明"].id], [f["王小明"].id, f["王小明"].id], [f["王小明"].id, f["電話"].id],
                [f["王小明"].id, f["陳美玲"].id], [f["王小明"].id, "f999"]):
        with pytest.raises((ValueError, KeyError)):
            s.link(ids)
    assert s.groups() == {} and all(x.group == "" for x in s.findings)


def test_linking_with_a_member_joins_its_group():
    s, f = names_store()
    s.link([f["王小明"].id, f["小明"].id])
    assert s.link([f["小明"].id, f["Wang"].id]) == "g1"
    assert [x.text for x in s.groups()["g1"]] == ["王小明", "小明", "Wang"]
    assert s.link([f["王小明"].id, f["Wang"].id]) == "g1"  # already together: nothing changes


def test_names_of_two_groups_cannot_be_linked():
    s, f = names_store()
    a = s.link([f["王小明"].id, f["小明"].id])
    b = s.link([f["Wang"].id, f["陳美玲"].id])
    assert (a, b) == ("g1", "g2")
    with pytest.raises(ValueError):
        s.link([f["小明"].id, f["Wang"].id])
    assert {g: [x.text for x in members] for g, members in s.groups().items()} == {"g1": ["王小明", "小明"], "g2": ["Wang", "陳美玲"]}


def test_switching_off_or_retyping_leaves_the_group():
    s, f = names_store()
    s.link([f["王小明"].id, f["小明"].id, f["Wang"].id])
    s.set_on(f["Wang"].id, False)
    assert [x.text for x in s.groups()["g1"]] == ["王小明", "小明"] and f["Wang"].group == ""
    s.set_on(f["Wang"].id, True)
    assert f["Wang"].group == ""  # coming back on does not put it back
    s.set_type(f["小明"].id, "其他")
    assert s.groups() == {} and f["王小明"].group == ""  # a group of one is no group


def test_switching_off_the_last_place_of_a_name_leaves_the_group():
    s, f = names_store()
    s.link([f["王小明"].id, f["小明"].id, f["Wang"].id])
    s.set_on(f["Wang"].id, False, occurrence=0)
    assert f["Wang"].on is False and f["Wang"].group == ""


def test_a_name_can_be_taken_out_of_its_group():
    s, f = names_store()
    s.link([f["王小明"].id, f["小明"].id, f["Wang"].id])
    s.unlink(f["小明"].id)
    assert [x.text for x in s.groups()["g1"]] == ["王小明", "Wang"]
    s.unlink(f["Wang"].id)
    assert s.groups() == {}
    s.unlink(f["Wang"].id)  # not in a group: nothing to do
    assert s.link([f["Wang"].id, f["陳美玲"].id]) == "g1"  # the smallest number not in use


def test_findings_saved_before_groups_existed_still_load():
    s, f = names_store()
    data = s.to_json()
    for d in data["findings"]:
        del d["group"]
    again = FindingStore.from_json(s.inv, data)
    assert all(x.group == "" for x in again.findings) and again.groups() == {}
