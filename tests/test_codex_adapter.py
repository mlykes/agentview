"""Codex resume-list adapter tests."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from agentview.collector.adapters.codex import (
    CodexAdapter,
    _latest_state_db,
    collapse_same_process_continuations,
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
            thread_pid_fn=lambda thread_id: 4321 if thread_id == "thread-live" else None,
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
        self.assertEqual(record.pid, 4321)

    def test_attach_resumes_by_thread_id_and_is_reconnectable(self):
        attach = {r.name: r for r in self.records}["Fallback title"].attach
        self.assertTrue(attach.available)
        self.assertEqual(attach.argv[-3:], ["/usr/local/bin/codex", "resume", "thread-untitled"])
        self.assertIn("tmux attach -r", attach.argv_readonly[2])

    def test_active_thread_is_visible_but_not_falsely_resumable(self):
        record = {r.name: r for r in self.records}["Useful title"]
        self.assertEqual(record.status, "busy")
        self.assertFalse(record.attach.available)
        self.assertIn("one writer", record.attach.reason)

    def test_missing_binary_only_disables_attach(self):
        adapter = CodexAdapter(
            codex_home=self.home, which_fn=lambda name: None,
            tmux_available_fn=lambda: False,
            active_thread_fn=lambda thread_id: False,
            tmux_has_session_fn=lambda session: False,
            thread_pid_fn=lambda thread_id: None,
        )
        records, _ = adapter.discover(ContextRef(id="ctx"))
        self.assertTrue(records)
        self.assertFalse(records[0].attach.available)

    def test_newest_state_schema_wins(self):
        (self.home / "state_6.sqlite").touch()
        self.assertEqual(_latest_state_db(self.home).name, "state_6.sqlite")


class ForkContinuationTest(unittest.TestCase):
    @staticmethod
    def record(session_id, name, pid, parent=None):
        extra = {"session_id": session_id}
        if parent:
            extra["forked_from_id"] = parent
        return AgentRecord(
            id="ctx:codex:" + session_id, harness="codex", context_id="ctx",
            name=name, pid=pid, extra=extra,
        )

    def test_cd_continuation_in_same_process_hides_old_thread(self):
        old = self.record("old", "same chat", 123)
        moved = self.record("new", "same chat", 123, parent="old")
        self.assertEqual(collapse_same_process_continuations([old, moved]), [moved])

    def test_separate_live_forks_remain_visible(self):
        old = self.record("old", "same chat", 123)
        fork = self.record("new", "same chat", 456, parent="old")
        self.assertEqual(collapse_same_process_continuations([old, fork]), [old, fork])


if __name__ == "__main__":
    unittest.main()
