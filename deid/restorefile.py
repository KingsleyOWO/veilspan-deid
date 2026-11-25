"""The restore file: the table that turns codes back into what they stand for.

Since 2026-11-19 the file is written as plain JSON, without a password (the owner's decision: the password
was a step in the way). The table is complete personal data, so whoever holds the file holds the data; the
screens and the README say so. The server still keeps nothing: the file lives in the job folder like the
upload itself and is deleted with it.

Files written before that date are encrypted with a password (scrypt to turn the password into a key,
AES-256-GCM to encrypt and to detect any change, both from the `cryptography` package). They are still
opened here; nothing writes that format any more. To write it again, see `git show e0360f4:deid/restorefile.py`."""
import base64
import binascii
import codecs
import json
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

FORMAT = "deid-restore"
PLAIN, ENCRYPTED = 2, 1  # the versions of the file: written now, and written until 2026-11-19
SCRYPT_R, SCRYPT_P = 8, 1
MAX_N = 2 ** 17  # what an encrypted file may ask this server to spend on opening it


class NotRestoreFile(ValueError):
    """The data is not a restore file this program wrote."""


class NeedsPassword(ValueError):
    """The file was written with a password and none was given."""


class WrongKey(ValueError):
    """The password is wrong, or the file was changed after it was written; the two cannot be told apart."""


def pack(table: dict[str, str]) -> bytes:
    """The file as it is written now: readable JSON, no password."""
    doc = {"format": FORMAT, "v": PLAIN, "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "codes": table}
    return json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8")


def _table(codes) -> dict[str, str]:
    if not isinstance(codes, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in codes.items()):
        raise NotRestoreFile("not a restore file")
    return codes


def _key(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(password.encode("utf-8"))


def _bound(header: dict) -> bytes:
    """The readable part of an encrypted file, tied to the encrypted part so that changing either is noticed."""
    return json.dumps(header, sort_keys=True, separators=(",", ":")).encode("ascii")


def _text(data: bytes) -> str:
    """The file as text. It is written as UTF-8; opened and saved again in Notepad it gains a byte-order mark, or
    becomes UTF-16 with one (Notepad's "Unicode"). Anything else that does not decode is no restore file."""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def open_(data: bytes, password: str | None = None) -> dict[str, str]:
    """The table of either kind of file. A password is only looked at for a file that was written with one."""
    try:
        envelope = json.loads(_text(data))
        if not isinstance(envelope, dict) or envelope.get("format") != FORMAT:
            raise ValueError
        if envelope.get("v") == PLAIN:
            return _table(envelope.get("codes"))
        header = {k: envelope[k] for k in ("format", "v", "kdf", "n", "r", "p", "salt", "nonce")}
        n, r, p = header["n"], header["r"], header["p"]
        # type() and not ==: true == 1 in Python, and a version written as true is no version this program wrote
        if header["v"] != ENCRYPTED or header["kdf"] != "scrypt" or not all(type(x) is int
                                                                             for x in (header["v"], n, r, p)):
            raise ValueError
        if not (2 ** 14 <= n <= MAX_N and n & (n - 1) == 0 and r == SCRYPT_R and p == SCRYPT_P):
            raise ValueError
        salt = base64.b64decode(header["salt"], validate=True)
        nonce = base64.b64decode(header["nonce"], validate=True)
        sealed = base64.b64decode(envelope["data"], validate=True)
        if len(nonce) != 12 or not 8 <= len(salt) <= 64:
            raise ValueError
    except (ValueError, KeyError, TypeError, UnicodeDecodeError, binascii.Error):
        raise NotRestoreFile("not a restore file") from None
    if not password:
        raise NeedsPassword("this file was written with a password")
    try:
        body = AESGCM(_key(password, salt, n, r, p)).decrypt(nonce, sealed, _bound(header))
    except InvalidTag:
        raise WrongKey("wrong password or altered file") from None
    return _table(json.loads(body.decode("utf-8")).get("codes"))
