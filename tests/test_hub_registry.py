"""Hub registry: TTL expiry, context nesting, stuck detection."""

from __future__ import annotations

import time
import unittest

from agentview.hub.registry import Registry


def snapshot(context_id, label, agents, kind="host", parent_id=None):
    return {
        "context": {
            "id": context_id, "label": label, "kind": kind,
            "parent_id": parent_id, "platform": "linux", "arch": "x86_64",
        },
        "agents": agents,
        "warnings": [],
        "collected_at": time.time(),
    }


def agent(agent_id="a", status="idle", started_ago=60.0, updated_ago=5.0):
    now = time.time()
    return {
        "id": agent_id, "name": agent_id, "status": status,
        "started_at": now - started_ago, "updated_at": now - updated_ago,
    }


class NestingTest(unittest.TestCase):
    def setUp(self):
        self.registry = Registry()
        self.registry.ingest(snapshot("h1", "laptop", [agent("host-agent")]))
        self.registry.ingest(
            snapshot("c1", "devcontainer: demo", [agent("ctr-agent")],
                     kind="container", parent_id="h1")
        )

    def test_container_nests_under_its_host(self):
        view = self.registry.view()
        self.assertEqual(len(view["contexts"]), 1)
        root = view["contexts"][0]
        self.assertEqual(root["context"]["id"], "h1")
        self.assertEqual([c["context"]["id"] for c in root["children"]], ["c1"])

    def test_totals_count_nested_agents(self):
        self.assertEqual(self.registry.view()["totals"]["agents"], 2)

    def test_orphan_container_stays_top_level(self):
        """Better a flat list than a wrong tree."""
        registry = Registry()
        registry.ingest(
            snapshot("c9", "orphan", [agent()], kind="container", parent_id="never-seen")
        )
        view = registry.view()
        self.assertEqual([c["context"]["id"] for c in view["contexts"]], ["c9"])

    def test_context_cannot_parent_itself(self):
        registry = Registry()
        registry.ingest(snapshot("x", "self", [agent()], kind="container", parent_id="x"))
        self.assertEqual(len(registry.view()["contexts"]), 1)


class ExpiryTest(unittest.TestCase):
    def test_dead_collector_expires(self):
        """A collector that dies must not leave its agents on screen forever."""
        registry = Registry(ttl=0.0)
        registry.ingest(snapshot("h1", "laptop", [agent()]))
        time.sleep(0.01)
        self.assertEqual(registry.view()["totals"]["agents"], 0)
        registry.prune()
        self.assertEqual(registry.contexts(), [])

    def test_fresh_collector_is_kept(self):
        registry = Registry(ttl=60.0)
        registry.ingest(snapshot("h1", "laptop", [agent()]))
        self.assertEqual(registry.view()["totals"]["agents"], 1)

    def test_resnapshot_replaces_rather_than_appends(self):
        registry = Registry()
        registry.ingest(snapshot("h1", "laptop", [agent("one"), agent("two")]))
        registry.ingest(snapshot("h1", "laptop", [agent("one")]))
        self.assertEqual(registry.view()["totals"]["agents"], 1)


class StuckTest(unittest.TestCase):
    def test_busy_and_silent_is_stuck(self):
        registry = Registry(stuck_after=100.0)
        registry.ingest(snapshot("h1", "l", [agent(status="busy", updated_ago=500.0)]))
        self.assertEqual(registry.view()["totals"]["stuck"], 1)

    def test_busy_and_recent_is_not_stuck(self):
        """A long turn is not a wedge -- this false positive is why the default is 15m."""
        registry = Registry(stuck_after=900.0)
        registry.ingest(snapshot("h1", "l", [agent(status="busy", updated_ago=500.0)]))
        self.assertEqual(registry.view()["totals"]["stuck"], 0)

    def test_idle_is_never_stuck(self):
        registry = Registry(stuck_after=1.0)
        registry.ingest(snapshot("h1", "l", [agent(status="idle", updated_ago=99999.0)]))
        self.assertEqual(registry.view()["totals"]["stuck"], 0)


class FlatViewTest(unittest.TestCase):
    def test_flat_agents_spans_every_context(self):
        registry = Registry()
        registry.ingest(snapshot("h1", "laptop", [agent("a")]))
        registry.ingest(snapshot("c1", "ctr", [agent("b")], kind="container", parent_id="h1"))
        names = sorted(a["name"] for a in registry.flat_agents())
        self.assertEqual(names, ["a", "b"])

    def test_snapshot_without_context_id_is_ignored(self):
        registry = Registry()
        registry.ingest({"context": {}, "agents": [agent()]})
        self.assertEqual(registry.view()["totals"]["agents"], 0)


if __name__ == "__main__":
    unittest.main()
