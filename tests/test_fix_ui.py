"""What the page relies on for the 2026-11-24 presentation fixes (result shown again, deleted tracked text in its
own section, the warning about the list). The page itself is plain JavaScript without a build step and is checked
in a browser; these tests pin the server's side of it."""
import io
import re
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deid.jobs import JobStore
from deid.pipeline import build_context
from deid.web.app import create_app
from tests.builders import make_docx
from tests.fakes import keyword_llm

STATIC = Path(__file__).resolve().parent.parent / "deid" / "web" / "static"
NAMES = {"王小明": "姓名", "陳美玲": "姓名"}


@pytest.fixture
def client(settings):
    settings.use_agent = False
    llm = keyword_llm(NAMES)

    def factory(src, workdir, s, progress=None, restore=False):
        return build_context(src, workdir, s, llm=llm, progress=progress, restore=restore)

    store = JobStore(settings, context_factory=factory)
    store.start()
    with TestClient(create_app(settings, store)) as c:
        c.store = store
        yield c
    store.stop()


def plain_docx(tmp_path) -> bytes:
    return make_docx(tmp_path / "p.docx", ["承辦人 王小明 分機 2301", "王小明 再次出現", "一般內容"], header="頁首 王小明").read_bytes()


def tracked_docx(tmp_path) -> bytes:
    """Body: 請聯絡{陳美玲 deleted}先生確認。 / 承辦人王{小明 deleted}先生簽收。 / a paragraph deleted whole."""
    base = make_docx(tmp_path / "b.docx", ["標記甲", "標記乙", "一般內容"], deleted="原聯絡人 陳美玲 0933-222-111").read_bytes()

    def run(t):
        return f'<w:r><w:t xml:space="preserve">{t}</w:t></w:r>'

    def gone(i, t):
        return (f'<w:del w:id="{i}" w:author="修訂者" w:date="2026-01-01T00:00:00Z"><w:r><w:delText xml:space="preserve">{t}'
                "</w:delText></w:r></w:del>")

    swaps = {"標記甲": "<w:p>" + run("請聯絡") + gone(21, "陳美玲") + run("先生確認。") + "</w:p>",
             "標記乙": "<w:p>" + run("承辦人王") + gone(22, "小明") + run("先生簽收。") + "</w:p>"}
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(base)) as zin, zipfile.ZipFile(out, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info)
            if info.filename == "word/document.xml":
                xml = data.decode("utf-8")
                for mark, new in swaps.items():
                    m = re.search(r"<w:p>(?:(?!</w:p>).)*" + mark + r"(?:(?!</w:p>).)*</w:p>", xml, re.S)
                    assert m, mark
                    xml = xml[:m.start()] + new + xml[m.end():]
                data = xml.encode("utf-8")
            zout.writestr(info, data)
    return out.getvalue()


def upload(client, data, mode="mask", name="報告.docx"):
    r = client.post("/api/jobs", files={"file": (name, io.BytesIO(data), "application/octet-stream")}, data={"mode": mode})
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]
    end = time.time() + 20
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("review", "failed"):
            assert job["status"] == "review", job
            return job_id
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def review(client, job_id):
    r = client.get(f"/api/jobs/{job_id}/review")
    assert r.status_code == 200, r.text
    return r.json()


def generate(client, job_id, mode):
    r = client.post(f"/api/jobs/{job_id}/{'apply' if mode == 'mask' else 'export'}")
    assert r.status_code == 200, r.text
    return r.json()


def stored(client, job_id):
    return client.get(f"/api/jobs/{job_id}").json()["result"]


def finding(rev, text):
    return next(f for f in rev["findings"] if f["text"] == text)


# ---- U2: the result is kept on the job, so the page can show it again --------------------------------------------

def test_the_stored_result_after_apply_has_what_the_page_needs_to_show_it_again(client, tmp_path):
    job_id = upload(client, plain_docx(tmp_path))
    assert stored(client, job_id) is None
    answer = generate(client, job_id, "mask")
    result = stored(client, job_id)
    assert result["ok"] is True and result["residue"] == [] and result["problems"] == [] and result["notes"] == []
    assert {"text_spans", "boxes", "meta"} <= set(result["stats"])  # the page writes its sentence from these
    assert {k: result[k] for k in answer} == answer  # nothing the page was told by apply is missing later
    assert review(client, job_id)["job"]["result"] == result  # the review screen's own request carries it too


def test_the_stored_result_after_export_has_what_the_page_needs_to_show_it_again(client, tmp_path):
    job_id = upload(client, plain_docx(tmp_path), "code")
    answer = generate(client, job_id, "code")
    result = stored(client, job_id)
    assert result == {**answer, "made": result["made"]} or result == answer
    assert result["kind"] == "code" and result["ok"] is True
    assert {"codes", "blanked", "chars"} <= set(result["stats"])
    assert result["file"]["ext"] == ".docx" and {"placed", "left"} <= set(result["file"])
    assert isinstance(result["file_notes"], list) and isinstance(result["notes"], list) and isinstance(result["residue"], list)
    assert review(client, job_id)["job"]["result"] == result


