"""FastAPI server: app picker, ghost-DOM page, MJPEG stream, SSE DOM push, action API."""

import asyncio
import html
import os
import secrets
import time
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from pydantic import BaseModel

from . import apps, ax, cursor, dom, watch, workspace
from .capture import WindowCapture

TOKEN = os.environ.get("WEBUSE_TOKEN") or secrets.token_urlsafe(12)

app = FastAPI(title="webuse")

# Mount the multi-app workspace and the installed/running-apps catalog+launcher.
# Neither module imports server.py (they depend only on ax/winctl), so importing
# them here creates no cycle. The auth+CSRF middleware below wraps their routes too.
app.include_router(workspace.router)
app.include_router(apps.router)


# Headers a reverse proxy / tunnel injects. cloudflared (the documented tunnel)
# proxies from loopback and may rewrite Host to 127.0.0.1, so the socket peer and
# Host header alone can't prove a request is direct-local — the presence of any
# forwarding header reliably marks it as proxied, so it must carry the token.
_PROXY_HEADERS = (
    "x-forwarded-for", "forwarded", "x-real-ip", "x-forwarded-host",
    "x-forwarded-proto", "cf-connecting-ip", "via",
)


def _is_local(request: Request) -> bool:
    """True only for direct loopback requests. A tunnel (e.g. cloudflared) proxies
    via 127.0.0.1, so the socket peer alone proves nothing — also require a
    loopback Host header and no proxy-forwarding headers."""
    client = request.client.host if request.client else ""
    if client not in ("127.0.0.1", "::1"):
        return False
    if any(h in request.headers for h in _PROXY_HEADERS):
        return False
    host = request.headers.get("host", "").lower()
    if host.startswith("["):  # bracketed IPv6, maybe with :port
        host = host[1 : host.index("]")] if "]" in host else host
    elif host.count(":") == 1:  # hostname/IPv4 with :port
        host = host.split(":")[0]
    return host in ("127.0.0.1", "localhost", "::1")


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _token_ok(supplied: str | None) -> bool:
    return supplied is not None and secrets.compare_digest(supplied, TOKEN)


def _same_origin(request: Request) -> bool:
    """Reject browser cross-site requests so a malicious page can't drive the
    local server via /api/action (which otherwise skips the token for localhost).
    Trust Sec-Fetch-Site when present; else compare the Origin host to Host.
    A non-browser client (no Origin, no Sec-Fetch-Site — curl, the agent) sends
    neither and carries no ambient cookies, so it isn't a CSRF vector."""
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site in ("same-origin", "none")
    origin = request.headers.get("origin")
    if not origin:
        return True
    return urlparse(origin).netloc.lower() == request.headers.get("host", "").lower()


@app.middleware("http")
async def auth(request: Request, call_next):
    qtoken = request.query_params.get("token")
    authed = (
        _is_local(request)
        or _token_ok(qtoken)
        or _token_ok(request.cookies.get("webuse_token"))
    )
    if not authed:
        return PlainTextResponse("unauthorized: append ?token=<token>", status_code=401)
    # CSRF: even a trusted-local request must not be driven cross-site by a webpage.
    if request.method not in _SAFE_METHODS and not _same_origin(request):
        return PlainTextResponse("forbidden: cross-site request", status_code=403)
    # A valid ?token= in the URL: set the cookie and redirect to a clean URL so the
    # secret doesn't linger in history, logs, or Referer. Safe methods only, so a
    # POST body is never dropped by the redirect.
    if qtoken is not None and _token_ok(qtoken) and request.method in _SAFE_METHODS:
        resp = RedirectResponse(str(request.url.remove_query_params("token")), status_code=303)
        resp.set_cookie("webuse_token", TOKEN, httponly=True, samesite="strict")
        return resp
    return await call_next(request)


class Step(BaseModel):
    action: str
    id: str | None = None
    value: str | None = None
    x: float | None = None
    y: float | None = None
    activate: bool = False  # click only: raise the app and use the global HID tap


