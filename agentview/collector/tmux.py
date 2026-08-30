"""tmux introspection.

tmux is the attach substrate: you cannot grab the PTY of a process that was started
outside a multiplexer, so an agent is attachable exactly when it is running inside a
tmux pane. This module answers "which pane, if any".

Stdlib only.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Dict, List, NamedTuple, Optional

#: Field order must match PANE_FORMAT.
PANE_FORMAT = "#{session_name}\t#{pane_pid}\t#{pane_current_command}\t#{pane_current_path}"


class Pane(NamedTuple):
    session: str
    pid: int
    command: str
    path: str


def available() -> bool:
    return shutil.which("tmux") is not None


#: tmux sessions agentview parks its own Claude and Codex clients in. These are
#: plumbing, not agents. Left visible, every session opened from the HUD would show
#: up a second time as a generic tmux record.
AGENTVIEW_BG_PREFIX = "agentview_bg_"
AGENTVIEW_CODEX_PREFIX = "agentview_codex_"


def list_panes() -> List[Pane]:
    """Every pane across every tmux session, or [] when tmux is absent or idle."""
    if not available():
        return []
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", PANE_FORMAT],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    # No server running is the normal "no sessions" case, not an error worth surfacing.
    if result.returncode != 0:
        return []

    panes: List[Pane] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        if parts[0].startswith((AGENTVIEW_BG_PREFIX, AGENTVIEW_CODEX_PREFIX)):
            continue
        panes.append(Pane(session=parts[0], pid=pid, command=parts[2], path=parts[3]))
    return panes


def session_for_pid(
    pid: int, panes: List[Pane], parents: Optional[Dict[int, int]] = None
) -> Optional[str]:
    """Which tmux session contains this pid, if any.

    An agent is usually a grandchild of the pane process (pane -> shell -> agent),
    so this walks the ancestry rather than comparing pids directly.
    """
    from agentview.collector.procs import ancestors

    if not panes:
        return None
    by_pid = {pane.pid: pane.session for pane in panes}
    if pid in by_pid:
        return by_pid[pid]
    for parent in ancestors(pid, parents):
        if parent in by_pid:
            return by_pid[parent]
    return None


def has_session(name: str) -> bool:
    if not available():
        return False
    try:
        return subprocess.run(
            ["tmux", "has-session", "-t", name],
            capture_output=True,
            timeout=10,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def path_for_session(name: str) -> Optional[str]:
    """Live cwd of a session's active pane, including agentview's internal panes."""
    if not available():
        return None
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", name, "#{pane_current_path}"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    path = result.stdout.strip() if result.returncode == 0 else ""
    return path or None
