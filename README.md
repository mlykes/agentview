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

## Stable and preview hubs

A clean main checkout and a feature checkout can run the full topology together:

```bash
cd /Users/mlykes/Developer/agentview-main
git pull --ff-only
./bin/agentview hub --profile stable       # port 7788

cd /Users/mlykes/Developer/agentview
./bin/agentview hub --profile preview      # port 7789
```

When starting a hub from inside Codex or another agent, detach it so the harness does
not treat the long-running server as one of the session's background shell commands:

```bash
./bin/agentview hub --profile preview --daemon
```

`--daemon` uses POSIX process detachment implemented by AgentView itself—not macOS
`launchctl` or Linux `systemd`—so the same command works on macOS, Linux, and inside
an already-running Linux container. PID and log files live under
`~/.agentview/run/<instance-id>.{pid,log}`. When AgentView is the container's primary
process, leave it in the foreground and let the container runtime supervise it.

`stable` is the backward-compatible default. `--port` overrides either profile's
port, and the profile, checkout, branch, short commit, dirty marker and effective
port appear in the authenticated page and startup banner. Always use the launcher
inside the checkout being previewed: a global symlink targeting the main checkout
will run main's code regardless of the selected port.

Both hubs intentionally read and control the same agents and share
`~/.agentview/names.json`, `~/.agentview/remotes.json`, and the auth token. Thus an
attach, stop, rename, colour change, or launch in either writable UI affects shared
state. Full-topology previews also approximately double polling traffic.

`remotes.json` accepts SSH host aliases (the same values accepted by `--remote`),
for example:

```json
{"hosts": ["devbox", "buildbox"]}
```

Running Docker containers are discovered automatically on the local machine and on
each configured SSH host unless `--no-containers` is supplied.

Each hub deploys its collector into an isolated cache:
`~/.agentview/code/<instance-id>` over SSH and
`/tmp/.agentview-code-<instance-id>` in a container. Old `~/.agentview/code` contents
and `/tmp/.agentview-code-*` instances are not deleted automatically; stale or legacy
caches are safe to remove after their hubs have stopped.

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
- **Codex** — interactive threads from the same local store as `codex resume --all`;
  opening one runs `codex resume <thread-id>`
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
| Codex resumable session | `codex resume <thread id>` — opens the saved thread in a client |
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

## Remote machines

Name an SSH host and it appears in the HUD:

```bash
python3 -m agentview hub --remote pronto_server
```

That is the whole setup. The host is remembered in `~/.agentview/remotes.json`, so
later runs just work.

**Nothing is installed on the remote.** The hub sends its own collector over the SSH
connection you already have — about 20KB, gzipped in memory — unpacks it under
`~/.agentview/code`, and runs it there. No package registry, no internet, no port
opened on the far side, nothing for an IT department to approve. It is re-sent
automatically if it ever goes missing, so a rebuilt box heals itself.

The host is whatever you would type after `ssh`, so an alias from `~/.ssh/config`
works and brings its `User`, `IdentityFile` and `ProxyJump` with it.

Three things work across the connection:

| | |
|---|---|
| **seeing agents** | the collector runs there each tick; its snapshot is merged into the HUD |
| **starting agents** | **+ new agent** lists each machine and what is installed *on that machine* |
| **opening a terminal** | attach becomes `ssh <host> -t tmux attach -t <session>` |

The context is labelled with the name you typed rather than the machine's own
hostname — a work box is often called something like `SPU5-1-2-7-61358`, which is not
how you think of it.

**Everything runs through a login shell**, which is not a detail. SSH hands out a
non-login PATH — on a stock Debian box `/usr/local/bin:/usr/bin:/bin:/usr/games` —
while Claude Code installs to `~/.local/bin`. Probing without `bash -l` reports a
perfectly good install as missing, and the launch menu comes back empty.

What does *not* work remotely is attaching to a background agent. `claude attach`
reaches a session over a unix socket on its own machine, and that socket is not
exposed across SSH, so those rows say so rather than offering a button that would run
the command against a job id on the wrong box. Remote agents started through
agentview run under tmux and attach normally.

## Containers

Agents inside a container are invisible from the host's process table, so the hub
looks inside them too — on this machine and on every SSH host — and nests each
container under the machine running it:

```
Masons-MacBook-Pro                      9 agents
  devcontainer: agentview-demo          1
pronto_server                           1 agent
  devcontainer: Opetopic                1
```

The collector already describes a container from the inside, reading the
devcontainer's own name and workspace folder, so a card says
`devcontainer: Opetopic` rather than a hex id.

Only containers that **report at least one agent** get a card. A machine can easily
run a dozen — databases, proxies, a pgadmin — and a card each would bury the thing
this is for. Containers with no interpreter are skipped outright; the collector is
copied into the rest under `/tmp`, which is writable even in images that run as a
non-root user.

Enumerating containers is comparatively expensive, so it happens rarely, while
containers that actually hold an agent are re-collected often enough that their rows
do not flicker against the registry's TTL. `--no-containers` turns the whole thing
off.

Attach follows the same "just an argv" rule, gaining one wrapper per layer:

| where the agent is | attach |
|---|---|
| this machine | `tmux attach -t <session>` |
| a container here | `docker exec -it <id> tmux attach -t <session>` |
| a container on an SSH host | `ssh <host> -t docker exec -it <id> tmux attach …` |

A container image without tmux still gets its agents *listed*, with an honest note
that there is no terminal to reach them through. Background agents inside a container
cannot be attached either: `claude attach` speaks to a unix socket in there, and
running it outside would target a job id that does not exist on this side.

A container that bind-mounts the host's `~/.claude` — common for devcontainers — does
not double-report the host's agents. That needs no special handling: the collector
checks each session's pid is a live Claude Code process, and a host pid is not live
inside the container's PID namespace, so those records are dropped by the same
liveness check that removes stale ones.

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


**Colours come from the session where possible.** Claude Code gives each session a
colour; agentview carries it through to the agent's name and the row's left edge, so a
session you recognise in `claude agents` looks the same here. The status dot keeps
meaning status — busy, idle, blocked, stuck — because that is the question the HUD
exists to answer.

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

**You can also set a colour here**, from the swatch beside the name.

That means a colour has two places it can be changed — the swatch, or `/color` inside
the session — so **the most recent change wins**. Both are timestamped: the transcript
records when `/color` ran, and the swatch records when you clicked it. If one source
simply always overruled the other, changing the colour in the losing place would appear
to do nothing.

A colour set from the list is also **pushed into the session itself**, so the two do
not just agree in the HUD — `claude agents` and the session's own UI show it too.

There is no API for that and no `claude color` subcommand, so the only route is typing
`/color` into the terminal. agentview does not do that the moment you click: it waits
until the next time you open that agent's terminal, so you are looking at the session
when text appears in it. The swatch shows a ring while a colour is waiting to be sent.

Two things it will not do. It skips a **busy** agent — the text would sit in the prompt
and be submitted as a message when the turn ended — and keeps the colour queued for
next time. And it sends Ctrl-U first, so the command cannot be appended to a half-typed
draft; that does discard such a draft, which is the cost of the feature.

The displaced value is kept as `harness_color` and shown on hover, so you can see what
the session itself says. Clearing the swatch colour falls back to the session's.

A colour with no time attached counts as older than one with a time. That applies to
overrides written before this existed, and to a background session's colour taken from
`state.json`, which records no time of its own.

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
