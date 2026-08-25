"""Adapter interface.

An adapter discovers agents from one source. Adapters are tried in priority order and
their results merged by ``AgentRecord.id``; earlier (richer) adapters win on conflict,
later ones may fill in fields the earlier left blank.

Stdlib only.
"""

from __future__ import annotations

from typing import List, Tuple

from agentview.model import AgentRecord, ContextRef


class Adapter:
    #: Machine key, also used as ``AgentRecord.source``.
    name = "base"
    #: Lower number = higher priority when merging.
    priority = 100

    def available(self) -> bool:
        """Cheap check for whether this adapter can do anything on this box."""
        return False

    def discover(self, ctx: ContextRef) -> Tuple[List[AgentRecord], List[str]]:
        """Return (records, warnings). Must never raise for routine problems --
        an unreadable file is a warning, not a crash, or one bad session takes the
        whole HUD down."""
        raise NotImplementedError
