"""Native tinyfi.sh reverse-tunnel so `uv run python main.py` is publicly
reachable with no extra steps.

tinyfi.sh is a free SSH reverse-tunnel service: an `ssh -R <sub>:80:localhost:<port>`
to tinyfi.sh publishes the local port at https://<sub>.tinyfi.sh. We spawn that
ssh in a background thread, auto-reconnect with backoff if it drops, and shut it
down cleanly on exit. Failure is non-fatal — the server keeps serving locally.
"""

import atexit
import subprocess
import threading
import time

HOST = "tinyfi.sh"


def build_argv(port: int, subdomain: str) -> list[str]:
    """The exact ssh command that opens the reverse tunnel. Kept pure so it can be
    unit-checked without ever connecting."""
    return [
        "ssh",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "ExitOnForwardFailure=yes",
        "-N",  # no remote command: forward only
        "-R", f"{subdomain}:80:localhost:{port}",
        HOST,
    ]


class Tunnel:
    """A supervised ssh reverse tunnel: one background thread respawns ssh with
    exponential backoff whenever it exits, until stop() is called."""

    def __init__(self, port: int, subdomain: str):
        self.port = port
        self.subdomain = subdomain
        self.argv = build_argv(port, subdomain)
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._thread = threading.Thread(target=self._run, name="tinyfi-tunnel", daemon=True)

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
                print(f"  ⚠ tunnel: could not launch ssh ({e}); serving locally only.")
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
            # A tunnel that ran a while then dropped is a transient network blip —
            # reconnect fast. One that dies immediately (bad auth/host) backs off.
            if time.monotonic() - started > 30:
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


def start(port: int, subdomain: str) -> Tunnel:
    """Open a supervised reverse tunnel and return its handle (call .stop() to
    close; it's also registered for clean shutdown at process exit)."""
    return Tunnel(port, subdomain).start()
