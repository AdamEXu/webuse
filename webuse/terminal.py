"""Persistent PTY terminal sessions — the non-GUI escape hatch.

A real login shell behind a pseudo-terminal, so cwd, env, activated venvs and
shell history persist across commands and interactive programs (REPLs, ssh,
prompts, full-screen TUIs) work. Raw shell output is fed to a terminal emulator
(pyte) that renders ANSI into a character grid plus scrollback, so callers read
plain text instead of escape codes.

One reader thread per session, mirroring watch.py/apps.py's threading stance.
Sessions are created on demand by name, so a client can POST straight to
/api/term/main/input with no create-session round trip.
"""

import fcntl
import os
import pty
import signal
import struct
import subprocess
import termios
import threading
import time

import pyte

DEFAULT_COLS, DEFAULT_ROWS = 100, 30
HISTORY_LINES = 2000   # scrollback retained by the emulator
MAX_TEXT_LINES = 600   # cap on the transcript handed back to callers
READ_CHUNK = 65536
DEFAULT_SESSION = "main"

# Control bytes for keys a browser client can't otherwise deliver. Named, not
# raw, so the client sends {"key": "Ctrl-C"} instead of encoding \x03 itself.
KEYS: dict[str, str] = {
    "Enter": "\r",
    "Tab": "\t",
    "Escape": "\x1b",
    "Backspace": "\x7f",
    "Up": "\x1b[A",
    "Down": "\x1b[B",
    "Right": "\x1b[C",
    "Left": "\x1b[D",
    "Home": "\x1b[H",
    "End": "\x1b[F",
    "PageUp": "\x1b[5~",
    "PageDown": "\x1b[6~",
    "Ctrl-C": "\x03",   # interrupt the foreground command
    "Ctrl-D": "\x04",   # EOF / exit the shell or REPL
    "Ctrl-Z": "\x1a",   # suspend
    "Ctrl-L": "\x0c",   # clear
    "Ctrl-A": "\x01",
    "Ctrl-E": "\x05",
    "Ctrl-U": "\x15",
    "Ctrl-K": "\x0b",
    "Ctrl-W": "\x17",
    "Ctrl-R": "\x12",
}


def _child_setup() -> None:
    """Run in the forked child: become a session leader and adopt the pty slave
    (fd 0) as the controlling terminal. Without a controlling terminal the shell
    disables job control and ^C never reaches the foreground command."""
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _render(line, cols: int) -> str:
    """One emulator line -> str. Lines are sparse char maps defaulting to blank,
    so index every column rather than iterating the populated keys."""
    return "".join(line[x].data for x in range(cols))


