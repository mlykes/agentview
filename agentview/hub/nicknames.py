"""Display names agentview keeps for agents.

Renaming here does *not* rename the session in its harness. There is no supported way
to do that from outside: Claude Code has no `rename` subcommand, and driving its
`/rename` command through the PTY would type into a live prompt -- appending to
whatever the user had half-typed, or queueing a message to a busy agent. Neither is
something a viewer should do behind your back.

So the name is agentview's own label. The harness's name is kept alongside it as
``harness_name`` rather than discarded, so nothing is hidden and clearing the label
restores the original.

Stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Dict, Optional

#: Long enough for a sentence fragment, short enough to stay on one row.
MAX_NAME = 64

#: Control characters would corrupt the row (and the terminal, for anyone piping
#: `/v1/agents` through a shell), so they never make it into the store.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def default_path() -> Path:
    return Path(os.environ.get("AGENTVIEW_HOME") or (Path.home() / ".agentview")) / "names.json"


def clean(name: Optional[str]) -> Optional[str]:
    """Normalise a submitted name, or None if it is empty and should clear the label."""
    if not isinstance(name, str):
        return None
    name = _CONTROL.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        return None
    return name[:MAX_NAME]


class Nicknames:
    """A tiny persistent map of agent id -> label.

    Kept in memory and written through on every change: the file is a handful of
    short strings, and losing a rename because the hub was killed would be worse
    than the write.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else default_path()
        self._lock = threading.Lock()
        self._names: Dict[str, str] = self._load()

    def _load(self) -> Dict[str, str]:
        try:
            with self.path.open("r", errors="replace") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(k): clean(v)
            for k, v in data.items()
            if isinstance(k, str) and clean(v)
        }

    def _save(self) -> None:
        # Write-and-rename, so a crash mid-write cannot leave a truncated file that
        # would drop every label on the next start.
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            with tmp.open("w") as fh:
                json.dump(self._names, fh, indent=2, sort_keys=True)
            os.replace(str(tmp), str(self.path))
        except OSError:
            pass  # a label that fails to persist is not worth failing the request over

    def get(self, agent_id: Optional[str]) -> Optional[str]:
        if not agent_id:
            return None
        with self._lock:
            return self._names.get(agent_id)

    def set(self, agent_id: str, name: Optional[str]) -> Optional[str]:
        """Set a label, or clear it when ``name`` is empty. Returns what was stored."""
        cleaned = clean(name)
        with self._lock:
            if cleaned is None:
                self._names.pop(agent_id, None)
            else:
                self._names[agent_id] = cleaned
            self._save()
        return cleaned

    def all(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._names)
