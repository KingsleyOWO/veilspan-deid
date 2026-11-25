'use strict';
// Front end for the de-identification app. Everything is built with DOM calls and textContent:
// document text is never parsed as markup.

const $ = (id) => document.getElementById(id);
const SOURCES = { model: '模型', agent: '代理', rule: '規則', face: '人臉偵測', vision: '看圖判斷', user: '手動', meta: '檔案屬性' };
const SCREENS = ['upload', 'working', 'failed', 'review', 'restore'];
const HINTS = {
  mask: '遮蔽清單列出被遮蔽的原始內容，本身含有個資，請妥善保管。下載遮蔽檔之後，伺服器上的檔案會在一段時間後自動刪除。',
  code: '代號版交給雲端，還原檔自己保管，兩種都要下載。代號版有檔案的話，檔案和文字擇一交給雲端即可。還原檔沒有加密，裡面就是代號和原本個資的對照表，拿到的人都看得到：不要和代號版一起傳到雲端。遮蔽清單列出被換成代號的原始內容，和還原檔一樣本身含有個資，請妥善保管，也不要傳到雲端。每次產生，同一個字串的代號可能不同：還原時要用和代號版同一次產生的還原檔（檔名的時間相同），之前下載的不能混用。下載代號版之後，伺服器上的檔案會在一段時間後自動刪除，還原檔之後就拿不到了。',
};
const HOME_FROM = ['working', 'failed', 'review'];  // the screens of a document that has its own way back to the home page
const state = { id: null, job: null, rev: null, timer: null, current: null, open: new Set(), units: {}, canvases: {}, pending: null,
  result: null, left: null };
const errors = new Map();

function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'data') Object.assign(node.dataset, v);
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, '');
    else if (v !== false && v != null) node.setAttribute(k, v);
  }
  for (const c of children) if (c != null) node.append(c);
  return node;
}

async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(url, opts); } catch (e) { return { ok: false, status: 0, data: { error: '無法連線到伺服器' } }; }
  let data = null;
  try { data = await res.json(); } catch (e) { data = {}; }
  // Any change to the findings may come back with the ranges that are really masked now (sent only while
  // something is masked in part): keep them, so the document pane shows which characters stay readable.
  if (res.ok && state.rev && data && method !== 'GET' && 'masks' in data) { state.rev.masks = data.masks; delete data.masks; }
  // The server throws away the last result whenever the findings change (its files no longer match): so does the page.
  if (res.ok && method !== 'GET' && /\/findings(\/|$)/.test(url)) forgetResult();
  return { ok: res.ok, status: res.status, data };
}

function show(name) {
  for (const s of SCREENS) $(s).hidden = s !== name;
  $('go-home').hidden = !HOME_FROM.includes(name);
  if (name === 'upload') renderLeft();
  hidePopups();
}

// Leaving a document for the home page deletes nothing: it stays on the server under the same address until it expires.
function goHome() {
  if (state.id && state.job) state.left = { id: state.id, name: state.job.filename };
  location.hash = '';
}

function renderLeft() {
  $('upload-left').hidden = !state.left;
  if (state.left) $('upload-left-name').textContent = t('「{name}」還保留在伺服器上，時間到了會自動刪除。', state.left);
}

function busy(source) {
  state.busy = source;
  $('busy-text').textContent = t(source || '');
  $('busy').hidden = !source;
}
function hidePopups() { $('selbtn').hidden = true; $('popover').hidden = true; state.pending = null; clearDraft(); }
const jobUrl = (rest) => `/api/jobs/${state.id}${rest || ''}`;

// A download is a navigation, so the browser writes the file to disk as it arrives and the page holds none of it. But
// a navigation that the server answers with an error would replace the app with that answer's JSON. So the same address
// is asked first from the page: the status line says whether a file is coming, and only then does the page go there
// for it. The first answer's body is dropped unread. Costs: one more request per download, and a file that goes
// away between the two requests still shows the server's JSON (the interval is milliseconds).
async function download(rest) {
  const url = jobUrl(rest);
  const selectedLanguage = language;
  const asked = new AbortController();
  let res;
  try { res = await fetch(url, { signal: asked.signal, cache: 'no-store' }); } catch (e) { return alert(t('無法連線到伺服器')); }
  if (!res.ok) {
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }  // an answer from something in between need not be JSON
    return alert(detailText(data?.error) || t('下載失敗'));
  }
  asked.abort();
  if (url === jobUrl(rest) && selectedLanguage === language) location.href = url;
}

// ---- upload and progress -------------------------------------------------------------------

async function upload(file, mode) {
  $('upload-error').hidden = true;
  if (!file) return;
  if (!/\.(docx|pdf|xlsx|xlsm)$/i.test(file.name)) return showError('upload-error', '目前只支援 .docx、.pdf、.xlsx、.xlsm');
  busy('上傳中…');
  const form = new FormData();
  form.append('file', file);
  form.append('mode', mode || 'mask');  // which of the two functions this document is for
  form.append('lang', language);
  const r = await api('POST', '/api/jobs', form);
  busy('');
  if (!r.ok) return showError('upload-error', r.data.error || '上傳失敗');
  location.hash = r.data.id;
}
function showError(id, source) {
  errors.set(id, source);
  $(id).textContent = detailText(source);
  $(id).hidden = false;
}

function stopPolling() { if (state.timer) { clearInterval(state.timer); state.timer = null; } }

async function load(id) {
  stopPolling();
  state.id = id;
  if (state.left && state.left.id === id) state.left = null;
  const r = await api('GET', jobUrl());
  if (!r.ok) {
    state.id = null;
    history.replaceState(null, '', location.pathname + location.search);
    show('upload');
    if (r.status === 404) showError('upload-error', '找不到這份文件，可能已經被刪除。請重新上傳。');
    return;
  }
  route(r.data);
}

function route(job) {
  state.job = job;
  if (job.status === 'review') return loadReview();
  renderJobStatus();
  if (job.status === 'failed') {
    stopPolling();
    $('failed-name').textContent = job.filename;
    return show('failed');
  }
  $('working-name').textContent = job.filename;
  $('working-bar').style.width = `${Math.round((job.progress || 0) * 100)}%`;
  if ($('working').hidden) show('working');
  if (!state.timer) {
    state.timer = setInterval(async () => {
      const r = await api('GET', jobUrl());
      if (r.ok) route(r.data); else if (r.status === 404) load(state.id);
    }, 1000);
  }
}

function renderJobStatus() {
  const job = state.job;
  if (!job) return;
  $('failed-error').textContent = detailText(job.error || '處理失敗');
  $('working-stage').textContent = detailText(job.stage || '排隊中');
}

// ---- review: document pane ------------------------------------------------------------------

async function loadReview() {
  stopPolling();
  const r = await api('GET', jobUrl('/review'));
  if (!r.ok) return load(state.id);
  state.rev = r.data;
  state.current = null;
  state.open = new Set();
  state.code = r.data.job.mode === 'code';  // codes instead of masks: no partial style, another output
  $('review-name').textContent = r.data.job.filename;
  renderReviewLabels('姓名');
  buildDoc();
  renderPanels();
  renderAll();
  show('review');
  // The server keeps the last result until the findings change: show it again, as after any return to this screen.
  state.result = r.data.job.result || null;
  if (state.result) showResult(state.result); else { $('result').hidden = true; syncResultButton(); }
}

function renderReviewLabels(chosen) {
  $('btn-apply').textContent = t(state.code ? '產生代號版' : '產生遮蔽檔');
  $('add-text-input').placeholder = t(state.code ? '新增要換成代號的文字（全文）' : '新增要遮蔽的文字（全文）');
  fillTypes($('add-text-type'), chosen);
}

