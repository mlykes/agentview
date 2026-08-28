"""Pushing a colour set in the list back into the session itself.

`/color` is the only way to change a Claude Code session's own colour -- there is no
API and no `claude color` subcommand -- so this types into the terminal. That is
exactly the thing deliberately not done for renames, and it is only acceptable here
because it waits for the session to be open and idle: the user is looking at the
terminal when it happens, rather than having text appear in a session nobody is
watching.
"""

from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentview.hub.overrides import Overrides
from agentview.hub.registry import Registry
from agentview.hub.server import colour_keystrokes, colour_to_push


class KeystrokesTest(unittest.TestCase):
    def test_clears_the_prompt_before_typing(self):
        """Without the leading Ctrl-U the command is appended to whatever draft is
        in the prompt and submitted as one line."""
        self.assertEqual(colour_keystrokes("red"), b"\x15/color red\r")

    def test_ends_with_a_return_so_the_command_runs(self):
        self.assertTrue(colour_keystrokes("teal").endswith(b"\r"))


class ColourToPushTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Overrides(Path(self.tmp.name) / "names.json")

    def _agent(self, **kw):
        agent = {"id": "a:1", "harness": "claude-code", "status": "idle"}
        agent.update(kw)
        return agent

    def test_a_queued_colour_is_handed_over(self):
        self.store.set_colour("a:1", "red", push=True)
        self.assertEqual(colour_to_push(self._agent(), self.store, "a:1"), "red")

    def test_it_is_handed_over_only_once(self):
        """Cleared on the way out, not after a successful write: retrying on every
        reconnect would type into the prompt again and again."""
        self.store.set_colour("a:1", "red", push=True)
        colour_to_push(self._agent(), self.store, "a:1")
        self.assertIsNone(colour_to_push(self._agent(), self.store, "a:1"))

    def test_a_colour_set_without_a_push_is_not_typed(self):
        self.store.set_colour("a:1", "red")
        self.assertIsNone(colour_to_push(self._agent(), self.store, "a:1"))

    def test_a_busy_agent_is_left_alone(self):
        """The text would sit in the prompt and be submitted as a message when the
        turn ended."""
        self.store.set_colour("a:1", "red", push=True)
        self.assertIsNone(colour_to_push(self._agent(status="busy"), self.store, "a:1"))

    def test_a_busy_agent_keeps_the_colour_queued(self):
        self.store.set_colour("a:1", "red", push=True)
        colour_to_push(self._agent(status="busy"), self.store, "a:1")
        self.assertEqual(colour_to_push(self._agent(), self.store, "a:1"), "red")

    def test_other_harnesses_are_left_alone(self):
        """`/color` is a Claude Code command; anything else would receive it as a
        line of text."""
        self.store.set_colour("a:1", "red", push=True)
        self.assertIsNone(
            colour_to_push(self._agent(harness="opencode"), self.store, "a:1")
        )

    def test_a_missing_agent_is_not_an_error(self):
        self.assertIsNone(colour_to_push(None, self.store, "a:1"))

    def test_clearing_a_colour_cancels_the_queued_push(self):
        self.store.set_colour("a:1", "red", push=True)
        self.store.set_colour("a:1", "")
        self.assertIsNone(colour_to_push(self._agent(), self.store, "a:1"))

    def test_a_queued_push_survives_a_restart(self):
        """The hub can be restarted between setting a colour and opening the
        terminal; the colour is still owed."""
        self.store.set_colour("a:1", "red", push=True)
        reloaded = Overrides(self.store.path)
        self.assertEqual(colour_to_push(self._agent(), reloaded, "a:1"), "red")


class PendingIsVisibleTest(unittest.TestCase):
    """The row says a colour is waiting, rather than leaving the delay unexplained."""

    def _agents(self, store):
        registry = Registry(override_fn=store.get)
        registry.ingest({
            "context": {"id": "h1", "label": "host", "kind": "host"},
            "agents": [{"id": "a:1", "name": "one", "status": "idle"}],
            "warnings": [], "collected_at": 0,
        })
        return {a["id"]: a for a in registry.flat_agents()}

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Overrides(Path(self.tmp.name) / "names.json")

    def test_a_queued_colour_is_reported(self):
        self.store.set_colour("a:1", "red", push=True)
        self.assertTrue(self._agents(self.store)["a:1"]["color_pending"])

    def test_nothing_is_reported_once_it_has_been_sent(self):
        self.store.set_colour("a:1", "red", push=True)
        self.store.take_pending_colour("a:1")
        self.assertNotIn("color_pending", self._agents(self.store)["a:1"])

    def test_a_colour_set_without_a_push_is_not_reported(self):
        self.store.set_colour("a:1", "red")
        self.assertNotIn("color_pending", self._agents(self.store)["a:1"])



class HandlerPathTest(unittest.TestCase):
    """Exercise the method the server actually calls.

    The pure functions above were green while this path raised NameError on a
    constant that was never defined -- only a live attach found it. Testing the
    decision in isolation is not enough when the caller is where the wiring lives.
    """

    class FakeSession:
        def __init__(self):
            self.written = b""

        def write(self, data):
            self.written += data

    def setUp(self):
        from agentview.hub.server import Handler, HubState

        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Overrides(Path(self.tmp.name) / "names.json")

        registry = Registry(override_fn=self.store.get)
        registry.ingest({
            "context": {"id": "h1", "label": "host", "kind": "host"},
            "agents": [{"id": "a:1", "name": "one", "status": "idle",
                        "harness": "claude-code", "context_id": "h1"}],
            "warnings": [], "collected_at": 0,
        })
        self.handler = Handler.__new__(Handler)  # no socket needed for this method
        self.handler.state = HubState(registry, None, overrides=self.store)

    def _run_push(self):
        from agentview.hub import server as server_mod

        session = self.FakeSession()
        original = server_mod.COLOUR_PUSH_DELAY
        server_mod.COLOUR_PUSH_DELAY = 0.0
        try:
            self.handler._push_colour("a:1", session)
            time.sleep(0.3)  # the write is on a timer thread
        finally:
            server_mod.COLOUR_PUSH_DELAY = original
        return session

    def test_the_queued_colour_is_typed_into_the_session(self):
        self.store.set_colour("a:1", "blue", push=True)
        self.assertEqual(self._run_push().written, b"\x15/color blue\r")

    def test_nothing_is_typed_when_nothing_is_queued(self):
        self.store.set_colour("a:1", "blue")
        self.assertEqual(self._run_push().written, b"")

    def test_the_delay_is_configured(self):
        from agentview.hub.server import COLOUR_PUSH_DELAY

        self.assertIsInstance(COLOUR_PUSH_DELAY, float)

if __name__ == "__main__":
    unittest.main()
