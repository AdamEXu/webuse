"""Auth + CSRF: token/localhost/cookie gate, same-origin checks, the HTTP auth
middleware, and the WebSocket auth helper. All request-trust logic lives here."""

import secrets
from urllib.parse import urlparse

from fastapi import Request, WebSocket
from fastapi.responses import PlainTextResponse, RedirectResponse

from . import config

TOKEN = config.token()  # stable across boots (env / persisted file / generated once)


# Headers a reverse proxy / tunnel injects. cloudflared (the documented tunnel)
# proxies from loopback and may rewrite Host to 127.0.0.1, so the socket peer and
# Host header alone can't prove a request is direct-local — the presence of any
# forwarding header reliably marks it as proxied, so it must carry the token.
_PROXY_HEADERS = (
    "x-forwarded-for", "forwarded", "x-real-ip", "x-forwarded-host",
    "x-forwarded-proto", "cf-connecting-ip", "via",
)


# Set once a public tunnel (tinyfi.sh) is open. A raw `ssh -R` forward delivers
# every remote request to localhost with an attacker-controllable Host header and
# no proxy markers, so it is indistinguishable from a direct local request — the
# ONLY safe rule while exposed is: trust nothing without the token. main.py calls
# mark_tunnel_active() after starting the tunnel.
_tunnel_active = False


def mark_tunnel_active() -> None:
    global _tunnel_active
    _tunnel_active = True


def _is_local(request: Request) -> bool:
    """True only for direct loopback requests. A tunnel proxies via 127.0.0.1, so
    the socket peer alone proves nothing — require a loopback Host header and no
    proxy-forwarding headers. When a public tunnel is open, localhost trust is
    disabled outright (a raw ssh -R forward can forge all of those signals)."""
    if _tunnel_active:
        return False
    client = request.client.host if request.client else ""
    if client not in ("127.0.0.1", "::1"):
        return False
    if any(h in request.headers for h in _PROXY_HEADERS):
        return False
    host = request.headers.get("host", "").lower()
    if host.startswith("["):  # bracketed IPv6, maybe with :port
        host = host[1 : host.index("]")] if "]" in host else host
    elif host.count(":") == 1:  # hostname/IPv4 with :port
        host = host.split(":")[0]
    return host in ("127.0.0.1", "localhost", "::1")


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _token_ok(supplied: str | None) -> bool:
    return supplied is not None and secrets.compare_digest(supplied, TOKEN)


def _same_origin(request: "Request | WebSocket") -> bool:
    """Reject browser cross-site requests so a malicious page can't drive the
    local server via /api/action (which otherwise skips the token for localhost).
    Trust Sec-Fetch-Site when present; else compare the Origin host to Host.
    A non-browser client (no Origin, no Sec-Fetch-Site — curl, the agent) sends
    neither and carries no ambient cookies, so it isn't a CSRF vector."""
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site in ("same-origin", "none")
    origin = request.headers.get("origin")
    if not origin:
        return True
    return urlparse(origin).netloc.lower() == request.headers.get("host", "").lower()


def add_auth_middleware(app) -> None:
    """Register the HTTP auth + CSRF middleware on `app`. Semantics identical to
    the original server.py handler: token/localhost/cookie gate, cross-site
    rejection for unsafe methods, and the ?token= -> redirect+set-cookie flow."""

    @app.middleware("http")
    async def auth(request: Request, call_next):
        qtoken = request.query_params.get("token")
        authed = (
            _is_local(request)
            or _token_ok(qtoken)
            or _token_ok(request.cookies.get("webuse_token"))
        )
        if not authed:
            return PlainTextResponse("unauthorized: append ?token=<token>", status_code=401)
        # CSRF: even a trusted-local request must not be driven cross-site by a webpage.
        if request.method not in _SAFE_METHODS and not _same_origin(request):
            return PlainTextResponse("forbidden: cross-site request", status_code=403)
        # A valid ?token= in the URL: set the cookie and redirect to a clean URL so the
        # secret doesn't linger in history, logs, or Referer. Safe methods only, so a
        # POST body is never dropped by the redirect.
        if qtoken is not None and _token_ok(qtoken) and request.method in _SAFE_METHODS:
            resp = RedirectResponse(str(request.url.remove_query_params("token")), status_code=303)
            resp.set_cookie("webuse_token", TOKEN, httponly=True, samesite="strict")
            return resp
        return await call_next(request)


def ws_authed(ws: WebSocket) -> bool:
    tok = ws.query_params.get("token") or ws.cookies.get("webuse_token")
    if _token_ok(tok):
        return True
    if _tunnel_active:  # exposed: token required, no localhost trust (see _is_local)
        return False
    client = ws.client.host if ws.client else ""
    if client not in ("127.0.0.1", "::1") or any(h in ws.headers for h in _PROXY_HEADERS):
        return False
    # Browsers don't apply CORS to WebSockets, so a loopback socket alone can't prove
    # same-site: any page the victim visits could open ws://127.0.0.1 and inject HID or
    # read the stream. Require same-origin (mirrors _same_origin) to block that.
    return _same_origin(ws)
