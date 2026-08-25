"""Work out *where* this collector is running.

The collector is the only thing that can answer this -- a hub receiving a feed has no
way to tell a devcontainer from a bare host. So each collector self-reports, and the
hub just believes it.

Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import subprocess
from pathlib import Path
from typing import Optional

from agentview.model import CONTEXT_CONTAINER, CONTEXT_HOST, ContextRef

#: Env var a devcontainer/compose file can set so a container knows which host it
#: belongs to. Without it we cannot derive the host's identity from inside, so
#: nesting falls back to flat listing rather than guessing wrong.
PARENT_ENV = "AGENTVIEW_PARENT_ID"

#: Env var to override the display label (e.g. "work devbox").
LABEL_ENV = "AGENTVIEW_LABEL"


def _short_hash(value: str) -> str:
    """Stable, non-reversible id. We hash rather than ship a raw hardware UUID."""
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]


def _macos_platform_uuid() -> Optional[str]:
    try:
        out = subprocess.run(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if "IOPlatformUUID" in line and '"' in line:
            return line.rsplit('"', 2)[-2]
    return None


def machine_id() -> str:
    """A stable id for this machine, hashed."""
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            raw = Path(path).read_text().strip()
            if raw:
                return _short_hash(raw)
        except OSError:
            pass
    if platform.system() == "Darwin":
        uuid = _macos_platform_uuid()
        if uuid:
            return _short_hash(uuid)
    # Last resort. Hostname can change, but it is better than a random id that
    # would make the same machine look new on every restart.
    return _short_hash("hostname:" + socket.gethostname())


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return ""


def in_container() -> bool:
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True
    cgroup = _read_text("/proc/1/cgroup")
    return any(marker in cgroup for marker in ("docker", "containerd", "kubepods", "libpod"))


def container_id() -> Optional[str]:
    """Best-effort container id from cgroups, falling back to hostname.

    Docker sets a container's hostname to the short container id unless overridden,
    which is why the fallback is usually right.
    """
    for source in ("/proc/self/cgroup", "/proc/1/cgroup"):
        for line in _read_text(source).splitlines():
            for chunk in line.replace("/", ":").split(":"):
                chunk = chunk.strip()
                if len(chunk) >= 12 and all(c in "0123456789abcdef" for c in chunk):
                    return chunk[:12]
    host = socket.gethostname()
    if len(host) == 12 and all(c in "0123456789abcdef" for c in host):
        return host
    return None


def is_devcontainer() -> bool:
    return any(
        os.environ.get(v)
        for v in ("REMOTE_CONTAINERS", "DEVCONTAINER", "CODESPACES", "REMOTE_CONTAINERS_IPC")
    )


def workspace_folder() -> Optional[str]:
    """The devcontainer's workspace folder, if we're in one."""
    for var in ("AGENTVIEW_WORKSPACE", "CONTAINER_WORKSPACE_FOLDER", "WORKSPACE_FOLDER"):
        val = os.environ.get(var)
        if val:
            return val
    # The convention used by the devcontainers we care about.
    if Path("/workspace").is_dir():
        return "/workspace"
    return None


def devcontainer_name() -> Optional[str]:
    """Read ``name`` out of a mounted devcontainer.json, when one is reachable."""
    ws = workspace_folder()
    if not ws:
        return None
    for rel in (".devcontainer/devcontainer.json", ".devcontainer.json"):
        path = Path(ws) / rel
        try:
            raw = path.read_text(errors="replace")
        except OSError:
            continue
        # devcontainer.json is JSON-with-comments; strip line comments before parsing.
        stripped = "\n".join(
            line for line in raw.splitlines() if not line.lstrip().startswith("//")
        )
        try:
            data = json.loads(stripped)
        except ValueError:
            continue
        name = data.get("name")
        if isinstance(name, str) and name:
            return name
    return None


def via_ssh() -> bool:
    return bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))


def detect(parent_id: Optional[str] = None, label: Optional[str] = None) -> ContextRef:
    """Build the ContextRef describing this collector's location."""
    host = socket.gethostname()
    short_host = host.split(".")[0]
    system = platform.system().lower()
    arch = platform.machine()
    containerized = in_container()

    if containerized:
        cid = container_id()
        dc_name = devcontainer_name()
        ws = workspace_folder()
        if dc_name:
            default_label = "devcontainer: {}".format(dc_name)
        else:
            default_label = "container: {}".format(cid or short_host)
        ctx_id = "ctr-" + _short_hash("{}|{}|{}".format(cid or short_host, dc_name or "", ws or ""))
        return ContextRef(
            id=ctx_id,
            kind=CONTEXT_CONTAINER,
            label=label or os.environ.get(LABEL_ENV) or default_label,
            hostname=host,
            platform=system,
            arch=arch,
            parent_id=parent_id or os.environ.get(PARENT_ENV),
            via_ssh=via_ssh(),
            container_id=cid,
            container_name=dc_name,
            workspace_folder=ws,
        )

    return ContextRef(
        id="host-" + machine_id(),
        kind=CONTEXT_HOST,
        label=label or os.environ.get(LABEL_ENV) or short_host,
        hostname=host,
        platform=system,
        arch=arch,
        parent_id=None,
        via_ssh=via_ssh(),
    )
