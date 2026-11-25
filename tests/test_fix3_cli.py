"""Third tier of the review of 2026-11-23, the command line and the example settings: what the service-side
fixes left for the files around them. A setting that cannot be read ends the command with its message; the
example settings file reads when its lines are switched on; a place the program cannot mask is still printed
now that it is no longer a warning as well. All names are made up."""
import os
import re
import subprocess
import sys
from pathlib import Path

from deid import cli
from deid.config import load_settings
from tests.builders import make_docx

ROOT = Path(__file__).resolve().parent.parent


def test_a_setting_that_cannot_be_read_ends_the_command_with_its_message(tmp_path):
    (tmp_path / ".env").write_text("USE_AGENT=ture\nMAX_UPLOAD_MB=200  # 上限\n", encoding="utf-8")
    doc = make_docx(tmp_path / "a.docx", ["承辦人 王小明"])
    env = {**os.environ, "PYTHONPATH": str(ROOT), "DEID_FAKE_LLM": "1", "DATA_DIR": str(tmp_path / "data")}
    env.pop("USE_AGENT", None)
    env.pop("MAX_UPLOAD_MB", None)
    for command in (["run", str(doc), "--out", str(tmp_path / "out"), "--no-agent"], ["serve"]):
        r = subprocess.run([sys.executable, "-m", "deid", *command], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 1, (command, r.stderr)
        assert "USE_AGENT" in r.stderr and "MAX_UPLOAD_MB" in r.stderr and "Traceback" not in r.stderr


def test_the_example_settings_file_reads_when_its_lines_are_switched_on(tmp_path):
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    on = re.sub(r"(?m)^#\s*(?=[A-Z_]+=)", "", example)  # every setting the file shows commented out
    assert on != example and len(re.findall(r"(?m)^[A-Z_]+=", on)) >= 20
    (tmp_path / "x.env").write_text(on, encoding="utf-8")
    settings = load_settings(tmp_path / "x.env", environ={})
    assert settings.pdf_dpi == 200 and settings.chunk_chars == 2500 and settings.job_timeout_min == 180


def test_the_command_line_prints_a_place_the_program_cannot_mask(tmp_path, monkeypatch, capsys):
    real = cli.analyze

    def analyze(ctx, **kw):
        real(ctx, **kw)
        ctx.unmaskable.append({"where": "內嵌的 OLE 物件 oleObject1.bin", "text": "王小明", "reason": "程式改不到"})

    monkeypatch.setattr(cli, "analyze", analyze)
    monkeypatch.setenv("DEID_FAKE_LLM", "1")
    monkeypatch.setenv("DEID_FAKE_NAMES", "王小明")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    doc = make_docx(tmp_path / "a.docx", ["承辦人 王小明"])
    assert cli.main(["run", str(doc), "--out", str(tmp_path / "out"), "--no-agent"]) == 0
    out = capsys.readouterr().out
    assert "無法自動遮蔽，需人工處理：內嵌的 OLE 物件 oleObject1.bin（程式改不到）" in out
    assert "王小明" not in out  # the string itself is in the report file, not on the terminal
