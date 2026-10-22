// subjetunes/desktop/web/app.js
// No framework, no build step. One state object, one render per page, actions on data-a.
// Pipeline: Library → Batch → Tag → Write. Settings holds sources, rules and vocabulary.

const TOKEN = new URLSearchParams(location.search).get("t") || "";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const fmtTime = (s) => (isFinite(s) && s > 0 ? `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}` : "0:00");
// macOS: ⌘ instead of Ctrl in every hint (the shortcuts already accept both)
const MOD = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent) ? "⌘" : "Ctrl";
const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
const DPR = () => Math.min(2, devicePixelRatio || 1);

// Band splits for the HI/MID/LOW meters. Pioneer doesn't publish DJM crossover points (the manuals
// list only the frequency each EQ range is measured at: DJM-900NXS2 LOW 20 Hz / MID 1 kHz / HI 30 kHz,
// DJM-900nexus LOW 70 Hz / HI 13 kHz), so these are our own splits. Change them here.
const XOVER = { lowMid: 250, midHigh: 2500 };

async function api(path, body, timeoutMs = 60000) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(`/api/${path}`, { method: body === undefined ? "GET" : "POST", headers: { "X-Token": TOKEN, "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body), signal: ctl.signal });
  } catch (e) {
    throw new Error(e.name === "AbortError" ? `No answer after ${timeoutMs / 1000}s. See data\\logs\\subjetunes.log.` : `Can't reach Subjetunes: ${e.message}`);
  } finally { clearTimeout(timer); }
  const data = await res.json().catch(() => ({ error: `Unexpected reply (${res.status})` }));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

const S = {
  page: "library", status: null, rows: [], detail: null, tagKey: null, sel: null,
  filter: "all", bucket: "", q: "", region: null, keys: new Set(), focus: false, view: "list", limit: 300,
  open: new Set(), folds: new Set(["overview", "buckets"]), job: null,
  plan: null, skip: new Set(), wopts: { fields: true, tags: true, playlists: true }, receipt: null, writing: false, savedAt: 0,
  srcOpen: null, flapPrev: {},
};

// ------------------------------------------------------------------ helpers
function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = `toast show${bad ? " bad" : ""}`;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.className = "toast"), bad ? 7000 : 2600);
}
async function run(fn) { try { return await fn(); } catch (e) { toast(e.message, true); } }
const levelTags = () => (S.status?.vocab || []).find((g) => g.kind === "level")?.tags || [];
const levelOf = (row) => levelTags().findIndex((t) => row?.tags.includes(t)) + 1;
const bucketName = (id) => S.status?.buckets.find((b) => b.id === id)?.name || id;
const srcName = (id) => S.status?.sources.find((s) => s.id === id)?.name || id;
const meter = (n, edge) => `<span class="meter${edge ? " edge" : ""}" data-l="${n}"><i></i><i></i><i></i><i></i><i></i></span>`;
const check = `<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10.6l4 4L16 6"/></svg>`;
const chev = `<svg class="chev" viewBox="0 0 16 16" aria-hidden="true"><path d="M6 3l5 5-5 5"/></svg>`;
const playSvg = `<svg viewBox="0 0 14 14" aria-hidden="true"><path d="M3 1.5v11l9-5.5z" fill="currentColor"/></svg>`;
const I = {
  library: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="2.4"/>',
  batch: '<path d="M4 6h16M4 12h16M4 18h10"/>',
  tag: '<path d="M3 12V4h8l10 10-8 8z"/><circle cx="7.5" cy="8.5" r="1.3"/>',
  write: '<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6"/><path d="M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3"/>',
  settings: '<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>',
  track: '<path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/>',
  bolt: '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
  list: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  lanes: '<rect x="3" y="4" width="5" height="16" rx="1"/><rect x="10" y="4" width="5" height="11" rx="1"/><rect x="17" y="4" width="4" height="7" rx="1"/>',
  up: '<path d="M6 15l6-6 6 6"/>', down: '<path d="M6 9l6 6 6-6"/>', back: '<path d="M15 6l-6 6 6 6"/>',
  reload: '<path d="M20 12a8 8 0 1 1-2.3-5.7"/><path d="M20 4v5h-5"/>',
};
const icon = (k) => `<svg viewBox="0 0 24 24" aria-hidden="true">${I[k]}</svg>`;
const fold = (id, title, sum, body) => `<details class="fold" data-fold="${id}" ${S.folds.has(id) ? "open" : ""}><summary><span class="h">${title}</span><span class="sum">${sum || ""}</span></summary><div class="body">${body}</div></details>`;

// one tooltip for everything with data-tip
let tipShow = () => {}, tipCur = null;
(() => {
  const tip = $("#tip");
  const show = (el) => {
    tipCur = el;
    tip.textContent = el.dataset.tip; tip.classList.add("show");
    const r = el.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
    let x = Math.min(innerWidth - w - 8, Math.max(8, r.left + r.width / 2 - w / 2)), y = r.top - h - 8;
    if (y < 6) y = r.bottom + 8;
    if (el.closest(".rail")) { x = r.right + 10; y = r.top + r.height / 2 - h / 2; }
    tip.style.left = `${x}px`; tip.style.top = `${y}px`;
  };
  tipShow = show;
  document.addEventListener("pointerover", (e) => { const el = e.target.closest("[data-tip]"); if (el && el !== tipCur && el.dataset.tip) show(el); else if (!el && tipCur) { tipCur = null; tip.classList.remove("show"); } });
  document.addEventListener("focusin", (e) => { const el = e.target.closest("[data-tip]"); if (el?.dataset.tip && e.target.matches(":focus-visible")) show(el); });
  ["pointerdown", "scroll", "keydown"].forEach((t) => document.addEventListener(t, () => { tipCur = null; tip.classList.remove("show"); }, true));
})();

function visibleRows() {
  const q = S.q.trim().toLowerCase();
  return S.rows.filter((r) => {
    if (S.filter === "review" && !(r.uncertain.length || r.bucket_edge.length)) return false;
    if (S.filter === "edited" && !(r.tag_edited || r.bucket_edited || Object.keys(r.pending).length)) return false;
    if (S.filter === "unscored" && r.scored) return false;
    if (S.bucket === "_none" && r.buckets.length) return false;
    if (S.bucket && S.bucket !== "_none" && !r.buckets.includes(S.bucket)) return false;
    if (S.region) { const l = levelOf(r); if (!r.bpm || r.bpm < S.region.bpm[0] || r.bpm > S.region.bpm[1] || l < S.region.lv[0] || l > S.region.lv[1]) return false; }
    if (S.keys.size && !S.keys.has(r.tonality)) return false;
    if (q && !`${r.title} ${r.artist} ${r.genre} ${r.tags.join(" ")} ${r.clues.join(" ")}`.toLowerCase().includes(q)) return false;
    return true;
  });
}

async function refresh(rowsToo = true) {
  S.status = await api("status");
  if (S.status.loading) { render(); clearTimeout(refresh._t); refresh._t = setTimeout(() => run(() => refresh(rowsToo)), 600); return; }
  if (rowsToo) S.rows = S.status.library ? (await api("tracks")).rows : [];
  render();
}
function patchRow(res) {
  if (res.row) S.rows = S.rows.map((r) => (r.key === res.row.key ? res.row : r));
  if (res.status) S.status = res.status;
  if (S.detail && res.row && S.detail.row?.key === res.row.key) S.detail.row = res.row;
  render();
}
async function go(page) {
  S.page = page;
  if (page === "write" && S.status?.library) await loadPlan();
  if (page === "settings") S.libInfo = await api("library_info").catch(() => null);
  if (page === "tag") { const rows = visibleRows(); if (!rows.some((r) => r.key === S.tagKey)) S.tagKey = rows[0]?.key || null; if (S.tagKey) return openTag(S.tagKey); }
  render(); $("#main").scrollTo(0, 0);
}

// ------------------------------------------------------------------ shell
const NAV = [["library", "Library"], ["batch", "Batch"], ["tag", "Tag"], ["write", "Write"], ["settings", "Settings"]];
function renderShell() {
  const st = S.status || {};
  const tips = { library: "Library", batch: `Batch${st.counts?.tracks ? `  ·  ${st.counts.tracks} tracks` : ""}`, tag: `Tag${st.counts?.review ? `  ·  ${st.counts.review} not sure` : ""}`, write: `Write${st.pending ? `  ·  ${st.pending} staged` : ""}`, settings: "Settings, rules, sources" };
  const mark = `<svg viewBox="0 0 26 26" aria-hidden="true">${[1, 2, 3, 4, 5].map((n, i) => `<rect x="${2 + i * 4.6}" y="${20 - (6 + i * 3)}" width="3.2" height="${6 + i * 3}" rx="1" fill="var(--e${n})"/>`).join("")}</svg>`;
  $("#rail").innerHTML = `<div class="rail-mark">${mark}</div><span class="rail-ind"></span>
    ${NAV.map(([id, l]) => `<button class="rail-item" type="button" data-a="go" data-v="${id}" aria-label="${l}" data-tip="${esc(tips[id])}" ${S.page === id ? 'aria-current="page"' : ""}>${icon(id)}${id === "write" && st.pending ? '<span class="led"></span>' : ""}</button>`).join("")}
    <div class="rail-foot" data-tip="${st.library ? (st.rekordbox_running ? "rekordbox is open: reading works, writing waits" : "rekordbox is closed: ready to write") : "No library"}"><span class="lamp ${st.library ? (st.rekordbox_running ? "led" : "ok") : ""}"></span></div>`;
  const cur = $('.rail-item[aria-current="page"]');
  if (cur) $(".rail-ind").style.setProperty("--y", `${cur.offsetTop}px`);

  const trail = [["library", "Library"]];
  if (["batch", "tag"].includes(S.page) && st.batch_name) trail.push(["batch", st.batch_name]);
  if (S.page === "tag" && S.detail?.row) trail.push(["tag", S.detail.row.title]);
  if (["write", "settings"].includes(S.page)) trail.splice(0, 1, [S.page, NAV.find((n) => n[0] === S.page)[1]]);
  $("#crumbs").innerHTML = trail.map(([id, l], i) => `${i ? chev : ""}<button class="crumb" type="button" data-a="go" data-v="${id}" ${i === trail.length - 1 ? 'aria-current="page"' : ""}>${esc(l)}</button>`).join("");

  const chip = $("#chip");
  const state = S.writing ? "writing" : Date.now() - S.savedAt < 5000 ? "saved" : st.pending ? "staged" : "idle";
  chip.dataset.state = state;
  chip.innerHTML = `<span class="lamp"></span>${{ idle: "", staged: st.pending, writing: "…", saved: "✓" }[state]}`;
  chip.dataset.tip = { idle: "Nothing staged for rekordbox", staged: `${st.pending} edit${st.pending > 1 ? "s" : ""} staged, not written yet${st.rekordbox_running ? ". Close rekordbox to write." : ""}`, writing: "Writing to rekordbox", saved: "Saved to your library" }[state];
}

function render() {
  renderShell();
  const main = $("#main");
  if (S.status?.loading) main.innerHTML = `<div class="page"><div class="empty"><canvas class="vessel" data-vessel></canvas><h2>${esc(S.status.loading)}…</h2></div></div>`;
  else main.innerHTML = ({ library: pageLibrary, batch: pageBatch, tag: pageTag, write: pageWrite, settings: pageSettings }[S.page])();
  mount();
}
function mount() {
  $$(".seg").forEach((seg) => { const on = $('button[aria-pressed="true"]', seg), g = $(".glide", seg); if (on && g) { g.style.width = `${on.offsetWidth}px`; g.style.transform = `translateX(${on.offsetLeft}px)`; } });
  $$("canvas[data-vessel]").forEach(vessel);
  $$("canvas[data-swarm]").forEach(swarm);
  $$("canvas[data-wheel]").forEach(wheel);
  $$("canvas[data-knob]").forEach(knob);
  $$("button[data-hold]").forEach(holdButton);
  $$("[data-flaps]").forEach(flapsAnimate);
  $$("[data-bands]").forEach(buildBands);
  // "toggle" also fires when a details element is inserted already open, so act only on real changes
  $$("details.fold").forEach((d) => d.addEventListener("toggle", () => {
    const id = d.dataset.fold;
    if (d.open === S.folds.has(id)) return;
    d.open ? S.folds.add(id) : S.folds.delete(id);
    if (d.open && id === "elsewhere") run(lookupNow);
  }));
}

function needLibrary() {
  return `<div class="page"><div class="empty"><canvas class="vessel" data-vessel></canvas><h2>No library yet</h2>
    <div class="row" style="justify-content:center"><button class="btn solid" data-a="opendb" data-tip="Reads master.db. rekordbox can stay open.">Use my rekordbox library</button><button class="btn flat" data-a="go" data-v="settings">Another file</button></div>
    ${S.status?.error ? `<p style="color:var(--bad);margin-top:12px">${esc(S.status.error)}</p>` : ""}</div></div>`;
}
function needBatch() {
  return `<div class="page"><div class="empty"><canvas class="vessel" data-vessel></canvas><h2>Empty batch</h2>
    <div class="row" style="justify-content:center"><button class="btn solid" data-a="go" data-v="library">Pick playlists</button><button class="btn flat" data-a="palette">One track <kbd>${MOD} K</kbd></button></div></div></div>`;
}

