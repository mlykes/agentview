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
| `argv` | string[] \| null | Normal interactive attach — the default |
| `argv_readonly` | string[] \| null | Used only when the hub runs with `--read-only` |

| Context | argv |
|---|---|
| local | `tmux attach -t <session>` |
| container | `docker exec -it <cid> tmux attach -t <session>` |
| remote | `ssh <host> -t tmux attach -t <session>` |

**The hub reads argv from the registry, never from the request.** A client-supplied
command would be arbitrary execution behind a loopback port. `resolve_attach()` takes
an agent id and nothing else.

**The terminal is a normal terminal.** It accepts input, because a viewer who opens
one generally wants to use it, and anything less stops it behaving like the terminal
you would otherwise have run the agent in. A hub started with `--read-only` attaches
with `tmux attach -r` instead — a deployment-wide choice, enforced server-side by
tmux rather than by the browser declining to send keystrokes.

There was briefly a per-session "allow input" toggle. It was removed: read-only is a
property of the tmux client, so flipping it meant tearing down and restarting the
client, which reset the terminal to a fixed size and visibly resized the window.
Complexity that made the terminal behave *less* like a terminal.

**The session is resized on every connect**, so it matches the window that opened it
rather than whichever window opened it first.

Only agents in the hub's own context can be attached to today. The argv is written for
the collector's machine, so running a remote one locally would attach to the wrong box.

Attach is *just an argv*, so a harness that exposes its own way in gets one without any
new machinery. Claude Code background agents have no controlling terminal, but
`claude attach <job id>` opens a client onto the running session over its unix socket;
the adapter emits that argv wrapped in `tmux new-session -A`, which is create-or-attach,
so a reconnect joins the existing client instead of stacking another one.

Those tmux sessions are named `agentview_bg_*` and are filtered out of pane discovery.
They hold a *client*, not an agent -- the agent's own process lives outside tmux with no
tty, so ancestry cannot dedupe it, and without the filter every background agent you
opened would appear twice.

What remains genuinely unattachable is a process started outside a multiplexer: there is
no PTY to add. Those agents still appear (presence is the point of the HUD) with
`available: false` and an honest `reason`.

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
| `POST /v1/hello` | collector → hub | Announce context |
| `POST /v1/snapshot` | collector → hub | Push a Snapshot; replaces that context's view |
| `GET /v1/view` | client → hub | Nested view: contexts, children, totals |
| `GET /v1/agents` | client → hub | Flat snapshot for a TUI or `curl \| jq` |
| `GET /v1/attach/{id}/stream` | browser → hub | **SSE** stream of terminal output |
| `POST /v1/attach/{id}/input` | browser → hub | Bytes toward the PTY |
| `POST /v1/attach/{id}/resize` | browser → hub | `{"cols": n, "rows": n}` |
| `POST /v1/attach/{id}/close` | browser → hub | Tear the session down |
| `GET /v1/health` | anyone | Liveness; the only unauthenticated `/v1` route |

**SSE, not WebSocket.** It keeps the hub stdlib-only (no hand-rolled RFC6455 framing),
survives SSH tunnels and proxies unchanged, and a terminal view is overwhelmingly
read-heavy. Output frames are base64, because terminal output is arbitrary bytes and
SSE framing is line-oriented:

```
data: WzIxNF0gdGhpbmtpbmcuLi4NCg==

: ping
```

An `event: end` frame means the PTY exited; the client should stop reconnecting.

**The page and its static assets are unauthenticated.** A browser cannot attach an
`Authorization` header to `<script src>` or `<link rel=stylesheet>`, so gating them
only breaks the page. They carry no session data. Auth applies to `/v1/*`, where the
data is — via `Authorization: Bearer` or `?t=`, since `EventSource` cannot set headers
either.

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
