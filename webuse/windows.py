"""Per-window addressing: map an app's AX windows to stable CGWindowIDs, parse
handles, and resolve a window element by id.

A "handle" is how the rest of the system names which window of an app to drive:
  "pid"        -> (pid, None)  — today's focused/main-window precedence (back-compat)
  "pid.winid"  -> (pid, winid) — a specific CGWindowID of that app

The AX->CGWindowID bridge is the private ApplicationServices symbol
  AXError _AXUIElementGetWindow(AXUIElementRef, CGWindowID*)
(spike-confirmed callable via ctypes). When it's unavailable we fall back to
matching the AXWindow to the on-screen CGWindowList by frame + title.

Self-contained (own AX attribute reader) so `ax` can import this
without a cycle."""

import ctypes
import ctypes.util

import objc
from ApplicationServices import (
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementPerformAction,
    AXValueGetValue,
)

try:  # older PyObjC builds may not expose it; then the global default applies
    from ApplicationServices import AXUIElementSetMessagingTimeout
except ImportError:
    AXUIElementSetMessagingTimeout = None
from Quartz import (
    CGWindowListCopyWindowInfo,
    kCGNullWindowID,
    kCGWindowListExcludeDesktopElements,
    kCGWindowListOptionOnScreenOnly,
)

kAXValueCGPointType = 1
kAXValueCGSizeType = 2


def _load_get_window():
    """Bind _AXUIElementGetWindow(AXUIElementRef, CGWindowID*) -> AXError via
    ctypes. Returns a callable(AXUIElementRef-as-int)->CGWindowID|None, or None if
    the private symbol can't be resolved (then callers use the frame/title
    fallback)."""
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("ApplicationServices"))
        fn = lib._AXUIElementGetWindow
    except (OSError, AttributeError):
        return None
    fn.restype = ctypes.c_int
    fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]

    def get_window(ref_id: int):
        out = ctypes.c_uint32(0)
        err = fn(ctypes.c_void_p(ref_id), ctypes.byref(out))
        return int(out.value) if err == 0 else None

    return get_window


_GET_WINDOW = _load_get_window()

# Per-app AX messaging timeout. The system default (~6s) lets ONE hung app
# stall a walk long enough to truncate the whole tree; a short timeout turns a
# hung reply into a per-attribute None (callers already treat errors as None).
MESSAGING_TIMEOUT_S = 0.75


def app_element(pid: int):
    """App-level AXUIElement with the short messaging timeout applied. Every
    app-element creation funnels through here so no path forgets the timeout."""
    el = AXUIElementCreateApplication(pid)
    if AXUIElementSetMessagingTimeout is not None:
        AXUIElementSetMessagingTimeout(el, MESSAGING_TIMEOUT_S)
    return el


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


def ax_window_id(el) -> int | None:
    """Stable CGWindowID for an AXWindow element via the private bridge, or None
    (missing symbol / not a window)."""
    if _GET_WINDOW is None or el is None:
        return None
    return _GET_WINDOW(objc.pyobjc_id(el))


def _app_windows(pid: int):
    return list(_attr(app_element(pid), "AXWindows") or [])


def _cg_window_info(window_id: int) -> dict | None:
    wins = CGWindowListCopyWindowInfo(
        kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
        kCGNullWindowID,
    ) or []
    for w in wins:
        if int(w.get("kCGWindowNumber", -1)) == window_id:
            return w
    return None


def _resolve_by_frame(pid: int, window_id: int):
    """Fallback resolver: match the CGWindowID's on-screen frame + title to one of
    the app's AXWindows when the private bridge is unavailable."""
    info = _cg_window_info(window_id)
    if info is None:
        return None
    b = info["kCGWindowBounds"]
    tx, ty, tw, th = b["X"], b["Y"], b["Width"], b["Height"]
    title = str(info.get("kCGWindowName") or "")
    best, best_score = None, None
    for w in _app_windows(pid):
        g = _geom(w)
        if g is None:
            continue
        score = abs(g[0] - tx) + abs(g[1] - ty) + abs(g[2] - tw) + abs(g[3] - th)
        if title and str(_attr(w, "AXTitle") or "") == title:
            score -= 10000.0  # a matching title dominates minor frame drift
        if best is None or score < best_score:
            best, best_score = w, score
    return best


def list_windows(pid: int) -> list[dict]:
    """The app's AXWindows as [{window_id, title, focused, minimized}].
    window_id is None only when neither the bridge nor the frame fallback can
    identify it (still listed, just not directly addressable)."""
    app = app_element(pid)
    focused = _attr(app, "AXFocusedWindow")
    out = []
    for w in _attr(app, "AXWindows") or []:
        wid = ax_window_id(w)
        out.append(
            {
                "window_id": wid,
                "title": str(_attr(w, "AXTitle") or ""),
                "focused": bool(focused is not None and w == focused),
                "minimized": _attr(w, "AXMinimized") in (True, 1),
            }
        )
    return out


def resolve_window(pid: int, window_id: int | None = None):
    """AXWindow element for (pid, window_id). window_id None keeps today's
    precedence (AXFocusedWindow -> AXMainWindow -> AXWindows[0]); an explicit id is
    matched via the bridge, then the frame/title fallback."""
    app = app_element(pid)
    if window_id is None:
        win = _attr(app, "AXFocusedWindow") or _attr(app, "AXMainWindow")
        if win is None:
            wins = _attr(app, "AXWindows")
            win = wins[0] if wins else None
        return win
    for w in _attr(app, "AXWindows") or []:
        if ax_window_id(w) == window_id:
            return w
    return _resolve_by_frame(pid, window_id)


def live_origin(pid: int, window_id: int | None = None) -> tuple[float, float] | None:
    """CURRENT top-left of the addressed window, read live from AX. Snapshot
    origins go stale the moment the window moves — coordinate actions must
    resolve against where the window is now."""
    win = resolve_window(pid, window_id)
    g = _geom(win) if win is not None else None
    return (g[0], g[1]) if g is not None else None


def raise_window(pid: int, window_id: int | None) -> bool:
    """AXRaise a specific window to the front (Accessibility, not HID). No-op-safe
    when the window can't be resolved."""
    if window_id is None:
        return False
    win = resolve_window(pid, window_id)
    return win is not None and AXUIElementPerformAction(win, "AXRaise") == 0


def parse_handle(handle) -> tuple[int, int | None]:
    """'pid' -> (pid, None); 'pid.winid' -> (pid, winid)."""
    s = str(handle)
    if "." in s:
        pid_s, wid_s = s.split(".", 1)
        return int(pid_s), int(wid_s)
    return int(s), None


def format_handle(pid: int, window_id: int | None = None) -> str:
    return f"{pid}.{window_id}" if window_id is not None else str(pid)
