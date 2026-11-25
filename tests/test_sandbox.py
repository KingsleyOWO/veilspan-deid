import pytest

from deid.agent import sandbox
from deid.config import load_settings
from tests.builders import make_docx

pytestmark = pytest.mark.docker


@pytest.fixture
def env(tmp_path):
    settings = load_settings(tmp_path / "none.env", environ={})
    return settings, make_docx(tmp_path / "a.docx", ["內文 王小明"], embedded_xlsx_strings=["陳美玲"])


def test_script_can_read_the_uploaded_file(env):
    settings, doc = env
    assert sandbox.available(settings)
    doc.chmod(0o600)
    res = sandbox.run_python(settings, doc, "print(open('/work/input.docx','rb').read(2))", workdir=doc.parent)
    assert res["ok"] and res["output"].strip() == "b'PK'" and res["timed_out"] is False
    assert doc.stat().st_mode & 0o777 == 0o600
    assert not list(doc.parent.glob("sandbox-*"))


def test_script_can_inspect_an_embedded_workbook(env):
    settings, doc = env
    code = ("import zipfile, io\nz = zipfile.ZipFile('/work/input.docx')\n"
            "inner = zipfile.ZipFile(io.BytesIO(z.read('word/embeddings/Workbook1.xlsx')))\n"
            "print(inner.read('xl/sharedStrings.xml').decode('utf-8'))")
    res = sandbox.run_python(settings, doc, code)
    assert res["ok"] and "陳美玲" in res["output"]


def test_script_has_no_network(env):
    settings, doc = env
    code = "import socket\nprint(sorted(name for _, name in socket.if_nameindex()))"
    res = sandbox.run_python(settings, doc, code)
    assert res["ok"] and res["output"].strip() == "['lo']"


def test_script_cannot_write_to_the_input_or_the_file_system(env):
    settings, doc = env
    before = doc.read_bytes()
    code = ("for p in ('/work/input.docx', '/work/x', '/usr/x', '/x'):\n"
            "    try:\n        open(p, 'w').write('x'); print('WROTE', p)\n    except OSError:\n        print('denied', p)\n"
            "import os; print('uid', os.getuid())")
    res = sandbox.run_python(settings, doc, code)
    assert "WROTE" not in res["output"] and res["output"].count("denied") == 4 and "uid 65534" in res["output"]
    assert doc.read_bytes() == before


def test_runaway_script_is_stopped(env):
    settings, doc = env
    res = sandbox.run_python(settings, doc, "while True:\n    pass", timeout=5)
    assert res["timed_out"] is True and res["ok"] is False


def test_long_output_is_truncated(env):
    settings, doc = env
    res = sandbox.run_python(settings, doc, "print('x' * 50000)")
    assert len(res["output"]) == sandbox.OUTPUT_LIMIT and res["truncated"] is True


def test_error_output_comes_back_to_the_model(env):
    settings, doc = env
    res = sandbox.run_python(settings, doc, "raise ValueError('boom')")
    assert res["ok"] is False and "ValueError: boom" in res["output"]
