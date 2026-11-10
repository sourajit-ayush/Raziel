// Raziel Hub - the page. Talks only to hub_server.py on this computer, with the token from the link's #t=...
// Everything is built with DOM calls and textContent: nothing from a note, an answer or a fact is ever HTML.
"use strict";

const TOKEN_KEY = "raziel-hub-token";
const state = { focus: "nexus", tab: "all", overview: null, items: [], metrics: null, chat: [], asking: false };
const KINDS = { all: null, reports: "report,answer,memory", metrics: "metric,event" };

function token() {
  const m = location.hash.match(/[#&]t=([A-Za-z0-9_-]{32,})/);
  if (m) {
    try { sessionStorage.setItem(TOKEN_KEY, m[1]); } catch (e) { /* private window: keep it in memory */ }
    history.replaceState(null, "", location.pathname);       // the token doesn't stay in the address bar
    state.token = m[1];
  }
  if (!state.token) {
    try { state.token = sessionStorage.getItem(TOKEN_KEY); } catch (e) { /* none */ }
  }
  return state.token;
}

async function api(path, body) {
  const opts = { headers: { "X-Raziel-Token": token() || "" }, cache: "no-store" };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  if (r.status === 403) throw new Error("token");
  return r.json();
}

// ------------------------------------------------------------------ tiny DOM helper

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.setAttribute("class", v);
    else if (k === "text") el.textContent = v;
    else if (k === "style") Object.assign(el.style, v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) el.append(kid);
  return el;
}
const $ = (id) => document.getElementById(id);
const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };

function when(iso) {
  if (!iso) return "";
  const d = new Date(iso), now = new Date();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return "Today " + time;
  const y = new Date(now); y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return "Yesterday " + time;
  return d.toLocaleDateString([], { day: "numeric", month: "short" }) + " " + time;
}
const secs = (v) => (v === null || v === undefined) ? "—" : v.toFixed(2) + " s";

// ------------------------------------------------------------------ the universe

const BLURB = {
  nexus: "Everything runs through her: conversations, memory, the briefing, and how fast she answers.",
  music: "Spotify, YouTube, playlists and the queue. She turns the music down while you talk.",
  messages: "WhatsApp, Gmail, Calendar and your phone. Nothing is sent until you say yes.",
  knowledge: "Your files, your notes, the web, and ChatGPT or Gemini answers read aloud.",
  forge: "Apps, folders, typing, screenshots and PC controls. Risky ones always ask first.",
  time: "Reminders, timers, to-do lists, the weather and the news.",
  shield: "Voice ID, game and call mode, locking, and the phone link."
};
const STATE_TEXT = { idle: "Listening for her name", listening: "Listening to you", thinking: "Thinking",
                     speaking: "Speaking", sleeping: "Asleep", unknown: "Starting" };

function beings() { return (state.overview && state.overview.beings) || []; }
function being(key) { return beings().find((b) => b.key === key) || { key, name: key, title: "", color: "#FFB84D", helpers: [] }; }

// The map itself is universe.js (the galaxies on the canvas). Here: what it is told.
function renderMap() {
  Universe.setBeings(beings());
  Universe.setFocus(state.focus);
  renderPulse();
}

// Her state tints the sky and sets how fast the galaxies turn; the being that is working flares.
function renderPulse() {
  const p = state.pulse || state.overview || {};
  const st = state.asking && (!p.state || p.state === "idle" || p.state === "sleeping") ? "thinking" : (p.state || "unknown");
  document.body.dataset.state = st === "idle" ? "listening" : st;
  Universe.setState(st, STATE_TEXT[st] || st);
  Universe.setActive(st === "thinking" || st === "speaking" ? (p.active || null) : null);
}

async function refreshPulse() {
  if (document.hidden) return;
  try { state.pulse = await api("/api/pulse"); } catch (e) { return; }
  renderPulse();
}

function renderFocus() {
  const b = being(state.focus);
  const kicker = $("kicker");
  kicker.textContent = b.title + (state.focus === "nexus" ? " · all beings" : "");
  kicker.style.color = b.color;
  $("heading").textContent = state.focus === "nexus" ? "Everything she made" : `${b.name}, ${(b.title || "").toLowerCase()}`;
  $("blurb").textContent = BLURB[state.focus] || "";
  clear($("helpers")).append(...(b.helpers || []).map((x) =>
    h("span", { class: "pill" }, h("span", { class: "dot", style: { background: b.color } }), x)));
  const stats = (state.overview && state.overview.stats && state.overview.stats[state.focus]) || [];
  clear($("stats")).append(...stats.map(([v, label]) =>
    h("div", { class: "card stat" }, h("div", { class: "v", text: v }), h("div", { class: "muted small", text: label }))));
}

// ------------------------------------------------------------------ deliverables / metrics

const ACTIONS = { report: "Read", answer: "Read", memory: "Open note", event: null, metric: null };

