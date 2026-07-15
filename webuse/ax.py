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
    CGPreflightScreenCaptureAccess,
    CGRequestScreenCaptureAccess,
)

from . import windows

kAXValueCGPointType = 1
kAXValueCGSizeType = 2

MAX_NODES = 4000
# Above this many children, prefer a container's visible-subset attribute
# (AXVisibleRows/Cells/Children) over walking every child.
VISIBLE_SUBSET_MIN = 40
MAX_TEXT = 50_000
WALK_BUDGET_S = 4.0

# Per-(pid, window_id) state, so element ids never collide across an app's
# windows. The key is (pid, window_id|None); None is the focused/main-window
# precedence (back-compat) and also the app-level bucket for menu items, which
# belong to no window and are reachable from every window via _lookup's fallback.
# AXUIElement supports CFEqual/CFHash across snapshots, so the same on-screen
# element keeps the same id for the lifetime of the process — agents can cache
# element references safely. Snapshots run concurrently from the watcher thread
# and the endpoint threadpool, so all id allocation and lookups go through
# _id_lock; ids not seen for ID_TTL_S are pruned so long-lived dynamic apps don't
# grow the maps without bound (recently-seen ids survive refreshes so an
# agent-held id stays valid for a while after its element scrolls away).
_id_lock = threading.Lock()
_counters: dict = {}  # key -> int
_ids_by_el: dict = {}  # key -> {AXUIElement: id}
_els_by_id: dict = {}  # key -> {id: AXUIElement}
_seen_at: dict = {}  # key -> {id: monotonic time last walked}
_last_window: dict = {}  # key -> window dict
ID_TTL_S = 120.0


def _intern(key, ids_by_el, els_by_id, seen_at, el, ts, prefix="e") -> str:
    with _id_lock:
        node_id = ids_by_el.get(el)
        if node_id is None:
            counter = _counters.get(key, 0) + 1
            _counters[key] = counter
            node_id = f"{prefix}{counter}"
            ids_by_el[el] = node_id
            els_by_id[node_id] = el
        seen_at[node_id] = ts
        return node_id


def register(pid: int, el) -> str:
    """Intern an element the window walk never reaches (e.g. a menu item) so
    perform() can resolve it by id. Menu items belong to no window, so they go in
    the app-level (pid, None) bucket, which _lookup consults from any window.
    Refreshes its TTL, so a client that re-reads the menu keeps the ids alive.

    Menu ids get an 'm' prefix so they can't collide with a window walk's 'e' ids:
    on a pid.winid page the window lives in the (pid, window_id) bucket while the
    menu lives in (pid, None), each with its own counter starting at 1 — without a
    distinct prefix, menu 'e40' and window 'e40' would be indistinguishable strings
    and _lookup (window bucket first) would resolve the menu press to the wrong
    control."""
    key = (pid, None)
    with _id_lock:
        ids_by_el = _ids_by_el.setdefault(key, {})
        els_by_id = _els_by_id.setdefault(key, {})
        seen_at = _seen_at.setdefault(key, {})
    return _intern(key, ids_by_el, els_by_id, seen_at, el, time.monotonic(), prefix="m")


def _prune(ids_by_el, els_by_id, seen_at, now) -> None:
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
    """Drop all state for every window of a pid (call when a watched app exited)."""
    with _id_lock:
        for d in (_counters, _ids_by_el, _els_by_id, _seen_at, _last_window):
            for k in [k for k in d if k[0] == pid]:
                d.pop(k, None)


def forget_window(pid: int, window_id: int | None) -> None:
    """Drop state for a single (pid, window_id) — used when one window's watcher
    retires while other windows of the same app stay live."""
    key = (pid, window_id)
    with _id_lock:
        for d in (_counters, _ids_by_el, _els_by_id, _seen_at, _last_window):
            d.pop(key, None)


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


def snapshot(pid: int, window_id: int | None = None) -> dict:
    """Walk the addressed window of `pid` (window_id None => focused/main-window
    precedence). Returns window info + raw nodes, and registers every element under
    the (pid, window_id) key so actions on that window can resolve ids later."""
    win = windows.resolve_window(pid, window_id)
    if win is None:
        raise LookupError(f"no accessible window for pid {pid}")

    key = (pid, window_id)
    wx, wy, ww, wh = _geom(win) or (0.0, 0.0, 800.0, 600.0)
    window = {
        "pid": pid,
        "window_id": window_id if window_id is not None else windows.ax_window_id(win),
        "app": app_name(pid),
        "title": _coerce(_attr(win, "AXTitle")) or "",
        "x": wx, "y": wy, "w": ww, "h": wh,
    }
    _last_window[key] = window

    now = time.monotonic()
    with _id_lock:
        ids_by_el = _ids_by_el.setdefault(key, {})
        els_by_id = _els_by_id.setdefault(key, {})
        seen_at = _seen_at.setdefault(key, {})
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
        node_id = _intern(key, ids_by_el, els_by_id, seen_at, el, now)
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
                "help": _coerce(_attr(el, "AXHelp")),  # tooltip text (hover, read-only)
                "enabled": _attr(el, "AXEnabled") in (None, True, 1),
                "actions": actions,
                "x": geom[0], "y": geom[1], "w": geom[2], "h": geom[3],
            }
        )

    _prune(ids_by_el, els_by_id, seen_at, now)
    return {"window": window, "nodes": nodes, "truncated": truncated}


