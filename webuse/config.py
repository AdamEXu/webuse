"""Small persisted config — the stable auth token and tunnel subdomain.

Both must survive reboots so a shared tunnel link keeps working.
Resolution order (each): the matching env var, else a persisted file under
~/.config/webuse/, else generate once and write it there.
"""

import os
import secrets
from pathlib import Path

_DIR = Path.home() / ".config" / "webuse"
_TOKEN_PATH = _DIR / "token"
_SUBDOMAIN_PATH = _DIR / "subdomain"


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


def token() -> str:
    """Return the stable token, creating and persisting one on first use."""
    return _persisted(_TOKEN_PATH, "WEBUSE_TOKEN", lambda: secrets.token_urlsafe(12))


def subdomain() -> str:
    """Return the stable per-machine tunnel subdomain (short, url-safe), creating
    and persisting one on first use, so the public URL is stable across boots."""
    # 6 lowercase-alnum chars: valid DNS label, short, effectively collision-free.
    gen = lambda: "w" + secrets.token_hex(3)  # noqa: E731
    return _persisted(_SUBDOMAIN_PATH, "WEBUSE_SUBDOMAIN", gen)
