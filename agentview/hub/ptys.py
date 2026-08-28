"""PTY sessions behind the terminal view.

`tmux attach` needs a real controlling terminal, not a pipe, so each attach runs under
pty.fork(). The hub owns these rather than the collector because the hub is the thing
the browser can reach -- collectors dial out and never listen.

Output is fanned out to any number of viewers and also kept in a scrollback ring, so a
browser that connects late still sees the current screen instead of a blank pane.

Stdlib only. POSIX only -- Windows would need pywinpty; the UI degrades honestly there.
"""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import signal
import struct
import termios
import threading
import time
from typing import Callable, Dict, List, Optional

#: Bytes of scrollback replayed to a newly-connected viewer.
SCROLLBACK_BYTES = 256 * 1024

#: A session with no viewers for this long is torn down.
IDLE_TIMEOUT = 300.0

READ_CHUNK = 65536


class PtySession:
    def __init__(self, key: str, argv: List[str], cols: int = 120, rows: int = 32) -> None:
        self.key = key
        self.argv = list(argv)
        self.cols = cols
        self.rows = rows
        self.pid: Optional[int] = None
        self.fd: Optional[int] = None
        self.exit_status: Optional[int] = None
        #: Set once the child has been waited on. `os.kill(pid, 0)` succeeds for a
        #: zombie, so liveness cannot be decided from the pid alone.
        self.exited = False
        self.started_at = time.time()
        self.last_viewer_at = time.time()

        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._subscribers: Dict[int, Callable[[bytes], None]] = {}
        self._next_sub_id = 1
        self._reader: Optional[threading.Thread] = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        pid, fd = pty.fork()
        if pid == 0:
            # Child. Nothing here may raise back into the parent, so any failure
            # exits the forked process directly.
            try:
                env = dict(os.environ)
                env.setdefault("TERM", "xterm-256color")
                # A nested tmux client must not inherit the server's own session
                # context, or it refuses with "sessions should be nested with care".
                env.pop("TMUX", None)
                os.execvpe(self.argv[0], self.argv, env)
            except BaseException:
                os._exit(127)

        self.pid = pid
        self.fd = fd
        self.resize(self.cols, self.rows)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        while True:
            # Read the descriptor through a local: close() sets self.fd to None from
            # another thread, and `os.read(None, ...)` raises TypeError, which is not
            # an OSError. That killed this thread outright, so _reap() never ran and
            # the child was left a zombie forever.
            fd = self.fd
            if fd is None:
                break
            try:
                data = os.read(fd, READ_CHUNK)
            except OSError as exc:
                # EIO is the normal signal that the child closed the terminal.
                if exc.errno not in (errno.EIO, errno.EBADF):
                    pass
                break
            if not data:
                break
            self._publish(data)
        self._reap()

    def _publish(self, data: bytes) -> None:
        with self._lock:
            self._buffer.extend(data)
            if len(self._buffer) > SCROLLBACK_BYTES:
                del self._buffer[: len(self._buffer) - SCROLLBACK_BYTES]
            subscribers = list(self._subscribers.values())
        for send in subscribers:
            try:
                send(data)
            except Exception:  # noqa: BLE001 - a dead viewer must not stop the others
                pass

    def _reap(self) -> None:
        """Wait for the child properly, or it stays a zombie and looks alive forever.

        The read loop ends on EIO, which the terminal reports as soon as the child
        closes it -- which can be *before* the child has finished exiting. A WNOHANG
        wait at that moment returns 0, collects nothing, and leaves a zombie that
        `os.kill(pid, 0)` happily reports as alive. The session is then never
        restarted: reconnecting replays the dead session's scrollback instead of
        opening a new terminal.
        """
        if self.pid:
            try:
                _, status = os.waitpid(self.pid, 0)
                self.exit_status = status
            except OSError:
                pass  # already reaped, or never ours to reap
            self.exited = True
        self._publish(b"\r\n[agentview] terminal session ended\r\n")

    # -- io ---------------------------------------------------------------

    def scrollback(self) -> bytes:
        with self._lock:
            return bytes(self._buffer)

    def subscribe(self, send: Callable[[bytes], None]) -> int:
        with self._lock:
            sub_id = self._next_sub_id
            self._next_sub_id += 1
            self._subscribers[sub_id] = send
            self.last_viewer_at = time.time()
        return sub_id

    def unsubscribe(self, sub_id: int) -> None:
        with self._lock:
            self._subscribers.pop(sub_id, None)
            self.last_viewer_at = time.time()

    @property
    def viewers(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def write(self, data: bytes) -> None:
        if self.fd is None:
            return
        try:
            os.write(self.fd, data)
        except OSError:
            pass

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = max(2, int(cols)), max(2, int(rows))
        if self.fd is None:
            return
        try:
            packed = struct.pack("HHHH", self.rows, self.cols, 0, 0)
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, packed)
        except OSError:
            pass

    @property
    def alive(self) -> bool:
        if self.pid is None or self.exited:
            return False
        try:
            os.kill(self.pid, 0)
        except OSError:
            return False
        return True

    def close(self) -> None:
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGHUP)
            except OSError:
                pass
            self.exited = True
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


class PtyManager:
    def __init__(self, idle_timeout: float = IDLE_TIMEOUT) -> None:
        self._sessions: Dict[str, PtySession] = {}
        self._lock = threading.Lock()
        self.idle_timeout = idle_timeout

    def get_or_start(self, key: str, argv: List[str], cols: int, rows: int) -> PtySession:
        with self._lock:
            session = self._sessions.get(key)
            if session is not None and session.alive:
                return session
            if session is not None:
                session.close()
            session = PtySession(key, argv, cols=cols, rows=rows)
            self._sessions[key] = session
        session.start()
        return session

    def get(self, key: str) -> Optional[PtySession]:
        with self._lock:
            return self._sessions.get(key)

    def close(self, key: str) -> None:
        with self._lock:
            session = self._sessions.pop(key, None)
        if session:
            session.close()

    def reap_idle(self) -> None:
        now = time.time()
        with self._lock:
            stale = [
                key
                for key, s in self._sessions.items()
                if (not s.alive) or (s.viewers == 0 and now - s.last_viewer_at > self.idle_timeout)
            ]
            sessions = [self._sessions.pop(k) for k in stale]
        for session in sessions:
            session.close()

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()
