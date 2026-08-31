"""Process liveness helpers.

Claude Code's session registry files outlive their process, so *every* record must be
liveness-checked or the HUD shows ghosts -- agents that look idle but are long dead.
That was the single most important finding when designing this adapter.

Stdlib only.
"""

from __future__ import annotations

import os
import subprocess
from typing import Dict, List, Optional


def cwd_for_pid(pid: int) -> Optional[str]:
    """Working directory of a live process, on Linux and macOS.

    A harness registry records where a session *started*. Agents move -- `/cd`, or a
    shell that changed directory before launching -- and the row then names a
    directory the agent is no longer in, which is worse than naming none, because
    grouping by directory files it under the wrong repo.
    """
    if pid <= 0:
        return None
    try:
        return os.readlink("/proc/{}/cwd".format(pid))
    except OSError:
        pass  # no /proc: macOS and the BSDs answer through lsof instead
    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in result.stdout.splitlines():
        if line.startswith("n/"):
            return line[1:]
    return None


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


def pid_matches(
    pid: int,
    expect: str,
    table: Optional[Dict[int, str]] = None,
    argv_table: Optional[Dict[int, str]] = None,
) -> bool:
    """Alive *and* actually the program we expect.

    Guards against PID reuse: a recycled pid would otherwise resurrect a dead agent
    as whatever process inherited its number.

    The executable name alone is not enough, and the gap is platform-shaped. macOS
    `ps -o comm=` prints the full path, so a background agent running
    ``~/.local/share/claude/versions/2.1.251`` still reads as "claude". Linux prints
    the basename -- ``2.1.251`` -- which carries no trace of the harness, so every
    background agent on a Linux host looked dead and was silently dropped. The
    install path is the evidence that survives on both, so fall back to argv[0].

    Only argv[0] is consulted, never the whole command line: `grep claude` and
    `vim ~/.claude/settings.json` would both match the latter.
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
    if expect.lower() in comm.lower():
        return True
    argv = (argv_table or {}).get(pid)
    if argv:
        return expect.lower() in argv.split(" ", 1)[0].lower()
    return False


def parent_map() -> Dict[int, int]:
    """pid -> ppid for every visible process."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,ppid="], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    parents: Dict[int, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                parents[int(parts[0])] = int(parts[1])
            except ValueError:
                continue
    return parents


def ancestors(pid: int, parents: Optional[Dict[int, int]] = None, limit: int = 40) -> List[int]:
    """Walk a pid's parent chain.

    Used to decide whether an agent is running inside a tmux pane: the harness is
    typically a grandchild of the pane process (pane -> shell -> agent), so a direct
    parent check is not enough.
    """
    parents = parents if parents is not None else parent_map()
    chain: List[int] = []
    seen = set()
    current = pid
    for _ in range(limit):
        parent = parents.get(current)
        if parent is None or parent in seen or parent <= 1:
            break
        chain.append(parent)
        seen.add(parent)
        current = parent
    return chain


def command_table() -> Dict[int, str]:
    """pid -> full command line.

    Distinct from process_table(), which returns the executable name. A harness is
    frequently launched through an interpreter -- `node /usr/local/bin/opencode`,
    `python3 .../aider`, a shell script -- so the name we need to recognise appears
    in argv, not in the executable name. Liveness still uses process_table(); this is
    only for identification.
    """
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    table: Dict[int, str] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_str, _, args = line.partition(" ")
        try:
            table[int(pid_str)] = args.strip()
        except ValueError:
            continue
    return table


def descendants(root: int, parents: Dict[int, int]) -> set:
    """Every pid beneath `root`, following the parent map downwards."""
    children: Dict[int, List[int]] = {}
    for pid, ppid in parents.items():
        children.setdefault(ppid, []).append(pid)
    found = set()
    queue = list(children.get(root, []))
    while queue:
        pid = queue.pop()
        if pid in found:
            continue
        found.add(pid)
        queue.extend(children.get(pid, []))
    return found