class Action(BaseModel):
    pid: int
    action: str | None = None  # single-step form (back-compat)
    id: str | None = None
    value: str | None = None
    x: float | None = None
    y: float | None = None
    activate: bool = False
    actions: list[Step] | None = None  # batch form: steps run strictly in order


SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_"


def _perm_row(name: str, why: str, ok: bool, pane: str) -> str:
    badge = '<span class="ok">✓ granted</span>' if ok else '<span class="no">✗ missing</span>'
    link = "" if ok else f' — <a href="{SETTINGS_URL}{pane}">open System Settings</a>'
    return (
        f'<div class="perm">{badge}<div><strong>{name}</strong>'
        f"<br><small>{why}</small>{link}</div></div>"
    )


@app.get("/", response_class=HTMLResponse)
def index():
    ax_ok, sc_ok = ax.trusted(), ax.screen_recording()
    setup = ""
    if not (ax_ok and sc_ok):
        setup = f"""<div class="card">
<h2>One-time setup — webuse needs two macOS permissions</h2>
{_perm_row("Screen Recording", "so webuse can see the app", sc_ok, "ScreenCapture")}
{_perm_row("Accessibility", "so it can click and type", ax_ok, "Accessibility")}
<p><button onclick="fetch('/api/permissions/request',{{method:'POST'}})
  .then(()=>setTimeout(()=>location.reload(),1500))">Request permissions</button>
<small>Grant in System Settings, then restart webuse and reload this page.</small></p>
</div>"""
    # Server-rendered fallback so the list works before JS/SSE kicks in; the live
    # /api/apps/events feed (subscribed below) replaces it and keeps it current.
    cards = "".join(
        f'<a class="app" href="/app/{a["pid"]}"><strong>{html.escape(a["name"])}</strong>'
        f'<small>pid {a["pid"]} · <span onclick="event.preventDefault();'
        f"location.href='/app/{a['pid']}?debug=1'\">debug</span></small></a>"
        for a in apps._running_apps()
    )
    return f"""<!doctype html><meta charset="utf-8"><title>webuse — pick an app</title>
<style>
body {{ font: 15px -apple-system, sans-serif; max-width: 720px; margin: 3em auto; padding: 0 1em; }}
.card {{ border: 1px solid #e0a800; border-radius: 10px; padding: 1em 1.2em; margin: 1.5em 0;
  background: #fff9e6; }}
.card h2 {{ margin: 0 0 .6em; font-size: 1.05em; }}
.perm {{ display: flex; gap: .7em; align-items: baseline; margin: .5em 0; }}
.ok {{ color: #1a7f37; font-weight: 600; white-space: nowrap; }}
.no {{ color: #c0392b; font-weight: 600; white-space: nowrap; }}
.cta {{ display: block; margin: 1.2em 0; padding: 14px 18px; border-radius: 10px;
  background: #4c8bf5; color: #fff; text-decoration: none; font-weight: 600; }}
.cta:hover {{ background: #3b78e0; }}
.cta small {{ display: block; font-weight: 400; opacity: .9; margin-top: 2px; }}
h2.sec {{ font-size: 1em; margin: 1.6em 0 .6em; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 10px; padding: 0; }}
.app {{ display: flex; flex-direction: column; gap: 2px; padding: 12px 14px;
  border: 1px solid #ddd; border-radius: 10px; text-decoration: none; color: inherit; }}
a.app:hover {{ border-color: #4c8bf5; background: #f5f8ff; }}
.app small {{ color: #777; }}
.app small span {{ text-decoration: underline; cursor: pointer; }}
.app small button {{ font: inherit; font-size: 12px; padding: 2px 9px; cursor: pointer;
  border: 1px solid #4c8bf5; border-radius: 6px; background: #fff; color: #4c8bf5; }}
details {{ margin: 1em 0; }}
details > summary {{ cursor: pointer; color: #4c8bf5; }}
#empty-running {{ color: #888; }}
</style>
<body>
<h1>webuse</h1>
<p>Each running app below is served as a live, interactive web page.
Open one and drive it like a website. Append <code>?debug=1</code> to outline the ghost DOM.</p>
<a class="cta" href="/workspace">Open multi-app workspace →
<small>Drive several apps side by side on one page.</small></a>
{setup}
<h2 class="sec">Running apps</h2>
<div class="grid" id="running">{cards}</div>
<details id="installed-wrap">
<summary>Browse installed apps</summary>
<div class="grid" id="installed"><small>Loading…</small></div>
</details>
<script>
// Live running-apps list: subscribe to the apps SSE feed and rebuild the grid on
// every launch/quit. Falls back to the server-rendered cards if SSE never opens.
const running = document.getElementById('running');
function runCard(a) {{
  const el = document.createElement('a');
  el.className = 'app'; el.href = '/app/' + a.pid;
  const s = document.createElement('strong'); s.textContent = a.name;
  const sm = document.createElement('small'); sm.textContent = 'pid ' + a.pid + ' · ';
  const dbg = document.createElement('span'); dbg.textContent = 'debug';
  dbg.addEventListener('click', e => {{ e.preventDefault(); location.href = '/app/' + a.pid + '?debug=1'; }});
  sm.appendChild(dbg);
  el.append(s, sm);
  return el;
}}
function renderRunning(apps) {{
  running.textContent = '';
  if (!apps.length) {{
    const p = document.createElement('div'); p.id = 'empty-running';
    p.textContent = 'No regular apps running.'; running.appendChild(p); return;
  }}
  for (const a of apps) running.appendChild(runCard(a));
}}
try {{
  const es = new EventSource('/api/apps/events');
  es.onmessage = ev => {{ try {{ renderRunning(JSON.parse(ev.data).apps); }} catch (e) {{}} }};
}} catch (e) {{}}

// Installed-apps catalog: lazy-load on first expand. Running ones link straight to
// their page; the rest get an Open button (launch via /api/apps/open, then jump).
const wrap = document.getElementById('installed-wrap');
const installed = document.getElementById('installed');
let loaded = false;
wrap.addEventListener('toggle', async () => {{
  if (!wrap.open || loaded) return;
  loaded = true;
  try {{
    const r = await fetch('/api/apps?installed=1');
    const apps = (await r.json()).apps;
    installed.textContent = '';
    for (const a of apps) {{
      const el = document.createElement(a.running ? 'a' : 'div');
      el.className = 'app';
      if (a.running) el.href = '/app/' + a.pid;
      const s = document.createElement('strong'); s.textContent = a.name;
      const sm = document.createElement('small');
      if (a.running) {{ sm.textContent = 'running · pid ' + a.pid; }}
      else {{
        const b = document.createElement('button'); b.textContent = 'Open';
        b.addEventListener('click', () => openApp(a, b));
        sm.appendChild(b);
      }}
      el.append(s, sm);
      installed.appendChild(el);
    }}
  }} catch (e) {{ installed.textContent = 'Failed to load installed apps.'; }}
}});
async function openApp(a, btn) {{
  btn.disabled = true; btn.textContent = 'Opening…';
  try {{
    const r = await fetch('/api/apps/open', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{bundle_id: a.bundle_id, path: a.path, name: a.name}}),
    }});
    const j = await r.json();
    if (j.ok && j.pid) {{ location.href = '/app/' + j.pid; return; }}
    btn.disabled = false; btn.textContent = 'Retry';
  }} catch (e) {{ btn.disabled = false; btn.textContent = 'Retry'; }}
}}
</script>
</body>"""


