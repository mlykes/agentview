"""Harness identification.

The whole-command-line scan this replaced matched any path containing a harness
name -- "pi" fired on half the filesystem. Identification looks at argv[0] and
argv[1] only.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentview import harnesses


class IdentifyTest(unittest.TestCase):
    def setUp(self):
        self.table = harnesses.load()

    def assertHarness(self, command, expected):
        self.assertEqual(harnesses.identify(command, self.table).get("harness"), expected)

    def test_bare_command(self):
        self.assertHarness("claude", "claude-code")

    def test_absolute_path(self):
        self.assertHarness("/usr/local/bin/opencode", "opencode")

    def test_process_title_suffix(self):
        self.assertHarness("claude bg-spare", "claude-code")

    def test_interpreter_wrapper(self):
        """`node .../opencode` is how several harnesses actually appear in ps."""
        self.assertHarness("node /usr/local/bin/opencode", "opencode")
        self.assertHarness("/bin/bash /tmp/bin/opencode", "opencode")
        self.assertHarness("python3 /opt/aider/aider", "aider")

    def test_unrelated_process_is_not_an_agent(self):
        self.assertHarness("sleep 600", None)
        self.assertHarness("python3 manage.py runserver", None)

    def test_harness_name_inside_an_unrelated_path_does_not_match(self):
        """The regression: 'pi' must not fire on /usr/lib/pi-tools/helper."""
        self.assertHarness("/usr/lib/pi-tools/helper --flag", None)
        self.assertHarness("/opt/claude-backups/cleanup.sh", None)

    def test_argument_containing_a_harness_name_does_not_match(self):
        self.assertHarness("grep -r claude /var/log", None)

    def test_empty(self):
        self.assertHarness("", None)


class ConfigOverrideTest(unittest.TestCase):
    def test_user_config_extends_the_table(self):
        """Adding a harness must be a config line, not a code change."""
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / ".agentview"
            home.mkdir()
            (home / "harnesses.json").write_text(
                json.dumps({"mytool": {"harness": "mytool", "label": "MyTool"}})
            )
            import os

            os.environ["AGENTVIEW_HOME"] = str(home)
            try:
                table = harnesses.load()
            finally:
                del os.environ["AGENTVIEW_HOME"]
        self.assertEqual(harnesses.identify("mytool", table).get("harness"), "mytool")
        self.assertEqual(harnesses.identify("claude", table).get("harness"), "claude-code")


if __name__ == "__main__":
    unittest.main()
