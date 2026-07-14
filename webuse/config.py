"""Small persisted config — chiefly the stable auth token.

The URL token must survive reboots so a shared tunnel link keeps working.
Resolution order: WEBUSE_TOKEN env, else a persisted file under
~/.config/webuse/token, else generate once and write it there.
"""

import os
import secrets
from pathlib import Path

_TOKEN_PATH = Path.home() / ".config" / "webuse" / "token"


def token() -> str:
    """Return the stable token, creating and persisting one on first use."""
    env = os.environ.get("WEBUSE_TOKEN")
    if env:
        return env
    try:
        existing = _TOKEN_PATH.read_text().strip()
        if existing:
            try:  # tighten perms on a file written before we chmod'd (best effort)
                os.chmod(_TOKEN_PATH, 0o600)
            except OSError:
                pass
            return existing
    except OSError:
        pass
    tok = secrets.token_urlsafe(12)
    try:
        _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Write private (0600) and atomically, so the tunnel secret isn't world-
        # readable and concurrent starts don't leave mismatched tokens.
        tmp = _TOKEN_PATH.with_suffix(f".{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(tok)
        os.replace(tmp, _TOKEN_PATH)
        return tok
    except OSError:
        # Read-only home, or another process won the race: prefer a token that
        # persisted over our in-memory one so concurrent servers agree.
        try:
            existing = _TOKEN_PATH.read_text().strip()
            if existing:
                return existing
        except OSError:
            pass
        return tok  # non-persistent, but stable for this process
