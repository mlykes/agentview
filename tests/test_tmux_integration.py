"""End-to-end: a tmux-launched agent becomes attachable.

Skipped where tmux is unavailable (CI containers, Windows). These use real tmux
sessions with a unique name and always tear them down.
"""

from __future__ import annotations

import os
import subprocess
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentview.collector import context as context_mod
from agentview.collector import tmux
from agentview.collector.adapters.tmux_adapter import TmuxAdapter
from agentview.collector.core import collect

SESSION = "agentview_selftest_{}".format(os.getpid())


@unittest.skipUnless(tmux.available(), "tmux is not installed")
class TmuxAttachTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = TemporaryDirectory()
        # A stand-in binary named after a real harness, so the generic path is what
        # is under test rather than the Claude Code adapter.
        fake = Path(cls.tmp.name) / "opencode"
        fake.write_text("#!/bin/sh\nwhile true; do echo working; sleep 2; done\n")
        fake.chmod(0o755)
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", SESSION, str(fake)],
            capture_output=True, timeout=20,
        )
        # tmux reports the pane before the child has necessarily exec'd.
        for _ in range(40):
            if tmux.list_panes():
                break
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["tmux", "kill-session", "-t", SESSION], capture_output=True)
        cls.tmp.cleanup()

    def _our_session(self, records):
        return [r for r in records if r.extra.get("tmux_session") == SESSION]

    def test_agent_in_tmux_is_discovered(self):
        records, _ = TmuxAdapter().discover(context_mod.detect())
        found = self._our_session(records)
        self.assertTrue(found, "tmux-launched harness was not discovered")
        self.assertEqual(found[0].harness, "opencode")

    def test_discovered_agent_is_attachable(self):
        records, _ = TmuxAdapter().discover(context_mod.detect())
        agent = self._our_session(records)[0]
        self.assertTrue(agent.attach.available)
        self.assertIn(SESSION, agent.attach.argv)
        # A normal interactive attach by default -- the terminal should behave like
        # the terminal you would otherwise run this agent in.
        self.assertNotIn("-r", agent.attach.argv)
        # The read-only variant exists for a hub started with --read-only.
        self.assertIn("-r", agent.attach.argv_readonly or [])

    def test_collect_surfaces_it_end_to_end(self):
        snapshot = collect()
        agent = self._our_session(snapshot.agents)
        self.assertTrue(agent, "agent missing from a full collect()")
        self.assertTrue(agent[0].attach.available)

    def test_session_for_pid_walks_ancestry(self):
        panes = tmux.list_panes()
        ours = [p for p in panes if p.session == SESSION]
        self.assertTrue(ours)
        self.assertEqual(tmux.session_for_pid(ours[0].pid, panes), SESSION)

    def test_unrelated_pid_is_not_in_a_session(self):
        # pid 1 rather than our own: running the suite from inside tmux is normal,
        # and would make a self-referential assertion fail for the wrong reason.
        self.assertIsNone(tmux.session_for_pid(1, tmux.list_panes()))


# Needs a real tmux server, like the classes above. Without the guard this class's
# setUpClass shells out unconditionally and errors on a host with no tmux -- which is
# exactly what the dependency-free CI container is.
@unittest.skipUnless(tmux.available(), "tmux is not installed")
class AgentviewOwnClientsAreHiddenTest(unittest.TestCase):
    """agentview parks `claude attach` clients in tmux sessions of its own.

    Those panes must never be discovered as agents. The background session they show
    is already in the list via the Claude Code adapter, and its real process lives
    outside tmux entirely -- so pane discovery cannot dedupe it by ancestry, and every
    background agent you opened would appear a second time.
    """

    BG_SESSION = tmux.AGENTVIEW_BG_PREFIX + "unittest"

    @classmethod
    def setUpClass(cls):
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", cls.BG_SESSION, "sh", "-c", "sleep 30"],
            capture_output=True, timeout=20, check=False,
        )
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(
            ["tmux", "kill-session", "-t", cls.BG_SESSION], capture_output=True, check=False
        )

    def test_the_session_really_exists(self):
        """Otherwise the assertion below would pass for the wrong reason."""
        result = subprocess.run(
            ["tmux", "has-session", "-t", self.BG_SESSION], capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 0)

    def test_pane_discovery_skips_it(self):
        sessions = [pane.session for pane in tmux.list_panes()]
        self.assertNotIn(self.BG_SESSION, sessions)


if __name__ == "__main__":
    unittest.main()
