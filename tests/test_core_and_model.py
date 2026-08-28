"""Collector core: merge precedence, adapter isolation, snapshot serialization."""

from __future__ import annotations

import json
import unittest

from agentview.collector.adapters.base import Adapter
from agentview.collector.core import collect, merge
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


if __name__ == "__main__":
    unittest.main()
