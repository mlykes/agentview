"""Collector core: run every adapter, merge the results, emit a Snapshot.

Stdlib only.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, List, Optional

from agentview.collector import context as context_mod
from agentview.collector import tmux
from agentview.collector.adapters.tmux_adapter import attach_for_session
from agentview.collector.adapters.base import Adapter
from agentview.collector.adapters.claude_code import ClaudeCodeAdapter
from agentview.collector.adapters.codex import CodexAdapter
from agentview.collector.adapters.heartbeat import HeartbeatAdapter
from agentview.collector.adapters.tmux_adapter import TmuxAdapter
from agentview.model import AgentRecord, ContextRef, Snapshot

#: Fields a lower-priority adapter is allowed to fill in when a higher-priority one
#: left them empty. Everything else belongs to the winning adapter.
_ENRICHABLE = ("git_branch", "cwd", "harness_version", "detail", "tokens", "name")


def default_adapters(config_dir: Optional[Path] = None) -> List[Adapter]:
    """Adapters in priority order.

    M5 adds the generic tmux/process/heartbeat adapters here; the merge below
    already handles them.
    """
    return [
        ClaudeCodeAdapter(config_dir=config_dir),
        CodexAdapter(),
        TmuxAdapter(),
        HeartbeatAdapter(),
    ]


def merge(groups: List[List[AgentRecord]]) -> List[AgentRecord]:
    """Merge per-adapter results by record id, richest adapter first."""
    merged: Dict[str, AgentRecord] = {}
    for records in groups:
        for record in records:
            existing = merged.get(record.id)
            if existing is None:
                merged[record.id] = record
                continue
            # Same agent seen by a lower-priority adapter: keep the richer record
            # but let the newcomer fill in blanks.
            for fieldname in _ENRICHABLE:
                if getattr(existing, fieldname, None) in (None, "") and getattr(
                    record, fieldname, None
                ) not in (None, ""):
                    setattr(existing, fieldname, getattr(record, fieldname))
            # Attach is all-or-nothing: take it if we don't have a working one.
            if not existing.attach.available and record.attach.available:
                existing.attach = record.attach
    return sorted(merged.values(), key=lambda r: (r.harness, r.name.lower()))


def collect(
    ctx: Optional[ContextRef] = None,
    adapters: Optional[List[Adapter]] = None,
    config_dir: Optional[Path] = None,
) -> Snapshot:
    ctx = ctx or context_mod.detect()
    adapters = adapters if adapters is not None else default_adapters(config_dir)

    groups: List[List[AgentRecord]] = []
    warnings: List[str] = []

    for adapter in sorted(adapters, key=lambda a: a.priority):
        try:
            if not adapter.available():
                continue
            records, adapter_warnings = adapter.discover(ctx)
        except Exception as exc:  # noqa: BLE001 - one bad adapter must not kill the HUD
            warnings.append("{}: adapter failed: {}".format(adapter.name, exc))
            continue
        groups.append(records)
        warnings.extend(adapter_warnings)

    agents = merge(groups)
    agents = drop_shadowed_tmux_records(agents)
    apply_attach(agents)

    return Snapshot(
        context=ctx,
        agents=agents,
        collected_at=time.time(),
        warnings=warnings,
    )


def drop_shadowed_tmux_records(records: List[AgentRecord]) -> List[AgentRecord]:
    """Remove generic tmux records that describe an agent a real adapter already found.

    The two adapters key on different things -- Claude Code on its session id, tmux on
    the pane -- so id-based merging cannot see they are the same agent. They are the
    same agent exactly when the richer record's process lives inside the tmux record's
    pane. This has to happen here rather than in an adapter, because it is the only
    place with the whole picture.
    """
    tmux_records = [r for r in records if r.source == "tmux" and r.pid]
    if not tmux_records:
        return records

    from agentview.collector.procs import ancestors, parent_map

    parents = parent_map()
    pane_pids = {r.pid: r for r in tmux_records}
    shadowed = set()

    for record in records:
        if record.source == "tmux" or not record.pid:
            continue
        for parent in [record.pid] + ancestors(record.pid, parents):
            owner = pane_pids.get(parent)
            if owner is not None:
                shadowed.add(owner.id)
                # Hand the attach path to the record that survives.
                record.attach = owner.attach
                record.extra.setdefault("tmux_session", owner.extra.get("tmux_session"))
                break

    return [r for r in records if r.id not in shadowed]


def apply_attach(records: List[AgentRecord]) -> None:
    """Give every agent running inside tmux a working attach path.

    An agent started outside a multiplexer keeps whatever its adapter said -- normally
    an explicit "no attach" with a reason, which the UI shows rather than silently
    disabling the affordance.
    """
    panes = tmux.list_panes()
    if not panes:
        return

    from agentview.collector.procs import parent_map

    parents = parent_map()
    for record in records:
        if record.attach.available or not record.pid:
            continue
        session = tmux.session_for_pid(record.pid, panes, parents)
        if session:
            record.attach = attach_for_session(session)
            record.extra["tmux_session"] = session
