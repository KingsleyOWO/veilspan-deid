import base64
import io
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from deid.formats.base import adapter_for
from deid.jobs import JobStore
from deid.pipeline import build_context
from deid.web.app import create_app
from tests.builders import make_docx, make_pdf, text_image
from tests.fakes import FakeFaces, FakeOcr, keyword_llm, ocr_line

NAMES = {"王小明": "姓名"}


@pytest.fixture
def client(settings):
    settings.use_agent = False
    llm = keyword_llm(NAMES)

    def factory(src, workdir, s, progress=None, restore=False):
        return build_context(src, workdir, s, llm=llm, progress=progress, restore=restore,
                             ocr=FakeOcr([ocr_line("登入者：王小明")]), faces=FakeFaces([(300.0, 20.0, 380.0, 120.0)]))

    store = JobStore(settings, context_factory=factory)
    store.start()
    with TestClient(create_app(settings, store)) as c:
        c.store = store
        yield c
    store.stop()


@pytest.fixture
def docx_bytes(tmp_path):
    return make_docx(tmp_path / "s.docx", ["承辦人 王小明 分機 2301", "王小明 再次出現", "一般內容"], header="頁首 王小明",
                     images=[("image1.png", text_image(["x"], size=(400, 200)))], creator="林志豪").read_bytes()


