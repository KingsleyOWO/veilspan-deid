"""Seven small defects of the page (review of 2026-11-23, items D1-D7).

The page is plain JavaScript without a build step, so what it does is checked in a real browser: one run of a
Playwright script (node, headless Chromium) against a real server on a free local port, started here with the
detector double the other tests use. The script writes what it saw as JSON and each test below reads its part.
These tests are skipped when node, the Playwright package or its Chromium cannot be found (set DEID_PLAYWRIGHT
to the folder of the package, `.../node_modules/playwright`); the two static tests always run.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from deid import restorefile
from deid.config import load_settings
from deid.jobs import JobStore
from deid.pipeline import build_context
from deid.web.app import create_app
from tests.builders import make_docx, make_encrypted_restore_file, make_pdf, text_image
from tests.fakes import keyword_llm

STATIC = Path(__file__).resolve().parent.parent / "deid" / "web" / "static"
NAMES = {"王小明": "姓名", "陳美玲": "姓名", "林志豪": "姓名"}
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
PAGE = (STATIC / "index.html").read_text(encoding="utf-8")

# ---- the browser script -------------------------------------------------------------------------------------------

SCENARIOS = r"""
const fs = require('fs');
const path = require('path');
const { chromium } = require(process.env.PW_DIR);
const [BASE, FILES, SHOTS] = process.argv.slice(2);
const F = (n) => path.join(FILES, n);
const seen = { requests: [], pageErrors: [] };

