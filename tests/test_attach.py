"""Attach resolution and PTY sessions."""

from __future__ import annotations

import time
import unittest

from agentview.hub.ptys import PtyManager, PtySession
from agentview.hub.registry import Registry
from agentview.hub.server import HubState


def agent(agent_id, available=True, argv=None, reason=None, context_id="h1"):
    return {
        "id": agent_id, "name": agent_id, "status": "idle", "context_id": context_id,
        "attach": {
            "available": available,
            "argv": argv or (["echo", "hi"] if available else None),
            "argv_readwrite": None,
            "reason": reason,
        },
    }


def snapshot(context_id, agents):
    return {
        "context": {"id": context_id, "label": context_id, "kind": "host",
                    "platform": "linux", "arch": "x86_64", "parent_id": None},
        "agents": agents, "warnings": [], "collected_at": time.time(),
    }


class ResolveAttachTest(unittest.TestCase):
    def setUp(self):
        self.registry = Registry()
        self.registry.ingest(snapshot("h1", [
            agent("h1:x:ok"),
            agent("h1:x:no", available=False, reason="started outside tmux"),
        ]))
        self.registry.ingest(snapshot("h2", [agent("h2:x:remote", context_id="h2")]))
        self.state = HubState(self.registry, token=None, local_context_id="h1")

    def test_local_attachable_agent_resolves(self):
        argv, error = self.state.resolve_attach("h1:x:ok")
        self.assertIsNone(error)
        self.assertEqual(argv, ["echo", "hi"])

    def test_unknown_agent(self):
        _, error = self.state.resolve_attach("nope")
        self.assertEqual(error, "no such agent")

    def test_unattachable_agent_returns_its_reason(self):
        argv, error = self.state.resolve_attach("h1:x:no")
        self.assertIsNone(argv)
        self.assertIn("outside tmux", error)

    def test_agent_on_another_machine_is_refused(self):
        """The argv is written for the collector's box; running it here is wrong."""
        argv, error = self.state.resolve_attach("h2:x:remote")
        self.assertIsNone(argv)
        self.assertIn("another machine", error)

    def test_argv_never_comes_from_the_caller(self):
        """resolve_attach takes only an id -- there is no path for a client to
        supply a command."""
        import inspect

        params = list(inspect.signature(self.state.resolve_attach).parameters)
        self.assertEqual(params, ["agent_id"])


class ReadWriteResolutionTest(unittest.TestCase):
    """Read-only is a property of the tmux client, so switching modes means
    replacing the session with the read-write argv."""

    def setUp(self):
        registry = Registry()
        ro_rw = {
            "id": "h1:x:both", "name": "both", "status": "idle", "context_id": "h1",
            "attach": {"available": True, "argv": ["tmux", "attach", "-r", "-t", "s"],
                       "argv_readwrite": ["tmux", "attach", "-t", "s"], "reason": None},
        }
        ro_only = {
            "id": "h1:x:ro", "name": "ro", "status": "idle", "context_id": "h1",
            "attach": {"available": True, "argv": ["tmux", "attach", "-r", "-t", "s"],
                       "argv_readwrite": None, "reason": None},
        }
        registry.ingest(snapshot("h1", [ro_rw, ro_only]))
        self.state = HubState(registry, token=None, local_context_id="h1")

    def test_readwrite_argv_drops_the_read_only_flag(self):
        argv, error = self.state.resolve_attach_rw("h1:x:both")
        self.assertIsNone(error)
        self.assertNotIn("-r", argv)

    def test_default_argv_keeps_it(self):
        argv, _ = self.state.resolve_attach("h1:x:both")
        self.assertIn("-r", argv)

    def test_agent_without_a_readwrite_argv_is_refused(self):
        argv, error = self.state.resolve_attach_rw("h1:x:ro")
        self.assertIsNone(argv)
        self.assertIn("cannot accept input", error)

    def test_readwrite_still_refuses_a_remote_agent(self):
        registry = Registry()
        registry.ingest(snapshot("h2", [{
            "id": "h2:x:r", "name": "r", "status": "idle", "context_id": "h2",
            "attach": {"available": True, "argv": ["tmux"], "argv_readwrite": ["tmux"],
                       "reason": None},
        }]))
        state = HubState(registry, token=None, local_context_id="h1")
        _, error = state.resolve_attach_rw("h2:x:r")
        self.assertIn("another machine", error)


class PtySessionTest(unittest.TestCase):
    def test_captures_output_and_exits(self):
        session = PtySession("t", ["sh", "-c", "echo hello-from-pty"])
        session.start()
        for _ in range(50):
            if b"hello-from-pty" in session.scrollback():
                break
            time.sleep(0.05)
        self.assertIn(b"hello-from-pty", session.scrollback())
        session.close()

    def test_scrollback_is_replayed_to_late_subscribers(self):
        session = PtySession("t2", ["sh", "-c", "echo early-output; sleep 5"])
        session.start()
        for _ in range(50):
            if b"early-output" in session.scrollback():
                break
            time.sleep(0.05)
        # A viewer connecting now must still see what already happened.
        self.assertIn(b"early-output", session.scrollback())
        session.close()

    def test_manager_reuses_a_live_session(self):
        manager = PtyManager()
        first = manager.get_or_start("k", ["sh", "-c", "sleep 5"], 80, 24)
        second = manager.get_or_start("k", ["sh", "-c", "sleep 5"], 80, 24)
        self.assertIs(first, second)
        manager.close_all()

    def test_manager_reaps_dead_sessions(self):
        manager = PtyManager(idle_timeout=0.0)
        manager.get_or_start("k", ["sh", "-c", "exit 0"], 80, 24)
        time.sleep(0.4)
        manager.reap_idle()
        self.assertIsNone(manager.get("k"))


if __name__ == "__main__":
    unittest.main()
