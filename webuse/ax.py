"""macOS Accessibility tree access: snapshots, element registry, actions."""

import threading
import time

from AppKit import NSRunningApplication, NSWorkspace
from ApplicationServices import (
    AXIsProcessTrusted,
    AXUIElementCopyActionNames,
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementPerformAction,
    AXUIElementSetAttributeValue,
    AXValueGetValue,
)
from Quartz import (
    CGEventCreate,
    CGEventCreateMouseEvent,
    CGEventGetLocation,
    CGPreflightScreenCaptureAccess,
    CGRequestScreenCaptureAccess,
    CGEventPost,
    CGEventSetIntegerValueField,
    CGWarpMouseCursorPosition,
    kCGEventLeftMouseDown,
    kCGEventLeftMouseUp,
    kCGHIDEventTap,
    kCGMouseButtonLeft,
    kCGMouseEventClickState,
)

kAXValueCGPointType = 1
kAXValueCGSizeType = 2

MAX_NODES = 4000
# Above this many children, prefer a container's visible-subset attribute
# (AXVisibleRows/Cells/Children) over walking every child.
VISIBLE_SUBSET_MIN = 40
MAX_TEXT = 50_000
WALK_BUDGET_S = 4.0

# Per-pid state. AXUIElement supports CFEqual/CFHash across snapshots, so the
# same on-screen element keeps the same id for the lifetime of the process —
# agents can cache element references safely. Snapshots run concurrently from the
# watcher thread and the endpoint threadpool, so all id allocation and lookups go
# through _id_lock; ids not seen for ID_TTL_S are pruned so long-lived dynamic
# apps don't grow the maps without bound (recently-seen ids survive refreshes so
# an agent-held id stays valid for a while after its element scrolls away).
_id_lock = threading.Lock()
_counters: dict[int, int] = {}
_ids_by_el: dict[int, dict] = {}  # pid -> {AXUIElement: id}
_els_by_id: dict[int, dict] = {}  # pid -> {id: AXUIElement}
_seen_at: dict[int, dict] = {}  # pid -> {id: monotonic time last walked}
_last_window: dict[int, dict] = {}
ID_TTL_S = 120.0


def _intern(pid, ids_by_el, els_by_id, seen_at, el, ts) -> str:
    with _id_lock:
        node_id = ids_by_el.get(el)
        if node_id is None:
            counter = _counters.get(pid, 0) + 1
            _counters[pid] = counter
            node_id = f"e{counter}"
            ids_by_el[el] = node_id
            els_by_id[node_id] = el
        seen_at[node_id] = ts
        return node_id


def register(pid: int, el) -> str:
    """Intern an element the window walk never reaches (e.g. a menu item) so
    perform() can resolve it by id. Refreshes its TTL, so a client that re-reads
    the menu keeps the ids alive."""
    with _id_lock:
        ids_by_el = _ids_by_el.setdefault(pid, {})
        els_by_id = _els_by_id.setdefault(pid, {})
        seen_at = _seen_at.setdefault(pid, {})
    return _intern(pid, ids_by_el, els_by_id, seen_at, el, time.monotonic())


def _prune(pid, ids_by_el, els_by_id, seen_at, now) -> None:
    with _id_lock:
        dead = [i for i, t in seen_at.items() if now - t > ID_TTL_S]
        for i in dead:
            el = els_by_id.pop(i, None)
            seen_at.pop(i, None)
            if el is not None:
                ids_by_el.pop(el, None)


def is_running(pid: int) -> bool:
    return NSRunningApplication.runningApplicationWithProcessIdentifier_(pid) is not None


def forget(pid: int) -> None:
    """Drop all per-pid state (call when a watched app has exited)."""
    with _id_lock:
        for d in (_counters, _ids_by_el, _els_by_id, _seen_at, _last_window):
            d.pop(pid, None)


def trusted() -> bool:
    return bool(AXIsProcessTrusted())


def screen_recording() -> bool:
    return bool(CGPreflightScreenCaptureAccess())


def request_permissions() -> None:
    """Trigger the system Screen Recording prompt (first time only).
    Accessibility has no request API — users must grant it in System Settings."""
    CGRequestScreenCaptureAccess()


