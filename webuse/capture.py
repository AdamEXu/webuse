"""Window capture via ScreenCaptureKit, encoded as JPEG / MJPEG."""

import asyncio
import threading
import time

import ScreenCaptureKit as SCK
from AppKit import (
    NSBitmapImageFileTypeJPEG,
    NSBitmapImageRep,
    NSImageCompressionFactor,
    NSScreen,
)
from Quartz import (
    CGRectMake,
    CGRectNull,
    CGWindowListCopyWindowInfo,
    CGWindowListCreateImage,
    kCGNullWindowID,
    kCGWindowImageBestResolution,
    kCGWindowImageBoundsIgnoreFraming,
    kCGWindowListExcludeDesktopElements,
    kCGWindowListOptionIncludingWindow,
    kCGWindowListOptionOnScreenOnly,
)


class CaptureError(RuntimeError):
    pass


# Process-wide latch: once SCScreenshotManager's completion handler is seen to
# never fire (some macOS builds), every later WindowCapture skips it from the
# start instead of each independently eating the screenshot timeout.
_sck_screenshot_broken = False


def _finish(invoke, timeout=10.0):
    """Run an SCK completion-handler API synchronously."""
    done = threading.Event()
    box = {}

    def handler(result, error):
        box["r"], box["e"] = result, error
        done.set()

    invoke(handler)
    if not done.wait(timeout):
        raise CaptureError("ScreenCaptureKit call timed out")
    if box.get("e"):
        raise CaptureError(str(box["e"]))
    return box.get("r")


def _scale() -> float:
    s = NSScreen.mainScreen()
    return float(s.backingScaleFactor()) if s else 2.0