function fillTypes(select, chosen) {
  select.replaceChildren(...state.rev.types.map((type) => el('option', { value: type, text: t(type), selected: type === chosen })));
}

function imageLabel(id) {
  const im = state.rev.images[id];
  if (!im) return id;
  return state.rev.kind === 'pdf' ? detailText(im.name) : t('圖片 {number}', { number: id.replace('img', '') });
}

function buildDoc() {
  const rev = state.rev;
  const doc = $('doc');
  doc.replaceChildren();
  state.units = {};
  state.canvases = {};
  let section = null;
  // Text that tracked changes deleted is still in the file, so it is checked and masked like the rest. It is not shown
  // in the paragraph it came from but in a section of its own after everything else (design 5), one block per deletion.
  const deleted = [];
  for (const b of rev.view) {
    if (b.type === 'text') {
      if (b.section !== section) { section = b.section; doc.append(el('h3', { class: 'section-title', text: detailText(section) })); }
      const p = el('p', { class: 'unit', data: { unit: b.unit } });
      const unit = { node: p, del: b.del || [], text: rev.units[b.unit] ? rev.units[b.unit].text : '', delNodes: [], segs: null };
      state.units[b.unit] = unit;
      for (const [start] of unit.del) {
        const node = el('p', { class: 'unit', data: { unit: b.unit, base: start } });  // its text starts at `start` in the unit
        unit.delNodes.push(node);
        deleted.push(el('div', { class: 'del-item' }, el('span', { class: 'tag', text: b.section }), node));
      }
      doc.append(p);
      continue;
    }
    const im = rev.images[b.image];
    if (!im) continue;
    if (b.type === 'page') section = null;
    const fig = el('div', { class: 'figure' });
    const cap = el('div', { class: 'cap' }, el('span', { text: imageLabel(b.image) }));
    if (im.supported && im.url) {
      cap.append(el('button', { class: 'link', text: b.type === 'page' ? t('整頁遮蔽') : t('整張遮蔽'), onclick: () => addRegion(b.image, [0, 0, im.width, im.height], '其他') }));
      const canvas = el('div', { class: `canvas ${b.type === 'page' ? 'page' : ''}`, data: { image: b.image } },
        el('img', { src: im.url, alt: imageLabel(b.image), loading: 'lazy', width: im.width, height: im.height, draggable: 'false' }));
      canvas.addEventListener('pointerdown', startDraft);
      state.canvases[b.image] = canvas;
      fig.append(cap, canvas);
    } else {
      fig.append(cap, el('div', { class: 'missing', text: t('{name}：這個圖片格式無法檢查，請自行確認內容。', { name: im.name }) }));
    }
    doc.append(fig);
  }
  if (deleted.length) {
    doc.append(el('h3', { class: 'section-title', text: t('已刪除的修訂文字') }),
      el('p', { class: 'hint', text: t('這些是追蹤修訂中已刪除的文字，仍然留在檔案裡，所以也要檢查；在這裡一樣可以選取文字來新增，或開關項目。') }),
      ...deleted);
  }
  if (!rev.view.length) doc.append(el('p', { class: 'empty', text: t('這份文件沒有可顯示的內容。') }));
}

function renderMarks() {
  const byUnit = {};
  const boxes = [];
  for (const f of state.rev.findings) {
    f.occurrences.forEach((o, i) => {
      const active = f.on && o.on;
      if (o.kind === 'text') {
        if (state.units[o.unit]) (byUnit[o.unit] = byUnit[o.unit] || []).push({ s: o.start, e: o.end, fid: f.id, active });
        for (const [image, box] of o.page_boxes || []) boxes.push({ image, box, fid: f.id, active });
      } else if (o.kind === 'box' && o.box) {
        boxes.push({ image: o.unit, box: o.box, fid: f.id, active, index: i });
      }
    });
  }
  for (const [uid, unit] of Object.entries(state.units)) {
    const refs = byUnit[uid] || [];
    const text = unit.text;
    if (!refs.length && !unit.del.length) { unit.node.textContent = text; continue; }
    const cuts = new Set([0, text.length]);
    for (const r of refs) { cuts.add(Math.max(0, r.s)); cuts.add(Math.min(text.length, r.e)); }
    // Once something is masked in part, the server says which characters are really masked in each unit.
    const masks = state.rev.masks && !state.code ? (state.rev.masks[uid] || []) : null;
    for (const [s, e] of masks || []) { cuts.add(Math.max(0, s)); cuts.add(Math.min(text.length, e)); }
    for (const [s, e] of unit.del) { cuts.add(s); cuts.add(e); }
    const points = [...cuts].sort((a, b) => a - b);
    const parts = [];  // what the paragraph shows
    const held = unit.del.map(() => []);  // what each block of deleted text shows
    const segs = [];  // the paragraph without the deleted text: [position in what it shows, position in the unit, length]
    for (let i = 0; i + 1 < points.length; i++) {
      const a = points[i], b = points[i + 1];
      const piece = text.slice(a, b);
      const cover = refs.filter((r) => r.s <= a && r.e >= b);
      const gone = unit.del.findIndex(([s, e]) => s <= a && e >= b);  // which deleted block this piece belongs to, or -1
      const deleted = gone >= 0;
      let node;
      if (cover.length) {
        const lead = cover.find((r) => r.active) || cover[0];
        const kept = lead.active && masks && !masks.some(([s, e]) => s <= a && e >= b);
        const cls = `${lead.active ? 'on' : 'off'}${kept ? ' kept' : ''}${deleted ? ' del' : ''}${state.current === lead.fid ? ' cur' : ''}`;
        node = el('mark', { class: cls, text: piece, data: { fid: lead.fid }, title: deleted ? t('已刪除的修訂文字') : kept ? t('保留的字（遮一半或自訂）') : null });
      } else if (deleted) {
        node = el('span', { class: 'del', text: piece, title: t('已刪除的修訂文字') });
      } else {
        node = document.createTextNode(piece);
      }
      if (deleted) { held[gone].push(node); continue; }
      parts.push(node);
      const last = segs[segs.length - 1];
      if (last && last[1] + last[2] === a) last[2] += b - a;
      else segs.push([segs.reduce((n, g) => n + g[2], 0), a, b - a]);
    }
    unit.node.replaceChildren(...parts);
    unit.delNodes.forEach((n, k) => n.replaceChildren(...held[k]));
    unit.node.hidden = unit.del.length > 0 && !parts.length;  // a paragraph that was deleted whole has nothing left to show
    unit.segs = unit.del.length ? segs : null;
  }
  for (const canvas of Object.values(state.canvases)) canvas.querySelectorAll('.box:not(.draft)').forEach((n) => n.remove());
  for (const b of boxes) {
    const canvas = state.canvases[b.image];
    const im = state.rev.images[b.image];
    if (!canvas || !im || !im.width || !im.height) continue;
    const [x0, y0, x1, y1] = b.box;
    const node = el('div', { class: `box ${b.active ? 'on' : 'off'}`, data: { fid: b.fid } });
    node.style.left = `${(x0 / im.width) * 100}%`;
    node.style.top = `${(y0 / im.height) * 100}%`;
    node.style.width = `${((x1 - x0) / im.width) * 100}%`;
    node.style.height = `${((y1 - y0) / im.height) * 100}%`;
    canvas.append(node);
  }
}

// ---- review: list pane ----------------------------------------------------------------------

