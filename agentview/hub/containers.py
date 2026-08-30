"""Agents running inside containers.

A devcontainer is one of the contexts this project exists to show: an agent isolated
in one is invisible from the host's process table, so without looking inside, the HUD
quietly under-reports.

The collector already knows how to describe a container from within -- it reads the
devcontainer's own name and workspace folder, so a container reports itself as
``devcontainer: Opetopic (/workspace)`` rather than a hex id. What was missing is the
hub running it in there, which is what this does: enumerate containers, copy the
collector in, run it, and nest the result under the machine hosting it.

It works the same for a container on this machine and one on an SSH host, because the
only difference is which `hosts.py` executor runs the command.

**Containers that bind-mount the host's `~/.claude` do not double-report.** That
sounds like it needs special handling and does not: the collector checks that each
session's pid is a live Claude Code process, and a host pid is not live inside the
container's own PID namespace, so those records are dropped as ghosts by the same
check that removes stale ones.

Stdlib only.
"""

from __future__ import annotations

import json
import shlex
import time
from typing import Any, Dict, List, Optional, Tuple

#: Unpacked inside the container. /tmp because it is writable in essentially every
#: image, including the ones that run as a non-root user with a read-only home.
#: Fallback when no instance is named; a live hub always passes its own so two
#: hubs never unpack over each other inside the same container.
CODE_DIR = "/tmp/.agentview-code-default"

#: Containers are enumerated far less often than agents are polled: the set changes
#: rarely, and each probe costs an exec (or an ssh plus an exec).
DEFAULT_INTERVAL = 20.0

PS_FORMAT = "{{.ID}}\t{{.Names}}\t{{.Image}}"


def available(host) -> bool:
    code, out, _ = host.run("command -v docker >/dev/null && echo yes || echo no", timeout=20)
    return code == 0 and out.strip() == "yes"


def list_containers(host) -> Tuple[List[Dict[str, str]], Optional[str]]:
    code, out, err = host.run("docker ps --format {}".format(shlex.quote(PS_FORMAT)))
    if code != 0:
        detail = (err or out).strip().splitlines()
        return [], detail[-1] if detail else "docker ps failed"
    found = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip():
            found.append({
                "id": parts[0].strip(),
                "name": parts[1].strip(),
                "image": parts[2].strip() if len(parts) > 2 else "",
            })
    return found, None


def python_in(host, cid: str) -> Optional[str]:
    """The container's python3, or None -- most service images have none."""
    code, out, _ = host.run(
        "docker exec {} sh -c 'command -v python3 || command -v python' 2>/dev/null".format(
            shlex.quote(cid)
        ),
        timeout=25,
    )
    if code != 0:
        return None
    path = out.strip().splitlines()
    return path[0].strip() if path and path[0].strip() else None


def code_dir(instance: Optional[str] = None) -> str:
    if not instance:
        return CODE_DIR
    from agentview.hub.runtime import deployment_dir
    return deployment_dir("container", instance)


def sync_collector(host, cid: str, payload: bytes, instance: Optional[str] = None) -> Optional[str]:
    """Copy the collector into the container. Returns an error string, or None."""
    command = "docker exec -i {c} sh -c {inner}".format(
        c=shlex.quote(cid),
        inner=shlex.quote("mkdir -p {d} && tar -xzf - -C {d}".format(d=code_dir(instance))),
    )
    code, err = host.run_input(command, payload)
    if code != 0:
        return err.strip().splitlines()[-1] if err.strip() else "copy into container failed"
    return None


def collect_once(host, cid: str, python: str = "python3", instance: Optional[str] = None):
    command = "docker exec {c} sh -c {inner}".format(
        c=shlex.quote(cid),
        inner=shlex.quote("cd {d} && {p} -m agentview.collector --once".format(
            d=code_dir(instance), p=python
        )),
    )
    code, out, err = host.run(command)
    if code != 0:
        detail = (err or out).strip().splitlines()
        return None, detail[-1] if detail else "collector failed in container"
    try:
        snapshot = json.loads(out)
    except ValueError:
        return None, "collector did not return JSON"
    if not isinstance(snapshot, dict):
        return None, "collector returned an unexpected shape"
    return snapshot, None


def attach_argv(host, cid: str, session: str, readonly: bool = False) -> List[str]:
    """`docker exec` into the container's tmux, through ssh when the host is remote.

    Attach stays *just an argv* here too -- a container on another machine only adds
    one more wrapper, not another code path.
    """
    inner = ["docker", "exec", "-it", cid, "tmux", "attach"]
    if readonly:
        inner.append("-r")
    inner += ["-t", session]
    if getattr(host, "ssh_host", None):
        return host.wrap(inner)
    return inner


def rewrite(snapshot: Dict[str, Any], host, cid: str, parent_id: Optional[str]) -> Dict[str, Any]:
    """Nest a container snapshot under the machine running it, and fix its attach.

    The collector inside reports `tmux attach -t <s>`, which is correct in there and
    reaches nothing out here.
    """
    context = dict(snapshot.get("context") or {})
    context["parent_id"] = parent_id
    context["container_id"] = context.get("container_id") or cid
    if getattr(host, "ssh_host", None):
        context["via_ssh"] = True
        context["ssh_host"] = host.ssh_host

    agents = []
    for agent in snapshot.get("agents") or []:
        agent = dict(agent)
        agent["context_id"] = context.get("id")
        agent["container_id"] = cid
        if getattr(host, "ssh_host", None):
            agent["ssh_host"] = host.ssh_host
        session = (agent.get("extra") or {}).get("tmux_session")
        if session:
            agent["attach"] = {
                "available": True,
                "reason": None,
                "argv": attach_argv(host, cid, session),
                "argv_readonly": attach_argv(host, cid, session, readonly=True),
            }
        elif (agent.get("attach") or {}).get("available"):
            # A background agent's `claude attach` talks to a unix socket inside the
            # container. Running that command out here would target a job id that
            # does not exist on this side.
            agent["attach"] = {
                "available": False,
                "reason": "not attachable from outside the container - only tmux sessions can be reached",
                "argv": None,
                "argv_readonly": None,
            }
        agents.append(agent)

    return {
        "context": context,
        "agents": agents,
        "warnings": snapshot.get("warnings") or [],
        "collected_at": snapshot.get("collected_at") or time.time(),
    }