class WindowCapture:
    """Captures one app window by pid (works even when the window is occluded)."""

    def __init__(
        self, pid: int, target_frame=None, get_frame=None, fps: float = 8.0,
        quality: float = 0.6, window_id: int | None = None
    ):
        self.pid = pid
        self.target_frame = target_frame  # (x, y, w, h) of the AX window, to disambiguate
        self.target_window_id = window_id  # exact CGWindowID to capture, when known
        self.get_frame = get_frame  # callable -> current AX window (x, y, w, h) or None
        self.fps = fps
        self.quality = quality
        self._filter = None
        self._config = None
        self._window_id = None
        self._resolved_frame = None
        self._frames_since_resolve = 0
        # sticky fallback once SCScreenshotManager misbehaves; start latched if a
        # prior capture in this process already found it broken.
        self._use_cg = _sck_screenshot_broken

    def _resolve(self):
        content = _finish(SCK.SCShareableContent.getShareableContentWithCompletionHandler_)

        # macOS reports isOnScreen==False for every window on a Space other than
        # the active one (and for fullscreen apps in their own Space), yet
        # initWithDesktopIndependentWindow_ still captures real content for them.
        # So don't hard-filter on isOnScreen — rank by frame match and use
        # on-screen only as a tiebreak. Require layer 0 (skips desktop/backstop/
        # wallpaper at large-negative layers, menubar=24, Dock=20, and shadows)
        # and skip degenerate 1x1/0x0 helper windows.
        def _key(w):
            f = w.frame()
            onscreen_pen = 0 if w.isOnScreen() else 1
            # An exact CGWindowID match wins outright (prepended), so multi-window
            # apps stream the addressed window; None leaves ranking unchanged.
            id_pen = 0 if (
                self.target_window_id is not None
                and int(w.windowID()) == self.target_window_id
            ) else 1
            if self.target_frame:
                tx, ty, tw, th = self.target_frame
                dist = (
                    abs(f.origin.x - tx) + abs(f.origin.y - ty)
                    + abs(f.size.width - tw) + abs(f.size.height - th)
                )
                return (id_pen, dist, onscreen_pen)  # id, then frame, then on-screen
            return (id_pen, onscreen_pen, -f.size.width * f.size.height)

        cands = []
        for w in content.windows():
            app = w.owningApplication()
            if app is None or int(app.processID()) != self.pid:
                continue
            if w.windowLayer() != 0:  # skip menubar/dock/desktop/backstop/shadows
                continue
            f = w.frame()
            if f.size.width < 2 or f.size.height < 2:  # skip 1x1 / 0x0 helper windows
                continue
            cands.append(w)
        if not cands:
            raise CaptureError(f"no window for pid {self.pid}")
        best = min(cands, key=_key)
        scale = _scale()
        conf = SCK.SCStreamConfiguration.alloc().init()
        conf.setWidth_(max(1, int(best.frame().size.width * scale)))
        conf.setHeight_(max(1, int(best.frame().size.height * scale)))
        conf.setShowsCursor_(False)
        self._filter = SCK.SCContentFilter.alloc().initWithDesktopIndependentWindow_(best)
        self._config = conf
        self._window_id = int(best.windowID())
        f = best.frame()
        self._resolved_frame = (
            float(f.origin.x), float(f.origin.y), float(f.size.width), float(f.size.height)
        )
        self._frames_since_resolve = 0

    def _resolve_cg(self):
        """Resolve the window via the legacy CGWindowList API for environments
        where ScreenCaptureKit fails (e.g. SCShareableContent TCC errors)."""
        wins = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
            kCGNullWindowID,
        ) or []
        best, best_score = None, None
        for w in wins:
            if w.get("kCGWindowOwnerPID") != self.pid or w.get("kCGWindowLayer", 0) != 0:
                continue
            b = w["kCGWindowBounds"]
            f = (b["X"], b["Y"], b["Width"], b["Height"])
            if self.target_window_id is not None and \
                    int(w["kCGWindowNumber"]) == self.target_window_id:
                score = -1e18  # exact CGWindowID match wins outright
            elif self.target_frame:
                score = sum(abs(a - t) for a, t in zip(f, self.target_frame))
            else:
                score = -f[2] * f[3]  # biggest window wins
            if best is None or score < best_score:
                best, best_score = w, score
        if best is None:
            raise CaptureError(f"no on-screen window for pid {self.pid}")
        b = best["kCGWindowBounds"]
        self._window_id = int(best["kCGWindowNumber"])
        self._resolved_frame = (
            float(b["X"]), float(b["Y"]), float(b["Width"]), float(b["Height"])
        )
        self._frames_since_resolve = 0

    def _cg_image(self):
        """Legacy CGWindowList capture — fallback for OS builds where
        SCScreenshotManager's completion handler never fires. Tries the exact
        window first; builds that return None for that get the window's screen
        rect instead (occluding windows included — degraded but live)."""
        img = CGWindowListCreateImage(
            CGRectNull,
            kCGWindowListOptionIncludingWindow,
            self._window_id,
            kCGWindowImageBoundsIgnoreFraming | kCGWindowImageBestResolution,
        )
        if img is None and self._resolved_frame:
            x, y, w, h = self._resolved_frame
            img = CGWindowListCreateImage(
                CGRectMake(x, y, w, h),
                kCGWindowListOptionOnScreenOnly,
                kCGNullWindowID,
                kCGWindowImageBestResolution,
            )
        return img

    def frame_jpeg(self) -> bytes:
        if self._use_cg:
            self._resolve_cg()  # cheap; also keeps the rect fresh across moves
            img = self._cg_image()
        else:
            if self._filter is None or self._frames_since_resolve > 120:  # backstop only;
                try:  # moves/resizes re-resolve instantly via get_frame in mjpeg()
                    self._resolve()
                except CaptureError:
                    self._resolve_cg()  # SCK broken here: switch to legacy capture
                    self._use_cg = True
                    return self.frame_jpeg()
            try:
                img = _finish(
                    lambda h: SCK.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
                        self._filter, self._config, h
                    ),
                    timeout=3.0,  # a real screenshot returns in <1s; don't wait the full 10s
                )
            except CaptureError:  # e.g. completion handler never fires (macOS 26 beta)
                global _sck_screenshot_broken
                _sck_screenshot_broken = True  # latch so later streams skip SCK entirely
                self._use_cg = True
                img = self._cg_image()
                if img is None:
                    raise
        self._frames_since_resolve += 1
        if img is None:
            raise CaptureError("no image returned")
        rep = NSBitmapImageRep.alloc().initWithCGImage_(img)
        data = rep.representationUsingType_properties_(
            NSBitmapImageFileTypeJPEG, {NSImageCompressionFactor: self.quality}
        )
        if data is None:
            raise CaptureError("JPEG encode failed")
        return bytes(data)

    def _next_frame(self) -> bytes:
        """One capture step: re-resolve if the AX window moved/resized, then grab
        a JPEG. Blocking (SCK/AX + time-boxed screenshot up to ~10s) — run off the
        event loop via run_in_executor, never inline in the async generator."""
        if self.get_frame and self._resolved_frame:
            f = self.get_frame()
            if f and any(abs(a - b) > 1.0 for a, b in zip(f, self._resolved_frame)):
                self.target_frame = f
                self._filter = None  # window moved/resized: re-resolve now
        return self.frame_jpeg()

    async def jpegs(self):
        """Async generator yielding raw JPEG frames, paced to `fps`. It's async (not
        a sync generator) so an open stream does NOT pin an anyio threadpool worker
        while it sleeps/blocks: the blocking SCK/AX capture runs in the default
        executor and pacing uses asyncio.sleep, so many concurrent tabs no longer
        starve the pool that serves the other sync endpoints. Returns after ~5s of
        capture failures rather than looping silently forever (the MJPEG wrapper
        closes the HTTP stream so the client <img> fires onerror; the stream_ws
        wrapper closes the socket so the client falls back to MJPEG)."""
        loop = asyncio.get_running_loop()
        errors = 0
        while True:
            t0 = time.time()
            try:
                jpg = await loop.run_in_executor(None, self._next_frame)
                errors = 0
            except CaptureError:
                self._filter = None
                errors += 1
                if errors >= 5:
                    return
                await asyncio.sleep(1.0)
                continue
            yield jpg
            await asyncio.sleep(max(0.0, 1.0 / self.fps - (time.time() - t0)))

    async def mjpeg(self):
        """multipart/x-mixed-replace MJPEG framing around jpegs() — the fallback
        transport. server.py integrator: StreamingResponse consumes this async
        generator directly on the event loop, so `/app/{pid}/stream` needs no change
        even though app_stream is a plain `def` (the handler just builds the response
        and returns; iteration happens on the loop)."""
        async for jpg in self.jpegs():
            yield (
                b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n"
            )
