"""Terminal API: a persistent shell per session name, driven by writes and read
back as rendered text. Sessions are created on demand, so the whole surface an
agent needs is POST /api/term/main/input."""

import asyncio
import json
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse

from .. import terminal
from ..models import TermInput, TermResize

router = APIRouter()


async def _ready(s: terminal.Session, timeout: float = 3.0) -> None:
    """Wait for a newly spawned shell to finish drawing its banner and prompt.
    Writing before that races the line editor's redraw, which strands the echoed
    command above the prompt. Once satisfied it's remembered on the session, so
    only the very first call on a shell ever pays this."""
    if s.prompt_seen:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if s.version > 0 and time.monotonic() - s.last_output >= 0.15:
            break
        await asyncio.sleep(0.02)
    s.prompt_seen = True


async def _settle(s: terminal.Session, quiet_ms: int, timeout_ms: int) -> bool:
    """Wait until the shell has produced no output for `quiet_ms`. Returns False
    on timeout — the command is still running, the caller just stopped waiting
    (poll GET /api/term/{sid} for the rest, or send Ctrl-C)."""
    quiet = max(0, min(quiet_ms, 5_000)) / 1000
    deadline = time.monotonic() + max(0, min(timeout_ms, 120_000)) / 1000
    await asyncio.sleep(min(0.06, quiet))  # let the write reach the shell first
    while time.monotonic() < deadline:
        if time.monotonic() - s.last_output >= quiet:
            return True
        await asyncio.sleep(0.03)
    return False


@router.get("/api/term")
def term_list():
    """Live sessions, plus the key names /input accepts."""
    return {"sessions": terminal.list_sessions(), "keys": sorted(terminal.KEYS)}


@router.get("/api/term/{sid}")
async def term_get(sid: str):
    """The session's rendered transcript. Creates the session if it's new, so a
    first read shows a fresh shell prompt rather than a 404."""
    s = terminal.get(sid)
    await _ready(s)
    return s.snapshot()


@router.post("/api/term/{sid}/input")
async def term_input(sid: str, body: TermInput):
    """Write to the shell: literal text, a named key, or text + Return. By
    default the reply waits for output to settle and carries the resulting
    screen, so an agent types a command and reads its output in one round trip."""
    s = terminal.get(sid)
    await _ready(s)
    if body.key is not None and body.key not in terminal.KEYS:
        return JSONResponse(
            {"ok": False, "error": f"unknown key {body.key!r}", "code": "unknown_key",
             "keys": sorted(terminal.KEYS)},
            status_code=400,
        )
    if body.value:
        s.write(body.value)
    if body.key:
        s.key(body.key)
    if body.enter:
        s.key("Enter")
    settled = await _settle(s, body.quiet_ms, body.timeout_ms) if body.wait else None
    out = s.snapshot()
    out["ok"] = True
    if settled is not None:
        # settled=False => still producing output when we stopped waiting. Note
        # this tracks OUTPUT, not completion: a silent `sleep 30` settles at once.
        # "busy" (in the snapshot) is the one that says a command is still running.
        out["settled"] = settled
    return out


@router.post("/api/term/{sid}/resize")
def term_resize(sid: str, body: TermResize):
    s = terminal.get(sid)
    s.resize(body.cols, body.rows)
    return s.snapshot()


@router.delete("/api/term/{sid}")
def term_close(sid: str):
    """Kill the shell. The name stays usable — the next call opens a fresh one."""
    return {"ok": terminal.close(sid)}


@router.get("/api/term/{sid}/events")
async def term_events(sid: str):
    """SSE: push the transcript whenever the shell writes something. Polled
    against the session's version counter — the emulator screen is shared
    mutable state, so there's no per-subscriber queue to drain."""
    s = terminal.get(sid)

    async def gen():
        last = -1
        idle_since = time.monotonic()
        while True:
            if s.version != last:
                last = s.version
                idle_since = time.monotonic()
                yield f"data: {json.dumps(s.snapshot())}\n\n"
            elif time.monotonic() - idle_since > 15:
                idle_since = time.monotonic()
                yield ": ping\n\n"
            await asyncio.sleep(0.05)

    return StreamingResponse(gen(), media_type="text/event-stream")