// ------------------------------------------------------------------ library
function tree(nodes) {
  return `<ul class="tree">${nodes.map((n) => {
    if (n.folder) return `<li class="${S.open.has(n.path) ? "open" : ""}"><button class="node folder" type="button" data-a="fold" data-v="${esc(n.path)}"><svg viewBox="0 0 12 12"><path d="M4 2l4 4-4 4"/></svg>${esc(n.name)}</button>${tree(n.children)}</li>`;
    return `<li><button class="node" type="button" role="checkbox" aria-checked="${S.status.batch.playlists.includes(n.path)}" data-a="pl" data-v="${esc(n.path)}"><span class="mini">${check}</span>${esc(n.name)}<span class="n">${n.count}</span></button></li>`;
  }).join("")}</ul>`;
}
function pageLibrary() {
  const st = S.status;
  if (!st?.library) return needLibrary();
  const c = st.counts, j = S.job;
  const singles = st.batch.tracks.map((k) => S.rows.find((r) => r.key === k)).filter(Boolean);
  const log = j ? fold("log", "Analysis", `${j.state}, ${fmtTime(j.elapsed)}`, `<div class="trace">${j.log.slice(-150).map((l) => `<div class="${/error|fail|traceback/i.test(l) ? "bad" : ""}">${esc(l)}</div>`).join("")}</div>`) : "";
  return `<div class="page">
    <div class="row" style="margin-bottom:16px"><h1 class="title">Library</h1><span class="meta">${st.library.tracks.toLocaleString()}</span><span class="grow"></span>
      <button class="iconbtn" data-a="reload" data-tip="Reload from rekordbox">${icon("reload")}</button></div>
    ${tree(st.library.playlists)}
    ${log}
    <div class="dockbar">
      ${c.tracks ? `<span class="big">${esc(st.batch_name)}</span><span class="meta" data-tip="${c.scored} of ${c.tracks} have scores. BPM, key and beat grid come from rekordbox.">${c.tracks} tracks</span>
        ${singles.length ? `<span class="pills">${singles.map((r) => `<span class="pill soft">${esc(r.title)}<button class="x" data-a="droptrack" data-v="${esc(r.key)}" aria-label="Remove">×</button></span>`).join("")}</span>` : ""}
        ${j?.state === "running" ? `<div class="bar grow" style="max-width:220px"><i style="width:${(j.progress || 0) * 100}%"></i></div>` : '<span class="grow"></span>'}
        <button class="btn flat sm" data-a="clearbatch" data-tip="Empty the batch">Clear</button>
        <button class="btn" data-a="analyze" ${!st.engine || j?.state === "running" ? "disabled" : ""} data-tip="${st.engine ? "Measure energy, mood and the rest. rekordbox's BPM, key and grid are reused." : "Analysis isn't in this build yet. Scores come from data\\scores.csv or demo scores."}">${j?.state === "running" ? "Analysing" : "Analyse"}</button>
        <button class="btn solid" data-a="go" data-v="batch">Open batch</button>`
      : `<span class="meta">Tick playlists, or</span><button class="btn flat sm" data-a="palette">find one track <kbd>${MOD} K</kbd></button>`}
    </div></div>`;
}

// ------------------------------------------------------------------ batch
function rowsHtml() {
  const rows = visibleRows(), lv = levelTags();
  return `<ul class="rows${S.focus ? " focus" : ""}">${rows.slice(0, S.limit).map((r) => {
    const tags = r.tags.filter((t) => !lv.includes(t) && !t.startsWith("#b-"));
    const edited = r.tag_edited || r.bucket_edited || Object.keys(r.pending).length;
    return `<li data-a="open" data-v="${esc(r.key)}" class="${r.key === S.sel ? "on" : ""}">${meter(levelOf(r), r.borderline.length)}
      <div class="t"><b>${esc(r.title)}</b><span>${esc(r.artist)}</span></div>
      <div class="pills">${r.buckets.map((b) => `<span class="pill solid">${esc(bucketName(b))}</span>`).join("")}${tags.slice(0, 4).map((t) => `<span class="pill soft">${esc(t.slice(1))}</span>`).join("")}${r.uncertain.slice(0, 2).map((t) => `<span class="pill unsure" data-tip="${esc(r.reasons[t] || "")}">${esc(t.slice(1))}</span>`).join("")}</div>
      <div class="f">${r.error ? `<span class="dot bad" data-tip="${esc("Analysis failed: " + r.error)}"></span>` : ""}${edited ? '<span class="dot led" data-tip="Edited, not written yet"></span>' : ""}${r.bpm ? r.bpm.toFixed(0) : "–"} ${esc(r.tonality || "")}</div></li>`;
  }).join("")}</ul>${rows.length > S.limit ? `<div class="row" style="justify-content:center;margin-top:12px"><button class="btn flat" data-a="more">${rows.length - S.limit} more</button></div>` : ""}
  ${rows.length ? "" : `<div class="empty"><h2>Nothing matches</h2><button class="btn flat" data-a="clearfilters">Clear filters</button></div>`}`;
}
function lanesHtml() {
  const rows = visibleRows();
  const lanes = [...S.status.buckets.map((b) => ({ id: b.id, name: b.name, tip: b.rule || "Decided by hand" })), { id: "", name: "No bucket", tip: "Tracks in no bucket" }];
  return `<div class="lanes" id="lanes">${lanes.map((ln) => {
    const items = rows.filter((r) => (ln.id ? r.buckets.includes(ln.id) : !r.buckets.length));
    return `<div class="lane" data-lane="${esc(ln.id)}"><div class="lane-head" data-tip="${esc(ln.tip)}">${esc(ln.name)}<em>${items.length}</em></div>
      <div class="lane-body">${items.slice(0, 80).map((r) => `<div class="card${r.bucket_edge.includes(ln.id) ? " edge" : ""}" tabindex="0" data-card="${esc(r.key)}" data-tip="${r.bucket_edge.includes(ln.id) ? "On the edge of this rule" : ""}">${meter(levelOf(r))}<div class="tx"><b>${esc(r.title)}</b><span>${esc(r.artist)}</span></div></div>`).join("")}${items.length > 80 ? `<div class="lane-more">+${items.length - 80}</div>` : ""}</div></div>`;
  }).join("")}</div>`;
}
function renderRows() {
  const w = $("#rowsWrap"); if (!w) return render();
  w.innerHTML = S.view === "lanes" ? lanesHtml() : rowsHtml();
  const n = $("#rowsCount"); if (n) n.textContent = `${visibleRows().length} / ${S.rows.length}`;
  const fc = $("#filterChips"); if (fc) fc.innerHTML = filterChips();
}
function filterChips() {
  const out = [];
  if (S.region) out.push(`<span class="pill">${Math.round(S.region.bpm[0])}–${Math.round(S.region.bpm[1])} bpm, E${S.region.lv[0]}${S.region.lv[1] !== S.region.lv[0] ? `–${S.region.lv[1]}` : ""}<button class="x" data-a="clearregion">×</button></span>`);
  if (S.keys.size) out.push(`<span class="pill">${[...S.keys].join(" ")}<button class="x" data-a="clearkeys">×</button></span>`);
  return out.join("");
}
function pageBatch() {
  const st = S.status;
  if (!st?.library) return needLibrary();
  if (!st.counts.tracks) return needBatch();
  const c = st.counts;
  const edited = S.rows.filter((r) => r.tag_edited || r.bucket_edited || Object.keys(r.pending).length).length;
  const keyed = new Set(S.rows.map((r) => r.tonality).filter(Boolean)).size;
  return `<div class="page">
    <div class="row" style="margin-bottom:14px"><h1 class="title">${esc(st.batch_name)}</h1><span class="meta" id="rowsCount" data-tip="Shown / in batch">${visibleRows().length} / ${S.rows.length}</span><span class="grow"></span>
      ${S.view === "list" ? `<button class="toggle" data-a="focus" aria-pressed="${S.focus}" data-tip="Focus: dim everything but the selected track  ·  F"><span class="sw"><i></i></span></button>` : ""}
      <div class="seg"><span class="glide"></span><button data-a="view" data-v="list" aria-pressed="${S.view === "list"}" data-tip="List  ·  L">${icon("list")}</button><button data-a="view" data-v="lanes" aria-pressed="${S.view === "lanes"}" data-tip="Bucket lanes: drag tracks between buckets. Ctrl copies.  ·  L">${icon("lanes")}</button></div>
      <button class="btn solid" data-a="go" data-v="tag" data-tip="Tag track by track, by keyboard">Tag</button></div>
    ${fold("overview", "Overview", `${c.scored} scored, ${keyed} keys`, `<div class="overview"><canvas class="swarm" data-swarm data-tip="BPM × energy. Drag to filter, click a dot to open it, double-click to clear."></canvas><canvas class="wheel" data-wheel></canvas></div>`)}
    <div class="filters">
      <div class="seg"><span class="glide"></span>${[["all", "All", c.tracks], ["review", "Not sure", c.review], ["edited", "Edited", edited], ["unscored", "No scores", c.tracks - c.scored]].map(([id, l, n]) => `<button data-a="filter" data-v="${id}" aria-pressed="${S.filter === id}">${l}<span class="n">${n}</span></button>`).join("")}</div>
      <div class="chipset">${st.buckets.map((b) => `<button class="pill" data-a="bucketfilter" data-v="${esc(b.id)}" aria-pressed="${S.bucket === b.id}" data-tip="${esc(b.rule || "")}">${esc(b.name)} <span class="src">${b.count}</span></button>`).join("")}</div>
      <span id="filterChips" class="pills">${filterChips()}</span><span class="grow"></span>
      <input class="input" type="search" id="search" placeholder="Search" value="${esc(S.q)}" data-tip="Titles, artists, tags and clues">
    </div>
    <div id="rowsWrap">${S.view === "lanes" ? lanesHtml() : rowsHtml()}</div></div>`;
}

