"""Codex resumable-session adapter.

Codex has no Claude-style background-agent registry.  Its closest equivalent is the
thread list used by ``codex resume``.  Current Codex releases materialize that list in
``$CODEX_HOME/state_*.sqlite``; this adapter reads it read-only and emits one idle,
resumable record per visible interactive thread.

Stdlib only.
"""

from __future__ import annotations

import os
import fcntl
import shlex
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from agentview.collector import tmux as tmux_mod
from agentview.collector.adapters.base import Adapter
from agentview.model import AgentRecord, AttachSpec, ContextRef, STATUS_BUSY, STATUS_IDLE

HARNESS = "codex"
HARNESS_LABEL = "Codex"
SESSION_PREFIX = "agentview_codex_"

#: How recently a thread must have moved for "a client has it open" to mean "it is
#: working". Codex offers no busy/idle signal of its own -- only an advisory writer
#: lock, which stays held for as long as the client is open, and a recency stamp that
#: only moves when something actually happens. Treating the lock alone as busy marked
#: every terminal left open overnight as busy, and the hub's stuck detector then
#: flagged all of them: the one number the HUD exists to report became noise.
#:
#: Matched to the hub's own stuck threshold, and for the same reason: a single long
#: turn can go many minutes without touching the stamp.
ACTIVE_WINDOW = 900.0


def default_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def _latest_state_db(home: Path) -> Optional[Path]:
    """Return the newest schema-numbered state database Codex has created."""
    candidates = []
    for path in home.glob("state_*.sqlite"):
        try:
            number = int(path.stem.rsplit("_", 1)[1])
        except (IndexError, ValueError):
            continue
        candidates.append((number, path))
    return max(candidates, default=(0, None))[1]


def thread_has_writer(home: Path, thread_id: str) -> bool:
    """Whether Codex currently holds the thread's advisory writer lock."""
    path = home / "thread-writer-locks" / (thread_id + ".lock")
    try:
        with path.open("rb") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            finally:
                try:
                    fcntl.flock(fh, fcntl.LOCK_UN)
                except OSError:
                    pass
    except OSError:
        return False
    return False


def resume_attach_argv(
    thread_id: str, codex_bin: str, use_tmux: bool
) -> Tuple[List[str], Optional[List[str]]]:
    command = [codex_bin, "resume", thread_id]
    if not use_tmux:
        return command, None

    # UUIDs contain only tmux-safe characters. A stable name means reopening the
    # viewer reconnects to the same resumed TUI instead of starting another client.
    session = SESSION_PREFIX + thread_id
    argv = ["tmux", "new-session", "-A", "-s", session] + command
    create = " ".join(
        shlex.quote(part)
        for part in (["tmux", "new-session", "-d", "-s", session] + command)
    )
    readonly = [
        "sh",
        "-c",
        "tmux has-session -t {s} 2>/dev/null || {create}; exec tmux attach -r -t {s}".format(
            s=shlex.quote(session), create=create
        ),
    ]
    return argv, readonly


class CodexAdapter(Adapter):
    name = HARNESS
    priority = 15

    def __init__(
        self,
        codex_home: Optional[Path] = None,
        which_fn: Optional[Callable[[str], Optional[str]]] = None,
        tmux_available_fn: Optional[Callable[[], bool]] = None,
        active_thread_fn: Optional[Callable[[str], bool]] = None,
        tmux_has_session_fn: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self.codex_home = Path(codex_home) if codex_home else default_codex_home()
        self._which = which_fn or shutil.which
        self._tmux_available_fn = tmux_available_fn or tmux_mod.available
        self._active_thread_fn = active_thread_fn or (
            lambda thread_id: thread_has_writer(self.codex_home, thread_id)
        )
        self._tmux_has_session_fn = tmux_has_session_fn or tmux_mod.has_session

    def available(self) -> bool:
        return _latest_state_db(self.codex_home) is not None

    def _attach_for(self, thread_id: str, active: bool) -> AttachSpec:
        session = SESSION_PREFIX + thread_id
        if active:
            # A thread first opened through agentview already has a resumable client
            # parked in our tmux session. Reattach to that client directly.
            if self._tmux_has_session_fn(session):
                return AttachSpec(
                    available=True,
                    argv=["tmux", "attach", "-t", session],
                    argv_readonly=["tmux", "attach", "-r", "-t", session],
                )
            return AttachSpec.unavailable(
                "already open in another Codex client - Codex allows one writer per session"
            )
        codex_bin = self._which("codex")
        if not codex_bin:
            return AttachSpec.unavailable("`codex` is not on PATH - cannot resume this session")
        argv, readonly = resume_attach_argv(
            thread_id, codex_bin, bool(self._tmux_available_fn())
        )
        return AttachSpec(available=True, argv=argv, argv_readonly=readonly)

    def discover(self, ctx: ContextRef) -> Tuple[List[AgentRecord], List[str]]:
        db_path = _latest_state_db(self.codex_home)
        if db_path is None:
            return [], []

        # mode=ro avoids creating WAL files or taking a writer lock in Codex's own
        # state directory. immutable=0 is intentional: we want committed WAL data.
        try:
            connection = sqlite3.connect("file:{}?mode=ro".format(db_path), uri=True)
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT id, title, name, cwd, cli_version, tokens_used, git_branch,
                       created_at_ms, updated_at_ms, recency_at_ms
                  FROM threads
                 WHERE archived = 0
                   AND preview <> ''
                   AND source = 'cli'
                   AND (thread_source IS NULL OR thread_source = 'user')
                 ORDER BY recency_at_ms DESC, id DESC
                """
            ).fetchall()
            connection.close()
        except (OSError, sqlite3.Error) as exc:
            return [], ["codex: cannot read {}: {}".format(db_path.name, exc)]

        now = time.time()
        records = []
        for row in rows:
            thread_id = str(row["id"])
            # Two separate questions: is a client holding this thread (which decides
            # whether we can resume it), and is anything actually happening in it
            # (which decides what we claim about it).
            held = self._active_thread_fn(thread_id)
            display_name = row["name"] or row["title"] or thread_id[:8]
            updated_ms = row["recency_at_ms"] or row["updated_at_ms"]
            updated_at = (updated_ms / 1000.0) if updated_ms else None
            working = bool(
                held and updated_at is not None and (now - updated_at) <= ACTIVE_WINDOW
            )
            records.append(
                AgentRecord(
                    id="{}:{}:{}".format(ctx.id, HARNESS, thread_id),
                    harness=HARNESS,
                    harness_label=HARNESS_LABEL,
                    harness_version=row["cli_version"] or None,
                    context_id=ctx.id,
                    name=str(display_name),
                    cwd=row["cwd"] or None,
                    git_branch=row["git_branch"] or None,
                    status=STATUS_BUSY if working else STATUS_IDLE,
                    detail=(
                        "working in a Codex client" if working
                        else "open in a Codex client, idle" if held
                        else "resumable Codex session"
                    ),
                    started_at=(row["created_at_ms"] / 1000.0) if row["created_at_ms"] else None,
                    updated_at=updated_at,
                    tokens=row["tokens_used"] if isinstance(row["tokens_used"], int) else None,
                    attach=self._attach_for(thread_id, held),
                    source=self.name,
                    extra={"session_id": thread_id, "resumable": True},
                )
            )
        return records, []
