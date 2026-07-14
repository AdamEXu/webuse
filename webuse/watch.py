"""Per-pid DOM watchers: AXObserver notifications -> fresh ghost-DOM pushes.

Each watcher owns a daemon thread running a CFRunLoop with an AXObserver
attached, re-walks the AX tree when notified (or kicked after an action,
or every 2s as a safety net), and publishes changed payloads to subscriber
asyncio.Queues as (event, json) tuples — event is None for a normal DOM
update, "gone" when the app/window disappeared.
"""

import asyncio
import json
import threading
import time

import objc
from ApplicationServices import (
    AXObserverAddNotification,
    AXObserverCreate,
    AXObserverGetRunLoopSource,
    AXObserverRemoveNotification,
    AXUIElementCreateApplication,
    kAXErrorCannotComplete,
)
from Quartz import (
    CFRunLoopAddSource,
    CFRunLoopGetCurrent,
    CFRunLoopRemoveSource,
    CFRunLoopRunInMode,
    kCFRunLoopDefaultMode,
)

from . import ax, dom

NOTIFICATIONS = (
    "AXValueChanged",
    "AXUIElementDestroyed",
    "AXFocusedUIElementChanged",
    "AXWindowMoved",
    "AXWindowResized",
    "AXWindowCreated",
    "AXTitleChanged",
)
# AXValueChanged registered on the app element misses value changes on
# descendants, so we also (un)subscribe it on the focused element as focus moves.
FOCUSED_NOTIFICATIONS = ("AXValueChanged", "AXUIElementDestroyed")
COALESCE_S = 0.04  # let notification bursts settle before re-walking
SAFETY_NET_S = 2.0  # re-walk at least this often even without notifications
IDLE_GRACE_S = 30.0  # retire a watcher this long after its last subscriber leaves

_watchers: dict[int, "DomWatcher"] = {}
_registry_lock = threading.Lock()


def subscribe(
    pid: int, loop: asyncio.AbstractEventLoop
) -> tuple["DomWatcher", asyncio.Queue]:
    """Get-or-create the watcher for `pid` and atomically register a subscriber,
    so a watcher can't be retired for idleness in the gap between the two."""
    q: asyncio.Queue = asyncio.Queue()
    with _registry_lock:
        w = _watchers.get(pid)
        if w is None or w._stop.is_set():
            w = _watchers[pid] = DomWatcher(pid, loop)
        with w._lock:
            w._subs.add(q)
    return w, q


def kick(pid: int) -> None:
    """Nudge the watcher for `pid`, if one exists (no-op otherwise)."""
    with _registry_lock:
        w = _watchers.get(pid)
    if w:
        w.kick()


class DomWatcher:
    def __init__(self, pid: int, loop: asyncio.AbstractEventLoop):
        self.pid = pid
        self.loop = loop
        self._dirty = threading.Event()
        self._dirty.set()  # walk immediately on start
        self._stop = threading.Event()
        self._gone = False  # set by _walk when the process itself has exited
        self._lock = threading.Lock()
        self._subs: set[asyncio.Queue] = set()
        self.current: tuple[str | None, str] | None = None  # last (event, payload)
        self._observer = None  # kept alive; run-loop source references it
        self._app_el = None
        self._focused_el = None  # element the focused-value notifications are on
        threading.Thread(target=self._run, name=f"domwatch-{pid}", daemon=True).start()

    def kick(self) -> None:
        self._dirty.set()

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def _retire(self, force: bool = False) -> bool:
        """Remove from the registry and signal the thread to exit. Skipped (unless
        `force`) if a subscriber slipped in — both this and subscribe() re-check
        under _registry_lock then self._lock, so the last one wins consistently."""
        with _registry_lock:
            with self._lock:
                if self._subs and not force:
                    return False
            if _watchers.get(self.pid) is self:
                del _watchers[self.pid]
            self._stop.set()
            return True

    # -- watcher thread ------------------------------------------------------

    def _run(self):
        with objc.autorelease_pool():
            self._attach_observer()
            last_walk = 0.0
            idle_since = None
            while not self._stop.is_set():
                CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.05, True)
                with self._lock:
                    have_subs = bool(self._subs)
                # No subscribers for IDLE_GRACE_S, or the process exited: retire so
                # we don't leak a spinning run-loop thread per pid ever viewed.
                if have_subs:
                    idle_since = None
                elif idle_since is None:
                    idle_since = time.monotonic()
                elif time.monotonic() - idle_since > IDLE_GRACE_S and self._retire():
                    break
                if self._gone and self._retire(force=True):
                    break
                now = time.monotonic()
                if not self._dirty.is_set() and now - last_walk < SAFETY_NET_S:
                    continue
                time.sleep(COALESCE_S)
                CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0, True)  # drain burst
                self._dirty.clear()
                last_walk = time.monotonic()
                self._walk()
            self._detach_observer()
        ax.forget(self.pid)

    def _attach_observer(self):
        """Register AX notifications on the app element. Failure is non-fatal:
        the safety-net re-walk keeps the DOM fresh (just slower)."""

        @objc.callbackFor(AXObserverCreate)
        def on_notify(observer, element, notification, refcon):
            self._dirty.set()
            if str(notification) == "AXFocusedUIElementChanged":
                self._track_focused()

        err, obs = AXObserverCreate(self.pid, on_notify, None)
        if err != 0 or obs is None:
            return
        self._app_el = AXUIElementCreateApplication(self.pid)
        for note in NOTIFICATIONS:
            for _ in range(5):  # transient kAXErrorCannotComplete on app launch
                if AXObserverAddNotification(obs, self._app_el, note, None) != kAXErrorCannotComplete:
                    break
                time.sleep(0.1)
        CFRunLoopAddSource(
            CFRunLoopGetCurrent(), AXObserverGetRunLoopSource(obs), kCFRunLoopDefaultMode
        )
        self._observer = obs
        self._track_focused()  # cover the initially-focused element too

    def _track_focused(self):
        """Move the value/destroyed notifications onto the currently-focused
        element, so a focused control's live value changes wake us (the app-level
        AXValueChanged doesn't fire for descendants). Runs on the watcher thread."""
        if self._observer is None or self._app_el is None:
            return
        focused = ax._attr(self._app_el, "AXFocusedUIElement")
        if focused is None or focused == self._focused_el:
            return
        if self._focused_el is not None:
            for note in FOCUSED_NOTIFICATIONS:
                AXObserverRemoveNotification(self._observer, self._focused_el, note)
        for note in FOCUSED_NOTIFICATIONS:
            AXObserverAddNotification(self._observer, focused, note, None)
        self._focused_el = focused

    def _detach_observer(self):
        if self._observer is not None:
            CFRunLoopRemoveSource(
                CFRunLoopGetCurrent(),
                AXObserverGetRunLoopSource(self._observer),
                kCFRunLoopDefaultMode,
            )
            self._observer = None

    def _walk(self):
        try:
            snap = ax.snapshot(self.pid)
            item = (
                None,
                json.dumps(
                    {
                        "window": snap["window"],
                        "truncated": snap["truncated"],
                        "nodes": dom.build_nodes(snap),
                    }
                ),
            )
        except LookupError as e:
            item = ("gone", json.dumps({"error": str(e)}))
            if not ax.is_running(self.pid):  # process exited: stop watching for good
                self._gone = True
        except Exception as e:  # never kill the watcher thread
            item = ("gone", json.dumps({"error": f"snapshot failed: {e}"}))
        if item == self.current:
            return
        self.current = item
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            self.loop.call_soon_threadsafe(q.put_nowait, item)