@app.post("/api/permissions/request")
def permissions_request():
    ax.request_permissions()
    return {"trusted": ax.trusted(), "screen_recording": ax.screen_recording()}


@app.get("/app/{pid}", response_class=HTMLResponse)
def app_page(pid: int, debug: int = 0):
    name = html.escape(ax.app_name(pid))
    return APP_PAGE.replace("__PID__", str(pid)).replace("__APP__", name).replace(
        "__DEBUG__", "debug" if debug else ""
    )


@app.get("/app/{pid}/dom")
def app_dom(pid: int):
    try:
        snap = ax.snapshot(pid)
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return {
        "window": snap["window"],
        "truncated": snap["truncated"],
        "nodes": dom.build_nodes(snap),
    }


def _sse(item: tuple[str | None, str]) -> str:
    event, payload = item
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {payload}\n\n"


@app.get("/app/{pid}/events")
async def app_events(pid: int):
    watcher, q = watch.subscribe(pid, asyncio.get_running_loop())

    async def gen():
        try:
            if watcher.current:
                yield _sse(watcher.current)
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield _sse(item)
        finally:
            watcher.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/app/{pid}/stream")
def app_stream(pid: int):
    def frame():  # stays fresh: the DOM watcher re-snapshots on AXWindowMoved/Resized
        w = ax.last_window(pid)
        return (w["x"], w["y"], w["w"], w["h"]) if w else None

    cap = WindowCapture(pid, target_frame=frame(), get_frame=frame)
    return StreamingResponse(
        cap.mjpeg(), media_type="multipart/x-mixed-replace; boundary=frame"
    )


