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
    def __init__(self, registry: Registry, token: Optional[str]) -> None:
        self.registry = registry
        self.token = token  # None => auth disabled


class Handler(BaseHTTPRequestHandler):
    server_version = "agentview"
    state: HubState = None  # type: ignore[assignment]

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args):  # noqa: A003 - quiet by default
        pass

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

        if not self._authorized(query):
            if path == "/" or path.startswith("/static"):
                return self._send(
                    401,
                    b"<h1>agentview</h1><p>Add <code>?t=&lt;token&gt;</code> to the URL. "
                    b"The hub prints the full link on startup.</p>",
                    "text/html; charset=utf-8",
                )
            return self._json(401, {"error": "unauthorized"})

        if path in ("/", "/index.html"):
            return self._file(WEB_ROOT / "index.html")
        if path.startswith("/static/"):
            name = path[len("/static/"):]
            if "/" in name or ".." in name:
                return self._json(404, {"error": "not found"})
            return self._file(WEB_ROOT / name)

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
        return self._json(404, {"error": "not found"})

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
    Handler.state = HubState(registry, token)

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