def list_apps() -> list[dict]:
    apps = []
    for a in NSWorkspace.sharedWorkspace().runningApplications():
        if a.activationPolicy() == 0:  # regular apps (Dock icon)
            apps.append(
                {"pid": int(a.processIdentifier()), "name": str(a.localizedName() or "?")}
            )
    return sorted(apps, key=lambda a: a["name"].lower())


def app_name(pid: int) -> str:
    a = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    return str(a.localizedName()) if a else str(pid)


def _attr(el, name):
    err, val = AXUIElementCopyAttributeValue(el, name, None)
    return val if err == 0 else None


def _geom(el):
    pos, size = _attr(el, "AXPosition"), _attr(el, "AXSize")
    if pos is None or size is None:
        return None
    ok1, p = AXValueGetValue(pos, kAXValueCGPointType, None)
    ok2, s = AXValueGetValue(size, kAXValueCGSizeType, None)
    if not (ok1 and ok2):
        return None
    return (float(p.x), float(p.y), float(s.width), float(s.height))


def _far_offscreen(geom, wx, wy, ww, wh) -> bool:
    """True if `geom` lies more than one window-dimension beyond the visible rect.
    A tall scroll region (e.g. a playlist with thousands of rows) buries on-screen
    chrome like a transport bar under a huge off-screen subtree; walking it all
    exhausts the node/time budget before the chrome is reached. Pruning subtrees
    this far out keeps the budget for what's visible (plus ~one screen of
    scroll-ahead in every direction). Case-agnostic: keyed only on geometry."""
    gx, gy, gw, gh = geom
    return (
        gx + gw < wx - ww or gx > wx + 2 * ww
        or gy + gh < wy - wh or gy > wy + 2 * wh
    )


def _coerce(v):
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v[:MAX_TEXT]
    if isinstance(v, (int, float)):
        return v
    s = str(v)
    return s if len(s) <= 200 else None


def snapshot(pid: int) -> dict:
    """Walk the focused/main window of `pid`. Returns window info + raw nodes,
    and registers every element so actions can resolve ids later."""
    app_el = AXUIElementCreateApplication(pid)
    win = _attr(app_el, "AXFocusedWindow") or _attr(app_el, "AXMainWindow")
    if win is None:
        wins = _attr(app_el, "AXWindows")
        win = wins[0] if wins else None
    if win is None:
        raise LookupError(f"no accessible window for pid {pid}")

    wx, wy, ww, wh = _geom(win) or (0.0, 0.0, 800.0, 600.0)
    window = {
        "pid": pid,
        "app": app_name(pid),
        "title": _coerce(_attr(win, "AXTitle")) or "",
        "x": wx, "y": wy, "w": ww, "h": wh,
    }
    _last_window[pid] = window

    now = time.monotonic()
    with _id_lock:
        ids_by_el = _ids_by_el.setdefault(pid, {})
        els_by_id = _els_by_id.setdefault(pid, {})
        seen_at = _seen_at.setdefault(pid, {})
    nodes: list[dict] = []
    deadline = now + WALK_BUDGET_S
    truncated = False

    stack = list(_attr(win, "AXChildren") or [])
    stack.reverse()
    while stack:
        if len(nodes) >= MAX_NODES or time.monotonic() > deadline:
            truncated = True
            break
        el = stack.pop()
        role = _attr(el, "AXRole")
        if role is None:
            continue
        geom = _geom(el)
        # Skip elements (and their subtrees) buried far outside the visible window:
        # a tall scroll region's off-screen rows would otherwise drain the walk
        # budget before on-screen chrome (e.g. a transport bar late in child order)
        # is reached. On-screen and near-fold content is always kept.
        if geom is not None and _far_offscreen(geom, wx, wy, ww, wh):
            continue
        # Prefer a container's on-screen subset over all its children. Any
        # heavily populated container that exposes a visible-subset attribute
        # (tables, outlines, lists, collection/grid views, …) is walked by that
        # subset only — the same signal a browser agent sees for a virtualized
        # list — so hundreds of scrolled-off rows never drain the walk budget
        # before later on-screen controls (e.g. an app's transport bar). Keyed
        # on child count, not app or role, so it stays general. Complements
        # _far_offscreen, which can't help here: it must enumerate every row
        # (an IPC each) before pruning, whereas this never enumerates them.
        children = _attr(el, "AXChildren")
        if children and len(children) > VISIBLE_SUBSET_MIN:
            visible = (
                _attr(el, "AXVisibleRows")
                or _attr(el, "AXVisibleCells")
                or _attr(el, "AXVisibleChildren")
            )
            if visible:
                children = visible
        if children:
            stack.extend(reversed(list(children)))
        if geom is None:
            continue
        err, action_names = AXUIElementCopyActionNames(el, None)
        actions = [str(a) for a in action_names] if err == 0 and action_names else []
        node_id = _intern(pid, ids_by_el, els_by_id, seen_at, el, now)
        title = _coerce(_attr(el, "AXTitle"))
        desc = _coerce(_attr(el, "AXDescription"))
        nodes.append(
            {
                "id": node_id,
                "role": str(role),
                "subrole": _coerce(_attr(el, "AXSubrole")),
                "label": title or desc or "",
                "value": _coerce(_attr(el, "AXValue")),
                "placeholder": _coerce(_attr(el, "AXPlaceholderValue")),
                "enabled": _attr(el, "AXEnabled") in (None, True, 1),
                "actions": actions,
                "x": geom[0], "y": geom[1], "w": geom[2], "h": geom[3],
            }
        )

    _prune(pid, ids_by_el, els_by_id, seen_at, now)
    return {"window": window, "nodes": nodes, "truncated": truncated}


