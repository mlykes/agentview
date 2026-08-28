"""agentview's own per-agent display settings: label and colour.

Written against stdlib unittest, like the rest: the hub must run on a bare
interpreter, so its tests must too.
"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentview.hub.overrides import MAX_NAME, Overrides, clean_colour, clean_name
from agentview.hub.registry import Registry


class CleanNameTest(unittest.TestCase):
    def test_trims_and_collapses_whitespace(self):
        self.assertEqual(clean_name("  api   refactor \n"), "api refactor")

    def test_empty_becomes_none_so_it_clears_the_label(self):
        for value in ("", "   ", "\n", None, 17, []):
            self.assertIsNone(clean_name(value))

    def test_control_characters_are_stripped(self):
        """These would corrupt the row, and the terminal of anyone piping
        /v1/agents through a shell."""
        self.assertEqual(clean_name("api\x1b[31mred\x00"), "api[31mred")

    def test_overlong_names_are_truncated(self):
        self.assertEqual(len(clean_name("x" * 500)), MAX_NAME)


class CleanColourTest(unittest.TestCase):
    def test_known_colours_pass(self):
        self.assertEqual(clean_colour("Red"), "red")
        self.assertEqual(clean_colour(" orange "), "orange")

    def test_unknown_colours_are_rejected(self):
        """The value ends up inside a CSS custom property name. Anything we have no
        token for would render as no colour at all, so it is refused rather than
        stored and silently ignored."""
        for value in ("chartreuse", "#ff0000", "var(--x)", "", None, 3):
            self.assertIsNone(clean_colour(value))


class OverridesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "names.json"
        self.store = Overrides(self.path)

    def test_set_and_read_a_label(self):
        self.assertEqual(self.store.set_name("a:1", "the api one"), "the api one")
        self.assertEqual(self.store.get("a:1"), {"name": "the api one"})

    def test_set_and_read_a_colour(self):
        self.assertEqual(self.store.set_colour("a:1", "teal"), "teal")
        self.assertEqual(self.store.get("a:1")["color"], "teal")

    def test_label_and_colour_coexist(self):
        self.store.set_name("a:1", "api")
        self.store.set_colour("a:1", "pink")
        entry = self.store.get("a:1")
        self.assertEqual(entry["name"], "api")
        self.assertEqual(entry["color"], "pink")

    def test_clearing_one_leaves_the_other(self):
        self.store.set_name("a:1", "api")
        self.store.set_colour("a:1", "pink")
        self.store.set_name("a:1", "")
        self.assertEqual(self.store.get("a:1")["color"], "pink")
        self.assertNotIn("name", self.store.get("a:1"))

    def test_unset_agent_has_nothing(self):
        self.assertEqual(self.store.get("nope"), {})
        self.assertEqual(self.store.get(None), {})

    def test_clearing_everything_drops_the_record(self):
        """Otherwise the file accumulates an empty entry per agent ever touched."""
        self.store.set_name("a:1", "api")
        self.store.set_name("a:1", "")
        self.assertEqual(self.store.all(), {})

    def test_an_unknown_colour_clears_rather_than_stores(self):
        self.store.set_colour("a:1", "teal")
        self.assertIsNone(self.store.set_colour("a:1", "chartreuse"))
        self.assertEqual(self.store.get("a:1"), {})

    def test_overrides_survive_a_restart(self):
        self.store.set_name("a:1", "kept")
        self.store.set_colour("a:1", "blue")
        reloaded = Overrides(self.path)
        entry = reloaded.get("a:1")
        self.assertEqual(entry["name"], "kept")
        self.assertEqual(entry["color"], "blue")

    def test_a_colour_records_when_it_was_set(self):
        """The time is what lets this be compared against a `/color` in the
        session, so that the more recent of the two wins."""
        before = time.time()
        self.store.set_colour("a:1", "blue")
        self.assertGreaterEqual(self.store.get("a:1")["color_at"], before)

    def test_clearing_a_colour_drops_its_timestamp(self):
        self.store.set_colour("a:1", "blue")
        self.store.set_colour("a:1", "")
        self.assertNotIn("color_at", self.store.get("a:1"))

    def test_the_timestamp_survives_a_restart(self):
        self.store.set_colour("a:1", "blue")
        stamp = self.store.get("a:1")["color_at"]
        self.assertEqual(Overrides(self.path).get("a:1")["color_at"], stamp)

    def test_the_older_flat_file_still_loads(self):
        """The file began as {id: name}. Labels made before colours existed must not
        be dropped by the upgrade."""
        self.path.write_text(json.dumps({"a:1": "made earlier", "a:2": ""}))
        loaded = Overrides(self.path)
        self.assertEqual(loaded.get("a:1"), {"name": "made earlier"})
        self.assertEqual(loaded.get("a:2"), {})

    def test_a_flat_file_is_upgraded_in_place_on_write(self):
        self.path.write_text(json.dumps({"a:1": "made earlier"}))
        store = Overrides(self.path)
        store.set_colour("a:1", "green")
        written = json.loads(self.path.read_text())["a:1"]
        self.assertEqual(written["name"], "made earlier")
        self.assertEqual(written["color"], "green")

    def test_a_corrupt_file_loses_overrides_but_not_the_hub(self):
        self.path.write_text("{not json")
        self.assertEqual(Overrides(self.path).all(), {})

    def test_a_non_object_file_is_ignored(self):
        self.path.write_text("[1, 2, 3]")
        self.assertEqual(Overrides(self.path).all(), {})

    def test_hand_edited_values_are_cleaned_on_the_way_in(self):
        """The file is editable by hand, so it cannot be trusted to be clean."""
        self.path.write_text(json.dumps({
            "a:1": {"name": "  spaced  out  ", "color": "NOPE"},
        }))
        self.assertEqual(Overrides(self.path).get("a:1"), {"name": "spaced out"})

    def test_missing_directory_is_created(self):
        nested = Path(self.tmp.name) / "deep" / "down" / "names.json"
        Overrides(nested).set_name("a:1", "x")
        self.assertEqual(Overrides(nested).get("a:1"), {"name": "x"})


class RegistryOverlayTest(unittest.TestCase):
    """Overrides are applied in _annotate, so every read path agrees."""

    def _registry(self, overrides, harness_colour=None, harness_at=None):
        agents = [
            {"id": "h1:x:1", "name": "session-abc", "status": "idle"},
            {"id": "h1:x:2", "name": "untouched", "status": "idle"},
        ]
        if harness_colour:
            agents[0]["color"] = harness_colour
        if harness_at is not None:
            agents[0]["extra"] = {"color_at": harness_at}
        registry = Registry(override_fn=lambda agent_id: overrides.get(agent_id, {}))
        registry.ingest({
            "context": {"id": "h1", "label": "host", "kind": "host"},
            "agents": agents,
            "warnings": [],
            "collected_at": 0,
        })
        return registry

    def _agents(self, *args, **kwargs):
        return {a["id"]: a for a in self._registry(*args, **kwargs).flat_agents()}

    def test_label_replaces_the_displayed_name(self):
        agents = self._agents({"h1:x:1": {"name": "my label"}})
        self.assertEqual(agents["h1:x:1"]["name"], "my label")
        self.assertEqual(agents["h1:x:1"]["harness_name"], "session-abc")

    def test_colour_is_applied_where_the_harness_records_none(self):
        """The case this exists for: an interactive session's /color is never
        written to disk, so there is nothing to inherit."""
        agents = self._agents({"h1:x:1": {"color": "red"}})
        self.assertEqual(agents["h1:x:1"]["color"], "red")
        self.assertNotIn("harness_color", agents["h1:x:1"])

    def test_an_explicit_colour_beats_an_untimed_harness_one(self):
        agents = self._agents({"h1:x:1": {"color": "red"}}, harness_colour="green")
        self.assertEqual(agents["h1:x:1"]["color"], "red")
        self.assertEqual(agents["h1:x:1"]["harness_color"], "green")

    def test_the_more_recent_change_wins(self):
        """A colour can be set in two places -- the swatch here, or `/color` in the
        session. If one always overruled the other, changing it in the losing place
        would appear to do nothing, which is exactly what was reported."""
        agents = self._agents(
            {"h1:x:1": {"color": "red", "color_at": 100.0}},
            harness_colour="green", harness_at=200.0,
        )
        self.assertEqual(agents["h1:x:1"]["color"], "green")

    def test_a_newer_override_still_wins(self):
        agents = self._agents(
            {"h1:x:1": {"color": "red", "color_at": 300.0}},
            harness_colour="green", harness_at=200.0,
        )
        self.assertEqual(agents["h1:x:1"]["color"], "red")
        self.assertEqual(agents["h1:x:1"]["harness_color"], "green")

    def test_an_untimed_override_loses_to_a_timed_session_colour(self):
        """Overrides written before colours were timestamped carry no time. Treating
        them as older is what lets a later `/color` take effect."""
        agents = self._agents(
            {"h1:x:1": {"color": "red"}}, harness_colour="green", harness_at=200.0,
        )
        self.assertEqual(agents["h1:x:1"]["color"], "green")

    def test_an_override_still_applies_where_the_harness_has_nothing(self):
        agents = self._agents({"h1:x:1": {"color": "red", "color_at": 1.0}})
        self.assertEqual(agents["h1:x:1"]["color"], "red")

    def test_the_harness_colour_is_used_when_nothing_is_set_here(self):
        agents = self._agents({}, harness_colour="green")
        self.assertEqual(agents["h1:x:1"]["color"], "green")
        self.assertNotIn("harness_color", agents["h1:x:1"])

    def test_unaffected_agents_are_untouched(self):
        agents = self._agents({"h1:x:1": {"name": "my label"}})
        self.assertEqual(agents["h1:x:2"]["name"], "untouched")
        self.assertNotIn("harness_name", agents["h1:x:2"])

    def test_the_grouped_view_agrees_with_the_flat_list(self):
        """The UI reads /v1/view and scripts read /v1/agents; they must not disagree."""
        registry = self._registry({"h1:x:1": {"name": "my label", "color": "pink"}})
        grouped = {a["id"]: a for a in registry.view()["contexts"][0]["agents"]}
        self.assertEqual(grouped["h1:x:1"]["name"], "my label")
        self.assertEqual(grouped["h1:x:1"]["color"], "pink")

    def test_a_registry_without_overrides_still_works(self):
        registry = Registry()
        registry.ingest({
            "context": {"id": "h1", "label": "host", "kind": "host"},
            "agents": [{"id": "h1:x:1", "name": "plain", "status": "idle"}],
            "warnings": [], "collected_at": 0,
        })
        self.assertEqual(registry.flat_agents()[0]["name"], "plain")


if __name__ == "__main__":
    unittest.main()