def _dispatch(pid: int, s: Step) -> dict:
    if s.action in ("press", "confirm", "set_value"):
        if not s.id:
            return {"ok": False, "error": "id required"}
        result = ax.perform(pid, s.id, s.action, s.value)
    elif s.action == "click":
        win = ax.last_window(pid)
        if win is None or s.x is None or s.y is None:
            return {"ok": False, "error": "no window or coords"}
        result = ax.click(pid, win["x"] + s.x, win["y"] + s.y)
        result["point"] = [s.x, s.y]
    else:
        return {"ok": False, "error": f"unknown action {s.action!r}"}
    # Drive the on-screen agent cursor to where the action landed (global coords).
    win, pt = ax.last_window(pid), result.get("point")
    if win and pt:
        cursor.show_action(win["x"] + pt[0], win["y"] + pt[1], s.action)
    return result


@app.post("/api/action")
def action(a: Action):
    steps = a.actions or (
        [Step(action=a.action, id=a.id, value=a.value, x=a.x, y=a.y, activate=a.activate)]
        if a.action
        else []
    )
    if not steps:
        return JSONResponse({"ok": False, "error": "no action(s) given"}, status_code=400)
    results: list[dict] = []
    for i, s in enumerate(steps):
        if i:
            time.sleep(0.08)  # let the app react between steps
        r = _dispatch(a.pid, s)
        results.append(r)
        if not r.get("ok"):
            break  # stop at the first failure; its index = len(results) - 1
    watch.kick(a.pid)  # push a fresh ghost DOM to SSE clients right away
    ok = all(r.get("ok") for r in results)
    out: dict = {"ok": ok, "results": results}
    if not ok:
        out["failed_step"] = len(results) - 1
    try:  # inline post-action DOM so agents skip the observe round trip
        snap = ax.snapshot(a.pid)
        out["dom"] = {
            "window": snap["window"],
            "truncated": snap["truncated"],
            "nodes": dom.build_nodes(snap),
        }
    except LookupError as e:
        out["dom"] = None
        out["dom_error"] = str(e)
    return out


APP_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>__APP__</title>
<style>
* { box-sizing: border-box; }
body { margin: 0; font: 13px -apple-system, sans-serif; }
#stage { position: relative; overflow: hidden; }
#screen { display: block; position: absolute; left: 0; top: 0; }
#overlay { position: absolute; left: 0; top: 0; }
.ghost { position: absolute; margin: 0; padding: 0; border: 0; border-radius: 0;
  background: transparent; color: transparent; caret-color: #111;
  font: 13px -apple-system, sans-serif; text-align: left; cursor: default;
  -webkit-appearance: none; appearance: none; }
.ghost:focus { outline: none; background: rgba(255,255,255,.01); }
a.ghost { text-decoration: none; }
span.ghost { pointer-events: none; overflow: hidden; }
input.ghost[type=checkbox], input.ghost[type=radio] { opacity: 0.01; }
#offscreen { position: absolute; left: -10000px; top: 0; width: 600px; overflow: hidden; }
#offscreen .ghost { position: static; display: block; color: #000; }
#agent-note { position: absolute; left: -10000px; top: 0; }
#banner { display: none; position: fixed; left: 0; right: 0; top: 0; z-index: 10;
  padding: 8px 12px; background: #c0392b; color: #fff; }
