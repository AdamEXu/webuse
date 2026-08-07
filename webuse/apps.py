"""Installed-apps catalog + live running-apps feed.

Exposes a FastAPI router the integrator mounts with app.include_router(apps.router):
  GET  /api/apps?installed={0,1}  running list (default) or full installed catalog
  POST /api/apps/open             launch/reactivate an app, return its pid
  GET  /api/apps/events           SSE full running-app snapshot on every launch/terminate

Mirrors watch.py's threading/SSE registry (one module-global AppsWatcher, no per-pid)
and ax.list_apps()'s dict shape / activationPolicy==0 filter. Polling (~1.5s) is the
correctness backbone; NSWorkspace launch/terminate notifications are a latency
accelerator that shortcut the poll sleep, exactly like watch.py's safety-net stance.
No per-app/host/bundle special-casing — the installed catalog is a generic top-level
scan of the Launchpad-visible /Applications-family directories (no Spotlight/mdfind,
which surfaces hidden framework/debug bundles Launchpad never shows).
"""

import asyncio
import glob
import json
import os
import subprocess
import sys
import threading
import time

import objc
from AppKit import (
    NSBitmapImageFileTypePNG,
    NSBitmapImageRep,
    NSBundle,
    NSCalibratedRGBColorSpace,
    NSCompositingOperationSourceOver,
    NSGraphicsContext,
    NSMakeRect,
    NSRunningApplication,
    NSWorkspace,
    NSZeroRect,
)

CATALOG_TTL_S = 60.0  # per-dir glob + NSBundle reads is the cost; cache the catalog
POLL_S = 1.5          # correctness backbone: catch every launch/terminate within this
OPEN_POLL_S = 0.2     # launch-and-poll cadence
OPEN_TIMEOUT_S = 6.0

SCAN_DIRS = (
    "/Applications",
    "/Applications/Utilities",
    "/System/Applications",
    "/System/Applications/Utilities",
    os.path.expanduser("~/Applications"),
)


# -- enumeration -------------------------------------------------------------
#
# NSWorkspace.runningApplications() is KVO-fed off the main run loop, so on a
# background thread (FastAPI's threadpool, the watcher thread) it returns a
# FROZEN snapshot once primed — a run-loop pump won't refresh it without a main
# run loop, which headless uvicorn doesn't run. A fresh child process, however,
# reads the current list on first access. So the authoritative running list is
# gathered by spawning a short-lived interpreter (~100ms, fine at 1.5s cadence).
# runningApplicationsWithBundleIdentifier_ IS fresh on any thread, so _open_app
# uses that directly (see below). Both keep the activationPolicy==0 filter and
# the {pid,name,bundle_id} shape from ax.list_apps().

_ENUM_SRC = (
    "import json;from AppKit import NSWorkspace;"
    "print(json.dumps([[int(a.processIdentifier()),str(a.localizedName() or '?'),"
    "(str(a.bundleIdentifier()) if a.bundleIdentifier() else None)] "
    "for a in NSWorkspace.sharedWorkspace().runningApplications() "
    "if a.activationPolicy()==0]))"
)

from .cocoa import data_bytes, pooled


