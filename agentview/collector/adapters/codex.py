"""Codex resumable-session adapter.

Codex has no Claude-style background-agent registry.  Its closest equivalent is the
thread list used by ``codex resume``.  Current Codex releases materialize that list in
``$CODEX_HOME/state_*.sqlite``; this adapter reads it read-only and emits one idle,
resumable record per visible interactive thread.

Stdlib only.
"""

from __future__ import annotations

import fcntl
import glob
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from agentview.collector import procs
from agentview.collector import tmux as tmux_mod
from agentview.collector.adapters.base import Adapter
from agentview.model import STATUS_BUSY, STATUS_IDLE, AgentRecord, AttachSpec, ContextRef

HARNESS = "codex"
HARNESS_LABEL = "Codex"
SESSION_PREFIX = "agentview_codex_"

#: Codex names each thread's transcript after the thread, so an open rollout file
#: identifies which thread a process is working on.
ROLLOUT_MARKER = "rollout-"

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


def fork_parent(home: Path, thread_id: str) -> Optional[str]:
    """The thread this one was forked from, if any.

    Codex does not always continue a session in the same thread. A settings change
    such as `/cd` forks it: a new thread id, the same conversation, the same process.
    The old thread stays in the store, so both appear in `codex resume` and both would
    appear here -- the same session listed twice under the same name.

    Recorded in the first line of the thread's own transcript, so this is read from
    what Codex wrote, not inferred.
    """
    try:
        paths = list((home / "sessions").glob("*/*/*/*{}*.jsonl".format(thread_id)))
    except OSError:
        return None
    for path in paths:
        try:
            with path.open("r", errors="replace") as fh:
                first = json.loads(fh.readline())
        except (OSError, ValueError):
            continue
        payload = first.get("payload") if isinstance(first, dict) else None
        parent = payload.get("forked_from_id") if isinstance(payload, dict) else None
        if isinstance(parent, str) and parent:
            return parent
    return None


def collapse_forked_continuations(records: List[AgentRecord]) -> List[AgentRecord]:
    """Drop a thread that a later fork has continued, so one session is one row.

    Kept only when the fork is a genuinely separate live session: if the parent has
    its own process, distinct from the child's, then two terminals really are open on
    the two threads and both belong on screen. A rename also keeps both, on the
    grounds that the user has told them apart deliberately.
    """
    by_id = {r.extra.get("session_id"): r for r in records}
    superseded = set()
    for child in records:
        parent = by_id.get(child.extra.get("forked_from_id"))
        if parent is None or child.name != parent.name:
            continue
        # A parent running in its own process is a real second session, not history.
        if parent.pid and parent.pid != child.pid:
            continue
        superseded.add(parent.id)
    return [r for r in records if r.id not in superseded]


def resumed_thread_pids(commands: Dict[int, str]) -> Dict[str, int]:
    """thread id -> pid, for clients started as ``codex resume <thread-id>``.

    The only *exact* join available between a thread and a process. Codex's writer
    lock files are empty, so nothing on disk records who holds a thread, but a resumed
    client carries the id in its own argv.

    This matters because a `codex` running in a tmux session is otherwise reported
    twice -- once here from the thread store, once by the generic tmux adapter from
    the pane -- and the two ids cannot be joined. Giving the record a pid is all
    `core.drop_shadowed_tmux_records` needs to recognise them as one agent, and it
    then hands over the pane's attach: reattaching to the terminal already running the
    session, rather than starting a second client Codex would refuse anyway.

    Every session agentview opens is a resume, so this covers the HUD's own sessions
    and any resumed by hand. A `codex` started fresh carries no id in its argv and is
    joined by `open_rollouts` instead.
    """
    found: Dict[str, int] = {}
    for pid, command in (commands or {}).items():
        parts = command.split()
        if len(parts) < 3 or os.path.basename(parts[0]) != "codex":
            continue
        try:
            index = parts.index("resume")
        except ValueError:
            continue
        if index + 1 < len(parts) and not parts[index + 1].startswith("-"):
            # `codex resume --last` names no thread. Skipping the flag matters beyond
            # tidiness: a bogus entry here would count the pid as claimed and stop
            # `open_rollouts` from joining it the other way.
            found.setdefault(parts[index + 1], pid)
    return found


#: Subcommands that mean "not a session". `app-server` is the important one: it is a
#: single shared backend serving every thread, and it holds their transcripts open, so
#: it looks exactly like a client to any join based on open files. Binding it would
#: name one arbitrary thread after a process that belongs to all of them.
NOT_A_SESSION = ("app-server", "mcp", "mcp-server", "completion", "exec")