function describe(f) {
  if (f.kind === 'meta') return t('{label}：{value}', { label: detailText(f.reason), value: f.text });
  if (f.kind === 'region') {
    const o = f.occurrences[0];
    const im = state.rev.images[o.unit] || {};
    const whole = o.box && o.box[0] <= 0 && o.box[1] <= 0 && o.box[2] >= im.width && o.box[3] >= im.height;
    return t(whole ? '{image}（整張）' : '{image} 的區域', { image: imageLabel(o.unit) });
  }
  return f.text;
}

function snippet(o) {
  if (o.kind === 'text' || o.kind === 'meta') {
    const unit = state.rev.units[o.unit];
    if (!unit) return [o.kind === 'meta' ? t('檔案屬性') : o.unit];
    const text = unit.text;
    const gone = (state.units[o.unit]?.del || []).some(([s, e]) => s <= o.start && o.end <= e);
    return [t('{label}：{value}', { label: gone ? t('已刪除的修訂文字') : detailText(unit.section), value: `…${text.slice(Math.max(0, o.start - 12), o.start)}` }),
      el('b', { text: text.slice(o.start, o.end) }), `${text.slice(o.end, o.end + 12)}…`];
  }
  return [t('{image} 內的文字', { image: imageLabel(o.unit) })];
}

// Drawing the list again replaces every control in it, and keyboard focus would fall back to the page. So the control that
// has it is noted before (the entry or group it belongs to, and its place among the same kind of control there) and
// the one in the same place gets it afterwards.
const FOCUSABLE = 'input, select, button';
const kindOf = (n) => `${n.tagName}|${n.type || ''}|${n.getAttribute('aria-label') || ''}|${n.className}`;
function focusSpot(list) {
  const node = document.activeElement;
  if (!node || !list.contains(node) || !node.matches(FOCUSABLE)) return null;
  const owner = node.closest('.item') || node.closest('.group-head');
  if (!owner) return null;
  const same = [...owner.querySelectorAll(FOCUSABLE)].filter((n) => kindOf(n) === kindOf(node));
  return { fid: owner.dataset.fid || null, type: owner.closest('.group').dataset.type, kind: kindOf(node), n: same.indexOf(node) };
}
function restoreFocus(list, spot) {
  if (!spot) return;
  const owner = spot.fid ? list.querySelector(`.item[data-fid="${spot.fid}"]`)
    : [...list.querySelectorAll('.group')].filter((g) => g.dataset.type === spot.type).map((g) => g.querySelector('.group-head'))[0];
  if (!owner) return;
  const same = [...owner.querySelectorAll(FOCUSABLE)].filter((n) => kindOf(n) === spot.kind);
  const target = same[spot.n] || same[same.length - 1];
  if (target) target.focus({ preventScroll: true });
}

function renderList() {
  const list = $('list');
  const keep = list.scrollTop;
  const spot = focusSpot(list);
  const groups = new Map();
  for (const f of state.rev.findings) { if (!groups.has(f.type)) groups.set(f.type, []); groups.get(f.type).push(f); }
  const order = state.rev.types.filter((t) => groups.has(t)).concat([...groups.keys()].filter((t) => !state.rev.types.includes(t)));
  const nodes = [];
  for (const type of order) {
    const items = groups.get(type);
    const on = items.filter((f) => f.on).length;
    const actions = el('span', { class: 'group-actions' });
    if (!state.code && (state.rev.partial_types || []).includes(type)) {
      const partial = (state.rev.styles || {})[type] === 'partial';
      actions.append(el('button', { class: 'link', text: partial ? t('改為全遮') : t('遮一半'), title: partial ? t('這一類改回整筆遮蔽') : t('這一類只遮一部分的字'),
        onclick: () => bulkStyle(type, partial ? 'full' : 'partial') }));
    }
    actions.append(el('button', { class: 'link', text: on ? t('全部關閉') : t('全部開啟'), onclick: () => bulk(type, !on) }));
    const head = el('div', { class: 'group-head' },
      el('span', {}, t(type), el('span', { class: 'count', text: t('啟用 {on} / {total}', { on, total: items.length }) })), actions);
    nodes.push(el('div', { class: 'group', data: { type } }, head, ...items.map(renderItem)));
  }
  if (!nodes.length) nodes.push(el('p', { class: 'empty', text: t('沒有找到需要遮蔽的項目。可以在左邊選取文字或在圖片上拉框來新增。') }));
  list.replaceChildren(...nodes);
  list.scrollTop = keep;
  restoreFocus(list, spot);
  $('partial-note').hidden = state.code || !state.rev.findings.some((f) => (f.style === 'partial' || f.style === 'custom') && f.on);
  const chips = Object.entries(state.rev.findings.filter((f) => f.on).reduce((acc, f) => { acc[f.type] = (acc[f.type] || 0) + 1; return acc; }, {}));
  $('review-summary').replaceChildren(...(chips.length ? chips.map(([type, n]) => el('span', { class: 'chip', text: `${t(type)} ${n}` })) : [el('span', { class: 'chip', text: t('沒有啟用的項目') })]));
}

function renderItem(f) {
  const total = f.occurrences.length;
  const active = f.on ? f.occurrences.filter((o) => o.on).length : 0;
  const open = state.open.has(f.id);
  const item = el('div', { class: `item${f.on ? '' : ' is-off'}${state.current === f.id ? ' current' : ''}`, data: { fid: f.id } });
  const box = el('input', { type: 'checkbox', 'aria-label': t('遮蔽 {item}', { item: describe(f) }) });
  box.checked = f.on;
  box.addEventListener('change', () => patch(f.id, { on: box.checked }));
  const meta = el('div', { class: 'item-meta' }, el('span', { class: 'tag', text: t(SOURCES[f.source] || f.source) }));
  if (f.kind !== 'region') meta.append(el('span', { text: f.on && active < total
    ? t('{active} / {count} 處', { active, count: total }) : t('{count} 處', { count: total }) }));
  if ((f.reason || '').startsWith('代理存疑')) meta.append(el('span', { class: 'tag doubt', text: t('代理存疑') }));
  if (f.style === 'partial' && !state.code) meta.append(el('span', { class: 'tag partial', text: f.fits ? t('遮一半') : t('遮一半不適用，全遮') }));
  if (f.style === 'custom' && !state.code) meta.append(el('span', { class: 'tag partial', text: t('自訂') }));
  if (f.occurrences.some((o) => o.fuzzy)) meta.append(el('span', { class: 'tag fuzzy', text: t('含相似比對') }));
  const same = samePerson(f);
  if (same.length) meta.append(el('span', { class: 'tag same', text: t('同一人：{names}', { names: same.map((x) => x.text).join(language === 'en' ? ', ' : '、') }),
    title: t('這幾個寫法會用同一個號碼的代號') }));
  meta.append(el('button', { class: 'link', text: open ? t('收合') : t('展開'), onclick: () => { open ? state.open.delete(f.id) : state.open.add(f.id); renderList(); } }));
  item.append(el('div', { class: 'item-main' }, box,
    el('div', { class: 'item-text', onclick: () => focusFinding(f.id, true) }, el('div', { text: describe(f) }),
      f.preview && !state.code ? el('div', { class: 'preview', text: `→ ${f.preview}`, title: t('遮蔽後的樣子') }) : null, meta)));
  if (f.reason && f.kind !== 'meta') item.append(el('div', { class: 'reason', text: detailText(f.reason) }));
  if (open) item.append(renderDetail(f));
  return item;
}

// The code function: names said to be one person share a number ([姓名1], [姓名1b]).
function samePerson(f) {
  return state.code && f.group ? state.rev.findings.filter((x) => x.group === f.group && x.id !== f.id) : [];
}

async function setSamePerson(fid, other) {
  const r = await api('POST', jobUrl(`/findings/${fid}/group`), { with: other });
  if (!r.ok) alert(detailText(r.data.error) || t('更新失敗'));
  await refreshFindings();
}