def last_window(pid: int) -> dict | None:
    return _last_window.get(pid)


def _lookup(pid: int, el_id: str):
    with _id_lock:
        return _els_by_id.get(pid, {}).get(el_id)


def perform(pid: int, el_id: str, action: str, value=None) -> dict:
    el = _lookup(pid, el_id)
    if el is None:
        return {"ok": False, "error": f"unknown element id {el_id} (stale snapshot?)"}
    if action == "press":
        err = AXUIElementPerformAction(el, "AXPress")
    elif action == "confirm":
        err = AXUIElementPerformAction(el, "AXConfirm")
    elif action == "set_value":
        AXUIElementSetAttributeValue(el, "AXFocused", True)
        err = AXUIElementSetAttributeValue(el, "AXValue", "" if value is None else str(value))
    else:
        return {"ok": False, "error": f"unknown action {action!r}"}
    result = {"ok": err == 0, "axerror": int(err)}
    geom, win = _geom(el), _last_window.get(pid)
    if geom and win:  # post-action element center, window-relative (for cursors/agents)
        result["point"] = [
            round(geom[0] + geom[2] / 2 - win["x"], 1),
            round(geom[1] + geom[3] / 2 - win["y"], 1),
        ]
    return result


def click(pid: int, x: float, y: float) -> dict:
    """Click at global screen coordinates (points) — the last-resort path for
    bare-pixel targets with no addressable element.

    CGEventPostToPid delivers to a pid without moving the pointer, but most apps
    silently drop synthesized coordinate clicks that carry no real HID telemetry
    — verified a no-op even on Calculator. The only reliable delivery is to
    activate the target app and post a genuine HID-tap click, so that's what we
    do. To keep it from hijacking the user, we snapshot the real pointer first
    and warp it back afterward, so it just flicks to the target and returns.
    Addressable controls go through AXPress (background, no focus/pointer
    change) and never reach here."""
    saved = CGEventGetLocation(CGEventCreate(None))  # real pointer, to restore
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app:
        app.activateWithOptions_(1 << 1)  # NSApplicationActivateIgnoringOtherApps
        time.sleep(0.15)
    for etype in (kCGEventLeftMouseDown, kCGEventLeftMouseUp):
        ev = CGEventCreateMouseEvent(None, etype, (x, y), kCGMouseButtonLeft)
        CGEventSetIntegerValueField(ev, kCGMouseEventClickState, 1)  # some apps ignore clickState 0
        CGEventPost(kCGHIDEventTap, ev)
        time.sleep(0.04)
    CGWarpMouseCursorPosition(saved)  # put the user's pointer back where it was
    return {"ok": True, "strategy": "focus+hid"}
