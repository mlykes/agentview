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
