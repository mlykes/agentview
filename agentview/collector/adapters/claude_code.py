"""Claude Code adapter.

Built against the real on-disk layout, verified on a live machine:

  ~/.claude/sessions/<pid>.json      live registry: pid, sessionId, cwd, status,
                                     kind, agent, name, version, startedAt, jobId
  ~/.claude/jobs/<short>/state.json  state (blocked/active), detail, tokens, color
  ~/.claude/jobs/<short>/timeline.jsonl   append-only state-change events
  ~/.claude/projects/<slug>/<sid>.jsonl   transcript -- read ONLY for gitBranch

Registry files outlive their process, so every record is liveness-checked. We never
parse messages: the detail view attaches to the agent's own terminal instead.

Stdlib only.
"""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from agentview.collector.adapters.base import Adapter
from agentview.collector.procs import pid_matches, process_table
from agentview.model import (
    STATUS_BLOCKED,
    STATUS_BUSY,
    STATUS_IDLE,
    STATUS_UNKNOWN,
    AgentRecord,
    AttachSpec,
    ContextRef,
)

HARNESS = "claude-code"
HARNESS_LABEL = "Claude Code"


def default_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with path.open("r", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _ms_to_s(value: Any) -> Optional[float]:
    """Claude Code writes epoch milliseconds; our model uses seconds."""
    if isinstance(value, (int, float)) and value > 0:
        return float(value) / 1000.0
    return None


def _tail_field(path: Path, field: str, max_bytes: int = 65536) -> Optional[Any]:
    """Scan backwards through a JSONL file for the newest record carrying ``field``.

    Transcripts reach megabytes, so we read only the tail. This is the one thing we
    take from the transcript at all -- the git branch.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()  # discard the partial first line
            chunk = fh.read()
    except OSError:
        return None
    for line in reversed(chunk.splitlines()):
        if not line.strip():
            continue
        try:
            record = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(record, dict) and record.get(field) is not None:
            return record[field]
    return None


def _last_timeline_event(job_dir: Path) -> Optional[Dict[str, Any]]:
    path = job_dir / "timeline.jsonl"
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > 16384:
                fh.seek(size - 16384)
                fh.readline()
            chunk = fh.read()
    except OSError:
        return None
    for line in reversed(chunk.splitlines()):
        if not line.strip():
            continue
        try:
            event = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(event, dict):
            return event
    return None


def _resolve_status(session_status: Optional[str], job_state: Optional[str]) -> str:
    """Merge the two status axes Claude Code exposes.

    ``sessions/<pid>.json:status`` is the live busy/idle signal. ``jobs/*/state.json:
    state`` says whether the job is blocked on a human. A session can be 'busy' while
    its last recorded job state is 'blocked', so the live signal wins; 'blocked' only
    surfaces once the session has actually gone quiet.
    """
    if session_status == "busy":
        return STATUS_BUSY
    if job_state == "blocked":
        return STATUS_BLOCKED
    if session_status == "idle":
        return STATUS_IDLE
    if job_state == "active":
        return STATUS_BUSY
    return STATUS_UNKNOWN


class ClaudeCodeAdapter(Adapter):
    name = HARNESS
    priority = 10  # richest source we have; wins merges

    def __init__(
        self,
        config_dir: Optional[Path] = None,
        process_table_fn: Optional[Callable[[], Dict[int, str]]] = None,
    ) -> None:
        self.config_dir = Path(config_dir) if config_dir else default_config_dir()
        #: Injectable so tests can pin liveness instead of depending on whatever
        #: happens to be running on the machine at the time.
        self._process_table_fn = process_table_fn or process_table

    def available(self) -> bool:
        return (self.config_dir / "sessions").is_dir()

    def _git_branch(self, session_id: str) -> Optional[str]:
        pattern = str(self.config_dir / "projects" / "*" / "{}.jsonl".format(session_id))
        for match in glob.glob(pattern):
            branch = _tail_field(Path(match), "gitBranch")
            if not isinstance(branch, str) or not branch:
                continue
            # Claude Code records the literal "HEAD" when the cwd is not a git repo
            # (and for a detached HEAD). Either way it is not a branch name, and
            # showing it would imply a repo that isn't there.
            if branch == "HEAD":
                return None
            return branch
        return None

    def discover(self, ctx: ContextRef) -> Tuple[List[AgentRecord], List[str]]:
        records: List[AgentRecord] = []
        warnings: List[str] = []

        sessions_dir = self.config_dir / "sessions"
        if not sessions_dir.is_dir():
            return records, warnings

        try:
            session_files = sorted(sessions_dir.glob("*.json"))
        except OSError as exc:
            return records, ["claude-code: cannot list sessions dir: {}".format(exc)]

        table = self._process_table_fn()

        for path in session_files:
            data = _load_json(path)
            if not data:
                warnings.append("claude-code: unreadable session file {}".format(path.name))
                continue

            pid = data.get("pid")
            session_id = data.get("sessionId")
            if not isinstance(pid, int) or not isinstance(session_id, str):
                continue

            # The ghost check. Without this the HUD happily reports agents that
            # exited days ago, which is worse than showing nothing.
            if not pid_matches(pid, "claude", table):
                continue

            job_state: Dict[str, Any] = {}
            job_id = data.get("jobId")
            if isinstance(job_id, str) and job_id:
                job_state = _load_json(self.config_dir / "jobs" / job_id / "state.json") or {}

            detail = job_state.get("detail")
            if not detail and isinstance(job_id, str) and job_id:
                event = _last_timeline_event(self.config_dir / "jobs" / job_id)
                if event:
                    detail = event.get("detail")

            name = (
                data.get("name")
                or job_state.get("name")
                or job_state.get("intent")
                or session_id[:8]
            )

            tokens = job_state.get("tokens")
            record = AgentRecord(
                id="{}:{}:{}".format(ctx.id, HARNESS, session_id),
                harness=HARNESS,
                harness_label=HARNESS_LABEL,
                harness_version=data.get("version") or job_state.get("cliVersion"),
                context_id=ctx.id,
                name=str(name),
                cwd=data.get("cwd") or job_state.get("cwd"),
                git_branch=self._git_branch(session_id),
                status=_resolve_status(data.get("status"), job_state.get("state")),
                detail=str(detail) if detail else None,
                pid=pid,
                started_at=_ms_to_s(data.get("startedAt")),
                updated_at=_ms_to_s(data.get("updatedAt") or data.get("statusUpdatedAt")),
                tokens=tokens if isinstance(tokens, int) else None,
                color=job_state.get("color"),
                # M3 fills this in for tmux-launched agents; until then be explicit
                # about why the detail view is disabled rather than silently dead.
                attach=AttachSpec.unavailable(
                    "started outside tmux - no terminal to attach to"
                ),
                source=self.name,
                extra={
                    "session_id": session_id,
                    "job_id": job_id,
                    "kind": data.get("kind"),
                    "entrypoint": data.get("entrypoint"),
                    "agent": data.get("agent"),
                },
            )
            records.append(record)

        return records, warnings
