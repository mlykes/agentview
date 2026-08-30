"""Identity for one independently deployed hub checkout."""

from __future__ import annotations

import hashlib
import re
import socket
import subprocess
from pathlib import Path
from typing import Dict

PROFILE_PORTS = {"stable": 7788, "preview": 7789}


def checkout_path() -> Path:
    return Path(__file__).resolve().parents[2]


def git_identity(path: Path = None) -> Dict[str, object]:
    path = Path(path or checkout_path()).resolve()

    def git(*args):
        try:
            return subprocess.check_output(
                ["git", "-C", str(path)] + list(args), stderr=subprocess.DEVNULL,
                text=True, timeout=3,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    branch = git("symbolic-ref", "--short", "HEAD") or "detached"
    commit = git("rev-parse", "--short", "HEAD") or "unknown"
    dirty = bool(git("status", "--porcelain"))
    return {"checkout": str(path), "branch": branch, "commit": commit, "dirty": dirty}


def instance_id(profile: str, port: int, checkout: Path = None, hostname: str = None) -> str:
    """A readable, bounded id with a digest preventing slug collisions."""
    checkout = Path(checkout or checkout_path()).resolve()
    hostname = hostname or socket.gethostname()
    raw = "{}\0{}\0{}\0{}".format(profile, port, hostname, checkout)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    prefix = re.sub(r"[^a-z0-9]+", "-", "{}-{}-{}".format(profile, port, hostname).lower())
    prefix = prefix.strip("-")[:40] or "hub"
    return "{}-{}".format(prefix, digest)


def deployment_dir(kind: str, instance: str) -> str:
    """Where this hub unpacks its collector on a remote host or in a container.

    Scoped per instance, not fixed. Two hubs -- a stable one and a preview one --
    otherwise push different versions of the collector to the same directory and
    overwrite each other, so whichever synced last decides what both of them
    collect. The failure is silent and looks like a code bug on one of the hubs.
    """
    if kind == "ssh":
        return "~/.agentview/code/{}".format(instance)
    return "/tmp/.agentview-code-{}".format(instance)