function renderDetail(f) {
  const detail = el('div', { class: 'detail' });
  const select = el('select', { 'aria-label': t('類型') });
  fillTypes(select, f.type);
  select.addEventListener('change', () => patch(f.id, { type: select.value }));
  const row = el('div', { class: 'row' }, el('label', {}, t('類型') + ' ', select));
  if ((f.partial_ok || f.custom_ok) && !state.code) {
    const style = el('select', { 'aria-label': t('遮法') },
      el('option', { value: 'full', text: t('全遮'), selected: f.style !== 'partial' && f.style !== 'custom' }),
      f.partial_ok ? el('option', { value: 'partial', text: t('遮一半'), selected: f.style === 'partial' }) : null,
      f.custom_ok ? el('option', { value: 'custom', text: t('自訂'), selected: f.style === 'custom' }) : null);
    style.addEventListener('change', () => patch(f.id, { style: style.value }));
    row.append(el('label', {}, t('遮法') + ' ', style));
  }
  if (f.source === 'user') row.append(el('button', { text: t('刪除這筆'), onclick: () => removeFinding(f.id) }));
  detail.append(row);
  if (f.style === 'custom' && f.custom_ok && !state.code) detail.append(renderPicker(f));
  if (state.code && f.kind === 'text' && f.type === '姓名' && f.on) {
    const others = state.rev.findings.filter((x) => x.kind === 'text' && x.type === '姓名' && x.on && x.id !== f.id && !(f.group && x.group === f.group));
    const who = el('div', { class: 'row' });
    if (others.length) {
      const pick = el('select', { 'aria-label': t('和哪一筆是同一人') }, el('option', { value: '', text: t('和…是同一人') }),
        ...others.map((x) => el('option', { value: x.id, text: x.text })));
      pick.addEventListener('change', () => { if (pick.value) setSamePerson(f.id, pick.value); });
      who.append(el('label', {}, t('同一人') + ' ', pick));
    }
    if (f.group) who.append(el('button', { text: t('移出這一組'), onclick: () => setSamePerson(f.id, null) }));
    if (who.childNodes.length) detail.append(who);
  }
  if (f.kind !== 'region') {
    f.occurrences.forEach((o, i) => {
      const cb = el('input', { type: 'checkbox', 'aria-label': t('這一處') });
      cb.checked = f.on && o.on;
      cb.addEventListener('change', () => patch(f.id, { occurrence: { index: i, on: cb.checked } }));
      const line = el('span', { onclick: () => focusOccurrence(f, o) }, ...snippet(o));
      detail.append(el('div', { class: 'occ' }, cb, line, o.fuzzy ? el('span', { class: 'tag fuzzy', text: t('相似') }) : null));
    });
  }
  return detail;
}

// Custom masking: one button per character of the entry, pressed = masked. Positions count characters the way
// the server does (code points), so a character outside the basic plane is one position here too.
function renderPicker(f) {
  const chars = Array.from(f.text);
  const hidden = chars.map(() => false);
  for (const [s, e] of f.spans || []) for (let i = Math.max(0, s); i < e && i < chars.length; i++) hidden[i] = true;
  const box = el('div', { class: 'picker', role: 'group', 'aria-label': t('自訂遮法：點一個字切換遮或留下') });
  chars.forEach((c, i) => {
    if (/\s/.test(c)) { box.append(el('span', { class: 'gap', text: ' ' })); return; }
    box.append(el('button', { type: 'button', class: `ch${hidden[i] ? ' masked' : ''}`, 'aria-pressed': String(hidden[i]), text: c,
      title: hidden[i] ? t('會遮掉。點一下改成留下') : t('會留下。點一下改成遮掉'),
      onclick: () => { hidden[i] = !hidden[i]; patch(f.id, { spans: runsOf(hidden) }); } }));
  });
  return el('div', { class: 'pick' }, el('div', { class: 'hint', text: t('點一個字切換：深色的字會遮掉，其他的留下。') }), box);
}

function runsOf(flags) {
  const out = [];
  flags.forEach((on, i) => {
    if (!on) return;
    if (out.length && out[out.length - 1][1] === i) out[out.length - 1][1] = i + 1; else out.push([i, i + 1]);
  });
  return out;
}

function renderPanels() {
  const rev = state.rev;
  const notes = rev.warnings.map(detailText);
  for (const u of rev.unmaskable || []) notes.push(t('無法自動遮蔽：{where}（{reason}）', { where: detailText(u.where), reason: detailText(u.reason) }));
  const w = $('warnings');
  w.hidden = !notes.length;
  w.querySelector('summary').textContent = t('注意事項（{count}）：有些部分需要你自己確認', { count: notes.length });
  w.querySelector('ul').replaceChildren(...notes.map(line));  // a part's internal path reads in words here too
  const a = $('agent-log');
  const out = rev.outcome || {};
  a.hidden = !out.agent;
  if (out.agent) {
    const summary = t('代理檢查紀錄：{count} 步', { count: out.steps || 0 });
    a.querySelector('summary').textContent = out.finished ? summary : t('{summary}，{reason}', { summary, reason: detailText(out.fallback_reason || '未自行結束') });
    a.querySelector('ol').replaceChildren(...rev.log.map((e) => el('li', {}, t('第 {step} 步 {tool}（{seconds} 秒）', e) + ' ', el('code', { text: `${e.args} → ${e.result}` }))));
  }
}

function renderAll() { renderMarks(); renderList(); }

function replaceFinding(f) {
  const i = state.rev.findings.findIndex((x) => x.id === f.id);
  if (i >= 0) state.rev.findings[i] = f; else state.rev.findings.push(f);
}

async function patch(fid, body) {
  const r = await api('PATCH', jobUrl(`/findings/${fid}`), body);
  if (r.ok) replaceFinding(r.data); else alert(detailText(r.data.error) || t('更新失敗'));
  renderAll();
  await followDerived(r);
}

// A change can alter which entries exist: masking a spreadsheet cell brings up the cells computed from it, and
// switching off a mailbox brings back what the agent added inside it. Fetch the list again.
async function followDerived(r) {
  if (r.ok && r.data && (r.data.derived || r.data.relist)) await refreshFindings();
}

async function refreshFindings() {
  const r = await api('GET', jobUrl('/review'));
  if (r.ok) { state.rev.findings = r.data.findings; state.rev.masks = r.data.masks; state.rev.styles = r.data.styles; renderAll(); }
}

async function bulkStyle(type, style) {
  const r = await api('POST', jobUrl('/findings/bulk'), { type, style });
  if (!r.ok) alert(detailText(r.data.error) || t('更新失敗'));
  await refreshFindings();
}

async function bulk(type, on) {
  const r = await api('POST', jobUrl('/findings/bulk'), { type, on });
  if (!r.ok) alert(detailText(r.data.error) || t('更新失敗'));
  await refreshFindings();
}

async function removeFinding(fid) {
  const r = await api('DELETE', jobUrl(`/findings/${fid}`));
  if (!r.ok) return alert(detailText(r.data.error) || t('刪除失敗'));
  state.rev.findings = state.rev.findings.filter((f) => f.id !== fid);
  if (state.current === fid) state.current = null;
  renderAll();
  await followDerived(r);
}

function flash(node) {
  if (!node) return;
  node.scrollIntoView({ block: 'center', behavior: 'smooth' });
  node.classList.remove('flash');
  void node.offsetWidth;
  node.classList.add('flash');
}