// ------------------------------------------------------------------ tag (one screen, keyboard first)
async function openTag(key, lookup = S.folds.has("elsewhere")) {
  S.tagKey = key; S.sel = key;
  S.detail = await api(`track?key=${encodeURIComponent(key)}&lookup=${lookup ? 1 : 0}`);
  S.page = "tag"; render(); $("#main").scrollTo(0, 0);
  if (lookup) pollDetail(key);
}
async function lookupNow() { if (S.detail?.row) { S.detail = await api(`track?key=${encodeURIComponent(S.detail.row.key)}&lookup=1`); render(); pollDetail(S.detail.row.key); } }
async function pollDetail(key) {
  while (S.page === "tag" && S.detail?.row?.key === key && S.detail.loading.length) {
    await new Promise((r) => setTimeout(r, 1200));
    if (S.page !== "tag" || S.detail?.row?.key !== key) return;
    const d = await api(`track?key=${encodeURIComponent(key)}&lookup=1`);
    if (document.activeElement?.matches("input")) { Object.assign(S.detail, { sources: d.sources, cloud: d.cloud, loading: d.loading }); continue; }
    S.detail = d; render();
  }
}
function quickKeys() {
  const map = {}, used = new Set(["j", "k", "f", "l"]);
  for (const g of (S.status?.vocab || []).filter((g) => g.kind !== "level")) for (const t of g.tags) {
    const c = [...t.slice(1).replace(/[^a-z]/g, ""), ..."abcdeghimnopqrstuvwxyz"].find((x) => !used.has(x));
    if (c) { used.add(c); map[c] = t; }
  }
  return map;
}
// Tags as pills, one row per axis. Click toggles on this track, double-click (or right-click)
// renames it in the vocabulary, + adds a new tag to that axis. An empty name removes it.
function tagRowsHtml(row, { track = true } = {}) {
  const keyOf = Object.fromEntries(Object.entries(quickKeys()).map(([k, t]) => [t, k]));
  const rows = S.status.vocab.filter((g) => g.kind !== "level").map((g) => {
    const on = track ? g.tags.filter((t) => row.tags.includes(t)).length : 0;
    const pills = g.tags.map((t) => {
      const active = track && row.tags.includes(t), unsure = track && row.uncertain.includes(t);
      const why = track ? row.reasons[t] : "";
      const tip = [why, g.manual?.includes(t) ? "your tag" : "", "double-click to rename"].filter(Boolean).join("\n");
      return `<button class="pill tagp${active ? "" : " off"}${unsure ? " unsure" : ""}" type="button" aria-pressed="${active}" data-ptag="${esc(t)}" data-group="${esc(g.group)}" data-tip="${esc(tip)}">${esc(t.slice(1))}${unsure ? "?" : ""}${track && keyOf[t] ? `<kbd>${keyOf[t].toUpperCase()}</kbd>` : ""}</button>`;
    }).join("");
    return `<div class="tagrow"><span class="axis" data-tip="${esc([g.tip, `up to ${g.max}`].filter(Boolean).join("\n"))}"${track && on > g.max ? ' style="color:var(--led)"' : ""}>${esc(g.group)}</span>${pills}<button class="pill add" type="button" data-addaxis="${esc(g.group)}" aria-label="Add a ${esc(g.group)} tag" data-tip="Add a ${esc(g.group)} tag">+</button></div>`;
  }).join("");
  return `<div class="tagrows${track ? "" : " edit-only"}">${rows}</div>`;
}
function profileLine(r) {
  const byGroup = (g) => (S.status.vocab.find((x) => x.group === g)?.tags || []).filter((t) => r.tags.includes(t)).map((t) => t.slice(1));
  const l = levelOf(r);
  return [byGroup("mood").join(", "), l ? `energy ${l}` : "", byGroup("movement")[0], byGroup("context").join(", ")].filter(Boolean).join(" / ") || "no profile yet";
}
function flapsHtml(id, text, width) {
  const s = String(text).padStart(width, " ");
  return `<span class="flaps" data-flaps="${id}" data-v="${esc(s)}">${[...s].map((ch) => `<span class="flap"><span>${ch === " " ? "&nbsp;" : esc(ch)}</span></span>`).join("")}</span>`;
}
function starsHtml(d) {
  const f = d.fields.find((x) => x.id === "rating");
  const v = +(f?.pending ?? f?.value ?? 0) || 0;
  const star = '<svg viewBox="0 0 20 20"><path d="M10 1.8l2.5 5.2 5.7.8-4.1 4 1 5.6L10 14.7l-5.1 2.7 1-5.6-4.1-4 5.7-.8z"/></svg>';
  return `<span class="stars${f?.pending != null ? " staged" : ""}" data-stars data-tip="Rating${f?.pending != null ? ", staged" : ""}">${[1, 2, 3, 4, 5].map((n) => `<button type="button" data-a="rate" data-v="${n}" class="${n <= v ? "on" : ""}" aria-label="${n} star${n > 1 ? "s" : ""}">${star}</button>`).join("")}</span>`;
}
function fieldHtml(f, editable) {
  const val = f.pending ?? f.value ?? "";
  const alts = f.alts.slice(0, 4).map((a) => `<button class="pill" type="button" ${editable && f.kind !== "ro" ? `data-a="alt" data-f="${f.id}" data-v="${esc(a.value)}"` : "disabled"} data-tip="${esc(a.sources.map(srcName).join(", "))}">${esc(a.value)}</button>`).join("");
  const input = f.kind === "ro" || !editable ? `<div class="ro">${esc(val === null || val === "" ? "–" : val)}</div>` : `<input class="input" id="f_${f.id}" data-fld="${f.id}" value="${esc(val)}" placeholder="–" autocomplete="off">`;
  return `<div class="fld${f.pending != null ? " changed" : ""}"><label for="f_${f.id}">${f.label}</label><div>${input}${alts ? `<div class="alts">${alts}</div>` : ""}</div></div>`;
}
function pageTag() {
  const st = S.status;
  if (!st?.library) return needLibrary();
  const d = S.detail;
  if (!d?.row) return needBatch();
  const r = d.row, rows = visibleRows(), i = rows.findIndex((x) => x.key === r.key), l = levelOf(r);
  const extra = r.tags.filter((t) => !st.vocab.some((g) => g.tags.includes(t)) && !t.startsWith("#b-"));
  const pend = d.fields.filter((f) => f.pending != null && f.id !== "rating").length;
  const found = d.sources.filter((s) => s.state === "found");
  const art = d.artwork ? `<img src="/api/artwork?t=${encodeURIComponent(TOKEN)}&key=${encodeURIComponent(r.key)}" alt="">` : meter(l).replace('class="meter"', 'class="meter" style="transform:scale(2.6)"');
  const block = r.tags.length ? `{st ${r.tags.join(" ")}}` : "";
  const scores = Object.entries(r.scores).sort((a, b) => a[0].localeCompare(b[0]));
  return `<div class="page">
    <div class="row" style="margin-bottom:12px"><button class="iconbtn" data-a="go" data-v="batch" data-tip="Back to the batch  ·  Esc">${icon("back")}</button><span class="grow"></span>
      <span class="meta" data-tip="${S.filter !== "all" || S.bucket || S.q || S.region || S.keys.size ? "Following the batch filter" : "Position in the batch"}">${i >= 0 ? `${i + 1} / ${rows.length}` : "–"}</span>
      <button class="iconbtn" data-a="step" data-v="-1" data-tip="Previous  ·  ↑">${icon("up")}</button><button class="iconbtn" data-a="step" data-v="1" data-tip="Next  ·  ↓">${icon("down")}</button></div>
    <section class="deck" data-l="${l}">
      <div class="art">${art}</div>
      <div style="min-width:0"><h1>${esc(r.title)}</h1><div class="who" data-tip="${esc([r.label, r.genre, r.year].filter(Boolean).join("  ·  "))}">${esc(r.artist)}</div>
        <div class="readouts"><span data-tip="BPM from rekordbox">${flapsHtml("bpm", r.bpm ? r.bpm.toFixed(1) : "--.-", 5)}</span><span data-tip="Key (Camelot)">${flapsHtml("key", r.tonality || "--", (r.tonality || "--").length)}</span>${starsHtml(d)}<span class="grow"></span><div class="bands" data-bands data-tip="HI / MID / LOW while it plays. Splits at ${XOVER.lowMid} Hz and ${XOVER.midHigh / 1000} kHz."></div></div></div>
      <canvas class="knob" data-knob data-v="${l}" tabindex="0" role="slider" aria-label="Energy" aria-valuemin="0" aria-valuemax="5" aria-valuenow="${l}" data-tip="Energy ${l || "unset"}. Drag, scroll or press 1–5, 0 clears."></canvas>
      <button class="play" type="button" data-a="play" data-v="${esc(r.key)}" aria-label="Play" data-tip="Play from 40%  ·  Space">${playSvg}</button>
    </section>
    <div class="profile" data-tip="The track in a line: mood, energy, movement, where it fits">${esc(profileLine(r))}</div>
    <div style="margin:14px 0 6px">${tagRowsHtml(r)}
      ${extra.length ? `<div class="pills" style="margin-top:10px">${extra.map((t) => `<span class="pill soft">${esc(t)}<button class="x" data-a="untag" data-v="${esc(t)}" aria-label="Remove">×</button></span>`).join("")}</div>` : ""}
      <div class="hash" id="hash"><input class="input" id="hashInput" placeholder="#" autocomplete="off" aria-label="Add a tag" data-tip="Type # and a word, Enter adds it"><div class="hash-menu" id="hashMenu" role="listbox"></div></div>
      <div class="raw" data-tip="What rekordbox's Comments field will say">${esc(r.comment ? `${r.comment} ` : "")}<b>${esc(block)}</b></div>
      ${r.tag_edited ? `<button class="btn flat sm" data-a="resettags" style="margin-top:6px" data-tip="Backspace">Undo my tag edits</button>` : ""}</div>
    ${fold("buckets", "Buckets", r.buckets.map(bucketName).join(", ") || "none", `<div class="pills">${st.buckets.map((b) => `<button class="pill" type="button" data-a="bucket" data-v="${esc(b.id)}" aria-pressed="${r.buckets.includes(b.id)}" data-tip="${esc(b.rule || b.description || "")}${r.bucket_edge.includes(b.id) ? "  ·  on the edge" : ""}">${esc(b.name)}${r.bucket_edge.includes(b.id) ? "?" : ""}</button>`).join("")}</div>`)}
    ${fold("details", "Details", pend ? `<span class="dot led"></span>${pend} staged` : d.editable ? "" : "read only", `<div class="fields">${d.fields.filter((f) => f.id !== "rating").map((f) => fieldHtml(f, d.editable)).join("")}</div>${pend ? `<div class="row" style="margin-top:12px"><button class="btn flat sm" data-a="discard">Discard</button></div>` : ""}`)}
    ${fold("elsewhere", "Elsewhere", d.loading.length ? "asking…" : found.length ? `${found.map((s) => s.name).join(", ")}` : "", `
      ${d.cloud.length ? `<div class="cloud" style="margin-bottom:16px">${d.cloud.map((c) => `<button type="button" data-a="tag" data-v="#${esc(c.tag)}" aria-pressed="${r.tags.includes(`#${c.tag}`)}" style="font-size:${12 + Math.round(c.weight * 18)}px;opacity:${(0.5 + c.weight * 0.5).toFixed(2)}" data-tip="${esc(c.sources.map(srcName).join(", "))}. Click to tag.">${esc(c.label)}${c.sources.length > 1 ? `<sup>${c.sources.length}</sup>` : ""}</button>`).join("")}</div>` : ""}
      <div class="pills" style="margin-bottom:14px">${r.mytags.map((t) => `<span class="pill soft" data-tip="My Tag in rekordbox">${esc(t.split("/").pop())}</span>`).join("")}${r.native.filter((c) => !r.mytags.some((m) => m.toLowerCase().replace(/\s+/g, "-").endsWith(c))).map((c) => `<span class="pill" data-tip="Genre or a word from your comment. Subjetunes learns from these.">${esc(c)}</span>`).join("")}</div>
      <div class="pills">${d.sources.map((s) => {
        const tip = [s.state === "found" ? (s.match != null && s.match < 1 ? `found, ${Math.round(s.match * 100)}% match` : "found") : { none: "not found", off: "off", error: "error", waiting: "asking" }[s.state] || s.state, s.note].filter(Boolean).join(": ");
        const inner = `<span class="dot ${s.state === "found" ? "ok" : s.state === "error" ? "bad" : s.state === "waiting" ? "led" : ""}"></span>${esc(s.name)}`;
        return s.url ? `<a class="pill" href="${esc(s.url)}" target="_blank" rel="noreferrer" data-tip="${esc(tip)}" style="text-decoration:none">${inner}</a>` : `<span class="pill${s.state === "off" || s.state === "none" ? " unsure" : ""}" data-tip="${esc(tip)}">${inner}</span>`;
      }).join("")}</div>
      <button class="btn flat sm" data-a="relookup" style="margin-top:8px">Look up again</button>`)}
    ${fold("analysis", "Analysis", d.grid ? `grid ${d.grid.bpm.toFixed(2)}` : scores.length ? `${scores.length} scores` : "", `<div class="ledger">
      ${d.grid ? `<div class="lr mono"><span>First beat</span><strong>${d.grid.first_beat.toFixed(3)} s</strong></div><div class="lr mono"><span>Grid BPM</span><strong>${d.grid.bpm.toFixed(2)}</strong></div><div class="lr mono"><span>Bars</span><strong>${d.grid.downbeats}</strong></div>` : `<div class="lr"><span>Beat grid</span><strong>Unknown</strong></div>`}
      ${scores.map(([k, v]) => `<div class="lr mono"><span>${esc(k)}</span><strong>${v.toFixed(2)}</strong></div>`).join("")}</div>`)}
  </div>`;
}
async function toggleTag(row, t) { patchRow(await api("tags", row.tags.includes(t) ? { key: row.key, remove: [t] } : { key: row.key, add: [t] })); }
const currentRow = () => (S.page === "tag" ? S.detail?.row : S.rows.find((r) => r.key === S.sel));