@pooled
def _running_apps() -> list[dict]:
    """Live running regular-app list (fresh on any thread) — same shape/order the
    SSE feed pushes. Falls back to the in-process (possibly stale) list if the
    helper interpreter can't run."""
    rows = None
    try:
        r = subprocess.run(
            [sys.executable, "-c", _ENUM_SRC], capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0 and r.stdout.strip():
            rows = json.loads(r.stdout)
    except Exception:
        rows = None
    if rows is None:  # fallback: in-process (may be frozen on a bg thread)
        rows = [
            [int(a.processIdentifier()), str(a.localizedName() or "?"),
             str(a.bundleIdentifier()) if a.bundleIdentifier() else None]
            for a in NSWorkspace.sharedWorkspace().runningApplications()
            if a.activationPolicy() == 0
        ]
    apps = [{"pid": p, "name": n, "bundle_id": b} for p, n, b in rows]
    return sorted(apps, key=lambda a: a["name"].lower())


def _pids_by_bundle() -> dict[str, int]:
    """{bundle_id -> pid} for running regular apps (last one wins; fine for flags)."""
    out: dict[str, int] = {}
    for a in _running_apps():
        if a["bundle_id"]:
            out[a["bundle_id"]] = a["pid"]
    return out


def _running_pids_for_bundle(bid: str) -> set[int]:
    """Fresh pid set for one bundle id — runningApplicationsWithBundleIdentifier_
    queries live on any thread (unlike runningApplications()), so no subprocess."""
    return {
        int(a.processIdentifier())
        for a in NSRunningApplication.runningApplicationsWithBundleIdentifier_(bid)
        if a.activationPolicy() == 0
    }


def _scan_bundles() -> list[str]:
    paths: list[str] = []
    for d in SCAN_DIRS:
        paths.extend(glob.glob(os.path.join(d, "*.app")))
    return paths


def _build_catalog() -> list[dict]:
    """Full installed catalog sans running/pid flags (those recomputed per call)."""
    seen: set[str] = set()
    apps: list[dict] = []
    for path in _scan_bundles():
        try:
            real = os.path.realpath(path)
        except OSError:
            continue
        if real in seen or not path.endswith(".app"):
            continue
        seen.add(real)
        bundle = NSBundle.bundleWithPath_(path)
        bid = bundle.bundleIdentifier() if bundle else None
        apps.append(
            {
                "name": os.path.basename(path)[:-4],
                "bundle_id": str(bid) if bid else None,
                "path": path,
            }
        )
    return sorted(apps, key=lambda a: a["name"].lower())


_catalog_cache: list[dict] | None = None
_catalog_at = 0.0
_catalog_lock = threading.Lock()


@pooled
def _installed_apps() -> list[dict]:
    """Installed catalog (TTL-cached) with fresh running/pid flags per call."""
    global _catalog_cache, _catalog_at
    with _catalog_lock:
        if _catalog_cache is None or time.monotonic() - _catalog_at > CATALOG_TTL_S:
            _catalog_cache = _build_catalog()
            _catalog_at = time.monotonic()
        catalog = _catalog_cache
    pids = _pids_by_bundle()
    out = []
    for a in catalog:
        pid = pids.get(a["bundle_id"]) if a["bundle_id"] else None
        out.append({**a, "running": pid is not None, "pid": pid})
    return out


# -- icons -------------------------------------------------------------------

_ICON_SIZE = 36
_icon_cache: dict[int, bytes | None] = {}  # pid -> png (an app's icon is stable)
_icon_lock = threading.Lock()


def icon_png(pid: int, size: int = _ICON_SIZE) -> bytes | None:
    """The running app's icon as a small PNG, or None. Drawn into a bitmap-backed
    graphics context (no lockFocus), so it's safe off the main thread."""
    with _icon_lock:
        if pid in _icon_cache:
            return _icon_cache[pid]
    png = _render_icon(pid, size)
    with _icon_lock:
        _icon_cache[pid] = png
    return png


@pooled
def _render_icon(pid: int, size: int) -> bytes | None:
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    img = app.icon() if app is not None else None
    if img is None:
        return None
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size, size, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0
    )
    if rep is None:
        return None
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    if ctx is None:
        return None
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(ctx)
    img.drawInRect_fromRect_operation_fraction_(
        NSMakeRect(0, 0, size, size), NSZeroRect, NSCompositingOperationSourceOver, 1.0
    )
    NSGraphicsContext.restoreGraphicsState()
    data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
    return data_bytes(data) if data else None


# -- open --------------------------------------------------------------------

def _resolve_bundle_id(bundle_id=None, path=None, name=None) -> str | None:
    if bundle_id:
        return bundle_id
    if path:
        b = NSBundle.bundleWithPath_(path)
        bid = b.bundleIdentifier() if b else None
        return str(bid) if bid else None
    if name:
        for a in _installed_apps():
            if a["name"].lower() == name.lower():
                return a["bundle_id"]
    return None


