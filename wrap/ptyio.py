"""Interactive project bash over a PTY. Output is a byte stream for xterm.js."""

from __future__ import annotations

import fcntl
import os
import select
import signal
import struct
import subprocess
import termios
import threading
import time
from pathlib import Path
from shutil import which

PROC_DIR = Path(os.environ.get("WRAP_STATE", "/var/lib/wrap/state.json")).resolve().parent / "console"
BUF_CAP = 256 * 1024

_lock = threading.Lock()
_ptys: dict[str, "_Pty"] = {}


def _winsize(cols: int, rows: int) -> bytes:
    cols = max(20, min(int(cols), 400))
    rows = max(8, min(int(rows), 120))
    return struct.pack("HHHH", rows, cols, 0, 0)


def _clamp(cols: int, rows: int) -> tuple[int, int]:
    return max(20, min(int(cols), 400)), max(8, min(int(rows), 120))


class _Pty:
    def __init__(self, sid: str, cwd: Path, cols: int, rows: int) -> None:
        self.sid = sid
        self.cwd = cwd
        self.cols, self.rows = _clamp(cols, rows)
        self.master: int | None = None
        self.proc: subprocess.Popen[bytes] | None = None
        self.cv = threading.Condition()
        self.buf = bytearray()
        self.dropped = 0
        self.written = 0
        self.alive = True
        self._reader: threading.Thread | None = None

    def start(self) -> None:
        bash = which("bash") or "/bin/bash"
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, _winsize(self.cols, self.rows))
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        env["WRAP_SESSION_ID"] = self.sid
        env["WRAP_URL"] = f"http://127.0.0.1:{os.environ.get('WRAP_PORT', '3000')}"
        def _child() -> None:
            os.setsid()
            try:
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)
            except OSError:
                pass

        try:
            self.proc = subprocess.Popen(
                [bash, "-il"],
                cwd=str(self.cwd),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
                close_fds=True,
                preexec_fn=_child,
            )
        except Exception:
            os.close(master)
            os.close(slave)
            raise
        os.close(slave)
        self.master = master
        fl = fcntl.fcntl(master, fcntl.F_GETFL)
        fcntl.fcntl(master, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        fcntl.ioctl(master, termios.TIOCSWINSZ, _winsize(self.cols, self.rows))
        PROC_DIR.mkdir(parents=True, exist_ok=True)
        (PROC_DIR / f"{self.sid}.pid").write_text(f"{self.proc.pid}\n")
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name=f"wrap-pty-{self.sid}")
        self._reader.start()

    def _read_loop(self) -> None:
        fd = self.master
        if fd is None:
            self._mark_dead()
            return
        while self.alive:
            try:
                ready, _, _ = select.select([fd], [], [], 0.25)
            except (OSError, ValueError):
                break
            if not ready:
                if self.proc is not None and self.proc.poll() is not None:
                    break
                continue
            try:
                data = os.read(fd, 8192)
            except BlockingIOError:
                continue
            except OSError:
                break
            if not data:
                if self.proc is not None and self.proc.poll() is not None:
                    break
                time.sleep(0.05)
                continue
            with self.cv:
                self.buf.extend(data)
                self.written += len(data)
                extra = len(self.buf) - BUF_CAP
                if extra > 0:
                    del self.buf[:extra]
                    self.dropped += extra
                self.cv.notify_all()
        self._mark_dead()

    def _mark_dead(self) -> None:
        self.alive = False
        self._reap()
        self._close_master()
        pid_path = PROC_DIR / f"{self.sid}.pid"
        try:
            pid_path.unlink()
        except OSError:
            pass
        with self.cv:
            self.cv.notify_all()

    def _close_master(self) -> None:
        fd = self.master
        self.master = None
        if fd is None:
            return
        try:
            os.close(fd)
        except OSError:
            pass

    def _reap(self) -> None:
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            return
        try:
            proc.wait(timeout=0.2)
        except Exception:
            pass

    def since(self, pos: int) -> tuple[int, bytes]:
        with self.cv:
            if pos < self.dropped:
                pos = self.dropped
            data = bytes(self.buf[pos - self.dropped :])
            return self.written, data

    def wait(self, pos: int, timeout: float) -> int:
        deadline = time.time() + timeout
        with self.cv:
            while self.written == pos and self.alive:
                remain = deadline - time.time()
                if remain <= 0:
                    break
                self.cv.wait(timeout=remain)
            return self.written

    def write(self, data: bytes) -> None:
        fd = self.master
        if not self.alive or fd is None or not data:
            return
        try:
            os.write(fd, data)
        except OSError:
            self._mark_dead()

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = _clamp(cols, rows)
        fd = self.master
        if fd is None:
            return
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, _winsize(self.cols, self.rows))
        except OSError:
            pass

    def kill(self) -> None:
        self.alive = False
        proc = self.proc
        if proc is not None and proc.poll() is None:
            pid = proc.pid
            for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(pid, sig)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        os.kill(pid, sig)
                    except (ProcessLookupError, PermissionError, OSError):
                        break
                if sig != signal.SIGKILL:
                    try:
                        proc.wait(timeout=0.4)
                    except subprocess.TimeoutExpired:
                        continue
                    break
        self._close_master()
        if proc is not None:
            try:
                proc.wait(timeout=0.5)
            except Exception:
                pass
        pid_path = PROC_DIR / f"{self.sid}.pid"
        try:
            pid_path.unlink()
        except OSError:
            pass
        with self.cv:
            self.cv.notify_all()


def _kill_pid(pid: int) -> None:
    for sig in (signal.SIGHUP, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                return
        if sig == signal.SIGHUP:
            time.sleep(0.15)


def reap_orphans() -> None:
    """Kill bash PTYs left behind by a previous wrap process."""
    if not PROC_DIR.is_dir():
        return
    for path in PROC_DIR.glob("*.pid"):
        try:
            pid = int(path.read_text().strip() or "0")
        except (OSError, ValueError):
            pid = 0
        if pid > 1:
            _kill_pid(pid)
        try:
            path.unlink()
        except OSError:
            pass


def start(sid: str, cwd: Path, cols: int = 120, rows: int = 36) -> None:
    with _lock:
        old = _ptys.pop(sid, None)
    if old is not None:
        old.kill()
    pty = _Pty(sid, cwd, cols, rows)
    pty.start()
    with _lock:
        _ptys[sid] = pty


def _get(sid: str) -> _Pty | None:
    with _lock:
        return _ptys.get(sid)


def alive(sid: str) -> bool:
    pty = _get(sid)
    return pty is not None and pty.alive


def write(sid: str, data: str | bytes) -> None:
    pty = _get(sid)
    if pty is None or not pty.alive:
        raise RuntimeError("console is gone")
    raw = data.encode("utf-8") if isinstance(data, str) else data
    pty.write(raw)


def resize(sid: str, cols: int, rows: int) -> None:
    pty = _get(sid)
    if pty is None or not pty.alive:
        raise RuntimeError("console is gone")
    pty.resize(cols, rows)


def since(sid: str, pos: int) -> tuple[int, bytes]:
    pty = _get(sid)
    if pty is None:
        return pos, b""
    return pty.since(pos)


def wait(sid: str, pos: int, timeout: float = 0.4) -> int:
    pty = _get(sid)
    if pty is None:
        return pos
    return pty.wait(pos, timeout)


def kill(sid: str) -> None:
    with _lock:
        pty = _ptys.pop(sid, None)
    if pty is not None:
        pty.kill()


def kill_all() -> None:
    with _lock:
        items = list(_ptys.items())
        _ptys.clear()
    for _, pty in items:
        pty.kill()
