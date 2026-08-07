"""Per-app endpoints: windows, icon, menu, ghost DOM, SSE DOM push, MJPEG +
WebSocket screen streams, and the low-latency WebSocket input channel."""

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)

from .. import apps, ax, dispatch, dom, menubar, watch, windows
from ..capture import WindowCapture
from ..models import Step
from ..security import ws_authed

router = APIRouter()


def _parse(handle: str) -> tuple[int, int | None]:
    """'pid' or 'pid.winid' -> (pid, window_id|None). Raises ValueError on junk."""
    return windows.parse_handle(handle)


@router.get("/app/{handle}/windows")
def app_windows(handle: str):
    """The app's windows, so the client can offer per-window navigation."""
    try:
        pid, _ = _parse(handle)
    except ValueError:
        return JSONResponse({"error": "bad app handle"}, status_code=400)
    return {"pid": pid, "windows": windows.list_windows(pid)}


@router.get("/app/{handle}/icon")
def app_icon(handle: str):
    """The app's icon as a PNG (app-level; window_id in the handle is ignored)."""
    try:
        pid, _ = _parse(handle)
    except ValueError:
        return PlainTextResponse("bad app handle", status_code=400)
    png = apps.icon_png(pid)
    if png is None:
        return PlainTextResponse("no icon", status_code=404)
    return Response(
        content=png, media_type="image/png",
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get("/app/{handle}/menu")
def app_menu(handle: str):
    try:
        pid, _ = _parse(handle)
    except ValueError:
        return JSONResponse({"menu": [], "error": "bad app handle"}, status_code=400)
    try:
        return {"menu": menubar.read_menu(pid)}
    except Exception as e:  # a missing/odd menu bar must not break the page
        return {"menu": [], "error": str(e)}


@router.get("/app/{handle}/dom")
def app_dom(handle: str):
    try:
        pid, window_id = _parse(handle)
    except ValueError:
        return JSONResponse({"error": "bad app handle"}, status_code=400)
    try:
        snap = ax.snapshot(pid, window_id)
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return {
        "window": snap["window"],
        "truncated": snap["truncated"],
        "nodes": dom.build_nodes(snap),
    }


@router.get("/app/{handle}/focus")
def app_focus(handle: str):
    """Where the app's keyboard focus currently sits. The compose bar polls this
    so text is only ever sent at a target the caller can see."""
    try:
        pid, _ = _parse(handle)
    except ValueError:
        return JSONResponse({"error": "bad app handle"}, status_code=400)
    return {"focus": ax.focused_info(pid)}


def _sse(item: tuple[str | None, str]) -> str:
    event, payload = item
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {payload}\n\n"


@router.get("/app/{handle}/events")
async def app_events(handle: str):
    pid, window_id = _parse(handle)
    watcher, q = watch.subscribe(pid, asyncio.get_running_loop(), window_id)

    async def gen():
        try:
            if watcher.current:
                yield _sse(watcher.current)
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield _sse(item)
        finally:
            watcher.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/app/{handle}/stream")
def app_stream(handle: str):
    pid, window_id = _parse(handle)

    def frame():  # stays fresh: the DOM watcher re-snapshots on AXWindowMoved/Resized
        w = ax.last_window(pid, window_id)
        return (w["x"], w["y"], w["w"], w["h"]) if w else None

    cap = WindowCapture(pid, target_frame=frame(), get_frame=frame, window_id=window_id)
    return StreamingResponse(
        cap.mjpeg(), media_type="multipart/x-mixed-replace; boundary=frame"
    )


@router.websocket("/app/{handle}/stream_ws")
async def app_stream_ws(ws: WebSocket, handle: str):
    """Lower-latency screen transport: push each captured JPEG as a binary frame as
    soon as it's ready (the page swaps it onto the <img> via a Blob URL), instead of
    the MJPEG multipart stream. The client keeps the MJPEG endpoint as a fallback, so
    if this socket never opens (or drops) nothing regresses. The handle fixes which
    window is captured, exactly like /stream."""
    if not ws_authed(ws):
        await ws.close(code=1008)
        return
    try:
        pid, window_id = _parse(handle)
    except ValueError:
        await ws.close(code=1008)
        return
    await ws.accept()

    def frame():
        w = ax.last_window(pid, window_id)
        return (w["x"], w["y"], w["w"], w["h"]) if w else None

    cap = WindowCapture(pid, target_frame=frame(), get_frame=frame, window_id=window_id)
    gen = cap.jpegs()
    # CLIENT-PACED PULL. A background producer keeps a single latest-frame slot
    # fresh; we send a frame ONLY when the client asks for the next (it pings after
    # rendering each frame). That caps in-flight at one frame, so a slow link can
    # never build a backlog — each request is answered with the FRESHEST captured
    # frame and whatever was captured in between is silently dropped. (TCP never
    # drops and can buffer without bound, so push pacing lets latency grow on
    # moving video; pull pacing fixes it regardless of transport buffering.) The
    # client's ping doubles as the disconnect signal: ws.receive() raises when it
    # goes away, letting us stop capturing for a dead socket.
    gone = asyncio.Event()
    fresh = asyncio.Event()
    slot = {"jpg": None}

    async def _produce():
        try:
            async for jpg in gen:
                if gone.is_set():
                    break
                slot["jpg"] = jpg  # overwrite — only the newest frame survives
                fresh.set()
        except Exception:
            pass
        finally:
            gone.set()
            fresh.set()

    producer = asyncio.create_task(_produce())
    try:
        while True:
            try:
                await ws.receive()  # client "ready" ping (also raises on disconnect)
            except Exception:
                break
            if gone.is_set():
                break
            while slot["jpg"] is None and not gone.is_set():  # await a frame (<=~1/fps)
                fresh.clear()
                if slot["jpg"] is None:
                    await fresh.wait()
            if gone.is_set():
                break
            jpg, slot["jpg"] = slot["jpg"], None
            try:
                await ws.send_bytes(jpg)
            except Exception:
                break
    finally:
        gone.set()
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)
        await gen.aclose()  # stop pulling capture frames for a dead socket


@router.websocket("/app/{handle}/ws")
async def app_ws(ws: WebSocket, handle: str):
    """Low-latency input channel: the page streams compact step messages
    (move/click/drag/scroll/key…) here instead of one HTTP POST per event. Each
    is run off the event loop so a drag's sleeps never stall the stream. The
    handle fixes which window every step targets."""
    if not ws_authed(ws):
        await ws.close(code=1008)
        return
    try:
        pid, window_id = _parse(handle)
    except ValueError:
        await ws.close(code=1008)
        return
    await ws.accept()
    loop = asyncio.get_running_loop()
    try:
        while True:
            msg = await ws.receive_json()
            try:
                step = Step(**msg)
            except Exception:
                continue
            await loop.run_in_executor(None, dispatch.dispatch, pid, step, window_id)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
