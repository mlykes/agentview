"""Shared vocabulary for agentview.

These dataclasses are the contract between collectors, the hub, and any frontend
(web today, TUI later). They are also what PROTOCOL.md documents, so a future Go or
TypeScript port has something concrete to target.

Stdlib only -- this module is imported by the collector.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Status values an agent can report. Kept deliberately small; harness-specific
# vocabulary gets normalized into these by each adapter.
STATUS_BUSY = "busy"
STATUS_IDLE = "idle"
STATUS_BLOCKED = "blocked"  # waiting on a human
STATUS_DONE = "done"        # finished its turn; the process is still around
STATUS_FAILED = "failed"    # stopped because something broke
STATUS_UNKNOWN = "unknown"

#: Work has stopped and will not resume on its own. The stuck detector ignores
#: these, and so should anything that measures how long an agent has been quiet.
TERMINAL_STATUSES = (STATUS_DONE, STATUS_FAILED)

CONTEXT_HOST = "host"
CONTEXT_CONTAINER = "container"


@dataclass
class AttachSpec:
    """How to get a PTY onto an agent's own terminal UI.

    Attach is *just an argv*. That is the whole trick that makes one code path work
    for local, container and remote agents alike -- only the argv differs.
    """

    available: bool = False
    #: Why attach is unavailable, shown directly in the UI. Be honest here rather
    #: than silently disabling the button.
    reason: Optional[str] = None
    #: Command yielding a normal interactive PTY -- the terminal behaves like any
    #: other terminal you would run this agent in.
    argv: Optional[List[str]] = None
    #: Read-only variant, used only when the hub runs with --read-only.
    argv_readonly: Optional[List[str]] = None

    @classmethod
    def unavailable(cls, reason: str) -> "AttachSpec":
        return cls(available=False, reason=reason)


@dataclass
class ContextRef:
    """Where a collector -- and therefore its agents -- is running.

    A context is a machine or a container. Containers carry ``parent_id`` so the UI
    can nest a devcontainer inside its host instead of listing it as a sibling.
    """

    id: str
    kind: str = CONTEXT_HOST
    label: str = ""
    hostname: str = ""
    platform: str = ""  # darwin | linux | windows
    arch: str = ""
    #: Set on containers: the context id of the host running them, when derivable.
    parent_id: Optional[str] = None
    #: True when this collector's own session arrived over SSH.
    via_ssh: bool = False
    container_id: Optional[str] = None
    container_name: Optional[str] = None
    #: devcontainer ``workspaceFolder`` when we can read it.
    workspace_folder: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class AgentRecord:
    """One running agent, normalized across harnesses."""

    #: Globally unique and stable across ticks: "<context_id>:<harness>:<native_id>".
    id: str
    harness: str  # machine key, e.g. "claude-code"
    context_id: str
    #: Human label for the harness, e.g. "Claude Code".
    harness_label: str = ""
    harness_version: Optional[str] = None
    name: str = ""
    cwd: Optional[str] = None
    git_branch: Optional[str] = None
    status: str = STATUS_UNKNOWN
    #: Last human-readable state line, e.g. "awaiting project details".
    detail: Optional[str] = None
    pid: Optional[int] = None
    started_at: Optional[float] = None  # epoch seconds
    updated_at: Optional[float] = None  # epoch seconds
    tokens: Optional[int] = None
    #: Display accent, when the harness exposes one.
    color: Optional[str] = None
    attach: AttachSpec = field(default_factory=AttachSpec)
    #: Which adapter produced this record, for debugging merge precedence.
    source: str = ""
    #: Harness-specific extras that do not deserve a first-class field.
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class Snapshot:
    """What a collector emits each tick."""

    context: ContextRef
    agents: List[AgentRecord] = field(default_factory=list)
    collected_at: float = 0.0
    #: Non-fatal problems (an adapter that failed, an unreadable file). Surfaced in
    #: the UI so a silently-empty HUD is distinguishable from a genuinely idle one.
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "context": self.context.to_dict(),
            "agents": [a.to_dict() for a in self.agents],
            "collected_at": self.collected_at,
            "warnings": list(self.warnings),
        }
