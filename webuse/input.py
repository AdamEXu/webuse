"""Real HID input injection via Quartz CGEvents.

webuse is a per-app FOREGROUND remote desktop: Accessibility is used only to
discover and render the ghost DOM; ALL interaction is genuine HID input posted
to kCGHIDEventTap after the target app is activated. The real system cursor is
supposed to move — that's the point (hover, drag, context menus all behave like
a human is driving).

All coordinates are GLOBAL screen coordinates in points (top-left origin, the
same space AX geometry uses). The server maps window-relative stream coordinates
to global by adding the window origin before calling in here.

One module-level CGEventSource is shared by every event, with its local-events
suppression interval set to 0 so the user's own mouse doesn't stutter for the
usual ~0.25s after we post an event.
"""

import time

from AppKit import NSRunningApplication
from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventCreateMouseEvent,
    CGEventCreateScrollWheelEvent,
    CGEventKeyboardSetUnicodeString,
    CGEventPost,
    CGEventSetFlags,
    CGEventSetIntegerValueField,
    CGEventSetLocation,
    CGEventSourceCreate,
    CGEventSourceSetLocalEventsSuppressionInterval,
    kCGEventFlagMaskAlternate,
    kCGEventFlagMaskCommand,
    kCGEventFlagMaskControl,
    kCGEventFlagMaskShift,
    kCGEventLeftMouseDown,
    kCGEventLeftMouseDragged,
    kCGEventLeftMouseUp,
    kCGEventMouseMoved,
    kCGEventRightMouseDown,
    kCGEventRightMouseDragged,
    kCGEventRightMouseUp,
    kCGEventSourceStateHIDSystemState,
    kCGHIDEventTap,
    kCGMouseButtonLeft,
    kCGMouseButtonRight,
    kCGMouseEventClickState,
    kCGScrollEventUnitPixel,
)

# One shared source for all injected events. Suppression interval 0.0 keeps the
# user's real mouse from freezing for ~0.25s after each synthetic event.
_SOURCE = CGEventSourceCreate(kCGEventSourceStateHIDSystemState)
CGEventSourceSetLocalEventsSuppressionInterval(_SOURCE, 0.0)

# Per-button event types: (down, up, dragged, cg-button-constant).
_BUTTONS = {
    "left": (kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGEventLeftMouseDragged,
             kCGMouseButtonLeft),
    "right": (kCGEventRightMouseDown, kCGEventRightMouseUp, kCGEventRightMouseDragged,
              kCGMouseButtonRight),
}

_DRAG_STEPS = 12       # upper bound on interpolated points (never an open loop)
_DRAG_INTERVAL = 0.012  # ~12ms between drag points
_SETTLE = 0.12          # settle after activating an app before driving it


def _post_mouse(etype, gx, gy, button_const, click_state):
    ev = CGEventCreateMouseEvent(_SOURCE, etype, (gx, gy), button_const)
    if click_state:
        CGEventSetIntegerValueField(ev, kCGMouseEventClickState, click_state)
    CGEventPost(kCGHIDEventTap, ev)


def activate(pid: int) -> None:
    """Bring the app to the foreground so HID events reach it, then settle."""
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is not None:
        app.activateWithOptions_(1 << 1)  # NSApplicationActivateIgnoringOtherApps
        time.sleep(_SETTLE)


def move(gx: float, gy: float) -> None:
    """Move the real cursor to a global point (this is hover)."""
    _post_mouse(kCGEventMouseMoved, gx, gy, kCGMouseButtonLeft, 0)


def mouse_down(gx: float, gy: float, button: str = "left") -> None:
    down, _up, _drag, const = _BUTTONS[button]
    _post_mouse(down, gx, gy, const, 1)


def mouse_up(gx: float, gy: float, button: str = "left") -> None:
    _down, up, _drag, const = _BUTTONS[button]
    _post_mouse(up, gx, gy, const, 1)


def click(gx: float, gy: float, button: str = "left", count: int = 1) -> None:
    """Move to (gx, gy) then press+release. count=2 posts a full double-click
    (many AppKit controls need each intermediate click state); button='right'
    posts a context-menu click."""
    down, up, _drag, const = _BUTTONS[button]
    move(gx, gy)
    for state in range(1, count + 1):  # 1 for a click; 1 then 2 for a double-click
        _post_mouse(down, gx, gy, const, state)
        _post_mouse(up, gx, gy, const, state)