def codex_pids(commands: Dict[int, str]) -> List[int]:
    """Pids that are a `codex` session client.

    Excludes `codex-code-mode-host`, the per-client helper, by matching the exact
    basename; and the shared subcommands above by looking at the first argument.
    """
    found = []
    for pid, command in (commands or {}).items():
        parts = command.split()
        if not parts or os.path.basename(parts[0]) != "codex":
            continue
        if len(parts) > 1 and parts[1] in NOT_A_SESSION:
            continue
        found.append(pid)
    return found


def open_rollouts(pids: List[int]) -> Dict[int, List[str]]:
    """pid -> every rollout transcript that pid holds open.

    A list, not one path: a client that starts a second thread keeps the first one's
    transcript open too, so a pid can hold several. Which of them it is *working on*
    is decided by the caller, which has the recency the store records.

    The exact answer for a `codex` started fresh, which carries no thread id in its
    argv: the process holds its thread's rollout file open, and the file is named
    after the thread. Nothing here is inferred from timing.

    Linux answers this from /proc with no subprocess at all. Elsewhere -- macOS, the
    BSDs -- there is no /proc, so this asks `lsof` about exactly these pids, which is
    a targeted lookup rather than a scan of the machine (~80ms for a handful here).
    Where neither is available the join is simply skipped: a duplicate row is a much
    smaller sin than a wrong one.
    """
    if not pids:
        return {}

    found: Dict[int, List[str]] = {}
    if os.path.isdir("/proc"):
        for pid in pids:
            for link in glob.glob("/proc/{}/fd/*".format(pid)):
                try:
                    target = os.readlink(link)
                except OSError:
                    continue
                if ROLLOUT_MARKER in target:
                    found.setdefault(pid, []).append(target)
        return found

    try:
        out = subprocess.run(
            ["lsof", "-p", ",".join(str(pid) for pid in pids), "-Fn"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}

    # -Fn emits `p<pid>` lines followed by the `n<name>` lines belonging to it.
    current = None
    for line in out.splitlines():
        if line.startswith("p"):
            try:
                current = int(line[1:])
            except ValueError:
                current = None
        elif line.startswith("n") and current is not None and ROLLOUT_MARKER in line:
            found.setdefault(current, []).append(line[1:])
    return found


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
        commands_fn: Optional[Callable[[], Dict[int, str]]] = None,
        rollouts_fn: Optional[Callable[[List[int]], Dict[int, str]]] = None,
    ) -> None:
        self.codex_home = Path(codex_home) if codex_home else default_codex_home()
        self._which = which_fn or shutil.which
        self._tmux_available_fn = tmux_available_fn or tmux_mod.available
        self._active_thread_fn = active_thread_fn or (
            lambda thread_id: thread_has_writer(self.codex_home, thread_id)
        )
        self._tmux_has_session_fn = tmux_has_session_fn or tmux_mod.has_session
        self._commands_fn = commands_fn or procs.command_table
        #: thread id -> fork parent (or None). Fixed for the life of a thread, and
        #: each lookup opens a file, so it is read once rather than every tick.
        self._forks: Dict[str, Optional[str]] = {}
        self._rollouts_fn = rollouts_fn or open_rollouts

    def _fork_parent(self, thread_id: str) -> Optional[str]:
        if thread_id not in self._forks:
            self._forks[thread_id] = fork_parent(self.codex_home, thread_id)
        return self._forks[thread_id]

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
        commands = self._commands_fn()
        pids = resumed_thread_pids(commands)
        # Whatever argv could not answer, ask the filesystem. Skipped entirely when no
        # unclaimed `codex` is running, which is the usual case.
        unclaimed = [pid for pid in codex_pids(commands) if pid not in set(pids.values())]
        if unclaimed:
            # `rows` is ordered newest-active first, so the first thread a pid has a
            # transcript open for is the one it is working on now -- the earlier ones
            # are threads it moved on from but has not closed.
            ids = [str(row["id"]) for row in rows]
            for pid, paths in self._rollouts_fn(unclaimed).items():
                blob = "\n".join(paths)
                for thread_id in ids:
                    if thread_id in blob:
                        pids.setdefault(thread_id, pid)
                        break
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
                    pid=pids.get(thread_id),
                    attach=self._attach_for(thread_id, held),
                    source=self.name,
                    extra={
                        "session_id": thread_id,
                        "resumable": True,
                        "forked_from_id": self._fork_parent(thread_id),
                    },
                )
            )
        return collapse_forked_continuations(records), []
