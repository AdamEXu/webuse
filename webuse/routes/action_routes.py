"""Action API: run one or a batch of steps (/api/action), raise an app
(/api/activate), and permission status/request (/api/permissions*)."""

import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .. import ax, dispatch, dom, input, watch, windows
from ..models import Action, Activate, Step

router = APIRouter()


@router.get("/api/permissions")
def permissions():
    """Current permission status as JSON, for the SPA's permission picker."""
    return {"trusted": ax.trusted(), "screen_recording": ax.screen_recording()}


@router.post("/api/permissions/request")
def permissions_request():
    ax.request_permissions()
    return {"trusted": ax.trusted(), "screen_recording": ax.screen_recording()}


@router.post("/api/activate")
def activate(a: Activate):
    """Raise the app to the foreground so HID events reach it, and (if a window is
    addressed) raise that window. Called on page load (one app at a time) and
    whenever the stage takes focus."""
    input.activate(a.pid)
    windows.raise_window(a.pid, a.window_id)
    return {"ok": True, "pid": a.pid, "window_id": a.window_id}


@router.post("/api/action")
def action(a: Action):
    steps = a.actions or (
        [Step(
            action=a.action, id=a.id, value=a.value, x=a.x, y=a.y, x2=a.x2, y2=a.y2,
            dx=a.dx, dy=a.dy, code=a.code, down=a.down, shift=a.shift, ctrl=a.ctrl,
            alt=a.alt, meta=a.meta, activate=a.activate,
        )]
        if a.action
        else []
    )
    if not steps:
        return JSONResponse({"ok": False, "error": "no action(s) given"}, status_code=400)
    results: list[dict] = []
    for i, s in enumerate(steps):
        if i:
            time.sleep(0.08)  # let the app react between steps
        r = dispatch.dispatch(a.pid, s, a.window_id)
        results.append(r)
        if not r.get("ok"):
            break  # stop at the first failure; its index = len(results) - 1
    watch.kick(a.pid, a.window_id)  # push a fresh ghost DOM to SSE clients right away
    ok = all(r.get("ok") for r in results)
    out: dict = {"ok": ok, "results": results}
    if not ok:
        out["failed_step"] = len(results) - 1
        if results[-1].get("code"):  # the failing step's machine-readable code
            out["code"] = results[-1]["code"]
    try:  # inline post-action DOM so agents skip the observe round trip
        snap = ax.snapshot(a.pid, a.window_id)
        out["dom"] = {
            "window": snap["window"],
            "truncated": snap["truncated"],
            "nodes": dom.build_nodes(snap),
        }
    except LookupError as e:
        out["dom"] = None
        out["dom_error"] = str(e)
    return out
