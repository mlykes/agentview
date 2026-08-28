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
git clone https://github.com/mlykes/agentview && cd agentview
python3 -m agentview hub
```

No install step. If you want `agentview` on your PATH, symlink the launcher — it
resolves its own location, so it works from any directory:

```bash
ln -s "$PWD/bin/agentview" ~/.local/bin/agentview
```

Everything below can be written either way: `python3 -m agentview <cmd>` from a
checkout, or `agentview <cmd>` once symlinked.

The hub prints a URL with an auth token — open it. It collects from the machine it runs
on automatically. To report a *different* machine or container into the same HUD:

```bash
python3 -m agentview.collector --hub http://<hub-host>:7788 --token <token> \
    --parent <hub-machine-context-id>      # so it nests under its host
```

(The collector keeps its own `-m agentview.collector` entry point: it is the piece you
copy to a locked-down machine on its own, and it must run with nothing else present.)

## Status

**M1–M3 work**: collector, hub + overview UI, and terminal attach.

Click any attachable agent and its own terminal opens in the browser — agentview does
not parse or re-render transcripts, it attaches to the session the harness is already
drawing. It is a normal terminal: it takes input, follows your window size, and behaves
like the terminal you would otherwise have run the agent in. Start the hub with
`--read-only` if you want a monitoring-only deployment; that is enforced by tmux,
server-side.

Supported today:
- **Claude Code** — rich adapter reading its session registry
- **Any harness running in tmux** — recognised by process name from a table you can
  extend in `~/.agentview/harnesses.json`, no code change
- **Anything else** — write a heartbeat file to `~/.agentview/agents/*.json`

## Attaching to an agent

Opening a terminal splits the view rather than covering it: the agent list stays on the
left, so you can see everything running while you drive one of them, and clicking
another agent switches the terminal.

Click **+ new agent** in the HUD. It lists the agent CLIs actually installed on that
machine, starts the one you pick under tmux, and opens its terminal as soon as it
registers. No command to type, no terminal to go find.

The same thing from a shell, if you prefer:

```bash
python3 -m agentview run -- claude
python3 -m agentview run --name api -- opencode
```

Agents reach their terminal by one of two routes, picked per session:

| session | route |
|---|---|
| running inside tmux | `tmux attach -t <session>` — the terminal it already draws |
| Claude Code background agent | `claude attach <job id>` — a fresh client onto the running session |
| started in a bare terminal | not attachable |

Background agents have no controlling terminal at all (`ps` reports tty `??`), so there
is no PTY to hook onto — but they are not unreachable. Claude Code exposes each session
on a unix socket, and `claude attach` opens a client onto it; detaching leaves the agent
running. agentview parks that client in a tmux session of its own so closing the browser
tab does not kill it.

What is genuinely out of reach is an agent started in an ordinary terminal that is not a
multiplexer: you cannot add a PTY to a process already running outside one. That is an
OS constraint, not something agentview can work around, so those rows say so plainly
rather than offering a button that would do nothing.

The browser never supplies a command. It names a harness, and the hub resolves the
binary from its own table — otherwise a loopback port would be arbitrary code
execution. `--no-launch` turns the feature off, and `--read-only` implies it.

`agentview run` clears the environment variables that identify a *parent* agent session
before launching. Without that, an agent started from inside another agent inherits its
parent's session id — and, for Claude Code, its messaging socket and token — and
registers under the parent's name. Since launching from inside an agent is the obvious
way to try this, that was the common case rather than an edge case.

## Reading the list

**Agents are grouped by working directory.** "What is running in this repo" is the
question you actually ask; the host matters less once you have more than a couple of
machines. Each context card breaks into one group per directory, so the row itself no
longer repeats the path — it carries the git branch instead.

**Stopping an agent.** The ✕ beside a name ends it, after a confirmation. Like attach,
stopping is *just an argv* resolved from the registry rather than from the request:

| session | how it stops |
|---|---|
| background | `claude stop <job id>` — shuts the session down, transcript kept |
| running in tmux | the tmux session it lives in is killed |
| started in a bare terminal | no control shown; there is no lever to pull |

Only agents in the hub's own context can be stopped — the argv is written for the
collector's machine — and `--read-only` refuses it entirely.


**Colours come from the harness, not from agentview.** Claude Code assigns each
background session a colour; agentview carries it through to the agent's name and the
row's left edge, so a session you recognise in `claude agents` looks the same here. The
status dot keeps meaning status — busy, idle, blocked, stuck — because that is the
question the HUD exists to answer.

A colour is read from two places, in this order:

| source | covers |
|---|---|
| `jobs/<id>/state.json` | background sessions, where the harness records it as a field |
| the transcript's `/color` command | everything else, including interactive sessions |

The second one matters more than it sounds. `/color` is not stored as a field
anywhere — not in the session file, the job state, or the daemon roster — so an
interactive session's colour looks unreadable at first. It is recorded in the
transcript as a local command, and that is the only durable trace of it. Without
reading it, a session shows plain in the HUD while its own UI shows the colour you set,
which reads as agentview being broken.

Transcripts reach megabytes, so the first read is bounded and every read after it is
incremental — the file only ever grows. A `/color` set before that window and never
repeated is missed, which shows as no colour rather than a wrong one.

Sessions with no colour anywhere stay neutral rather than getting an invented one.

**You can also set a colour here**, from the swatch beside the name, for the cases the
harness has nothing to say about. An explicit choice in agentview wins over the
harness's, and the displaced value is kept as `harness_color` and shown on hover.
Clearing it falls back to the harness — so if you set a colour here and later change it
with `/color`, clear the override or the row will keep showing yours.

**Renaming is agentview's own label.** Click the pencil beside a name, type, press
Enter. Clearing the field restores the original.

The label does not rename the session in its harness, and that is deliberate rather
than a shortcut. There is no supported way to do it from outside — Claude Code has no
`rename` subcommand — and driving its `/rename` through the terminal would type into a
live prompt, appending to whatever you had half-written or queueing a message to a busy
agent. A viewer should not do that behind your back. So the harness's own name is kept
alongside the label as `harness_name` and shown on hover, and labels live in
`~/.agentview/names.json`.

Labels are applied where every reader sees them, so `/v1/agents`, the grouped view and
the TUI client cannot disagree about what a row is called. `--read-only` refuses
renames for the same reason it refuses to launch agents.

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