@pytest.mark.parametrize("mode", ["mask", "code"])
def test_any_change_to_the_findings_takes_the_stored_result_and_its_files_away(client, tmp_path, mode):
    job_id = upload(client, plain_docx(tmp_path), mode)
    name = finding(review(client, job_id), "王小明")
    download = f"/api/jobs/{job_id}/download" + ("" if mode == "mask" else "?what=text")
    edits = {
        "switch one off": lambda: client.patch(f"/api/jobs/{job_id}/findings/{name['id']}", json={"on": False}),
        "switch a type off": lambda: client.post(f"/api/jobs/{job_id}/findings/bulk", json={"type": "姓名", "on": False}),
        "add text": lambda: client.post(f"/api/jobs/{job_id}/findings", json={"kind": "text", "text": "一般內容", "type": "其他", "all": True}),
    }
    for what, edit in edits.items():
        generate(client, job_id, mode)
        assert stored(client, job_id) is not None and client.get(download).status_code == 200, what
        assert edit().status_code == 200, what
        assert stored(client, job_id) is None, what
        assert client.get(download).status_code == 409, what  # the old file is not offered either
    generate(client, job_id, mode)
    mine = next(f for f in review(client, job_id)["findings"] if f["source"] == "user")
    assert client.delete(f"/api/jobs/{job_id}/findings/{mine['id']}").status_code == 200
    assert stored(client, job_id) is None


@pytest.mark.parametrize("mode", ["mask", "code"])
def test_a_refused_change_leaves_the_stored_result_alone(client, tmp_path, mode):
    """The page forgets the result only when a change was accepted, as the server does."""
    job_id = upload(client, plain_docx(tmp_path), mode)
    name = finding(review(client, job_id), "王小明")
    made = generate(client, job_id, mode)
    assert client.patch(f"/api/jobs/{job_id}/findings/{name['id']}", json={"type": "沒有這個類型"}).status_code == 400
    assert client.patch(f"/api/jobs/{job_id}/findings/nope", json={"on": False}).status_code == 404
    assert client.post(f"/api/jobs/{job_id}/findings", json={"kind": "text", "text": "不在文件裡的字", "type": "其他"}).status_code == 422
    assert stored(client, job_id)["ok"] is made["ok"]


# ---- U4: deleted tracked-change text ------------------------------------------------------------------------------

def test_the_review_gives_deleted_text_as_ranges_inside_the_unit_text(client, tmp_path):
    job_id = upload(client, tracked_docx(tmp_path))
    rev = review(client, job_id)
    blocks = {b["unit"]: b for b in rev["view"] if b["type"] == "text"}
    shown = {uid: [rev["units"][uid]["text"][s:e] for s, e in b["del"]] for uid, b in blocks.items() if b.get("del")}
    assert sorted(shown.values()) == [["原聯絡人 陳美玲 0933-222-111"], ["小明"], ["陳美玲"]]
    # the body text with the deleted ranges taken out is what a reader of the document sees
    kept = {}
    for uid, b in blocks.items():
        text = rev["units"][uid]["text"]
        for s, e in reversed(b.get("del", [])):
            text = text[:s] + text[e:]
        kept[uid] = text
    assert "請聯絡先生確認。" in kept.values() and "承辦人王先生簽收。" in kept.values() and "" in kept.values()
    assert all(b["section"] == "內文" for b in blocks.values() if b.get("del"))


def test_text_picked_inside_or_across_deleted_text_can_be_added_by_position(client, tmp_path):
    """The page sends the position in the unit's text and the text found there, also for a pick across a deletion."""
    job_id = upload(client, tracked_docx(tmp_path))
    rev = review(client, job_id)
    uid = next(u for u, t in rev["units"].items() if t["text"] == "請聯絡陳美玲先生確認。")
    ask = lambda text, start, end: client.post(f"/api/jobs/{job_id}/findings", json={  # noqa: E731
        "kind": "text", "text": text, "type": "其他", "all": False, "unit": uid, "start": start, "end": end})
    r = ask("美玲", 4, 6)  # inside the deleted 陳美玲
    assert r.status_code == 200 and [(o["unit"], o["start"], o["end"]) for o in r.json()["occurrences"]] == [(uid, 4, 6)]
    r = ask("聯絡陳美玲先生", 1, 8)  # across it: the text includes what was deleted
    assert r.status_code == 200 and r.json()["text"] == "聯絡陳美玲先生"
    assert ask("聯絡先生", 1, 8).status_code == 422  # the text as shown without the deletion is not what is there
    assert ask("確認", 8, 10).status_code == 200  # after the deletion
    assert {f["text"] for f in review(client, job_id)["findings"] if f["source"] == "user"} == {"美玲", "聯絡陳美玲先生", "確認"}


# ---- U1 and the page's ids ----------------------------------------------------------------------------------------

def test_the_code_functions_result_warns_that_the_list_holds_personal_data():
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    hints = dict(re.findall(r"^  (mask|code): '([^']*)',$", script, re.M))
    assert set(hints) == {"mask", "code"}
    for mode, text in hints.items():
        assert "遮蔽清單" in text and "含有個資" in text and "妥善保管" in text, mode


def test_every_element_the_script_looks_up_exists_in_the_page():
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    ids = set(re.findall(r"""\$\('([\w-]+)'\)""", script))
    for group in re.findall(r"for \(const (?:\[dropId, fileId, mode\]|id|\[id, items\]) of (\[.*\])\)", script):  # ids named in a loop
        ids |= set(re.findall(r"'([a-z][\w-]*)'", group)) - {"mask", "code"}
    missing = sorted(i for i in ids if f'id="{i}"' not in page)
    assert missing == []
    assert 'id="go-home"' in page and 'id="btn-result"' in page
