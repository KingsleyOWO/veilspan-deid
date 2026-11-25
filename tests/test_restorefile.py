"""The restore file: the table that turns codes back into what they stand for. Since 2026-11-19 it is written
without a password (the owner's decision); files written before, which are encrypted, are still opened.
All values are made up."""
import base64
import json

import pytest

from deid.restorefile import NeedsPassword, NotRestoreFile, WrongKey, open_, pack
from tests.builders import make_encrypted_restore_file as encrypted

TABLE = {"[姓名1]": "王小明", "[電話1]": "0912-345-678", "[電子郵件1]": "lin.demo@example.com"}
PASSWORD = "正確的密碼 horse-42"


# ---- written now: no password ------------------------------------------------------------

def test_what_was_packed_comes_back_without_a_password():
    assert open_(pack(TABLE)) == TABLE


def test_the_file_is_plain_json_that_says_what_it_is():
    data = pack(TABLE)
    doc = json.loads(data.decode("utf-8"))
    assert doc["format"] == "deid-restore" and doc["v"] == 2 and doc["codes"] == TABLE and doc["created"]
    assert "王小明".encode("utf-8") in data  # not encrypted, and readable as it stands: whoever holds the file holds the data


def test_a_password_given_for_a_file_without_one_is_ignored():
    assert open_(pack(TABLE), "anything at all") == TABLE


def test_an_empty_table_still_makes_a_file():
    assert open_(pack({})) == {}


# ---- written before: encrypted, still opened -----------------------------------------------

def test_a_file_written_with_a_password_still_opens_with_it():
    assert open_(encrypted(TABLE, PASSWORD), PASSWORD) == TABLE


def test_a_file_written_with_a_password_says_that_it_needs_one():
    for nothing in ("", None):
        with pytest.raises(NeedsPassword):
            open_(encrypted(TABLE, PASSWORD), nothing)
    with pytest.raises(NeedsPassword):
        open_(encrypted(TABLE, PASSWORD))


def test_a_wrong_password_and_an_altered_file_give_the_same_error():
    data = encrypted(TABLE, PASSWORD)
    with pytest.raises(WrongKey):
        open_(data, PASSWORD + "x")
    envelope = json.loads(data)
    body = bytearray(base64.b64decode(envelope["data"]))
    body[5] ^= 1
    with pytest.raises(WrongKey):
        open_(json.dumps({**envelope, "data": base64.b64encode(bytes(body)).decode()}).encode(), PASSWORD)
    other_salt = base64.b64encode(b"0" * 16).decode()
    with pytest.raises(WrongKey):
        open_(json.dumps({**envelope, "salt": other_salt}).encode(), PASSWORD)


def test_a_file_asking_for_an_absurd_amount_of_work_is_refused_without_trying():
    for field, value in (("n", 2 ** 30), ("r", 4096), ("p", 512), ("kdf", "none"), ("v", 99)):
        with pytest.raises(NotRestoreFile):
            open_(encrypted(TABLE, PASSWORD, **{field: value}), PASSWORD)


# ---- neither -------------------------------------------------------------------------------

@pytest.mark.parametrize("data", [
    b"", b"hello", b"{}", b"[1, 2]", json.dumps({"format": "something-else"}).encode(), b"\xff\xfe\x00",
    json.dumps({"format": "deid-restore", "v": 2}).encode(),                                # no table
    json.dumps({"format": "deid-restore", "v": 2, "codes": ["[姓名1]"]}).encode(),           # not a table
    json.dumps({"format": "deid-restore", "v": 2, "codes": {"[姓名1]": 7}}).encode(),        # not text
    json.dumps({"format": "deid-restore", "v": 3, "codes": {}}).encode(),                   # a version not known here
])
def test_something_that_is_not_a_restore_file_is_told_apart(data):
    for password in ("", PASSWORD):
        with pytest.raises(NotRestoreFile):
            open_(data, password)