def drag(x1: float, y1: float, x2: float, y2: float, button: str = "left") -> None:
    """Press at (x1,y1), glide through a small bounded set of interpolated
    dragged points, release at (x2,y2)."""
    down, up, dragged, const = _BUTTONS[button]
    move(x1, y1)
    _post_mouse(down, x1, y1, const, 1)
    for i in range(1, _DRAG_STEPS):
        t = i / _DRAG_STEPS
        _post_mouse(dragged, x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, const, 0)
        time.sleep(_DRAG_INTERVAL)
    _post_mouse(up, x2, y2, const, 1)


def scroll(gx: float, gy: float, dx: float, dy: float) -> None:
    """Post a pixel-unit scroll-wheel event at a global point. dy scrolls
    vertically, dx horizontally (positive dy scrolls content up, matching wheel
    convention)."""
    ev = CGEventCreateScrollWheelEvent(
        _SOURCE, kCGScrollEventUnitPixel, 2, int(dy), int(dx)
    )
    CGEventSetLocation(ev, (gx, gy))
    CGEventPost(kCGHIDEventTap, ev)


# --- keyboard -------------------------------------------------------------

def key(code: int, mods: int = 0, down: bool = True) -> None:
    """Post one keyDown or keyUp for a macOS virtual keycode with CGEventFlags."""
    ev = CGEventCreateKeyboardEvent(_SOURCE, code, down)
    if mods:
        CGEventSetFlags(ev, mods)
    CGEventPost(kCGHIDEventTap, ev)


def key_tap(code: int, mods: int = 0) -> None:
    """Press and release a key."""
    key(code, mods, True)
    key(code, mods, False)


def type_text(s: str) -> None:
    """Type literal text as a bounded sequence of key taps. Each character is
    delivered by its unicode string on the key event, so it's layout-robust and
    handles any character regardless of the physical keymap."""
    for ch in s:
        down = CGEventCreateKeyboardEvent(_SOURCE, 0, True)
        CGEventKeyboardSetUnicodeString(down, len(ch), ch)
        CGEventPost(kCGHIDEventTap, down)
        up = CGEventCreateKeyboardEvent(_SOURCE, 0, False)
        CGEventKeyboardSetUnicodeString(up, len(ch), ch)
        CGEventPost(kCGHIDEventTap, up)
        time.sleep(0.004)


# KeyboardEvent.code -> macOS virtual keycode (kVK_*). Keyed on the PHYSICAL
# code, not the produced character, so it's independent of keyboard layout.
KEYMAP: dict[str, int] = {
    # letters
    "KeyA": 0, "KeyB": 11, "KeyC": 8, "KeyD": 2, "KeyE": 14, "KeyF": 3,
    "KeyG": 5, "KeyH": 4, "KeyI": 34, "KeyJ": 38, "KeyK": 40, "KeyL": 37,
    "KeyM": 46, "KeyN": 45, "KeyO": 31, "KeyP": 35, "KeyQ": 12, "KeyR": 15,
    "KeyS": 1, "KeyT": 17, "KeyU": 32, "KeyV": 9, "KeyW": 13, "KeyX": 7,
    "KeyY": 16, "KeyZ": 6,
    # digits
    "Digit0": 29, "Digit1": 18, "Digit2": 19, "Digit3": 20, "Digit4": 21,
    "Digit5": 23, "Digit6": 22, "Digit7": 26, "Digit8": 28, "Digit9": 25,
    # arrows
    "ArrowLeft": 123, "ArrowRight": 124, "ArrowDown": 125, "ArrowUp": 126,
    # editing / whitespace
    "Enter": 36, "Return": 36, "NumpadEnter": 76, "Tab": 48, "Escape": 53,
    "Space": 49, "Backspace": 51, "Delete": 117,
    "Home": 115, "End": 119, "PageUp": 116, "PageDown": 121,
    # punctuation
    "Minus": 27, "Equal": 24, "BracketLeft": 33, "BracketRight": 30,
    "Backslash": 42, "Semicolon": 41, "Quote": 39, "Comma": 43,
    "Period": 47, "Slash": 44, "Backquote": 50,
    # function row
    "F1": 122, "F2": 120, "F3": 99, "F4": 118, "F5": 96, "F6": 97,
    "F7": 98, "F8": 100, "F9": 101, "F10": 109, "F11": 103, "F12": 111,
}

# KeyboardEvent modifier booleans -> CGEventFlags.
MODMAP: dict[str, int] = {
    "shiftKey": kCGEventFlagMaskShift,
    "ctrlKey": kCGEventFlagMaskControl,
    "altKey": kCGEventFlagMaskAlternate,
    "metaKey": kCGEventFlagMaskCommand,
}


def flags_for(mods: dict) -> int:
    """Combine a dict of KeyboardEvent modifier booleans into CGEventFlags."""
    out = 0
    for name, flag in MODMAP.items():
        if mods.get(name):
            out |= flag
    return out
