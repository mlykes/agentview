"""Claude Code adapter tests.

Written against stdlib ``unittest`` on purpose: the collector must run on a bare
interpreter, so its tests must too. pytest discovers these as-is in the devcontainer.

Liveness is injected rather than observed so the suite is deterministic -- whether pid
1001 happens to exist on the machine running the tests must not change the result.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from agentview.collector.adapters.claude_code import (
    NO_TERMINAL,
    ClaudeCodeAdapter,
    _resolve_status,
    scan_colour,
)
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
    1005: "claude",
}

#: Whether `claude` and `tmux` exist is a property of the box the tests run on, so
#: both are injected. Otherwise this suite would pass or fail depending on PATH.
FAKE_CLAUDE = "/usr/local/bin/claude"


class ClaudeCodeAdapterTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch(
            "agentview.collector.procs.pid_alive", side_effect=lambda pid: pid in FAKE_TABLE
        )
        self.addCleanup(patcher.stop)
        patcher.start()

        self.ctx = ContextRef(id="ctx-test", label="test", platform="linux", arch="x86_64")
        self.adapter = ClaudeCodeAdapter(
            config_dir=FIXTURES,
            process_table_fn=lambda: dict(FAKE_TABLE),
            which_fn=lambda name: FAKE_CLAUDE if name == "claude" else None,
            tmux_available_fn=lambda: True,
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

    def test_background_session_is_attachable_via_claude_attach(self):
        """A bg session has no controlling terminal, but it is not unreachable.

        `claude attach <job id>` opens a client onto the running session over its
        unix socket. Reporting these as unattachable -- which we did until we checked
        -- hid every background agent behind a dead button.
        """
        attach = self.by_name["refactor-api"].attach
        self.assertTrue(attach.available)
        self.assertIsNone(attach.reason)
        self.assertEqual(attach.argv[-3:], [FAKE_CLAUDE, "attach", "job-live"])

    def test_background_attach_is_parked_in_tmux_for_reconnects(self):
        """`new-session -A` is create-or-attach: reopening joins the same client
        rather than stacking a second one onto the session."""
        attach = self.by_name["refactor-api"].attach
        self.assertEqual(
            attach.argv[:5],
            ["tmux", "new-session", "-A", "-s", "agentview_bg_job-live"],
        )

    def test_background_readonly_variant_creates_then_attaches_readonly(self):
        readonly = self.by_name["refactor-api"].attach.argv_readonly
        self.assertEqual(readonly[:2], ["sh", "-c"])
        self.assertIn("has-session -t agentview_bg_job-live", readonly[2])
        self.assertIn("tmux attach -r -t agentview_bg_job-live", readonly[2])

    def test_interactive_session_defers_to_the_tmux_adapter(self):
        """An interactive session owns a real terminal. If that terminal is a tmux
        pane, TmuxAdapter supplies the attach during the merge -- so this adapter must
        leave it unavailable rather than routing it through `claude attach`, which is
        for background sessions only."""
        attach = self.by_name["hand-started"].attach
        self.assertFalse(attach.available)
        self.assertEqual(attach.reason, NO_TERMINAL)


class BackgroundAttachTest(unittest.TestCase):
    """The attach route is decided from what is on the box, so vary that directly."""

    def _adapter(self, which, tmux_available):
        return ClaudeCodeAdapter(
            config_dir=FIXTURES,
            process_table_fn=lambda: dict(FAKE_TABLE),
            which_fn=which,
            tmux_available_fn=lambda: tmux_available,
        )

    def _refactor_api(self, adapter):
        ctx = ContextRef(id="ctx-test", label="test")
        with mock.patch(
            "agentview.collector.procs.pid_alive", side_effect=lambda pid: pid in FAKE_TABLE
        ):
            records, _ = adapter.discover(ctx)
        return {r.name: r for r in records}["refactor-api"]

    def test_without_tmux_the_client_runs_directly_in_the_pty(self):
        """Still attachable -- it just restarts on each reconnect instead of
        surviving one. Degrading to that beats disabling the button."""
        adapter = self._adapter(lambda n: FAKE_CLAUDE if n == "claude" else None, False)
        attach = self._refactor_api(adapter).attach
        self.assertTrue(attach.available)
        self.assertEqual(attach.argv, [FAKE_CLAUDE, "attach", "job-live"])
        self.assertIsNone(attach.argv_readonly)

    def test_missing_claude_binary_says_so(self):
        adapter = self._adapter(lambda n: None, True)
        attach = self._refactor_api(adapter).attach
        self.assertFalse(attach.available)
        self.assertIn("PATH", attach.reason)


class SessionColourTest(unittest.TestCase):
    """Where a session's colour comes from.

    `/color` is not stored as a field anywhere -- not the session file, the job
    state, or the daemon roster -- so an interactive session's colour looked
    unreadable. It is recorded in the transcript as a local command, which is what
    this reads. Missing it meant a session showed plain in the HUD while its own UI
    showed the colour the user had set.
    """

    def setUp(self):
        patcher = mock.patch(
            "agentview.collector.procs.pid_alive", side_effect=lambda pid: pid in FAKE_TABLE
        )
        self.addCleanup(patcher.stop)
        patcher.start()
        self.adapter = ClaudeCodeAdapter(
            config_dir=FIXTURES,
            process_table_fn=lambda: dict(FAKE_TABLE),
            which_fn=lambda name: FAKE_CLAUDE if name == "claude" else None,
            tmux_available_fn=lambda: True,
        )
        records, _ = self.adapter.discover(
            ContextRef(id="ctx-test", label="test", platform="linux")
        )
        self.by_name = {r.name: r for r in records}

    def test_interactive_colour_comes_from_the_transcript(self):
        self.assertEqual(self.by_name["hand-started"].color, "teal")

    def test_the_latest_colour_wins(self):
        """The fixture sets red and later teal; the session is teal."""
        self.assertNotEqual(self.by_name["hand-started"].color, "red")

    def test_job_state_wins_when_it_records_one(self):
        """A background session's state.json is authoritative -- the transcript is
        only consulted when there is nothing better."""
        self.assertEqual(self.by_name["refactor-api"].color, "blue")

    def test_a_session_with_no_colour_stays_uncoloured(self):
        self.assertIsNone(self.by_name["waiting-on-me"].git_branch)  # sanity: fixture read
        adapter = ClaudeCodeAdapter(
            config_dir=FIXTURES, process_table_fn=lambda: dict(FAKE_TABLE)
        )
        self.assertIsNone(adapter._session_colour("no-such-session"))

    def test_rescanning_only_reads_what_was_appended(self):
        """Transcripts are append-only and reach megabytes, so each tick reads the
        new tail rather than the whole file."""
        sid = "aaaaaaaa-0000-0000-0000-000000000005"
        first = self.adapter._session_colour(sid)
        offset, _ = self.adapter._colour_scan[sid]
        size = self.adapter._transcript(sid).stat().st_size
        self.assertEqual(first, "teal")
        self.assertEqual(offset, size)  # nothing left to re-read


class ScanColourTest(unittest.TestCase):
    def _line(self, colour):
        # The real records carry an escaped newline between the tags. Written as a
        # literal one it would be invalid JSON, which is worth getting right here:
        # the scanner has to cope with the real shape, not a convenient one.
        return json.dumps({
            "type": "system",
            "subtype": "local_command",
            "content": (
                "<command-name>/color</command-name>\n"
                "            <command-message>color</command-message>\n"
                "            <command-args>" + colour + "</command-args>"
            ),
        }).encode()

    def test_finds_the_last_colour(self):
        chunk = b"\n".join([self._line("red"), self._line("green")])
        self.assertEqual(scan_colour(chunk), "green")

    def test_ignores_unrelated_records(self):
        chunk = b'{"type":"user","content":"talk about /color someday"}'
        self.assertIsNone(scan_colour(chunk))

    def test_survives_a_truncated_line(self):
        chunk = b'{"type":"system","content":"<command-name>/color</comm\n' + self._line("pink")
        self.assertEqual(scan_colour(chunk), "pink")

    def test_empty_input_is_no_colour(self):
        self.assertIsNone(scan_colour(b""))

    def test_case_is_normalised(self):
        self.assertEqual(scan_colour(self._line("TEAL")), "teal")


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
