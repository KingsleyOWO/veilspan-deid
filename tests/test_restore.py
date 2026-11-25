"""Putting the strings back into text that came back with codes in it. All values are made up."""
from deid.codes import EN_NOTICE, EN_NOTICE_SAME
from deid.restore import restore

TABLE = {"[姓名1]": "王小明", "[姓名2]": "陳美玲", "[電話1]": "0912-345-678", "[個資1]": "特殊需求", "[姓名12]": "林志豪"}


def test_codes_are_replaced_by_what_they_stand_for():
    r = restore("已通知 [姓名1]，請於週五前回電 [電話1]。[姓名1] 與 [姓名2] 均同意。", TABLE)
    assert r.text == "已通知 王小明，請於週五前回電 0912-345-678。王小明 與 陳美玲 均同意。"
    assert r.replaced == 4 and r.unknown == [] and r.unused == ["[個資1]", "[姓名12]"]


def test_a_longer_number_is_not_read_as_a_shorter_one():
    assert restore("[姓名12] 與 [姓名1]", TABLE).text == "林志豪 與 王小明"


def test_the_ways_a_model_tends_to_rewrite_a_code_are_still_recognised():
    text = "【姓名1】、［姓名２］、\\[電話1\\]、[ 姓名 1 ]、〔個資1〕、**[姓名2]**"
    r = restore(text, TABLE)
    assert r.text == "王小明、陳美玲、0912-345-678、王小明、特殊需求、**陳美玲**"
    assert r.replaced == 6


def test_codes_that_are_not_in_the_table_stay_and_are_listed_once():
    r = restore("[姓名3] 回覆了 [姓名1]，並轉給 [姓名3] 與 [地址1]", TABLE)
    assert r.text == "[姓名3] 回覆了 王小明，並轉給 [姓名3] 與 [地址1]"
    assert r.unknown == ["[姓名3]", "[地址1]"] and r.replaced == 1


def test_brackets_that_are_not_codes_are_left_alone():
    text = "見【表1】與 [1]、[已遮蔽]、[TODO]、[註2]，【第 1 頁】"
    r = restore(text, TABLE)
    assert r.text == text and r.unknown == [] and r.replaced == 0


def test_what_is_put_back_is_not_read_again_as_a_code():
    r = restore("[姓名1]", {"[姓名1]": "[姓名2]", "[姓名2]": "王小明"})
    assert r.text == "[姓名2]" and r.replaced == 1


def test_an_empty_table_changes_nothing():
    r = restore("沒有代號的回覆 [姓名1]", {})
    assert r.text == "沒有代號的回覆 [姓名1]" and r.unknown == ["[姓名1]"] and r.unused == []


# ---- one number for the writings of one person: [姓名1], [姓名1b] --------------------------

SAME = {"[姓名1]": "王小明", "[姓名1b]": "小明"}


def test_lettered_codes_restore_to_their_own_string_however_rewritten():
    done = restore("[姓名1B]、［姓名１ｂ］、[姓名1 b]、\\[姓名1b\\] 與 【姓名1】", SAME)
    assert done.text == "小明、小明、小明、小明 與 王小明" and done.replaced == 5
    assert done.unknown == [] and done.unused == []


def test_a_letter_the_table_does_not_have_is_listed_and_left_alone():
    done = restore("[姓名1c] 與 [姓名1]", SAME)
    assert done.text == "[姓名1c] 與 王小明" and done.unknown == ["[姓名1c]"] and done.unused == ["[姓名1b]"]


def test_english_export_notices_do_not_restore_example_codes():
    text = f"{EN_NOTICE}\n{EN_NOTICE_SAME}\n\nContact 【NAME 1】 and [NAME1b]."
    done = restore(text, {"[NAME1]": "Avery Reed", "[NAME1b]": "Avery"})
    assert done.text == "Contact Avery Reed and Avery."
    assert done.replaced == 2 and not done.unknown and not done.unused
