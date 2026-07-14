"""A virtual agent cursor drawn on the real Mac screen (never in the browser).

A borderless, click-through, always-on-top overlay window shows an agent-colored
pointer that glides to each action's location, with a ring pulse on click/press
and a tinted pulse while typing — so a human watching the Mac sees the agent
work, alongside (not replacing) their own pointer.

AppKit must run on the main thread, so main.py calls build() then run() on the
main thread and starts uvicorn on a background thread; the server thread calls
show_action(), which hops to the main thread to update the overlay. If there is
no GUI session (headless/orphaned), build() returns False and webuse runs
without the overlay.
"""

import objc
from AppKit import (
    NSAnimationContext,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSMakeRect,
    NSScreen,
    NSTimer,
    NSView,
    NSWindow,
    NSWindowStyleMaskBorderless,
)

SIZE = 64          # overlay window is SIZE×SIZE; hotspot (arrow tip) at its center
HOT = SIZE / 2
_ACCENT = (0.30, 0.55, 0.96)  # agent blue

_overlay = None  # set by build(); None => no overlay (headless)


def _main_screen_height():
    s = NSScreen.mainScreen()
    return float(s.frame().size.height) if s else 0.0


class _CursorView(NSView):
    def initWithFrame_(self, f):
        self = objc.super(_CursorView, self).initWithFrame_(f)
        if self is not None:
            self._pulse = -1.0      # <0 = inactive, else 0..1 progress
            self._typing = False
        return self

    def isFlipped(self):  # top-left origin so our math matches screen coords
        return True

    def drawRect_(self, rect):
        r, g, b = _ACCENT
        # click/press ring pulse, expanding + fading from the hotspot
        if self._pulse >= 0.0:
            prog = self._pulse
            radius = 6 + prog * 22
            ring = NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(HOT - radius, HOT - radius, radius * 2, radius * 2)
            )
            NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, (1 - prog) * 0.8).set()
            ring.setLineWidth_(2.5)
            ring.stroke()
        # arrow, tip at the hotspot (center), body down-right
        p = NSBezierPath.bezierPath()
        pts = [(0, 0), (0, 18), (5, 13.5), (8.5, 21), (11, 19.5), (7.5, 12.5), (14, 12.5)]
        pts = [(HOT + x, HOT + y) for x, y in pts]
        p.moveToPoint_(pts[0])
        for pt in pts[1:]:
            p.lineToPoint_(pt)
        p.closePath()
        glow = 1.0 if self._typing else 0.0
        NSColor.colorWithSRGBRed_green_blue_alpha_(
            r + glow * 0.4, g, b - glow * 0.3, 1.0
        ).set()
        p.fill()
        NSColor.whiteColor().set()
        p.setLineWidth_(1.4)
        p.stroke()


class _Overlay(objc.lookUpClass("NSObject")):
    def init(self):
        self = objc.super(_Overlay, self).init()
        if self is None:
            return None
        self.screen_h = _main_screen_height()
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, SIZE, SIZE),
            NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered,
            False,
        )
        win.setOpaque_(False)
        win.setBackgroundColor_(NSColor.clearColor())
        win.setIgnoresMouseEvents_(True)   # click-through: never steals input
        win.setLevel_(2147483631)          # above ~everything
        win.setCollectionBehavior_((1 << 0) | (1 << 4) | (1 << 8))  # all Spaces + fullscreen
        view = _CursorView.alloc().initWithFrame_(NSMakeRect(0, 0, SIZE, SIZE))
        win.setContentView_(view)
        self.win = win
        self.view = view
        self.pulse_timer = None
        return self

    # --- called on the main thread only ---

    def _origin_for(self, gx, gy):
        # global top-left screen point -> AppKit bottom-left window origin,
        # centering the hotspot on (gx, gy).
        return (gx - HOT, self.screen_h - gy - HOT)

    def apply_(self, payload):
        gx, gy, kind = payload["x"], payload["y"], payload["type"]
        if not self.win.isVisible():
            self.win.setFrameOrigin_(self._origin_for(gx, gy))
            self.win.orderFrontRegardless()
        else:  # glide to the new spot
            NSAnimationContext.beginGrouping()
            NSAnimationContext.currentContext().setDuration_(0.18)
            self.win.animator().setFrameOrigin_(self._origin_for(gx, gy))
            NSAnimationContext.endGrouping()
        self.view._typing = kind == "set_value"
        if kind != "set_value":
            self.view._pulse = 0.0
            if self.pulse_timer is None:
                self.pulse_timer = (
                    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                        1 / 60.0, self, "pulseStep:", None, True
                    )
                )
        self.view.setNeedsDisplay_(True)

    def pulseStep_(self, timer):
        v = self.view
        if v._pulse < 0:
            return
        v._pulse += 0.055
        if v._pulse >= 1.0:
            v._pulse = -1.0
            if self.pulse_timer is not None:
                self.pulse_timer.invalidate()
                self.pulse_timer = None
        v.setNeedsDisplay_(True)

    # --- thread-safe entry from the server thread ---
    def signal_(self, payload):
        self.performSelectorOnMainThread_withObject_waitUntilDone_("apply_:", payload, False)


def build() -> bool:
    """Create the overlay on the main thread. Returns False if there's no GUI
    session (so webuse can still run headless without a cursor)."""
    global _overlay
    if NSScreen.mainScreen() is None:
        return False
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # no Dock icon
    _overlay = _Overlay.alloc().init()
    return _overlay is not None


def run():
    """Run the AppKit event loop (blocks the main thread)."""
    NSApplication.sharedApplication().run()


def show_action(gx: float, gy: float, kind: str) -> None:
    """Move the on-screen cursor to a global screen point and pulse. Safe to
    call from any thread; a no-op when there's no overlay."""
    if _overlay is not None:
        _overlay.signal_({"x": float(gx), "y": float(gy), "type": kind})
