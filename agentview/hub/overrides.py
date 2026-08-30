"""Per-agent display settings agentview keeps for itself: label and colour.

Neither is written back to the harness, and that is deliberate rather than a
shortcut.

*Label.* Claude Code has no `rename` subcommand, and driving its `/rename` through
the PTY would type into a live prompt -- appending to whatever was half-typed, or
queueing a message to a busy agent. A viewer should not do that behind your back.

*Colour.* The harness's colour is used whenever it records one, which is often not
at all: a `/color` set in an interactive session is never written to disk (the
session file is rewritten seconds later without it), and a background session that
never got a ``state.json`` -- a pre-warmed spare, for instance -- has nowhere to keep
one. A colour set here fills that gap, and takes precedence when both exist: an
explicit choice made in agentview should not be overruled by an inherited default.
The harness's colour is kept as ``harness_color`` when it is displaced.

The harness's own name is preserved alongside the label as ``harness_name``, so
clearing an override restores what the harness calls it.

Stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from agentview.hub.locks import file_lock

#: Long enough for a sentence fragment, short enough to stay on one row.
MAX_NAME = 64

#: Control characters would corrupt the row (and the terminal of anyone piping
#: `/v1/agents` through a shell), so they never make it into the store.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

#: Colours the UI has a token for. An allowlist rather than free text: the value is
#: interpolated into a CSS custom property name, and anything outside this set would
#: silently render as no colour at all.
COLOURS = (
    "red", "orange", "yellow", "green", "teal", "cyan",
    "blue", "purple", "magenta", "pink", "gray",
)


def default_path() -> Path:
    return Path(os.environ.get("AGENTVIEW_HOME") or (Path.home() / ".agentview")) / "names.json"


def clean_name(name: Optional[str]) -> Optional[str]:
    """Normalise a submitted label, or None if it is empty and should clear it."""
    if not isinstance(name, str):
        return None
    name = _CONTROL.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        return None
    return name[:MAX_NAME]


def clean_colour(colour: Optional[str]) -> Optional[str]:
    """Normalise a submitted colour, or None to fall back to the harness's own."""
    if not isinstance(colour, str):
        return None
    colour = colour.strip().lower()
    return colour if colour in COLOURS else None


class Overrides:
    """A small persistent map of agent id -> {"name": ..., "color": ...}.

    Written through on every change: the file is a handful of short strings, and
    losing an edit because the hub was killed would be worse than the write.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else default_path()
        self._lock = threading.Lock()
        self._entries: Dict[str, Dict[str, str]] = self._load()
        self._stamp = self._mtime()

    def _mtime(self):
        try:
            stat = self.path.stat()
            return (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return None

    def _reload_if_changed(self) -> None:
        stamp = self._mtime()
        if stamp != self._stamp:
            self._entries = self._load()
            self._stamp = stamp

    # -- persistence ------------------------------------------------------

    @staticmethod
    def _coerce(value: Any) -> Dict[str, str]:
        """Accept both shapes on disk.

        The file started life as a flat ``{id: name}`` map. Reading that shape keeps
        labels made before colours existed, and the file is editable by hand, so
        neither shape can be trusted to be clean.
        """
        if isinstance(value, str):
            name = clean_name(value)
            return {"name": name} if name else {}
        if not isinstance(value, dict):
            return {}
        entry: Dict[str, str] = {}
        name = clean_name(value.get("name"))
        if name:
            entry["name"] = name
        colour = clean_colour(value.get("color"))
        if colour:
            entry["color"] = colour
            at = value.get("color_at")
            if isinstance(at, (int, float)) and at > 0:
                entry["color_at"] = float(at)
            if value.get("color_pending"):
                entry["color_pending"] = True
        return entry

    def _load(self) -> Dict[str, Dict[str, str]]:
        try:
            with self.path.open("r", errors="replace") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        out: Dict[str, Dict[str, str]] = {}
        for key, value in data.items():
            if not isinstance(key, str):
                continue
            entry = self._coerce(value)
            if entry:
                out[key] = entry
        return out

    def _save(self) -> None:
        # Write-and-rename, so a crash mid-write cannot leave a truncated file that
        # would drop every override on the next start.
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name("{}.{}.{}.tmp".format(
                self.path.name, os.getpid(), threading.get_ident()
            ))
            with tmp.open("w") as fh:
                json.dump(self._entries, fh, indent=2, sort_keys=True)
            os.replace(str(tmp), str(self.path))
            self._stamp = self._mtime()
        except OSError:
            pass  # an override that fails to persist is not worth failing a request over

    # -- reads ------------------------------------------------------------

    def get(self, agent_id: Optional[str]) -> Dict[str, str]:
        if not agent_id:
            return {}
        with self._lock:
            self._reload_if_changed()
            return dict(self._entries.get(agent_id) or {})

    def take_pending_colour(self, agent_id: str) -> Optional[str]:
        """The colour still owed to the session, cleared as it is handed over.

        Cleared on the way out rather than after a successful write: a colour that
        failed to reach the session should not be retried on every reconnect, since
        each attempt types into a live prompt.
        """
        with self._lock:
            with file_lock(self.path.with_name(self.path.name + ".lock")):
                self._entries = self._load()
                entry = self._entries.get(agent_id)
                if not entry or not entry.get("color_pending"):
                    return None
                colour = entry.get("color")
                entry.pop("color_pending", None)
                self._save()
                return colour

    def all(self) -> Dict[str, Dict[str, str]]:
        with self._lock:
            self._reload_if_changed()
            return {k: dict(v) for k, v in self._entries.items()}

    # -- writes -----------------------------------------------------------

    def _set(self, agent_id: str, field: str, value: Optional[str]) -> Optional[str]:
        with self._lock:
            with file_lock(self.path.with_name(self.path.name + ".lock")):
                # Reload inside the process lock: another hub may have changed an
                # unrelated agent since our last read.
                self._entries = self._load()
                entry = dict(self._entries.get(agent_id) or {})
                if value is None:
                    entry.pop(field, None)
                else:
                    entry[field] = value
                if entry:
                    self._entries[agent_id] = entry
                else:
                    self._entries.pop(agent_id, None)
                self._save()
        return value

    def set_name(self, agent_id: str, name: Optional[str]) -> Optional[str]:
        """Set a label, or clear it when ``name`` is empty."""
        return self._set(agent_id, "name", clean_name(name))

    def set_colour(
        self, agent_id: str, colour: Optional[str], push: bool = False
    ) -> Optional[str]:
        """Set a colour, or clear it to fall back to the harness's own.

        Stamped with the time, so a colour set here and one set with `/color` in the
        session can be ordered against each other. Without that the two sources can
        only disagree: whichever one is declared the winner overrules the other no
        matter which the user actually touched last.
        """
        value = clean_colour(colour)
        with self._lock:
            with file_lock(self.path.with_name(self.path.name + ".lock")):
                self._entries = self._load()
                entry = dict(self._entries.get(agent_id) or {})
                if value is None:
                    entry.pop("color", None)
                    entry.pop("color_at", None)
                    entry.pop("color_pending", None)
                else:
                    entry["color"] = value
                    entry["color_at"] = time.time()
                    # Queued for the session itself. Setting a colour here cannot reach
                    # the agent until there is a terminal to type it into, so it waits
                    # for the next time one is opened rather than being lost.
                    if push:
                        entry["color_pending"] = True
                if entry:
                    self._entries[agent_id] = entry
                else:
                    self._entries.pop(agent_id, None)
                self._save()
        return value
