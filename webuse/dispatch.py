"""The HID action engine: resolve a Step's target to global screen coords and
deliver it as real input (click/drag/scroll/key/type) or a discovery-side AX
action. Every real-input path funnels through dispatch()."""

from . import ax, input, windows
from .models import Step


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


def dispatch(pid: int, s: Step, window_id: int | None = None) -> dict:
    if s.activate:
        input.activate(pid)
        windows.raise_window(pid, window_id)  # raise the addressed window, if any
    # Discovery-side AX actions: the web menu bar presses real menu items (which
    # live outside the app window and can't be reached by a coordinate click).
    if s.action in ("press", "confirm", "set_value"):
        if not s.id:
            return {"ok": False, "error": "id required", "code": "missing_id"}
        return ax.perform(pid, s.id, s.action, s.value, window_id)
    if s.action == "key":
        code = input.KEYMAP.get(s.code or "")
        if code is None:
            return {"ok": False, "error": f"unknown key {s.code!r}", "code": "unknown_key"}
        mods = input.flags_for(_mods(s))
        if s.down is None:
            input.key_tap(code, mods)
        else:
            input.key(code, mods, s.down)
        return {"ok": True, "key": s.code, "verified": False}
    if s.action == "type":
        if not s.value:
            return {"ok": False, "error": "value required", "code": "value_required"}
        input.type_text(s.value)
        return {"ok": True}
    # Everything else targets a point (global coords) via real HID input.
    g = _global_target(pid, s, window_id)
    if g is None:
        return {"ok": False, "error": "no window or target", "code": "no_target"}
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
            return {"ok": False, "error": "drag needs x2,y2", "code": "drag_needs_endpoint"}
        input.drag(gx, gy, win["x"] + s.x2, win["y"] + s.y2)
    else:
        return {"ok": False, "error": f"unknown action {s.action!r}", "code": "unknown_action"}
    out: dict = {"ok": True}
    pt = _rel_point(pid, gx, gy, window_id)
    if pt:
        out["point"] = pt
    return out


def _mods(s: Step) -> dict:
    return {"shiftKey": s.shift, "ctrlKey": s.ctrl, "altKey": s.alt, "metaKey": s.meta}
