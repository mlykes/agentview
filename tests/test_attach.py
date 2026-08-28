"""Attach resolution and PTY sessions."""

from __future__ import annotations

import os
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
            "argv_readonly": None,
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


class ReadOnlyPolicyTest(unittest.TestCase):
    """Read-only is a hub-wide deployment choice (--read-only), not a per-session
    toggle. A terminal should behave like a terminal by default."""

    def setUp(self):
        self.registry = Registry()
        self.registry.ingest(snapshot("h1", [{
            "id": "h1:x:both", "name": "both", "status": "idle", "context_id": "h1",
            "attach": {"available": True,
                       "argv": ["tmux", "attach", "-t", "s"],
                       "argv_readonly": ["tmux", "attach", "-r", "-t", "s"],
                       "reason": None},
        }]))

    def test_default_attach_accepts_input(self):
        state = HubState(self.registry, token=None, local_context_id="h1")
        argv, error = state.resolve_attach("h1:x:both")
        self.assertIsNone(error)
        self.assertNotIn("-r", argv)

    def test_read_only_hub_uses_the_read_only_argv(self):
        state = HubState(self.registry, token=None, local_context_id="h1",
                         allow_input=False)
        argv, _ = state.resolve_attach("h1:x:both")
        self.assertIn("-r", argv)

    def test_read_only_hub_falls_back_when_no_variant_exists(self):
        registry = Registry()
        registry.ingest(snapshot("h1", [{
            "id": "h1:x:only", "name": "only", "status": "idle", "context_id": "h1",
            "attach": {"available": True, "argv": ["tmux", "attach", "-t", "s"],
                       "argv_readonly": None, "reason": None},
        }]))
        state = HubState(registry, token=None, local_context_id="h1", allow_input=False)
        argv, error = state.resolve_attach("h1:x:only")
        self.assertIsNone(error)
        self.assertEqual(argv, ["tmux", "attach", "-t", "s"])


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

    def _wait_for_exit(self, session, seconds=6.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if session.exited:
                return
            time.sleep(0.05)
        self.fail("child never reaped after {}s".format(seconds))

    def test_a_reaped_child_never_looks_alive(self):
        """A zombie answers `os.kill(pid, 0)` just like a running process, so
        liveness cannot be decided from the pid alone.

        The pid here belongs to a process that is genuinely still running, which is
        exactly what a zombie's pid looks like to `os.kill`. Setting the flag that
        _reap sets is what tells the two apart.
        """
        session = PtySession("dead", ["sh", "-c", "sleep 5"])
        session.start()
        os.kill(session.pid, 0)  # the check the old code relied on: still succeeds
        session.exited = True    # what _reap records once the child is gone
        self.assertFalse(session.alive)
        session.close()

    def test_manager_restarts_a_session_whose_child_exited(self):
        """The user-visible bug: a child that had exited still looked alive, so
        reconnecting replayed the dead terminal's scrollback instead of opening a
        new one. Background sessions hit this constantly -- unlike `tmux attach`,
        their client is meant to exit."""
        manager = PtyManager()
        first = manager.get_or_start("k", ["sh", "-c", "sleep 5"], 80, 24)
        first.exited = True
        second = manager.get_or_start("k", ["sh", "-c", "sleep 5"], 80, 24)
        self.assertIsNot(first, second)
        self.assertNotEqual(first.pid, second.pid)
        self.assertTrue(second.alive)
        manager.close_all()

    def test_child_is_reaped_rather_than_left_defunct(self):
        session = PtySession("zombie", ["sh", "-c", "exit 0"])
        session.start()
        self._wait_for_exit(session)
        with self.assertRaises(OSError):  # nothing left to wait on
            os.waitpid(session.pid, os.WNOHANG)

    def test_reader_survives_the_descriptor_being_nulled(self):
        """close() sets self.fd to None from another thread while the reader is in
        its loop. `os.read(None, ...)` raises TypeError, which `except OSError` does
        not catch -- so the reader thread died before reaching _reap() and the child
        stayed defunct for the life of the hub. Calling the loop with fd already None
        exercises that path directly; the timing window is too narrow to hit on
        demand."""
        session = PtySession("nofd", ["sh", "-c", "sleep 5"])
        session.fd = None
        session._read_loop()  # must return, not raise
        self.assertTrue(True)

    def test_manager_reaps_dead_sessions(self):
        manager = PtyManager(idle_timeout=0.0)
        manager.get_or_start("k", ["sh", "-c", "exit 0"], 80, 24)
        time.sleep(0.4)
        manager.reap_idle()
        self.assertIsNone(manager.get("k"))


if __name__ == "__main__":
    unittest.main()
