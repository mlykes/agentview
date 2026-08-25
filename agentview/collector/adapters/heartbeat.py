"""Heartbeat-file adapter -- the escape hatch for any harness we have never seen.

A harness (or a wrapper script) that writes a JSON file into ~/.agentview/agents/
shows up in the HUD with no code change here. This is the documented integration
path in PROTOCOL.md, and it is what makes "supports any harness" true rather than
aspirational.

Expected file shape (all fields optional except id and name):

    {"id": "my-thing-1", "name": "build watcher", "harness": "mytool",
     "harness_label": "MyTool", "status": "busy", "cwd": "/srv/app",
     "pid": 123, "detail": "compiling", "started_at": 1787690000}

Stdlib only.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import List, Optional, Tuple

from agentview.collector.adapters.base import Adapter
from agentview.collector.procs import pid_alive
from agentview.model import (
    STATUS_IDLE,
    STATUS_UNKNOWN,
    AgentRecord,
    AttachSpec,
    ContextRef,
)

#: Files older than this without a refresh are considered abandoned.
STALE_AFTER = 60.0


def default_dir() -> Path:
    return Path(os.environ.get("AGENTVIEW_HOME") or (Path.home() / ".agentview")) / "agents"


class HeartbeatAdapter(Adapter):
    name = "heartbeat"
    priority = 50

    def __init__(self, directory: Optional[Path] = None) -> None:
        self.directory = Path(directory) if directory else default_dir()

    def available(self) -> bool:
        return self.directory.is_dir()

    def discover(self, ctx: ContextRef) -> Tuple[List[AgentRecord], List[str]]:
        records: List[AgentRecord] = []
        warnings: List[str] = []
        now = time.time()

        try:
            paths = sorted(self.directory.glob("*.json"))
        except OSError as exc:
            return records, ["heartbeat: cannot list {}: {}".format(self.directory, exc)]

        for path in paths:
            try:
                data = json.loads(path.read_text(errors="replace"))
                mtime = path.stat().st_mtime
            except (OSError, ValueError):
                warnings.append("heartbeat: unreadable {}".format(path.name))
                continue
            if not isinstance(data, dict):
                continue

            native_id = str(data.get("id") or path.stem)

            # Two liveness paths: an explicit pid, or a recently-touched file.
            pid = data.get("pid")
            if isinstance(pid, int):
                if not pid_alive(pid):
                    continue
            elif now - mtime > STALE_AFTER:
                continue

            harness = str(data.get("harness") or "external")
            records.append(
                AgentRecord(
                    id="{}:{}:{}".format(ctx.id, harness, native_id),
                    harness=harness,
                    harness_label=str(data.get("harness_label") or harness),
                    harness_version=data.get("harness_version"),
                    context_id=ctx.id,
                    name=str(data.get("name") or native_id),
                    cwd=data.get("cwd"),
                    git_branch=data.get("git_branch"),
                    status=str(data.get("status") or STATUS_UNKNOWN) or STATUS_IDLE,
                    detail=data.get("detail"),
                    pid=pid if isinstance(pid, int) else None,
                    started_at=data.get("started_at"),
                    updated_at=data.get("updated_at") or mtime,
                    tokens=data.get("tokens") if isinstance(data.get("tokens"), int) else None,
                    attach=AttachSpec.unavailable("registered via heartbeat file"),
                    source=self.name,
                    extra={"path": str(path)},
                )
            )
        return records, warnings
