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
| `/app/{pid}` | A single app rendered as a ghost DOM over its live screen stream, with a compose bar and a terminal stacked below it. |
| `/app/{pid}?debug=1` | Same, with every ghost element outlined — a "show layout bounds" for macOS. |
| `/terminal` | A real zsh session on this Mac, listed in the sidebar like an app. Same session as the panel under every app page. |

### Typing, keys, and the terminal

Ghost text fields are non-editable mirrors, and many web agents can only fill text inputs and
click — they can't press arbitrary keys or call an HTTP API. So the chrome provides both as
plain DOM:

- **Compose bar** (under the app) — a real `<input>`; type text and click **Send** (or **Send ↵**)
  and it's delivered to the app as actual HID keystrokes. It shows which control currently has the
  app's keyboard focus. Beside it, buttons for `Enter` `Tab` `Esc` arrows `⌘A` `⌘C` `⌘V` — the keys
  typing alone can't express.
- **Terminal** — type a command, click **Run**, read the output that appears above the box (it
  updates by itself over SSE). **Prefer it over driving the GUI for anything a command can do**:
  `osascript -e 'set volume output volume 30'` beats hunting for a volume slider. It's a pty, so
  cwd/env/virtualenvs/history persist and interactive programs work (`python`, `ssh`, `vim`). A
  `running` indicator shows while a command holds the terminal; `^C` interrupts it.

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
| `GET /app/{pid}/dom` | — | Ghost DOM as JSON: `{window, nodes:[{id,role,label,value,x,y,w,h,…}]}`. |
| `GET /app/{pid}/events` | — | Server-Sent Events: pushes a fresh ghost DOM on every change (AXObserver-driven) — no polling. |
| `GET /app/{pid}/stream` | — | MJPEG stream of the app window (works even when occluded or on another Space). |
| `POST /api/action` | `{pid, action, id?, value?, x?, y?, activate?}` or `{pid, actions:[…]}` for a batch | Act on the app (see below). |
| `GET /api/apps` | `?installed=1` for the full catalog | Running apps (or all installed apps with launch info). |
| `POST /api/apps/open` | `{bundle_id?}` / `{name?}` / `{path?}` | Launch an app; returns its `pid`. |
| `GET /api/apps/events` | — | SSE: running-app launches/quits. |
| `GET /app/{pid}/focus` | — | Where the app's keyboard focus sits: `{role, label, value, editable}`. |
| `POST /api/term/{sid}/input` | `{value?, key?, enter?, wait?}` | Write to a shell session (created on demand — `main` is the one the UI uses). Returns the rendered screen. |
| `GET /api/term/{sid}` | — | The session's transcript + `busy` / `alive`. |
| `GET /api/term/{sid}/events` | — | SSE: pushes the transcript as the shell writes. |
| `POST /api/term/{sid}/resize` | `{cols, rows}` | Resize the shell's grid. |
| `DELETE /api/term/{sid}` | — | Kill the shell (the name stays usable). |

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
