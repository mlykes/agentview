"""agentview's own labels for agents.

Written against stdlib unittest, like the rest: the hub must run on a bare
interpreter, so its tests must too.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentview.hub.nicknames import MAX_NAME, Nicknames, clean
from agentview.hub.registry import Registry


class CleanTest(unittest.TestCase):
    def test_trims_and_collapses_whitespace(self):
        self.assertEqual(clean("  api   refactor \n"), "api refactor")

    def test_empty_becomes_none_so_it_clears_the_label(self):
        for value in ("", "   ", "\n", None, 17, []):
            self.assertIsNone(clean(value))

    def test_control_characters_are_stripped(self):
        """These would corrupt the row, and the terminal of anyone piping
        /v1/agents through a shell."""
        self.assertEqual(clean("api\x1b[31mred\x00"), "api[31mred")

    def test_overlong_names_are_truncated(self):
        self.assertEqual(len(clean("x" * 500)), MAX_NAME)


class NicknamesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "names.json"
        self.store = Nicknames(self.path)

    def test_set_then_get(self):
        self.assertEqual(self.store.set("a:1", "the api one"), "the api one")
        self.assertEqual(self.store.get("a:1"), "the api one")

    def test_unset_agent_has_no_label(self):
        self.assertIsNone(self.store.get("nope"))
        self.assertIsNone(self.store.get(None))

    def test_empty_name_clears_the_label(self):
        self.store.set("a:1", "temporary")
        self.assertIsNone(self.store.set("a:1", "   "))
        self.assertIsNone(self.store.get("a:1"))

    def test_labels_survive_a_restart(self):
        self.store.set("a:1", "kept")
        self.assertEqual(Nicknames(self.path).get("a:1"), "kept")

    def test_a_corrupt_file_loses_labels_but_not_the_hub(self):
        self.path.write_text("{not json")
        self.assertEqual(Nicknames(self.path).all(), {})

    def test_a_non_object_file_is_ignored(self):
        self.path.write_text("[1, 2, 3]")
        self.assertEqual(Nicknames(self.path).all(), {})

    def test_stored_names_are_cleaned_on_the_way_back_in(self):
        """The file is editable by hand, so it cannot be trusted to be clean."""
        self.path.write_text(json.dumps({"a:1": "  spaced  out  ", "a:2": ""}))
        loaded = Nicknames(self.path)
        self.assertEqual(loaded.get("a:1"), "spaced out")
        self.assertIsNone(loaded.get("a:2"))

    def test_missing_directory_is_created(self):
        nested = Path(self.tmp.name) / "deep" / "down" / "names.json"
        Nicknames(nested).set("a:1", "x")
        self.assertEqual(Nicknames(nested).get("a:1"), "x")


class RegistryOverlayTest(unittest.TestCase):
    """The label is applied in _annotate, so every read path agrees on the name."""

    def _registry(self, labels):
        registry = Registry(nickname_fn=lambda agent_id: labels.get(agent_id))
        registry.ingest({
            "context": {"id": "h1", "label": "host", "kind": "host"},
            "agents": [
                {"id": "h1:x:1", "name": "session-abc", "status": "idle"},
                {"id": "h1:x:2", "name": "untouched", "status": "idle"},
            ],
            "warnings": [],
            "collected_at": 0,
        })
        return registry

    def test_label_replaces_the_displayed_name(self):
        agents = {a["id"]: a for a in self._registry({"h1:x:1": "my label"}).flat_agents()}
        self.assertEqual(agents["h1:x:1"]["name"], "my label")

    def test_the_harness_name_is_kept_not_discarded(self):
        agents = {a["id"]: a for a in self._registry({"h1:x:1": "my label"}).flat_agents()}
        self.assertEqual(agents["h1:x:1"]["harness_name"], "session-abc")

    def test_unlabelled_agents_are_untouched(self):
        agents = {a["id"]: a for a in self._registry({"h1:x:1": "my label"}).flat_agents()}
        self.assertEqual(agents["h1:x:2"]["name"], "untouched")
        self.assertNotIn("harness_name", agents["h1:x:2"])

    def test_the_grouped_view_agrees_with_the_flat_list(self):
        """The UI reads /v1/view and scripts read /v1/agents; they must not disagree."""
        registry = self._registry({"h1:x:1": "my label"})
        grouped = registry.view()["contexts"][0]["agents"]
        by_id = {a["id"]: a["name"] for a in grouped}
        self.assertEqual(by_id["h1:x:1"], "my label")

    def test_a_registry_without_labels_still_works(self):
        registry = Registry()
        registry.ingest({
            "context": {"id": "h1", "label": "host", "kind": "host"},
            "agents": [{"id": "h1:x:1", "name": "plain", "status": "idle"}],
            "warnings": [], "collected_at": 0,
        })
        self.assertEqual(registry.flat_agents()[0]["name"], "plain")


if __name__ == "__main__":
    unittest.main()
