"""Hub registry: merge snapshots from every collector, expire the dead ones.

A collector that dies must not leave its agents on screen forever, so entries are
TTL-expired. This is the hub's only real logic -- everything else is transport.

Stdlib only, like the rest of agentview.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

#: A context is dropped if we have not heard from its collector in this long.
#: Three snapshot intervals, so one missed tick does not make agents flicker.
DEFAULT_TTL_SECONDS = 15.0

#: A busy agent whose updated_at has not moved in this long is probably wedged.
#:
#: Calibrated against real sessions rather than guessed: a harness updates this
#: timestamp on status *changes*, not continuously, so a single long turn can go
#: many minutes without touching it. Five minutes flagged an agent that was simply
#: thinking hard. Fifteen is long enough that a genuine wedge is the likelier
#: explanation; tune with --stuck-after.
DEFAULT_STUCK_SECONDS = 900.0


class Registry:
    def __init__(
        self,
        ttl: float = DEFAULT_TTL_SECONDS,
        stuck_after: float = DEFAULT_STUCK_SECONDS,
    ) -> None:
        self._lock = threading.Lock()
        self._contexts: Dict[str, Dict[str, Any]] = {}
        self.ttl = ttl
        self.stuck_after = stuck_after

    def ingest(self, snapshot: Dict[str, Any]) -> None:
        """Accept a snapshot from one collector, replacing that context's view."""
        context = snapshot.get("context") or {}
        context_id = context.get("id")
        if not context_id:
            return
        with self._lock:
            self._contexts[context_id] = {
                "context": context,
                "agents": snapshot.get("agents") or [],
                "warnings": snapshot.get("warnings") or [],
                "collected_at": snapshot.get("collected_at") or time.time(),
                "received_at": time.time(),
            }

    def _live(self, now: float) -> List[Dict[str, Any]]:
        return [
            entry
            for entry in self._contexts.values()
            if now - entry["received_at"] <= self.ttl
        ]

    def prune(self) -> None:
        now = time.time()
        with self._lock:
            dead = [
                cid
                for cid, entry in self._contexts.items()
                if now - entry["received_at"] > self.ttl
            ]
            for cid in dead:
                del self._contexts[cid]

    def view(self) -> Dict[str, Any]:
        """The aggregated view the UI renders.

        Contexts are nested: a container whose ``parent_id`` names a live context is
        attached to that parent rather than listed as a sibling. A container whose
        parent we have never seen stays top-level -- better a flat list than a
        wrong tree.
        """
        now = time.time()
        with self._lock:
            entries = self._live(now)

        by_id = {e["context"]["id"]: e for e in entries}
        children: Dict[str, List[Dict[str, Any]]] = {}
        roots: List[Dict[str, Any]] = []

        for entry in entries:
            node = {
                "context": entry["context"],
                "agents": [self._annotate(a, now) for a in entry["agents"]],
                "warnings": entry["warnings"],
                "collected_at": entry["collected_at"],
                "age": round(now - entry["received_at"], 1),
                "children": [],
            }
            parent_id = entry["context"].get("parent_id")
            if parent_id and parent_id in by_id and parent_id != entry["context"]["id"]:
                children.setdefault(parent_id, []).append(node)
            else:
                roots.append(node)

        for node in roots:
            node["children"] = children.get(node["context"]["id"], [])

        total = sum(len(n["agents"]) for n in roots) + sum(
            len(c["agents"]) for n in roots for c in n["children"]
        )
        busy = sum(
            1
            for n in roots
            for c in [n] + n["children"]
            for a in c["agents"]
            if a.get("status") == "busy"
        )
        stuck = sum(
            1
            for n in roots
            for c in [n] + n["children"]
            for a in c["agents"]
            if a.get("stuck")
        )

        roots.sort(key=lambda n: n["context"].get("label", ""))
        return {
            "contexts": roots,
            "totals": {"agents": total, "busy": busy, "stuck": stuck,
                       "contexts": len(entries)},
            "served_at": now,
        }

    def _annotate(self, agent: Dict[str, Any], now: float) -> Dict[str, Any]:
        """Add derived fields the UI needs but a collector should not compute."""
        agent = dict(agent)
        started = agent.get("started_at")
        updated = agent.get("updated_at")
        agent["uptime"] = (now - started) if isinstance(started, (int, float)) else None
        idle_for = (now - updated) if isinstance(updated, (int, float)) else None
        agent["idle_for"] = idle_for
        # The question the HUD exists to answer: is anything wedged?
        agent["stuck"] = bool(
            agent.get("status") == "busy"
            and idle_for is not None
            and idle_for > self.stuck_after
        )
        return agent

    def flat_agents(self) -> List[Dict[str, Any]]:
        """Every live agent, ungrouped -- for `GET /v1/agents` and the TUI."""
        now = time.time()
        with self._lock:
            entries = self._live(now)
        return [self._annotate(a, now) for e in entries for a in e["agents"]]

    def contexts(self) -> List[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            return [e["context"] for e in self._live(now)]

    def find_agent(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """One agent by id, with its context attached.

        The hub reads the attach argv from here rather than accepting it from the
        browser. A client-supplied argv would be arbitrary command execution behind
        a loopback port.
        """
        now = time.time()
        with self._lock:
            entries = self._live(now)
        for entry in entries:
            for agent in entry["agents"]:
                if agent.get("id") == agent_id:
                    found = dict(agent)
                    found["context"] = entry["context"]
                    return found
        return None
