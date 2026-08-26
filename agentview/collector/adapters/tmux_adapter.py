"""Generic tmux discovery -- the harness-agnostic path.

We cannot write file-format parsers for harnesses we have never run. We can notice
that something in the harness table is running inside a tmux pane. That is how
agentview supports Opencode, Pi, Aider and anything else on day one: recognise the
process, and attach to the terminal it already draws.

Records from here are intentionally thin -- name, cwd, status guess. A dedicated
adapter (see claude_code.py) wins the merge when one exists.

Stdlib only.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from agentview import harnesses
from agentview.collector import tmux
from agentview.collector.adapters.base import Adapter
from agentview.collector.procs import command_table, descendants, parent_map
from agentview.model import STATUS_UNKNOWN, AgentRecord, AttachSpec, ContextRef


def attach_for_session(session: str) -> AttachSpec:
    """Read-only by default. A stray keystroke into a running agent is a real way
    to derail it, so input is opt-in per session in the UI."""
    return AttachSpec(
        available=True,
        argv=["tmux", "attach", "-r", "-t", session],
        argv_readwrite=["tmux", "attach", "-t", session],
    )


class TmuxAdapter(Adapter):
    name = "tmux"
    priority = 30

    def __init__(self, table: Optional[Dict[str, Dict[str, str]]] = None) -> None:
        self.table = table if table is not None else harnesses.load()

    def available(self) -> bool:
        return tmux.available()

    def discover(self, ctx: ContextRef) -> Tuple[List[AgentRecord], List[str]]:
        panes = tmux.list_panes()
        if not panes:
            return [], []

        commands = command_table()
        parents = parent_map()
        records: List[AgentRecord] = []

        for pane in panes:
            # The pane's own command is usually the shell, so look through everything
            # running beneath it too. Deepest match wins: `bash -> node -> opencode`
            # should report opencode, not bash.
            identity = harnesses.identify(commands.get(pane.pid, pane.command), self.table)
            agent_pid = pane.pid

            if not identity:
                for pid in sorted(descendants(pane.pid, parents)):
                    found = harnesses.identify(commands.get(pid, ""), self.table)
                    if found:
                        identity, agent_pid = found, pid
                        break
            if not identity:
                continue

            records.append(
                AgentRecord(
                    id="{}:{}:tmux-{}".format(ctx.id, identity["harness"], pane.session),
                    harness=identity["harness"],
                    harness_label=identity["label"],
                    context_id=ctx.id,
                    name=pane.session,
                    cwd=pane.path or None,
                    status=STATUS_UNKNOWN,
                    pid=agent_pid,
                    attach=attach_for_session(pane.session),
                    source=self.name,
                    extra={
                        "tmux_session": pane.session,
                        "pane_pid": pane.pid,
                        "command": commands.get(agent_pid, pane.command),
                    },
                )
            )
        return records, []
