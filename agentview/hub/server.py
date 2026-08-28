"""agentview hub -- stdlib HTTP server.

Deliberately dependency-free, like the collector. `git clone && python3 -m
agentview.hub` works on any box with Python 3.9+, no registry access required. That
matters more for this project than the ergonomics of a web framework: the whole point
is running on a machine where you cannot install anything.

Binds loopback by default. You reach it from a laptop with:

    ssh -L 7788:localhost:7788 <host>

Stdlib only.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, unquote, urlparse

from agentview.collector import context as context_mod
from agentview.collector.core import collect
from agentview import harnesses
from agentview import runner
from agentview.hub.ptys import PtyManager
from agentview.hub.overrides import COLOURS, Overrides
from agentview.hub import remotes as remotes_mod
from agentview.hub.registry import Registry

WEB_ROOT = Path(__file__).parent / "web"
TOKEN_PATH = Path.home() / ".agentview" / "token"


def load_or_create_token() -> str:
    """A shared secret so another user on the same box cannot read your sessions.

    Loopback binding is the primary control; this is the second layer.
    """
    try:
        if TOKEN_PATH.exists():
            existing = TOKEN_PATH.read_text().strip()
            if existing:
                return existing
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    try:
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(token)
        os.chmod(str(TOKEN_PATH), 0o600)
    except OSError:
        pass  # ephemeral token is still better than none
    return token


def available_harnesses():
    """Harnesses from the table that are actually installed here.

    Resolved with shutil.which so the UI only ever offers something that can start,
    and so the command handed to tmux comes from this side.
    """
    found = []
    for command, identity in sorted(harnesses.load().items()):
        path = shutil.which(command)
        if path:
            found.append({
                "harness": identity["harness"],
                "label": identity["label"],
                "command": path,
            })
    # One entry per harness, even when several commands map to it.
    seen, unique = set(), []
    for entry in found:
        if entry["harness"] not in seen:
            seen.add(entry["harness"])
            unique.append(entry)
    return unique


#: How long to let a terminal paint before typing into it. tmux redraws on
#: attach, but a freshly started `claude attach` client needs a moment before
#: it has a prompt to receive the command.
COLOUR_PUSH_DELAY = 1.5


def colour_to_push(agent, overrides, agent_id: str) -> Optional[str]:
    """The colour owed to a session's own UI, or None to leave it alone.

    A colour set in the list cannot reach the agent by itself -- there is no API for
    it, and `color` is not a `claude` subcommand -- so the only route is typing into
    the terminal. Doing that when the swatch is clicked would type into a session
    nobody is looking at; deferring it to the next attach means the user is watching
    when it happens.

    Refused while the agent is busy: the text would sit in the prompt and be
    submitted as a message when the turn ended. It stays queued for the next attach.
    Refused for other harnesses, which have no `/color`.
    """
    if agent is None or agent.get("harness") != "claude-code":
        return None
    if agent.get("status") == "busy":
        return None
    return overrides.take_pending_colour(agent_id)


def colour_keystrokes(colour: str) -> bytes:
    """Ctrl-U first: without it the command is appended to whatever draft is sitting
    in the prompt and run as one line."""
    return b"\x15/color " + colour.encode("ascii", "ignore") + b"\r"


class HubState:
    def __init__(
        self,
        registry: Registry,
        token: Optional[str],
        ptys: Optional[PtyManager] = None,
        local_context_id: Optional[str] = None,
        allow_input: bool = True,
        can_launch: bool = True,
        overrides: Optional[Overrides] = None,
    ) -> None:
        self.registry = registry
        #: agentview's own label and colour for each agent. A read-only hub does
        #: not write them.
        self.overrides = overrides
        self.token = token  # None => auth disabled
        self.ptys = ptys or PtyManager()
        #: Only agents in this context can be attached to; see resolve_attach().
        self.local_context_id = local_context_id
        self.allow_input = allow_input
        #: Whether the UI may start new agents. Off when the hub is read-only --
        #: a monitoring deployment should not be able to spawn processes.
        self.can_launch = can_launch and allow_input
        #: Editing a row mutates hub-side state, so a read-only hub refuses it for
        #: the same reason it refuses launching.
        self.can_edit = allow_input and overrides is not None
        #: host -> {"harnesses": {command: path}, "context_id": ..., "error": ...}.
        #: Filled in by the remote poll loop; read when listing launch targets and
        #: when resolving a command to start there.
        self.remotes: Dict[str, Dict[str, Any]] = {}

    def resolve_attach(self, agent_id: str):
        """(argv, error) for an agent, enforcing what this hub can actually reach.

        argv always comes from the registry -- what the collector reported -- never
        from the request.
        """
        agent = self.registry.find_agent(agent_id)
        if agent is None:
            return None, "no such agent"
        attach = agent.get("attach") or {}
        if not attach.get("available"):
            return None, attach.get("reason") or "attach unavailable for this agent"
        context_id = agent.get("context_id")
        # An agent on a remote host is reachable when its argv already goes through
        # ssh -- the collector's own argv was rewritten at ingest for exactly this.
        if agent.get("ssh_host"):
            key = "argv" if self.allow_input else "argv_readonly"
            argv = attach.get(key) or attach.get("argv")
            if not argv or not isinstance(argv, list):
                return None, "no attach command reported"
            return argv, None
        if self.local_context_id and context_id != self.local_context_id:
            # The argv is written for the collector's machine. Running it here would
            # attach to the wrong box, or to nothing. Remote attach needs the hub to
            # reach that context (ssh / docker exec) and lands in a later milestone.
            return None, "agent is on another machine - remote attach not supported yet"
        # A normal attach unless this hub was started with --read-only, which is a
        # deployment-wide choice. There is no per-session toggle: the terminal should
        # behave like the terminal you would otherwise run this agent in.
        key = "argv" if self.allow_input else "argv_readonly"
        argv = attach.get(key) or attach.get("argv")
        if not argv or not isinstance(argv, list):
            return None, "no attach command reported"
        return argv, None


class Handler(BaseHTTPRequestHandler):
    server_version = "agentview"
    #: HTTP/1.1 is required for the SSE stream. Under HTTP/1.0 there is no chunked
    #: framing, so a browser cannot tell where one piece of a bodiless response ends
    #: and buffers the whole thing instead of dispatching events -- the terminal then
    #: sits empty forever. curl reads raw bytes and never noticed the difference.
    #: Every non-streaming response below sends Content-Length, which 1.1 requires.
    protocol_version = "HTTP/1.1"
    state: HubState = None  # type: ignore[assignment]

    # -- plumbing ---------------------------------------------------------

    #: Set by --verbose. Access logging is off by default because a HUD polling
    #: every 2s would otherwise bury the startup banner.
    verbose = False

    def log_message(self, fmt, *args):  # noqa: A003
        if self.verbose:
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))
            sys.stderr.flush()

    def _authorized(self, query: Dict[str, Any]) -> bool:
        if self.state.token is None:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            if secrets.compare_digest(header[7:].strip(), self.state.token):
                return True
        supplied = (query.get("t") or [""])[0]
        return bool(supplied) and secrets.compare_digest(supplied, self.state.token)

    def _send(self, code: int, body: bytes, ctype: str, extra: Optional[Dict] = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # No external requests are possible from this page anyway; make it explicit.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:",
        )
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: Any):
        self._send(code, json.dumps(payload, default=str).encode("utf-8"), "application/json")

    # -- routes -----------------------------------------------------------

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path

        if path == "/v1/health":
            return self._json(200, {"ok": True, "version": "0.1.0"})

        # The shell page and its assets are served without a token, for two reasons:
        # they contain no session data, and a browser cannot attach an Authorization
        # header to a subresource request like <script src> or <link rel=stylesheet>.
        # Gating them here silently 401'd the stylesheet and the script, leaving an
        # unstyled page stuck on "Loading...". Auth belongs on /v1/*, which is where
        # the agent data actually lives.
        if path in ("/", "/index.html"):
            # Paint with real data on first frame instead of flashing "Loading...".
            # Only for an authorized request: an unauthenticated / must not carry
            # session data, which is the whole reason /v1/* is gated.
            return self._index(self._authorized(query), query)
        if path.startswith("/static/"):
            name = path[len("/static/"):]
            # One optional "vendor/" segment; everything else is rejected rather
            # than resolved, so no traversal can escape the web root.
            if name.startswith("vendor/"):
                leaf = name[len("vendor/"):]
                if "/" in leaf or ".." in leaf or not leaf:
                    return self._json(404, {"error": "not found"})
                return self._file(WEB_ROOT / "vendor" / leaf)
            if "/" in name or ".." in name:
                return self._json(404, {"error": "not found"})
            return self._file(WEB_ROOT / name)

        if not self._authorized(query):
            return self._json(401, {"error": "unauthorized"})

        if path.startswith("/v1/attach/") and path.endswith("/stream"):
            # unquote: agent ids contain ":" and a browser percent-encodes it in a
            # path segment. urlparse does not decode, so without this the lookup
            # fails with "no such agent" for every real client -- while curl, which
            # sends the id raw, works fine.
            agent_id = unquote(path[len("/v1/attach/"):-len("/stream")])
            return self._attach_stream(agent_id, query)

        if path == "/v1/harnesses":
            return self._json(200, {"harnesses": available_harnesses(),
                                    "targets": self._launch_targets(),
                                    "can_launch": self.state.can_launch,
                                    "can_edit": self.state.can_edit,
                                    "colours": list(COLOURS)})

        if path == "/v1/view":
            return self._json(200, self.state.registry.view())
        if path == "/v1/agents":
            return self._json(
                200,
                {
                    "agents": self.state.registry.flat_agents(),
                    "contexts": self.state.registry.contexts(),
                    "served_at": time.time(),
                },
            )
        return self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._authorized(query):
            return self._json(401, {"error": "unauthorized"})

        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, OSError) as exc:
            return self._json(400, {"error": "bad payload: {}".format(exc)})

        if parsed.path == "/v1/hello":
            return self._json(200, {"ok": True, "ttl": self.state.registry.ttl})
        if parsed.path == "/v1/launch":
            if not self.state.can_launch:
                return self._json(403, {"error": "launching is disabled on this hub"})
            requested = str(payload.get("harness") or "")
            host = payload.get("host") or None
            # The command is resolved from the server's own table -- the local one, or
            # what was probed on that remote. The browser names a harness and a host;
            # it never supplies a command, or this would be arbitrary execution behind
            # a loopback port, on someone else's machine as well as this one.
            if host:
                if host not in self.state.remotes:
                    return self._json(400, {"error": "unknown host"})
                target = next(
                    (t for t in self._launch_targets() if t["host"] == host), None
                )
                match = next(
                    (h for h in (target or {}).get("harnesses", [])
                     if h["harness"] == requested),
                    None,
                )
                if match is None:
                    return self._json(
                        400, {"error": "harness not installed on " + host}
                    )
                name = str(payload.get("name") or "").strip() or match["harness"]
                session = runner.unique_session_name(name)
                error = remotes_mod.launch(host, match["command"], session)
                if error:
                    return self._json(500, {"error": error})
                return self._json(200, {"ok": True, "session": session,
                                        "harness": match["harness"], "host": host})
            match = next(
                (h for h in available_harnesses() if h["harness"] == requested), None
            )
            if match is None:
                return self._json(400, {"error": "unknown or unavailable harness"})
            name = str(payload.get("name") or "").strip() or match["harness"]
            try:
                session = runner.launch_detached([match["command"]], name)
            except Exception as exc:  # noqa: BLE001 - report, do not traceback at a browser
                return self._json(500, {"error": str(exc)})
            return self._json(200, {"ok": True, "session": session,
                                    "harness": match["harness"]})

        if parsed.path in ("/v1/rename", "/v1/color"):
            if not self.state.can_edit:
                return self._json(403, {"error": "editing is disabled on this hub"})
            agent_id = str(payload.get("id") or "")
            if not agent_id:
                return self._json(400, {"error": "missing agent id"})
            if self.state.registry.find_agent(agent_id) is None:
                return self._json(404, {"error": "no such agent"})
            if parsed.path == "/v1/rename":
                label = self.state.overrides.set_name(agent_id, payload.get("name"))
                return self._json(200, {"ok": True, "name": label})
            # An unknown colour clears the override rather than being stored: the
            # value becomes part of a CSS custom property name, and one we have no
            # token for would render as no colour at all.
            colour = self.state.overrides.set_colour(
                agent_id, payload.get("color"), push=True
            )
            return self._json(200, {"ok": True, "color": colour})

        if parsed.path == "/v1/stop":
            if not self.state.can_edit:
                return self._json(403, {"error": "stopping is disabled on this hub"})
            agent_id = str(payload.get("id") or "")
            agent = self.state.registry.find_agent(agent_id) if agent_id else None
            if agent is None:
                return self._json(404, {"error": "no such agent"})
            if agent.get("context_id") != self.state.local_context_id:
                # Same rule as attach: the argv is written for the collector's
                # machine, so running it here would act on the wrong box.
                return self._json(409, {"error": "agent is on another machine"})
            argv, error = runner.stop_argv(agent)
            if error:
                return self._json(409, {"error": error})
            try:
                result = subprocess.run(argv, capture_output=True, text=True, timeout=20)
            except (OSError, subprocess.SubprocessError) as exc:
                return self._json(500, {"error": str(exc)})
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip().splitlines()
                return self._json(
                    500, {"error": detail[-1] if detail else "stop failed"}
                )
            # The terminal is pointing at something that no longer exists.
            self.state.ptys.close(agent_id)
            # The row lingers until the collector's next tick notices it is gone;
            # dropping the override now would leave a renamed agent nameless if the
            # stop turns out not to have taken.
            return self._json(200, {"ok": True})

        if parsed.path == "/v1/snapshot":
            self.state.registry.ingest(payload)
            return self._json(200, {"ok": True})

        if parsed.path.startswith("/v1/attach/"):
            rest = parsed.path[len("/v1/attach/"):]
            agent_id, _, action = rest.rpartition("/")
            agent_id = unquote(agent_id)  # see the note on the stream route
            session = self.state.ptys.get(agent_id)

            if action == "input":
                if session is None:
                    return self._json(404, {"error": "no live terminal"})
                # Terminal protocol replies (DA, OSC colour queries, cursor reports)
                # arrive on this path too, and tmux waits on them before painting.
                # They must go through even in read-only mode -- what makes read-only
                # safe is that the session itself runs `tmux attach -r`, which
                # discards keystrokes server-side. Client-side filtering would be
                # both weaker and broken.
                session.write(str(payload.get("d", "")).encode("utf-8"))
                return self._json(200, {"ok": True})

            if action == "resize":
                if session is None:
                    return self._json(404, {"error": "no live terminal"})
                session.resize(int(payload.get("cols", 80)), int(payload.get("rows", 24)))
                return self._json(200, {"ok": True})

            if action == "close":
                self.state.ptys.close(agent_id)
                return self._json(200, {"ok": True})

        return self._json(404, {"error": "not found"})

    def _attach_stream(self, agent_id: str, query):
        argv, error = self.state.resolve_attach(agent_id)
        if error:
            return self._json(409, {"error": error})

        try:
            cols = int((query.get("cols") or ["120"])[0])
            rows = int((query.get("rows") or ["32"])[0])
        except ValueError:
            cols, rows = 120, 32

        session = self.state.ptys.get_or_start(agent_id, argv, cols, rows)
        # An existing session was sized by whoever opened it first. Resize on every
        # connect so the terminal matches this window rather than a stale one.
        session.resize(cols, rows)
        self._push_colour(agent_id, session)

        queue = []
        event = threading.Event()

        def send(data: bytes):
            queue.append(data)
            event.set()

        # Replay scrollback so a viewer joining late sees the current screen rather
        # than an empty pane until the agent next prints something.
        backlog = session.scrollback()

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.close_connection = True

        sub_id = session.subscribe(send)
        try:
            if backlog:
                self._sse(backlog)
            last_ping = time.time()
            while True:
                event.wait(timeout=15.0)
                event.clear()
                while queue:
                    self._sse(queue.pop(0))
                if not session.alive and not queue:
                    self._sse_raw("event: end\ndata: {}\n\n")
                    break
                if time.time() - last_ping > 15.0:
                    # Keeps intermediaries and the browser from dropping an idle stream.
                    self._sse_raw(": ping\n\n")
                    last_ping = time.time()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # viewer navigated away
        finally:
            session.unsubscribe(sub_id)
            self._sse_end()

    def _launch_targets(self):
        """Where a new agent can be started, and what is installed there.

        A remote's list is probed on the far side rather than assumed to match this
        machine's -- they rarely do, and offering a harness that is not installed
        would fail only once the user had already chosen it.
        """
        table = harnesses.load()
        targets = [{
            "host": None,
            "label": "this machine",
            "harnesses": available_harnesses(),
        }]
        for host, info in sorted(self.state.remotes.items()):
            found = info.get("harnesses") or {}
            seen, entries = set(), []
            for command, path in sorted(found.items()):
                identity = table.get(command)
                if not identity or identity["harness"] in seen:
                    continue
                seen.add(identity["harness"])
                entries.append({
                    "harness": identity["harness"],
                    "label": identity["label"],
                    "command": path,
                })
            targets.append({
                "host": host,
                "label": host,
                "harnesses": entries,
                "error": info.get("error"),
            })
        return targets

    def _push_colour(self, agent_id: str, session) -> None:
        """Type `/color` into a session agentview was told to recolour."""
        if self.state.overrides is None:
            return
        agent = self.state.registry.find_agent(agent_id)
        colour = colour_to_push(agent, self.state.overrides, agent_id)
        if not colour:
            return
        keys = colour_keystrokes(colour)
        # After the terminal has painted -- tmux redraws on attach, and a fresh
        # `claude attach` client needs a moment before it has a prompt to type into.
        timer = threading.Timer(COLOUR_PUSH_DELAY, lambda: session.write(keys))
        timer.daemon = True
        timer.start()

    def _sse(self, data: bytes) -> None:
        """Terminal output is arbitrary bytes; base64 keeps it safe through SSE's
        line-oriented framing."""
        self._sse_raw("data: {}\n\n".format(base64.b64encode(data).decode("ascii")))

    def _sse_raw(self, text: str) -> None:
        """One chunked-encoding frame per SSE payload, flushed immediately."""
        payload = text.encode("utf-8")
        self.wfile.write(b"%x\r\n" % len(payload))
        self.wfile.write(payload)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _sse_end(self) -> None:
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _index(self, authorized: bool, query=None):
        try:
            html = (WEB_ROOT / "index.html").read_text()
        except OSError:
            return self._json(404, {"error": "not found"})
        if not authorized:
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

        blocks = [
            self._data_block("bootstrap", self.state.registry.view()),
            # Capabilities go in the first frame too. Learning them from the async
            # /v1/harnesses fetch left the per-row controls missing until the next
            # poll, which reads as "the feature isn't there" rather than "not yet".
            self._data_block("caps", {"can_launch": self.state.can_launch,
                                      "can_edit": self.state.can_edit,
                                    "colours": list(COLOURS)}),
        ]

        # For a ?open=<agent id> deep link, hand over the terminal's current screen
        # as well, so it paints immediately instead of sitting blank until the agent
        # next writes something. Same reasoning as the HUD bootstrap above.
        agent_id = (query or {}).get("open", [None])[0]
        if agent_id:
            argv, error = self.state.resolve_attach(agent_id)
            if not error:
                try:
                    session = self.state.ptys.get_or_start(agent_id, argv, 120, 32)
                    # A session started just now has nothing to replay yet. Give it a
                    # brief, bounded moment to paint -- tmux redraws immediately on
                    # attach -- so the deep link opens on content rather than black.
                    deadline = time.time() + 0.6
                    while not session.scrollback() and time.time() < deadline:
                        time.sleep(0.02)
                    blocks.append(self._data_block("term-bootstrap", {
                        "agent_id": agent_id,
                        "data": base64.b64encode(session.scrollback()).decode("ascii"),
                    }))
                except Exception:  # noqa: BLE001 - the SSE stream will retry anyway
                    pass

        html = html.replace("<!--BOOTSTRAP-->", "\n".join(blocks))
        return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

    @staticmethod
    def _data_block(element_id, payload):
        """A non-executable JSON block the page reads. Inert under CSP; escaping "<"
        stops a payload from breaking out of the script element."""
        text = json.dumps(payload, default=str).replace("<", "\\u003c")
        return '<script type="application/json" id="{}">{}</script>'.format(element_id, text)

    def _file(self, path: Path):
        try:
            body = path.read_bytes()
        except OSError:
            return self._json(404, {"error": "not found"})
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        return self._send(200, body, ctype)


def local_collector_loop(registry: Registry, interval: float, label, parent) -> None:
    """Inline collector for the machine the hub runs on.

    Most of the time you want to watch the box you started the hub on, and making
    that require a second process would be pointless ceremony. Remote machines and
    containers still push in over HTTP exactly the same way.
    """
    ctx = context_mod.detect(parent_id=parent, label=label)
    while True:
        try:
            registry.ingest(collect(ctx=ctx).to_dict())
        except Exception:  # noqa: BLE001 - a bad tick must not kill the hub
            pass
        time.sleep(interval)


def remote_collector_loop(state: "HubState", host: str, interval: float) -> None:
    """Poll one SSH host by running the collector there.

    The collector is copied over on the first tick and after any failure that looks
    like it went missing, so a remote that is rebooted or cleaned up heals itself
    without anyone logging in.
    """
    synced = False
    while True:
        try:
            if not synced:
                error = remotes_mod.sync_code(host)
                if error:
                    state.remotes.setdefault(host, {})["error"] = error
                    time.sleep(max(interval, 10.0))
                    continue
                synced = True

            snapshot, error = remotes_mod.collect_once(host)
            info = state.remotes.setdefault(host, {})
            if error:
                info["error"] = error
                # Most likely the copy is gone or half-written; send it again.
                synced = False
            else:
                info["error"] = None
                info["context_id"] = (snapshot.get("context") or {}).get("id")
                state.registry.ingest(remotes_mod.rewrite_for_ssh(snapshot, host))
                found, herr = remotes_mod.remote_harnesses(
                    host, sorted(harnesses.load().keys())
                )
                if not herr:
                    info["harnesses"] = found
        except Exception as exc:  # noqa: BLE001 - one bad host must not kill the hub
            state.remotes.setdefault(host, {})["error"] = str(exc)
        time.sleep(interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m agentview.hub")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: loopback)")
    parser.add_argument("--port", type=int, default=7788)
    parser.add_argument("--interval", type=float, default=3.0, help="local collector tick")
    parser.add_argument(
        "--remote", action="append", default=[], metavar="HOST",
        help="watch an ssh host (repeatable); remembered in ~/.agentview/remotes.json",
    )
    parser.add_argument(
        "--remote-interval", type=float, default=6.0,
        help="how often to poll each ssh host",
    )
    parser.add_argument("--no-local", action="store_true", help="do not collect from this machine")
    parser.add_argument("--no-auth", action="store_true", help="disable the token (loopback only)")
    parser.add_argument("--token", default=None, help="use this token instead of ~/.agentview/token")
    parser.add_argument("--label", default=None, help="display label for this machine")
    parser.add_argument("--parent", default=None)
    parser.add_argument("--verbose", action="store_true", help="log every HTTP request")
    parser.add_argument(
        "--read-only", action="store_true",
        help="never forward keystrokes to an agent's terminal",
    )
    parser.add_argument(
        "--no-launch", action="store_true",
        help="do not allow starting new agents from the UI",
    )
    parser.add_argument("--ttl", type=float, default=15.0, help="seconds before a context expires")
    parser.add_argument(
        "--stuck-after", type=float, default=900.0,
        help="flag a busy agent as stuck after this many seconds without an update",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.no_auth and args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "refusing --no-auth on a non-loopback bind ({}): that would expose every\n"
            "session on this machine to the network.".format(args.host),
            file=sys.stderr,
        )
        return 2

    token = None if args.no_auth else (args.token or load_or_create_token())
    overrides = Overrides()
    registry = Registry(
        ttl=args.ttl, stuck_after=args.stuck_after, override_fn=overrides.get
    )
    local_ctx = context_mod.detect(parent_id=args.parent, label=args.label)
    Handler.state = HubState(
        registry,
        token,
        local_context_id=None if args.no_local else local_ctx.id,
        allow_input=not args.read_only,
        can_launch=not args.no_launch,
        overrides=overrides,
    )
    Handler.verbose = args.verbose

    if not args.no_local:
        thread = threading.Thread(
            target=local_collector_loop,
            args=(registry, args.interval, args.label, args.parent),
            daemon=True,
        )
        thread.start()

    hosts = remotes_mod.load_remotes(args.remote)
    if args.remote:
        # Naming a host on the command line is also how you add one for good.
        remotes_mod.save_remotes(hosts)
    for host in hosts:
        Handler.state.remotes.setdefault(host, {})
        threading.Thread(
            target=remote_collector_loop,
            args=(Handler.state, host, args.remote_interval),
            daemon=True,
        ).start()

    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        print("cannot bind {}:{}: {}".format(args.host, args.port, exc), file=sys.stderr)
        return 1

    url = "http://{}:{}/".format(
        "127.0.0.1" if args.host in ("0.0.0.0", "127.0.0.1") else args.host, args.port
    )
    if token:
        url += "?t=" + token

    print("agentview hub listening on {}:{}".format(args.host, args.port))
    print("")
    print("  open:  {}".format(url))
    print("")
    if args.host == "127.0.0.1":
        print("  remote? ssh -L {p}:localhost:{p} {h}".format(p=args.port, h=socket.gethostname()))
    print("  ctrl-c to stop")
    sys.stdout.flush()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        return 0
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
