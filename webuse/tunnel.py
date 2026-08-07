"""Named Cloudflare tunnel, so the public URL is a stable hostname you own.

`cloudflared tunnel run <name>` dials out to Cloudflare and serves the local port
at the hostname routed to that tunnel — no inbound ports, no per-boot URL churn.
We supervise the process in a background thread, restart it with backoff if it
drops, and shut it down cleanly on exit. Failure is never fatal: the server keeps
serving locally.

Setup is a one-time, account-level thing and deliberately NOT automated here
(it mutates DNS on your zone):

    cloudflared tunnel login
    cloudflared tunnel create <name>
    cloudflared tunnel route dns <name> <hostname>
"""

import atexit
import shutil
import subprocess
import threading
import time
from pathlib import Path

CLOUDFLARED = "cloudflared"
CERT = Path.home() / ".cloudflared" / "cert.pem"
# A run that dies faster than this is a configuration error, not a network blip.
FAST_FAIL_S = 5.0


def build_argv(port: int, name: str) -> list[str]:
    """The exact cloudflared command that publishes the local port. Kept pure so
    it can be unit-checked without ever connecting."""
    return [
        CLOUDFLARED,
        "tunnel",
        "--no-autoupdate",  # don't let a self-update restart the tunnel mid-session
        "run",
        "--url", f"http://127.0.0.1:{port}",
        name,
    ]


def preflight() -> str | None:
    """Why the tunnel can't start, as a human-readable reason, or None if it
    looks ready. Checked before spawning so the failure is a clear message
    instead of a process that silently dies in the background."""
    if shutil.which(CLOUDFLARED) is None:
        return "cloudflared is not installed — brew install cloudflared"
    if not CERT.exists():
        return f"cloudflared is not logged in ({CERT} missing) — run: cloudflared tunnel login"
    return None


class Tunnel:
    """A supervised cloudflared process: one background thread respawns it with
    exponential backoff whenever it exits, until stop() is called."""

    def __init__(self, port: int, name: str):
        self.port = port
        self.name = name
        self.argv = build_argv(port, name)
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._warned = False
        self._thread = threading.Thread(target=self._run, name="cf-tunnel", daemon=True)

    def start(self) -> "Tunnel":
        self._thread.start()
        atexit.register(self.stop)
        return self

    def _run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._proc = subprocess.Popen(
                    self.argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as e:
                print(f"  ⚠ tunnel: could not launch cloudflared ({e}); serving locally only.")
                return
            # Close the race with stop(): if stop() ran between the while-check and
            # Popen, it couldn't see this child — terminate it ourselves and exit.
            if self._stop.is_set():
                self.stop()
                return
            started = time.monotonic()
            self._proc.wait()
            if self._stop.is_set():
                return
            ran_for = time.monotonic() - started
            # Say something the first time it dies instantly — otherwise a bad
            # tunnel name or missing DNS route looks like "the URL just doesn't work".
            if ran_for < FAST_FAIL_S and not self._warned:
                self._warned = True
                print(
                    f"  ⚠ tunnel: cloudflared exited after {ran_for:.1f}s. Run this to see why:\n"
                    f"      {' '.join(self.argv)}"
                )
            # A tunnel that ran a while then dropped is a transient network blip —
            # reconnect fast. One that dies immediately backs off.
            if ran_for > 30:
                backoff = 1.0
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)

    def stop(self):
        self._stop.set()
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


def start(port: int, name: str) -> Tunnel:
    """Open a supervised tunnel and return its handle (call .stop() to close; it's
    also registered for clean shutdown at process exit)."""
    return Tunnel(port, name).start()
