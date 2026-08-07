"""Cocoa interop rules this package has to follow to stay leak-free."""

import functools

import objc


def pooled(fn):
    """Run `fn` inside an NSAutoreleasePool.

    Cocoa and Core Foundation hand back plenty of AUTORELEASED objects, which live
    until the current thread's pool drains. The main thread's run loop drains one
    every cycle; a plain worker thread has neither a run loop nor a pool, so
    anything autoreleased on it is simply never freed. Every ObjC entry point in
    this package runs on such a thread — the asyncio executor that captures frames,
    anyio's threadpool that serves the sync endpoints, the app/window watchers'
    own loops — so each one needs a pool of its own.

    This is not a micro-optimization. A captured frame is a full-resolution,
    IOSurface-backed CGImage (~17 MB for a 1266x853 window at 2x), so one 8fps
    stream leaked ~137 MB/s and pulled WindowServer up in lockstep, until the
    server was jetsammed at ~98 GB resident.

    Whatever crosses back out must be a Python value or an object the caller owns
    (a `bytes` copy, a plain str/float, a +1 Create/Copy result) — never a bare
    autoreleased ObjC object, which the drain would free under the caller.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with objc.autorelease_pool():
            return fn(*args, **kwargs)

    return wrapper


def data_bytes(data) -> bytes:
    """Copy an NSData into Python bytes — the only conversion that doesn't leak.

    `bytes(nsdata)` and `nsdata.bytes().tobytes()` both strand a reference to the
    NSData (measured on pyobjc 12.2.1: every converted object stays resident, so a
    JPEG stream leaks one frame's worth per frame, ~2.7 MB/s at 8fps — an
    autorelease pool does NOT reclaim it). Going through `memoryview` releases the
    buffer properly and is flat over hundreds of thousands of frames.
    """
    return memoryview(data).tobytes()
