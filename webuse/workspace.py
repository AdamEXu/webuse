"""Multi-app workspace: one page hosting a persistent sidebar of running apps and
1..N live app panes side by side, with no page navigation. Every per-pane part
(stream <img>, ghost overlay, SSE subscription, action posting) reuses the
EXISTING per-pid endpoints in server.py unchanged. The only new server pieces are
a sidebar data feed, window focus, and window arrange (tile).

The integrator wires this in via `from . import workspace` and
`app.include_router(workspace.router)`; this file never edits server.py or ax.py."""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import apps, winctl

router = APIRouter()


class FocusBody(BaseModel):
    pid: int


class ArrangeBody(BaseModel):
    pids: list[int]
    cols: int | None = None


@router.get("/workspace", response_class=HTMLResponse)
def workspace_page():
    return WORKSPACE_PAGE


@router.get("/workspace/apps")
def workspace_apps():
    return {"apps": apps._running_apps()}


@router.post("/api/workspace/focus")
def workspace_focus(body: FocusBody):
    return winctl.focus_app(body.pid)


@router.post("/api/workspace/arrange")
def workspace_arrange(body: ArrangeBody):
    return {"frames": winctl.tile(body.pids, body.cols)}


# WORKSPACE_PAGE duplicates-and-adapts APP_PAGE's per-pane JS (build/sync/render/
# act/refresh/announce + virtual cursor + stream-restart + EventSource) into a
# per-pane Pane(pid, name, host) factory so nothing is extracted yet — the
# integrator can unify APP_PAGE and Pane later. All pane state is closed over per
# instance, so multiple panes never collide.
WORKSPACE_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>webuse — workspace</title>
<style>
* { box-sizing: border-box; }
body { margin: 0; font: 13px -apple-system, sans-serif; display: flex; height: 100vh; }
:root { --accent: #4c8bf5; }

nav { flex: 0 0 220px; overflow-y: auto; border-right: 1px solid #ddd;
  padding: 10px; background: #fafafa; }
nav h2 { font-size: 12px; text-transform: uppercase; color: #888; margin: 4px 6px 10px; }
nav button { display: flex; flex-direction: column; align-items: flex-start; gap: 2px;
  width: 100%; text-align: left; padding: 8px 10px; margin-bottom: 6px; border: 1px solid #ddd;
  border-radius: 8px; background: #fff; cursor: pointer; font: inherit; }
nav button:hover { border-color: var(--accent); background: #f5f8ff; }
nav button[aria-pressed="true"] { border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 12%, #fff); }
nav button small { color: #888; }

main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
#toolbar { flex: 0 0 auto; padding: 8px 12px; border-bottom: 1px solid #eee;
  display: flex; align-items: center; gap: 10px; }
#toolbar button { padding: 6px 12px; border: 1px solid var(--accent); border-radius: 7px;
  background: #fff; color: var(--accent); cursor: pointer; font: inherit; }
#toolbar button:hover { background: #f5f8ff; }
#panes { flex: 1; overflow: auto; display: grid; gap: 12px; padding: 12px;
  align-content: start; }
#empty { color: #888; padding: 24px; }

section.pane { border: 1px solid #ddd; border-radius: 10px; overflow: hidden;
  display: flex; flex-direction: column; background: #fff; }
section.pane > header { display: flex; align-items: center; gap: 8px; padding: 6px 10px;
  border-bottom: 1px solid #eee; background: #fafafa; }
section.pane > header .name { font-weight: 600; flex: 1; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
section.pane > header button { padding: 3px 9px; border: 1px solid #ccc; border-radius: 6px;
  background: #fff; cursor: pointer; font: inherit; font-size: 12px; }
section.pane > header button:hover { border-color: var(--accent); color: var(--accent); }

.stage { position: relative; overflow: hidden; }
.screen { display: block; position: absolute; left: 0; top: 0; }
.overlay { position: absolute; left: 0; top: 0; }
.ghost { position: absolute; margin: 0; padding: 0; border: 0; border-radius: 0;
  background: transparent; color: transparent; caret-color: #111;
  font: 13px -apple-system, sans-serif; text-align: left; cursor: default;
  -webkit-appearance: none; appearance: none; }
.ghost:focus { outline: none; background: rgba(255,255,255,.01); }
a.ghost { text-decoration: none; }
span.ghost { pointer-events: none; overflow: hidden; }
input.ghost[type=checkbox], input.ghost[type=radio] { opacity: 0.01; }
.offscreen { position: absolute; left: -10000px; top: 0; width: 600px; overflow: hidden; }
.offscreen .ghost { position: static; display: block; color: #000; }
.banner { display: none; padding: 6px 10px; background: #c0392b; color: #fff; font-size: 12px; }
section.pane.gone .banner { display: block; }
.status { display: none; position: absolute; right: 8px; top: 8px; z-index: 10;
  padding: 5px 9px; background: rgba(0,0,0,.75); color: #fff; border-radius: 6px; }

.cursor { position: absolute; width: 18px; height: 18px; color: var(--accent);
  pointer-events: none; z-index: 10; opacity: 0;
  transition: left .28s cubic-bezier(.3,.7,.3,1), top .28s cubic-bezier(.3,.7,.3,1), opacity .2s; }
.cursor.thinking { animation: wiggle 1.6s ease-in-out infinite; }
@keyframes wiggle {
  0%, 100% { transform: translate(0, 0) rotate(0deg); }
  25% { transform: translate(2px, -1px) rotate(2deg); }
  50% { transform: translate(-1px, 2px) rotate(-2deg); }
  75% { transform: translate(1px, 1px) rotate(1deg); }
}
.ripple { position: absolute; width: 26px; height: 26px; border-radius: 50%;
  border: 2px solid var(--accent); background: color-mix(in srgb, var(--accent) 25%, transparent);
  pointer-events: none; z-index: 9; transform: translate(-50%, -50%) scale(0);
  animation: pulse .45s ease-out forwards; }
@keyframes pulse {
  from { transform: translate(-50%, -50%) scale(0); opacity: 1; }
  to { transform: translate(-50%, -50%) scale(2.2); opacity: 0; }
}
.chip { position: absolute; padding: 3px 8px; border-radius: 5px;
  background: var(--accent); color: #fff; font-size: 12px; white-space: nowrap;
  pointer-events: none; z-index: 10; opacity: 1; transition: opacity .4s; }
.chip.fade { opacity: 0; }

footer { flex: 0 0 auto; padding: 8px 12px; border-top: 1px solid #eee;
  color: #666; font-size: 12px; }
#agent-note { position: absolute; left: -10000px; top: 0; }
</style></head>
<body>
<nav aria-label="Running apps"><h2>Running apps</h2><div id="applist"></div></nav>
<main>
  <div id="toolbar">
    <button id="arrange">Arrange windows to layout</button>
    <span id="hint" style="color:#888">Click an app to open a pane. Open several to work side by side.</span>
  </div>
  <div id="panes"><div id="empty">No panes open — pick an app from the sidebar.</div></div>
  <footer>
    <div id="agent-note">This page is a multi-app workspace: each pane is a live macOS
    application rendered as a web page. Every control is a real HTML element positioned over
    a per-pane screen stream: click buttons and links normally, type into inputs (values
    commit on change/Enter). Clicking empty space forwards a native click at those
    coordinates to that pane's app. Coordinate clicks are delivered in the background — the
    user's real cursor and focus never move. Batch several steps into ONE request: POST
    /api/action with {"pid":PID,"actions":[{"action":"set_value","id":"e12","value":"hello"},
    {"action":"press","id":"e7"}]}. Each pane owns its own EventSource(/app/PID/events),
    /app/PID/dom fallback poll, and /app/PID/stream. Use the sidebar to open/close apps and
    the Focus button to raise a window.</div>
    <strong>If clicks don't go through for some actions, it might be a focus issue. Try focusing the window then performing the action.</strong>
  </footer>
</main>

<template id="pane-tpl">
  <section class="pane">
    <header>
      <span class="name"></span>
      <button class="focus" type="button">Focus</button>
      <button class="close" type="button">Close</button>
    </header>
    <div class="banner"></div>
    <div class="stage">
      <img class="screen" alt="live app screen">
      <div class="overlay"></div>
      <div class="cursor"><svg width="18" height="18" viewBox="0 0 18 18"><path
        d="M1 1 L1 14.5 L4.7 11.3 L7 16.5 L9.4 15.4 L7.1 10.3 L12 10.3 Z"
        fill="currentColor" stroke="#fff" stroke-width="1"/></svg></div>
      <div class="status"></div>
    </div>
    <div class="offscreen"></div>
  </section>
</template>

<script>
// ---- Per-pane instance: duplicates APP_PAGE's JS, scoped to one <section>. ----
function Pane(pid, name, host) {
  const screenEl = host.querySelector('.screen');
  const overlay = host.querySelector('.overlay');
  const off = host.querySelector('.offscreen');
  const stage = host.querySelector('.stage');
  const cursor = host.querySelector('.cursor');
  const statusEl = host.querySelector('.status');
  const banner = host.querySelector('.banner');
  host.querySelector('.name').textContent = name;
  screenEl.src = `/app/${pid}/stream`;

  const lastNode = new Map(); // id -> JSON of node last applied
  let lastSize = null, statusTimer = null, idleTimer = null;
  let es = null, pollTimer = null, alive = true;

  function typing() {
    const a = document.activeElement;
    return a && host.contains(a) && a.classList.contains('ghost') &&
      (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA');
  }

  function agentCursor(x, y, kind, detail) {
    cursor.classList.remove('thinking');
    cursor.style.opacity = '1';
    cursor.style.left = x + 'px';
    cursor.style.top = y + 'px';
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => cursor.classList.add('thinking'), 2000);
    if (kind === 'set_value' && detail)
      setTimeout(() => chip(x, y, detail), 300);
  }
  function ripple(x, y) {
    const r = document.createElement('div');
    r.className = 'ripple';
    r.style.left = x + 'px'; r.style.top = y + 'px';
    stage.appendChild(r);
    r.addEventListener('animationend', () => r.remove());
  }
  function chip(x, y, text) {
    const c = document.createElement('div');
    c.className = 'chip';
    c.textContent = text.length > 24 ? text.slice(0, 24) + '…' : text;
    c.style.left = (x + 20) + 'px'; c.style.top = (y + 14) + 'px';
    stage.appendChild(c);
    setTimeout(() => c.classList.add('fade'), 1200);
    c.addEventListener('transitionend', () => c.remove());
  }
  function announce(p) {
    let x, y;
    if (p.id != null) {
      const el = overlay.querySelector('[data-ax-id="' + CSS.escape(p.id) + '"]');
      if (el && el.parentElement === overlay) {
        x = el.offsetLeft + el.offsetWidth / 2;
        y = el.offsetTop + el.offsetHeight / 2;
      }
    } else if (p.x != null && p.y != null) { x = p.x; y = p.y; }
    if (x != null) agentCursor(x, y, p.action, p.value);
    return x != null ? [x, y] : null;
  }

  async function act(payload) {
    payload.pid = pid;
    const estimate = announce(payload);
    let point = null;
    try {
      const r = await fetch('/api/action', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      if (r.ok) {
        const j = await r.json();
        point = (j.results && j.results[0] && j.results[0].point) || null;
      }
    } catch (e) {}
    if (payload.action !== 'set_value') {
      const p = point || estimate;
      if (p) {
        cursor.style.left = p[0] + 'px'; cursor.style.top = p[1] + 'px';
        ripple(p[0], p[1]);
      }
    }
    setTimeout(refresh, 400);
  }

  function setAttr(e, k, v) {
    if (v == null || v === '') { if (e.hasAttribute(k)) e.removeAttribute(k); }
    else if (e.getAttribute(k) !== String(v)) e.setAttribute(k, v);
  }
  function setStyle(e, k, v) { if (e.style[k] !== v) e.style[k] = v; }

  function build(n, e) {
    if (!e) {
      e = document.createElement(n.tag);
      e.className = 'ghost';
      e.dataset.axId = n.id;
      e.dataset.kind = n.kind;
      if (n.tag === 'input') e.type = n.type || 'text';
      if (n.tag === 'a') e.href = '#';
    }
    if (n.tag === 'input' || n.tag === 'textarea') {
      if (document.activeElement !== e) {
        if (e.type === 'checkbox' || e.type === 'radio') {
          if (e.checked !== !!n.checked) e.checked = !!n.checked;
        } else {
          const v = n.value ?? '';
          if (e.value !== v) e.value = v;
        }
      }
    } else {
      const t = n.text || '';
      if (e.textContent !== t) e.textContent = t;
    }
    setAttr(e, 'aria-label', n.label);
    setAttr(e, 'title', n.label);
    setAttr(e, 'placeholder', n.placeholder);
    if (n.tag === 'input' || n.tag === 'button' || n.tag === 'textarea') {
      if (e.disabled !== !!n.disabled) e.disabled = !!n.disabled;
    }
    setStyle(e, 'left', n.x + 'px'); setStyle(e, 'top', n.y + 'px');
    setStyle(e, 'width', n.w + 'px'); setStyle(e, 'height', n.h + 'px');
    return e;
  }

  function sync(container, nodes) {
    const byId = new Map();
    for (const el of Array.from(container.children)) byId.set(el.dataset.axId, el);
    const seen = new Set();
    for (const n of nodes) {
      seen.add(n.id);
      const existing = byId.get(n.id);
      const key = JSON.stringify(n);
      if (existing && existing.tagName.toLowerCase() === n.tag) {
        if (lastNode.get(n.id) === key) continue;
        build(n, existing);
      } else {
        if (existing) existing.remove();
        container.appendChild(build(n));
      }
      lastNode.set(n.id, key);
    }
    for (const [id, el] of byId) if (!seen.has(id)) { el.remove(); lastNode.delete(id); }
  }

  function render(d) {
    const w = d.window;
    const size = w.w + 'x' + w.h;
    if (lastSize && size !== lastSize) restartStream();
    lastSize = size;
    for (const el of [stage, screenEl]) {
      setStyle(el, 'width', w.w + 'px'); setStyle(el, 'height', w.h + 'px');
    }
    sync(overlay, d.nodes.filter(n => !n.offscreen));
    sync(off, d.nodes.filter(n => n.offscreen));
  }

  async function refresh() {
    if (!alive || typing()) return;
    try {
      const r = await fetch(`/app/${pid}/dom`);
      if (r.ok) render(await r.json());
    } catch (e) {}
  }

  function onClick(ev) {
    const t = ev.target.closest('[data-ax-id]');
    if (!t) return;
    if (t.dataset.kind === 'text') return;
    ev.preventDefault();
    act({id: t.dataset.axId, action: 'press'});
  }
  overlay.addEventListener('click', onClick);
  off.addEventListener('click', onClick);

  overlay.addEventListener('change', (ev) => {
    const t = ev.target;
    if (t.dataset && t.dataset.kind === 'text')
      act({id: t.dataset.axId, action: 'set_value', value: t.value});
  });
  overlay.addEventListener('keydown', (ev) => {
    const t = ev.target;
    if (ev.key === 'Enter' && t.dataset && t.dataset.kind === 'text' && t.tagName === 'INPUT') {
      ev.preventDefault();
      act({id: t.dataset.axId, action: 'set_value', value: t.value});
      setTimeout(() => act({id: t.dataset.axId, action: 'confirm'}), 200);
      t.blur();
    }
  });

  function restartStream() {
    statusEl.textContent = 'Reconnecting to app stream…';
    statusEl.style.display = 'block';
    screenEl.src = `/app/${pid}/stream?ts=${Date.now()}`;
    clearTimeout(statusTimer);
    statusTimer = setTimeout(() => { statusEl.style.display = 'none'; }, 2000);
  }
  screenEl.addEventListener('error', () => { if (alive) setTimeout(restartStream, 500); });
  screenEl.addEventListener('load', () => { statusEl.style.display = 'none'; });

  // Coordinates stay pane-relative == window-relative, exactly like APP_PAGE.
  screenEl.addEventListener('click', (ev) => {
    const r = screenEl.getBoundingClientRect();
    act({action: 'click', x: ev.clientX - r.left, y: ev.clientY - r.top});
  });

  refresh();
  es = new EventSource(`/app/${pid}/events`);
  es.onmessage = (ev) => {
    host.classList.remove('gone');
    if (!typing()) render(JSON.parse(ev.data));
  };
  es.addEventListener('gone', (ev) => {
    banner.textContent = 'App unavailable: ' + (JSON.parse(ev.data).error || 'window gone');
    host.classList.add('gone');
  });
  // Fallback poll (SSE is primary), matching APP_PAGE's initial refresh cadence.
  pollTimer = setInterval(refresh, 4000);

  return {
    pid,
    focus() {
      fetch('/api/workspace/focus', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({pid}),
      }).catch(() => {});
    },
    destroy() {
      alive = false;
      if (es) es.close();
      clearInterval(pollTimer);
      clearTimeout(statusTimer);
      clearTimeout(idleTimer);
      host.remove();
    },
  };
}

// ---- Workspace shell: sidebar, pane lifecycle, arrange. ----
const applist = document.getElementById('applist');
const panesEl = document.getElementById('panes');
const emptyEl = document.getElementById('empty');
const panes = new Map(); // pid -> Pane instance

function layoutGrid() {
  const n = panes.size;
  const cols = n ? Math.ceil(Math.sqrt(n)) : 1;
  panesEl.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
  emptyEl.style.display = n ? 'none' : 'block';
}

function openPane(pid, name) {
  if (panes.has(pid)) return;
  const frag = document.getElementById('pane-tpl').content.cloneNode(true);
  const section = frag.querySelector('section');
  section.setAttribute('aria-label', name);
  section.dataset.pid = pid;
  panesEl.appendChild(section);
  const pane = Pane(pid, name, section);
  section.querySelector('.focus').addEventListener('click', () => pane.focus());
  section.querySelector('.close').addEventListener('click', () => closePane(pid));
  panes.set(pid, pane);
  layoutGrid();
  syncPressed();
}

function closePane(pid) {
  const pane = panes.get(pid);
  if (!pane) return;
  pane.destroy();
  panes.delete(pid);
  layoutGrid();
  syncPressed();
}

function togglePane(pid, name) {
  if (panes.has(pid)) closePane(pid); else openPane(pid, name);
}

function syncPressed() {
  for (const btn of applist.querySelectorAll('button[data-pid]'))
    btn.setAttribute('aria-pressed', panes.has(+btn.dataset.pid) ? 'true' : 'false');
}

// Sidebar live-update: poll /workspace/apps ~3s and diff the button list.
async function refreshApps() {
  let apps;
  try {
    const r = await fetch('/workspace/apps');
    if (!r.ok) return;
    apps = (await r.json()).apps;
  } catch (e) { return; }
  const wanted = new Map(apps.map(a => [a.pid, a.name]));
  for (const btn of Array.from(applist.querySelectorAll('button[data-pid]'))) {
    if (!wanted.has(+btn.dataset.pid)) btn.remove();
  }
  const have = new Set([...applist.querySelectorAll('button[data-pid]')].map(b => +b.dataset.pid));
  for (const a of apps) {
    if (have.has(a.pid)) continue;
    const btn = document.createElement('button');
    btn.dataset.pid = a.pid;
    btn.setAttribute('aria-pressed', 'false');
    btn.innerHTML = '<span></span><small></small>';
    btn.children[0].textContent = a.name;
    btn.children[1].textContent = 'pid ' + a.pid;
    btn.addEventListener('click', () => togglePane(a.pid, a.name));
    applist.appendChild(btn);
  }
  // Close panes whose app has quit.
  for (const pid of [...panes.keys()]) if (!wanted.has(pid)) closePane(pid);
  syncPressed();
}

document.getElementById('arrange').addEventListener('click', () => {
  const pids = [...panesEl.querySelectorAll('section.pane')].map(s => +s.dataset.pid);
  if (!pids.length) return;
  fetch('/api/workspace/arrange', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({pids}),
  }).catch(() => {});
});

refreshApps();
setInterval(refreshApps, 3000);
</script></body></html>"""
