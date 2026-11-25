import pytest

from deid.patterns import candidates, is_safety_net

B1 = ("會議紀錄 2026/03/12：陳志明經理（身分證 A123456789，民國 68 年 5 月 3 日生）報告第一季營收成長 12%。"
      "採購型號 A123-4567 的伺服器共 3 台，訂單編號 4311-2222-3333-4444 已出貨。信用卡 4311 2222 3333 4444 到期日 08/28。"
      "聯絡林先生請打 0912-345-678 或寄 lin.demo@example.com。")


def by_text(text):
    return {c.text: c for c in candidates(text)}


def test_valid_national_id_is_verified():
    c = by_text("身分證 A123456789，")["A123456789"]
    assert (c.kind, c.verified) == ("身分證字號", True)
    assert "身分證 A123456789，"[c.start:c.end] == "A123456789"


def test_id_with_bad_checksum_is_not_a_candidate():
    assert "A123456788" not in by_text("編號 A123456788 ")


def test_id_embedded_in_longer_token_is_ignored():
    assert by_text("序號XA1234567890Z") == {}


def test_new_style_resident_certificate_is_verified():
    c = by_text("居留證 A800000014 ")["A800000014"]
    assert (c.kind, c.verified) == ("居留證號", True)


def test_luhn_valid_card_is_verified_with_any_separator():
    found = by_text("卡號 4111 1111 1111 1111 與 4111-1111-1111-1111")
    assert found["4111 1111 1111 1111"].verified and found["4111-1111-1111-1111"].verified
    assert found["4111 1111 1111 1111"].kind == "信用卡號"


def test_card_like_number_failing_luhn_is_unverified():
    assert by_text("信用卡 4311 2222 3333 4444 到期")["4311 2222 3333 4444"].verified is False


def test_mobile_and_email_are_unverified_candidates():
    found = by_text(B1)
    assert (found["0912-345-678"].kind, found["0912-345-678"].verified) == ("手機", False)
    assert (found["lin.demo@example.com"].kind, found["lin.demo@example.com"].verified) == ("電子郵件", False)


def test_model_number_and_dates_are_not_candidates():
    found = by_text(B1)
    assert "A123-4567" not in found and "2026/03/12" not in found and "08/28" not in found


def test_safety_net_takes_verified_ids_without_context():
    text = "附註：小明另提供 A123456789 供報名使用。"
    c = by_text(text)["A123456789"]
    assert is_safety_net(c, text) is True


def test_safety_net_takes_cards_only_when_the_word_card_precedes():
    order = "訂單編號 4111-1111-1111-1111 已出貨。"
    card = "信用卡 4111 1111 1111 1111 到期日"
    assert is_safety_net(by_text(order)["4111-1111-1111-1111"], order) is False
    assert is_safety_net(by_text(card)["4111 1111 1111 1111"], card) is True


def test_safety_net_never_takes_a_card_number_that_fails_its_checksum():
    text = "信用卡 4111 1111 1111 1112 到期日"
    assert is_safety_net(by_text(text)["4111 1111 1111 1112"], text) is False


def test_a_customer_or_member_number_in_a_link_is_a_candidate_and_only_the_number():
    text = "員工預約連結：https://www.clinic.example.com.tw/order/?CustomerNo=90412&lang=zh 眷屬 www.clinic.example.com.tw/o?member_id=AB-77#top"
    found = by_text(text)
    assert found["90412"].kind == "客戶編號" and text[found["90412"].start:found["90412"].end] == "90412"
    assert found["AB-77"].kind == "客戶編號"
    assert is_safety_net(found["90412"], text) is True


def test_the_names_a_link_uses_for_a_customer_number():
    for name in ("cust", "CustNo", "customer", "customer_id", "CustomerNumber", "MemberNo", "member-code", "memberId"):
        text = f"https://shop.example.com/a?x=1&{name}=K8P2Q"
        assert [c.text for c in candidates(text) if c.kind == "客戶編號"] == ["K8P2Q"], name


def test_other_link_parameters_are_not_customer_numbers():
    for query in ("?ProjectNo=2031040077", "?id=2031040077", "?guid=0a1b2c3d-1111", "?customize=1", "?customer_service=on",
                  "?client_id=abc123", "?members=12", "?cust="):
        assert [c for c in candidates("https://intra.example.org.tw/page" + query) if c.kind == "客戶編號"] == [], query
    assert [c for c in candidates("客戶編號 CustomerNo=90412 請洽櫃台") if c.kind == "客戶編號"] == []  # not in a link


def test_safety_net_takes_every_mailbox():
    text = "來信 it-service@hospital.example.org.tw 或 lin.demo@example.com"
    assert [is_safety_net(c, text) for c in candidates(text)] == [True, True]


@pytest.mark.parametrize("text, number", [
    ("如有問題請洽資訊室，總機 (02)2592-6998 轉 2301。", "(02)2592-6998"),
    ("電話：02-2592-6998#2301", "02-2592-6998"),
    ("Tel 04 2345 6789", "04 2345 6789"),
    ("客服專線 0800-123-456", "0800-123-456"),
    ("國際電話 +886-2-2592-6998", "+886-2-2592-6998"),
    ("總機 2592-6998 轉 522", "2592-6998"),
    ("傳真：2592 6999", "2592 6999"),
    ("手機 0912-345-678", "0912-345-678"),
])
def test_a_phone_number_is_always_masked_whoever_it_belongs_to(text, number):
    found = {c.text: c for c in candidates(text)}
    assert number in found and is_safety_net(found[number], text) is True
    assert not [t for t in found if t in ("2301", "522")]  # the extension is not part of it


@pytest.mark.parametrize("text", ["帳號 0123-456-789012", "訂單編號 4311-2222-3333-4444", "會議日期 2026/03/12", "型號 A123-4567",
                                  "統一編號 12345678", "1999-2000 年度預算", "分機 2301", "比例 0.25-0.50", "編號 0123456789",
                                  "ISBN 0-306-40615-2", "尺寸 2592-6998 公釐"])
def test_numbers_that_are_not_written_as_phone_numbers_are_left_alone(text):
    assert [c.text for c in candidates(text) if c.kind in ("電話", "手機")] == []