function hashOptions(q) {
  const w = q.replace(/^#/, "").trim().toLowerCase();
  if (!w) return [];
  const row = currentRow(), pool = new Map();
  S.status.vocab.forEach((g) => g.tags.forEach((t) => pool.set(t, g.group)));
  (S.detail?.cloud || []).forEach((c) => pool.has(`#${c.tag}`) || pool.set(`#${c.tag}`, c.sources.map(srcName).join(", ")));
  const out = [...pool.entries()].filter(([t]) => t.slice(1).includes(w) && !row?.tags.includes(t)).slice(0, 5).map(([tag, note]) => ({ tag, note }));
  const typed = `#${w.replace(/\s+/g, "-")}`;
  if (!out.some((o) => o.tag === typed)) out.push({ tag: typed, note: "new" });
  return out;
}
function renderHash() {
  const box = $("#hash"), inp = $("#hashInput"), menu = $("#hashMenu");
  if (!box) return;
  S.hopts = hashOptions(inp.value); S.hsel = Math.min(S.hsel || 0, Math.max(0, S.hopts.length - 1));
  box.classList.toggle("open", !!S.hopts.length);
  menu.innerHTML = S.hopts.map((o, i) => `<button class="hash-opt" type="button" aria-selected="${i === S.hsel}" data-a="hash" data-v="${esc(o.tag)}"><b>${esc(o.tag)}</b><small>${esc(o.note)}</small></button>`).join("");
}

// ------------------------------------------------------------------ write
async function loadPlan() { if (S.status?.library?.kind === "masterdb") S.plan = await api("write-plan", { ...S.wopts, skip: [...S.skip] }); else S.plan = null; }
const FIELD_LABEL = { comments: "Comments", title: "Title", artist: "Artist", album: "Album", label: "Label", genre: "Genre", year: "Year", key: "Key", rating: "Rating", remixer: "Remixer", mix_name: "Mix name", isrc: "ISRC", release_date: "Release date", track_no: "Track", disc_no: "Disc", composer: "Composer", original_artist: "Original artist" };
function stampSvg(date) {
  return `<svg class="stamp" viewBox="0 0 150 78" aria-hidden="true"><defs><filter id="ink" x="-5%" y="-10%" width="110%" height="120%">
    <feTurbulence type="fractalNoise" baseFrequency=".85" numOctaves="2" seed="7" result="n"/><feColorMatrix in="n" type="matrix" values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 -1.25 1.45" result="m"/><feComposite in="SourceGraphic" in2="m" operator="in"/></filter></defs>
    <g filter="url(#ink)" style="color:var(--ok)"><rect x="4" y="6" width="142" height="66" rx="6" fill="none" stroke="currentColor" stroke-width="3.2"/><rect x="10" y="12" width="130" height="54" rx="3" fill="none" stroke="currentColor" stroke-width="1.2"/>
    <text x="75" y="44" text-anchor="middle" fill="currentColor" style="font:700 25px var(--din);letter-spacing:2px">WRITTEN</text><text x="75" y="59" text-anchor="middle" fill="currentColor" style="font:600 9px var(--din);letter-spacing:1px">${esc(date)}</text></g></svg>`;
}
function pageWrite() {
  const st = S.status;
  if (!st?.library) return needLibrary();
  if (st.library.kind !== "masterdb") return `<div class="page"><div class="row"><h1 class="title">Write</h1><span class="grow"></span><button class="btn solid" data-a="exportxml" data-tip="This library is an XML file, so you get an XML to import">Write XML</button></div>${S.lastExport ? `<p class="meta">${esc(S.lastExport.path)}</p>` : ""}</div>`;
  const p = S.plan, rc = S.receipt, running = st.rekordbox_running;
  const groups = [];
  for (const h of p?.hunks || []) { const cid = h.id.split(":")[0]; let g = groups[groups.length - 1]; if (!g || g.cid !== cid) groups.push((g = { cid, title: h.title, artist: h.artist, hunks: [] })); g.hunks.push(h); }
  const shown = groups.slice(0, S.wlimit || 80);
  const keptPl = (p?.playlists || []).filter((x) => !x.skipped);
  const can = p && (p.kept || keptPl.length) && !running && !S.writing;
  const opt = (id, label, tip) => `<button class="toggle" data-a="wopt" data-v="${id}" aria-pressed="${S.wopts[id]}" data-tip="${esc(tip)}"><span class="sw"><i></i></span>${label}</button>`;
  const slip = rc ? `<div class="receipt-slip" style="margin-bottom:24px">${stampSvg(rc.at?.replace("T", " ").slice(0, 16) || "")}<h3>Receipt</h3>
      <div class="ledger"><div class="lr"><span>Changes</span><strong>${rc.changes ?? 0}</strong></div><div class="lr"><span>Tracks</span><strong>${rc.tracks}</strong></div><div class="lr"><span>Playlists</span><strong>${Object.keys(rc.playlists || {}).length}</strong></div>
      ${rc.folder ? `<div class="lr"><span>Folder</span><strong>${esc(rc.folder)}</strong></div>` : ""}<div class="lr" data-tone="ok"><span>Read back</span><strong>every value</strong></div><div class="lr"><span>Backup</span><strong data-tip="${esc(rc.backup || "")}">${esc((rc.backup || "").split(/[\\/]/).pop())}</strong></div></div>
      <div class="row" style="margin-top:12px"><button class="btn sm" data-a="reveal" data-v="${esc(rc.backup || "")}">Show backup</button><button class="btn flat sm" data-a="receiptdone">Done</button></div></div>` : "";
  return `<div class="page">
    <div class="row" style="margin-bottom:12px"><h1 class="title">Write</h1><span class="meta">${p ? `${p.kept} changes, ${p.tracks} tracks${keptPl.length ? `, ${keptPl.length} playlists` : ""}` : ""}</span></div>
    <div class="row" style="gap:22px;margin-bottom:16px">${opt("fields", "Details", `${st.pending} staged field edits`)}${opt("tags", "Tags", "Tags into Comments, for this batch. Your own comment text stays.")}${opt("playlists", "Playlists", `One playlist per bucket under Subjetunes/${st.batch_name || "Batch"}. Nothing existing changes.`)}
      ${(p?.playlists || []).length && S.wopts.playlists ? `<span class="pills">${p.playlists.map((x) => `<button class="pill" type="button" data-a="hunk" data-v="${esc(x.id)}" aria-pressed="${!x.skipped}" data-tip="${x.count} tracks">${esc(x.name)}</button>`).join("")}</span>` : ""}</div>
    ${slip}
    ${p && p.total ? `<div class="hunks">${shown.map((g) => {
      const all = g.hunks.every((h) => h.skipped);
      return `<details class="${all ? "skipped" : ""}"><summary><b>${esc(g.title)}</b><span>${esc(g.artist)}</span><span class="n">${g.hunks.map((h) => FIELD_LABEL[h.field] || h.field).join(", ")}</span>
        <button class="btn flat sm" data-a="hunktrack" data-v="${esc(g.cid)}">${all ? "Keep" : "Skip"}</button></summary>
        ${g.hunks.map((h) => `<div class="hl${h.skipped ? " skipped" : ""}"><span class="k">${esc(FIELD_LABEL[h.field] || h.field)}</span><div class="d">${h.old ? `<div class="rm">${esc(h.old)}</div>` : ""}<div class="ad">${esc(h.new || "(empty)")}</div></div><button class="keep" data-a="hunk" data-v="${esc(h.id)}">${h.skipped ? "keep" : "skip"}</button></div>`).join("")}</details>`;
    }).join("")}</div>${groups.length > shown.length ? `<div class="row" style="justify-content:center;margin-top:10px"><button class="btn flat" data-a="wmore">${groups.length - shown.length} more tracks</button></div>` : ""}`
      : rc ? "" : `<div class="empty"><canvas class="vessel" data-vessel></canvas><h2>Nothing to write</h2></div>`}
    <div class="dockbar" ${rc && !can ? "hidden" : ""}>
      <button class="hold" data-hold ${can ? "" : "disabled"} aria-label="Hold to write" data-tip="${running ? "Close rekordbox first. Everything stays staged." : "Hold to write: backup first, one transaction, every value read back"}"><canvas></canvas></button>
      <div><div class="big">${S.writing ? "Writing…" : can ? `Hold to write ${p.kept} change${p.kept === 1 ? "" : "s"}` : running ? "rekordbox is open" : "Nothing to write"}</div><div class="meta">${running ? "Close it to write" : "master.db, backup first"}</div></div>
      <span class="grow"></span><span class="lamp ${running ? "led" : "ok"}" data-tip="${running ? "rekordbox is open" : "rekordbox is closed"}"></span></div></div>`;
}

// ------------------------------------------------------------------ settings
const SRC_FIELDS = { lastfm: ["api_key"], discogs: ["token"], soundcloud: ["client_id", "client_secret"], beets: ["library"], lexicon: ["url"] };
const SRC_HINT = { rekordbox: "your library", file: "tags inside the audio file", beets: "path to beets library.db", lexicon: "Lexicon running, Local API on", musicbrainz: "no key needed", lastfm: "free key: last.fm/api", discogs: "personal token from Discogs settings", soundcloud: "app client id and secret", beatport: "experimental, reads their website" };
function ago(t) { if (!t) return "never"; const s = Date.now() / 1000 - t; return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`; }
function libInfoHtml() {
  const i = S.libInfo;
  if (!i) return "";
  const rows = i.candidates.map((c) => `<div class="lr"><span><span class="dot ${c.exists ? "ok" : ""}"></span>${esc(c.how)}</span><strong class="mono" style="font:500 12px var(--mono)">${esc(c.path)}</strong></div>`).join("");
  const inst = i.installs.length ? i.installs.map((x) => `rekordbox ${esc(x.version || "?")}`).join(", ") : "no rekordbox app found (the library can still be opened)";
  return `<div class="ledger" style="margin-bottom:10px" data-tip="Where Subjetunes looked for master.db, in order">${rows}</div><p class="meta" style="margin:0 0 10px">${inst}  ·  your data: ${esc(i.home)}</p>`;
}
function pageSettings() {
  const st = S.status;
  if (!st) return "";
  const src = st.sources.map((s) => {
    const on = s.settings.enabled ?? s.default_on, state = !s.ready ? (on ? "setup" : "off") : s.online ? "ready" : "local", x = s.stats || {};
    const open = S.srcOpen === s.id;
    return `<div class="lr" style="display:block"><div class="row" style="flex-wrap:nowrap"><button class="btn flat" style="padding:0;color:var(--ink)" data-a="srcopen" data-v="${s.id}" data-tip="${esc(SRC_HINT[s.id] || "")}">${esc(s.name)}</button><span class="grow"></span>
      ${s.online ? `<span class="meta" data-tip="found, not found, errors  ·  last answer ${ago(x.last)}">${x.found || 0} / ${x.none || 0} / ${x.error || 0}</span>` : ""}<span class="dot ${state === "ready" || state === "local" ? "ok" : state === "setup" ? "led" : ""}" data-tip="${state}"></span></div>
      ${open ? `<div class="row" style="margin-top:8px">${s.id !== "rekordbox" ? `<button class="toggle" data-a="srcon" data-v="${s.id}" aria-pressed="${on}"><span class="sw"><i></i></span></button>` : ""}${(SRC_FIELDS[s.id] || []).map((k) => `<input class="input" data-src="${s.id}" data-k="${k}" placeholder="${k.replace("_", " ")}" value="${esc(s.settings[k] || "")}" style="flex:1;min-width:150px">`).join("")}${(SRC_FIELDS[s.id] || []).length ? `<button class="btn sm" data-a="srcsave" data-v="${s.id}">Save</button>` : ""}${s.why ? `<span class="meta" style="flex-basis:100%;color:var(--led)">${esc(s.why)}</span>` : ""}</div>` : ""}</div>`;
  }).join("");
  const buckets = `<div class="ledger">${st.buckets.map((b, i) => `<div class="lr" style="display:grid;grid-template-columns:170px 1fr 48px 28px;gap:10px;align-items:start">
      <input class="input" data-b="${i}" data-f="name" value="${esc(b.name)}" aria-label="Name"><div><input class="input mono ${b.error ? "bad" : ""}" style="width:100%" data-b="${i}" data-f="rule" value="${esc(b.rule)}" placeholder="${b.use_llm ? "decided by the LLM" : "energy >= 0.75 and looseness < 0.5"}" aria-label="Rule" data-tip="energy, swing, looseness, bpm, camelot, rating, year, mood_*, comp_*, crit_*, and &quot;dubstep&quot; in clues">
      <div class="meta" id="err${i}" style="${b.error ? "color:var(--bad)" : ""}">${esc(b.error || "")}</div></div><strong style="padding-top:8px" data-tip="Tracks in this batch">${b.count}</strong><button class="btn flat sm" data-a="bucketdel" data-v="${i}" aria-label="Remove">×</button></div>`).join("")}</div>
    <div class="row" style="margin-top:10px"><button class="btn sm" data-a="bucketadd">Add</button><button class="btn solid sm" data-a="bucketsave">Save buckets</button></div>`;
  const vocab = `${st.problems.length ? `<div class="ledger" style="margin-bottom:10px">${st.problems.map((p) => `<div class="lr" data-tone="bad"><span>Problem</span><strong>${esc(p)}</strong></div>`).join("")}</div>` : ""}
    ${tagRowsHtml(null, { track: false })}<p class="meta" style="margin-top:10px" data-tip="Your renames and additions are saved to data\\user.yaml">Double-click a tag to rename it, + adds one.</p>`;
  return `<div class="page"><h1 class="title" style="margin-bottom:14px">Settings</h1>
    ${fold("sources", "Sources", `${st.sources.filter((s) => s.ready).length} ready`, `<div class="ledger">${src}</div>`)}
    ${fold("rules", "Buckets", st.buckets.map((b) => b.name).join(", "), buckets)}
    ${fold("vocab", "Tag vocabulary", st.problems.length ? `${st.problems.length} problems` : `${st.vocab.reduce((n, g) => n + g.tags.length, 0)} tags`, vocab)}
    ${fold("scores", "Scores", st.demo ? "demo" : st.source || "", `<button class="toggle" data-a="demo" aria-pressed="${st.demo}" data-tip="Fake numbers for trying the workflow. Turn off before writing."><span class="sw"><i></i></span>Demo scores</button>`)}
    ${fold("lib", "Library", esc(st.library?.path || "none"), `${libInfoHtml()}<div class="row"><input class="input" id="libpath" style="flex:1" placeholder="Another master.db or collection XML"><button class="btn sm" data-a="openpath">Open</button><button class="btn flat sm" data-a="opendb">Use my rekordbox library</button></div><p class="meta" style="margin-top:10px">Data: ${esc(st.home)}\\data</p>`)}
  </div>`;
}

// ------------------------------------------------------------------ components
function canvasFit(cv) {
  const d = DPR(), w = cv.clientWidth, h = cv.clientHeight;
  cv.width = Math.round(w * d); cv.height = Math.round(h * d);
  const ctx = cv.getContext("2d"); ctx.setTransform(d, 0, 0, d, 0, 0);
  return { ctx, w, h };
}

// Detent knob: six stops (0 = no energy, 1–5), spring-snaps, tick marks in the energy colours.
function knob(cv) {
  const { ctx, w, h } = canvasFit(cv);
  const cx = w / 2, cy = h / 2, R = Math.min(w, h) * 0.34;
  const A0 = -135, STEP = 54, angOf = (v) => ((A0 + v * STEP) * Math.PI) / 180;
  let value = +cv.dataset.v || 0, target = angOf(value), ang = knob.last ?? target, vel = 0, flash = 1, raf = 0, drag = null;
  const col = (v) => (v ? css(`--e${v}`) : css("--faint"));
  function draw() {
    ctx.clearRect(0, 0, w, h);
    for (let v = 0; v <= 5; v++) {
      const a = angOf(v) - Math.PI / 2, on = v === value;
      ctx.strokeStyle = col(v); ctx.globalAlpha = on ? 1 : v ? 0.35 : 0.25; ctx.lineWidth = on ? 3 : 2; ctx.lineCap = "round";
      ctx.beginPath(); ctx.moveTo(cx + Math.cos(a) * (R + 7), cy + Math.sin(a) * (R + 7)); ctx.lineTo(cx + Math.cos(a) * (R + (on ? 15 + flash * 3 : 12)), cy + Math.sin(a) * (R + (on ? 15 + flash * 3 : 12))); ctx.stroke();
    }
    ctx.globalAlpha = 1;
    const g = ctx.createRadialGradient(cx - R * 0.3, cy - R * 0.4, R * 0.1, cx, cy, R);
    g.addColorStop(0, css("--raise")); g.addColorStop(1, css("--cap"));
    ctx.fillStyle = css("--knurl"); ctx.beginPath(); ctx.arc(cx, cy, R, 0, 7); ctx.fill();
    ctx.strokeStyle = css("--line-2"); ctx.lineWidth = 1;
    for (let k = 0; k < 40; k++) { const a = ang + (k / 40) * Math.PI * 2; ctx.beginPath(); ctx.moveTo(cx + Math.cos(a) * R * 0.86, cy + Math.sin(a) * R * 0.86); ctx.lineTo(cx + Math.cos(a) * R, cy + Math.sin(a) * R); ctx.stroke(); }
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, R * 0.8, 0, 7); ctx.fill();
    const a = ang - Math.PI / 2;
    ctx.strokeStyle = value ? col(value) : css("--ink"); ctx.lineWidth = 3; ctx.lineCap = "round";
    ctx.beginPath(); ctx.moveTo(cx + Math.cos(a) * R * 0.28, cy + Math.sin(a) * R * 0.28); ctx.lineTo(cx + Math.cos(a) * R * 0.72, cy + Math.sin(a) * R * 0.72); ctx.stroke();
  }
  function tick() {
    raf = 0;
    if (!cv.isConnected) return;
    vel = (vel + (target - ang) * 0.2) * 0.62; ang += vel; flash *= 0.9; knob.last = ang;
    draw();
    if (!reduce && (Math.abs(target - ang) > 0.002 || Math.abs(vel) > 0.002 || flash > 0.02)) raf = requestAnimationFrame(tick); else { ang = target; draw(); }
  }
  const kick = () => { if (!raf) raf = requestAnimationFrame(tick); };
  const set = (v, commit) => { v = Math.max(0, Math.min(5, v)); if (v !== value) { value = v; flash = 1; } target = angOf(v); kick(); if (commit) A.energy(String(v)); };
  const fromPoint = (e) => { const b = cv.getBoundingClientRect(), x = e.clientX - b.left - cx, y = e.clientY - b.top - cy; let deg = (Math.atan2(y, x) * 180) / Math.PI + 90; if (deg > 180) deg -= 360; return Math.round((deg - A0) / STEP); };
  cv.onpointerdown = (e) => { cv.setPointerCapture(e.pointerId); drag = { y: e.clientY, v: value, moved: false }; };
  cv.onpointermove = (e) => { if (!drag) return; const dv = Math.round((drag.y - e.clientY) / 22); if (dv) drag.moved = true; if (drag.moved) set(drag.v + dv, false); };
  cv.onpointerup = (e) => { if (!drag) return; const moved = drag.moved; drag = null; set(moved ? value : fromPoint(e), true); };
  cv.onwheel = (e) => { e.preventDefault(); set(value + (e.deltaY < 0 ? 1 : -1), true); };
  cv.onkeydown = (e) => { if (["ArrowUp", "ArrowRight"].includes(e.key)) { e.preventDefault(); e.stopPropagation(); set(value + 1, true); } if (["ArrowDown", "ArrowLeft"].includes(e.key)) { e.preventDefault(); e.stopPropagation(); set(value - 1, true); } };
  draw(); kick();
}

// Split-flap: each flap steps through characters until it lands on the new one.
const FLAP_POOL = " -0123456789.ABCDEFGHIJKLMNOPQRSTUVWXYZ";
function flapsAnimate(el) {
  const id = el.dataset.flaps, to = el.dataset.v, from = S.flapPrev[id];
  S.flapPrev[id] = to;
  if (!from || from === to || reduce) return;
  const flaps = $$(".flap", el), cur = [...from.padStart(to.length, " ").slice(-to.length)];
  flaps.forEach((f, i) => { f.firstChild.textContent = cur[i] === " " ? "\u00a0" : cur[i]; });
  let steps = 0;
  const timer = setInterval(() => {
    let moving = false;
    flaps.forEach((f, i) => {
      if (cur[i] === to[i]) return;
      moving = true;
      cur[i] = FLAP_POOL[(FLAP_POOL.indexOf(cur[i]) + 1) % FLAP_POOL.length] || to[i];
      if (steps > 30) cur[i] = to[i];
      f.firstChild.textContent = cur[i] === " " ? "\u00a0" : cur[i];
      f.classList.remove("spin"); void f.offsetWidth; f.classList.add("spin");
    });
    steps++;
    if (!moving || !el.isConnected) clearInterval(timer);
  }, 38);
}

// HI / MID / LOW meters fed by the audio that's playing (Web Audio analyser on the <audio> element).
const SEG = 12;
const AUD = { ctx: null, an: null, data: null, peaks: [0, 0, 0], raf: 0 };
function buildBands(el) {
  if (el.childElementCount) return;
  const n = el.classList.contains("sm") ? 8 : SEG;
  el.innerHTML = ["HI", "MID", "LOW"].map((b) => `<div class="band">${Array.from({ length: n }, (_, i) => `<i class="${i >= n - 2 ? "h" : i >= n - 5 ? "m" : ""}"></i>`).join("")}<b>${b}</b></div>`).join("");
  // column-reverse puts the first <i> at the bottom, so the last two are the red ones up top
  $$(".band", el).forEach((band) => { const is = $$("i", band); is.forEach((x, i) => { x.className = i >= n - 2 ? "h" : i >= n - 5 ? "m" : ""; }); });
}
function ensureAudioGraph() {
  if (AUD.ctx) { if (AUD.ctx.state === "suspended") AUD.ctx.resume(); return; }
  try {
    const a = $("#audio"), ctx = new AudioContext();
    const src = ctx.createMediaElementSource(a), an = ctx.createAnalyser();
    an.fftSize = 4096; an.smoothingTimeConstant = 0.55;
    src.connect(an); an.connect(ctx.destination);
    Object.assign(AUD, { ctx, an, data: new Float32Array(an.frequencyBinCount) });
  } catch (e) { console.warn("band meters unavailable", e); }
}
function bandLevels() {
  const { an, data, ctx } = AUD;
  an.getFloatFrequencyData(data);
  const hz = ctx.sampleRate / 2 / data.length, acc = [0, 0, 0];
  for (let i = 1; i < data.length; i++) {
    const f = i * hz, p = Math.pow(10, data[i] / 10);
    acc[f < XOVER.lowMid ? 2 : f < XOVER.midHigh ? 1 : 0] += p;   // order: HI, MID, LOW
  }
  // dB per band; HI gets a lift so a normal mix reads similar heights across bands
  return acc.map((p, i) => Math.max(0, Math.min(1, (10 * Math.log10(p + 1e-12) + [64, 58, 52][i]) / 52)));
}
function metersLoop() {
  AUD.raf = 0;
  const a = $("#audio");
  if (!AUD.an || a.paused) { $$("[data-bands] i.on, [data-bands] i.peak").forEach((x) => x.classList.remove("on", "peak")); return; }
  const lv = bandLevels();
  AUD.peaks = AUD.peaks.map((p, i) => Math.max(lv[i], p - 0.008));
  $$("[data-bands]").forEach((el) => {
    $$(".band", el).forEach((band, b) => {
      const is = $$("i", band), n = is.length, lit = Math.round(lv[b] * n), pk = Math.min(n - 1, Math.round(AUD.peaks[b] * n));
      is.forEach((x, i) => { x.classList.toggle("on", i < lit); x.classList.toggle("peak", i === pk && pk >= lit && pk > 0); });
    });
  });
  AUD.raf = requestAnimationFrame(metersLoop);
}

// Hold to confirm: a tick ring fills while held; letting go early cancels.
function holdButton(btn) {
  const cv = $("canvas", btn), { ctx, w, h } = canvasFit(cv), cx = w / 2, cy = h / 2, R = w * 0.38, HOLD = 1100;
  let p = 0, t0 = 0, raf = 0, state = "idle", flash = 0;
  const draw = () => {
    ctx.clearRect(0, 0, w, h);
    for (let k = 0; k < 28; k++) {
      const a = (k / 28) * Math.PI * 2 - Math.PI / 2, on = k / 28 < p;
      ctx.strokeStyle = state === "abort" ? css("--bad") : on ? css("--ink") : css("--line-2"); ctx.globalAlpha = state === "abort" ? flash : 1;
      ctx.lineWidth = on ? 2.4 : 1.6; ctx.lineCap = "round";
      ctx.beginPath(); ctx.moveTo(cx + Math.cos(a) * (R - 5), cy + Math.sin(a) * (R - 5)); ctx.lineTo(cx + Math.cos(a) * (R + 2), cy + Math.sin(a) * (R + 2)); ctx.stroke();
    }
    ctx.globalAlpha = 1;
    ctx.save(); ctx.translate(cx, cy);
    if (state === "done") { ctx.strokeStyle = css("--ok"); ctx.lineWidth = 3; ctx.beginPath(); ctx.moveTo(-8, 0); ctx.lineTo(-2, 6); ctx.lineTo(9, -6); ctx.stroke(); }
    else { ctx.rotate(p * Math.PI / 4); ctx.fillStyle = btn.disabled ? css("--faint") : css("--ink"); const s = 9 + p * 3; ctx.fillRect(-s / 2, -s / 2, s, s); }
    ctx.restore();
  };
  const loop = (now) => {
    raf = 0;
    if (state === "hold") { p = Math.min(1, (now - t0) / HOLD); if (p >= 1) { state = "done"; draw(); return run(A.write); } }
    else if (state === "abort") { p = Math.max(0, p - 0.06); flash *= 0.9; if (p <= 0 && flash < 0.05) state = "idle"; }
    draw();
    if (state !== "idle" && btn.isConnected) raf = requestAnimationFrame(loop);
  };
  const down = (e) => { if (btn.disabled || state === "done") return; e?.preventDefault?.(); state = "hold"; t0 = performance.now() - p * HOLD; if (!raf) raf = requestAnimationFrame(loop); };
  const up = () => { if (state === "hold") { state = "abort"; flash = 1; if (!raf) raf = requestAnimationFrame(loop); } };
  btn.onpointerdown = down; btn.onpointerup = up; btn.onpointerleave = up;
  btn.onkeydown = (e) => { if ((e.key === " " || e.key === "Enter") && !e.repeat) { e.stopPropagation(); down(e); } };
  btn.onkeyup = (e) => { if (e.key === " " || e.key === "Enter") up(); };
  draw();
}

// BPM × energy map: one dot per track, energy rows, beeswarm within each row.
function swarm(cv) {
  const { ctx, w, h } = canvasFit(cv);
  const rows = S.rows.filter((r) => r.bpm);
  if (!rows.length) { ctx.fillStyle = css("--faint"); ctx.font = "13px " + css("--ui"); ctx.fillText("No BPM in this batch", 16, 24); return; }
  let lo = Math.min(...rows.map((r) => r.bpm)), hi = Math.max(...rows.map((r) => r.bpm));
  if (hi - lo < 10) { lo -= 5; hi += 5; }
  const pad = { l: 26, r: 14, t: 12, b: 24 }, rowH = (h - pad.t - pad.b) / 6, r0 = Math.max(2.2, Math.min(4, rowH / 7));
  const X = (b) => pad.l + ((b - lo) / (hi - lo)) * (w - pad.l - pad.r), Y = (l) => pad.t + (5 - l + 0.5) * rowH;
  const B = (x) => lo + ((x - pad.l) / (w - pad.l - pad.r)) * (hi - lo), L = (y) => Math.max(0, Math.min(5, 5 - Math.floor((y - pad.t) / rowH)));
  // greedy beeswarm: within its row, a dot takes the nearest free vertical slot
  const placed = rows.map((r) => ({ r, l: levelOf(r), x: X(r.bpm) })).sort((a, b) => a.x - b.x);
  const byRow = {};
  for (const d of placed) {
    const list = (byRow[d.l] ||= []), cy = Y(d.l);
    for (let k = 0; k < 40; k++) {
      const dy = k === 0 ? 0 : Math.ceil(k / 2) * r0 * 1.9 * (k % 2 ? -1 : 1);
      if (Math.abs(dy) > rowH / 2 - r0) { d.y = cy + (Math.random() - 0.5) * (rowH - 2 * r0); break; }
      if (!list.some((o) => Math.abs(o.x - d.x) < r0 * 2 && Math.abs(o.y - (cy + dy)) < r0 * 2)) { d.y = cy + dy; break; }
    }
    d.y ??= cy; list.push(d);
  }
  const t0 = performance.now(), sel = new Set(visibleRows().map((r) => r.key));
  let brush = null, hover = null;
  const draw = (now) => {
    const p = reduce ? 1 : Math.min(1, (now - t0) / 700);
    ctx.clearRect(0, 0, w, h);
    for (let l = 0; l <= 5; l++) { ctx.fillStyle = l ? css(`--e${l}`) : css("--faint"); ctx.globalAlpha = 0.9; ctx.fillRect(6, Y(l) - rowH * 0.3, 3, rowH * 0.6); }
    ctx.globalAlpha = 1; ctx.fillStyle = css("--faint"); ctx.font = `600 10px ${css("--din")}`; ctx.textAlign = "center";
    const step = (hi - lo) > 40 ? 10 : 5;
    for (let b = Math.ceil(lo / step) * step; b <= hi; b += step) ctx.fillText(String(b), X(b), h - 7);
    if (S.region) { ctx.fillStyle = css("--line"); const x1 = X(S.region.bpm[0]), x2 = X(S.region.bpm[1]), y1 = Y(S.region.lv[1]) - rowH / 2, y2 = Y(S.region.lv[0]) + rowH / 2; ctx.fillRect(x1, y1, x2 - x1, y2 - y1); }
    for (const d of placed) {
      const e = 1 - Math.pow(1 - Math.max(0, Math.min(1, p * 1.6 - (d.x / w) * 0.6)), 3);
      ctx.globalAlpha = sel.has(d.r.key) ? 0.95 : 0.15;
      ctx.fillStyle = d.l ? css(`--e${d.l}`) : css("--faint");
      ctx.beginPath(); ctx.arc(d.x, -10 + (d.y + 10) * e, d === hover ? r0 + 1.5 : r0, 0, 7); ctx.fill();
      if (d.r.key === S.sel) { ctx.globalAlpha = 1; ctx.strokeStyle = css("--ink"); ctx.lineWidth = 1.5; ctx.beginPath(); ctx.arc(d.x, d.y, r0 + 3, 0, 7); ctx.stroke(); }
    }
    ctx.globalAlpha = 1;
    if (brush) { ctx.strokeStyle = css("--ink"); ctx.setLineDash([3, 3]); ctx.strokeRect(brush.x0, brush.y0, brush.x1 - brush.x0, brush.y1 - brush.y0); ctx.setLineDash([]); }
    if (p < 1 && cv.isConnected) requestAnimationFrame(draw);
  };
  const pt = (e) => { const b = cv.getBoundingClientRect(); return [e.clientX - b.left, e.clientY - b.top]; };
  const nearest = (x, y) => { let best = null, bd = 64; for (const d of placed) { const dd = (d.x - x) ** 2 + (d.y - y) ** 2; if (dd < bd) { bd = dd; best = d; } } return best; };
  cv.onpointerdown = (e) => { cv.setPointerCapture(e.pointerId); const [x, y] = pt(e); brush = { x0: x, y0: y, x1: x, y1: y }; };
  cv.onpointermove = (e) => {
    const [x, y] = pt(e);
    if (brush) { brush.x1 = x; brush.y1 = y; return draw(performance.now() + 1e4); }
    const n = nearest(x, y);
    if (n !== hover) { hover = n; cv.dataset.tip = n ? `${n.r.title}  ·  ${n.r.artist}\n${n.r.bpm.toFixed(1)} bpm, energy ${n.l || "unset"}` : "BPM × energy. Drag to filter, click a dot to open it, double-click to clear."; tipShow(cv); draw(performance.now() + 1e4); }
  };
  cv.onpointerup = (e) => {
    const b = brush; brush = null;
    if (!b) return;
    if (Math.abs(b.x1 - b.x0) < 5 && Math.abs(b.y1 - b.y0) < 5) { const n = nearest(b.x0, b.y0); if (n) return run(() => openTag(n.r.key)); return draw(performance.now() + 1e4); }
    const bp = [B(Math.min(b.x0, b.x1)), B(Math.max(b.x0, b.x1))], lv = [L(Math.max(b.y0, b.y1)), L(Math.min(b.y0, b.y1))];
    S.region = { bpm: bp, lv }; S.limit = 300; renderRows(); swarm(cv);
  };
  cv.ondblclick = () => { S.region = null; renderRows(); swarm(cv); };
  requestAnimationFrame(draw);
}

// Camelot wheel: outer ring B (major), inner ring A (minor). Click to filter; hover shows what mixes.
function wheel(cv) {
  const { ctx, w, h } = canvasFit(cv), cx = w / 2, cy = h / 2, R = Math.min(w, h) / 2 - 4;
  const count = {};
  S.rows.forEach((r) => { if (r.tonality) count[r.tonality] = (count[r.tonality] || 0) + 1; });
  const max = Math.max(1, ...Object.values(count));
  let hover = null;
  const seg = (n, letter) => ({ key: `${n}${letter}`, a0: ((n % 12) * 30 - 105) * Math.PI / 180, a1: ((n % 12) * 30 - 75) * Math.PI / 180, r0: letter === "B" ? R * 0.68 : R * 0.36, r1: letter === "B" ? R : R * 0.66 });
  const all = []; for (let n = 1; n <= 12; n++) all.push(seg(n, "B"), seg(n, "A"));
  const compat = (k) => { const n = parseInt(k), l = k.slice(-1); return new Set([`${n}${l}`, `${(n % 12) + 1}${l}`, `${((n + 10) % 12) + 1}${l}`, `${n}${l === "A" ? "B" : "A"}`]); };
  const draw = () => {
    ctx.clearRect(0, 0, w, h);
    const near = hover ? compat(hover.key) : null;
    for (const s of all) {
      const c = count[s.key] || 0, on = S.keys.has(s.key);
      ctx.beginPath(); ctx.arc(cx, cy, s.r1, s.a0 + 0.02, s.a1 - 0.02); ctx.arc(cx, cy, s.r0, s.a1 - 0.02, s.a0 + 0.02, true); ctx.closePath();
      ctx.fillStyle = on ? css("--ink") : css("--raise"); ctx.globalAlpha = on ? 1 : 0.35 + 0.65 * (c / max); ctx.fill();
      if (near?.has(s.key)) { ctx.globalAlpha = 1; ctx.strokeStyle = css("--ink"); ctx.lineWidth = s === hover ? 2 : 1; ctx.setLineDash(s === hover ? [] : [3, 2]); ctx.stroke(); ctx.setLineDash([]); }
      if (c || on || s === hover) {
        const am = (s.a0 + s.a1) / 2, rm = (s.r0 + s.r1) / 2;
        ctx.globalAlpha = 1; ctx.fillStyle = on ? css("--bg") : c ? css("--ink") : css("--faint");
        ctx.font = `600 ${s.key.endsWith("B") ? 11 : 10}px ${css("--din")}`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.fillText(s.key, cx + Math.cos(am) * rm, cy + Math.sin(am) * rm);
      }
    }
    ctx.globalAlpha = 1;
  };
  const hit = (e) => {
    const b = cv.getBoundingClientRect(), x = e.clientX - b.left - cx, y = e.clientY - b.top - cy, r = Math.hypot(x, y);
    let a = Math.atan2(y, x); return all.find((s) => r >= s.r0 && r <= s.r1 && ((a >= s.a0 && a < s.a1) || (a + 2 * Math.PI >= s.a0 && a + 2 * Math.PI < s.a1))) || null;
  };
  cv.onpointermove = (e) => { const s = hit(e); if (s !== hover) { hover = s; cv.dataset.tip = s ? `${s.key}: ${count[s.key] || 0} tracks. Dashed keys mix with it.` : "Camelot keys. Click to filter."; tipShow(cv); draw(); } };
  cv.onpointerleave = () => { hover = null; draw(); };
  cv.onclick = (e) => { const s = hit(e); if (!s) return; S.keys.has(s.key) ? S.keys.delete(s.key) : S.keys.add(s.key); S.limit = 300; renderRows(); draw(); const sw = $("canvas[data-swarm]"); if (sw) swarm(sw); };
  cv.dataset.tip = "Camelot keys. Click to filter.";
  draw();
}

// Bucket lanes: drag a card to another lane. Moving changes buckets; Ctrl/Alt copies.
let drag = null;
document.addEventListener("pointerdown", (e) => {
  const card = e.target.closest(".card[data-card]");
  if (!card || e.button !== 0) return;
  drag = { card, key: card.dataset.card, from: card.closest(".lane").dataset.lane, x: e.clientX, y: e.clientY, started: false, copy: e.ctrlKey || e.altKey };
});
document.addEventListener("pointermove", (e) => {
  if (!drag) return;
  if (!drag.started) {
    if (Math.hypot(e.clientX - drag.x, e.clientY - drag.y) < 5) return;
    const r = drag.card.getBoundingClientRect();
    drag.started = true; drag.dx = e.clientX - r.left; drag.dy = e.clientY - r.top;
    drag.float = drag.card.cloneNode(true); drag.float.classList.add("float"); drag.float.style.width = `${r.width}px`; document.body.appendChild(drag.float);
    drag.ghost = document.createElement("div"); drag.ghost.className = "ghost"; drag.ghost.style.height = `${r.height}px`;
    drag.card.style.opacity = drag.copy ? ".5" : "0";
  }
  drag.float.style.left = `${e.clientX - drag.dx}px`; drag.float.style.top = `${e.clientY - drag.dy}px`;
  const lane = document.elementsFromPoint(e.clientX, e.clientY).find((el) => el.classList?.contains("lane"));
  $$(".lane.over").forEach((l) => l !== lane && l.classList.remove("over"));
  if (lane) {
    lane.classList.add("over");
    const body = $(".lane-body", lane), after = [...body.querySelectorAll(".card:not(.float)")].find((c) => c.getBoundingClientRect().top + c.offsetHeight / 2 > e.clientY);
    body.insertBefore(drag.ghost, after || null);
  }
  drag.to = lane?.dataset.lane;
});
document.addEventListener("pointerup", () => run(async () => {
  const d = drag; drag = null;
  if (!d) return;
  if (!d.started) return openTag(d.key);
  d.float.remove(); d.ghost.remove(); $$(".lane.over").forEach((l) => l.classList.remove("over"));
  if (d.to === undefined || d.to === d.from) { d.card.style.opacity = ""; return; }
  const body = { key: d.key, add: d.to ? [d.to] : [], remove: !d.copy && d.from ? [d.from] : [] };
  if (!d.to) body.remove = d.from ? [d.from] : [];
  const res = await api("buckets/edit", body);
  if (res.row) S.rows = S.rows.map((r) => (r.key === res.row.key ? res.row : r));
  S.status = res.status; renderShell(); renderRows();
}));

// Empty vessel with drifting motes; click to scatter them.
function vessel(cv) {
  const { ctx, w, h } = canvasFit(cv), N = 20, cx = w / 2, cy = h / 2 + 8, u = Math.min(w, h) / 200;
  const m = Array.from({ length: N }, (_, i) => { const a = (i / N) * 6.283, r = (24 + ((i * 53) % 44)) * u, hx = cx + Math.cos(a) * r, hy = cy - 28 * u + Math.sin(a) * r * 0.5; return { hx, hy, x: hx, y: hy, vx: 0, vy: 0, r: (1 + ((i * 7) % 9) * 0.15) * u, p: Math.random() * 6.28 }; });
  const ink = css("--ink"), faint = css("--faint"), line = css("--line-2"), sunk = css("--sunk");
  cv.onpointerdown = (e) => { const b = cv.getBoundingClientRect(), px = e.clientX - b.left, py = e.clientY - b.top; m.forEach((o) => { const dx = o.x - px, dy = o.y - py, d = Math.hypot(dx, dy) + 1; o.vx += (dx / d) * 320 * u / (d + 30); o.vy += (dy / d) * 320 * u / (d + 30); }); };
  function frame(t) {
    if (!cv.isConnected) return;
    ctx.clearRect(0, 0, w, h);
    const rw = 54 * u, rh = 16 * u, dep = 26 * u;
    ctx.save(); ctx.translate(cx, cy + 14 * u);
    ctx.fillStyle = sunk; ctx.beginPath(); ctx.ellipse(0, 0, rw * 0.92, rh * 0.84, 0, 0, 7); ctx.fill();
    ctx.strokeStyle = line; ctx.lineWidth = 2 * u; ctx.beginPath(); ctx.ellipse(0, 0, rw, rh, 0, 0, 7); ctx.stroke();
    ctx.strokeStyle = faint; ctx.beginPath(); ctx.moveTo(-rw, 0); ctx.bezierCurveTo(-rw, dep * 0.9, -rw * 0.62, dep, 0, dep); ctx.bezierCurveTo(rw * 0.62, dep, rw, dep * 0.9, rw, 0); ctx.stroke();
    ctx.restore();
    for (const o of m) {
      const ww = reduce ? 0 : t * 0.0009 + o.p, tx = o.hx + Math.cos(ww) * 6 * u, ty = o.hy + Math.sin(ww * 1.3) * 5 * u;
      o.vx = (o.vx + (tx - o.x) * 0.02) * 0.9; o.vy = (o.vy + (ty - o.y) * 0.02) * 0.9; o.x += o.vx; o.y += o.vy;
      ctx.globalAlpha = 0.25 + 0.5 * (0.5 + 0.5 * Math.sin((reduce ? 0 : t * 0.003) + o.p));
      ctx.fillStyle = ink; ctx.beginPath(); ctx.arc(o.x, o.y, o.r, 0, 7); ctx.fill();
    }
    ctx.globalAlpha = 1;
    if (!reduce) requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}

// ------------------------------------------------------------------ palette (Ctrl K)
const ACTIONS = [["Open batch", () => go("batch")], ["Tag", () => go("tag")], ["Write", () => go("write")], ["Library", () => go("library")], ["Settings", () => go("settings")],
  ["Reload from rekordbox", () => api("reload", {}).then(() => refresh())], ["Bucket lanes", () => { S.view = "lanes"; go("batch"); }], ["Only tracks I'm not sure about", () => { S.filter = "review"; go("batch"); }]];
const P = { items: [], sel: 0, timer: 0, tracks: [] };
function openPalette() { $("#palette").hidden = false; const i = $("#paletteInput"); i.value = ""; P.tracks = []; P.sel = 0; paletteRender(); i.focus(); }
function closePalette() { $("#palette").hidden = true; }
function playlistsFlat(nodes, out = []) { for (const n of nodes || []) { if (!n.folder) out.push(n); playlistsFlat(n.children, out); } return out; }
function paletteRender() {
  const q = $("#paletteInput").value.trim().toLowerCase();
  const acts = ACTIONS.filter(([l]) => !q || l.toLowerCase().includes(q)).slice(0, q ? 4 : 6).map(([label, fn]) => ({ kind: "action", label, fn }));
  const pls = q ? playlistsFlat(S.status?.library?.playlists).filter((p) => p.name.toLowerCase().includes(q)).slice(0, 6).map((p) => ({ kind: "playlist", label: p.name, sub: p.path.split("/").slice(0, -1).join(" › "), n: p.count, path: p.path })) : [];
  P.items = [...P.tracks.map((t) => ({ kind: "track", label: t.title, sub: t.artist, n: `${t.bpm ? t.bpm.toFixed(0) : ""} ${t.tonality || ""}`, key: t.key })), ...pls, ...acts];
  P.sel = Math.min(P.sel, Math.max(0, P.items.length - 1));
  const grp = (k, title) => { const its = P.items.filter((i) => i.kind === k); return its.length ? `<li class="grp">${title}</li>${its.map((i) => { const idx = P.items.indexOf(i); return `<li class="opt" aria-selected="${idx === P.sel}" data-pi="${idx}">${icon(k === "track" ? "track" : k === "playlist" ? "batch" : "bolt")}<span>${esc(i.label)}${i.sub ? `<small>${esc(i.sub)}</small>` : ""}</span><em>${esc(i.n ?? "")}</em></li>`; }).join("")}` : ""; };
  $("#paletteList").innerHTML = grp("track", "Tracks") + grp("playlist", "Playlists") + grp("action", "Go") || '<li class="grp">Nothing</li>';
  $('#paletteList [aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
}
async function paletteChoose(i, add) {
  const it = P.items[i]; if (!it) return;
  closePalette();
  if (it.kind === "action") return it.fn();
  if (it.kind === "playlist") { S.status = await api("batch", { playlists: [it.path], tracks: [] }); S.rows = (await api("tracks")).rows; return go("batch"); }
  if (add) { const b = S.status.batch; S.status = await api("batch", { playlists: b.playlists, tracks: [...b.tracks, it.key] }); S.rows = (await api("tracks")).rows; render(); return toast("Added to the batch"); }
  if (!S.rows.some((r) => r.key === it.key)) { S.status = await api("batch", { playlists: [], tracks: [it.key] }); S.rows = (await api("tracks")).rows; }
  return openTag(it.key);
}
$("#paletteInput").addEventListener("input", () => { P.sel = 0; paletteRender(); clearTimeout(P.timer); const q = $("#paletteInput").value.trim(); P.timer = setTimeout(() => run(async () => { P.tracks = q.length > 1 && S.status?.library ? (await api(`search?q=${encodeURIComponent(q)}`)).results.slice(0, 8) : []; paletteRender(); }), 160); });
$("#paletteInput").addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") { e.preventDefault(); P.sel = Math.min(P.items.length - 1, P.sel + 1); paletteRender(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); P.sel = Math.max(0, P.sel - 1); paletteRender(); }
  else if (e.key === "Enter") { e.preventDefault(); run(() => paletteChoose(P.sel, e.shiftKey)); }
  else if (e.key === "Escape") closePalette();
});
$("#palette").addEventListener("click", (e) => { if (e.target.id === "palette") return closePalette(); const li = e.target.closest("[data-pi]"); if (li) run(() => paletteChoose(+li.dataset.pi, e.shiftKey)); });
$("#cmdTrigger").addEventListener("click", openPalette);
$("#cmdTrigger").dataset.tip = `Find a track, playlist or action  ·  ${MOD} K`;

// ------------------------------------------------------------------ actions
const A = {
  go: (v) => go(v), palette: () => openPalette(),
  fold: (v) => { S.open.has(v) ? S.open.delete(v) : S.open.add(v); render(); },
  pl: async (v) => { const b = S.status.batch, pls = new Set(b.playlists); pls.has(v) ? pls.delete(v) : pls.add(v); S.status = await api("batch", { playlists: [...pls], tracks: b.tracks }); S.rows = (await api("tracks")).rows; render(); },
  droptrack: async (v) => { const b = S.status.batch; S.status = await api("batch", { playlists: b.playlists, tracks: b.tracks.filter((k) => k !== v) }); S.rows = (await api("tracks")).rows; render(); },
  clearbatch: async () => { await api("batch", { playlists: [], tracks: [] }); refresh(); },
  reload: async () => { await api("reload", {}); refresh(); },
  opendb: async () => { await api("open-db", {}); S.open.clear(); refresh(); },
  openpath: async () => { await api("open", { path: $("#libpath").value }); S.open.clear(); refresh(); },
  demo: async () => { await api("demo", { on: !S.status.demo }); refresh(); },
  analyze: async () => { S.job = await api("analyze", {}); S.folds.add("log"); render(); pollJob(); },
  filter: (v) => { S.filter = v; S.limit = 300; render(); },
  bucketfilter: (v) => { S.bucket = S.bucket === v ? "" : v; S.limit = 300; render(); },
  clearregion: () => { S.region = null; render(); }, clearkeys: () => { S.keys.clear(); render(); },
  clearfilters: () => { S.filter = "all"; S.bucket = ""; S.q = ""; S.region = null; S.keys.clear(); render(); },
  view: (v) => { S.view = v; render(); },
  focus: () => { S.focus = !S.focus; render(); },
  more: () => { S.limit += 300; renderRows(); },
  open: (v) => openTag(v),
  step: async (v) => { const rows = visibleRows(), i = rows.findIndex((x) => x.key === S.detail?.row?.key), n = rows[Math.max(0, Math.min(rows.length - 1, i + +v))]; if (n && n.key !== S.detail?.row?.key) { await openTag(n.key); autoPlay(); } },
  play: (v) => play(v),
  energy: async (v) => {
    const row = currentRow(); if (!row) return;
    const lv = levelTags(), cur = levelOf(row), n = +v;
    if (n === cur) return;
    if (!n) return patchRow(await api("tags", { key: row.key, remove: [lv[cur - 1]] }));
    patchRow(await api("tags", { key: row.key, add: [lv[n - 1]] }));
  },
  tag: (v) => toggleTag(currentRow(), v),
  untag: async (v) => patchRow(await api("tags", { key: currentRow().key, remove: [v] })),
  hash: async (v) => { await toggleTag(currentRow(), v); $("#hashInput")?.focus(); },
  resettags: async () => patchRow(await api("tags", { key: currentRow().key, reset: true })),
  bucket: async (v) => { const r = currentRow(); patchRow(await api("buckets/edit", { key: r.key, [r.buckets.includes(v) ? "remove" : "add"]: [v] })); },
  rate: async (v, el) => {
    const f = S.detail.fields.find((x) => x.id === "rating"), cur = +(f?.pending ?? f?.value ?? 0) || 0, n = +v === cur ? 0 : +v;
    S.detail = await api("stage", { key: S.detail.row.key, fields: { rating: String(n) } }); S.status = await api("status"); render();
    $$(`[data-stars] button`).forEach((b, i) => i < n && b.classList.add("pop"));
  },
  alt: async (v, el) => { S.detail = await api("stage", { key: S.detail.row.key, fields: { [el.dataset.f]: v } }); S.status = await api("status"); render(); },
  discard: async () => { await api("unstage", { key: S.detail.row.key }); S.status = await api("status"); openTag(S.detail.row.key); },
  relookup: async () => { await api("lookup", { key: S.detail.row.key, force: true }); openTag(S.detail.row.key, true); },
  bucketadd: () => { S.status.buckets.push({ id: `bucket_${S.status.buckets.length + 1}`, name: "New bucket", rule: "", description: "", count: 0 }); render(); },
  bucketdel: (v) => { S.status.buckets.splice(+v, 1); render(); },
  bucketsave: async () => { const bs = S.status.buckets.map((b) => ({ ...b, id: b.id || b.name.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "") })); await api("buckets", { buckets: bs }); await refresh(); toast("Buckets saved"); },
  wopt: async (v) => { S.wopts[v] = !S.wopts[v]; await loadPlan(); render(); },
  hunk: async (v) => { S.skip.has(v) ? S.skip.delete(v) : S.skip.add(v); await loadPlan(); render(); },
  hunktrack: async (cid) => { const ids = S.plan.hunks.filter((h) => h.id.startsWith(`${cid}:`)).map((h) => h.id), all = ids.every((id) => S.skip.has(id)); ids.forEach((id) => (all ? S.skip.delete(id) : S.skip.add(id))); await loadPlan(); render(); },
  wmore: () => { S.wlimit = (S.wlimit || 80) + 80; render(); },
  write: async () => {
    S.writing = true; renderShell();
    try {
      S.receipt = await api("write", { ...S.wopts, skip: [...S.skip] }, 300000);
      S.savedAt = Date.now(); S.skip.clear();
      if (Object.keys(S.receipt.playlists || {}).length) S.wopts.playlists = false;
    } finally { S.writing = false; await refresh(); await loadPlan(); render(); setTimeout(renderShell, 5200); }
  },
  receiptdone: () => { S.receipt = null; render(); },
  reveal: (v) => api("reveal", { path: v }),
  exportxml: async () => { S.lastExport = await api("export", { what: "both" }); render(); },
  srcopen: (v) => { S.srcOpen = S.srcOpen === v ? null : v; render(); },
  srcon: async (v) => { const s = S.status.sources.find((x) => x.id === v); S.status = await api("sources", { id: v, settings: { enabled: !(s.settings.enabled ?? s.default_on) } }); render(); },
  srcsave: async (v) => { const settings = {}; $$(`[data-src="${v}"]`).forEach((i) => (settings[i.dataset.k] = i.value.trim())); S.status = await api("sources", { id: v, settings }); render(); toast(`${srcName(v)} saved`); },
};

let ptagTimer = 0;
document.addEventListener("click", (e) => {
  const p = e.target.closest("[data-ptag]");
  if (p && !p.closest(".edit-only") && !p.querySelector("input")) {
    clearTimeout(ptagTimer);
    ptagTimer = setTimeout(() => run(() => toggleTag(currentRow(), p.dataset.ptag)), 220);
    return;
  }
  const add = e.target.closest("[data-addaxis]");
  if (add) return pillEditor(add, "", async (name) => {
    if (!name) return;
    const res = await api("vocab", { op: "add", group: add.dataset.addaxis, tag: name });
    await afterVocab(res);
    const row = currentRow();
    if (row && S.page === "tag" && res.tag && !row.tags.includes(res.tag)) await toggleTag(row, res.tag);
  });
}, true);
document.addEventListener("dblclick", (e) => { const p = e.target.closest("[data-ptag]"); if (p) { clearTimeout(ptagTimer); e.preventDefault(); editPill(p); } });
document.addEventListener("contextmenu", (e) => { const p = e.target.closest("[data-ptag]"); if (p) { e.preventDefault(); editPill(p); } });
function editPill(p) {
  const old = p.dataset.ptag;
  pillEditor(p, old.slice(1), async (name) => {
    if (name === old.slice(1)) return;
    if (!name) {
      if (!confirm(`Remove ${old} from the vocabulary? Tracks you tagged keep it until the next write.`)) return;
      return afterVocab(await api("vocab", { op: "remove", tag: old }));
    }
    const res = await api("vocab", { op: "rename", tag: old, new: name });
    await afterVocab(res); toast(`${old} is now ${res.tag}`);
  });
}
function pillEditor(el, value, done) {
  const inp = document.createElement("input");
  inp.className = "pill-input"; inp.value = value; inp.size = Math.max(6, value.length + 1); inp.setAttribute("aria-label", "Tag name");
  const prev = el.innerHTML;
  el.innerHTML = ""; el.appendChild(inp); el.classList.add("editing"); inp.focus(); inp.select();
  let finished = false;
  const finish = (save) => { if (finished) return; finished = true; el.classList.remove("editing"); el.innerHTML = prev; if (save) run(() => done(inp.value.trim().replace(/^#/, ""))); };
  inp.addEventListener("keydown", (e) => { e.stopPropagation(); if (e.key === "Enter") { e.preventDefault(); finish(true); } if (e.key === "Escape") finish(false); });
  inp.addEventListener("input", () => (inp.size = Math.max(6, inp.value.length + 1)));
  inp.addEventListener("click", (e) => e.stopPropagation());
  inp.addEventListener("blur", () => finish(false));
}
async function afterVocab(res) {
  S.status = res.status;
  S.rows = (await api("tracks")).rows;
  if (S.detail?.row) S.detail.row = S.rows.find((r) => r.key === S.detail.row.key) || S.detail.row;
  render();
}

document.addEventListener("click", (e) => run(async () => {
  const el = e.target.closest("[data-a]");
  if (!el || el.disabled || el.closest(".card")) return;
  if (el.closest("summary") && el.tagName === "BUTTON") e.preventDefault();
  const fn = A[el.dataset.a];
  if (fn) { if (el.tagName !== "SUMMARY") e.preventDefault(); await fn(el.dataset.v, el); }
}));
document.addEventListener("pointerdown", (e) => { const t = e.target.closest(".tile"); if (t) { t.classList.add("down"); setTimeout(() => t.classList.remove("down"), 90); } });
document.addEventListener("pointerover", (e) => { const s = e.target.closest("[data-stars]"); if (!s) return; const b = e.target.closest("button"); const n = b ? $$("button", s).indexOf(b) + 1 : 0; s.classList.toggle("preview", n > 0); $$("button", s).forEach((x, i) => x.classList.toggle("hov", i < n)); });
document.addEventListener("pointerout", (e) => { const s = e.target.closest("[data-stars]"); if (s && !s.contains(e.relatedTarget)) { s.classList.remove("preview"); $$("button", s).forEach((x) => x.classList.remove("hov")); } });
document.addEventListener("change", (e) => run(async () => {
  const t = e.target;
  if (t.dataset.fld && S.detail) { S.detail = await api("stage", { key: S.detail.row.key, fields: { [t.dataset.fld]: t.value } }); S.status = await api("status"); render(); }
}));
let ruleTimer;
document.addEventListener("input", (e) => {
  const t = e.target;
  if (t.id === "search") { S.q = t.value; S.limit = 300; clearTimeout(renderRows._t); renderRows._t = setTimeout(renderRows, 120); }
  if (t.id === "hashInput") { S.hsel = 0; renderHash(); }
  if (t.dataset.b !== undefined) {
    const b = S.status.buckets[+t.dataset.b]; b[t.dataset.f] = t.value;
    if (t.dataset.f === "rule") { clearTimeout(ruleTimer); ruleTimer = setTimeout(() => run(async () => { const { error } = await api("check-rule", { rule: t.value }); t.classList.toggle("bad", !!error); const h = $(`#err${t.dataset.b}`); h.textContent = error || ""; h.style.color = error ? "var(--bad)" : ""; }), 250); }
  }
});

