"""FastAPI app: auth/CSRF middleware, the app catalog + per-app + action routers,
and a static SPA (webuse/frontend/dist) served with client-side-routing fallback.

Thin by design — all logic lives in cohesive modules:
  security.py    auth/CSRF gate + middleware + ws auth
  models.py      Step/Action/Activate
  dispatch.py    the HID action engine
  terminal.py    persistent PTY shell sessions
  routes/        per-app endpoints + action API + terminal API
  apps.py        installed/running-app catalog + launcher
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import apps, security, terminal
from .routes import action_routes, app_routes, term_routes
# Re-export so `from webuse.server import TOKEN, app` and `server.mark_tunnel_active()`
# (used by main.py) keep working after the split.
from .security import TOKEN, mark_tunnel_active  # noqa: F401

@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    terminal.shutdown()  # reap the shells so stopping the server leaves no orphans


app = FastAPI(title="webuse", lifespan=lifespan)
security.add_auth_middleware(app)

# Order matters: the specific API + per-app routers are registered BEFORE the SPA
# catch-all, so /api/* and /app/{handle}/(windows|icon|menu|dom|events|stream|
# stream_ws|ws) always win over the client-side-routing fallback below.
app.include_router(apps.router)
app.include_router(action_routes.router)
app.include_router(term_routes.router)
app.include_router(app_routes.router)


# -- SPA (React build) -------------------------------------------------------
#
# The frontend builds to webuse/frontend/dist. A parallel task owns it and it may
# not exist yet at import time — every filesystem touch is guarded so a missing
# dist never breaks import or any route.

_DIST = Path(__file__).resolve().parent / "frontend" / "dist"
_INDEX = _DIST / "index.html"
_NOT_BUILT = "frontend not built - run: cd webuse/frontend && pnpm install && pnpm build"

# Hashed build assets. check_dir=False so a missing dist doesn't crash the mount at
# import; requests just 404 until the build exists.
app.mount("/assets", StaticFiles(directory=str(_DIST / "assets"), check_dir=False), name="assets")


@app.get("/{path:path}")
def spa(path: str):
    """Serve a real dist file when one matches (favicon, manifest, …), else fall
    back to index.html for client-side routing (/, /app/{handle}, anything else).
    Registered LAST so it can't shadow the API/app sub-routes above."""
    if _DIST.is_dir():
        root = _DIST
        candidate = (root / path).resolve()
        if path and str(candidate).startswith(str(root) + os.sep) and candidate.is_file():
            return FileResponse(str(candidate))
        if _INDEX.is_file():
            return FileResponse(str(_INDEX))
    return PlainTextResponse(_NOT_BUILT, status_code=503)
