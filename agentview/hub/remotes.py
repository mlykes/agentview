"""Machines reached over SSH.

The hub drives these itself rather than asking you to install and start something on
the far side. That is the whole point of the remote case: on a locked-down work
machine, "copy this directory and run it" is friction you have to repeat, and the
thing you actually want is for the host to appear in the HUD because you named it.

So the hub ships its own collector over the existing SSH connection and runs it
there. Nothing is installed, nothing is fetched, and no port is opened on the remote
-- it is the same `ssh` you already use, which is what keeps this usable somewhere an
IT department has opinions.

Three things happen over that connection:

  discovery   `python3 -m agentview.collector --once`, parsed as a snapshot
  launching   `tmux new-session -d` on the far side
  attaching   `ssh <host> -t tmux attach`, which is just another argv

**Everything runs under a login shell.** SSH gives a non-login shell whose PATH is
typically `/usr/local/bin:/usr/bin:/bin`, while agent CLIs install to `~/.local/bin`.
Without `-l` a perfectly working Claude Code install reports as missing, the launch
menu comes back empty, and the remote looks unsupported when it is merely unlit.

Stdlib only.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import tarfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agentview.hub.hosts import SshHost

#: Where the collector is unpacked on the remote. Under ~/.agentview so it sits with
#: the token and overrides rather than scattering files around $HOME.
#: Fallback for a hub that names no instance (a one-off `--once` run). A live hub
#: always passes its own, so two of them never share a directory.
REMOTE_CODE_DIR = "~/.agentview/code/default"

#: Long enough for a slow link and a cold Python start, short enough that a wedged
#: host cannot stall the whole poll loop.
SSH_TIMEOUT = 45.0

#: One definition, shared with hosts.SshHost. Kept as a name here because the attach
#: argv is built from it; two copies would drift the first time one is changed.
SSH_OPTS = SshHost.OPTS


def config_path() -> Path:
    home = Path(os.environ.get("AGENTVIEW_HOME") or (Path.home() / ".agentview"))
    return home / "remotes.json"


def load_remotes(extra: Optional[List[str]] = None) -> List[str]:
    """Hosts from ~/.agentview/remotes.json plus any given on the command line.

    A host is whatever you would type after `ssh`, so an alias from ~/.ssh/config
    works and its User/IdentityFile/ProxyJump come along for free. Re-implementing
    any of that here would only be a worse version of what ssh already does.
    """
    hosts: List[str] = []
    try:
        with config_path().open("r", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = None
    if isinstance(data, list):
        hosts.extend(str(h) for h in data if isinstance(h, str) and h.strip())
    elif isinstance(data, dict):
        hosts.extend(
            str(h) for h in data.get("hosts") or [] if isinstance(h, str) and h.strip()
        )
    for host in extra or []:
        if host and host not in hosts:
            hosts.append(host)
    # Preserve order, drop duplicates.
    seen, unique = set(), []
    for host in hosts:
        if host not in seen:
            seen.add(host)
            unique.append(host)
    return unique


def save_remotes(hosts: List[str]) -> None:
    path = config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w") as fh:
            json.dump(sorted(set(hosts)), fh, indent=2)
        os.replace(str(tmp), str(path))
    except OSError:
        pass


def login_shell_command(command: str) -> str:
    """Wrap a command so it runs with the user's real PATH.

    Not cosmetic: `~/.local/bin` -- where Claude Code installs -- is absent from the
    non-login PATH on a stock Debian box, so without this every agent CLI reports as
    missing.
    """
    return "bash -lc {}".format(shlex.quote(command))


def ssh_argv(host: str, command: str, tty: bool = False) -> List[str]:
    return SshHost(host).argv(command, tty=tty)


def run(host: str, command: str, timeout: float = SSH_TIMEOUT) -> Tuple[int, str, str]:
    return SshHost(host).run(command, timeout=timeout)


# -- shipping the collector -------------------------------------------------


def _package_root() -> Path:
    return Path(__file__).resolve().parent.parent


def package_tar() -> bytes:
    """The collector, as a tarball, built in memory.

    Only what the collector needs: the hub's own modules, its vendored web assets and
    the tests would multiply the bytes crossing a slow link for no benefit.
    """
    root = _package_root()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(root.parent)
            if rel.parts[1] in ("hub", "tui"):
                continue
            tar.add(str(path), arcname=str(rel))
    return buf.getvalue()


def code_dir(instance: Optional[str] = None) -> str:
    if not instance:
        return REMOTE_CODE_DIR
    from agentview.hub.runtime import deployment_dir
    return deployment_dir("ssh", instance)


def sync_code(host: str, timeout: float = SSH_TIMEOUT, instance: Optional[str] = None) -> Optional[str]:
    """Copy the collector to the remote. Returns an error string, or None."""
    payload = package_tar()
    command = "mkdir -p {d} && tar -xzf - -C {d}".format(d=code_dir(instance))
    code, err = SshHost(host).run_input(command, payload, timeout=timeout)
    if code != 0:
        return err.strip().splitlines()[-1] if err.strip() else "copy failed"
    return None


def collect_once(host: str, timeout: float = SSH_TIMEOUT, instance: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Run the collector on the remote and return its snapshot."""
    command = "cd {d} && python3 -m agentview.collector --once".format(d=code_dir(instance))
    code, out, err = run(host, command, timeout=timeout)
    if code != 0:
        return None, (err or out).strip().splitlines()[-1] if (err or out).strip() else "collector failed"
    try:
        snapshot = json.loads(out)
    except ValueError:
        return None, "collector did not return JSON"
    if not isinstance(snapshot, dict):
        return None, "collector returned an unexpected shape"
    return snapshot, None


