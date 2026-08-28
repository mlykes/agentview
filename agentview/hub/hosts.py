"""Where a command runs: this machine, or one reached over SSH.

The hub has to do the same handful of things -- list containers, probe for python,
copy the collector in, run it -- in two places. Rather than writing each twice, the
difference is confined to these two small classes, and everything above them is
written once.

Both run commands through a **login shell**. SSH gives a non-login PATH that omits
`~/.local/bin`, where agent CLIs install, and matching that locally keeps the two
paths honest rather than subtly different.

Stdlib only.
"""

from __future__ import annotations

import shlex
import subprocess
from typing import List, Optional, Tuple

DEFAULT_TIMEOUT = 45.0


class LocalHost:
    """The machine the hub runs on."""

    #: None means "not remote"; callers use it to decide whether to wrap an argv.
    ssh_host: Optional[str] = None
    label = "this machine"

    def argv(self, command: str, tty: bool = False) -> List[str]:
        return ["bash", "-lc", command]

    def run(self, command: str, timeout: float = DEFAULT_TIMEOUT) -> Tuple[int, str, str]:
        try:
            result = subprocess.run(
                self.argv(command), capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            return 124, "", "timed out after {:.0f}s".format(timeout)
        except OSError as exc:
            return 127, "", str(exc)
        return result.returncode, result.stdout, result.stderr

    def run_input(
        self, command: str, data: bytes, timeout: float = DEFAULT_TIMEOUT
    ) -> Tuple[int, str]:
        try:
            result = subprocess.run(
                self.argv(command), input=data, capture_output=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            return 124, "timed out"
        except OSError as exc:
            return 127, str(exc)
        return result.returncode, (result.stderr or b"").decode("utf-8", "replace")


class SshHost:
    """A machine reached with `ssh`, named however you would type it."""

    #: Fail fast rather than hang, and never prompt: a hub in the background cannot
    #: answer a password or a host-key question.
    OPTS = [
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
    ]

    def __init__(self, host: str) -> None:
        self.ssh_host = host
        self.label = host

    def argv(self, command: str, tty: bool = False) -> List[str]:
        argv = ["ssh"] + list(self.OPTS)
        if tty:
            argv.append("-t")
        argv.append(self.ssh_host)
        # One argument, so ssh's own shell does not re-split it, and a login shell so
        # ~/.local/bin is on PATH.
        argv.append("bash -lc " + shlex.quote(command))
        return argv

    def run(self, command: str, timeout: float = DEFAULT_TIMEOUT) -> Tuple[int, str, str]:
        try:
            result = subprocess.run(
                self.argv(command), capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            return 124, "", "timed out after {:.0f}s".format(timeout)
        except OSError as exc:
            return 127, "", str(exc)
        return result.returncode, result.stdout, result.stderr

    def run_input(
        self, command: str, data: bytes, timeout: float = DEFAULT_TIMEOUT
    ) -> Tuple[int, str]:
        try:
            result = subprocess.run(
                self.argv(command), input=data, capture_output=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            return 124, "timed out"
        except OSError as exc:
            return 127, str(exc)
        return result.returncode, (result.stderr or b"").decode("utf-8", "replace")

    def wrap(self, argv: List[str], tty: bool = True) -> List[str]:
        """Put an argv that would run on the far side behind this ssh connection."""
        inner = " ".join(shlex.quote(part) for part in argv)
        return ["ssh"] + list(self.OPTS) + (["-t"] if tty else []) + [self.ssh_host, inner]
