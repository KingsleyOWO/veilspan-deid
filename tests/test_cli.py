import json
import os
import subprocess
import sys
from pathlib import Path

from tests.builders import make_docx

ROOT = Path(__file__).resolve().parent.parent


def run(args, tmp_path, **env):
    full = {**os.environ, "DEID_FAKE_LLM": "1", "DEID_FAKE_NAMES": "王小明,陳美玲", "DATA_DIR": str(tmp_path / "data"), **env}
    return subprocess.run([sys.executable, "-m", "deid", *args], cwd=ROOT, env=full, capture_output=True, text=True, timeout=300)


def test_cli_writes_masked_file_and_report(tmp_path):
    doc = make_docx(tmp_path / "報告.docx", ["承辦人 王小明", "手機 0912-345-678", "型號 A123-4567"], creator="林志豪")
    out = tmp_path / "out"
    r = run(["run", str(doc), "--out", str(out), "--no-agent"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert (out / "masked_報告.docx").is_file()
    report = json.loads((out / "報告.docx.report.json").read_text(encoding="utf-8"))
    texts = {f["text"] for f in report["findings"] if f["on"]}
    assert {"王小明", "0912-345-678"} <= texts and report["verify"]["ok"] is True
    assert "驗證" in r.stdout


def test_cli_answers_report_counts_without_echoing_the_answers(tmp_path):
    doc = make_docx(tmp_path / "a.docx", ["承辦人 王小明", "窗口 林大同", "型號 A123-4567"])
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"expected": ["王小明", "林大同"], "forbidden": ["A123-4567"]}, ensure_ascii=False), encoding="utf-8")
    r = run(["run", str(doc), "--out", str(tmp_path / "out"), "--no-agent", "--answers", str(answers)], tmp_path)
    assert r.returncode == 0, r.stderr
    assert "1/2" in r.stdout and "林大同" not in r.stdout and "王小明" not in r.stdout
    assert "誤遮 0" in r.stdout


def test_cli_rejects_unsupported_and_broken_files(tmp_path):
    x = tmp_path / "a.pptx"
    x.write_bytes(b"x")
    r = run(["run", str(x), "--out", str(tmp_path / "out")], tmp_path)
    assert r.returncode == 1 and "只支援" in r.stderr
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"nope")
    r = run(["run", str(bad), "--out", str(tmp_path / "out")], tmp_path)
    assert r.returncode == 1 and "Word" in r.stderr


def test_cli_can_mask_chosen_types_in_part(tmp_path):
    from deid.formats.docx import DocxAdapter
    doc = make_docx(tmp_path / "報告.docx", ["承辦人 王小明", "手機 0912-345-678"])
    out = tmp_path / "out"
    r = run(["run", str(doc), "--out", str(out), "--no-agent", "--partial", "姓名"], tmp_path)
    assert r.returncode == 0, r.stderr
    texts = [t.text for t in DocxAdapter().parse(out / "masked_報告.docx").texts]
    assert texts == ["承辦人 王■明", "手機 ************"]  # only the type that was named
    report = json.loads((out / "報告.docx.report.json").read_text(encoding="utf-8"))
    assert {f["text"]: f["style"] for f in report["findings"] if f["on"]}["王小明"] == "partial"
    assert "遮一半：姓名 1" in r.stdout and "王小明" not in r.stdout
    every = run(["run", str(doc), "--out", str(out), "--no-agent", "--partial", "all"], tmp_path)
    assert every.returncode == 0, every.stderr
    assert [t.text for t in DocxAdapter().parse(out / "masked_報告.docx").texts] == ["承辦人 王■明", "手機 0912-***-678"]
    bad = run(["run", str(doc), "--out", str(out), "--no-agent", "--partial", "金融帳號"], tmp_path)
    assert bad.returncode == 1 and "不能遮一半" in bad.stderr


def test_cli_makes_coded_text_and_restores_a_reply(tmp_path):
    doc = make_docx(tmp_path / "報告.docx", ["承辦人 王小明", "手機 0912-345-678"])
    out = tmp_path / "out"
    r = run(["run", str(doc), "--out", str(out), "--no-agent", "--code"], tmp_path)  # no password is asked for
    assert r.returncode == 0, r.stderr
    [coded] = out.glob("代號版_*.txt")
    [key] = out.glob("還原檔_*.deidr")
    assert "報告" not in coded.name and key.stem.split("_")[1] == coded.stem.split("_")[1]  # a pair, named by time only
    text = coded.read_text(encoding="utf-8")
    assert "承辦人 [姓名1]" in text and "王小明" not in text and "0912" not in text
    assert not (out / "masked_報告.docx").exists()
    assert "代號 2" in r.stdout and "王小明" not in r.stdout
    reply = tmp_path / "reply.txt"
    reply.write_text("請 【姓名1】 回電 [電話1]，副本給 [姓名7]", encoding="utf-8")
    back = run(["restore", "--key", str(key), "--in", str(reply), "--out", str(tmp_path / "restored.txt")], tmp_path)
    assert back.returncode == 0, back.stderr
    assert (tmp_path / "restored.txt").read_text(encoding="utf-8") == "請 王小明 回電 0912-345-678，副本給 [姓名7]"
    assert "換回 2 處" in back.stdout and "[姓名7]" in back.stdout and "王小明" not in back.stdout


