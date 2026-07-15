"""FastAPI server: app picker, ghost-DOM page, MJPEG stream, SSE DOM push, action API."""

import asyncio
import html
import secrets
import time
from urllib.parse import urlparse

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel

from . import apps, ax, config, dom, input, menubar, watch, windows, workspace
from .capture import WindowCapture

TOKEN = config.token()  # stable across boots (env / persisted file / generated once)

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


def _same_origin(request: "Request | WebSocket") -> bool:
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
    id: str | None = None  # ghost-DOM element id: its center becomes the target point
    value: str | None = None  # set_value text, or the literal string for `type`
    x: float | None = None  # window-relative target (server adds the window origin)
    y: float | None = None
    x2: float | None = None  # drag end point (window-relative)
    y2: float | None = None
    dx: float = 0.0  # scroll: wheel deltas in pixels
    dy: float = 0.0
    code: str | None = None  # key: KeyboardEvent.code (physical, layout-robust)
    down: bool | None = None  # key: True=down, False=up, None=tap (down+up)
    shift: bool = False  # key/click modifier flags (KeyboardEvent.*Key)
    ctrl: bool = False
    alt: bool = False
    meta: bool = False
    activate: bool = False  # raise the app to the foreground before this step


class Action(BaseModel):
    pid: int
    window_id: int | None = None  # which window to target; None => focused/main
    action: str | None = None  # single-step form (back-compat)
    id: str | None = None
    value: str | None = None
    x: float | None = None
    y: float | None = None
    x2: float | None = None
    y2: float | None = None
    dx: float = 0.0
    dy: float = 0.0
    code: str | None = None
    down: bool | None = None
    shift: bool = False
    ctrl: bool = False
    alt: bool = False
    meta: bool = False
    activate: bool = False
    actions: list[Step] | None = None  # batch form: steps run strictly in order


class Activate(BaseModel):
    pid: int
    window_id: int | None = None


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


def _parse(handle: str) -> tuple[int, int | None]:
    """'pid' or 'pid.winid' -> (pid, window_id|None). Raises ValueError on junk."""
    return windows.parse_handle(handle)


@app.get("/app/{handle}", response_class=HTMLResponse)
def app_page(handle: str, debug: int = 0):
    try:
        pid, window_id = _parse(handle)
    except ValueError:
        return HTMLResponse("bad app handle", status_code=400)
    name = html.escape(ax.app_name(pid))
    return (
        APP_PAGE.replace("__PID__", str(pid))
        .replace("__HANDLE__", windows.format_handle(pid, window_id))
        .replace("__WINDOW_ID__", "null" if window_id is None else str(window_id))
        .replace("__APP__", name)
        .replace("__DEBUG__", "debug" if debug else "")
    )


@app.get("/app/{handle}/windows")
def app_windows(handle: str):
    """The app's windows, so the client can offer per-window navigation."""
    try:
        pid, _ = _parse(handle)
    except ValueError:
        return JSONResponse({"error": "bad app handle"}, status_code=400)
    return {"pid": pid, "windows": windows.list_windows(pid)}


