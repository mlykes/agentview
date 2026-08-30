"""Codex resume-list adapter tests."""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from agentview.collector.adapters.codex import (
    CodexAdapter,
    _latest_state_db,
    codex_pids,
    collapse_forked_continuations,
    resumed_thread_pids,
)
from agentview.model import AgentRecord, ContextRef, STATUS_IDLE


SCHEMA = """
CREATE TABLE threads (
 id TEXT PRIMARY KEY, title TEXT, name TEXT, cwd TEXT, cli_version TEXT,
 tokens_used INTEGER, git_branch TEXT, created_at_ms INTEGER, updated_at_ms INTEGER,
 recency_at_ms INTEGER, archived INTEGER, preview TEXT, source TEXT,
 thread_source TEXT
)
"""


class CodexAdapterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        db = sqlite3.connect(str(self.home / "state_5.sqlite"))
        db.executescript(SCHEMA)
        rows = [
            ("thread-live", "first prompt", "Useful title", "/workspace", "0.150.1",
             1234, "main", 1000, 2000, 3000, 0, "first prompt", "cli", "user"),
            ("thread-untitled", "Fallback title", None, "/other", "0.150.1",
             12, None, 4000, 5000, 6000, 0, "hello", "cli", "user"),
            ("thread-archived", "Archived", None, "/old", "0.150.1",
             1, None, 1, 1, 1, 1, "old", "cli", "user"),
            ("thread-exec", "Automation", None, "/bot", "0.150.1",
             1, None, 1, 1, 1, 0, "bot", "exec", "user"),
            ("thread-subagent", "Child", None, "/bot", "0.150.1",
             1, None, 1, 1, 1, 0, "child", "cli", "subagent"),
        ]
        db.executemany("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        db.commit()
        db.close()
        self.adapter = CodexAdapter(
            codex_home=self.home,
            which_fn=lambda name: "/usr/local/bin/codex" if name == "codex" else None,
            tmux_available_fn=lambda: True,
            active_thread_fn=lambda thread_id: thread_id == "thread-live",
            tmux_has_session_fn=lambda session: False,
            commands_fn=lambda: {},
        )
        self.records, self.warnings = self.adapter.discover(ContextRef(id="ctx"))

    def test_finds_the_same_kind_of_interactive_threads_as_resume(self):
        self.assertEqual([r.name for r in self.records], ["Fallback title", "Useful title"])
        self.assertEqual(self.records[0].status, STATUS_IDLE)
        self.assertFalse(self.warnings)

    def test_metadata_is_preserved(self):
        record = {r.name: r for r in self.records}["Useful title"]
        self.assertEqual(record.cwd, "/workspace")
        self.assertEqual(record.git_branch, "main")
        self.assertEqual(record.harness_version, "0.150.1")
        self.assertEqual(record.tokens, 1234)
        self.assertEqual(record.updated_at, 3.0)

    def test_attach_resumes_by_thread_id_and_is_reconnectable(self):
        attach = {r.name: r for r in self.records}["Fallback title"].attach
        self.assertTrue(attach.available)
        self.assertEqual(attach.argv[-3:], ["/usr/local/bin/codex", "resume", "thread-untitled"])
        self.assertIn("tmux attach -r", attach.argv_readonly[2])

    def test_a_held_thread_is_visible_but_not_falsely_resumable(self):
        record = {r.name: r for r in self.records}["Useful title"]
        self.assertFalse(record.attach.available)
        self.assertIn("one writer", record.attach.reason)

    def test_a_terminal_left_open_overnight_is_not_called_busy(self):
        """Codex holds the writer lock for as long as the client is open, whether or
        not anything is happening. Reading that as busy made every abandoned terminal
        a permanent `stuck` alert, which is the one number the HUD exists to report.
        """
        record = {r.name: r for r in self.records}["Useful title"]
        self.assertEqual(record.status, STATUS_IDLE)
        self.assertIn("idle", record.detail)

    def test_a_thread_that_just_moved_is_busy(self):
        """The other half: recent activity under a held lock is genuinely working,
        and must still be reported that way."""
        db = sqlite3.connect(str(self.home / "state_5.sqlite"))
        db.execute(
            "UPDATE threads SET recency_at_ms = ? WHERE id = 'thread-live'",
            (int(time.time() * 1000),),
        )
        db.commit()
        db.close()
        records, _ = self.adapter.discover(ContextRef(id="ctx"))
        record = {r.name: r for r in records}["Useful title"]
        self.assertEqual(record.status, "busy")
        self.assertIn("working", record.detail)

    def test_a_thread_nobody_has_open_is_idle_and_resumable(self):
        record = {r.name: r for r in self.records}["Fallback title"]
        self.assertEqual(record.status, STATUS_IDLE)
        self.assertIn("resumable", record.detail)

    def test_missing_binary_only_disables_attach(self):
        adapter = CodexAdapter(
            codex_home=self.home, which_fn=lambda name: None,
            tmux_available_fn=lambda: False,
            active_thread_fn=lambda thread_id: False,
            tmux_has_session_fn=lambda session: False,
            commands_fn=lambda: {},
        )
        records, _ = adapter.discover(ContextRef(id="ctx"))
        self.assertTrue(records)
        self.assertFalse(records[0].attach.available)

    def test_newest_state_schema_wins(self):
        (self.home / "state_6.sqlite").touch()
        self.assertEqual(_latest_state_db(self.home).name, "state_6.sqlite")


class ProcessJoinTest(unittest.TestCase):
    """Tying a thread to the process working on it.

    Without this, a `codex` running in a tmux session is reported twice: once from the
    thread store and once from the pane, with ids that cannot be joined. A pid is all
    `core.drop_shadowed_tmux_records` needs to see they are one agent.
    """

    def test_a_resumed_client_names_its_thread_in_argv(self):
        found = resumed_thread_pids({
            42: "codex resume 01a04642-7921-7233-b039-2e0d7da5fc4e",
            43: "/usr/bin/vim notes.txt",
        })
        self.assertEqual(found, {"01a04642-7921-7233-b039-2e0d7da5fc4e": 42})

    def test_the_helper_process_is_not_mistaken_for_a_client(self):
        """Every client spawns a `codex-code-mode-host`; matching the exact basename
        keeps it out without naming it."""
        self.assertEqual(codex_pids({7: "/opt/codex/bin/codex-code-mode-host"}), [])
        self.assertEqual(codex_pids({8: "/home/me/.local/bin/codex"}), [8])

    def test_the_shared_app_server_is_not_taken_for_a_session(self):
        """One `codex app-server` backs every thread and holds all their transcripts
        open, so a join based on open files sees it as a client for whichever thread
        it happens to match -- naming a shared process as one arbitrary session."""
        self.assertEqual(
            codex_pids({7: "/Users/me/.codex/packages/standalone/current/codex app-server --listen unix://"}),
            [],
        )

    def test_a_real_client_is_still_kept(self):
        self.assertEqual(codex_pids({8: "/home/me/.local/bin/codex"}), [8])
        self.assertEqual(codex_pids({9: "codex resume 01a04642"}), [9])

    def test_resume_with_a_flag_joins_nothing_rather_than_guessing(self):
        self.assertEqual(resumed_thread_pids({42: "codex resume --last"}), {})

    def test_a_fresh_client_is_joined_by_the_transcript_it_holds_open(self):
        """A bare `codex` carries no thread id, but it holds its thread's rollout
        file open, and the file is named after the thread."""
        adapter = self._adapter(
            commands={99: "codex"},
            rollouts={99: ["/home/me/.codex/sessions/rollout-2026-08-27-thread-live.jsonl"]},
        )
        records, _ = adapter.discover(ContextRef(id="ctx"))
        self.assertEqual({r.name: r.pid for r in records}["Useful title"], 99)

    def test_a_client_holding_two_transcripts_binds_to_the_active_one(self):
        """Starting a second thread leaves the first one's transcript open, so a pid
        can hold several. Picking the first found would bind to whichever the OS
        happened to list first."""
        adapter = self._adapter(
            commands={99: "codex"},
            rollouts={99: [
                "/rollouts/rollout-thread-live.jsonl",
                "/rollouts/rollout-thread-untitled.jsonl",
            ]},
        )
        records, _ = adapter.discover(ContextRef(id="ctx"))
        by_name = {r.name: r.pid for r in records}
        # thread-untitled is the more recently active of the two.
        self.assertEqual(by_name["Fallback title"], 99)
        self.assertIsNone(by_name["Useful title"])

    def test_argv_wins_over_the_filesystem_and_is_not_asked_twice(self):
        asked = []

        def rollouts(pids):
            asked.append(list(pids))
            return {}

        adapter = self._adapter(
            commands={99: "codex resume thread-live"}, rollouts_fn=rollouts
        )
        records, _ = adapter.discover(ContextRef(id="ctx"))
        self.assertEqual({r.name: r.pid for r in records}["Useful title"], 99)
        self.assertEqual(asked, [])  # nothing left unclaimed, so no lookup at all

    def _adapter(self, commands, rollouts=None, rollouts_fn=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        home = Path(temp.name)
        db = sqlite3.connect(str(home / "state_5.sqlite"))
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            ("thread-live", "first prompt", "Useful title", "/workspace", "0.150.1",
             1234, "main", 1000, 2000, 3000, 0, "first prompt", "cli", "user"),
            ("thread-untitled", "Fallback title", None, "/other", "0.150.1",
             12, None, 4000, 5000, 6000, 0, "hello", "cli", "user"),
        ])
        db.commit()
        db.close()
        return CodexAdapter(
            codex_home=home,
            which_fn=lambda name: "/usr/local/bin/codex",
            tmux_available_fn=lambda: True,
            active_thread_fn=lambda thread_id: False,
            tmux_has_session_fn=lambda session: False,
            commands_fn=lambda: commands,
            rollouts_fn=rollouts_fn or (lambda pids: rollouts or {}),
        )