(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 }, acceptDownloads: true, locale: 'zh-TW' });
  context.on('request', (r) => seen.requests.push(r.url()));
  const fresh = async () => {
    const page = await context.newPage();
    page.on('pageerror', (e) => seen.pageErrors.push(String(e)));
    page.dialogs = [];
    page.on('dialog', async (d) => { page.dialogs.push(d.message()); await d.accept(); });
    return page;
  };
  const shot = async (page, name, full) => { if (SHOTS && SHOTS !== '-') await page.screenshot({ path: path.join(SHOTS, name + '.png'), fullPage: !!full }); };
  const ONLY = process.env.ONLY ? process.env.ONLY.split(',') : null;  // for debugging one scenario
  // fn gets an object to fill as it goes, so that a step that fails does not lose what the earlier steps saw
  const run = async (key, fn) => {
    if (ONLY && !ONLY.includes(key)) return;
    const part = {};
    try { seen[key] = (await fn(part)) || part; } catch (e) { seen[key] = { ...part, crashed: String((e && e.stack) || e).slice(0, 600) }; }
  };
  const resultOpen = (page) => page.evaluate(() => { const r = document.getElementById('result'); return !!r && !r.hidden; }).catch(() => null);
  // REUSE=1 (a run against a server whose detector is a real model): one upload per function, the later scenarios open it again
  const jobs = {};
  const openDoc = async (page, file, mode) => {
    if (process.env.REUSE === '1' && jobs[mode]) {
      await page.goto(BASE + '/#' + jobs[mode]);
    } else {
      await page.goto(BASE);
      await page.setInputFiles(mode === 'code' ? '#file-code' : '#file', F(file));
    }
    await page.waitForSelector('#review:not([hidden])', { timeout: 120000 });
    await page.waitForSelector('.item', { timeout: 20000 });
    jobs[mode] = jobs[mode] || new URL(page.url()).hash.slice(1);
    if (await resultOpen(page)) await page.click('#result-back');  // a job that has a result shows it again
  };

  // ---- D1: downloads -------------------------------------------------------------------------------------
  const DL = /\/api\/jobs\/[^/]+\/(download|report)/;
  const gone = { status: 404, body: JSON.stringify({ error: '找不到這個工作，可能已被刪除' }) };
  const grab = async (page, sel) => {
    const [dl] = await Promise.all([page.waitForEvent('download', { timeout: 15000 }), page.click(sel)]);
    const p = await dl.path();
    return { name: dl.suggestedFilename(), size: fs.statSync(p).size, head: fs.readFileSync(p).subarray(0, 4).toString('latin1'), url: page.url() };
  };
  // Press a download button while the server answers with `mock`; put the page back if the press took it away.
  const press = async (page, sel, mock, state, keep) => {
    state.mock = mock;
    page.dialogs.length = 0;
    const before = page.url();
    let downloaded = false;
    const onDl = () => { downloaded = true; };
    page.on('download', onDl);
    await page.click(sel);
    await page.waitForTimeout(1200);
    page.off('download', onDl);
    let body = '';
    try { body = await page.evaluate(() => document.body.innerText.slice(0, 100)); } catch (e) { body = 'unreadable'; }
    const out = { dialogs: [...page.dialogs], urlSame: page.url() === before, downloaded, body, resultOpen: await resultOpen(page) };
    state.mock = null;
    if (!out.urlSame && !keep) {
      await page.goto(before);
      await page.waitForSelector('#result:not([hidden])', { timeout: 20000 });
    }
    return out;
  };
  const watchDownloads = async (page) => {
    const state = { mock: null };
    await page.route(DL, (route) => {
      const m = state.mock;
      if (!m) return route.continue();
      if (m === 'abort') return route.abort();
      return route.fulfill({ status: m.status, contentType: m.type || 'application/json', body: m.body });
    });
    return state;
  };

  await run('d1code', async (o) => {
    o.ok = {};
    o.gone = {};
    const page = await fresh();
    const st = await watchDownloads(page);
    await openDoc(page, '遮蔽測試.docx', 'code');
    await page.click('#btn-apply');
    await page.waitForSelector('#result:not([hidden])');
    const buttons = { coded: '#result-coded', text: '#result-download', restore: '#result-restore', report: '#result-report' };
    for (const [k, sel] of Object.entries(buttons)) o.ok[k] = await grab(page, sel);
    for (const [k, sel] of Object.entries(buttons)) o.gone[k] = await press(page, sel, gone, st);
  });

  // ---- D2: part names on the result screen ---------------------------------------------------------------
  const lines = (page, sel) => page.$$eval(sel + ' li', (lis) => lis.map((li) => ({ text: li.textContent, title: li.getAttribute('title') })));
  await run('d2canned', async () => {
    const page = await fresh();
    const canned = { ok: false, stats: { text_spans: 3, boxes: 0, meta: 1, strings: 2 },
      residue: ['word/document.xml: 「■■■」', 'word/footer1.xml: 「■■■」',
        'word/embeddings/Microsoft_Word_Document.docx!word/document.xml: 「林志豪」', 'docProps/core.xml: 「林志豪」',
        'xl/worksheets/sheet2.xml: 「X」', 'word/unknownpart.dat: 「X」', '第 2 頁: 「X」', '代號版文字: 「X」',
        'word/document.xml: 「word/footer1.xml」'],
      problems: ['輸出檔的 XML 格式錯誤：word/header2.xml', '圖片 word/media/image3.png 仍帶有相機或編輯軟體寫入的資訊',
        '內嵌檔案 word/embeddings/Microsoft_Word_Document.docx 的首頁縮圖未移除',
        '輸出檔的 XML 格式錯誤：word/embeddings/Microsoft_Word_Document.docx!word/document.xml'],
      notes: ['部分保留：word/footnotes.xml: 「林志豪」', '遮一半保留：word/endnotes.xml: 「X」'] };
    await page.route(/\/api\/jobs\/[^/]+\/apply$/, (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(canned) }));
    await page.route(/\/api\/jobs\/[^/]+\/review$/, async (route) => {
      const res = await route.fetch();
      const data = await res.json();
      data.unchecked = [{ name: 'word/media/image2.emf', reason: '不支援的圖片格式，未檢查' }, { name: 'word/embeddings/oleObject1.bin', reason: '內嵌物件，未檢查' }];
      data.warnings = ['word/media/image2.emf 沒有檢查', '有 1 張圖片沒有文字'];
      data.unmaskable = [{ where: 'word/embeddings/oleObject1.bin', text: '林志豪', reason: '程式改不到' }];
      await route.fulfill({ response: res, json: data });
    });
    await openDoc(page, '遮蔽測試.docx', 'mask');
    const panel = await lines(page, '#warnings');
    await page.click('#btn-apply');
    await page.waitForSelector('#result:not([hidden])');
    await shot(page, 'd2-part-names-in-words');
    return { issues: await lines(page, '#result-issues'), notes: await lines(page, '#result-notes'), unchecked: await lines(page, '#result-unchecked'), panel };
  });

  await run('d2real', async () => {
    const page = await fresh();
    await openDoc(page, '遮蔽測試.docx', 'mask');
    const fid = await page.evaluate(() => {
      const i = [...document.querySelectorAll('.item')].find((x) => x.querySelector('.item-text > div').textContent === '林志豪');
      return i ? i.dataset.fid : null;
    });
    if (!fid) return { skipped: 'no entry for 林志豪' };
    await page.click(`.item[data-fid="${fid}"] .item-meta button.link`);
    await page.locator(`.item[data-fid="${fid}"] .occ`, { hasText: '註腳' }).locator('input').click();
    await page.waitForTimeout(600);
    await page.click('#btn-apply');
    await page.waitForSelector('#result:not([hidden])');
    await shot(page, 'd2-real-partial-note');
    return { notes: await lines(page, '#result-notes'), issues: await lines(page, '#result-issues') };
  });

  // ---- D3: a selection across table cells ----------------------------------------------------------------
  await run('d3', async () => {
    const page = await fresh();
    await openDoc(page, '遮蔽測試.docx', 'mask');
    const select = (needle, from, to) => page.evaluate(([needle, from, to]) => {
      const unit = [...document.querySelectorAll('.unit')].find((u) => u.textContent.includes(needle));
      const base = unit.textContent.indexOf(needle);
      const walker = document.createTreeWalker(unit, NodeFilter.SHOW_TEXT);
      let pos = 0, a = null, b = null, n;
      while ((n = walker.nextNode())) {
        const len = n.nodeValue.length;
        if (!a && base + from < pos + len) a = [n, base + from - pos];
        if (base + to <= pos + len) { b = [n, base + to - pos]; break; }
        pos += len;
      }
      const r = document.createRange();
      r.setStart(a[0], a[1]);
      r.setEnd(b[0], b[1]);
      const s = window.getSelection();
      s.removeAllRanges();
      s.addRange(r);
      document.getElementById('doc').dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
      return s.toString();
    }, [needle, from, to]);
    const o = {};
    o.crossText = await select('林志豪 | 0922-111-333', 1, 10);
    await page.waitForTimeout(150);
    o.crossButton = await page.$eval('#selbtn', (e) => !e.hidden);
    await shot(page, 'd3-selection-across-cells');
    if (o.crossButton) {  // what pressing it did (the page as it was)
      await page.click('#selbtn button');
      await page.click('#popover-ok');
      await page.waitForTimeout(500);
      o.crossDialogs = [...page.dialogs];
    }
    page.dialogs.length = 0;
    await page.reload();
    await page.waitForSelector('.item');
    if (await resultOpen(page)) await page.click('#result-back');
    o.oneText = await select(' | 分機 2301', 3, 10);  // the last cell of the row 承辦人 | 王小明 | 分機 2301
    await page.waitForTimeout(150);
    o.oneButton = await page.$eval('#selbtn', (e) => !e.hidden);
    await shot(page, 'd3-selection-in-one-cell');
    if (o.oneButton) {
      await page.click('#selbtn button');
      await page.click('#popover-ok');
      await page.waitForTimeout(600);
    }
    o.oneDialogs = [...page.dialogs];
    o.oneListed = await page.evaluate(() => [...document.querySelectorAll('.item .item-text > div')].map((d) => d.textContent).includes('分機 2301'));
    return o;
  });

  // ---- D4, D6: the restore page --------------------------------------------------------------------------
  await run('d4', async () => {
    const page = await fresh();
    await page.goto(BASE + '/#restore');
    await page.setInputFiles('#restore-key', F('舊加密.deidr'));
    await page.fill('#restore-input', '[姓名1] 已到場');
    await page.click('#restore-run');
    await page.waitForSelector('#restore-password-field:not([hidden])');
    await page.fill('#restore-password', 'typed-secret');
    const o = { shown: await page.$eval('#restore-password-field', (e) => !e.hidden) };
    await shot(page, 'd4-password-field-shown');
    await page.setInputFiles('#restore-key', F('還原檔.deidr'));
    await page.waitForTimeout(200);
    o.fieldShownAfter = await page.$eval('#restore-password-field', (e) => !e.hidden);
    o.passwordAfter = await page.$eval('#restore-password', (e) => e.value);
    o.errorShownAfter = await page.$eval('#restore-error', (e) => !e.hidden);
    await shot(page, 'd4-password-field-gone');
    await page.click('#restore-run');
    await page.waitForSelector('#restore-result:not([hidden])');
    o.restored = await page.$eval('#restore-output', (e) => e.value);
    return o;
  });

  const blocks = (page) => page.$$eval('#restore-result > div', (ds) => ds.filter((d) => !d.hidden && d.querySelector('h3'))
    .map((d) => ({ id: d.id, heading: d.querySelector('h3').textContent, items: [...d.querySelectorAll('li')].map((li) => li.textContent) })));
  await run('d6', async () => {
    const o = {};
    const page = await fresh();
    for (const [k, file] of [['pdf', '回覆.pdf'], ['picture', '回覆含圖.docx']]) {
      await page.goto(BASE + '/#restore');
      await page.reload();
      await page.setInputFiles('#restore-key', F('還原檔.deidr'));
      await page.setInputFiles('#restore-file', F(file));
      await page.click('#restore-run');
      await page.waitForSelector('#restore-result:not([hidden])');
      o[k] = await blocks(page);
      await shot(page, 'd6-restore-' + k, true);
    }
    return o;
  });

  // ---- D5: the home page ---------------------------------------------------------------------------------
  await run('d5', async () => {
    const page = await fresh();
    await page.goto(BASE);
    await shot(page, 'd5-home');
    return { cards: await page.$$eval('.choice', (cs) => cs.map((c) => c.innerText)) };
  });

  // ---- D7: keyboard focus in the list --------------------------------------------------------------------
  await run('d7', async () => {
    const page = await fresh();
    await openDoc(page, '遮蔽測試.docx', 'mask');
    const where = () => page.evaluate(() => {
      const a = document.activeElement;
      const item = a && a.closest ? a.closest('.item') : null;
      const group = a && a.closest ? a.closest('.group') : null;
      return { tag: a && a.tagName, type: (a && a.type) || null, text: (a && a.tagName === 'BUTTON') ? a.textContent : null,
        fid: item ? item.dataset.fid : null, inDetail: !!(a && a.closest && a.closest('.detail')), group: group ? group.dataset.type || '?' : null,
        inHead: !!(a && a.closest && a.closest('.group-head')), inList: !!(a && a.closest && a.closest('#list')) };
    });
    const fid = await page.evaluate(() => document.querySelector('.item').dataset.fid);
    const o = { fid };
    await page.focus(`.item[data-fid="${fid}"] .item-main > input`);
    await page.keyboard.press('Space');
    await page.waitForTimeout(500);
    o.afterCheckbox = await where();
    await page.focus(`.item[data-fid="${fid}"] .item-meta button`);
    await page.keyboard.press('Enter');
    await page.waitForTimeout(300);
    o.afterExpand = await where();
    await shot(page, 'd7-focus-kept-after-expand');
    await page.focus(`.item[data-fid="${fid}"] .detail .occ input`);
    await page.keyboard.press('Space');
    await page.waitForTimeout(500);
    o.afterOccurrence = await where();
    await page.focus(`.item[data-fid="${fid}"] .item-meta button`);
    await page.keyboard.press('Enter');
    await page.waitForTimeout(300);
    o.afterCollapse = await where();
    o.groupOf = await page.evaluate((f) => { const g = document.querySelector(`.item[data-fid="${f}"]`).closest('.group'); return g.dataset.type || '?'; }, fid);
    await page.focus('.group-head button:last-child');
    await page.keyboard.press('Enter');
    await page.waitForTimeout(500);
    o.afterGroup = await where();
    return o;
  });

  // last: it deletes its job on the server
  await run('d1mask', async (o) => {
    const page = await fresh();
    const st = await watchDownloads(page);
    await openDoc(page, '遮蔽測試.docx', 'mask');
    await page.click('#btn-apply');
    await page.waitForSelector('#result:not([hidden])');
    o.ok = { file: await grab(page, '#result-download'), report: await grab(page, '#result-report') };
    o.gone = { file: await press(page, '#result-download', gone, st), report: await press(page, '#result-report', gone, st) };
    o.ack = await press(page, '#result-download', { status: 409, body: JSON.stringify({ error: '仍有殘留或問題，需確認後才能下載', needs_ack: true }) }, st);
    o.html = await press(page, '#result-download', { status: 502, type: 'text/html', body: '<html><body>Bad Gateway</body></html>' }, st);
    o.net = await press(page, '#result-download', 'abort', st);
    await shot(page, 'd1-failed-download-keeps-the-page');
    // for real: the job is deleted on the server (as after its time), then the button is pressed
    const id = new URL(page.url()).hash.slice(1);
    await page.evaluate((job) => fetch('/api/jobs/' + job, { method: 'DELETE' }), id);
    o.real = await press(page, '#result-download', null, st, true);
  });

  await browser.close();
  console.log('RESULT' + JSON.stringify(seen));
})().catch((e) => { console.error(e); process.exit(1); });
"""


# ---- finding what is needed ---------------------------------------------------------------------------------------

def _browser_ready(folder: Path) -> bool:
    if not shutil.which("node"):
        return False
    probe = subprocess.run(["node", "-e", "const {chromium}=require(process.argv[1]);"
                            "process.exit(require('fs').existsSync(chromium.executablePath())?0:3)", str(folder)],
                           capture_output=True, text=True, timeout=60)
    return probe.returncode == 0


def _playwright():
    """The first Playwright package (the folder DEID_PLAYWRIGHT names, else one cached by npx) whose Chromium is installed."""
    candidates = [Path(os.environ["DEID_PLAYWRIGHT"])] if os.environ.get("DEID_PLAYWRIGHT") else []
    candidates += sorted((Path.home() / ".npm" / "_npx").glob("*/node_modules/playwright"))
    for folder in candidates:
        if (folder / "package.json").is_file() and _browser_ready(folder):
            return folder
    return None


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    import uvicorn
    root = tmp_path_factory.mktemp("fix3ui")
    s = load_settings(root / "none.env", environ={})
    s.link_names = False
    s.use_agent = False
    s.data_dir = root / "data"
    s.llm_base_url, s.llm_model, s.llm_api_key = "http://127.0.0.1:9/v1", "test-model", "test-key"
    llm = keyword_llm(NAMES)

    def factory(src, workdir, settings, progress=None, restore=False):
        return build_context(src, workdir, settings, llm=llm, progress=progress, restore=restore)

    store = JobStore(s, context_factory=factory)
    store.start()
    server = uvicorn.Server(uvicorn.Config(create_app(s, store), host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    end = time.time() + 20
    while not server.started and time.time() < end:
        time.sleep(0.05)
    assert server.started, "the test server did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    yield {"base": f"http://127.0.0.1:{port}", "root": root}
    server.should_exit = True
    thread.join(10)
    store.stop()


def _files(root: Path) -> Path:
    folder = root / "files"
    folder.mkdir()
    make_docx(folder / "遮蔽測試.docx", ["承辦人 王小明 分機 2301", "王小明 再次出現"],
              table=[["承辦人", "王小明", "分機 2301"], ["聯絡人", "林志豪", "0922-111-333"]],
              header="頁首 陳美玲", footnote="註：資料由林志豪提供。")
    (folder / "舊加密.deidr").write_bytes(make_encrypted_restore_file({"[姓名1]": "王小明"}, "old-pass-1234"))
    (folder / "還原檔.deidr").write_bytes(restorefile.pack({"[姓名1]": "王小明"}))
    make_pdf(folder / "回覆.pdf", [{"lines": ["[姓名1] 已到場"]}])
    make_docx(folder / "回覆含圖.docx", ["[姓名1] 已到場"], images=[("image1.png", text_image(["圖"], size=(300, 100)))])
    return folder


@pytest.fixture(scope="module")
def seen(site):
    folder = _playwright()
    if folder is None:
        pytest.skip("node with the Playwright package and its Chromium is needed (DEID_PLAYWRIGHT)")
    root = site["root"]
    files = _files(root)
    script = root / "scenarios.js"
    script.write_text(SCENARIOS, encoding="utf-8")
    shots = os.environ.get("DEID_UI_SHOTS", "-")
    run = subprocess.run(["node", str(script), site["base"], str(files), shots], capture_output=True, text=True, timeout=420,
                         env={**os.environ, "PW_DIR": str(folder)}, cwd=root)
    mark = [ln for ln in run.stdout.splitlines() if ln.startswith("RESULT")]
    assert mark, f"the browser script gave no result:\n{run.stdout[-1500:]}\n{run.stderr[-1500:]}"
    if os.environ.get("DEID_UI_DUMP"):  # for looking at what the browser saw
        Path(os.environ["DEID_UI_DUMP"]).write_text(mark[-1][len("RESULT"):], encoding="utf-8")
    return json.loads(mark[-1][len("RESULT"):])


def part(seen, key):
    got = seen[key]
    assert "crashed" not in got, got["crashed"]
    return got


# ---- D1: a download that fails keeps the page -----------------------------------------------------------------------

def test_no_download_button_leaves_the_page_for_the_api_address():
    assert re.findall(r"location\.href\s*=\s*jobUrl", APP_JS) == []


def test_a_download_that_works_still_arrives_under_the_servers_name(seen):
    ok = part(seen, "d1mask")["ok"]
    assert ok["file"]["name"] == "masked_遮蔽測試.docx" and ok["file"]["head"].startswith("PK") and ok["file"]["size"] > 1000
    assert ok["report"]["name"] == "report.json"
    assert "/api/" not in ok["file"]["url"] and ok["file"]["url"].split("#")[0] == ok["report"]["url"].split("#")[0]  # still the app
    code = part(seen, "d1code")["ok"]
    assert re.fullmatch(r"代號版_\d{8}-\d{6}\.docx", code["coded"]["name"]) and code["coded"]["head"].startswith("PK")
    assert re.fullmatch(r"代號版_\d{8}-\d{6}\.txt", code["text"]["name"])
    assert re.fullmatch(r"還原檔_\d{8}-\d{6}\.deidr", code["restore"]["name"])
    assert code["report"]["name"] == "report.json"


@pytest.mark.parametrize("case, message", [
    ("gone", "找不到這個工作，可能已被刪除"),
    ("ack", "仍有殘留或問題，需確認後才能下載"),
    ("html", "下載失敗"),
    ("net", "無法連線到伺服器"),
])
def test_a_failed_download_keeps_the_page_and_shows_what_the_server_said(seen, case, message):
    mask = part(seen, "d1mask")
    tries = [mask["gone"]["file"], mask["gone"]["report"]] if case == "gone" else [mask[case]]
    if case == "gone":
        tries += list(part(seen, "d1code")["gone"].values())
    for t in tries:
        assert t["urlSame"] is True and t["resultOpen"] is True, t
        assert t["dialogs"] == [message] and t["downloaded"] is False, t
        assert "{" not in t["body"], t  # no raw JSON where the app was


def test_a_job_deleted_on_the_server_shows_the_servers_message_when_its_file_is_asked_for(seen):
    t = part(seen, "d1mask")["real"]
    assert t["urlSame"] is True and t["resultOpen"] is True and t["dialogs"] == ["找不到這個工作，可能已被刪除"], t


# ---- D2: where it is, in words ----------------------------------------------------------------------------------------

CANNED_ISSUES = [
    ("內文: 「■■■」", "word/document.xml: 「■■■」"),
    ("頁尾: 「■■■」", "word/footer1.xml: 「■■■」"),
    ("內嵌檔案 Microsoft_Word_Document.docx 的內文: 「林志豪」", "word/embeddings/Microsoft_Word_Document.docx!word/document.xml: 「林志豪」"),
    ("檔案屬性: 「林志豪」", "docProps/core.xml: 「林志豪」"),
    ("工作表（sheet2.xml）: 「X」", "xl/worksheets/sheet2.xml: 「X」"),
    ("word/unknownpart.dat: 「X」", None),  # no name for it: shown as it is
    ("第 2 頁: 「X」", None),
    ("代號版文字: 「X」", None),
    ("內文: 「word/footer1.xml」", "word/document.xml: 「word/footer1.xml」"),  # what was found is quoted as it was
    ("輸出檔的 XML 格式錯誤：頁首", "輸出檔的 XML 格式錯誤：word/header2.xml"),
    ("圖片 image3.png 仍帶有相機或編輯軟體寫入的資訊", "圖片 word/media/image3.png 仍帶有相機或編輯軟體寫入的資訊"),
    ("內嵌檔案 Microsoft_Word_Document.docx 的首頁縮圖未移除", "內嵌檔案 word/embeddings/Microsoft_Word_Document.docx 的首頁縮圖未移除"),
    ("輸出檔的 XML 格式錯誤：內嵌檔案 Microsoft_Word_Document.docx 的內文",
     "輸出檔的 XML 格式錯誤：word/embeddings/Microsoft_Word_Document.docx!word/document.xml"),
]


def test_the_result_screen_names_a_part_in_words_and_keeps_its_path_in_the_tooltip(seen):
    got = part(seen, "d2canned")
    shown = [(li["text"], li["title"]) for li in got["issues"]]
    assert shown == CANNED_ISSUES
    assert [(li["text"], li["title"]) for li in got["notes"]] == [
        ("部分保留：註腳: 「林志豪」", "部分保留：word/footnotes.xml: 「林志豪」"),
        ("遮一半保留：章節附註: 「X」", "遮一半保留：word/endnotes.xml: 「X」")]
    assert [(li["text"], li["title"]) for li in got["unchecked"]] == [
        ("圖片 image2.emf（不支援的圖片格式，未檢查）", "word/media/image2.emf（不支援的圖片格式，未檢查）"),
        ("內嵌物件 oleObject1.bin（內嵌物件，未檢查）", "word/embeddings/oleObject1.bin（內嵌物件，未檢查）"),
        ("內嵌物件 oleObject1.bin（程式改不到）", "word/embeddings/oleObject1.bin（程式改不到）")]


def test_the_notes_on_the_review_screen_name_a_part_in_words_too(seen):
    got = part(seen, "d2canned")
    assert [(li["text"], li["title"]) for li in got["panel"]] == [
        ("圖片 image2.emf 沒有檢查", "word/media/image2.emf 沒有檢查"),
        ("有 1 張圖片沒有文字", None),
        ("無法自動遮蔽：內嵌物件 oleObject1.bin（程式改不到）", "無法自動遮蔽：word/embeddings/oleObject1.bin（程式改不到）")]


def test_what_the_server_really_reports_for_a_part_left_unmasked_reads_in_words(seen):
    got = part(seen, "d2real")
    if "skipped" in got:
        pytest.skip(got["skipped"])
    assert [(li["text"], li["title"]) for li in got["notes"]] == [("部分保留：註腳: 「林志豪」", "部分保留：word/footnotes.xml: 「林志豪」")]


# ---- D3: a selection over two cells -------------------------------------------------------------------------------------

def test_a_selection_across_two_table_cells_offers_no_button_that_fails(seen):
    got = part(seen, "d3")
    assert got["crossText"] == "志豪 | 0922"
    assert got["crossButton"] is False, got
    assert got.get("crossDialogs", []) == []


def test_a_selection_inside_one_cell_still_offers_the_button_and_adds_it(seen):
    got = part(seen, "d3")
    assert got["oneText"] == "分機 2301" and got["oneButton"] is True
    assert got["oneDialogs"] == [] and got["oneListed"] is True


# ---- D4, D6: the restore page --------------------------------------------------------------------------------------------

def test_the_password_field_goes_when_another_restore_file_is_chosen(seen):
    got = part(seen, "d4")
    assert got["shown"] is True  # an old encrypted file asks for it
    assert got["fieldShownAfter"] is False and got["passwordAfter"] == "" and got["errorShownAfter"] is False
    assert got["restored"] == "王小明 已到場"  # and the new file restores without it


def test_the_explanation_for_a_pdf_reply_is_not_under_the_heading_for_what_was_not_read(seen):
    got = part(seen, "d6")
    pdf = got["pdf"]
    note = next(b for b in pdf if any(i.startswith("PDF 只能還原成文字") for i in b["items"]))
    assert note["heading"] != "沒有讀取的部分", pdf
    assert not any(b["heading"] == "沒有讀取的部分" for b in pdf), pdf  # nothing was left unread in that file


def test_what_was_not_read_stays_under_its_heading(seen):
    got = part(seen, "d6")["picture"]
    unread = next(b for b in got if b["heading"] == "沒有讀取的部分")
    assert unread["items"] == ["檔案裡有 1 張圖片，圖片裡的文字沒有讀取"], got


# ---- D5: formats on the home page ------------------------------------------------------------------------------------------

def test_both_cards_of_the_home_page_name_every_format_the_uploads_take():
    cards = re.findall(r'<section class="choice">(.*?)</section>', PAGE, re.S)
    assert len(cards) == 2
    for card in cards:
        words = re.sub(r"<[^>]+>", "", card)  # what a reader sees, not the accept= of the file input
        for ext in (".docx", ".xlsx", ".xlsm"):
            assert ext in words, (ext, words[:80])
        assert "PDF" in words
    assert ".xlsm" in re.search(r'id="restore-file"[^>]*accept="([^"]*)"', PAGE).group(1)


def test_the_home_page_as_shown_names_xlsm_in_both_cards(seen):
    cards = part(seen, "d5")["cards"]
    assert len(cards) == 2 and all(".xlsm" in c for c in cards), cards


# ---- D7: focus --------------------------------------------------------------------------------------------------------------

def test_focus_stays_on_the_same_control_after_the_list_is_drawn_again(seen):
    got = part(seen, "d7")
    fid = got["fid"]
    assert (got["afterCheckbox"]["fid"], got["afterCheckbox"]["type"]) == (fid, "checkbox") and not got["afterCheckbox"]["inDetail"], got
    assert (got["afterExpand"]["fid"], got["afterExpand"]["tag"], got["afterExpand"]["text"]) == (fid, "BUTTON", "收合"), got
    assert got["afterOccurrence"]["fid"] == fid and got["afterOccurrence"]["inDetail"] and got["afterOccurrence"]["type"] == "checkbox", got
    assert (got["afterCollapse"]["fid"], got["afterCollapse"]["text"]) == (fid, "展開"), got
    assert got["afterGroup"]["inHead"] and got["afterGroup"]["tag"] == "BUTTON" and got["afterGroup"]["group"] == got["groupOf"], got


# ---- the page asks nothing of anyone else -------------------------------------------------------------------------------------

def test_the_browser_asked_only_this_server_and_logged_no_page_error(seen, site):
    outside = [u for u in seen["requests"] if not u.startswith(site["base"]) and not u.startswith(("data:", "blob:"))]
    assert outside == []
    assert seen["pageErrors"] == []