class Session:
    """One shell + pty + emulator screen, with a reader thread pumping output."""

    def __init__(self, sid: str, cols: int, rows: int, cwd: str | None = None):
        self.id = sid
        self.cols, self.rows = cols, rows
        self.version = 0          # bumped on every output chunk; lets clients poll for change
        self.last_output = time.monotonic()
        # Cleared until the shell has finished drawing its startup banner + first
        # prompt. Writing before that races the redraw and garbles the screen, so
        # the first write waits on it; every later write skips the wait.
        self.prompt_seen = False
        self._lock = threading.Lock()

        self.screen = pyte.HistoryScreen(cols, rows, history=HISTORY_LINES, ratio=0.5)
        self.screen.set_mode(pyte.modes.LNM)
        self.stream = pyte.ByteStream(self.screen)  # decodes partial UTF-8 across chunks

        self.master, slave = pty.openpty()
        self._set_winsize(cols, rows)
        env = dict(os.environ)
        env.update(TERM="xterm-256color", COLUMNS=str(cols), LINES=str(rows))
        # A login shell so PATH/rc files match what the user gets in a real terminal.
        shell = env.get("SHELL") or "/bin/zsh"
        self.proc = subprocess.Popen(
            [shell, "-l"],
            stdin=slave, stdout=slave, stderr=slave,
            cwd=cwd or os.path.expanduser("~"),
            env=env,
            preexec_fn=_child_setup,
            close_fds=True,
        )
        os.close(slave)  # the child owns it now; our copy would block EOF detection
        self._reader = threading.Thread(target=self._pump, name=f"term-{sid}", daemon=True)
        self._reader.start()

    # -- output ------------------------------------------------------------

    def _pump(self) -> None:
        """Drain the pty until the shell exits. A closed slave surfaces as EIO on
        macOS (not b""), so treat both as end-of-stream."""
        while True:
            try:
                data = os.read(self.master, READ_CHUNK)
            except OSError:
                break
            if not data:
                break
            with self._lock:
                self.stream.feed(data)
                self.version += 1
                self.last_output = time.monotonic()
        with self._lock:
            self.version += 1  # wake pollers waiting on the dead session

    # -- input -------------------------------------------------------------

    def write(self, data: str) -> None:
        if not data or not self.alive:
            return
        os.write(self.master, data.encode())

    def key(self, name: str) -> bool:
        seq = KEYS.get(name)
        if seq is None:
            return False
        self.write(seq)
        return True

    def resize(self, cols: int, rows: int) -> None:
        cols, rows = max(20, min(cols, 400)), max(5, min(rows, 200))
        with self._lock:
            self.cols, self.rows = cols, rows
            self.screen.resize(rows, cols)
        self._set_winsize(cols, rows)

    def _set_winsize(self, cols: int, rows: int) -> None:
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    # -- state -------------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    @property
    def busy(self) -> bool:
        """True while a command (not the shell) owns the terminal. Read from the
        pty's foreground process group: the shell is a session leader, so its
        pgid is its pid, and anything else in front means a command is running.
        Quiet output alone can't tell `sleep 30` apart from a finished command."""
        try:
            return os.tcgetpgrp(self.master) != self.proc.pid
        except OSError:
            return False

    def snapshot(self) -> dict:
        """Current transcript + viewport. `text` is scrollback then the live
        screen — what a human sees after scrolling up — capped to the tail.
        `screen` is just the viewport, which is what full-screen TUIs redraw."""
        with self._lock:
            cols = self.cols
            hist = [_render(ln, cols) for ln in self.screen.history.top]
            view = list(self.screen.display)
            cursor = [self.screen.cursor.y, self.screen.cursor.x]
            version, idle = self.version, time.monotonic() - self.last_output
        lines = hist + view
        while lines and not lines[-1].strip():  # drop the blank tail below the cursor
            lines.pop()
        lines = [ln.rstrip() for ln in lines][-MAX_TEXT_LINES:]
        return {
            "id": self.id,
            "text": "\n".join(lines),
            "screen": [ln.rstrip() for ln in view],
            "cursor": cursor,
            "cols": cols,
            "rows": self.rows,
            "version": version,
            "idle_ms": round(idle * 1000),
            "busy": self.busy,
            "alive": self.alive,
            "exit_code": self.proc.returncode,
        }

    def close(self) -> None:
        if self.alive:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGHUP)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            os.close(self.master)
        except OSError:
            pass


# -- registry --------------------------------------------------------------

_sessions: dict[str, Session] = {}
_reg_lock = threading.Lock()


def get(sid: str = DEFAULT_SESSION, *, create: bool = True, cwd: str | None = None,
        cols: int = DEFAULT_COLS, rows: int = DEFAULT_ROWS) -> Session | None:
    """Fetch a session, creating it on demand (the common path — an agent can
    POST to a session name that has never existed). A session whose shell has
    exited is replaced, so `exit`ing the shell doesn't wedge the name."""
    with _reg_lock:
        s = _sessions.get(sid)
        if s is not None and s.alive:
            return s
        if s is not None:
            s.close()
            del _sessions[sid]
        if not create:
            return None
        s = Session(sid, cols, rows, cwd)
        _sessions[sid] = s
        return s


def close(sid: str) -> bool:
    with _reg_lock:
        s = _sessions.pop(sid, None)
    if s is None:
        return False
    s.close()
    return True


def list_sessions() -> list[dict]:
    with _reg_lock:
        sessions = list(_sessions.values())
    return [{"id": s.id, "alive": s.alive, "cols": s.cols, "rows": s.rows} for s in sessions]


def shutdown() -> None:
    """Kill every shell — called on server shutdown so no orphan shells linger."""
    with _reg_lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for s in sessions:
        s.close()