def upload(client, data, name="報告.docx"):
    r = client.post("/api/jobs", files={"file": (name, io.BytesIO(data), "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def ready(client, job_id, want="review"):
    end = time.time() + 20
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("review", "failed"):
            assert job["status"] == want, job
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def review(client, job_id):
    r = client.get(f"/api/jobs/{job_id}/review")
    assert r.status_code == 200, r.text
    return r.json()


def finding(rev, text=None, type=None):
    return next(f for f in rev["findings"] if (text is None or f["text"] == text) and (type is None or f["type"] == type))


def test_upload_then_review_returns_view_units_images_and_findings(client, docx_bytes):
    job_id = upload(client, docx_bytes)
    job = ready(client, job_id)
    assert job["filename"] == "報告.docx" and "created" in job and "ext" in job
    rev = review(client, job_id)
    assert rev["kind"] == "docx" and rev["view"][0]["type"] == "text"
    assert rev["units"]["t1"] == {"text": "承辦人 王小明 分機 2301", "section": "內文"}
    img = rev["images"]["img1"]
    assert (img["width"], img["height"], img["supported"]) == (400, 200, True) and img["url"].endswith("/assets/img1.png")
    assert client.get(img["url"]).content[:4] == b"\x89PNG"
    name = finding(rev, "王小明")
    assert name["on"] and {o["kind"] for o in name["occurrences"]} == {"text", "box"}
    assert finding(rev, type="人臉")["source"] == "face" and finding(rev, type="檔案屬性")["text"] == "林志豪"
    assert "姓名" in rev["types"] and "人臉" in rev["types"] and rev["summary"]["on"]["姓名"] == 1
    assert rev["outcome"] == {"agent": False} and rev["warnings"] == [] and rev["log"] == []


def test_review_is_not_available_before_processing_ends(client, docx_bytes):
    job_id = upload(client, b"not a zip", "bad.docx")
    ready(client, job_id, want="failed")
    assert client.get(f"/api/jobs/{job_id}/review").status_code == 409


def test_unsupported_and_oversized_uploads_get_readable_errors(client, settings):
    r = client.post("/api/jobs", files={"file": ("a.pptx", io.BytesIO(b"x"), "application/octet-stream")})
    assert r.status_code == 400 and r.json()["error"] == "目前只支援 .docx、.pdf、.xlsx、.xlsm"
    settings.max_upload_mb = 1
    r = client.post("/api/jobs", files={"file": ("a.docx", io.BytesIO(b"x" * (1024 * 1024 + 5)), "application/octet-stream")})
    assert r.status_code == 413 and "1 MB" in r.json()["error"]


def test_switching_findings_and_single_occurrences(client, docx_bytes):
    job_id = upload(client, docx_bytes)
    ready(client, job_id)
    name = finding(review(client, job_id), "王小明")
    r = client.patch(f"/api/jobs/{job_id}/findings/{name['id']}", json={"occurrence": {"index": 0, "on": False}})
    assert r.status_code == 200 and r.json()["occurrences"][0]["on"] is False and r.json()["on"] is True
    r = client.patch(f"/api/jobs/{job_id}/findings/{name['id']}", json={"on": False, "type": "其他"})
    assert (r.json()["on"], r.json()["type"]) == (False, "其他")
    assert finding(review(client, job_id), "王小明")["on"] is False
    assert client.patch(f"/api/jobs/{job_id}/findings/f999", json={"on": True}).status_code == 404
    assert client.patch(f"/api/jobs/{job_id}/findings/{name['id']}", json={"type": "不存在的類型"}).status_code == 400
    assert client.patch(f"/api/jobs/{job_id}/findings/{name['id']}", json={"occurrence": {"index": 99, "on": False}}).status_code == 400


def test_bulk_switch_by_type(client, docx_bytes):
    job_id = upload(client, docx_bytes)
    ready(client, job_id)
    r = client.post(f"/api/jobs/{job_id}/findings/bulk", json={"type": "人臉", "on": False})
    assert r.json() == {"changed": 1}
    assert finding(review(client, job_id), type="人臉")["on"] is False


def test_manual_text_everywhere_and_single_place(client, docx_bytes):
    job_id = upload(client, docx_bytes)
    ready(client, job_id)
    r = client.post(f"/api/jobs/{job_id}/findings", json={"kind": "text", "text": "承辦人", "type": "其他", "all": True})
    assert r.status_code == 200 and r.json()["source"] == "user" and len(r.json()["occurrences"]) == 1
    r = client.post(f"/api/jobs/{job_id}/findings", json={"kind": "text", "text": "2301", "type": "其他識別碼", "all": False,
                                                           "unit": "t1", "start": 11, "end": 15})
    assert r.status_code == 200 and [(o["unit"], o["start"], o["end"]) for o in r.json()["occurrences"]] == [("t1", 11, 15)]
    r = client.post(f"/api/jobs/{job_id}/findings", json={"kind": "text", "text": "不存在的字", "type": "其他", "all": True})
    assert r.status_code == 422 and r.json()["error"] == "文件中找不到這段文字"
    r = client.post(f"/api/jobs/{job_id}/findings", json={"kind": "text", "text": "2301", "type": "其他", "all": False,
                                                           "unit": "t1", "start": 0, "end": 4})
    assert r.status_code == 422


def test_manual_region_and_removal_rules(client, docx_bytes):
    job_id = upload(client, docx_bytes)
    ready(client, job_id)
    r = client.post(f"/api/jobs/{job_id}/findings", json={"kind": "region", "image": "img1", "box": [10, 10, 120, 60], "type": "其他"})
    assert r.status_code == 200 and r.json()["kind"] == "region" and r.json()["occurrences"][0]["box"] == [10, 10, 120, 60]
    mine = r.json()["id"]
    assert client.post(f"/api/jobs/{job_id}/findings", json={"kind": "region", "image": "img9", "box": [0, 0, 5, 5], "type": "其他"}).status_code == 422
    assert client.post(f"/api/jobs/{job_id}/findings", json={"kind": "region", "image": "img1", "box": [5, 5, 6, 6], "type": "其他"}).status_code == 422
    r = client.post(f"/api/jobs/{job_id}/findings", json={"kind": "region", "image": "img1", "box": [350, 150, 900, 900], "type": "其他"})
    assert r.json()["occurrences"][0]["box"] == [350, 150, 400, 200]
    assert client.delete(f"/api/jobs/{job_id}/findings/{mine}").status_code == 200
    model = finding(review(client, job_id), "王小明")["id"]
    assert client.delete(f"/api/jobs/{job_id}/findings/{model}").status_code == 400
    assert not any(f["id"] == mine for f in review(client, job_id)["findings"])


def test_apply_then_download_gives_the_masked_file_with_a_safe_filename_header(client, docx_bytes, tmp_path):
    job_id = upload(client, docx_bytes, "測試「報告」(一).docx")
    ready(client, job_id)
    assert client.get(f"/api/jobs/{job_id}/download").status_code == 409  # nothing generated yet
    r = client.post(f"/api/jobs/{job_id}/apply")
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["residue"] == [] and r.json()["stats"]["text_spans"] >= 3
    r = client.get(f"/api/jobs/{job_id}/download")
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    cd.encode("latin-1")
    assert cd.startswith('attachment; filename="masked.docx"; filename*=UTF-8\'\'masked_%E6%B8%AC%E8%A9%A6')
    assert "%E3%80%8C" in cd and "%28" in cd and '"' not in cd.split("filename*=")[1]
    out = tmp_path / "dl.docx"
    out.write_bytes(r.content)
    assert not any("王小明" in t.text for t in adapter_for(out).parse(out).texts)
    assert client.get(f"/api/jobs/{job_id}").json()["downloaded_at"] is not None


def test_editing_after_apply_requires_generating_again(client, docx_bytes):
    job_id = upload(client, docx_bytes)
    ready(client, job_id)
    client.post(f"/api/jobs/{job_id}/apply")
    name = finding(review(client, job_id), "王小明")
    client.patch(f"/api/jobs/{job_id}/findings/{name['id']}", json={"on": False})
    assert client.get(f"/api/jobs/{job_id}/download").status_code == 409
    assert client.get(f"/api/jobs/{job_id}").json()["result"] is None


def test_download_with_residue_needs_explicit_acknowledgement(client, docx_bytes):
    job_id = upload(client, docx_bytes)
    ready(client, job_id)
    client.post(f"/api/jobs/{job_id}/apply")
    client.store._update(job_id, result={"ok": False, "residue": ["customXml/item1.xml: 「x」"], "problems": [], "stats": {}})
    r = client.get(f"/api/jobs/{job_id}/download")
    assert r.status_code == 409 and r.json()["needs_ack"] is True
    assert client.get(f"/api/jobs/{job_id}/download?ack=1").status_code == 200


def test_report_lists_what_was_masked(client, docx_bytes):
    job_id = upload(client, docx_bytes)
    ready(client, job_id)
    client.post(f"/api/jobs/{job_id}/apply")
    r = client.get(f"/api/jobs/{job_id}/report")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    data = r.json()
    assert any(f["text"] == "王小明" and f["on"] for f in data["findings"]) and data["verify"]["ok"] is True


def test_unknown_ids_and_path_tricks_are_not_found(client, docx_bytes):
    job_id = upload(client, docx_bytes)
    ready(client, job_id)
    for url in ("/api/jobs/doesnotexist1234567890", "/api/jobs/doesnotexist1234567890/review",
                f"/api/jobs/{job_id}/assets/..%2Fjob.json", f"/api/jobs/{job_id}/assets/job.json",
                f"/api/jobs/{job_id}/assets/img1.png%00.json", f"/api/jobs/{job_id}/assets/img999.png"):
        assert client.get(url).status_code in (404, 422), url
    assert client.get("/api/jobs").status_code in (404, 405)


def test_retry_and_delete(client, docx_bytes):
    bad = upload(client, b"not a zip", "bad.docx")
    ready(client, bad, want="failed")
    assert client.post(f"/api/jobs/{bad}/retry").status_code == 200
    good = upload(client, docx_bytes)
    ready(client, good)
    assert client.post(f"/api/jobs/{good}/retry").status_code == 409
    assert client.delete(f"/api/jobs/{good}").status_code == 200
    assert client.get(f"/api/jobs/{good}").status_code == 404


def test_responses_are_not_cached_and_carry_a_strict_content_policy(client, docx_bytes):
    job_id = upload(client, docx_bytes)
    r = client.get(f"/api/jobs/{job_id}")
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
    page = client.get("/")
    assert "default-src 'self'" in page.headers["content-security-policy"]


def test_pdf_review_gives_pages_and_boxes_for_text_findings(client, tmp_path):
    data = make_pdf(tmp_path / "p.pdf", [{"lines": ["聯絡人 王小明 先生"]}]).read_bytes()
    job_id = upload(client, data, "p.pdf")
    ready(client, job_id)
    rev = review(client, job_id)
    assert rev["kind"] == "pdf" and rev["view"][0] == {"type": "page", "image": "pg1", "page": 1}
    occ = finding(rev, "王小明")["occurrences"][0]
    assert occ["kind"] == "text" and occ["page_boxes"][0][0] == "pg1" and len(occ["page_boxes"][0][1]) == 4
    r = client.post(f"/api/jobs/{job_id}/findings", json={"kind": "region", "image": "pg1", "box": [100, 100, 400, 200], "type": "其他"})
    assert r.status_code == 200
    assert client.post(f"/api/jobs/{job_id}/apply").json()["ok"] is True


def test_front_end_is_served_and_loads_nothing_from_outside(client):
    import re
    page = client.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert 'src="/static/app.js"' in page.text and 'href="/static/style.css"' in page.text
    assert "<script>" not in page.text and " onclick=" not in page.text  # the content policy forbids inline script
    for path in ("/", "/static/app.js", "/static/style.css"):
        body = client.get(path)
        assert body.status_code == 200, path
        refs = re.findall(r"""(?:src|href|url\(|import|fetch\()\s*=?\s*['"(]?\s*(https?:)?//""", body.text)
        assert refs == [], f"{path} refers to an outside address"
        assert "innerHTML" not in body.text, f"{path} must build the page with textContent"


def test_agent_strings_inside_an_already_masked_address_are_not_listed_until_the_address_is_switched_off(client, tmp_path):
    data = make_docx(tmp_path / "m.docx", ["信箱 alice0609@example.com", "一般內容"]).read_bytes()
    job_id = upload(client, data)
    ready(client, job_id)
    ctx = client.store.context(job_id)
    part = ctx.store.add_text("alice0609", "其他", "agent", reason="帳號")
    client.store.save_findings(job_id, ctx.store)

    rev = review(client, job_id)
    mail = finding(rev, text="alice0609@example.com")
    assert mail["on"] and [f for f in rev["findings"] if f["text"] == "alice0609"] == []
    assert sum(rev["summary"]["on"].values()) == sum(1 for f in rev["findings"] if f["on"])
    report = client.get(f"/api/jobs/{job_id}/report").json()
    assert [f["text"] for f in report["findings"] if "alice" in f["text"]] == ["alice0609@example.com"]

    off = client.patch(f"/api/jobs/{job_id}/findings/{mail['id']}", json={"on": False}).json()
    assert off["relist"] is True  # the page fetches the list again
    assert finding(review(client, job_id), text="alice0609")["id"] == part.id
    back = client.patch(f"/api/jobs/{job_id}/findings/{mail['id']}", json={"on": True}).json()
    assert back["relist"] is True and [f for f in review(client, job_id)["findings"] if f["text"] == "alice0609"] == []
    plain = client.patch(f"/api/jobs/{job_id}/findings/{mail['id']}", json={"type": "其他"}).json()
    assert "relist" not in plain


# ---- partial masking ---------------------------------------------------------------------------

def test_review_says_how_each_entry_will_look_and_which_types_can_be_partial(client, docx_bytes):
    job_id = upload(client, docx_bytes)
    ready(client, job_id)
    rev = review(client, job_id)
    name = finding(rev, "王小明")
    assert (name["style"], name["preview"], name["partial_ok"], name["fits"]) == ("full", "■■■", True, True)
    assert "姓名" in rev["partial_types"] and "金融帳號" not in rev["partial_types"]
    assert rev["styles"] == {} and rev["masks"] is None  # nothing is partial yet: every marked place is masked whole
    face = finding(rev, type="人臉")
    assert face["partial_ok"] is False and face["preview"] == ""


def test_one_entry_can_be_switched_to_partial_and_the_download_follows(client, docx_bytes, tmp_path):
    job_id = upload(client, docx_bytes)
    ready(client, job_id)
    name = finding(review(client, job_id), "王小明")
    r = client.patch(f"/api/jobs/{job_id}/findings/{name['id']}", json={"style": "partial"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["style"], body["preview"]) == ("partial", "王■明")
    assert body["masks"]["t1"] == [[5, 6]]  # which characters of each unit are really masked now
    rev = review(client, job_id)
    assert rev["masks"]["t1"] == [[5, 6]] and rev["styles"] == {}
    assert client.post(f"/api/jobs/{job_id}/apply").json()["ok"] is True
    out = tmp_path / "out.docx"
    out.write_bytes(client.get(f"/api/jobs/{job_id}/download").content)
    texts = [t.text for t in adapter_for(out).parse(out).texts]
    assert "承辦人 王■明 分機 2301" in texts and "頁首 王■明" in texts
    report = client.get(f"/api/jobs/{job_id}/report").json()
    assert [(f["style"], f["masked"]) for f in report["findings"] if f["text"] == "王小明"] == [("partial", "王■明")]
    back = client.patch(f"/api/jobs/{job_id}/findings/{name['id']}", json={"style": "full"}).json()
    assert back["preview"] == "■■■" and back["masks"] is None


def test_a_whole_type_can_be_switched_to_partial_and_back(client, docx_bytes):
    job_id = upload(client, docx_bytes)
    ready(client, job_id)
    r = client.post(f"/api/jobs/{job_id}/findings/bulk", json={"type": "姓名", "style": "partial"})
    assert r.status_code == 200 and r.json()["changed"] == 1
    rev = review(client, job_id)
    assert rev["styles"] == {"姓名": "partial"} and finding(rev, "王小明")["style"] == "partial"
    added = client.post(f"/api/jobs/{job_id}/findings", json={"kind": "text", "text": "承辦人", "type": "姓名"}).json()
    assert added["style"] == "partial" and added["preview"] == "承■人"  # entries added later follow the type
    assert client.post(f"/api/jobs/{job_id}/findings/bulk", json={"type": "姓名", "style": "full"}).json()["changed"] == 2
    assert review(client, job_id)["masks"] is None


def test_styles_that_do_not_apply_are_refused(client, docx_bytes):
    job_id = upload(client, docx_bytes)
    ready(client, job_id)
    rev = review(client, job_id)
    face, name = finding(rev, type="人臉"), finding(rev, "王小明")
    assert client.patch(f"/api/jobs/{job_id}/findings/{face['id']}", json={"style": "partial"}).status_code == 400
    assert client.patch(f"/api/jobs/{job_id}/findings/{name['id']}", json={"style": "half"}).status_code == 400
    assert client.post(f"/api/jobs/{job_id}/findings/bulk", json={"type": "人臉", "style": "partial"}).status_code == 400
    assert client.post(f"/api/jobs/{job_id}/findings/bulk", json={"style": "partial"}).status_code == 400
    assert client.post(f"/api/jobs/{job_id}/findings/bulk", json={"type": "姓名"}).status_code == 400


# ---- the code function and restoring -------------------------------------------------------------

PASSWORD = "一組夠長的密碼 42"


# ---- custom masking: the reviewer picks the characters -------------------------------------------

def test_an_entry_and_a_file_property_can_be_masked_as_picked_and_the_download_follows(client, docx_bytes, tmp_path):
    job_id = upload(client, docx_bytes)
    ready(client, job_id)
    rev = review(client, job_id)
    name, author, face = finding(rev, "王小明"), finding(rev, "林志豪"), finding(rev, type="人臉")
    assert (name["custom_ok"], author["custom_ok"], face["custom_ok"], name["spans"]) == (True, True, False, [])
    assert author["kind"] == "meta"
    start = client.patch(f"/api/jobs/{job_id}/findings/{name['id']}", json={"style": "custom"}).json()
    assert (start["style"], start["spans"], start["preview"]) == ("custom", [[0, 3]], "■■■")  # as it was masked
    r = client.patch(f"/api/jobs/{job_id}/findings/{name['id']}", json={"spans": [[0, 1]]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["style"], body["spans"], body["preview"]) == ("custom", [[0, 1]], "■小明")
    assert body["masks"]["t1"] == [[4, 5]]  # the page redraws which characters stay readable
    prop = client.patch(f"/api/jobs/{job_id}/findings/{author['id']}", json={"spans": [[1, 3]]}).json()
    assert (prop["style"], prop["preview"]) == ("custom", "林■■")
    assert client.post(f"/api/jobs/{job_id}/apply").json()["ok"] is True
    out = tmp_path / "out.docx"
    out.write_bytes(client.get(f"/api/jobs/{job_id}/download").content)
    after = adapter_for(out).parse(out)
    texts = [t.text for t in after.texts]
    assert "承辦人 ■小明 分機 2301" in texts and "頁首 ■小明" in texts
    assert [m.value for m in after.metas if m.label == "作者"] == ["林■■"]
    report = client.get(f"/api/jobs/{job_id}/report").json()
    assert [(f["style"], f["masked"]) for f in report["findings"] if f["text"] == "王小明"] == [("custom", "■小明")]


def test_custom_ranges_that_mask_nothing_or_do_not_fit_are_refused_and_change_nothing(client, docx_bytes):
    job_id = upload(client, docx_bytes)
    ready(client, job_id)
    rev = review(client, job_id)
    name, face = finding(rev, "王小明"), finding(rev, type="人臉")
    url = f"/api/jobs/{job_id}/findings/{name['id']}"
    assert client.patch(url, json={"spans": [[1, 2]]}).status_code == 200
    for bad in ([], [[0, 9]], [[2, 1]], "x", [["a", "b"]], None):
        r = client.patch(url, json={"spans": bad})
        assert r.status_code == 400 and "遮" in r.json()["error"], (bad, r.text)
    assert client.patch(f"/api/jobs/{job_id}/findings/{face['id']}", json={"spans": [[0, 1]]}).status_code == 400
    assert client.patch(f"/api/jobs/{job_id}/findings/{face['id']}", json={"style": "custom"}).status_code == 400
    assert finding(review(client, job_id), "王小明")["spans"] == [[1, 2]]


def upload_for_codes(client, data, name="報告.docx"):
    r = client.post("/api/jobs", files={"file": (name, io.BytesIO(data), "application/octet-stream")}, data={"mode": "code"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_upload_can_choose_the_function_and_refuses_one_it_does_not_know(client, docx_bytes):
    assert ready(client, upload(client, docx_bytes))["mode"] == "mask"
    assert ready(client, upload_for_codes(client, docx_bytes))["mode"] == "code"
    r = client.post("/api/jobs", files={"file": ("a.docx", io.BytesIO(docx_bytes), "application/octet-stream")}, data={"mode": "x"})
    assert r.status_code == 400


def test_coded_text_and_restore_file_are_made_and_downloaded(client, docx_bytes):
    from deid.restorefile import open_
    job_id = upload_for_codes(client, docx_bytes)
    ready(client, job_id)
    assert client.get(f"/api/jobs/{job_id}/download?what=text").status_code == 409  # nothing made yet
    r = client.post(f"/api/jobs/{job_id}/export")  # no password is asked for
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["stats"]["codes"] >= 1 and body["residue"] == []
    text = client.get(f"/api/jobs/{job_id}/download?what=text")
    assert text.status_code == 200 and text.headers["content-type"].startswith("text/plain; charset=utf-8")
    # The name of what goes to the cloud says nothing about the upload: file names often hold a person's name.
    from urllib.parse import unquote
    import re
    name = unquote(text.headers["content-disposition"].split("filename*=UTF-8\'\'")[1])
    assert re.fullmatch(r"代號版_\d{8}-\d{6}\.txt", name) and "報告" not in text.headers["content-disposition"]
    content = text.content.decode("utf-8")
    assert "承辦人 [姓名1] 分機 2301" in content and "王小明" not in content and "林志豪" not in content
    assert client.get(f"/api/jobs/{job_id}").json()["downloaded_at"] is not None
    key = client.get(f"/api/jobs/{job_id}/download?what=restore")
    assert key.status_code == 200 and ".deidr" in key.headers["content-disposition"]
    assert unquote(key.headers["content-disposition"].split("filename*=UTF-8\'\'")[1]) == name.replace("代號版", "還原檔").replace(".txt", ".deidr")
    assert open_(key.content)["[姓名1]"] == "王小明"  # opens without a password
    # A page loaded before the change still sends one: it is accepted and makes no difference.
    assert client.post(f"/api/jobs/{job_id}/export", json={"password": PASSWORD}).status_code == 200
    again = client.get(f"/api/jobs/{job_id}/download?what=restore")
    assert open_(again.content)["[姓名1]"] == "王小明" and PASSWORD.encode() not in again.content


@pytest.mark.parametrize(("lang", "prefix", "code", "heading"), [
    ("zh-TW", "代號版", "[姓名1]", "【頁首】"),
    ("en", "coded", "[NAME1]", "[Header]"),
])
def test_the_document_with_codes_is_downloaded_in_its_own_format_and_comes_back_through_the_restore_page(
        client, docx_bytes, lang, prefix, code, heading):
    import base64
    import re
    from urllib.parse import unquote
    from deid import codefile, replyfile, restorefile
    job_id = upload_for_codes(client, docx_bytes)
    ready(client, job_id)
    assert client.get(f"/api/jobs/{job_id}/download?what=coded").status_code == 409  # nothing made yet
    body = client.post(f"/api/jobs/{job_id}/export", params={"lang": lang}).json()
    assert body["lang"] == lang
    assert body["file"] == {"ext": ".docx", "placed": 3, "left": 0}
    assert body["file_notes"] == [codefile.PICTURES]  # the face in the picture is painted over
    r = client.get(f"/api/jobs/{job_id}/download?what=coded")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.wordprocessingml")
    name = unquote(r.headers["content-disposition"].split("filename*=UTF-8''")[1])
    assert re.fullmatch(rf"{prefix}_\d{{8}}-\d{{6}}\.docx", name) and "報告" not in r.headers["content-disposition"]
    assert client.get(f"/api/jobs/{job_id}").json()["downloaded_at"] is not None
    text = replyfile.read(r.content, "a.docx").text
    assert f"承辦人 {code} 分機 2301" in text and "王小明" not in text
    # The whole way round: this file, handed back as the cloud's reply, is the document again.
    key = client.get(f"/api/jobs/{job_id}/download?what=restore").content
    assert restorefile.open_(key)[code] == "王小明"
    text = client.get(f"/api/jobs/{job_id}/download?what=text").text
    assert heading in text and f"承辦人 {code} 分機 2301" in text
    back = restore_file_request(client, key, "reply.docx", r.content, lang=lang).json()
    assert back["replaced"] == 3 and back["file_ext"] == ".docx"
    assert heading in back["text"]
    assert replyfile.read(base64.b64decode(back["file_b64"]), "a.docx").text == replyfile.read(docx_bytes, "a.docx").text


def test_a_pdf_has_no_document_with_codes_to_download(client, tmp_path):
    from deid import codefile
    pdf = make_pdf(tmp_path / "a.pdf", [{"lines": ["承辦人 王小明"]}]).read_bytes()
    job_id = upload_for_codes(client, pdf, "報告.pdf")
    ready(client, job_id)
    body = client.post(f"/api/jobs/{job_id}/export").json()
    assert body["file"] is None and body["file_notes"] == [codefile.PDF_NOTE]
    r = client.get(f"/api/jobs/{job_id}/download?what=coded")
    assert r.status_code == 409 and r.json()["error"] == "這份文件沒有代號版檔案，請下載代號版文字"
    assert client.get(f"/api/jobs/{job_id}/download?what=text").status_code == 200


def test_each_function_refuses_the_other_ones_output(client, docx_bytes):
    masked, coded = upload(client, docx_bytes), upload_for_codes(client, docx_bytes)
    ready(client, masked)
    ready(client, coded)
    assert client.post(f"/api/jobs/{masked}/export").status_code == 409
    assert client.post(f"/api/jobs/{coded}/apply").status_code == 409
    assert client.get(f"/api/jobs/{masked}/download?what=restore").status_code == 409
    assert client.get(f"/api/jobs/{masked}/download?what=coded").status_code == 409
    assert client.get(f"/api/jobs/{coded}/download?what=nonsense").status_code == 400


def test_coded_text_with_something_left_readable_needs_acknowledgement(client, docx_bytes, monkeypatch):
    from deid import codes
    real = codes.build

    def leaky(inv, store, *, lang):
        out = real(inv, store, lang=lang)
        out.residue = ["代號版文字: 「王小明」"]
        return out

    monkeypatch.setattr(codes, "build", leaky)
    job_id = upload_for_codes(client, docx_bytes)
    ready(client, job_id)
    assert client.post(f"/api/jobs/{job_id}/export").json()["ok"] is False
    r = client.get(f"/api/jobs/{job_id}/download?what=text")
    assert r.status_code == 409 and r.json()["needs_ack"] is True
    assert client.get(f"/api/jobs/{job_id}/download?what=coded").json()["needs_ack"] is True
    assert client.get(f"/api/jobs/{job_id}/download?what=text&ack=1").status_code == 200
    assert client.get(f"/api/jobs/{job_id}/download?what=coded&ack=1").status_code == 200


def restore_request(client, key, text, password=None):
    return client.post("/api/restore", data={} if password is None else {"password": password},
                       files={"key": ("x.deidr", io.BytesIO(key), "application/octet-stream"),
                              "text": ("reply.txt", io.BytesIO(text.encode("utf-8")), "text/plain")})


def test_a_reply_is_restored_in_memory_and_nothing_is_kept(client, settings, caplog):
    import logging
    from deid.restorefile import pack
    key = pack({"[姓名1]": "王小明", "[電話1]": "0912-345-678"})

    def files():
        return sorted(str(p) for p in settings.data_dir.rglob("*"))

    before = files()
    with caplog.at_level(logging.DEBUG, logger="deid"):
        r = restore_request(client, key, "已通知【姓名1】，並請 [姓名2] 回電 [電話1]")
    assert r.status_code == 200, r.text
    assert r.json() == {"text": "已通知王小明，並請 [姓名2] 回電 0912-345-678", "replaced": 2, "unknown": ["[姓名2]"], "unused": [],
                        "notes": []}
    assert files() == before  # no job, no file
    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "王小明" not in logged and "0912" not in logged


def test_a_restore_file_made_with_a_password_before_is_still_restored(client, caplog):
    import logging
    from tests.builders import make_encrypted_restore_file
    key = make_encrypted_restore_file({"[姓名1]": "王小明"}, PASSWORD)
    asked = restore_request(client, key, "[姓名1]")
    assert asked.status_code == 400 and asked.json()["needs_password"] is True and "密碼" in asked.json()["error"]
    assert "王小明" not in asked.text
    wrong = restore_request(client, key, "[姓名1]", PASSWORD + "x")
    assert wrong.status_code == 400 and "密碼" in wrong.json()["error"] and "王小明" not in wrong.text
    with caplog.at_level(logging.DEBUG, logger="deid"):
        right = restore_request(client, key, "[姓名1]", PASSWORD)
    assert right.status_code == 200 and right.json()["text"] == "王小明"
    assert PASSWORD not in "\n".join(rec.getMessage() for rec in caplog.records)


def test_restore_explains_a_file_that_is_not_a_restore_file(client):
    other = restore_request(client, b"not a restore file", "[姓名1]")
    assert other.status_code == 400 and "還原檔" in other.json()["error"] and "needs_password" not in other.json()
    assert client.post("/api/restore", data={}).status_code == 422


def restore_file_request(client, key, name, data, *, lang="zh-TW", **more):
    files = {"key": ("x.deidr", io.BytesIO(key), "application/octet-stream"), "file": (name, io.BytesIO(data), "application/octet-stream")}
    files.update(more)
    return client.post("/api/restore", files=files, data={"lang": lang})


def test_a_word_or_pdf_file_from_the_cloud_is_restored_as_text_and_nothing_is_kept(client, settings, tmp_path, monkeypatch, caplog):
    import logging
    import tempfile
    from deid.restorefile import pack
    key = pack({"[姓名1]": "王小明", "[電話1]": "0912-345-678"})
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    word = make_docx(tmp_path / "w.docx", ["已通知 [姓名1]。", "請回電【電話1】，並轉告 [姓名2]。"],
                     images=[("image1.png", text_image(["x"], size=(400, 200)))]).read_bytes()
    pdf = make_pdf(tmp_path / "p.pdf", [{"lines": ["已通知 [姓名1]"]}, {"lines": ["請回電 [電話1]"]}]).read_bytes()
    before = sorted(str(p) for p in settings.data_dir.rglob("*"))
    with caplog.at_level(logging.DEBUG, logger="deid"):
        w = restore_file_request(client, key, "機密回覆檔名.docx", word)
        p = restore_file_request(client, key, "reply.PDF", pdf)
    assert w.status_code == 200, w.text
    body = w.json()
    restored = base64.b64decode(body.pop("file_b64"))
    assert body == {"text": "已通知 王小明。\n請回電0912-345-678，並轉告 [姓名2]。", "replaced": 2, "unknown": ["[姓名2]"], "unused": [],
                    "notes": ["檔案裡有 1 張圖片，圖片裡的文字沒有讀取"], "file_ext": ".docx"}
    from deid import replyfile
    assert replyfile.read(restored, "w.docx").text == body["text"]  # the Word file back, with the strings in place
    with zipfile.ZipFile(io.BytesIO(restored)) as z:
        assert "word/media/image1.png" in z.namelist()
    assert p.status_code == 200, p.text
    assert p.json()["text"] == "【第 1 頁】\n已通知 王小明\n\n【第 2 頁】\n請回電 0912-345-678" and p.json()["replaced"] == 2
    assert "file_b64" not in p.json() and any("PDF 只能還原成文字" in n for n in p.json()["notes"])  # a PDF comes back as text only
    assert sorted(str(x) for x in settings.data_dir.rglob("*")) == before and list(scratch.iterdir()) == []
    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "王小明" not in logged and "0912" not in logged and "機密回覆檔名" not in logged


def test_starting_the_service_removes_restore_scratch_a_crash_left_behind(settings, tmp_path, monkeypatch):
    import tempfile
    scratch = tmp_path / "scratch"
    (scratch / "deid-reply-left").mkdir(parents=True)
    (scratch / "deid-reply-left" / "reply.docx").write_bytes(b"x")
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    with TestClient(create_app(settings, JobStore(settings))):
        assert list(scratch.iterdir()) == []


def test_restore_wants_a_text_or_a_file_and_not_both(client, tmp_path):
    from deid.restorefile import pack
    key = pack({"[姓名1]": "王小明"})
    word = make_docx(tmp_path / "w.docx", ["已通知 [姓名1]。"]).read_bytes()
    both = restore_file_request(client, key, "w.docx", word, text=("reply.txt", io.BytesIO("[姓名1]".encode("utf-8")), "text/plain"))
    assert both.status_code == 400 and "擇一" in both.json()["error"] and "王小明" not in both.text
    neither = client.post("/api/restore", files={"key": ("x.deidr", io.BytesIO(key), "application/octet-stream")})
    assert neither.status_code == 400 and "貼上" in neither.json()["error"] and "檔案" in neither.json()["error"]


def test_restore_explains_a_file_it_cannot_read(client, tmp_path):
    from deid.restorefile import pack
    key = pack({"[姓名1]": "王小明"})
    kind = restore_file_request(client, key, "reply.rtf", b"[x1]")
    assert kind.status_code == 400 and ".docx" in kind.json()["error"] and ".pdf" in kind.json()["error"]
    broken = restore_file_request(client, key, "reply.docx", b"this is not a zip")
    assert broken.status_code == 400 and broken.json()["error"]
    scan = make_pdf(tmp_path / "s.pdf", [{"image": text_image(["[姓名1]"], size=(600, 800))}]).read_bytes()
    empty = restore_file_request(client, key, "s.pdf", scan)
    assert empty.status_code == 400 and "讀不到文字" in empty.json()["error"] and "貼上" in empty.json()["error"]
    bad_key = restore_file_request(client, b"not a restore file", "reply.docx", b"this is not a zip")
    assert bad_key.status_code == 400 and "還原檔" in bad_key.json()["error"]  # the restore file is looked at first


def test_restore_refuses_a_request_that_is_too_large(client):
    r = client.post("/api/restore", content=b"x", headers={"content-length": str(64 * 1024 * 1024), "content-type": "multipart/form-data; boundary=x"})
    assert r.status_code == 413


# ---- names of one person (the code function) ----------------------------------------------

@pytest.fixture
def people(settings):
    """A service whose detector knows two writings of one name."""
    settings.use_agent = False
    llm = keyword_llm({"王小明": "姓名", "小明": "姓名"})

    def factory(src, workdir, s, progress=None, restore=False):
        return build_context(src, workdir, s, llm=llm, progress=progress, restore=restore, ocr=FakeOcr(), faces=FakeFaces())

    store = JobStore(settings, context_factory=factory)
    store.start()
    with TestClient(create_app(settings, store)) as c:
        c.store = store
        yield c
    store.stop()


@pytest.fixture
def two_writings(tmp_path):
    return make_docx(tmp_path / "p.docx", ["王小明到場。", "小明另提供資料，分機 2301。"]).read_bytes()


def groups_of(client, job_id):
    return {f["text"]: f["group"] for f in review(client, job_id)["findings"] if f["type"] == "姓名"}


def test_two_names_can_be_made_one_person_by_hand_and_taken_apart_again(people, two_writings):
    job_id = upload_for_codes(people, two_writings)
    ready(people, job_id)
    rev = review(people, job_id)
    a, b = finding(rev, "王小明")["id"], finding(rev, "小明")["id"]
    assert groups_of(people, job_id) == {"王小明": "", "小明": ""}
    r = people.post(f"/api/jobs/{job_id}/findings/{a}/group", json={"with": b})
    assert r.status_code == 200 and r.json() == {"relist": True}
    assert groups_of(people, job_id) == {"王小明": "g1", "小明": "g1"}
    r = people.post(f"/api/jobs/{job_id}/findings/{b}/group", json={"with": None})
    assert r.status_code == 200 and groups_of(people, job_id) == {"王小明": "", "小明": ""}


def test_same_person_is_refused_in_a_mask_job_and_for_what_is_not_an_active_name(people, two_writings):
    masked = upload(people, two_writings)
    ready(people, masked)
    rev = review(people, masked)
    r = people.post(f"/api/jobs/{masked}/findings/{finding(rev, '王小明')['id']}/group", json={"with": finding(rev, "小明")["id"]})
    assert r.status_code == 400 and "代號＋還原" in r.json()["error"]
    job_id = upload_for_codes(people, two_writings)
    ready(people, job_id)
    rev = review(people, job_id)
    a, b = finding(rev, "王小明")["id"], finding(rev, "小明")["id"]
    people.post(f"/api/jobs/{job_id}/findings", json={"kind": "text", "text": "分機 2301", "type": "電話", "all": True})
    other = finding(review(people, job_id), "分機 2301")["id"]
    for target, partner in ((a, other), (a, a), (a, "f999"), ("f999", b)):
        r = people.post(f"/api/jobs/{job_id}/findings/{target}/group", json={"with": partner})
        assert r.status_code in (400, 404), (target, partner)
    assert people.post(f"/api/jobs/{job_id}/findings/{a}/group", json={}).status_code == 400
    people.patch(f"/api/jobs/{job_id}/findings/{b}", json={"on": False})
    r = people.post(f"/api/jobs/{job_id}/findings/{a}/group", json={"with": b})
    assert r.status_code == 400 and "啟用中的姓名" in r.json()["error"]
    assert set(groups_of(people, job_id).values()) == {""}


def test_switching_off_a_linked_name_tells_the_page_to_fetch_the_list_again(people, two_writings):
    job_id = upload_for_codes(people, two_writings)
    ready(people, job_id)
    rev = review(people, job_id)
    a, b = finding(rev, "王小明")["id"], finding(rev, "小明")["id"]
    people.post(f"/api/jobs/{job_id}/findings/{a}/group", json={"with": b})
    r = people.patch(f"/api/jobs/{job_id}/findings/{b}", json={"on": False})
    assert r.status_code == 200 and r.json()["relist"] is True  # the other name lost its group too
    assert groups_of(people, job_id) == {"王小明": "", "小明": ""}


@pytest.mark.parametrize(("lang", "label", "notice"), [
    ("zh-TW", "姓名", "同一個人的不同寫法"),
    ("en", "NAME", "different forms of the same person's name"),
])
def test_names_made_one_person_come_out_with_one_number(people, two_writings, lang, label, notice):
    job_id = upload_for_codes(people, two_writings)
    ready(people, job_id)
    rev = review(people, job_id)
    people.post(f"/api/jobs/{job_id}/findings/{finding(rev, '王小明')['id']}/group", json={"with": finding(rev, "小明")["id"]})
    assert people.post(f"/api/jobs/{job_id}/export", params={"lang": lang}).status_code == 200
    text = people.get(f"/api/jobs/{job_id}/download?what=text").content.decode("utf-8")
    assert f"[{label}1]到場。" in text and f"[{label}1b]另提供資料" in text and notice in text


def test_the_review_says_how_the_same_person_step_went(people, two_writings):
    job_id = upload_for_codes(people, two_writings)
    ready(people, job_id)
    assert review(people, job_id)["names"] is None  # the step is off in tests; the page shows nothing


def test_each_pair_of_coded_text_and_table_is_named_by_when_it_was_made(client, docx_bytes):
    """Making the text again can give other numbers to the same strings: a text must not look like it belongs
    to a table made another time."""
    import time as clock
    from urllib.parse import unquote

    def names(lang=""):
        out = []
        for what in ("restore", "text"):
            response = client.get(f"/api/jobs/{job_id}/download", params={"what": what, "lang": lang})
            out.append(unquote(response.headers["content-disposition"].split("filename*=UTF-8''")[1]))
        return out

    job_id = upload_for_codes(client, docx_bytes)
    ready(client, job_id)
    client.store._update(job_id, created=clock.time() - 3 * 86400)  # uploaded three days ago
    first = client.post(f"/api/jobs/{job_id}/export").json()
    table, text = names()
    stamp = clock.strftime("%Y%m%d-%H%M%S", clock.localtime(first["made"]))
    assert text == f"代號版_{stamp}.txt" and table == f"還原檔_{stamp}.deidr"
    assert names("en") == [f"restore_{stamp}.deidr", f"coded_{stamp}.txt"]
    clock.sleep(1.1)
    second = client.post(f"/api/jobs/{job_id}/export").json()
    assert second["made"] > first["made"] and names()[0] != table and names()[1] != text
    english = client.post(f"/api/jobs/{job_id}/export?lang=en").json()
    assert english["lang"] == "en"
    stale = client.get(f"/api/jobs/{job_id}/download", params={"what": "restore", "batch": second["made"]})
    assert stale.status_code == 409
    key = client.get(f"/api/jobs/{job_id}/download", params={"what": "restore", "batch": english["made"]})
    assert key.status_code == 200 and b"[NAME1]" in key.content
