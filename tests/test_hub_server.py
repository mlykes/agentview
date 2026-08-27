"""Hub HTTP surface.

These exist because of a real bug: auth was applied to /static/* as well as /v1/*,
so the browser -- which cannot attach an Authorization header to a <script src> or
<link rel=stylesheet> -- got 401s for the stylesheet and the script, and the page sat
forever on "Loading...". Status codes on the API looked perfectly healthy the whole
time. Anything the browser fetches without a token needs a test that also fetches
without a token.
"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from agentview.hub.registry import Registry
from agentview.hub.server import Handler, HubState

TOKEN = "test-token-abc"


def get(url, token=None):
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


class ServerTestBase(unittest.TestCase):
    token = TOKEN

    @classmethod
    def setUpClass(cls):
        registry = Registry()
        registry.ingest({
            "context": {"id": "h1", "label": "test-host", "kind": "host",
                        "platform": "linux", "arch": "x86_64", "parent_id": None},
            "agents": [{"id": "h1:x:1", "name": "demo", "status": "busy"}],
            "warnings": [],
            "collected_at": 0,
        })
        Handler.state = HubState(registry, cls.token)
        Handler.verbose = False
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = "http://127.0.0.1:{}".format(cls.httpd.server_address[1])
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()


class StaticAssetsTest(ServerTestBase):
    """The regression. A browser sends no token for subresources."""

    def test_stylesheet_is_served_without_a_token(self):
        status, body = get(self.base + "/static/style.css")
        self.assertEqual(status, 200)
        self.assertIn("--bg", body)

    def test_script_is_served_without_a_token(self):
        status, body = get(self.base + "/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn("/v1/view", body)

    def test_index_is_served_without_a_token(self):
        status, body = get(self.base + "/")
        self.assertEqual(status, 200)
        self.assertIn("agentview", body)

    def test_static_path_traversal_is_refused(self):
        status, _ = get(self.base + "/static/../server.py")
        self.assertNotEqual(status, 200)


class ApiAuthTest(ServerTestBase):
    def test_view_requires_a_token(self):
        status, _ = get(self.base + "/v1/view")
        self.assertEqual(status, 401)

    def test_view_accepts_bearer(self):
        status, body = get(self.base + "/v1/view", token=TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["totals"]["agents"], 1)

    def test_view_accepts_query_token(self):
        status, _ = get(self.base + "/v1/view?t=" + TOKEN)
        self.assertEqual(status, 200)

    def test_wrong_token_is_refused(self):
        status, _ = get(self.base + "/v1/view", token="nope")
        self.assertEqual(status, 401)

    def test_agents_requires_a_token(self):
        self.assertEqual(get(self.base + "/v1/agents")[0], 401)

    def test_health_is_open(self):
        status, body = get(self.base + "/v1/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])


class StreamFramingTest(ServerTestBase):
    """The regression that produced an empty terminal.

    BaseHTTPRequestHandler defaults to HTTP/1.0, which has no chunked framing. A
    bodiless streaming response therefore gives a browser no way to tell where one
    piece ends, so it buffers the whole thing and dispatches no events -- while curl,
    reading raw bytes, sees everything and looks perfectly healthy.
    """

    def test_server_speaks_http_1_1(self):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        conn.request("GET", "/v1/health")
        response = conn.getresponse()
        self.assertEqual(response.version, 11)
        conn.close()

    def test_sse_stream_is_chunked(self):
        import socket

        # An agent whose attach argv is a trivially real command.
        registry = self.__class__.httpd.RequestHandlerClass.state.registry
        registry.ingest({
            "context": {"id": "h1", "label": "t", "kind": "host", "platform": "linux",
                        "arch": "x86_64", "parent_id": None},
            "agents": [{
                "id": "h1:x:s", "name": "s", "status": "idle", "context_id": "h1",
                "attach": {"available": True, "argv": ["sh", "-c", "echo hi; sleep 5"],
                           "argv_readonly": None, "reason": None},
            }],
            "warnings": [], "collected_at": 0,
        })
        # The id is sent percent-encoded, exactly as a browser sends it. Agent ids
        # contain ":", and urlparse does not decode path segments -- so sending the
        # id raw (as curl does, and as every check here used to) passes while every
        # real client gets "no such agent" and an empty terminal.
        import urllib.parse

        encoded = urllib.parse.quote("h1:x:s", safe="")
        self.assertIn("%3A", encoded, "test must exercise the encoded form")
        sock = socket.create_connection(("127.0.0.1", self.httpd.server_address[1]), timeout=6)
        sock.sendall(
            ("GET /v1/attach/%s/stream HTTP/1.1\r\nHost: x\r\n" % encoded).encode()
            + b"Authorization: Bearer " + TOKEN.encode() + b"\r\n\r\n"
        )
        head = b""
        while b"\r\n\r\n" not in head and len(head) < 4096:
            chunk = sock.recv(512)
            if not chunk:
                break
            head += chunk
        sock.close()
        text = head.decode("utf-8", "replace")
        self.assertNotIn("no such agent", text)
        self.assertIn("HTTP/1.1 200", text)
        self.assertIn("chunked", text.lower())
        self.assertIn("text/event-stream", text.lower())


class EncodedAgentIdTest(ServerTestBase):
    """Agent ids contain ":" and browsers percent-encode it in a path segment."""

    def test_post_routes_decode_the_id(self):
        import urllib.parse

        registry = self.__class__.httpd.RequestHandlerClass.state.registry
        registry.ingest({
            "context": {"id": "h1", "label": "t", "kind": "host", "platform": "linux",
                        "arch": "x86_64", "parent_id": None},
            "agents": [{
                "id": "h1:x:enc", "name": "enc", "status": "idle", "context_id": "h1",
                "attach": {"available": True, "argv": ["sh", "-c", "sleep 5"],
                           "argv_readonly": ["sh", "-c", "sleep 5"], "reason": None},
            }],
            "warnings": [], "collected_at": 0,
        })
        encoded = urllib.parse.quote("h1:x:enc", safe="")

        # Start the terminal first via the (encoded) stream route, so a "no live
        # terminal" 404 cannot mask a decode miss on the POST route.
        import socket

        sock = socket.create_connection(("127.0.0.1", self.httpd.server_address[1]), timeout=6)
        sock.sendall(
            ("GET /v1/attach/%s/stream HTTP/1.1\r\nHost: x\r\n" % encoded).encode()
            + b"Authorization: Bearer " + TOKEN.encode() + b"\r\n\r\n"
        )
        sock.recv(256)
        try:
            request = urllib.request.Request(
                self.base + "/v1/attach/" + encoded + "/resize",
                data=b'{"cols": 90, "rows": 30}',
                headers={"Authorization": "Bearer " + TOKEN,
                         "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=8) as response:
                    status, body = response.status, response.read().decode()
            except urllib.error.HTTPError as exc:
                status, body = exc.code, exc.read().decode()
        finally:
            sock.close()
        self.assertNotIn("no such agent", body)
        self.assertEqual(status, 200, body)


class BootstrapTest(ServerTestBase):
    """The injected first-paint payload must follow the same rule as /v1/*."""

    def test_authorized_index_carries_data(self):
        _, body = get(self.base + "/?t=" + TOKEN)
        self.assertIn('id="bootstrap"', body)
        self.assertIn("test-host", body)

    def test_unauthenticated_index_leaks_nothing(self):
        _, body = get(self.base + "/")
        self.assertNotIn('id="bootstrap"', body)
        self.assertNotIn("test-host", body)


if __name__ == "__main__":
    unittest.main()