@app.get("/app/{handle}/icon")
def app_icon(handle: str):
    """The app's icon as a PNG (app-level; window_id in the handle is ignored)."""
    try:
        pid, _ = _parse(handle)
    except ValueError:
        return PlainTextResponse("bad app handle", status_code=400)
    png = apps.icon_png(pid)
    if png is None:
        return PlainTextResponse("no icon", status_code=404)
    return Response(
        content=png, media_type="image/png",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/app/{handle}/menu")
def app_menu(handle: str):
    try:
        pid, _ = _parse(handle)
    except ValueError:
        return JSONResponse({"menu": [], "error": "bad app handle"}, status_code=400)
    try:
        return {"menu": menubar.read_menu(pid)}
    except Exception as e:  # a missing/odd menu bar must not break the page
        return {"menu": [], "error": str(e)}


@app.get("/app/{handle}/dom")
def app_dom(handle: str):
    try:
        pid, window_id = _parse(handle)
    except ValueError:
        return JSONResponse({"error": "bad app handle"}, status_code=400)
    try:
        snap = ax.snapshot(pid, window_id)
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


@app.get("/app/{handle}/events")
async def app_events(handle: str):
    pid, window_id = _parse(handle)
    watcher, q = watch.subscribe(pid, asyncio.get_running_loop(), window_id)

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


@app.get("/app/{handle}/stream")
def app_stream(handle: str):
    pid, window_id = _parse(handle)

    def frame():  # stays fresh: the DOM watcher re-snapshots on AXWindowMoved/Resized
        w = ax.last_window(pid, window_id)
        return (w["x"], w["y"], w["w"], w["h"]) if w else None

    cap = WindowCapture(pid, target_frame=frame(), get_frame=frame, window_id=window_id)
    return StreamingResponse(
        cap.mjpeg(), media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.websocket("/app/{handle}/stream_ws")
async def app_stream_ws(ws: WebSocket, handle: str):
    """Lower-latency screen transport: push each captured JPEG as a binary frame as
    soon as it's ready (the page swaps it onto the <img> via a Blob URL), instead of
    the MJPEG multipart stream. The client keeps the MJPEG endpoint as a fallback, so
    if this socket never opens (or drops) nothing regresses. The handle fixes which
    window is captured, exactly like /stream."""
    if not _ws_authed(ws):
        await ws.close(code=1008)
        return
    try:
        pid, window_id = _parse(handle)
    except ValueError:
        await ws.close(code=1008)
        return
    await ws.accept()

    def frame():
        w = ax.last_window(pid, window_id)
        return (w["x"], w["y"], w["w"], w["h"]) if w else None

    cap = WindowCapture(pid, target_frame=frame(), get_frame=frame, window_id=window_id)
    try:
        async for jpg in cap.jpegs():
            await ws.send_bytes(jpg)
    except (WebSocketDisconnect, RuntimeError):
        pass  # client went away (send after disconnect raises RuntimeError)
    except Exception:
        pass


def _global_target(pid: int, s: Step, window_id: int | None) -> tuple[float, float] | None:
    """Resolve a step's target to GLOBAL screen coords (points): by element id
    (its center from the last snapshot) or by window-relative x,y (window origin
    + offset). Every real-input path funnels through here."""
    if s.id:
        return ax.center(pid, s.id, window_id)  # element geometry is already global
    win = ax.last_window(pid, window_id)
    if win is None or s.x is None or s.y is None:
        return None
    return (win["x"] + s.x, win["y"] + s.y)


def _rel_point(pid: int, gx: float, gy: float, window_id: int | None) -> list[float] | None:
    win = ax.last_window(pid, window_id)
    return [round(gx - win["x"], 1), round(gy - win["y"], 1)] if win else None


def _dispatch(pid: int, s: Step, window_id: int | None = None) -> dict:
    if s.activate:
        input.activate(pid)
        windows.raise_window(pid, window_id)  # raise the addressed window, if any
    # Discovery-side AX actions: the web menu bar presses real menu items (which
    # live outside the app window and can't be reached by a coordinate click).
    if s.action in ("press", "confirm", "set_value"):
        if not s.id:
            return {"ok": False, "error": "id required"}
        return ax.perform(pid, s.id, s.action, s.value, window_id)
    # Keyboard: real HID keys, addressed by KeyboardEvent.code (+ modifiers).
    if s.action == "key":
        code = input.KEYMAP.get(s.code or "")
        if code is None:
            return {"ok": False, "error": f"unknown key {s.code!r}"}
        mods = input.flags_for(_mods(s))
        if s.down is None:
            input.key_tap(code, mods)
        else:
            input.key(code, mods, s.down)
        return {"ok": True, "key": s.code}
    if s.action == "type":
        if not s.value:
            return {"ok": False, "error": "value required"}
        input.type_text(s.value)
        return {"ok": True}
    # Everything else targets a point (global coords) via real HID input.
    g = _global_target(pid, s, window_id)
    if g is None:
        return {"ok": False, "error": "no window or target"}
    gx, gy = g
    if s.action == "click":
        input.click(gx, gy)
    elif s.action == "rightclick":
        input.click(gx, gy, button="right")
    elif s.action == "dblclick":
        input.click(gx, gy, count=2)
    elif s.action == "move":
        input.move(gx, gy)
    elif s.action == "scroll":
        input.scroll(gx, gy, s.dx, s.dy)
    elif s.action == "drag":
        win = ax.last_window(pid, window_id)
        if win is None or s.x2 is None or s.y2 is None:
            return {"ok": False, "error": "drag needs x2,y2"}
        input.drag(gx, gy, win["x"] + s.x2, win["y"] + s.y2)
    else:
        return {"ok": False, "error": f"unknown action {s.action!r}"}
    out: dict = {"ok": True}
    pt = _rel_point(pid, gx, gy, window_id)
    if pt:
        out["point"] = pt
    return out


def _mods(s: Step) -> dict:
    return {"shiftKey": s.shift, "ctrlKey": s.ctrl, "altKey": s.alt, "metaKey": s.meta}


@app.post("/api/activate")
def activate(a: Activate):
    """Raise the app to the foreground so HID events reach it, and (if a window is
    addressed) raise that window. Called on page load (one app at a time) and
    whenever the stage takes focus."""
    input.activate(a.pid)
    windows.raise_window(a.pid, a.window_id)
    return {"ok": True, "pid": a.pid, "window_id": a.window_id}


def _ws_authed(ws: WebSocket) -> bool:
    tok = ws.query_params.get("token") or ws.cookies.get("webuse_token")
    if _token_ok(tok):
        return True
    client = ws.client.host if ws.client else ""
    if client not in ("127.0.0.1", "::1") or any(h in ws.headers for h in _PROXY_HEADERS):
        return False
    # Browsers don't apply CORS to WebSockets, so a loopback socket alone can't prove
    # same-site: any page the victim visits could open ws://127.0.0.1 and inject HID or
    # read the stream. Require same-origin (mirrors _same_origin) to block that.
    return _same_origin(ws)


@app.websocket("/app/{handle}/ws")
async def app_ws(ws: WebSocket, handle: str):
    """Low-latency input channel: the page streams compact step messages
    (move/click/drag/scroll/key…) here instead of one HTTP POST per event. Each
    is run off the event loop so a drag's sleeps never stall the stream. The
    handle fixes which window every step targets."""
    if not _ws_authed(ws):
        await ws.close(code=1008)
        return
    try:
        pid, window_id = _parse(handle)
    except ValueError:
        await ws.close(code=1008)
        return
    await ws.accept()
    loop = asyncio.get_running_loop()
    try:
        while True:
            msg = await ws.receive_json()
            try:
                step = Step(**msg)
            except Exception:
                continue
            await loop.run_in_executor(None, _dispatch, pid, step, window_id)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


@app.post("/api/action")
def action(a: Action):
    steps = a.actions or (
        [Step(
            action=a.action, id=a.id, value=a.value, x=a.x, y=a.y, x2=a.x2, y2=a.y2,
            dx=a.dx, dy=a.dy, code=a.code, down=a.down, shift=a.shift, ctrl=a.ctrl,
            alt=a.alt, meta=a.meta, activate=a.activate,
        )]
        if a.action
        else []
    )
    if not steps:
        return JSONResponse({"ok": False, "error": "no action(s) given"}, status_code=400)
    results: list[dict] = []
    for i, s in enumerate(steps):
        if i:
            time.sleep(0.08)  # let the app react between steps
        r = _dispatch(a.pid, s, a.window_id)
        results.append(r)
        if not r.get("ok"):
            break  # stop at the first failure; its index = len(results) - 1
    watch.kick(a.pid, a.window_id)  # push a fresh ghost DOM to SSE clients right away
    ok = all(r.get("ok") for r in results)
    out: dict = {"ok": ok, "results": results}
    if not ok:
        out["failed_step"] = len(results) - 1
    try:  # inline post-action DOM so agents skip the observe round trip
        snap = ax.snapshot(a.pid, a.window_id)
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
html, body { height: 100%; }
body { margin: 0; font: 13px -apple-system, sans-serif; }
/* App shell: persistent left sidebar + main column (menu bar over the stage). */
#shell { display: flex; height: 100vh; }
#sidebar { flex: 0 0 210px; width: 210px; overflow-y: auto; background: #f5f5f7;
  border-right: 1px solid #d0d0d0; user-select: none; -webkit-user-select: none; }
#sb-home { display: block; padding: 8px 12px; color: #4c8bf5; text-decoration: none;
  font-weight: 600; border-bottom: 1px solid #e2e2e5; position: sticky; top: 0;
  background: #f5f5f7; z-index: 1; }
#sb-home:hover { background: #ececef; }
.sb-approw { display: flex; align-items: center; gap: 6px; padding: 5px 8px; cursor: pointer;
  white-space: nowrap; overflow: hidden; }
.sb-approw:hover { background: #ececef; }
.sb-approw.current { background: #e4edff; }
.sb-caret { width: 12px; flex: 0 0 12px; text-align: center; color: #999; font-size: 10px; }
.sb-icon { width: 18px; height: 18px; flex: 0 0 18px; border-radius: 4px; }
.sb-name { overflow: hidden; text-overflow: ellipsis; }
.sb-wins { padding-left: 4px; }
.sb-win { display: block; padding: 4px 8px 4px 34px; color: #333; text-decoration: none;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: 12px; }
.sb-win:hover { background: #ececef; }
.sb-win.current { background: #4c8bf5; color: #fff; }
.sb-win.min { opacity: .55; font-style: italic; }
.sb-empty { padding: 4px 8px 4px 34px; color: #aaa; font-size: 12px; }
#main { flex: 1 1 auto; min-width: 0; min-height: 0; display: flex; flex-direction: column;
  overflow: hidden; }
/* Fit the streamed feed into the area left of the sidebar / below the menu bar. */
#stagewrap { flex: 1 1 auto; min-width: 0; min-height: 0; display: flex;
  align-items: center; justify-content: center; overflow: hidden; background: #111; }
/* #stagebox is the on-screen (scaled) footprint; #stage keeps native window points
   and is shrunk into it with a CSS transform, so the ghost overlay stays aligned. */
#stagebox { position: relative; overflow: hidden; }
#stage { position: absolute; left: 0; top: 0; transform-origin: top left;
  overflow: hidden; outline: none; touch-action: none; }
#screen { display: block; position: absolute; left: 0; top: 0; }
@media (prefers-color-scheme: dark) {
  #sidebar { background: #1e1e20; border-color: #3a3a3d; }
  #sb-home { background: #1e1e20; border-color: #333; }
  #sb-home:hover, .sb-approw:hover, .sb-win:hover { background: #2a2a2d; }
  .sb-approw.current { background: #2b3a5e; }
  .sb-win { color: #ccc; }
  .sb-empty { color: #666; }
}
/* The ghost DOM is discovery-only now: every pointer event must reach the stage
   and forward as real HID input, so the overlay never intercepts the mouse. */
#overlay { position: absolute; left: 0; top: 0; pointer-events: none; }
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
/* Keyboard-capture indicator: shown only while the stage has focus, so the user
   is never silently trapped — click it (or anywhere outside the stage) to release. */
#kb { display: none; position: fixed; right: 12px; bottom: 12px; z-index: 12;
  padding: 6px 11px; background: #1a7f37; color: #fff; border-radius: 20px;
  font: 12px -apple-system, sans-serif; cursor: pointer; box-shadow: 0 2px 8px rgba(0,0,0,.25); }
body.capturing #kb { display: block; }
body.debug .ghost { outline: 1px solid rgba(255, 0, 255, .8);
  background: rgba(255, 0, 255, .06); }
body.debug span.ghost { outline-color: rgba(0, 160, 255, .8);
  background: rgba(0, 160, 255, .05); }
/* No in-page cursor: interaction is real HID input, so the actual system cursor
   moves on the Mac and is captured in the stream. Nothing is faked here. */
/* Web menu bar: the app's real menus, rendered client-side; only the chosen
   item is sent to the app (as an AXPress). */
#menubar { display: flex; gap: 1px; background: #f5f5f7; border-bottom: 1px solid #d0d0d0;
  font: 13px -apple-system, sans-serif; padding: 2px 6px; position: relative; z-index: 20;
  user-select: none; -webkit-user-select: none; flex-wrap: wrap; }
#menubar .top { padding: 3px 10px; border-radius: 5px; }
#menubar .top:hover, #menubar .top.open { background: #dfe0e4; }
.menu { position: fixed; min-width: 210px; background: #fff; border: 1px solid #c8c8c8;
  border-radius: 8px; box-shadow: 0 10px 28px rgba(0,0,0,.20); padding: 4px; z-index: 40;
  font: 13px -apple-system, sans-serif; }
.menu .item { display: flex; align-items: center; justify-content: space-between; gap: 26px;
  padding: 4px 10px; border-radius: 5px; white-space: nowrap; }
.menu .item:not(.disabled):hover { background: #4c8bf5; color: #fff; }
.menu .item:not(.disabled):hover .sc { color: #fff; }
.menu .item.disabled { color: #b3b3b3; }
.menu .sep { height: 1px; background: #e3e3e3; margin: 4px 8px; }
.menu .lbl { display: flex; gap: 6px; }
.menu .sc { color: #999; font-size: 12px; }
.menu .item.sub .sc::after { content: '▸'; }
@media (prefers-color-scheme: dark) {
  #menubar { background: #2a2a2c; border-color: #444; color: #eee; }
  #menubar .top:hover, #menubar .top.open { background: #3a3a3d; }
  .menu { background: #2c2c2e; border-color: #555; color: #eee; }
  .menu .sep { background: #444; }
}
</style></head>
<body class="__DEBUG__">
<div id="agent-note">This page is a live macOS application ("__APP__") shown in the
FOREGROUND: an MJPEG screen stream with an invisible ghost DOM of HTML elements
positioned over it. The ghost DOM is for DISCOVERY only — read it to find controls
and their labels/positions. ALL interaction is real HID input delivered to the app
(the real cursor moves and the app is focused).
Drive it via POST /api/action with {"pid":__PID__,"window_id":__WINDOW_ID__,...} — one
step or "actions":[...] run strictly in order (window_id null targets the focused window). Point-addressed kinds take a ghost-element "id" (its center is
the target) OR window-relative "x","y": click, rightclick, dblclick, move (hover),
drag ("x","y" -> "x2","y2"), scroll ("dx","dy" pixels). Keyboard kinds: type
("value": literal text) and key ("code": a KeyboardEvent.code like "Enter"/"KeyA",
optional "shift"/"ctrl"/"alt"/"meta", "down": true/false or omit for a tap). The web
menu bar still presses real menu items over Accessibility (action "press" with an "id").
Example: {"pid":__PID__,"actions":[{"action":"click","id":"e12"},
{"action":"type","value":"hello"},{"action":"key","code":"Enter"}]}.
On failure the response has "failed_step"; it always includes per-step "results"
(each with "point":[x,y] where it landed) and "dom" — the fresh post-action ghost DOM
({window, truncated, nodes}) — so you act and observe in one round trip.
Content updates live; re-read after acting.</div>
<div id="banner"></div>
<div id="status"></div>
<div id="kb">⌨ keyboard captured — click to release</div>
<div id="shell">
<aside id="sidebar"><a id="sb-home" href="/">← webuse</a><div id="sb-apps"></div></aside>
<main id="main">
<div id="menubar"></div>
<div id="stagewrap"><div id="stagebox"><div id="stage" tabindex="0"><img id="screen" src="/app/__HANDLE__/stream" alt="live app screen" draggable="false">
<div id="overlay"></div></div></div></div>
</main>
</div>
<div id="offscreen"></div>
<script>
const PID = __PID__;
const HANDLE = "__HANDLE__";      // pid or pid.winid — which window this page drives
const WINDOW_ID = __WINDOW_ID__;  // null (focused window) or a specific CGWindowID
const screenEl = document.getElementById('screen');
const overlay = document.getElementById('overlay');
const off = document.getElementById('offscreen');
const stage = document.getElementById('stage');
const stagewrap = document.getElementById('stagewrap');
const stagebox = document.getElementById('stagebox');

// ---- Responsive feed scaling: shrink the native-sized stage (window points) to
// fit the area left of the sidebar / below the menu bar, preserving aspect via the
// more-constraining axis, never enlarging (scale <= 1). The whole #stage is scaled
// with one CSS transform, so the ghost overlay (native coords inside it) stays
// pixel-aligned with the streamed frame. rel() divides displayed offsets by `scale`
// to recover true window points before any input is sent. ----
let winW = 0, winH = 0, scale = 1;
function applyScale() {
  if (!winW || !winH) return;
  const availW = stagewrap.clientWidth, availH = stagewrap.clientHeight;
  if (availW < 1 || availH < 1) return;
  scale = Math.min(availW / winW, availH / winH, 1);
  setStyle(stage, 'width', winW + 'px'); setStyle(stage, 'height', winH + 'px');
  setStyle(stage, 'transform', 'scale(' + scale + ')');
  setStyle(stagebox, 'width', (winW * scale) + 'px');
  setStyle(stagebox, 'height', (winH * scale) + 'px');
}
window.addEventListener('resize', applyScale);

// Low-latency input over a WebSocket: one persistent connection carries every
// mouse/keyboard step, instead of an HTTP POST per event. Falls back to POST
// until the socket is open (and if it drops).
let ws = null, wsOpen = false;
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/app/${HANDLE}/ws`);
  ws.onopen = () => { wsOpen = true; };
  ws.onclose = () => { wsOpen = false; setTimeout(connectWS, 1000); };
  ws.onerror = () => { try { ws.close(); } catch (e) {} };
}
connectWS();
function send(payload) {
  payload.pid = PID;
  if (WINDOW_ID !== null && payload.window_id === undefined) payload.window_id = WINDOW_ID;
  if (wsOpen && ws.readyState === 1) { try { ws.send(JSON.stringify(payload)); return; } catch (e) {} }
  fetch('/api/action', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  }).catch(() => {});
}
let refreshTimer = null;
function refreshSoon() { clearTimeout(refreshTimer); refreshTimer = setTimeout(refresh, 250); }
// Menu bar still drives real menu items over Accessibility; then re-read the DOM.
function act(payload) { send(payload); refreshSoon(); }

// Raise the app to the foreground (one app at a time) so HID events reach it.
function activateApp() {
  fetch('/api/activate', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({pid: PID, window_id: WINDOW_ID}),
  }).catch(() => {});
}

// ---- Web menu bar: the app's real menus, rendered client-side. Only the
// chosen item is sent to the app (as an AXPress of its element id). ----
const menubarEl = document.getElementById('menubar');
let menuStack = [];    // open .menu divs, outer-first
let openTop = null;    // the currently-open top-level .top element
function closeMenus(n) {
  while (menuStack.length > n) menuStack.pop().remove();
  if (n === 0 && openTop) { openTop.classList.remove('open'); openTop = null; }
}
function buildMenu(items, level, x, y) {
  closeMenus(level);   // after this, menuStack.length === level
  const m = document.createElement('div');
  m.className = 'menu';
  for (const it of items) {
    if (it.separator) { const s = document.createElement('div'); s.className = 'sep'; m.appendChild(s); continue; }
    const el = document.createElement('div');
    el.className = 'item' + (it.disabled ? ' disabled' : '') + (it.children ? ' sub' : '');
    const lbl = document.createElement('span'); lbl.className = 'lbl';
    lbl.textContent = (it.checked ? '\\u2713 ' : '') + it.title;
    el.appendChild(lbl);
    if (it.shortcut || it.children) {
      const sc = document.createElement('span'); sc.className = 'sc'; sc.textContent = it.shortcut || '';
      el.appendChild(sc);
    }
    m.appendChild(el);
    if (it.disabled) continue;
    if (it.children) {
      el.addEventListener('mouseenter', () => {
        const r = el.getBoundingClientRect();
        buildMenu(it.children, level + 1, r.right - 5, r.top - 5);
      });
    } else {
      el.addEventListener('mouseenter', () => closeMenus(level + 1));
      if (it.id) el.addEventListener('click', (e) => {
        e.stopPropagation(); closeMenus(0); act({id: it.id, action: 'press'});
      });
    }
  }
  document.body.appendChild(m);
  const w = m.offsetWidth;
  if (x + w > window.innerWidth) x = Math.max(2, window.innerWidth - w - 4);
  m.style.left = x + 'px'; m.style.top = y + 'px';
  menuStack[level] = m;
  return m;
}
function openTopMenu(topEl, item) {
  closeMenus(0);
  topEl.classList.add('open'); openTop = topEl;
  const r = topEl.getBoundingClientRect();
  buildMenu(item.children || [], 0, r.left, r.bottom + 2);
}
function renderMenubar(tree) {
  menubarEl.replaceChildren();
  for (const top of tree) {
    if (!top.title || !top.children) continue;
    const t = document.createElement('div'); t.className = 'top'; t.textContent = top.title;
    t.addEventListener('click', (e) => {
      e.stopPropagation();
      if (openTop === t) closeMenus(0); else openTopMenu(t, top);
    });
    t.addEventListener('mouseenter', () => { if (openTop && openTop !== t) openTopMenu(t, top); });
    menubarEl.appendChild(t);
  }
}
document.addEventListener('click', () => closeMenus(0));
async function loadMenu() {
  if (menuStack.length) return;  // don't rebuild while a menu is open
  try {
    const r = await fetch(`/app/${HANDLE}/menu`);
    if (r.ok) renderMenubar((await r.json()).menu || []);
  } catch (e) {}
}
loadMenu();
setInterval(loadMenu, 5000);

// ---- Scroll: a wheel over the stage posts a real HID scroll at that point ----
let wheelAccum = {dx: 0, dy: 0}, wheelTimer = null, wheelPt = null;
stage.addEventListener('wheel', (ev) => {
  ev.preventDefault();  // don't scroll the browser page
  wheelPt = rel(ev);
  wheelAccum.dx += ev.deltaX; wheelAccum.dy += ev.deltaY;
  if (!wheelTimer) wheelTimer = setTimeout(flushWheel, 55);
}, {passive: false});
function flushWheel() {
  wheelTimer = null;
  const dx = wheelAccum.dx, dy = wheelAccum.dy;
  wheelAccum = {dx: 0, dy: 0};
  if (!wheelPt || (!dx && !dy)) return;
  send({action: 'scroll', x: wheelPt.x, y: wheelPt.y, dx, dy});
  refreshSoon();
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
  setAttr(e, 'title', n.help || n.label);  // AXHelp tooltip when present
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
  setStyle(screenEl, 'width', w.w + 'px'); setStyle(screenEl, 'height', w.h + 'px');
  winW = w.w; winH = w.h; applyScale();  // stage kept at native points, scaled to fit
  sync(overlay, d.nodes.filter(n => !n.offscreen));
  sync(off, d.nodes.filter(n => n.offscreen));
}

async function refresh() {
  try {
    const r = await fetch(`/app/${HANDLE}/dom`);
    if (r.ok) render(await r.json());
  } catch (e) {}
}

// ---- Mouse: forward every pointer event on the stage as real HID input.
// Coords are window-relative (the server adds the window origin -> global). ----
function rel(ev) {
  // screenEl lives inside the scaled #stage, so its rect is already in displayed
  // (scaled) pixels; divide the offset by `scale` to get true window points.
  const r = screenEl.getBoundingClientRect();
  return {x: (ev.clientX - r.left) / scale, y: (ev.clientY - r.top) / scale};
}
let hoverAt = 0;         // last hover post (throttle)
let press = null;        // {x,y} where the left button went down, while held
let dragged = false;     // moved past the drag threshold while pressed
let swallowClick = false; // suppress the click that trails a completed drag

stage.addEventListener('mousemove', (ev) => {
  const p = rel(ev);
  if (press) { if (Math.hypot(p.x - press.x, p.y - press.y) > 4) dragged = true; return; }
  const now = performance.now();
  if (now - hoverAt < 16) return;  // throttle hover to ~60fps (cheap over WS)
  hoverAt = now;
  send({action: 'move', x: p.x, y: p.y});
});
stage.addEventListener('mousedown', (ev) => {
  if (ev.button === 0) { press = rel(ev); dragged = false; swallowClick = false; }
});
stage.addEventListener('mouseup', (ev) => {
  if (ev.button !== 0 || !press) return;
  if (dragged) {  // press-drag-release becomes one real drag gesture
    const p = rel(ev);
    send({action: 'drag', x: press.x, y: press.y, x2: p.x, y2: p.y});
    swallowClick = true;
    refreshSoon();
  }
  press = null; dragged = false;
});
stage.addEventListener('click', (ev) => {
  if (swallowClick) { swallowClick = false; return; }
  if (ev.detail > 1) return;  // 2nd/3rd of a multi-click; dblclick handles doubles
  const p = rel(ev);
  send({action: 'click', x: p.x, y: p.y});
  refreshSoon();
});
stage.addEventListener('dblclick', (ev) => {
  const p = rel(ev);
  send({action: 'dblclick', x: p.x, y: p.y});
  refreshSoon();
});
stage.addEventListener('contextmenu', (ev) => {
  ev.preventDefault();
  const p = rel(ev);
  send({action: 'rightclick', x: p.x, y: p.y});
  refreshSoon();
});

// ---- Keyboard: forward keys as real HID, but only while the stage has focus,
// with a visible "captured" badge so the user is never silently trapped. ----
const kbBadge = document.getElementById('kb');
kbBadge.addEventListener('click', () => stage.blur());  // click badge to release
// Forward every key — including modifiers — as a real key down/up by physical
// code (no modifier flags), so the OS tracks modifier state exactly like a
// hardware keyboard. Any held key is released on blur/hide so a lost keyup can
// never leave Shift/Cmd stuck down.
const held = new Set();
function releaseHeld() {
  for (const code of held) send({action: 'key', code, down: false});
  held.clear();
}
stage.addEventListener('focus', () => {
  document.body.classList.add('capturing');
  activateApp();  // raise the app so keystrokes land there
});
stage.addEventListener('blur', () => { document.body.classList.remove('capturing'); releaseHeld(); });
window.addEventListener('blur', releaseHeld);
document.addEventListener('visibilitychange', () => { if (document.hidden) releaseHeld(); });
stage.addEventListener('keydown', (ev) => {
  ev.preventDefault();  // the app, not the browser, handles it
  held.add(ev.code);
  send({action: 'key', code: ev.code, down: true});
});
stage.addEventListener('keyup', (ev) => {
  ev.preventDefault();
  held.delete(ev.code);
  send({action: 'key', code: ev.code, down: false});
});

// Stream transport. The page starts on MJPEG (the <img> src set server-side) and
// upgrades to a lower-latency binary-JPEG WebSocket when it connects; if that
// socket never opens (or drops) MJPEG keeps running, so nothing regresses.
const statusEl = document.getElementById('status');
let lastSize = null, statusTimer = null;

// --- Low-latency WebSocket frames: swap each binary JPEG onto the <img> via a
// Blob URL. The previous frame's URL is revoked on a short delay so it isn't
// released mid-decode, keeping memory bounded to a couple of frames. ---
let streamWS = null, wsFrames = false, curBlobUrl = null;
function wsActive() { return streamWS && streamWS.readyState === 1 && wsFrames; }
function connectStreamWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  let sock;
  try { sock = new WebSocket(`${proto}://${location.host}/app/${HANDLE}/stream_ws`); }
  catch (e) { return; }
  sock.binaryType = 'blob';
  streamWS = sock;
  sock.onmessage = (ev) => {
    wsFrames = true;
    const url = URL.createObjectURL(ev.data);
    const old = curBlobUrl; curBlobUrl = url;
    screenEl.src = url;  // replaces the MJPEG connection on the first frame
    statusEl.style.display = 'none';
    if (old) setTimeout(() => URL.revokeObjectURL(old), 200);
  };
  sock.onclose = () => {
    streamWS = null;
    if (wsFrames) { wsFrames = false; restartStream(); }  // fall back to MJPEG
    setTimeout(connectStreamWS, 1500);  // keep trying to restore the fast path
  };
  sock.onerror = () => { try { sock.close(); } catch (e) {} };
}

function restartStream() {
  if (wsActive()) return;  // the WebSocket carries frames + handles resize itself
  statusEl.textContent = 'Reconnecting to app stream…';
  statusEl.style.display = 'block';
  if (curBlobUrl) { URL.revokeObjectURL(curBlobUrl); curBlobUrl = null; }
  screenEl.src = `/app/${HANDLE}/stream?ts=${Date.now()}`;
  clearTimeout(statusTimer);
  statusTimer = setTimeout(() => { statusEl.style.display = 'none'; }, 2000);
}
screenEl.addEventListener('error', () => setTimeout(restartStream, 500));
screenEl.addEventListener('load', () => { statusEl.style.display = 'none'; });
connectStreamWS();

// ---- Left sidebar: running apps (icon + name), each expandable to its windows.
// Clicking an app opens its focused window; clicking a window switches this page to
// that handle. Live-updates via the apps SSE feed; windows of expanded apps poll. ----
const sbApps = document.getElementById('sb-apps');
const expanded = new Set();       // pids whose windows are shown
const winCache = new Map();       // pid -> last windows list (instant re-render)
if (PID) expanded.add(PID);       // current app expanded by default
function renderApps(list) {
  sbApps.replaceChildren();
  for (const a of list) sbApps.appendChild(appRow(a));
}
function appRow(a) {
  const wrap = document.createElement('div');
  wrap.className = 'sb-app'; wrap.dataset.pid = a.pid;
  const row = document.createElement('div');
  row.className = 'sb-approw' + (a.pid === PID ? ' current' : '');
  const caret = document.createElement('span');
  caret.className = 'sb-caret'; caret.textContent = expanded.has(a.pid) ? '\\u25be' : '\\u25b8';
  const icon = document.createElement('img');
  icon.className = 'sb-icon'; icon.src = `/app/${a.pid}/icon`; icon.alt = '';
  const name = document.createElement('span'); name.className = 'sb-name'; name.textContent = a.name;
  row.append(caret, icon, name);
  const wins = document.createElement('div');
  wins.className = 'sb-wins'; wins.dataset.pid = a.pid;
  wins.style.display = expanded.has(a.pid) ? '' : 'none';
  caret.addEventListener('click', (e) => { e.stopPropagation(); toggleApp(a.pid, wins, caret); });
  row.addEventListener('click', () => { location.href = '/app/' + a.pid; });
  wrap.append(row, wins);
  if (expanded.has(a.pid)) loadWins(a.pid, wins);
  return wrap;
}
function toggleApp(pid, wins, caret) {
  if (expanded.has(pid)) {
    expanded.delete(pid); wins.style.display = 'none'; caret.textContent = '\\u25b8';
  } else {
    expanded.add(pid); wins.style.display = ''; caret.textContent = '\\u25be';
    loadWins(pid, wins);
  }
}
async function loadWins(pid, wins) {
  if (winCache.has(pid)) renderWins(pid, wins, winCache.get(pid));  // instant
  try {
    const r = await fetch(`/app/${pid}/windows`);
    if (!r.ok) return;
    const list = (await r.json()).windows || [];
    winCache.set(pid, list);
    renderWins(pid, wins, list);
  } catch (e) {}
}
function renderWins(pid, wins, list) {
  wins.replaceChildren();
  for (const w of list) {
    const el = document.createElement('a');
    el.className = 'sb-win' + (w.minimized ? ' min' : '');
    const isCur = pid === PID && (WINDOW_ID !== null ? w.window_id === WINDOW_ID : !!w.focused);
    if (isCur) el.classList.add('current');
    el.href = (w.window_id != null) ? `/app/${pid}.${w.window_id}` : `/app/${pid}`;
    el.textContent = w.title || '(untitled window)';
    wins.appendChild(el);
  }
  if (!list.length) {
    const e = document.createElement('div'); e.className = 'sb-empty';
    e.textContent = 'No windows'; wins.appendChild(e);
  }
}
fetch('/api/apps').then(r => r.json()).then(d => renderApps(d.apps || [])).catch(() => {});
try {
  const sbEs = new EventSource('/api/apps/events');
  sbEs.onmessage = (ev) => { try { renderApps(JSON.parse(ev.data).apps || []); } catch (e) {} };
} catch (e) {}
setInterval(() => {  // keep expanded apps' window lists fresh (new/closed windows)
  for (const pid of expanded) {
    const wins = sbApps.querySelector(`.sb-wins[data-pid="${pid}"]`);
    if (wins) loadWins(pid, wins);
  }
}, 4000);

activateApp();  // one app at a time: raise it to the foreground on page load
refresh(); // initial paint / fallback; live updates arrive over SSE below

const banner = document.getElementById('banner');
const es = new EventSource(`/app/${HANDLE}/events`); // auto-reconnects on error
es.onmessage = (ev) => {
  document.body.classList.remove('gone');
  render(JSON.parse(ev.data));
};
es.addEventListener('gone', (ev) => {
  banner.textContent = 'App unavailable: ' + (JSON.parse(ev.data).error || 'window gone');
  document.body.classList.add('gone');
});
</script></body></html>"""
