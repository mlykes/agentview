"""Collector core: run every adapter, merge the results, emit a Snapshot.

Stdlib only.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, List, Optional

from agentview.collector import context as context_mod
from agentview.collector.adapters.base import Adapter
from agentview.collector.adapters.claude_code import ClaudeCodeAdapter
from agentview.collector.adapters.heartbeat import HeartbeatAdapter
from agentview.model import AgentRecord, ContextRef, Snapshot

#: Fields a lower-priority adapter is allowed to fill in when a higher-priority one
#: left them empty. Everything else belongs to the winning adapter.
_ENRICHABLE = ("git_branch", "cwd", "harness_version", "detail", "tokens", "name")


def default_adapters(config_dir: Optional[Path] = None) -> List[Adapter]:
    """Adapters in priority order.

    M5 adds the generic tmux/process/heartbeat adapters here; the merge below
    already handles them.
    """
    return [ClaudeCodeAdapter(config_dir=config_dir), HeartbeatAdapter()]


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

    return Snapshot(
        context=ctx,
        agents=merge(groups),
        collected_at=time.time(),
        warnings=warnings,
    )