document.addEventListener("keydown", (e) => run(async () => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); return $("#palette").hidden ? openPalette() : closePalette(); }
  if (!$("#palette").hidden) return;
  const t = e.target;
  if (t.id === "hashInput") {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); S.hsel = Math.max(0, Math.min((S.hopts || []).length - 1, (S.hsel || 0) + (e.key === "ArrowDown" ? 1 : -1))); return renderHash(); }
    if (e.key === "Enter" && S.hopts?.length) { e.preventDefault(); const tag = S.hopts[S.hsel || 0].tag; t.value = ""; await toggleTag(currentRow(), tag); return $("#hashInput")?.focus(); }
    if (e.key === "Escape") { t.value = ""; renderHash(); return t.blur(); }
    return;
  }
  if (t.matches("input, select, textarea")) { if (e.key === "Escape") t.blur(); return; }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const audio = $("#audio"), row = currentRow();
  if (e.key === " " && row && !t.closest("button[data-hold]")) { e.preventDefault(); return audio.dataset.key === row.key && !audio.paused ? audio.pause() : play(row.key); }
  if (e.key === "ArrowRight" && audio.src && !t.closest("canvas")) { audio.currentTime += 10; return; }
  if (e.key === "ArrowLeft" && audio.src && !t.closest("canvas")) { audio.currentTime -= 10; return; }
  if (S.page === "batch") {
    if (e.key === "l") return A.view(S.view === "list" ? "lanes" : "list");
    if (S.view !== "list") return;
    const rows = visibleRows();
    if (["ArrowDown", "ArrowUp", "j", "k"].includes(e.key)) { e.preventDefault(); const i = rows.findIndex((r) => r.key === S.sel), n = rows[Math.max(0, Math.min(rows.length - 1, i + (e.key === "ArrowDown" || e.key === "j" ? 1 : -1)))]; if (n) { S.sel = n.key; renderRows(); $(".rows li.on")?.scrollIntoView({ block: "nearest" }); } return; }
    if (e.key === "Enter" && S.sel) return openTag(S.sel);
    if (e.key === "f") return A.focus();
    return;
  }
  if (S.page === "tag" && row) {
    if (["ArrowDown", "ArrowUp", "j", "k"].includes(e.key)) { e.preventDefault(); return A.step(e.key === "ArrowDown" || e.key === "j" ? 1 : -1); }
    if (e.key === "Escape") return go("batch");
    if (/^[0-5]$/.test(e.key)) { const k = $("canvas[data-knob]"); if (k) { k.dataset.v = e.key; } return A.energy(e.key); }
    if (e.key === "Backspace") { e.preventDefault(); return patchRow(await api("tags", { key: row.key, reset: true })); }
    const tag = quickKeys()[e.key.toLowerCase()];
    if (tag) { e.preventDefault(); return toggleTag(row, tag); }
  }
}));
document.addEventListener("keydown", (e) => { if (e.key === "Alt") document.body.classList.add("keys-on"); });
document.addEventListener("keyup", (e) => { if (e.key === "Alt") document.body.classList.remove("keys-on"); });