function focusFinding(fid, scrollDoc) {
  state.current = fid;
  document.querySelectorAll('.item.current').forEach((n) => n.classList.remove('current'));
  const item = document.querySelector(`.item[data-fid="${fid}"]`);
  if (item) { item.classList.add('current'); if (!scrollDoc) item.scrollIntoView({ block: 'nearest' }); }
  if (scrollDoc) flash($('doc').querySelector(`[data-fid="${fid}"]`));
}

function focusOccurrence(f, o) {
  state.current = f.id;
  let node = null;
  if (o.kind === 'text' && state.units[o.unit]) {
    const u = state.units[o.unit];
    const k = u.del.findIndex(([s, e]) => s <= o.start && o.start < e);  // in deleted text: that block, not the paragraph
    const host = k >= 0 ? u.delNodes[k] : u.node;
    node = host.querySelector(`mark[data-fid="${f.id}"]`) || host;
  }
  else if (o.kind === 'box' || (o.page_boxes || []).length) {
    const image = o.kind === 'box' ? o.unit : o.page_boxes[0][0];
    node = state.canvases[image] ? state.canvases[image].querySelector(`.box[data-fid="${f.id}"]`) || state.canvases[image] : null;
  }
  flash(node || $('doc').querySelector(`[data-fid="${f.id}"]`));
}

// ---- adding by hand -------------------------------------------------------------------------

function placeNear(node, rect) {
  node.hidden = false;
  const w = node.offsetWidth, h = node.offsetHeight;
  let left = rect.left + window.scrollX, top = rect.bottom + window.scrollY + 6;
  left = Math.max(8, Math.min(left, window.innerWidth - w - 8));
  if (top + h > window.innerHeight - 8) top = Math.max(8, rect.top + window.scrollY - h - 6);
  node.style.left = `${left}px`;
  node.style.top = `${top}px`;
}

function selectionInUnit() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
  const range = sel.getRangeAt(0);
  const unit = (n) => (n.nodeType === 1 ? n : n.parentElement).closest('.unit');
  const a = unit(range.startContainer), b = unit(range.endContainer);
  if (!a || a !== b) return null;
  const before = document.createRange();
  before.setStart(a, 0);
  before.setEnd(range.startContainer, range.startOffset);
  const shown = before.toString().length;  // characters of this block before the selection
  let text = range.toString();
  if (!text.trim()) return null;
  // A block of deleted text starts somewhere inside its unit (data-base).
  let start = Number(a.dataset.base || 0) + shown, end = start + text.length;
  const u = state.units[a.dataset.unit];
  if (u && u.segs && a === u.node) {
    // A paragraph with its deleted text taken out: positions in what is shown are not positions in the unit. A
    // selection across a deletion takes the deleted text with it, as it did while that was shown in the paragraph.
    start = unitPosition(u.segs, shown, false);
    end = unitPosition(u.segs, shown + text.length, true);
    text = u.text.slice(start, end);
  }
  if (crossesCells((state.rev.units[a.dataset.unit] || {}).text || '', start, end)) return null;
  return { unit: a.dataset.unit, start, end, text, rect: range.getBoundingClientRect() };
}

// A table row (Word) or a spreadsheet row is one unit whose cells are joined by " | ", and that is not in the file:
// no piece of text runs over it, so a selection that takes in the bar cannot be found, or masked, as one string. The
// page cannot tell such a bar from one the text itself holds, so a selection over either gets no button.
function crossesCells(text, start, end) {
  for (let i = text.indexOf(' | '); i >= 0 && i < end; i = text.indexOf(' | ', i + 1)) {
    if (start <= i + 1 && i + 1 < end) return true;
  }
  return false;
}

// Position in the unit of a position in the paragraph without its deleted text (segments: see renderMarks).
// A start belongs to the segment that begins there, an end to the one that ends there.
function unitPosition(segs, p, atEnd) {
  for (const [pos, orig, len] of segs) {
    if (atEnd ? p > pos && p <= pos + len : p >= pos && p < pos + len) return orig + (p - pos);
  }
  const last = segs[segs.length - 1];
  return last ? last[1] + last[2] : 0;
}

function onSelection() {
  if (!$('popover').hidden) return;
  const s = selectionInUnit();
  if (!s) { $('selbtn').hidden = true; return; }
  state.pending = { kind: 'text', ...s };
  placeNear($('selbtn'), s.rect);
}

function openPopover(title, rect, withAll, type) {
  $('selbtn').hidden = true;
  $('popover-title').textContent = title;
  fillTypes($('popover-type'), type);
  $('popover-all-row').hidden = !withAll;
  $('popover-all').checked = true;
  placeNear($('popover'), rect);
  $('popover-type').focus();
}

async function confirmPopover() {
  const p = state.pending;
  if (!p) return hidePopups();
  const type = $('popover-type').value;
  if (p.kind === 'region') {
    await addRegion(p.image, p.box, type);
  } else {
    const r = await api('POST', jobUrl('/findings'), { kind: 'text', text: p.text, type, all: $('popover-all').checked, unit: p.unit, start: p.start, end: p.end });
    if (r.ok) { replaceFinding(r.data); state.current = r.data.id; } else alert(detailText(r.data.error) || t('新增失敗'));
    window.getSelection().removeAllRanges();
    hidePopups();
    renderAll();
    return followDerived(r);
  }
  hidePopups();
  renderAll();
}

async function addRegion(image, box, type) {
  const r = await api('POST', jobUrl('/findings'), { kind: 'region', image, box, type });
  if (r.ok) { replaceFinding(r.data); state.current = r.data.id; renderAll(); } else alert(detailText(r.data.error) || t('新增失敗'));
}

let draft = null;
function clearDraft() { if (draft && draft.node) draft.node.remove(); draft = null; }

function startDraft(ev) {
  if (ev.button !== 0) return;
  const hit = ev.target.closest('.box');
  if (hit) { focusFinding(hit.dataset.fid, false); return; }
  hidePopups();
  const canvas = ev.currentTarget;
  const rect = canvas.getBoundingClientRect();
  draft = { canvas, rect, x0: ev.clientX - rect.left, y0: ev.clientY - rect.top, node: el('div', { class: 'box draft' }) };
  canvas.append(draft.node);
  canvas.setPointerCapture(ev.pointerId);
  canvas.addEventListener('pointermove', moveDraft);
  canvas.addEventListener('pointerup', endDraft, { once: true });
  ev.preventDefault();
}

function draftBox(ev) {
  const x = Math.max(0, Math.min(draft.rect.width, ev.clientX - draft.rect.left));
  const y = Math.max(0, Math.min(draft.rect.height, ev.clientY - draft.rect.top));
  return [Math.min(draft.x0, x), Math.min(draft.y0, y), Math.max(draft.x0, x), Math.max(draft.y0, y)];
}

function moveDraft(ev) {
  if (!draft) return;
  const [x0, y0, x1, y1] = draftBox(ev);
  Object.assign(draft.node.style, { left: `${x0}px`, top: `${y0}px`, width: `${x1 - x0}px`, height: `${y1 - y0}px` });
}

function endDraft(ev) {
  if (!draft) return;
  const { canvas, rect } = draft;
  canvas.removeEventListener('pointermove', moveDraft);
  const [x0, y0, x1, y1] = draftBox(ev);
  if (x1 - x0 < 6 || y1 - y0 < 6) return clearDraft();
  const im = state.rev.images[canvas.dataset.image];
  const sx = im.width / rect.width, sy = im.height / rect.height;
  state.pending = { kind: 'region', image: canvas.dataset.image, box: [x0 * sx, y0 * sy, x1 * sx, y1 * sy].map((v) => Math.round(v)) };
  openPopover(t('遮蔽這個區域'), draft.node.getBoundingClientRect(), false, '其他');
}

