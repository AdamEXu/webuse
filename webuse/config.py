"""Small persisted config — the stable auth token and the Cloudflare tunnel it's
published through.

All of it must survive reboots so a shared link keeps working. Resolution order
(each): the matching env var, else a persisted file under ~/.config/webuse/.
The token is generated on first use; the tunnel settings are not — they name
things that must exist in your Cloudflare account, so an invented value would be
a broken tunnel rather than a working default.
"""

import os
import secrets
from pathlib import Path

_DIR = Path.home() / ".config" / "webuse"
_TOKEN_PATH = _DIR / "token"
_HOSTNAME_PATH = _DIR / "hostname"
_TUNNEL_PATH = _DIR / "tunnel"


def _persisted(path: Path, env_name: str, generate) -> str:
    """Return a stable secret: env override, else the persisted file, else a
    freshly generated value written private (0600) and atomically so it isn't
    world-readable and concurrent starts don't leave mismatched values."""
    env = os.environ.get(env_name)
    if env:
        return env
    try:
        existing = path.read_text().strip()
        if existing:
            try:  # tighten perms on a file written before we chmod'd (best effort)
                os.chmod(path, 0o600)
            except OSError:
                pass
            return existing
    except OSError:
        pass
    val = generate()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(val)
        os.replace(tmp, path)
        return val
    except OSError:
        # Read-only home, or another process won the race: prefer a value that
        # persisted over our in-memory one so concurrent servers agree.
        try:
            existing = path.read_text().strip()
            if existing:
                return existing
        except OSError:
            pass
        return val  # non-persistent, but stable for this process


def _read(path: Path, env_name: str) -> str | None:
    """Env var, else the persisted file, else None. No generation — see module doc."""
    env = os.environ.get(env_name)
    if env:
        return env.strip() or None
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def _write(path: Path, value: str) -> None:
    """Persist a setting so later runs (and reboots) reuse it. Best effort."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(value)
        os.replace(tmp, path)
    except OSError:
        pass


def token() -> str:
    """Return the stable token, creating and persisting one on first use."""
    return _persisted(_TOKEN_PATH, "WEBUSE_TOKEN", lambda: secrets.token_urlsafe(12))


def hostname() -> str | None:
    """The public hostname routed to this machine's tunnel (e.g. app.example.com),
    or None until it's configured."""
    return _read(_HOSTNAME_PATH, "WEBUSE_HOSTNAME")


def tunnel_name() -> str | None:
    """The Cloudflare tunnel to run, or None until it's configured."""
    return _read(_TUNNEL_PATH, "WEBUSE_TUNNEL")


def set_tunnel(host: str | None = None, name: str | None = None) -> None:
    """Persist tunnel settings passed on the command line, so the next plain
    `python main.py` publishes to the same hostname without the flags."""
    if host:
        _write(_HOSTNAME_PATH, host)
    if name:
        _write(_TUNNEL_PATH, name)