body.gone #banner { display: block; }
#status { display: none; position: fixed; right: 12px; top: 12px; z-index: 10;
  padding: 6px 10px; background: rgba(0,0,0,.75); color: #fff; border-radius: 6px; }
#focusbtn { position: fixed; left: 12px; top: 12px; z-index: 11; padding: 5px 11px;
  border: 1px solid var(--accent); border-radius: 6px; background: rgba(255,255,255,.92);
  color: var(--accent); cursor: pointer; font: 12px -apple-system, sans-serif; }
body.gone #focusbtn { top: 44px; }
#focus-footer { padding: 8px 12px; border-top: 1px solid #eee; color: #666; font-size: 12px; }
body.debug .ghost { outline: 1px solid rgba(255, 0, 255, .8);
  background: rgba(255, 0, 255, .06); }
body.debug span.ghost { outline-color: rgba(0, 160, 255, .8);
  background: rgba(0, 160, 255, .05); }
/* Agent presence (virtual cursor, click pulse, typing indicator) is drawn on
   the real Mac screen by the server, NOT in this page — the browser must never
   fake a cursor. See webuse/cursor.py. */
</style></head>
<body class="__DEBUG__">
<div id="agent-note">This page is a live macOS application ("__APP__") rendered as a
web page. Every control is a real HTML element positioned over a screen stream:
click buttons and links normally, type into inputs (values commit on change/Enter).
Prefer clicking the actual control elements: those are delivered via the accessibility
API in the background, without moving the user's real cursor or focus.
Clicking anywhere without a control forwards a native coordinate click at that point;
that path briefly activates the app and flicks the pointer to the target (then restores
it), so use it only when no control element covers what you need.
Fastest path — batch several steps into ONE request: POST /api/action with
{"pid":__PID__,"actions":[{"action":"set_value","id":"e12","value":"hello"},
{"action":"press","id":"e7"}]}. Steps run in order (actions: press, confirm,
set_value with "id"; click with window-relative "x","y"); on failure the response
has "failed_step". The response always includes per-step "results" (each with
"point":[x,y] where it landed) and "dom" — the fresh post-action ghost DOM
({window, truncated, nodes}) — so you can act and observe in one round trip
instead of re-reading the page.
Content updates live; re-read the page after acting.</div>
<div id="banner"></div>
<div id="status"></div>
<button id="focusbtn" type="button">Focus</button>
<div id="stage"><img id="screen" src="/app/__PID__/stream" alt="live app screen">
<div id="overlay"></div></div>
<div id="offscreen"></div>
<footer id="focus-footer"><strong>If clicks don't go through for some actions, it might be a focus issue. Try focusing the window then performing the action.</strong></footer>
<script>
const PID = __PID__;
// Raise + activate this app's native window (reuses the workspace focus endpoint).
// Helps when background coordinate clicks are ignored because the app isn't focused.
document.getElementById('focusbtn').addEventListener('click', () => {
  fetch('/api/workspace/focus', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({pid: PID}),
  }).catch(() => {});
});
const screenEl = document.getElementById('screen');
const overlay = document.getElementById('overlay');
const off = document.getElementById('offscreen');
const stage = document.getElementById('stage');