async function addTextEverywhere(ev) {
  ev.preventDefault();
  const input = $('add-text-input');
  const err = $('add-text-error');
  err.hidden = true;
  const text = input.value.trim();
  if (!text) return;
  const r = await api('POST', jobUrl('/findings'), { kind: 'text', text, type: $('add-text-type').value, all: true });
  if (!r.ok) return showError('add-text-error', r.data.error || '新增失敗');
  input.value = '';
  replaceFinding(r.data);
  renderAll();
  await followDerived(r);
  focusFinding(r.data.id, true);
}

// ---- output ---------------------------------------------------------------------------------

async function applyMasks() {
  busy('正在產生遮蔽檔並驗證…');
  const r = await api('POST', jobUrl('/apply'));
  busy('');
  if (!r.ok) return alert(detailText(r.data.error) || t('產生失敗'));
  showResult(r.data);
}

// The code function: the text with codes, the table that maps them back and, for a Word or Excel document,
// the document itself with the codes in it. No password is asked for.
const FILE_KINDS = { '.docx': 'Word ', '.xlsx': 'Excel ', '.xlsm': 'Excel ' };
// The coded text says this in its first line; a file has no such line, so whoever sends the file says it.
const FILE_ADVICE = '把代號版檔案交給雲端時，請在提問裡加一句：「方括號代號（例如 [姓名1]）請原樣保留，不要翻譯、改寫或刪除。」'
  + '檔案屬性、隱藏內容、內嵌檔案和工作表名稱在檔案裡維持遮蔽，不寫代號（代號版文字也不含內嵌檔案的內容）。';

async function exportCodes() {
  const lang = language;
  busy('正在產生代號版…');
  const r = await api('POST', jobUrl(`/export?lang=${lang}`));
  busy('');
  if (!r.ok) return alert(detailText(r.data.error) || t('產生失敗'));
  if (lang === 'en' && r.data.lang !== 'en') return alert(t('這個伺服器版本無法匯出英文代號。'));
  showResult(r.data);
}

function resultCopy(res) {
  const s = res.stats || {};
  const parts = [];
  if (state.code) {
    parts.push(t('共使用 {count} 個代號。', { count: s.codes || 0 }));
    if (s.blanked) parts.push(t('另有 {count} 處圖片裡的文字寫成 [已遮蔽]（不能還原）。', { count: s.blanked }));
    if (res.file) parts.push(t('代號版 {format}檔裡寫入了 {count} 處代號。', { format: FILE_KINDS[res.file.ext] || '', count: res.file.placed }));
    parts.push(t(res.ok ? '在代號版文字裡找不到任何要換掉的內容。' : '下列內容在代號版文字裡仍然找得到：'));
  } else {
    parts.push(t('已遮蔽 {texts} 處文字、{boxes} 個圖片區域、{meta} 個檔案屬性。', { texts: s.text_spans || 0, boxes: s.boxes || 0, meta: s.meta || 0 }));
    parts.push(t(res.ok ? '重新檢查輸出檔後，找不到任何已遮蔽的內容。' : '下列內容在輸出檔中仍然找得到，或遮蔽沒有生效：'));
  }
  return parts.join(language === 'en' ? ' ' : '');
}

