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

## Status

Early. **M1 (collector + Claude Code adapter) is in progress.** See `PROTOCOL.md` for the
wire format and the repo plan for the milestone list.

## The collector has zero dependencies

By hard requirement — enforced in CI against a bare interpreter with empty site-packages:

```bash
python3 -m agentview.collector --once
```

That is what makes it deployable to a restricted box by copying a directory. The hub,
tests and tooling have real dependencies and run in the devcontainer.

## License

MIT
