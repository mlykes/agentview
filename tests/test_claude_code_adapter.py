"""Claude Code adapter tests.

Written against stdlib ``unittest`` on purpose: the collector must run on a bare
interpreter, so its tests must too. pytest discovers these as-is in the devcontainer.

Liveness is injected rather than observed so the suite is deterministic -- whether pid
1001 happens to exist on the machine running the tests must not change the result.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from agentview.collector.adapters.claude_code import ClaudeCodeAdapter, _resolve_status
from agentview.model import (
    STATUS_BLOCKED,
    STATUS_BUSY,
    STATUS_IDLE,
    STATUS_UNKNOWN,
    ContextRef,
)

FIXTURES = Path(__file__).parent / "fixtures" / "claude"

# 1002 is deliberately absent -> a ghost. 1004 is alive but is not Claude Code.
FAKE_TABLE = {
    1001: "claude bg-spare",
    1003: "claude",
    1004: "python3",
}


class ClaudeCodeAdapterTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch(
            "agentview.collector.procs.pid_alive", side_effect=lambda pid: pid in FAKE_TABLE
        )
        self.addCleanup(patcher.stop)
        patcher.start()

        self.ctx = ContextRef(id="ctx-test", label="test", platform="linux", arch="x86_64")
        self.adapter = ClaudeCodeAdapter(
            config_dir=FIXTURES, process_table_fn=lambda: dict(FAKE_TABLE)
        )
        self.records, self.warnings = self.adapter.discover(self.ctx)
        self.by_name = {r.name: r for r in self.records}

    def test_adapter_is_available(self):
        self.assertTrue(self.adapter.available())

    def test_finds_live_agent(self):
        agent = self.by_name["refactor-api"]
        self.assertEqual(agent.status, STATUS_BUSY)
        self.assertEqual(agent.cwd, "/workspace")
        self.assertEqual(agent.pid, 1001)
        self.assertEqual(agent.harness, "claude-code")
        self.assertEqual(agent.harness_version, "2.1.241")
        self.assertEqual(agent.tokens, 4096)
        self.assertEqual(agent.color, "blue")
        self.assertEqual(agent.detail, "refactoring the request layer")

    def test_excludes_ghost_whose_process_is_gone(self):
        """The most important behaviour: registry files outlive their process."""
        self.assertNotIn("long-gone", self.by_name)

    def test_excludes_recycled_pid(self):
        """pid 1004 is alive but belongs to python3, not Claude Code."""
        self.assertNotIn("pid-recycled", self.by_name)

    def test_blocked_job_reports_blocked(self):
        agent = self.by_name["waiting-on-me"]
        self.assertEqual(agent.status, STATUS_BLOCKED)
        self.assertEqual(agent.detail, "awaiting review of the migration plan")

    def test_real_branch_is_reported(self):
        self.assertEqual(self.by_name["refactor-api"].git_branch, "feature/request-layer")

    def test_head_placeholder_branch_is_suppressed(self):
        """Claude Code writes "HEAD" when the cwd is not a repo; not a branch name."""
        self.assertIsNone(self.by_name["waiting-on-me"].git_branch)

    def test_malformed_session_file_warns_but_does_not_crash(self):
        self.assertTrue(self.records, "a broken file must not suppress the good ones")
        self.assertTrue(any("broken.json" in w for w in self.warnings))

    def test_record_ids_are_namespaced_and_stable(self):
        again, _ = self.adapter.discover(self.ctx)
        self.assertEqual([r.id for r in self.records], [r.id for r in again])
        for record in self.records:
            self.assertTrue(record.id.startswith("ctx-test:claude-code:"))

    def test_attach_is_honestly_unavailable(self):
        """M1 has no attach yet -- the UI must say why, not silently disable."""
        attach = self.records[0].attach
        self.assertFalse(attach.available)
        self.assertTrue(attach.reason)


class ConfigDirTest(unittest.TestCase):
    def test_missing_config_dir_is_not_an_error(self):
        ctx = ContextRef(id="c", label="test")
        adapter = ClaudeCodeAdapter(config_dir=Path("/nonexistent/agentview/test"))
        self.assertFalse(adapter.available())
        self.assertEqual(adapter.discover(ctx), ([], []))


class StatusResolutionTest(unittest.TestCase):
    CASES = [
        # A session can be live-busy while its last recorded job state is stale
        # "blocked"; the live signal must win.
        ("busy", "blocked", STATUS_BUSY),
        ("idle", "blocked", STATUS_BLOCKED),
        ("idle", "active", STATUS_IDLE),
        ("busy", None, STATUS_BUSY),
        (None, "active", STATUS_BUSY),
        (None, None, STATUS_UNKNOWN),
    ]

    def test_status_resolution(self):
        for session_status, job_state, expected in self.CASES:
            with self.subTest(session=session_status, job=job_state):
                self.assertEqual(_resolve_status(session_status, job_state), expected)


if __name__ == "__main__":
    unittest.main()
