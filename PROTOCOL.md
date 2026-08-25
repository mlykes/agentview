# agentview wire protocol

Version `v1` (draft — M1 defines the data model; the transport lands in M2).

This document exists for two reasons:

1. The Python implementation is a proof of concept. When the collector is ported to a
   single static binary (Go is the likely target), this is the spec it must satisfy —
   so the port is a port, not a redesign.
2. A TUI, a `curl | jq` one-liner, and the web UI are all just clients. Anything the
   web UI can render, they can render.

## Roles

| Role | Listens? | Runs where |
|---|---|---|
| **Collector** | **No** | Every machine and container with agents on it |
| **Hub** | Yes, loopback | One machine; the UI is served from here |

Collectors **dial out** to the hub and keep the connection alive. No collector ever
binds a port. The hub's loopback socket is the only listening socket in the system,
reached from a laptop with `ssh -L 7788:localhost:7788 <host>`. This is deliberate:
it means deploying agentview adds nothing for a security team to review.

## Objects

### ContextRef

Where a collector — and therefore its agents — is running. The collector self-reports;
the hub believes it, because nothing else *can* know.

| Field | Type | Notes |
|---|---|---|
| `id` | string | `host-<hash>` or `ctr-<hash>`; stable across restarts |
| `kind` | `"host"` \| `"container"` | |
| `label` | string | Display name, e.g. `devbox` or `devcontainer: datacollector` |
| `hostname` | string | |
| `platform` | string | `darwin` \| `linux` \| `windows` |
| `arch` | string | |
| `parent_id` | string \| null | Container → its host's context id, for nesting |
| `via_ssh` | bool | This collector's session arrived over SSH |
| `container_id` | string \| null | |
| `container_name` | string \| null | devcontainer `name` when readable |
| `workspace_folder` | string \| null | devcontainer `workspaceFolder` |

`parent_id` cannot be derived from inside a container, so it is supplied by the
`AGENTVIEW_PARENT_ID` env var or `--parent`. When absent the UI lists the container
flat rather than guessing at a parent.

### AgentRecord

| Field | Type | Notes |
|---|---|---|
| `id` | string | `<context_id>:<harness>:<native_id>` — globally unique, stable |
| `harness` | string | `claude-code`, `opencode`, `pi`, … |
| `harness_label` | string | Display form, e.g. `Claude Code` |
| `harness_version` | string \| null | |
| `context_id` | string | FK to ContextRef |
| `name` | string | |
| `cwd` | string \| null | |
| `git_branch` | string \| null | Never the literal `HEAD` — see note |
| `status` | `busy`\|`idle`\|`blocked`\|`unknown` | `blocked` = waiting on a human |
| `detail` | string \| null | Last human-readable state line |
| `pid` | int \| null | |
| `started_at` | float \| null | **epoch seconds** |
| `updated_at` | float \| null | **epoch seconds** |
| `tokens` | int \| null | |
| `color` | string \| null | |
| `attach` | AttachSpec | |
| `source` | string | Adapter that produced it |
| `extra` | object | Harness-specific extras |

> **Timestamps are seconds.** Claude Code writes milliseconds on disk; adapters convert.
> **`git_branch` is never `"HEAD"`.** Claude Code records that placeholder when the cwd
> is not a repo; passing it through would imply a repo that isn't there.

### AttachSpec

Attach is *just an argv* — the trick that makes one code path serve local, container
and remote agents alike.

| Field | Type | Notes |
|---|---|---|
| `available` | bool | |
| `reason` | string \| null | Why not. **Always set when unavailable** — the UI shows it |
| `argv` | string[] \| null | Yields a PTY; read-only by default |
| `argv_readwrite` | string[] \| null | Used only when the user toggles input on |

| Context | argv |
|---|---|
| local | `tmux attach -r -t agentview:<id>` |
| container | `docker exec -it <cid> tmux attach -r -t agentview:<id>` |
| remote | `ssh <host> -t tmux attach -r -t agentview:<id>` |

You cannot attach to the PTY of a process started outside a multiplexer. Agents launched
normally still appear (presence is the point of the HUD) with `available: false` and an
honest `reason`.

### Snapshot

```json
{
  "context":      { "...ContextRef..." },
  "agents":       [ "...AgentRecord..." ],
  "collected_at": 1787692947.7,
  "warnings":     ["claude-code: unreadable session file broken.json"]
}
```

`warnings` carries non-fatal problems so a silently-empty HUD is distinguishable from a
genuinely idle one. A failing adapter produces a warning, never a crash.

## Endpoints (M2)

| Endpoint | Direction | Purpose |
|---|---|---|
| `POST /v1/hello` | collector → hub | Announce context, receive a collector id |
| `WS /v1/stream` | collector → hub | `agents.snapshot` every 5s; `agents.delta` and `timeline.event` immediately |
| `WS /v1/attach/{agent_id}` | browser ⇄ hub ⇄ collector | PTY bytes |
| `GET /v1/agents` | client → hub | REST snapshot for TUI / `curl \| jq` |

Attach frames:

```json
{"t": "i", "d": "ls\r"}                 // input  (browser → PTY)
{"t": "o", "d": "total 0\r\n"}          // output (PTY → browser)
{"t": "r", "cols": 120, "rows": 40}     // resize
```

## Liveness

The hub TTL-expires an agent it has not heard about for 3 snapshot intervals — a
collector that dies must not leave its agents on screen forever.

Collectors do their own liveness check before reporting. This is not optional: Claude
Code's session registry files outlive their process, so an unchecked collector reports
agents that exited days ago. Absence from a populated process table means gone; the
check also compares the process name to guard against PID reuse.

## Auth

A shared token at `~/.agentview/token`, generated on first run, sent as
`Authorization: Bearer <token>`. Loopback binding is the primary control; the token
stops another user on a shared dev box from reading your sessions.