function typing() {
  const a = document.activeElement;
  return a && a.classList.contains('ghost') &&
    (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA');
}

async function act(payload) {
  payload.pid = PID;
  // Fire-and-observe. Agent presence (cursor/pulse) is rendered on the real
  // screen by the server from this same action — nothing is drawn in-page.
  try {
    await fetch('/api/action', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
  } catch (e) {}
  setTimeout(refresh, 400);
}

// Write-guarded setters: never touch the DOM when the value is already right,
// so agents' cached element refs survive refreshes untouched.
function setAttr(e, k, v) {
  if (v == null || v === '') { if (e.hasAttribute(k)) e.removeAttribute(k); }
  else if (e.getAttribute(k) !== String(v)) e.setAttribute(k, v);
}
function setStyle(e, k, v) { if (e.style[k] !== v) e.style[k] = v; }

function build(n, e) {
  if (!e) { // invariants for a given id+tag; sync() recreates on tag change
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

// Update in place, keyed by stable ax id, so element identity (and any
// references an agent is holding) survives refreshes.
const lastNode = new Map(); // id -> JSON of the node last applied to the DOM

function sync(container, nodes) {
  const byId = new Map();
  for (const el of Array.from(container.children)) byId.set(el.dataset.axId, el);
  const seen = new Set();
  for (const n of nodes) {
    seen.add(n.id);
    const existing = byId.get(n.id);
    const key = JSON.stringify(n);
    if (existing && existing.tagName.toLowerCase() === n.tag) {
      if (lastNode.get(n.id) === key) continue; // unchanged: don't touch it
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
  const title = (w.title ? w.title + ' — ' : '') + w.app;
  if (document.title !== title) document.title = title;
  const size = w.w + 'x' + w.h;
  if (lastSize && size !== lastSize) restartStream(); // restart at the new resolution
  lastSize = size;
  for (const el of [stage, screenEl]) {
    setStyle(el, 'width', w.w + 'px'); setStyle(el, 'height', w.h + 'px');
  }
  sync(overlay, d.nodes.filter(n => !n.offscreen));
  sync(off, d.nodes.filter(n => n.offscreen));
}

async function refresh() {
  if (typing()) return;
  try {
    const r = await fetch(`/app/${PID}/dom`);
    if (r.ok) render(await r.json());
  } catch (e) {}
}

function onClick(ev) {
  const t = ev.target.closest('[data-ax-id]');
  if (!t) return;
  if (t.dataset.kind === 'text') return; // allow focus for typing
  ev.preventDefault();
  act({id: t.dataset.axId, action: 'press'});
}
overlay.addEventListener('click', onClick);
off.addEventListener('click', onClick);

overlay.addEventListener('change', (ev) => {
  const t = ev.target;
  if (t.dataset && t.dataset.kind === 'text') {
    act({id: t.dataset.axId, action: 'set_value', value: t.value});
  }
});

overlay.addEventListener('keydown', (ev) => {
  const t = ev.target;
  if (ev.key === 'Enter' && t.dataset && t.dataset.kind === 'text' &&
      t.tagName === 'INPUT') {
    ev.preventDefault();
    act({id: t.dataset.axId, action: 'set_value', value: t.value});
    setTimeout(() => act({id: t.dataset.axId, action: 'confirm'}), 200);
    t.blur();
  }
});

// Stream resilience: the server closes the MJPEG stream on capture failure or
// window resize; restart it (with a status note) instead of showing a dead frame.
const statusEl = document.getElementById('status');
let lastSize = null, statusTimer = null;
function restartStream() {
  statusEl.textContent = 'Reconnecting to app stream…';
  statusEl.style.display = 'block';
  screenEl.src = `/app/${PID}/stream?ts=${Date.now()}`;
  clearTimeout(statusTimer);
  statusTimer = setTimeout(() => { statusEl.style.display = 'none'; }, 2000);
}
screenEl.addEventListener('error', () => setTimeout(restartStream, 500));
screenEl.addEventListener('load', () => { statusEl.style.display = 'none'; });

screenEl.addEventListener('click', (ev) => {
  const r = screenEl.getBoundingClientRect();
  act({action: 'click', x: ev.clientX - r.left, y: ev.clientY - r.top});
});

refresh(); // initial paint / fallback; live updates arrive over SSE below

const banner = document.getElementById('banner');
const es = new EventSource(`/app/${PID}/events`); // auto-reconnects on error
es.onmessage = (ev) => {
  document.body.classList.remove('gone');
  if (!typing()) render(JSON.parse(ev.data));
};
es.addEventListener('gone', (ev) => {
  banner.textContent = 'App unavailable: ' + (JSON.parse(ev.data).error || 'window gone');
  document.body.classList.add('gone');
});
</script></body></html>"""
