# webuse

**Drive native macOS apps like websites.** webuse renders any running Mac app as a live,
interactive web page: a screen stream with an invisible "ghost DOM" of real HTML elements
(`<button>`, `<input>`, …) laid exactly over every native control, built from the macOS
Accessibility tree. Point a **web agent** at the URL and it drives the app the same way it drives
a website — reading the DOM, clicking elements, typing — no computer-use screenshotting required.
Apps that expose no accessibility info still work via coordinate clicks on the video layer.

It's built for [**TinyFish**](https://tinyfish.ai) — hand the tokened URL to an agent in the
playground and it drives your Mac as if it were any other site. Nothing about the page is
TinyFish-specific, though: it's ordinary HTML, so browser-use, Comet, Claude in Chrome and
anything else that can read a DOM and click work the same way. The design rule throughout is
that **every capability is reachable as a plain DOM element** — a harness that can only click
buttons and fill text inputs, with no API access and no ability to press arbitrary keys, can
still do everything (see [Typing, keys, and the terminal](#typing-keys-and-the-terminal)).

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

Build the web frontend once (and after any frontend change), then start the server:

```sh
cd webuse/frontend && pnpm install && pnpm build && cd ../..
uv run python main.py            # http://127.0.0.1:8787
uv run python main.py --port 9000 --host 127.0.0.1
```

> **Frontend dev:** `cd webuse/frontend && pnpm dev` runs Vite with a proxy to the
> Python server, for hot-reload work on the SPA.

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
| `/app/{handle}` | A single app window rendered as a ghost DOM over its live screen stream, with a compose bar and a collapsible terminal below it. |
| `/app/{handle}?debug=1` | Same, with every ghost element outlined — a "show layout bounds" for macOS. |
| `/terminal` | A real zsh session on this Mac, listed in the sidebar like an app. Same session as the panel under every app page. |

A **handle** is either `pid` (the app's focused/main window) or `pid.windowID` for one specific
window — `GET /app/{pid}/windows` lists them, and the sidebar nests an app's windows under it.
Every per-app route takes the same handle.

### Typing, keys, and the terminal

Ghost text fields are non-editable mirrors, and many web agents can only fill text inputs and
click — they can't press arbitrary keys or call an HTTP API. So the chrome provides both as
plain DOM:

- **Compose bar** (under the app) — a real `<input>`; type text and click **Send** (or **Send ↵**)
  and it's delivered to the app as actual HID keystrokes. It shows which control currently has the
  app's keyboard focus. Beside it, buttons for `Enter` `Tab` `Esc` arrows `⌘A` `⌘C` `⌘V` — the keys
  typing alone can't express.
- **Terminal** — collapsed under the app view; click **▸ Terminal** to open it (the choice sticks).
  Type a command, click **Run**, read the output that appears above the box (it updates by itself
  over SSE). **Prefer it over driving the GUI for anything a command can do**:
  `osascript -e 'set volume output volume 30'` beats hunting for a volume slider. It's a pty, so
  cwd/env/virtualenvs/history persist and interactive programs work (`python`, `ssh`, `vim`). A
  `running` indicator shows while a command holds the terminal; `^C` interrupts it. The screen
  shows its most recent output, with a button to expand the full scrollback — deliberately, so
  the newest output always survives an agent's page-extraction limit.

[`docs/agent-prompt.md`](docs/agent-prompt.md) has a prompt that drives all of this, and notes on
which agent failure modes each part of it exists to prevent.

## Sharing with a cloud agent (Cloudflare Tunnel)

webuse publishes itself through a **named Cloudflare tunnel**, so the public URL is a stable
hostname you own — it survives reboots and reconnects, and needs no inbound ports. One-time setup
(the `login` step opens a browser and asks which zone to authorize):

```sh
brew install cloudflared
cloudflared tunnel login
cloudflared tunnel create webuse-mbp
cloudflared tunnel route dns webuse-mbp tf-mbp.example.com   # creates the CNAME on your zone
```

Then tell webuse about it once — both settings persist to `~/.config/webuse/`, so later runs need
no flags:

```sh
uv run python main.py --hostname tf-mbp.example.com --tunnel-name webuse-mbp
uv run python main.py     # from now on, publishes to the same hostname
uv run python main.py --local   # serve on localhost only
```

The server prints `https://tf-mbp.example.com/?token=<token>` at startup — hand that to the agent.
The first tokened request sets a cookie; subsequent requests are authorized. While the tunnel is
up, **every** request needs the token, including local ones (the tunnel delivers to localhost, so
the localhost shortcut is switched off rather than trusted).

> ⚠️ **Treat the tunnel URL as root on your Mac.** Anyone with the URL + token can see your screen,
> click anything, and — via `/terminal` — run arbitrary shell commands as you. That is the point of
> the feature, but the token is the only thing between the internet and your account, and the
> hostname is now stable and guessable, so the token is doing all the work. Rotate it by deleting
> `~/.config/webuse/token`. For stronger protection, put Cloudflare Access in front of the hostname
> — but note that an identity policy will block browser-driving agents, which can't complete the
> login. Also: `/app/{pid}/dom` exposes the full text of the app's window — don't stream an app
> showing secrets.

## HTTP API (what an agent calls)

All routes accept the token via `?token=` (once, to set the cookie) when not on localhost.

| Method & path | Body / params | Purpose |
| --- | --- | --- |
| `GET /app/{handle}/dom` | — | Ghost DOM as JSON: `{window, nodes:[{id,role,label,value,x,y,w,h,…}]}`. |
| `GET /app/{handle}/events` | — | Server-Sent Events: pushes a fresh ghost DOM on every change (AXObserver-driven) — no polling. |
| `GET /app/{handle}/stream` | — | MJPEG stream of the window (works even when occluded or on another Space). |
| `GET /app/{handle}/stream_ws` | — | WebSocket stream: binary JPEG frames, pulled one at a time by the client, so latency stays bounded. |
| `GET /app/{handle}/ws` | — | WebSocket input: the same steps as `/api/action`, without a round trip each. |
| `GET /app/{handle}/windows` | — | Every window of the app: `{window_id, title, x, y, w, h}` — the ids that make a `pid.windowID` handle. |
| `GET /app/{handle}/menu` | — | The app's real menu bar as nested JSON; press an item by `id` with `action:"press"`. |
| `GET /app/{handle}/focus` | — | Where the app's keyboard focus sits: `{role, label, value, editable}`. |
| `POST /api/action` | `{pid, window_id?, action, id?, value?, x?, y?, activate?}` or `{pid, actions:[…]}` for a batch | Act on the app (see below). |
| `POST /api/activate` | `{pid, window_id?}` | Raise the app (and window) to the foreground. |
| `GET /api/apps` | `?installed=1` for the full catalog | Running apps (or all installed apps with launch info). |
| `POST /api/apps/open` | `{bundle_id?}` / `{name?}` / `{path?}` | Launch an app; returns its `pid`. |
| `GET /api/apps/events` | — | SSE: running-app launches/quits. |
| `GET /api/permissions` | — | `{trusted, screen_recording}` — what's been granted. |
| `POST /api/term/{sid}/input` | `{value?, key?, enter?, wait?}` | Write to a shell session (created on demand — `main` is the one the UI uses). Returns the rendered screen. |
| `GET /api/term/{sid}` | — | The session's transcript + `busy` / `alive`. |
| `GET /api/term/{sid}/events` | — | SSE: pushes the transcript as the shell writes. |
| `POST /api/term/{sid}/resize` | `{cols, rows}` | Resize the shell's grid. |
| `DELETE /api/term/{sid}` | — | Kill the shell (the name stays usable). |

**Actions** (`action` field). Pointer actions take either a ghost-element `id` (its centre becomes
the target) or window-relative `x`,`y`:

| Action | Fields | What it does |
| --- | --- | --- |
| `click` / `rightclick` / `dblclick` | `id` or `x,y` | A real HID click at that point. |
| `move` | `id` or `x,y` | Hover (some controls only reveal themselves on hover). |
| `scroll` | `x,y`, `dx,dy` | Wheel deltas at a point. |
| `drag` | `x,y` → `x2,y2` | Press, move, release as one gesture. |
| `type` | `value` | Literal text as real keystrokes. |
| `key` | `code`, optional `shift`/`ctrl`/`alt`/`meta`, `down` | One key by `KeyboardEvent.code` (`Enter`, `KeyA`); omit `down` for a tap. |
| `press` / `confirm` | `id` | `AXPress` / `AXConfirm` over Accessibility — how menu items are triggered, since they live outside the window. |
| `set_value` | `id`, `value` | Set a text field's whole value. |

Every step returns `point` (where it landed), and the response carries a fresh `dom`, so an agent
acts and observes in one round trip. A batch stops at the first failure and reports `failed_step`.

Element `id`s (`e1`, `e2`, …) are stable across snapshots for the life of the process, so an agent
can read the DOM once and reuse ids.

### `window.webuse` (injected JS)

Every app page exposes a small SDK for harnesses that can run JavaScript, so they can skip the
HTTP layer entirely. Each call maps to the same step the UI sends:

```js
await webuse.click("e12");           // element id, or webuse.click(x, y)
await webuse.type("hello");
await webuse.hotkey("cmd+s");
webuse.find("Save");                 // last-known nodes matching text
await webuse.dom();                  // fresh ghost DOM
```

Mutating calls resolve with the post-action ghost DOM.

## How it works

```
browser agent ─HTTP→ FastAPI (webuse/server.py)
                        ├─ ax.py        walk the Accessibility tree → nodes, stable element ids, AXPress/AXValue actions
                        ├─ dom.py       map AX roles → HTML tags
                        ├─ capture.py   ScreenCaptureKit → MJPEG (CGWindowList fallback; captures occluded / off-Space windows)
                        ├─ watch.py     per-app AXObserver on a background CFRunLoop → SSE push (instant DOM updates)
                        ├─ terminal.py  persistent pty shells; ANSI rendered to text by pyte
                        └─ apps.py      installed-app catalog, launch, live running-app SSE
```

The single-page frontend (React, built with Vite) lives in `webuse/frontend/` and
builds to `webuse/frontend/dist`, which the server serves statically with a
client-side-routing fallback.

The page layers an absolutely-positioned, transparent HTML element over each native control at its
real screen box. Reads come from the DOM/SSE; the video is just for the agent's screenshots and for
humans watching. Clicks route back to `AXPress` (semantic, background) or a pid-targeted `CGEvent`
(coordinate fallback).

### Cocoa threading rule

Every Cocoa entry point runs on a worker thread (the capture executor, anyio's endpoint pool, the
watchers), and those have no run loop — so **nothing autoreleased on them is ever freed** unless
you supply a pool. `cocoa.pooled` wraps each such entry point; add it to anything new that touches
AppKit/Quartz/Accessibility, and return plain Python across the boundary, never a bare
autoreleased object. Likewise use `cocoa.data_bytes` rather than `bytes(nsdata)`, which strands
its NSData under pyobjc 12.2.1. Skipping either is not a slow leak: one 8fps stream without a pool
leaked ~137 MB/s (a full-resolution CGImage per frame) and reached 98 GB before macOS killed it.

### Known limitations

- Heavily virtualized views (huge tables, some Electron apps) only expose their visible rows — the
  same limitation agents already tolerate on virtualized web pages.
- Ghost text fields are read-only mirrors by design: all text entry goes through real keystrokes,
  so writing to `.value` does nothing. Use the compose bar, or `action:"type"`.

## Development

```sh
uv run python -c "import webuse.server"   # import sanity
```

`main.py` is the entry point; the server assembles the routers in `webuse/server.py`.
