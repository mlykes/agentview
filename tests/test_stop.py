"""Stopping an agent from the viewer.

This is the one destructive thing the hub can do, so the rules it enforces matter
more than the happy path: the command is resolved from the registry rather than the
request, only agents on this machine can be stopped, and a read-only hub refuses.
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
from unittest import mock

from agentview import runner
from agentview.hub.overrides import Overrides
from agentview.hub.registry import Registry
from agentview.hub.server import Handler, HubState

TOKEN = "test-token-stop"


class StopArgvTest(unittest.TestCase):
    """How to stop an agent, derived from what the collector reported."""

    def test_background_session_uses_the_cli_subcommand(self):
        """`claude stop` shuts the session down rather than killing a process out
        from under its transcript."""
        agent = {"harness": "claude-code", "extra": {"job_id": "abc123"}}
        with mock.patch("agentview.runner.shutil.which", return_value="/bin/claude"):
            argv, error = runner.stop_argv(agent)
        self.assertIsNone(error)
        self.assertEqual(argv, ["/bin/claude", "stop", "abc123"])

    def test_tmux_agent_has_its_session_killed(self):
        agent = {"harness": "opencode", "extra": {"tmux_session": "agentview_api"}}
        with mock.patch("agentview.collector.tmux.available", return_value=True):
            argv, error = runner.stop_argv(agent)
        self.assertIsNone(error)
        self.assertEqual(argv, ["tmux", "kill-session", "-t", "agentview_api"])

    def test_background_wins_when_an_agent_has_both(self):
        """A background session parked in a tmux client has both signals. Stopping
        the session is right; killing the tmux client would only close the view."""
        agent = {
            "harness": "claude-code",
            "extra": {"job_id": "abc123", "tmux_session": "agentview_bg_abc123"},
        }
        with mock.patch("agentview.runner.shutil.which", return_value="/bin/claude"):
            argv, _ = runner.stop_argv(agent)
        self.assertEqual(argv[1], "stop")

    def test_bare_terminal_agent_cannot_be_stopped(self):
        argv, error = runner.stop_argv({"harness": "claude-code", "extra": {}})
        self.assertIsNone(argv)
        self.assertIn("cannot stop", error)

    def test_missing_claude_binary_is_reported(self):
        agent = {"harness": "claude-code", "extra": {"job_id": "abc123"}}
        with mock.patch("agentview.runner.shutil.which", return_value=None):
            argv, error = runner.stop_argv(agent)
        self.assertIsNone(argv)
        self.assertIn("PATH", error)


class StopServerBase(unittest.TestCase):
    allow_input = True

    @classmethod
    def setUpClass(cls):
        cls.tmp = TemporaryDirectory()
        overrides = Overrides(Path(cls.tmp.name) / "names.json")
        registry = Registry(override_fn=overrides.get)
        registry.ingest({
            "context": {"id": "h1", "label": "host", "kind": "host"},
            "agents": [
                {"id": "h1:x:1", "name": "killable", "status": "idle",
                 "harness": "claude-code", "context_id": "h1",
                 "extra": {"job_id": "abc123"}},
                {"id": "h1:x:2", "name": "bare", "status": "idle",
                 "harness": "claude-code", "context_id": "h1", "extra": {}},
            ],
            "warnings": [], "collected_at": 0,
        })
        registry.ingest({
            "context": {"id": "h2", "label": "elsewhere", "kind": "host"},
            "agents": [
                {"id": "h2:x:1", "name": "remote", "status": "idle",
                 "harness": "claude-code", "context_id": "h2",
                 "extra": {"job_id": "def456"}},
            ],
            "warnings": [], "collected_at": 0,
        })
        Handler.state = HubState(
            registry, TOKEN, local_context_id="h1",
            allow_input=cls.allow_input, overrides=overrides,
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

    def post(self, payload, token=TOKEN):
        request = urllib.request.Request(
            self.base + "/v1/stop",
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


class StopTest(StopServerBase):
    def test_stopping_runs_the_resolved_command(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("agentview.hub.server.subprocess.run", return_value=completed) as run:
            with mock.patch("agentview.runner.shutil.which", return_value="/bin/claude"):
                status, body = self.post({"id": "h1:x:1"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(run.call_args[0][0], ["/bin/claude", "stop", "abc123"])

    def test_the_command_never_comes_from_the_request(self):
        """A loopback port that ran a client-supplied command would be arbitrary
        execution. The payload's argv must be ignored entirely."""
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("agentview.hub.server.subprocess.run", return_value=completed) as run:
            with mock.patch("agentview.runner.shutil.which", return_value="/bin/claude"):
                self.post({"id": "h1:x:1", "argv": ["rm", "-rf", "/"], "command": "evil"})
        self.assertEqual(run.call_args[0][0], ["/bin/claude", "stop", "abc123"])

    def test_a_failing_command_reports_its_error(self):
        completed = mock.Mock(returncode=1, stdout="", stderr="no such session\n")
        with mock.patch("agentview.hub.server.subprocess.run", return_value=completed):
            with mock.patch("agentview.runner.shutil.which", return_value="/bin/claude"):
                status, body = self.post({"id": "h1:x:1"})
        self.assertEqual(status, 500)
        self.assertEqual(body["error"], "no such session")

    def test_an_unstoppable_agent_says_why(self):
        status, body = self.post({"id": "h1:x:2"})
        self.assertEqual(status, 409)
        self.assertIn("cannot stop", body["error"])

    def test_an_agent_on_another_machine_is_refused(self):
        """The argv is written for the collector's machine; running it here would
        act on the wrong box."""
        status, body = self.post({"id": "h2:x:1"})
        self.assertEqual(status, 409)
        self.assertIn("another machine", body["error"])

    def test_unknown_agent_is_refused(self):
        status, _ = self.post({"id": "h1:x:nope"})
        self.assertEqual(status, 404)

    def test_missing_id_is_refused(self):
        status, _ = self.post({})
        self.assertEqual(status, 404)

    def test_stopping_requires_a_token(self):
        with mock.patch("agentview.hub.server.subprocess.run") as run:
            status, _ = self.post({"id": "h1:x:1"}, token=None)
        self.assertEqual(status, 401)
        run.assert_not_called()


class ReadOnlyStopTest(StopServerBase):
    allow_input = False

    def test_a_read_only_hub_refuses_to_stop_anything(self):
        with mock.patch("agentview.hub.server.subprocess.run") as run:
            status, body = self.post({"id": "h1:x:1"})
        self.assertEqual(status, 403)
        self.assertIn("disabled", body["error"])
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