// ------------------------------------------------------------------ jobs & player
async function pollJob() {
  while (S.job?.state === "running") { await new Promise((r) => setTimeout(r, 1000)); S.job = await api(`job?id=${S.job.id}`); if (S.page === "library") render(); }
  await refresh();
  if (S.job) toast(S.job.state === "done" ? "Analysis finished" : `Analysis ${S.job.state}`, S.job.state !== "done");
}
function play(key) {
  const r = S.rows.find((x) => x.key === key) || S.detail?.row;
  if (!r) return;
  const audio = $("#audio");
  ensureAudioGraph();
  $("#player").hidden = false;
  $("#who").innerHTML = `<b>${esc(r.title)}</b><span>${esc(r.artist)}</span>`;
  if (audio.dataset.key !== key) {
    audio.src = `/api/audio?t=${encodeURIComponent(TOKEN)}&key=${encodeURIComponent(key)}`; audio.dataset.key = key; audio._seeked = false;
    audio.addEventListener("loadedmetadata", () => { if (audio.duration && !audio._seeked) { audio.currentTime = audio.duration * 0.4; audio._seeked = true; } }, { once: true });
  }
  audio.play().catch(() => {});
}
function autoPlay() { const a = $("#audio"), r = currentRow(); if (a.src && !a.paused && r) play(r.key); }
(() => {
  const audio = $("#audio"), seek = $("#seek");
  $("#play").onclick = () => { ensureAudioGraph(); audio.paused ? audio.play() : audio.pause(); };
  audio.onplay = () => { $("#playIcon").setAttribute("d", "M3 1.5h3v11H3zM8 1.5h3v11H8z"); if (!AUD.raf && AUD.an) AUD.raf = requestAnimationFrame(metersLoop); };
  audio.onpause = () => $("#playIcon").setAttribute("d", "M3 1.5v11l9-5.5z");
  audio.ontimeupdate = () => { $("#tcur").textContent = fmtTime(audio.currentTime); $("#tdur").textContent = fmtTime(audio.duration); if (!seek._drag && audio.duration) seek.value = (audio.currentTime / audio.duration) * 1000; };
  audio.onerror = () => toast("Can't play this file: missing, or a format WebView2 can't decode.", true);
  seek.oninput = () => { seek._drag = true; };
  seek.onchange = () => { seek._drag = false; if (audio.duration) audio.currentTime = (seek.value / 1000) * audio.duration; };
})();
addEventListener("resize", () => { clearTimeout(mount._t); mount._t = setTimeout(mount, 150); });

