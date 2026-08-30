"""Poll configured SSH hosts and containers with instance-scoped collector code."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agentview.hub.locks import file_lock


def default_path() -> Path:
    return Path(os.environ.get("AGENTVIEW_HOME") or (Path.home() / ".agentview")) / "remotes.json"


class RemoteStore:
    """Concurrency-safe, hand-editable topology configuration."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path or default_path())
        self._lock = threading.Lock()

    def read(self) -> Dict[str, List[Dict[str, str]]]:
        with self._lock:
            try:
                data = json.loads(self.path.read_text())
            except (OSError, ValueError):
                data = {}
        result = {"ssh": [], "containers": []}
        for kind in result:
            values = data.get(kind, []) if isinstance(data, dict) else []
            for value in values if isinstance(values, list) else []:
                if isinstance(value, str):
                    result[kind].append({"host" if kind == "ssh" else "name": value})
                elif isinstance(value, dict):
                    result[kind].append({str(k): str(v) for k, v in value.items()})
        return result

    def update(self, mutator) -> Dict[str, Any]:
        """Locked read-modify-write hook for future CLI/UI configuration."""
        with self._lock, file_lock(self.path.with_name(self.path.name + ".lock")):
            try:
                data = json.loads(self.path.read_text())
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
            updated = mutator(dict(data))
            if updated is None:
                updated = data
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp",
                                       dir=str(self.path.parent))
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump(updated, fh, indent=2, sort_keys=True)
                os.replace(tmp, str(self.path))
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return updated


def deployment_dir(kind: str, instance: str) -> str:
    if kind == "ssh":
        return "~/.agentview/code/{}".format(instance)
    if kind == "container":
        return "/tmp/.agentview-code-{}".format(instance)
    raise ValueError("unknown target kind: {}".format(kind))


def collector_archive(checkout: Path) -> bytes:
    """Package only runtime Python, with stable member names for both transports."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        package = Path(checkout) / "agentview"
        for path in package.rglob("*.py"):
            archive.add(str(path), arcname=str(path.relative_to(checkout)))
    return buf.getvalue()


class TopologyPoller:
    def __init__(self, registry, store: RemoteStore, checkout: Path, instance: str,
                 interval: float = 3.0) -> None:
        self.registry = registry
        self.store = store
        self.checkout = Path(checkout)
        self.instance = instance
        self.interval = interval
        self._archive = None

    def _bundle(self):
        if self._archive is None:
            self._archive = collector_archive(self.checkout)
        return self._archive

    def sync_and_collect_ssh(self, target: Dict[str, str]) -> Dict[str, Any]:
        host = target["host"]
        deploy = deployment_dir("ssh", self.instance)
        subprocess.run(["ssh", host, "mkdir", "-p", deploy], check=True, timeout=30,
                       capture_output=True)
        subprocess.run(["ssh", host, "tar", "-xzf", "-", "-C", deploy],
                       input=self._bundle(), check=True, timeout=60, capture_output=True)
        result = subprocess.run(
            ["ssh", host, "env", "PYTHONPATH=" + deploy,
             "python3", "-m", "agentview.collector", "--once"],
            check=True, timeout=60, capture_output=True, text=True,
        )
        return json.loads(result.stdout)

    def sync_and_collect_container(self, target: Dict[str, str]) -> Dict[str, Any]:
        name = target["name"]
        engine = target.get("engine", "docker")
        deploy = deployment_dir("container", self.instance)
        subprocess.run([engine, "exec", name, "mkdir", "-p", deploy], check=True,
                       timeout=30, capture_output=True)
        subprocess.run([engine, "exec", "-i", name, "tar", "-xzf", "-", "-C", deploy],
                       input=self._bundle(), check=True, timeout=60, capture_output=True)
        result = subprocess.run(
            [engine, "exec", name, "env", "PYTHONPATH=" + deploy,
             "python3", "-m", "agentview.collector", "--once"],
            check=True, timeout=60, capture_output=True, text=True,
        )
        return json.loads(result.stdout)

    def tick(self) -> None:
        config = self.store.read()  # reload edits made by the other hub every tick
        for target in config["ssh"]:
            if target.get("host"):
                try:
                    self.registry.ingest(self.sync_and_collect_ssh(target))
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
        for target in config["containers"]:
            if target.get("name"):
                try:
                    self.registry.ingest(self.sync_and_collect_container(target))
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass

    def run(self) -> None:
        while True:
            self.tick()
            time.sleep(self.interval)
