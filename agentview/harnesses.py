"""Which commands count as coding agents.

Adding support for a new harness is a line in a JSON file, not a code change. That is
the difference between "supports any harness" being true and being aspirational --
we cannot write file-format parsers for tools we have never run, but we can recognise
them in a process list.

Override or extend by writing ~/.agentview/harnesses.json:

    {"mytool": {"harness": "mytool", "label": "MyTool"}}

Stdlib only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

#: command name (as it appears in a process list) -> identity
DEFAULT_HARNESSES: Dict[str, Dict[str, str]] = {
    "claude": {"harness": "claude-code", "label": "Claude Code"},
    "opencode": {"harness": "opencode", "label": "Opencode"},
    "pi": {"harness": "pi", "label": "Pi"},
    "codex": {"harness": "codex", "label": "Codex"},
    "aider": {"harness": "aider", "label": "Aider"},
    "goose": {"harness": "goose", "label": "Goose"},
    "crush": {"harness": "crush", "label": "Crush"},
    "cursor-agent": {"harness": "cursor-agent", "label": "Cursor Agent"},
    "amp": {"harness": "amp", "label": "Amp"},
    "gemini": {"harness": "gemini", "label": "Gemini CLI"},
    "droid": {"harness": "droid", "label": "Droid"},
}


def config_path() -> Path:
    return Path(os.environ.get("AGENTVIEW_HOME") or (Path.home() / ".agentview")) / "harnesses.json"


def load() -> Dict[str, Dict[str, str]]:
    table = dict(DEFAULT_HARNESSES)
    try:
        raw: Any = json.loads(config_path().read_text(errors="replace"))
    except (OSError, ValueError):
        return table
    if isinstance(raw, dict):
        for command, identity in raw.items():
            if isinstance(identity, dict) and identity.get("harness"):
                table[str(command)] = {
                    "harness": str(identity["harness"]),
                    "label": str(identity.get("label") or identity["harness"]),
                }
    return table


def identify(command: str, table: Dict[str, Dict[str, str]] = None) -> Dict[str, str]:
    """Match a process command against the harness table.

    Handles the common shapes a process list produces: a bare name, an absolute
    path, and a name with arguments or a process title appended.
    """
    table = table if table is not None else load()
    if not command:
        return {}
    parts = command.strip().split()
    if not parts:
        return {}

    # Only argv[0] and argv[1] are considered. argv[0] covers a direct invocation;
    # argv[1] covers the interpreter case (`node .../opencode`, `python3 .../aider`,
    # a shell running a script). Scanning the whole command line instead would match
    # any path that happens to contain a harness name -- "pi" alone would fire on
    # half the filesystem.
    for candidate in parts[:2]:
        base = os.path.basename(candidate)
        if base in table:
            return table[base]
    return {}
