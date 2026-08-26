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
