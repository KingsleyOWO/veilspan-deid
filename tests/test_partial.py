"""Partial masking rules: which characters of a finding stay readable. All values are made up."""
import pytest

from deid.partial import PARTIAL_TYPES, applies, mask_spans, preview


def shown(kind: str, text: str) -> str:
    return preview(kind, text, "partial", "■", "*")


@pytest.mark.parametrize("text, expected", [
    ("王小明", "王■明"),
    ("王明", "王■"),
    ("歐陽小明", "歐■■明"),
    ("王", "■"),
    ("John Smith", "J*** S****"),
    ("Mary-Jane O'Neil", "M***-J*** O'N***"),
    ("J. Smith", "J. S****"),
    ("陳 David", "■ D****"),
])
def test_names_keep_the_first_and_last_character(text, expected):
    assert shown("姓名", text) == expected


@pytest.mark.parametrize("text, expected", [
    ("林先生", "■先生"),
    ("王小明先生", "王■明先生"),
    ("陳美玲 小姐", "陳■玲 小姐"),
    ("李老師", "■老師"),
    ("先生", "■■"),  # nothing but a title: no rule fits, so everything goes
])
def test_a_title_after_a_name_stays_and_the_rule_applies_to_what_is_before_it(text, expected):
    assert shown("姓名", text) == expected


def test_a_name_with_digits_in_it_never_shows_the_digits():
    assert shown("姓名", "王小明A123456789") == "王■明**********"  # nor the letter the number starts with


@pytest.mark.parametrize("text, expected", [
    ("0912-345-678", "0912-***-678"),
    ("0912345678", "0912***678"),
    ("02-2345-6789", "02-23**-*789"),
    ("+886-912-345-678", "+886-9**-***-678"),
    ("(03) 123-4567", "(03) 1**-*567"),
    ("2345-6789", "23**-*789"),
    ("1234567", "*******"),  # too short to keep anything
    ("分機 123", "■■ ***"),
])
def test_phones_keep_the_head_and_the_last_three_digits(text, expected):
    assert shown("電話", text) == expected


@pytest.mark.parametrize("text, expected", [
    ("lin.demo@example.com", "l******o@example.com"),
    ("abc@example.com", "a*c@example.com"),
    ("ab@example.com", "a*@example.com"),  # two characters: only the first stays, as with a name of two
    ("a@example.com", "*@example.com"),
    ("not-a-mailbox", "*************"),
    ("@example.com", "************"),
])
def test_mailboxes_keep_the_first_and_last_character_before_the_at_and_the_domain(text, expected):
    assert shown("電子郵件", text) == expected


@pytest.mark.parametrize("kind, text, expected", [
    ("身分證字號", "A123456789", "A******789"),
    ("護照號碼", "312345678", "3*****678"),
    ("其他識別碼", "EMP-00123", "E**-**123"),
    ("其他識別碼", "A1234", "*****"),  # fewer than six: all of it
])
def test_identifiers_keep_the_first_and_the_last_three(kind, text, expected):
    assert shown(kind, text) == expected


@pytest.mark.parametrize("text, expected", [
    ("台北市信義區市府路1號", "台北市信義區■■■*■"),
    ("110 台北市信義區市府路1號", "110 台北市信義區■■■*■"),
    ("新竹縣竹北市光明六路10號", "新竹縣竹北市■■■■**■"),
    ("高雄市前鎮區中山二路5號", "高雄市前鎮區■■■■*■"),
    ("信義區市府路1號", "信義區■■■*■"),
    ("台北市市府路1號", "台北市■■■*■"),
    ("市府路1號", "■■■*■"),  # no city or district to keep
    ("台北市信義區", "■■■■■■"),  # nothing would be masked: the rule does not fit
])
def test_addresses_keep_the_city_and_district(text, expected):
    assert shown("地址", text) == expected


@pytest.mark.parametrize("text, expected", [
    ("1979/05/03", "1979/**/**"),
    ("1979-5-3", "1979-*-*"),
    ("民國 68 年 5 月 3 日", "民國 68 年 * 月 * 日"),
    ("民國68年5月3日", "民國68年*月*日"),
    ("68.05.03", "68.**.**"),
    ("03/05/1979", "**/**/1979"),
    ("19790503", "1979****"),
    ("0680503", "068****"),
    ("六十八年五月三日", "六十八年■月■日"),
    ("5月3日", "*■*■"),  # no year in it
    ("1979", "****"),  # nothing would be masked
])
def test_birth_dates_keep_the_year(text, expected):
    assert shown("出生年月日", text) == expected


def test_other_types_are_always_masked_whole():
    assert "金融帳號" not in PARTIAL_TYPES
    assert mask_spans("金融帳號", "0123-456-789012") == [(0, 15)]
    assert shown("金融帳號", "0123-456-789012") == "*" * 15
    assert not applies("金融帳號", "0123-456-789012")


def test_full_style_masks_everything_whatever_the_type():
    assert preview("姓名", "王小明", "full", "■", "*") == "■■■"


def test_spans_are_half_open_ranges_inside_the_text():
    assert mask_spans("姓名", "王小明") == [(1, 2)]
    assert mask_spans("電話", "0912-345-678") == [(5, 8)]
    assert mask_spans("姓名", "John Smith") == [(1, 4), (6, 10)]


def test_applies_tells_whether_the_rule_keeps_anything():
    assert applies("姓名", "王小明")
    assert not applies("姓名", "王")
    assert not applies("電話", "1234567")


def test_empty_text_has_nothing_to_mask():
    assert mask_spans("姓名", "") == []
    assert shown("姓名", "") == ""


# ---- review fixes: a rule never shows what is not the thing it masks ---------------------------

@pytest.mark.parametrize("kind, text, expected", [
    ("電話", "0912345678 王小明", "0912***678 ■■■"),
    ("電話", "(02)2345-6789 ext 12", "(02)23**-***9 *** 12"),  # the last three digits are counted over the whole string
    ("身分證字號", "A123456789 王小明", "A******789 ■■■"),
    ("其他識別碼", "北市字第1234567號", "■■■■1***567■"),
    ("電子郵件", "a@x.com, bob@y.com", "*@x.com* *********"),
    ("電子郵件", "wang@example.com（王小明）", "w**g@example.com■■■■■"),
    ("電子郵件", "信箱 wang@example.com", "■■ ****************"),  # does not start with the address: no rule fits
    ("出生年月日", "1979/05/03 王小明", "1979/**/** ■■■"),
    ("出生年月日", "民國68年5月3日生", "民國68年*月*日生"),
    ("姓名", "王小明A123456789", "王■明**********"),
    ("姓名", "J. Smith", "J. S****"),
])
def test_text_glued_to_the_item_is_masked_not_shown(kind, text, expected):
    assert shown(kind, text) == expected
