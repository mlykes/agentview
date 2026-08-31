"""Human-readable rendering of a Snapshot, for `--format table`.

This is a debugging convenience, not the HUD. It also doubles as the proof that the
snapshot carries everything a TUI would need -- see agentview/tui/.

Stdlib only.
"""

from __future__ import annotations

import time
from typing import Optional

from agentview.model import (
    STATUS_BLOCKED,
    STATUS_BUSY,
    STATUS_DONE,
    STATUS_FAILED,
    Snapshot,
)

_DOT = {STATUS_BUSY: "*", STATUS_BLOCKED: "?", STATUS_DONE: "v", STATUS_FAILED: "x"}


def _age(ts: Optional[float], now: float) -> str:
    if not ts:
        return "-"
    delta = max(0, int(now - ts))
    if delta < 60:
        return "{}s".format(delta)
    if delta < 3600:
        return "{}m".format(delta // 60)
    if delta < 86400:
        return "{}h".format(delta // 3600)
    return "{}d".format(delta // 86400)


def render(snapshot: Snapshot) -> str:
    now = snapshot.collected_at or time.time()
    ctx = snapshot.context
    header = "{} - {}/{}".format(ctx.label, ctx.platform, ctx.arch)
    if ctx.via_ssh:
        header += " - ssh"
    if ctx.workspace_folder:
        header += " - {}".format(ctx.workspace_folder)

    lines = ["{}   {} agent(s)".format(header, len(snapshot.agents))]
    for agent in snapshot.agents:
        bits = [
            "  {} {:<28.28}".format(_DOT.get(agent.status, "-"), agent.name),
            "{:<14.14}".format(agent.harness_label or agent.harness),
            "{:<24.24}".format(agent.cwd or "-"),
            "{:<7.7}".format(agent.status),
            "{:>4}".format(_age(agent.started_at, now)),
        ]
        if agent.git_branch:
            bits.append(" [{}]".format(agent.git_branch))
        if agent.tokens:
            bits.append(" {} tok".format(agent.tokens))
        lines.append(" ".join(bits))
        if agent.detail:
            lines.append("      {:.100}".format(agent.detail.replace("\n", " ")))
    for warning in snapshot.warnings:
        lines.append("  ! {}".format(warning))
    return "\n".join(lines)
