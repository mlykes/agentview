"""Renaming an agent over HTTP.

The label is agentview's own -- it does not rename the session in its harness --
so the thing worth pinning down is that every reader agrees on the displayed name
while the harness's name stays visible underneath.
"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from agentview.hub.overrides import Overrides
from agentview.hub.registry import Registry
from agentview.hub.server import Handler, HubState

TOKEN = "test-token-rename"


def get(url, token=None):
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


class RenameServerBase(unittest.TestCase):
    """A hub with a label store, on its own port."""

    allow_input = True

    @classmethod
    def setUpClass(cls):
        cls.tmp = TemporaryDirectory()
        cls.overrides = Overrides(Path(cls.tmp.name) / "names.json")
        registry = Registry(override_fn=cls.overrides.get)
        registry.ingest({
            "context": {"id": "h1", "label": "test-host", "kind": "host",
                        "platform": "linux", "arch": "x86_64", "parent_id": None},
            "agents": [{"id": "h1:x:1", "name": "session-abc", "status": "busy"}],
            "warnings": [],
            "collected_at": 0,
        })
        Handler.state = HubState(
            registry, TOKEN, allow_input=cls.allow_input, overrides=cls.overrides
        )
        Handler.verbose = False
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = "http://127.0.0.1:{}".format(cls.httpd.server_address[1])
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def post(self, path, payload, token=TOKEN):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if token:
            request.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()
            try:
                return exc.code, json.loads(body)
            except ValueError:
                return exc.code, {"raw": body}

    def agent(self):
        _, body = get(self.base + "/v1/agents", TOKEN)
        return json.loads(body)["agents"][0]


class RenameTest(RenameServerBase):
    def tearDown(self):
        self.overrides.set_name("h1:x:1", None)
        self.overrides.set_colour("h1:x:1", None)

    def test_rename_changes_the_name_every_reader_sees(self):
        status, body = self.post("/v1/rename", {"id": "h1:x:1", "name": "the api one"})
        self.assertEqual(status, 200)
        self.assertEqual(body["name"], "the api one")
        self.assertEqual(self.agent()["name"], "the api one")

    def test_the_harness_name_is_still_reported(self):
        self.post("/v1/rename", {"id": "h1:x:1", "name": "my label"})
        self.assertEqual(self.agent()["harness_name"], "session-abc")

    def test_clearing_restores_the_original_name(self):
        self.post("/v1/rename", {"id": "h1:x:1", "name": "temporary"})
        status, body = self.post("/v1/rename", {"id": "h1:x:1", "name": ""})
        self.assertEqual(status, 200)
        self.assertIsNone(body["name"])
        self.assertEqual(self.agent()["name"], "session-abc")
        self.assertNotIn("harness_name", self.agent())

    def test_control_characters_never_reach_the_row(self):
        self.post("/v1/rename", {"id": "h1:x:1", "name": "api\x1b[31m\x00"})
        self.assertEqual(self.agent()["name"], "api[31m")

    def test_renaming_an_unknown_agent_is_refused(self):
        status, body = self.post("/v1/rename", {"id": "h1:x:nope", "name": "x"})
        self.assertEqual(status, 404)
        self.assertIn("no such agent", body["error"])

    def test_missing_id_is_refused(self):
        status, _ = self.post("/v1/rename", {"name": "x"})
        self.assertEqual(status, 400)

    def test_rename_requires_a_token(self):
        status, _ = self.post("/v1/rename", {"id": "h1:x:1", "name": "x"}, token=None)
        self.assertEqual(status, 401)
        self.assertEqual(self.agent()["name"], "session-abc")

    def test_setting_a_colour_shows_up_on_the_row(self):
        """The reason this endpoint exists: Claude Code records no colour for an
        interactive session, nor for a background one with no state.json, so there
        is nothing to inherit and the row would stay uncoloured forever."""
        status, body = self.post("/v1/color", {"id": "h1:x:1", "color": "red"})
        self.assertEqual(status, 200)
        self.assertEqual(body["color"], "red")
        self.assertEqual(self.agent()["color"], "red")

    def test_an_unknown_colour_is_refused_rather_than_stored(self):
        self.post("/v1/color", {"id": "h1:x:1", "color": "teal"})
        status, body = self.post("/v1/color", {"id": "h1:x:1", "color": "chartreuse"})
        self.assertEqual(status, 200)
        self.assertIsNone(body["color"])
        self.assertIsNone(self.agent().get("color"))

    def test_clearing_a_colour_falls_back(self):
        self.post("/v1/color", {"id": "h1:x:1", "color": "blue"})
        self.post("/v1/color", {"id": "h1:x:1", "color": ""})
        self.assertIsNone(self.agent().get("color"))

    def test_colour_requires_a_token(self):
        status, _ = self.post("/v1/color", {"id": "h1:x:1", "color": "red"}, token=None)
        self.assertEqual(status, 401)

    def test_colouring_an_unknown_agent_is_refused(self):
        status, _ = self.post("/v1/color", {"id": "h1:x:nope", "color": "red"})
        self.assertEqual(status, 404)

    def test_the_palette_is_published_so_the_page_cannot_drift(self):
        _, body = get(self.base + "/v1/harnesses", TOKEN)
        self.assertIn("red", json.loads(body)["colours"])

    def test_the_page_is_told_editing_is_available(self):
        _, body = get(self.base + "/v1/harnesses", TOKEN)
        self.assertTrue(json.loads(body)["can_edit"])

    def test_capabilities_are_in_the_first_frame(self):
        """The page reads these before its first render. Learning them only from
        the async /v1/harnesses fetch left every row's rename control missing for
        a poll interval, which looks like the feature is absent rather than late."""
        _, html = get(self.base + "/?t=" + TOKEN, TOKEN)
        self.assertIn('id="caps"', html)
        block = html.split('id="caps">', 1)[1].split("</script>", 1)[0]
        self.assertTrue(json.loads(block)["can_edit"])


class ReadOnlyRenameTest(RenameServerBase):
    """A read-only hub does not mutate hub state, for the same reason it refuses
    to launch agents."""

    allow_input = False

    def test_colour_is_refused(self):
        status, body = self.post("/v1/color", {"id": "h1:x:1", "color": "red"})
        self.assertEqual(status, 403)
        self.assertIsNone(self.agent().get("color"))

    def test_rename_is_refused(self):
        status, body = self.post("/v1/rename", {"id": "h1:x:1", "name": "nope"})
        self.assertEqual(status, 403)
        self.assertIn("disabled", body["error"])
        self.assertEqual(self.agent()["name"], "session-abc")

    def test_the_page_is_told_editing_is_unavailable(self):
        _, body = get(self.base + "/v1/harnesses", TOKEN)
        self.assertFalse(json.loads(body)["can_edit"])

    def test_the_first_frame_says_so_too(self):
        _, html = get(self.base + "/?t=" + TOKEN, TOKEN)
        block = html.split('id="caps">', 1)[1].split("</script>", 1)[0]
        self.assertFalse(json.loads(block)["can_edit"])


if __name__ == "__main__":
    unittest.main()