function renderItems() {
  const b = being(state.focus);
  const tabName = { all: "Deliverables", reports: "Reports", metrics: "Metrics" }[state.tab];
  $("listtitle").textContent = (state.focus === "nexus" ? "" : b.name + " · ") + tabName;
  const list = state.items;
  $("listnote").textContent = list.length + (list.length === 1 ? " item" : " items") + " · newest first";
  const grid = clear($("items"));
  for (const it of list) {
    const ib = being(it.being);
    const act = it.has_body && ACTIONS[it.kind] !== null ? (ACTIONS[it.kind] || "Read") : null;
    grid.append(h("article", { class: "deliv" },
      h("div", { class: "spread" },
        h("span", { class: "pill" }, h("span", { class: "dot", style: { background: ib.color } }), `${ib.name} · ${it.kind}`),
        h("span", { class: "muted small", text: when(it.created) })),
      h("h3", { text: it.title }),
      h("p", { text: it.summary }),
      act ? h("div", { class: "acts" }, h("button", { class: "ghost", text: act, onclick: () => openItem(it) })) : null));
  }
  const empty = $("empty");
  empty.hidden = list.length > 0;
  empty.textContent = `Nothing from ${b.name} yet. When one of its skills finishes something, it lands here.`;
  renderMetrics();
}

function renderMetrics() {
  const box = $("metrics");
  const show = state.tab === "metrics" && state.focus === "nexus" && state.metrics;
  box.hidden = !show;
  if (!show) return;
  clear(box);
  const days = state.metrics.days || [];
  const max = Math.max(10, ...days.map((d) => d.median));
  const desc = days.map((d) => `${d.day}: ${d.median} s`).join(", ");
  box.append(h("section", { class: "card pad" },
    h("h2", { class: "h3", text: "Time to transcribe a turn (median per day)" }),
    h("p", { class: "muted small", style: { margin: 0 }, text: "Dark orange: slower than 10 s." }),
    h("div", { class: "chart", role: "img", "aria-label": "Median transcription seconds per day. " + desc },
      days.map((d) => h("div", { class: "col" }, h("span", { text: d.median.toFixed(1) + " s" }),
        h("div", { class: "bar" + (d.median > 10 ? " slow" : ""), style: { height: Math.max(4, Math.round(d.median / max * 150)) + "px" } })))),
    h("div", { class: "chart-days" }, days.map((d) => h("span", { text: d.day.slice(5) })))));
  const turns = state.metrics.turns || [];
  box.append(h("section", { class: "card pad" },
    h("h2", { class: "h3", text: "Recent turns" }),
    turns.length ? h("div", { class: "tablebox" }, h("table", {},
      h("thead", {}, h("tr", {}, ["When", "You said", "Transcribe", "Route", "First sound", "Total"].map((t) => h("th", { text: t })))),
      h("tbody", {}, turns.map((t) => h("tr", {},
        h("td", { text: when(t.at) }), h("td", { text: t.said || "—" }), h("td", { text: secs(t.transcribe) }),
        h("td", { text: secs(t.route) }), h("td", { text: secs(t.first_sound) }),
        h("td", { class: t.total === null ? "" : (t.total > 10 ? "slowc" : "good"), text: t.total === null ? "no reply" : secs(t.total) }))))))
      : h("p", { class: "muted", style: { margin: 0 }, text: "Turns appear here from the next time you talk to her." })));
}

async function openItem(it) {
  const dlg = $("reader");
  $("readertitle").textContent = it.title;
  $("readerbody").textContent = "Loading…";
  if (!dlg.open) dlg.showModal();
  try {
    const r = await api("/api/item?id=" + encodeURIComponent(it.id));
    $("readerbody").textContent = r.body || "This one has no text.";
  } catch (e) {
    $("readerbody").textContent = "Couldn't load it.";
  }
}

// ------------------------------------------------------------------ side column

function bar(pct, color) {
  return h("div", { class: "track" }, h("div", { class: "fill", style: { width: Math.max(0, Math.min(100, pct)) + "%", background: color } }));
}