@pooled
def _open_app(bundle_id=None, path=None, name=None) -> dict:
    bid = _resolve_bundle_id(bundle_id, path, name)
    if not bid and not (path or name):
        return {"ok": False, "error": "provide bundle_id, path, or name"}

    before = _running_pids_for_bundle(bid) if bid else {a["pid"] for a in _running_apps()}

    # Launch via `open` (needs no run loop, reliably yields a pid on poll).
    if bid:
        cmd = ["open", "-b", bid]
    elif path:
        cmd = ["open", path]
    else:
        cmd = ["open", "-a", name]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except Exception as e:
        return {"ok": False, "error": f"launch failed: {e}"}
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or "launch failed").strip()}

    deadline = time.monotonic() + OPEN_TIMEOUT_S
    while time.monotonic() < deadline:
        if bid:
            matches = _running_pids_for_bundle(bid)
            new = matches - before
            if new:
                return {"ok": True, "pid": sorted(new)[0], "bundle_id": bid,
                        "already_running": False}
            if matches:  # singleton app just reactivated
                return {"ok": True, "pid": sorted(matches)[0], "bundle_id": bid,
                        "already_running": True}
        else:  # bundle id never resolved: match any newly-appeared regular app
            for a in _running_apps():
                if a["pid"] not in before:
                    return {"ok": True, "pid": a["pid"], "bundle_id": a["bundle_id"],
                            "already_running": False}
        time.sleep(OPEN_POLL_S)
    return {"ok": False, "error": "app did not appear after launch"}


# -- live updates ------------------------------------------------------------

_watcher: "AppsWatcher | None" = None
_watcher_lock = threading.Lock()


def get_watcher(loop: asyncio.AbstractEventLoop) -> "AppsWatcher":
    global _watcher
    with _watcher_lock:
        if _watcher is None:
            _watcher = AppsWatcher(loop)
        return _watcher


class AppsWatcher:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._subs: set[asyncio.Queue] = set()
        self.current: str | None = None  # last json payload pushed
        self._observers = []  # kept alive; run-loop sources reference them
        threading.Thread(target=self._run, name="appswatch", daemon=True).start()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    # -- watcher thread ------------------------------------------------------

    def _run(self):
        self._attach_observers()
        last: set[tuple[int, str | None]] = set()
        while True:
            # Poll is the correctness backbone; the observer (if it ever fires)
            # sets _wake to shortcut the sleep. No CFRunLoop pump: NSWorkspace
            # block-observers add no run-loop source, so pumping would busy-spin.
            self._wake.wait(POLL_S)
            self._wake.clear()
            with objc.autorelease_pool():
                apps = _running_apps()
            key = {(a["pid"], a["bundle_id"]) for a in apps}
            if key == last:
                continue
            last = key
            self._push(json.dumps({"apps": apps}))

    def _attach_observers(self):
        """Register launch/terminate notifications as a latency accelerator.
        Non-fatal on failure (and delivery needs a main run loop, so headless it
        may never fire) — the poll catches every change regardless."""
        try:
            nc = NSWorkspace.sharedWorkspace().notificationCenter()

            def on_change(note):
                self._wake.set()

            for name in ("NSWorkspaceDidLaunchApplicationNotification",
                         "NSWorkspaceDidTerminateApplicationNotification"):
                obs = nc.addObserverForName_object_queue_usingBlock_(
                    name, None, None, on_change
                )
                self._observers.append(obs)
        except Exception:
            pass

    def _push(self, payload: str):
        self.current = payload
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            self.loop.call_soon_threadsafe(q.put_nowait, payload)


# -- routes ------------------------------------------------------------------

import fastapi  # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

router = fastapi.APIRouter()


@router.get("/api/apps")
def get_apps(installed: int = 0):
    return {"apps": _installed_apps() if installed else _running_apps()}


class OpenReq(BaseModel):
    bundle_id: str | None = None
    path: str | None = None
    name: str | None = None


@router.post("/api/apps/open")
def open_app(req: OpenReq):
    result = _open_app(req.bundle_id, req.path, req.name)
    if not result.get("ok"):
        return JSONResponse(result, status_code=404)
    return result


@router.get("/api/apps/events")
async def apps_events():
    watcher = get_watcher(asyncio.get_running_loop())

    async def gen():
        q = watcher.subscribe()
        try:
            payload = watcher.current
            if payload is None:
                payload = json.dumps({"apps": _running_apps()})
            yield f"data: {payload}\n\n"
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"data: {item}\n\n"
        finally:
            watcher.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream")