def test_cli_also_writes_the_document_with_codes_and_takes_it_back_as_a_reply(tmp_path):
    from deid import replyfile
    doc = make_docx(tmp_path / "報告.docx", ["承辦人 王小明", "手機 0912-345-678"])
    out = tmp_path / "out"
    r = run(["run", str(doc), "--out", str(out), "--no-agent", "--code"], tmp_path)
    assert r.returncode == 0, r.stderr
    [coded] = out.glob("代號版_*.docx")
    [key] = out.glob("還原檔_*.deidr")
    assert "報告" not in coded.name and key.stem.split("_")[1] == coded.stem.split("_")[1]
    assert replyfile.read(coded.read_bytes(), "a.docx").text == "承辦人 [姓名1]\n手機 [電話1]"
    assert "代號版檔案：寫入 2 處" in r.stdout and "王小明" not in r.stdout
    back = run(["restore", "--key", str(key), "--in", str(coded), "--out", str(tmp_path / "restored.docx")], tmp_path)
    assert back.returncode == 0, back.stderr
    assert replyfile.read((tmp_path / "restored.docx").read_bytes(), "a.docx").text == "承辦人 王小明\n手機 0912-345-678"


def test_cli_still_restores_with_a_file_that_was_made_with_a_password(tmp_path):
    from tests.builders import make_encrypted_restore_file
    key = tmp_path / "old.deidr"
    key.write_bytes(make_encrypted_restore_file({"[姓名1]": "王小明"}, "made-up password 1"))
    reply = tmp_path / "reply.txt"
    reply.write_text("請 [姓名1] 回電", encoding="utf-8")
    back = run(["restore", "--key", str(key), "--in", str(reply), "--out", str(tmp_path / "restored.txt")],
               tmp_path, DEID_RESTORE_PASSWORD="made-up password 1")
    assert back.returncode == 0, back.stderr
    assert (tmp_path / "restored.txt").read_text(encoding="utf-8") == "請 王小明 回電" and "made-up" not in back.stdout
    wrong = run(["restore", "--key", str(key), "--in", str(reply)], tmp_path, DEID_RESTORE_PASSWORD="another password")
    assert wrong.returncode == 1 and "密碼" in wrong.stderr and "王小明" not in wrong.stdout


def test_cli_restores_a_word_file_the_cloud_handed_back(tmp_path):
    from deid.restorefile import pack
    key = tmp_path / "k.deidr"
    key.write_bytes(pack({"[姓名1]": "王小明", "[電話1]": "0912-345-678"}))
    reply = make_docx(tmp_path / "reply.docx", ["請 [姓名1] 回電", "電話【電話1】"])
    back = run(["restore", "--key", str(key), "--in", str(reply), "--out", str(tmp_path / "restored.txt")], tmp_path)
    assert back.returncode == 0, back.stderr
    assert (tmp_path / "restored.txt").read_text(encoding="utf-8") == "請 王小明 回電\n電話0912-345-678"
    assert "換回 2 處" in back.stdout and "王小明" not in back.stdout
    broken = tmp_path / "broken.docx"
    broken.write_bytes(b"this is not a zip")
    bad = run(["restore", "--key", str(key), "--in", str(broken)], tmp_path)
    assert bad.returncode == 1 and bad.stderr.strip() and "Traceback" not in bad.stderr


def test_cli_writes_the_restored_word_file_when_out_has_the_same_kind(tmp_path):
    from deid import replyfile
    from deid.restorefile import pack
    key = tmp_path / "k.deidr"
    key.write_bytes(pack({"[姓名1]": "王小明"}))
    reply = make_docx(tmp_path / "reply.docx", ["請 [姓名1] 回電"], table=[["姓名"], ["[姓名1]"]])
    out = tmp_path / "restored.docx"
    back = run(["restore", "--key", str(key), "--in", str(reply), "--out", str(out)], tmp_path)
    assert back.returncode == 0, back.stderr
    assert replyfile.read(out.read_bytes(), "restored.docx").text == "請 王小明 回電\n姓名\n王小明"
    assert "換回 2 處" in back.stdout and "王小明" not in back.stdout
    pdf = run(["restore", "--key", str(key), "--in", str(reply), "--out", str(tmp_path / "restored.pdf")], tmp_path)
    assert pdf.returncode == 0 and (tmp_path / "restored.pdf").read_text(encoding="utf-8") == "請 王小明 回電\n姓名\n王小明"  # another kind: the text