// ---- where a part of the file is, in words ----------------------------------------------------
// The checks name a place by the internal path inside the file (word/footnotes.xml; after a "!" the path inside an
// embedded file). A colleague knows these places by the words Word and Excel use; the path stays in the tooltip.
// A path that has no name here is shown as it is.
const baseName = (p) => p.slice(p.lastIndexOf('/') + 1);
const PART_NAMES = [
  [/^word\/document\.xml$/, '內文'],
  [/^word\/header\d*\.xml$/, '頁首'],
  [/^word\/footer\d*\.xml$/, '頁尾'],
  [/^word\/footnotes\.xml$/, '註腳'],
  [/^word\/endnotes\.xml$/, '章節附註'],
  [/^word\/comments(?:Extended|Ids|Extensible)?\.xml$/, '註解'],
  [/^(?:word\/people|xl\/persons\/[^/]+)\.xml$/, '註解與修訂的作者資料'],
  [/^word\/(?:charts|diagrams)\//, '圖表'],
  [/^word\/glossary\//, '建置組塊'],
  [/^word\/(?:settings|webSettings)\.xml$/, '文件設定'],
  [/^(?:word|xl)\/styles\.xml$/, '樣式'],
  [/^docProps\/core\.xml$/, '檔案屬性'],
  [/^docProps\/app\.xml$/, '檔案屬性（應用程式資訊）'],
  [/^docProps\/custom\.xml$/, '自訂檔案屬性'],
  [/^docProps\/thumbnail\./, '首頁縮圖'],
  [/^customXml\//, '自訂 XML 資料'],
  [/(?:^|\/)_rels\/[^/]*\.rels$/, '超連結與外部連結'],
  [/vbaProject\.bin$/, '巨集'],
  [/^(?:word|xl)\/media\/[^/]+$/, (p, bare) => (bare ? '' : `${t('圖片')} `) + baseName(p)],
  [/^(?:word|xl)\/embeddings\/[^/]+$/, (p, bare) => (bare ? '' : (/\.(?:docx|docm|xlsx|xlsm|pptx|pdf)$/i.test(p) ? `${t('內嵌檔案')} ` : `${t('內嵌物件')} `)) + baseName(p)],
  [/^xl\/worksheets\/sheet\d+\.xml$/, (p) => t('工作表（{name}）', { name: baseName(p) })],  // the number is all that tells two sheets apart here
  [/^xl\/sharedStrings\.xml$/, '儲存格文字'],
  [/^xl\/workbook\.xml$/, '活頁簿設定（工作表名稱、定義的名稱）'],
  [/^xl\/(?:comments\d*|threadedComments\/[^/]+)\.xml$/, '註解'],
  [/^xl\/drawings\//, '圖形與文字方塊'],
  [/^xl\/charts\//, '圖表'],
  [/^xl\/tables\//, '表格定義'],
  [/^xl\/pivotTables\//, '樞紐分析表'],
  [/^xl\/pivotCache\//, '樞紐分析快取'],
  [/^xl\/externalLinks\//, '外部活頁簿連結'],
  [/^xl\/connections\.xml$/, '資料連線'],
];
// `bare`: the sentence already says what kind of thing it is ("圖片 word/media/image1.png 仍帶有…").
function partLabel(path, bare) {
  const names = path.split('!');
  if (names.length > 1) {
    if (bare) return t('{outer} 裡的 {inner}', { outer: baseName(names[0]), inner: baseName(names[names.length - 1]) });
    const inner = names.slice(1).join('!');
    return t('{outer} 的{inner}', { outer: partLabel(names[0]) || names[0], inner: partLabel(inner) || inner });
  }
  for (const [re, name] of PART_NAMES) if (re.test(path)) return typeof name === 'function' ? name(path, bare) : t(name);
  return null;
}
// The character before a path is matched and put back, not looked behind at: a browser that does not know
// look-behind would refuse the whole script.
const PART_PATH = /(^|[^\w/])((?:(?:word|xl|docProps|customXml|_rels)\/[^\s!「」（）():：]+|\[Content_Types\]\.xml)(?:![^\s「」（）():：]+)*)/g;
function readable(text) {
  const cut = text.indexOf('「');  // what was found is quoted after the place: it stays as it was
  const head = cut < 0 ? text : text.slice(0, cut);
  const shown = head.replace(PART_PATH, (all, before, path, at) => before
    + (partLabel(path, /(?:內嵌檔案|內嵌物件|圖片) $/.test(head.slice(0, at + before.length))) || path));
  return shown + text.slice(head.length);
}
function line(text) {
  const shown = detailText(readable(text));
  return el('li', { text: shown, title: shown === text ? null : text });
}

function showResult(res) {
  state.result = res;
  $('result-ack').checked = false;
  renderResult(res);
  $('result').hidden = !resultInLanguage();
  syncResultButton();
}

function renderResult(res) {
  const card = $('result').querySelector('.card');
  card.classList.toggle('ok', res.ok);
  card.classList.toggle('bad', !res.ok);
  $('result-title').textContent = state.code
    ? t(res.ok ? '代號版已產生，檢查通過' : '代號版已產生，但仍有內容沒有換成代號')
    : t(res.ok ? '遮蔽完成，驗證通過' : '遮蔽完成，但驗證發現問題');
  $('result-text').textContent = resultCopy(res);
  $('result-download').textContent = state.code ? t('下載代號版文字') : t('下載遮蔽檔');
  $('result-restore').hidden = !state.code;
  const file = state.code ? res.file : null;  // the document with the codes in it, when one could be made
  $('result-coded').hidden = !file;
  if (file) $('result-coded').textContent = t('下載代號版 {format}檔', { format: FILE_KINDS[file.ext] || '' });
  $('result-download').classList.toggle('primary', !file);  // with a file, the text is the second choice
  const about = state.code ? [...(res.file_notes || []), ...(file ? [FILE_ADVICE] : [])] : [];
  $('result-file-notes').hidden = !about.length;
  $('result-file-notes').querySelector('ul').replaceChildren(...about.map((note) => el('li', { text: detailText(note) })));
  $('result-hint').textContent = t(state.code ? HINTS.code : HINTS.mask);
  $('result-issues').replaceChildren(...[...(res.residue || []), ...(res.problems || [])].map(line));
  const left = [...state.rev.unchecked, ...(state.rev.unmaskable || [])].map((u) =>
    t('{label}（{reason}）', { label: detailText(u.name || u.where), reason: detailText(u.reason) }));
  const notes = res.notes || [];
  $('result-notes').hidden = !notes.length;
  $('result-notes').querySelector('ul').replaceChildren(...notes.map(line));
  $('result-unchecked').hidden = !left.length;
  $('result-unchecked').querySelector('ul').replaceChildren(...left.map(line));
  $('result-ack-row').hidden = res.ok;
  $('result-download').disabled = !res.ok && !$('result-ack').checked;
  $('result-coded').disabled = $('result-download').disabled;
  $('result-download').dataset.ack = res.ok ? '' : '1';
}

// Closing the overlay keeps the result: the toolbar offers it again while it still matches the findings.
function hideResult() {
  const wasOpen = !$('result').hidden;
  $('result').hidden = true;
  syncResultButton();
  if (wasOpen && !$('btn-result').hidden) $('btn-result').focus();
}

function resultInLanguage() { return state.result && (!state.code || (state.result.lang || 'zh-TW') === language); }

function syncResultButton() { $('btn-result').hidden = !resultInLanguage() || !$('result').hidden; }

function forgetResult() {
  state.result = null;
  syncResultButton();
}

// Ask the server rather than trust the page: a result is offered only while the server still has it.
async function reopenResult() {
  const r = await api('GET', jobUrl());
  if (r.ok && r.data.result) return showResult(r.data.result);
  forgetResult();
  alert(t('清單已經更動，剛才的結果已經失效。請重新產生。'));
}

function downloadPath(what) {
  const params = new URLSearchParams();
  if (what) params.set('what', what);
  if (what !== 'restore' && $('result-download').dataset.ack) params.set('ack', '1');
  if (language === 'en') params.set('lang', 'en');
  if (state.code && state.result?.made) params.set('batch', state.result.made);
  return '/download' + (params.size ? '?' + params : '');
}

async function downloadReport() {
  if (language !== 'en') return download('/report');
  const r = await api('GET', jobUrl('/report'));
  if (language !== 'en') return;
  if (!r.ok) return alert(detailText(r.data.error) || t('無法下載遮蔽清單'));
  const report = r.data;
  for (const f of report.findings) {
    f.type = t(f.type);
    f.reason = detailText(f.reason);
  }
  for (const item of [...report.unchecked, ...report.unmaskable]) {
    if ('name' in item) item.name = detailText(item.name);
    if ('where' in item) item.where = detailText(item.where);
    item.reason = detailText(item.reason);
  }
  for (const [object, keys] of [[report, ['warnings']], [report.verify, ['residue', 'problems', 'notes', 'file_notes']]]) {
    for (const key of keys) if (object?.[key]) object[key] = object[key].map(detailText);
  }
  if (report.outcome.fallback_reason) report.outcome.fallback_reason = detailText(report.outcome.fallback_reason);
  if (report.outcome.names?.reason) report.outcome.names.reason = detailText(report.outcome.names.reason);
  downloadBlob(new Blob([JSON.stringify(report, null, 1)], { type: 'application/json' }), 'report.json');
}

async function deleteJob() {
  if (!state.id || !confirm(t('確定要刪除這份文件與所有處理結果嗎？'))) return;
  await api('DELETE', jobUrl());
  stopPolling();
  state.id = null;
  history.replaceState(null, '', location.pathname + location.search);
  show('upload');
}

// ---- wiring ---------------------------------------------------------------------------------

function init() {
  initLanguage();
  $('restore-copy').textContent = t('複製還原後的文字');
  $('language').addEventListener('change', (event) => {
    setLanguage(event.target.value);
    const url = new URL(location.href);
    url.searchParams.set('lang', language);
    history.replaceState(null, '', url);
    for (const [id, source] of errors) if (!$(id).hidden) $(id).textContent = detailText(source);
    busy(state.busy);
    renderJobStatus();
    renderLeft();
    if (!$('review').hidden) {
      hidePopups();
      const scrollTop = $('doc').scrollTop;
      renderReviewLabels($('add-text-type').value);
      buildDoc();
      renderPanels();
      renderAll();
      $('doc').scrollTop = scrollTop;
    }
    if (!$('result').hidden && state.result) {
      if (!resultInLanguage()) hideResult();
      else renderResult(state.result);
    }
    syncResultButton();
    for (const kind of ['key', 'file']) renderRestorePicker(kind);
    $('restore-copy').textContent = t('複製還原後的文字');
    if (!$('restore-result').hidden && state.restored) renderRestoredSummary();
  });
  for (const [dropId, fileId, mode] of [['drop', 'file', 'mask'], ['drop-code', 'file-code', 'code']]) {
    const drop = $(dropId);
    drop.addEventListener('click', () => $(fileId).click());
    drop.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $(fileId).click(); } });
    $(fileId).addEventListener('change', (e) => { upload(e.target.files[0], mode); e.target.value = ''; });
    for (const name of ['dragenter', 'dragover']) drop.addEventListener(name, (e) => { e.preventDefault(); drop.classList.add('over'); });
    for (const name of ['dragleave', 'drop']) drop.addEventListener(name, (e) => { e.preventDefault(); drop.classList.remove('over'); });
    drop.addEventListener('drop', (e) => upload(e.dataTransfer.files[0], mode));
  }
  $('go-restore').addEventListener('click', () => { location.hash = 'restore'; });
  for (const kind of ['key', 'file']) {
    $(`restore-${kind}-pick`).addEventListener('click', () => $(`restore-${kind}`).click());
    $(`restore-${kind}`).addEventListener('change', () => renderRestorePicker(kind));
    renderRestorePicker(kind);
  }
  $('restore-home').addEventListener('click', () => { clearRestore(); location.hash = ''; });
  $('restore-run').addEventListener('click', runRestore);
  $('restore-key').addEventListener('change', () => {  // the password belongs to the restore file that asked for it
    $('restore-password-field').hidden = true;
    $('restore-password').value = '';
    $('restore-error').hidden = true;
  });
  $('restore-copy').addEventListener('click', copyRestored);
  $('restore-save').addEventListener('click', saveRestored);
  $('restore-file-save').addEventListener('click', saveRestoredFile);
  $('restore-clear').addEventListener('click', clearRestore);
  $('result-restore').addEventListener('click', () => download(downloadPath('restore')));
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', (e) => e.preventDefault());

  $('failed-retry').addEventListener('click', async () => { await api('POST', jobUrl('/retry')); load(state.id); });
  $('failed-delete').addEventListener('click', deleteJob);
  $('working-cancel').addEventListener('click', deleteJob);
  $('btn-delete').addEventListener('click', deleteJob);
  $('go-home').addEventListener('click', goHome);
  $('upload-left-back').addEventListener('click', () => { location.hash = state.left.id; });
  $('btn-apply').addEventListener('click', () => (state.code ? exportCodes() : applyMasks()));
  $('add-text').addEventListener('submit', addTextEverywhere);

  $('doc').addEventListener('mouseup', () => setTimeout(onSelection, 0));
  $('doc').addEventListener('keyup', (e) => { if (e.shiftKey || e.key === 'Shift') onSelection(); });
  $('doc').addEventListener('click', (e) => { const m = e.target.closest('mark'); if (m && window.getSelection().isCollapsed) focusFinding(m.dataset.fid, false); });
  $('doc').addEventListener('scroll', () => { if ($('popover').hidden) $('selbtn').hidden = true; });
  $('selbtn').addEventListener('mousedown', (e) => e.preventDefault());
  $('selbtn').addEventListener('click', () => {
    const p = state.pending;
    if (!p) return;
    const shown = p.text.length > 30 ? `${p.text.slice(0, 30)}…` : p.text;
    openPopover(t('遮蔽「{text}」', { text: shown }), p.rect, true, '姓名');
  });
  $('popover-ok').addEventListener('click', confirmPopover);
  $('popover-cancel').addEventListener('click', hidePopups);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { hidePopups(); hideResult(); } });

  $('result-ack').addEventListener('change', (e) => {
    $('result-download').disabled = !e.target.checked;
    $('result-coded').disabled = !e.target.checked;
  });
  $('result-coded').addEventListener('click', () => download(downloadPath('coded')));
  $('result-download').addEventListener('click', () => download(downloadPath(state.code ? 'text' : '')));
  $('result-report').addEventListener('click', downloadReport);
  $('result-back').addEventListener('click', hideResult);
  $('btn-result').addEventListener('click', reopenResult);

  window.addEventListener('hashchange', start);
  start();
}

function start() {
  const id = location.hash.replace(/^#/, '');
  $('result').hidden = true;  // it belongs to the document that was open
  forgetResult();
  if (id === 'restore') { stopPolling(); state.id = null; return show('restore'); }
  if (id) load(id); else { stopPolling(); state.id = null; show('upload'); }
}

// ---- restoring a reply ----------------------------------------------------------------------

const UNREAD = /沒有讀取|無法讀取/;

function renderRestorePicker(kind) {
  $(`restore-${kind}-name`).textContent = $(`restore-${kind}`).files[0]?.name || t('尚未選擇檔案');
}

async function runRestore() {
  const fail = (message) => showError('restore-error', message);
  $('restore-error').hidden = true;
  $('restore-result').hidden = true;  // an earlier result must not stand under a new error
  const key = $('restore-key').files[0];
  const password = $('restore-password').value;
  const text = $('restore-input').value;
  const file = $('restore-file').files[0];
  if (!key) return fail('請選擇還原檔');
  if (file && text.trim()) return fail('貼上文字和上傳檔案請擇一：清掉其中一個再按還原');
  if (!file && !text.trim()) return fail('請貼上雲端的回覆，或選擇雲端給的檔案');
  const form = new FormData();
  form.append('key', key);
  form.append('lang', language);
  if (password) form.append('password', password);  // only a restore file made before 2026-11-19 has one
  if (file) form.append('file', file);  // read as text on the server
  else form.append('text', new Blob([text], { type: 'text/plain' }), 'reply.txt');  // as a file part: a reply can be long
  busy('還原中…');
  const r = await api('POST', '/api/restore', form);
  busy('');
  if (!r.ok) {
    if (r.data.needs_password) { $('restore-password-field').hidden = false; $('restore-password').focus(); }
    return fail(r.data.error || '還原失敗');
  }
  $('restore-output').value = r.data.text;
  restoredFile = r.data.file_b64 ? { b64: r.data.file_b64, ext: r.data.file_ext } : null;  // held in this page only
  state.restored = { replaced: r.data.replaced, notes: r.data.notes || [] };
  renderRestoredSummary();
  $('restore-file-save').hidden = !restoredFile;
  for (const [id, items] of [['restore-unknown', r.data.unknown], ['restore-unused', r.data.unused]]) {
    $(id).hidden = !items.length;
    $(id).querySelector('ul').replaceChildren(...items.map((t) => el('li', { text: t })));
  }
  $('restore-result').hidden = false;
}

function renderRestoredSummary() {
  $('restore-summary').textContent = t('已換回 {count} 處', { count: state.restored.replaced });
  if (restoredFile) $('restore-file-save').textContent = t('下載還原後的 {format}檔', { format: FILE_KINDS[restoredFile.ext] || '' });
  const notes = state.restored.notes;
  for (const [id, items] of [['restore-about', notes.filter((n) => !UNREAD.test(n))], ['restore-notes', notes.filter((n) => UNREAD.test(n))]]) {
    $(id).hidden = !items.length;
    $(id).querySelector('ul').replaceChildren(...items.map(line));
  }
}

function copyRestored() {
  const out = $('restore-output');
  out.select();
  // navigator.clipboard needs HTTPS; this page is served over plain HTTP inside the network.
  const done = document.execCommand('copy');
  $('restore-copy').textContent = done ? t('已複製') : t('請按 Ctrl+C 複製');
  setTimeout(() => { $('restore-copy').textContent = t('複製還原後的文字'); }, 1500);
}

let restoredFile = null;  // the reply in its own format with the strings put back, as the server answered it

function saveAs(blob, ext) {
  // Named by time only, like the coded text and the restore file: a file name often carries a person's name.
  const d = new Date();
  const two = (n) => String(n).padStart(2, '0');
  const stamp = `${d.getFullYear()}${two(d.getMonth() + 1)}${two(d.getDate())}-${two(d.getHours())}${two(d.getMinutes())}${two(d.getSeconds())}`;
  downloadBlob(blob, `${t('還原結果')}_${stamp}${ext}`);
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = el('a', { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function saveRestored() {
  // Made in the browser from what is on the screen: the restored text does not go back to the server.
  saveAs(new Blob(['\ufeff', $('restore-output').value], { type: 'text/plain;charset=utf-8' }), '.txt');  // the mark lets Notepad show Chinese
}

function saveRestoredFile() {
  if (!restoredFile) return;
  const raw = atob(restoredFile.b64);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
  saveAs(new Blob([bytes], { type: 'application/octet-stream' }), restoredFile.ext);
}

function clearRestore() {
  for (const id of ['restore-password', 'restore-input', 'restore-output', 'restore-key', 'restore-file']) $(id).value = '';
  for (const kind of ['key', 'file']) renderRestorePicker(kind);
  restoredFile = null;
  state.restored = null;
  $('restore-file-save').hidden = true;
  $('restore-result').hidden = true;
  $('restore-error').hidden = true;
  $('restore-password-field').hidden = true;
}

init();
