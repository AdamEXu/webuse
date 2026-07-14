# webuse

**Drive native macOS apps like websites.** webuse renders any running Mac app as a live,
interactive web page: a screen stream with an invisible "ghost DOM" of real HTML elements
(`<button>`, `<input>`, …) laid exactly over every native control, built from the macOS
Accessibility tree. Point a browser agent (browser-use, Comet, Claude in Chrome, …) at the URL
and it drives the app the same way it drives a website — reading the DOM, clicking elements,
typing — no computer-use screenshotting required. Apps that expose no accessibility info still
work via coordinate clicks on the video layer.

> **Why this works:** browser agents are optimized for the DOM — they read the whole page in one
> shot and click elements by their box. webuse gives them a real DOM whose elements map 1:1 to
> native controls (via `AXPress` / `AXValue`), delivered to the app **in the background** without
> moving your real cursor or stealing focus.

## Requirements

- macOS (built and tested on macOS 26 / Apple Silicon)
- [uv](https://docs.astral.sh/uv/)
- Two macOS permissions (one-time): **Accessibility** and **Screen Recording**

## Install

```sh
uv sync
```

## Run

```sh
uv run python main.py            # http://127.0.0.1:8787
uv run python main.py --port 9000 --host 127.0.0.1
```

Then open **http://127.0.0.1:8787** and pick an app.

On first run, grant the two permissions (the home page shows a live checklist with deep links):

- **Screen Recording** — so webuse can see the app. System Settings → Privacy & Security →
  Screen Recording. (webuse can trigger this prompt for you via the home page.)
- **Accessibility** — so it can click and type. System Settings → Privacy & Security →
  Accessibility. *(macOS has no API to request this — add the app yourself.)*

During development the permissions attach to your **terminal** (or whatever process runs
`main.py`), so grant them there. After granting, restart webuse and reload the page.

## Using it

| Page | What it is |
| --- | --- |
| `/` | Home: permission checklist + a live grid of running apps (updates as you open/quit apps). |
| `/app/{pid}` | A single app rendered as a ghost DOM over its live screen stream. |
| `/app/{pid}?debug=1` | Same, with every ghost element outlined — a "show layout bounds" for macOS. |
| `/workspace` | Multi-app view: a sidebar to switch apps, multiple app panes at once, per-window **Focus**, and **Arrange** to tile the real windows to the layout. |

**Sharing with a cloud agent (Cloudflare Tunnel).** The server prints an auth token at startup:

```sh
cloudflared tunnel --url http://127.0.0.1:8787
# then hand the agent:  https://<tunnel>.trycloudflare.com/?token=<token>
```

The first tokened request sets a cookie; subsequent requests are authorized. Direct localhost
requests skip the token; any *proxied* request (i.e. through the tunnel) must carry it.

> ⚠️ **Treat the tunnel URL as root on your Mac.** Anyone with the URL + token can see your screen
> and click anything. For anything beyond a quick test, put Cloudflare Access in front of a named
> tunnel. Also: `/app/{pid}/dom` exposes the full text of the app's window — don't stream an app
> showing secrets.

## HTTP API (what an agent calls)

All routes accept the token via `?token=` (once, to set the cookie) when not on localhost.

| Method & path | Body / params | Purpose |
| --- | --- | --- |
| `GET /app/{pid}/dom` | — | Ghost DOM as JSON: `{window, nodes:[{id,role,label,value,x,y,w,h,…}]}`. |
| `GET /app/{pid}/events` | — | Server-Sent Events: pushes a fresh ghost DOM on every change (AXObserver-driven) — no polling. |
| `GET /app/{pid}/stream` | — | MJPEG stream of the app window (works even when occluded or on another Space). |
| `POST /api/action` | `{pid, action, id?, value?, x?, y?, activate?}` or `{pid, actions:[…]}` for a batch | Act on the app (see below). |
| `GET /api/apps` | `?installed=1` for the full catalog | Running apps (or all installed apps with launch info). |
| `POST /api/apps/open` | `{bundle_id?}` / `{name?}` / `{path?}` | Launch an app; returns its `pid`. |
| `GET /api/apps/events` | — | SSE: running-app launches/quits. |
| `POST /api/workspace/focus` | `{pid}` | Raise + activate an app's window. |
| `POST /api/workspace/arrange` | `{pids:[…], cols?}` | Tile the given apps' native windows to a grid. |

**Actions** (`action` field):
- `press` — `AXPress` the element with `id` (a real semantic click; works on off-screen elements).
- `set_value` — set a text field's whole value: `{action:"set_value", id, value}`.
- `confirm` — `AXConfirm` (e.g. commit a field with Return).
- `click` — coordinate click at pane-relative `{x, y}`. Delivered to the app **in the background**
  by default (your cursor/focus don't move). If an app ignores background clicks, add
  `"activate": true` to raise it and click via the system cursor (a visible takeover — last resort).

Element `id`s (`e1`, `e2`, …) are stable across snapshots for the life of the process, so an agent
can read the DOM once and reuse ids.

## How it works

```
browser agent ─HTTP→ FastAPI (webuse/server.py)
                        ├─ ax.py        walk the Accessibility tree → nodes, stable element ids, AXPress/AXValue actions
                        ├─ dom.py       map AX roles → HTML tags
                        ├─ capture.py   ScreenCaptureKit → MJPEG (CGWindowList fallback; captures occluded / off-Space windows)
                        ├─ watch.py     per-app AXObserver on a background CFRunLoop → SSE push (instant DOM updates)
                        ├─ workspace.py multi-app page + sidebar
                        ├─ winctl.py    focus / move / tile native windows (settable AXPosition/AXSize)
                        └─ apps.py      installed-app catalog, launch, live running-app SSE
```

The page layers an absolutely-positioned, transparent HTML element over each native control at its
real screen box. Reads come from the DOM/SSE; the video is just for the agent's screenshots and for
humans watching. Clicks route back to `AXPress` (semantic, background) or a pid-targeted `CGEvent`
(coordinate fallback).

### Known limitations

- **One window per app** (whichever is focused). Multi-window support (per-window addressing) is
  in progress.
- Heavily virtualized views (huge tables, some Electron apps) only expose their visible rows — the
  same limitation agents already tolerate on virtualized web pages.

## Development

```sh
uv run python -c "import webuse.server"   # import sanity
```

`main.py` is the entry point; the server assembles the routers in `webuse/server.py`.
