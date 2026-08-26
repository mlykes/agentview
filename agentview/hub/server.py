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
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from agentview.collector import context as context_mod
from agentview.collector.core import collect
from agentview.hub.ptys import PtyManager
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


class HubState:
    def __init__(
        self,
        registry: Registry,
        token: Optional[str],
        ptys: Optional[PtyManager] = None,
        local_context_id: Optional[str] = None,
        allow_input: bool = True,
    ) -> None:
        self.registry = registry
        self.token = token  # None => auth disabled
        self.ptys = ptys or PtyManager()
        #: Only agents in this context can be attached to; see resolve_attach().
        self.local_context_id = local_context_id
        self.allow_input = allow_input

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
        if self.local_context_id and context_id != self.local_context_id:
            # The argv is written for the collector's machine. Running it here would
            # attach to the wrong box, or to nothing. Remote attach needs the hub to
            # reach that context (ssh / docker exec) and lands in a later milestone.
            return None, "agent is on another machine - remote attach not supported yet"
        argv = attach.get("argv")
        if not argv or not isinstance(argv, list):
            return None, "no attach command reported"
        return argv, None

    def resolve_attach_rw(self, agent_id: str):
        """The read-write attach argv, for when a viewer explicitly enables input."""
        agent = self.registry.find_agent(agent_id)
        if agent is None:
            return None, "no such agent"
        argv, error = self.resolve_attach(agent_id)
        if error:
            return None, error
        rw = (agent.get("attach") or {}).get("argv_readwrite")
        if not rw or not isinstance(rw, list):
            return None, "this agent cannot accept input"
        return rw, None


class Handler(BaseHTTPRequestHandler):
    server_version = "agentview"
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
            agent_id = path[len("/v1/attach/"):-len("/stream")]
            return self._attach_stream(agent_id, query)

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
        if parsed.path == "/v1/snapshot":
            self.state.registry.ingest(payload)
            return self._json(200, {"ok": True})

        if parsed.path.startswith("/v1/attach/"):
            rest = parsed.path[len("/v1/attach/"):]
            agent_id, _, action = rest.rpartition("/")
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

            if action == "mode":
                want_input = bool(payload.get("input"))
                if want_input and not self.state.allow_input:
                    return self._json(403, {"error": "input disabled on this hub (--read-only)"})
                if want_input:
                    argv, error = self.state.resolve_attach_rw(agent_id)
                else:
                    argv, error = self.state.resolve_attach(agent_id)
                if error:
                    return self._json(409, {"error": error})
                # Read-only is a property of the tmux client, so changing it means
                # replacing the session. The browser reopens its stream after this.
                self.state.ptys.close(agent_id)
                self.state.ptys.get_or_start(agent_id, argv, 120, 32)
                return self._json(200, {"ok": True, "input": want_input})

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
        self.end_headers()

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

    def _sse(self, data: bytes) -> None:
        """Terminal output is arbitrary bytes; base64 keeps it safe through SSE's
        line-oriented framing."""
        self._sse_raw("data: {}\n\n".format(base64.b64encode(data).decode("ascii")))

    def _sse_raw(self, text: str) -> None:
        self.wfile.write(text.encode("utf-8"))
        self.wfile.flush()

    def _index(self, authorized: bool, query=None):
        try:
            html = (WEB_ROOT / "index.html").read_text()
        except OSError:
            return self._json(404, {"error": "not found"})
        if not authorized:
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

        blocks = [self._data_block("bootstrap", self.state.registry.view())]

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m agentview.hub")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: loopback)")
    parser.add_argument("--port", type=int, default=7788)
    parser.add_argument("--interval", type=float, default=3.0, help="local collector tick")
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
    registry = Registry(ttl=args.ttl, stuck_after=args.stuck_after)
    local_ctx = context_mod.detect(parent_id=args.parent, label=args.label)
    Handler.state = HubState(
        registry,
        token,
        local_context_id=None if args.no_local else local_ctx.id,
        allow_input=not args.read_only,
    )
    Handler.verbose = args.verbose

    if not args.no_local:
        thread = threading.Thread(
            target=local_collector_loop,
            args=(registry, args.interval, args.label, args.parent),
            daemon=True,
        )
        thread.start()

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