// Dev mode (scripts/dev.py): reload when a web file changes, and come back to the same place
// after a reload or an app restart.
const DEV_KEY = "subjetunes-dev-place";
function devSave() {
  try { localStorage.setItem(DEV_KEY, JSON.stringify({ t: Date.now(), page: S.page, tagKey: S.tagKey, view: S.view, filter: S.filter, folds: [...S.folds], scroll: $("#main").scrollTop })); } catch {}
}
async function devRestore() {
  let p = null;
  try { p = JSON.parse(localStorage.getItem(DEV_KEY) || "null"); localStorage.removeItem(DEV_KEY); } catch {}
  if (!p || Date.now() - p.t > 120000) return;
  for (let i = 0; i < 150 && (!S.status || S.status.loading); i++) await new Promise((r) => setTimeout(r, 400));   // after a restart the library loads first
  Object.assign(S, { view: p.view || S.view, filter: p.filter || S.filter, folds: new Set(p.folds || []) });
  if (p.page === "tag" && p.tagKey && S.rows.some((r) => r.key === p.tagKey)) await openTag(p.tagKey);
  else if (p.page) await go(p.page);
  $("#main").scrollTo(0, p.scroll || 0);
}
async function devWatch() {
  const first = await api("dev").catch(() => null);
  if (!first?.dev) return;
  addEventListener("beforeunload", devSave);
  setInterval(() => { if (S.page) devSave(); }, 2000);        // so a restart (.py change) also comes back here
  let stamp = first.stamp;
  setInterval(async () => {
    const d = await api("dev").catch(() => null);
    if (d?.dev && d.stamp !== stamp) { devSave(); location.reload(); }
  }, 800);
  await devRestore();
}

refresh().then(devWatch).catch((e) => { $("#main").innerHTML = `<div class="page"><div class="empty"><h2>Can't reach the Subjetunes engine</h2><p>${esc(e.message)}</p></div></div>`; });