def remote_harnesses(host: str, commands: List[str], timeout: float = SSH_TIMEOUT):
    """Which agent CLIs exist on the remote, resolved with its login PATH."""
    if not commands:
        return [], None
    probe = "; ".join(
        "p=$(command -v {c} 2>/dev/null); [ -n \"$p\" ] && echo {c}=$p".format(c=shlex.quote(c))
        for c in commands
    )
    code, out, err = run(host, probe, timeout=timeout)
    # `command -v` exits non-zero when the last probe misses, which is not a failure
    # of the probe itself -- only an empty result with a real error is.
    found = {}
    for line in out.splitlines():
        if "=" in line:
            name, _, path = line.partition("=")
            if name and path:
                found[name.strip()] = path.strip()
    if not found and code not in (0, 1) and err.strip():
        return [], err.strip().splitlines()[-1]
    return found, None


def launch(host: str, command: str, session: str, timeout: float = SSH_TIMEOUT) -> Optional[str]:
    """Start an agent under tmux on the remote. Returns an error string, or None."""
    remote = "tmux new-session -d -s {s} {c}".format(
        s=shlex.quote(session), c=shlex.quote(command)
    )
    code, _, err = run(host, remote, timeout=timeout)
    if code != 0:
        detail = err.strip().splitlines()
        return detail[-1] if detail else "tmux refused to start the session"
    return None


def attach_argv(host: str, session: str) -> List[str]:
    """Attach is just an argv here too -- the ssh wrapper is the only difference."""
    return ["ssh"] + list(SSH_OPTS) + ["-t", host, "tmux attach -t " + shlex.quote(session)]


def rewrite_for_ssh(snapshot: Dict[str, Any], host: str) -> Dict[str, Any]:
    """Point a remote snapshot's attach commands back through this SSH connection.

    The collector reports the argv that works *on its own machine*; run here it would
    attach to the wrong box, or to nothing. Only agents whose tmux session we know can
    be rewritten -- anything else keeps its own honest "cannot attach" reason.
    """
    context = dict(snapshot.get("context") or {})
    context["via_ssh"] = True
    context["ssh_host"] = host
    # Label it with the name you actually type. The collector reports the machine's
    # own hostname, which on a work box is often an inventory code you would not
    # recognise -- `SPU5-1-2-7-61358` rather than `pronto_server`.
    context["hostname"] = context.get("hostname") or context.get("label") or ""
    context["label"] = host

    agents = []
    for agent in snapshot.get("agents") or []:
        agent = dict(agent)
        agent["context_id"] = context.get("id")
        #: Marks the agent as reachable through this SSH connection, which is what
        #: lets the hub attach to it despite it not being on this machine.
        agent["ssh_host"] = host
        session = (agent.get("extra") or {}).get("tmux_session")
        attach = dict(agent.get("attach") or {})
        if session:
            attach = {
                "available": True,
                "reason": None,
                "argv": attach_argv(host, session),
                "argv_readonly": ["ssh"] + list(SSH_OPTS)
                + ["-t", host, "tmux attach -r -t " + shlex.quote(session)],
            }
        elif attach.get("available"):
            # Reachable on its own machine but not through a tmux session we can
            # name -- a background agent's `claude attach`, for instance. Say so
            # rather than offering a button that would run the command locally.
            attach = {
                "available": False,
                "reason": "not attachable over ssh - only tmux sessions can be reached",
                "argv": None,
                "argv_readonly": None,
            }
        agent["attach"] = attach
        agents.append(agent)

    return {
        "context": context,
        "agents": agents,
        "warnings": snapshot.get("warnings") or [],
        "collected_at": snapshot.get("collected_at") or time.time(),
    }