class ForkedContinuationTest(unittest.TestCase):
    """Codex forks a thread when settings change -- a new id, the same conversation.
    Both threads stay in the store, so the same session is listed twice."""

    def _rec(self, ident, name, pid=None, forked_from=None):
        return AgentRecord(
            id="ctx:codex:" + ident, harness="codex", context_id="ctx", name=name,
            pid=pid, extra={"session_id": ident, "forked_from_id": forked_from},
        )

    def test_the_superseded_thread_is_hidden(self):
        kept = collapse_forked_continuations([
            self._rec("old", "same name"),
            self._rec("new", "same name", pid=42, forked_from="old"),
        ])
        self.assertEqual([r.extra["session_id"] for r in kept], ["new"])

    def test_a_fork_with_its_own_process_stays_visible(self):
        """Two live forks are two real terminals, not one session listed twice."""
        kept = collapse_forked_continuations([
            self._rec("old", "same name", pid=7),
            self._rec("new", "same name", pid=42, forked_from="old"),
        ])
        self.assertEqual(len(kept), 2)

    def test_a_renamed_fork_keeps_both(self):
        """The user told them apart deliberately."""
        kept = collapse_forked_continuations([
            self._rec("old", "before"),
            self._rec("new", "after", pid=42, forked_from="old"),
        ])
        self.assertEqual(len(kept), 2)

    def test_unrelated_threads_are_untouched(self):
        kept = collapse_forked_continuations([
            self._rec("a", "one"), self._rec("b", "two"),
        ])
        self.assertEqual(len(kept), 2)

    def test_a_fork_whose_parent_is_gone_is_kept(self):
        kept = collapse_forked_continuations([
            self._rec("new", "name", pid=42, forked_from="archived-parent"),
        ])
        self.assertEqual(len(kept), 1)


if __name__ == "__main__":
    unittest.main()