function renderSide() {
  const o = state.overview;
  if (!o) return;
  const v = o.vitals || {};
  const box = clear($("vitals"));
  const t = v.last_turn;
  box.append(h("div", { class: "vital" },
    h("div", { class: "row" }, h("span", { text: "Last turn" }),
      h("strong", { class: t ? (t.total > 10 ? "slowc" : "good") : "", text: t ? t.total.toFixed(1) + " s" : "—" })),
    bar(t ? t.total / 40 * 100 : 0, t && t.total > 10 ? "var(--slow)" : "var(--good)"),
    h("div", { class: "note", text: t ? `${t.said || "a turn"} · ${when(t.at)}` : "No timed turn yet" })));
  if (v.ram) {
    const used = (v.ram.total_gb - v.ram.free_gb) / v.ram.total_gb * 100;
    box.append(h("div", { class: "vital" },
      h("div", { class: "row" }, h("span", { text: "Free RAM" }), h("strong", { class: v.ram.free_gb < 1.5 ? "slowc" : "", text: `${v.ram.free_gb} / ${v.ram.total_gb} GB` })),
      bar(used, used > 90 ? "var(--slow)" : "var(--gold)"),
      h("div", { class: "note", text: v.ram.free_gb < 1.5 ? "Low: replies slow down when memory runs out" : "In use: " + Math.round(used) + "%" })));
  }
  box.append(h("div", { class: "vital" },
    h("div", { class: "row" }, h("span", { text: "LLM on the GPU" }), h("strong", { text: v.llm ? Math.round(v.llm.gpu * 100) + "%" : "not loaded" })),
    bar(v.llm ? v.llm.gpu * 100 : 0, "var(--gold)"),
    h("div", { class: "note", text: v.llm ? v.llm.model + ", the rest on the CPU" : (o.mode === "game" ? "Unloaded while a game is in front" : "Loads on the next question") })));
  if (v.echo) {
    const removed = v.echo.raw ? Math.round((1 - v.echo.clean / v.echo.raw) * 100) : 0;
    box.append(h("div", { class: "vital" },
      h("div", { class: "row" }, h("span", { text: "Echo removed" }), h("strong", { text: removed + "%" })),
      bar(removed, "var(--slow)"),
      h("div", { class: "note", text: `${v.echo.raw.toLocaleString()} → ${v.echo.clean.toLocaleString()} in the mic · barge-in at ${v.echo.threshold.toLocaleString()}` })));
  }

  const facts = clear($("facts"));
  $("factsrc").textContent = "Facts.md";
  if (!o.facts.length) facts.append(h("p", { class: "muted small", style: { margin: 0 }, text: "She knows nothing about you right now." }));
  for (const f of o.facts) {
    facts.append(h("div", { class: "fact" }, h("span", { text: f }),
      h("button", { class: "ghost", "aria-label": "Forget: " + f, text: "Forget", onclick: () => forget(f) })));
  }

  const chat = clear($("chat"));
  if (!state.chat.length) chat.append(h("p", { class: "muted small", style: { margin: 0 }, text: "Type above to ask her something. She answers here (silently)." }));
  for (const m of state.chat) chat.append(h("div", { class: "msg " + (m.you ? "you" : "her"), text: m.text }));
}

async function forget(fact) {
  if (!confirm(`Make Raziel forget this?\n\n${fact}`)) return;
  try {
    await api("/api/forget", { fact });
  } catch (e) { notice("Couldn't reach Raziel."); }
  await refreshOverview();
}

// ------------------------------------------------------------------ the rest

function notice(text, warn) {
  const n = $("notice");
  n.hidden = !text;
  n.textContent = text || "";
  n.className = "card notice" + (warn ? " warn" : "");
}

function renderTabs() {
  const tabs = clear($("tabs"));
  for (const [id, label] of [["all", "Everything"], ["reports", "Reports"], ["metrics", "Metrics"]]) {
    tabs.append(h("button", { class: "tab", "aria-pressed": String(state.tab === id), text: label,
      onclick: () => { state.tab = id; renderTabs(); refreshItems(); } }));
  }
}

function setFocus(key) {
  state.focus = key;
  renderMap(); renderFocus(); refreshItems();
}

async function refreshOverview() {
  try {
    state.overview = await api("/api/overview");
    notice("");
  } catch (e) {
    notice(e.message === "token"
      ? "This page needs Raziel's key. Open it with the Raziel Hub shortcut on your Desktop."
      : "Can't reach Raziel. Is she running?", true);
    return;
  }
  renderMap(); renderFocus(); renderSide();
}

async function refreshItems() {
  try {
    const q = new URLSearchParams();
    if (state.focus !== "nexus") q.set("being", state.focus);
    if (KINDS[state.tab]) q.set("kind", KINDS[state.tab]);
    state.items = (await api("/api/items?" + q.toString())).items || [];
    if (state.tab === "metrics" && state.focus === "nexus") state.metrics = await api("/api/metrics");
  } catch (e) { return; }
  renderItems();
}

async function onAsk(ev) {
  ev.preventDefault();
  const input = $("ask");
  const text = input.value.trim();
  if (!text || state.asking) return;
  state.asking = true;
  $("askbtn").disabled = true;
  input.value = "";
  state.chat.push({ you: true, text }, { you: false, text: "…" });
  renderSide(); renderPulse();
  let reply = "Couldn't reach Raziel.";
  try { reply = (await api("/api/ask", { text })).reply || reply; } catch (e) { /* keep the message */ }
  state.chat[state.chat.length - 1] = { you: false, text: reply };
  state.asking = false;
  $("askbtn").disabled = false;
  renderSide(); renderPulse();
  refreshItems();
}

function boot() {
  token();
  Universe.init({ onSelect: setFocus });
  renderTabs();
  $("askform").addEventListener("submit", onAsk);
  $("readerclose").addEventListener("click", () => $("reader").close());
  refreshOverview().then(refreshItems);
  setInterval(refreshOverview, 3000);
  setInterval(refreshPulse, 700);
  setInterval(refreshItems, 15000);
}
document.addEventListener("DOMContentLoaded", boot);
