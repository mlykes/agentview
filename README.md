# agentview

A local HUD for watching coding agents run — across machines, containers and harnesses.

One screen answers: **what is running right now, where is it running, and is any of it stuck?**

```
▼ laptop · darwin/arm64                                    4 agents
    ● agent_viewer        Claude Code 2.1.241   ~/            busy  2m
    ○ spotlight ranking   Claude Code 2.1.238   ~/            idle 14m

▼ devbox · linux/amd64 · ssh                               3 agents
    ● refactor-api        Opencode              ~/src/api     busy  6m
    ▼ 📦 devcontainer: datacollector  (/workspace)
        ● collector-fix   Claude Code 2.1.241   /workspace    busy  8m   ⎇ main
```

## Design in one paragraph

The **overview above is the only novel thing agentview builds.** For per-agent detail it
does not parse or re-render transcripts — it attaches to the agent's own terminal session
and lets each harness draw its own UI. That keeps the parser surface near zero and means
agentview works with *any* terminal-based harness, not just ones it has been taught.

Collectors run per machine (and per container) and **dial outbound** to a hub. No
collector ever binds a port; the hub is the only listening socket, on loopback. On a
locked-down work machine you reach it through the SSH session you already have:

```bash
ssh -L 7788:localhost:7788 devbox    # then open http://localhost:7788
```

Nothing phones home. Nothing needs a package registry at install time.

## Quickstart

No install, no dependencies, no internet:

```bash
git clone <this repo> && cd agentview
python3 -m agentview.hub
```

The hub prints a URL with an auth token — open it. It collects from the machine it runs
on automatically. To report a *different* machine or container into the same HUD:

```bash
python3 -m agentview.collector --hub http://<hub-host>:7788 --token <token> \
    --parent <hub-machine-context-id>      # so it nests under its host
```

## Status

**M1–M3 work**: collector, hub + overview UI, and terminal attach.

Click any attachable agent and its own terminal opens in the browser — agentview does
not parse or re-render transcripts, it attaches to the session the harness is already
drawing. Read-only by default; input is a deliberate per-session toggle, because typing
into a running agent by accident is a real way to derail it.

Supported today:
- **Claude Code** — rich adapter reading its session registry
- **Any harness running in tmux** — recognised by process name from a table you can
  extend in `~/.agentview/harnesses.json`, no code change
- **Anything else** — write a heartbeat file to `~/.agentview/agents/*.json`

## Attaching to an agent

You cannot attach to the PTY of a process started outside a multiplexer — that is an OS
fact, not something agentview can engineer around. So launch agents through the shim:

```bash
agentview run -- claude
agentview run --name api -- opencode
```

Agents started normally still appear in the HUD; their terminal view is disabled with an
explicit reason rather than silently doing nothing.

`agentview run` clears the environment variables that identify a *parent* agent session
before launching. Without that, an agent started from inside another agent inherits its
parent's session id — and, for Claude Code, its messaging socket and token — and
registers under the parent's name. Since launching from inside an agent is the obvious
way to try this, that was the common case rather than an edge case.

## Zero dependencies, by hard requirement

**Both the collector and the hub** are stdlib-only — enforced in CI against a bare
interpreter with empty site-packages:

```bash
python3 -S -E -m agentview.collector --once     # no site-packages at all
python3 -S -E -m unittest discover -s tests -t .
```

That is what makes agentview deployable to a restricted box by copying a directory.
There is no build step, no package registry, and nothing for a security team to review
beyond a loopback socket.

Linters and pytest do have dependencies; those run in the devcontainer, never on your
host.

## License

MIT
