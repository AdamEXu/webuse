"""Window-management helpers for the multi-app workspace: focus (raise/activate),
set_frame (move+resize), and tile (grid the native windows to match the layout).

Imports from `ax` but never edits it. `ax` doesn't expose the window element, so
the small window resolver is duplicated here with the SAME precedence snapshot()
uses (case-agnostic, no per-app branching)."""

import math

from AppKit import NSRunningApplication
from ApplicationServices import (
    AXUIElementCreateApplication,
    AXUIElementCopyAttributeValue,
    AXUIElementPerformAction,
    AXUIElementSetAttributeValue,
    AXValueCreate,
)
from Quartz import CGDisplayBounds, CGMainDisplayID, CGPoint, CGSize

kAXValueCGPointType = 1
kAXValueCGSizeType = 2

# NSApplicationActivateIgnoringOtherApps (mirrors ax.click's activate path).
_ACTIVATE_IGNORING_OTHER_APPS = 1 << 1

# Reserve a top strip so tiled windows aren't clipped under the menu bar.
_MENU_BAR_MARGIN = 37.0


def _attr(el, name):
    err, val = AXUIElementCopyAttributeValue(el, name, None)
    return val if err == 0 else None


def _window_el(pid: int):
    """Resolve the app's window element with the same precedence snapshot() uses:
    AXFocusedWindow -> AXMainWindow -> AXWindows[0]."""
    app_el = AXUIElementCreateApplication(pid)
    win = _attr(app_el, "AXFocusedWindow") or _attr(app_el, "AXMainWindow")
    if win is None:
        wins = _attr(app_el, "AXWindows")
        win = wins[0] if wins else None
    return win


def focus_app(pid: int) -> dict:
    """Raise + activate the app's window (the per-pane Focus button)."""
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    activated = bool(app.activateWithOptions_(_ACTIVATE_IGNORING_OTHER_APPS)) if app else False
    win = _window_el(pid)
    raised = win is not None and AXUIElementPerformAction(win, "AXRaise") == 0
    return {"ok": activated or raised, "pid": pid, "activated": activated, "raised": raised}


def set_frame(pid: int, x: float, y: float, w: float, h: float) -> dict:
    """Move + resize the app's window. AXPosition/AXSize are both settable."""
    win = _window_el(pid)
    if win is None:
        return {"ok": False, "pid": pid, "error": "no window"}
    pos = AXValueCreate(kAXValueCGPointType, CGPoint(x, y))
    size = AXValueCreate(kAXValueCGSizeType, CGSize(w, h))
    e1 = AXUIElementSetAttributeValue(win, "AXPosition", pos)
    e2 = AXUIElementSetAttributeValue(win, "AXSize", size)
    return {"ok": e1 == 0 and e2 == 0, "pid": pid, "x": x, "y": y, "w": w, "h": h}


def tile(pids: list[int], cols: int | None = None) -> list[dict]:
    """Grid the given windows over the main display's work area, in the order
    given. `cols` defaults to ceil(sqrt(n)) — derived from the count, not per app."""
    pids = [int(p) for p in pids]
    n = len(pids)
    if n == 0:
        return []
    if not cols or cols < 1:
        cols = math.ceil(math.sqrt(n))
    cols = min(cols, n)
    rows = math.ceil(n / cols)

    b = CGDisplayBounds(CGMainDisplayID())
    ox, oy = float(b.origin.x), float(b.origin.y) + _MENU_BAR_MARGIN
    ow, oh = float(b.size.width), float(b.size.height) - _MENU_BAR_MARGIN
    cw, ch = ow / cols, oh / rows

    frames = []
    for i, pid in enumerate(pids):
        r, c = divmod(i, cols)
        frames.append(set_frame(pid, ox + c * cw, oy + r * ch, cw, ch))
    return frames
