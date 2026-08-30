"""Collector core: merge precedence, adapter isolation, snapshot serialization."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from agentview.collector.adapters.base import Adapter
from agentview.collector.core import (
    collect,
    drop_shadowed_tmux_records,
    merge,
    refresh_live_locations,
)
from agentview.model import AgentRecord, AttachSpec, ContextRef


def record(**kwargs):
    base = dict(id="c:h:1", harness="h", context_id="c", name="a")
    base.update(kwargs)
    return AgentRecord(**base)


class Boom(Adapter):
    name = "boom"
    priority = 1

    def available(self):
        return True

    def discover(self, ctx):
        raise RuntimeError("adapter exploded")


class Fine(Adapter):
    name = "fine"
    priority = 2

    def available(self):
        return True

    def discover(self, ctx):
        return [record(name="survivor")], []


class MergeTest(unittest.TestCase):
    def test_prefers_higher_priority_but_fills_blanks(self):
        rich = record(cwd="/workspace", git_branch=None)
        poor = record(cwd="/elsewhere", git_branch="main")
        merged = merge([[rich], [poor]])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].cwd, "/workspace")  # richer adapter wins
        self.assertEqual(merged[0].git_branch, "main")  # but blanks get filled

    def test_keeps_a_working_attach_over_a_later_adapters(self):
        """The two attach routes must not fight.

        ClaudeCodeAdapter now supplies `claude attach` for background sessions and
        deliberately supplies nothing for interactive ones, so TmuxAdapter can fill
        those in. That only works if a route already present survives the merge.
        """
        bg = record(attach=AttachSpec(available=True, argv=["claude", "attach", "j1"]))
        later = record(attach=AttachSpec(available=True, argv=["tmux", "attach"]))
        merged = merge([[bg], [later]])
        self.assertEqual(merged[0].attach.argv, ["claude", "attach", "j1"])

    def test_takes_a_working_attach_from_a_later_adapter(self):
        without = record(attach=AttachSpec.unavailable("no tmux"))
        with_attach = record(attach=AttachSpec(available=True, argv=["tmux", "attach"]))
        merged = merge([[without], [with_attach]])
        self.assertTrue(merged[0].attach.available)
        self.assertEqual(merged[0].attach.argv, ["tmux", "attach"])


class CollectTest(unittest.TestCase):
    def test_a_failing_adapter_does_not_take_down_the_collection(self):
        """One bad adapter must degrade to a warning, never an empty or crashed HUD."""
        snapshot = collect(ctx=ContextRef(id="c", label="test"), adapters=[Boom(), Fine()])
        self.assertEqual([a.name for a in snapshot.agents], ["survivor"])
        self.assertTrue(any("adapter exploded" in w for w in snapshot.warnings))

    def test_snapshot_is_json_serializable(self):
        snapshot = collect(ctx=ContextRef(id="c", label="test"), adapters=[Fine()])
        payload = json.loads(json.dumps(snapshot.to_dict()))
        self.assertEqual(payload["agents"][0]["name"], "survivor")
        self.assertEqual(payload["context"]["id"], "c")


class ShadowedTmuxRecordTest(unittest.TestCase):
    """One agent, two adapters, ids that cannot be joined.

    A harness with its own store keys on a session or thread id; the tmux adapter keys
    on the pane. Only a pid says they are the same agent.
    """

    def _pair(self, rich_pid):
        pane_attach = AttachSpec(available=True, argv=["tmux", "attach", "-t", "work"])
        return [
            record(id="c:codex:thread-1", harness="codex", name="a real name",
                   pid=rich_pid, source="codex",
                   attach=AttachSpec.unavailable("cannot resume, it is already open")),
            record(id="c:codex:tmux-work", harness="codex", name="work", pid=4242,
                   source="tmux", attach=pane_attach, extra={"tmux_session": "work"}),
        ]

    def test_the_duplicate_pane_row_collapses_into_the_real_one(self):
        kept = drop_shadowed_tmux_records(self._pair(rich_pid=4242))
        self.assertEqual([r.id for r in kept], ["c:codex:thread-1"])
        self.assertEqual(kept[0].name, "a real name")

    def test_the_survivor_inherits_the_pane_it_is_running_in(self):
        """The point of joining, not just a tidier list: attach now reattaches to the
        terminal already running the session instead of starting a second client."""
        kept = drop_shadowed_tmux_records(self._pair(rich_pid=4242))
        self.assertTrue(kept[0].attach.available)
        self.assertEqual(kept[0].attach.argv, ["tmux", "attach", "-t", "work"])
        self.assertEqual(kept[0].extra["tmux_session"], "work")

    def test_an_unrelated_pane_is_left_alone(self):
        """Guard against over-merging: a pid outside the pane is a different agent."""
        kept = drop_shadowed_tmux_records(self._pair(rich_pid=None))
        self.assertEqual(len(kept), 2)


class LiveLocationTest(unittest.TestCase):
    """A registry records where a session started; agents move."""

    def test_a_moved_agent_is_filed_where_it_actually_is(self):
        rec = record(pid=4242, cwd="/where/it/started")
        with mock.patch("agentview.collector.procs.cwd_for_pid", return_value="/where/it/is"), \
                mock.patch("agentview.collector.core._git_branch", return_value="feature"):
            refresh_live_locations([rec])
        self.assertEqual(rec.cwd, "/where/it/is")
        self.assertEqual(rec.git_branch, "feature")

    def test_the_pane_beats_the_pid(self):
        """An adapter can report a supervisor rather than the TUI; tmux knows the
        pane the user is actually sitting in."""
        rec = record(pid=4242, cwd="/stale", extra={"tmux_session": "work"})
        with mock.patch("agentview.collector.procs.cwd_for_pid", return_value="/from-pid"), \
                mock.patch("agentview.collector.tmux.path_for_session",
                           return_value="/from-pane"), \
                mock.patch("agentview.collector.core._git_branch", return_value=None):
            refresh_live_locations([rec])
        self.assertEqual(rec.cwd, "/from-pane")

    def test_codex_keeps_the_directory_its_own_store_reports(self):
        """Codex changes a thread's logical directory without the long-lived CLI
        process ever chdir'ing, so the process cwd is the wrong answer for it."""
        rec = record(harness="codex", pid=4242, cwd="/the/thread/dir")
        with mock.patch("agentview.collector.procs.cwd_for_pid",
                        return_value="/where/the/cli/started"):
            refresh_live_locations([rec])
        self.assertEqual(rec.cwd, "/the/thread/dir")

    def test_an_agent_with_no_pid_is_left_alone(self):
        rec = record(pid=None, cwd="/unchanged")
        refresh_live_locations([rec])
        self.assertEqual(rec.cwd, "/unchanged")


if __name__ == "__main__":
    unittest.main()
