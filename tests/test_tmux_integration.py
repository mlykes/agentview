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
        # Read-only by default; the read-write variant is a separate argv.
        self.assertIn("-r", agent.attach.argv)
        self.assertNotIn("-r", agent.attach.argv_readwrite or [])

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


if __name__ == "__main__":
    unittest.main()