def last_window(pid: int, window_id: int | None = None) -> dict | None:
    return _last_window.get((pid, window_id)) or _last_window.get((pid, None))


def _lookup(pid: int, el_id: str, window_id: int | None = None):
    """Find a registered element by id in the addressed window, falling back to the
    app-level (pid, None) bucket so menu items (and bare-pid ids) resolve too."""
    with _id_lock:
        return (
            _els_by_id.get((pid, window_id), {}).get(el_id)
            or _els_by_id.get((pid, None), {}).get(el_id)
        )


def center(pid: int, el_id: str, window_id: int | None = None) -> tuple[float, float] | None:
    """Global screen-coordinate center of a registered element (by id), or None.
    Turns a ghost-DOM id into a real HID click target."""
    el = _lookup(pid, el_id, window_id)
    if el is None:
        return None
    g = _geom(el)
    if g is None:
        return None
    return (g[0] + g[2] / 2.0, g[1] + g[3] / 2.0)


def perform(pid: int, el_id: str, action: str, value=None, window_id: int | None = None) -> dict:
    el = _lookup(pid, el_id, window_id)
    if el is None:
        return {"ok": False, "error": f"unknown element id {el_id} (stale snapshot?)"}
    if action == "press":
        err = AXUIElementPerformAction(el, "AXPress")
        if err != 0:  # some controls (segmented/list rows) expose AXPick, not AXPress
            e2, names = AXUIElementCopyActionNames(el, None)
            if e2 == 0 and names and "AXPick" in [str(n) for n in names]:
                err = AXUIElementPerformAction(el, "AXPick")
    elif action == "confirm":
        err = AXUIElementPerformAction(el, "AXConfirm")
    elif action == "set_value":
        AXUIElementSetAttributeValue(el, "AXFocused", True)
        err = AXUIElementSetAttributeValue(el, "AXValue", "" if value is None else str(value))
    else:
        return {"ok": False, "error": f"unknown action {action!r}"}
    result = {"ok": err == 0, "axerror": int(err)}
    geom, win = _geom(el), last_window(pid, window_id)
    if geom and win:  # post-action element center, window-relative (for cursors/agents)
        result["point"] = [
            round(geom[0] + geom[2] / 2 - win["x"], 1),
            round(geom[1] + geom[3] / 2 - win["y"], 1),
        ]
    return result


def _scroll_area_at(pid: int, gx: float, gy: float, window_id: int | None = None):
    """Innermost AXScrollArea whose frame contains the global point. Descends the
    window tree along the containing path rather than hit-testing + walking up
    AXParent — many apps (e.g. Music) expose a broken parent chain that skips the
    scroll area. Elements without geometry are traversed but not matched."""
    win = windows.resolve_window(pid, window_id)
    if win is None:
        return None
    best, best_area = None, 0.0
    stack = [win]
    while stack:
        el = stack.pop()
        g = _geom(el)
        if g is not None:
            x, y, w, h = g
            if not (x <= gx <= x + w and y <= gy <= y + h):
                continue  # point outside: prune this subtree
            if str(_attr(el, "AXRole")) == "AXScrollArea" and (best is None or w * h < best_area):
                best, best_area = el, w * h
        stack.extend(_attr(el, "AXChildren") or [])
    return best


def _nudge_bar(bar, frac: float) -> bool:
    v = _attr(bar, "AXValue")
    if v is None:
        return False
    nv = min(1.0, max(0.0, float(v) + frac))
    if nv == float(v):
        return False
    return AXUIElementSetAttributeValue(bar, "AXValue", nv) == 0


def scroll(pid: int, gx: float, gy: float, dx: float, dy: float, window_id: int | None = None) -> dict:
    """Scroll the scroll area under global point (gx, gy) by nudging its AX
    scroll-bar value — pure AX, no event injection, no focus change. The wheel
    delta (pixels) is mapped to a fraction of the scroll area's height/width.
    Returns ok False when there's no accessible scroll bar there (the HID-wheel
    fallback is intentionally omitted — it needs focus-stealing injection)."""
    area = _scroll_area_at(pid, gx, gy, window_id)
    if area is None:
        return {"ok": False, "error": "no scroll area at point"}
    g = _geom(area) or (0.0, 0.0, 1000.0, 800.0)
    moved = False
    vbar, hbar = _attr(area, "AXVerticalScrollBar"), _attr(area, "AXHorizontalScrollBar")
    if vbar is not None and dy:
        moved |= _nudge_bar(vbar, dy / max(1.0, g[3] * 4))
    if hbar is not None and dx:
        moved |= _nudge_bar(hbar, dx / max(1.0, g[2] * 4))
    return {"ok": moved, "strategy": "ax-scrollbar"}
