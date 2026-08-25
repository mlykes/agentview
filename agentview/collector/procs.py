"""Process liveness helpers.

Claude Code's session registry files outlive their process, so *every* record must be
liveness-checked or the HUD shows ghosts -- agents that look idle but are long dead.
That was the single most important finding when designing this adapter.

Stdlib only.
"""

from __future__ import annotations

import os
import subprocess
from typing import Dict, Optional


def process_table() -> Dict[int, str]:
    """pid -> command/process-title, for every visible process.

    One subprocess call for the whole table rather than one per candidate pid.
    """
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,comm="],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    table: Dict[int, str] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_str, _, comm = line.partition(" ")
        try:
            table[int(pid_str)] = comm.strip()
        except ValueError:
            continue
    return table


def pid_alive(pid: int) -> bool:
    """Does this pid exist? Signal 0 does not actually deliver a signal."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, owned by someone else.
        return True
    except OSError:
        return False
    return True


def pid_matches(pid: int, expect: str, table: Optional[Dict[int, str]] = None) -> bool:
    """Alive *and* actually the program we expect.

    Guards against PID reuse: a recycled pid would otherwise resurrect a dead agent
    as whatever process inherited its number.
    """
    if not pid_alive(pid):
        return False
    if not table:
        # No table available (ps unavailable or failed). Fall back to bare
        # liveness rather than reporting every agent dead.
        return True
    comm = table.get(pid)
    if comm is None:
        # `ps -e` lists every visible process, so absence from a populated table
        # means this pid is gone -- or belongs to a process we cannot see, which
        # is not our agent either way.
        return False
    return expect.lower() in comm.lower()
