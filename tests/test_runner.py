"""`agentview run` -- session naming and environment sanitation."""

from __future__ import annotations

import os
import types
import unittest
from unittest import mock

from agentview import runner
from agentview.runner import (
    inherited_session_vars,
    sanitize,
    session_name,
    slugify,
)


class SlugTest(unittest.TestCase):
    def test_session_names_avoid_tmux_target_separators(self):
        """tmux treats ":" and "." as target separators, so they cannot appear."""
        for raw in ("my.agent", "api:v2", "weird/name", "Spaces Here"):
            name = session_name(raw)
            self.assertNotIn(":", name)
            self.assertNotIn(".", name)
            self.assertTrue(name.startswith("agentview_"))

    def test_empty_name_still_produces_something(self):
        self.assertEqual(slugify("!!!"), "agent")


class EnvSanitationTest(unittest.TestCase):
    """The regression: a child agent must not inherit its parent's identity.

    Launching an agent from inside another agent is the normal way to try this, and
    the parent's environment carries its session id, name and -- for Claude Code --
    its messaging socket and token. Inheriting them made the new agent register
    under the parent's name.
    """

    PARENT_ENV = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "CLAUDE_CODE_SESSION_ID": "abc",
        "CLAUDE_CODE_MESSAGING_TOKEN": "secret",
        "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/s.sock",
        "CLAUDECODE": "1",
        "CLAUDE_PID": "123",
        "AI_AGENT": "claude",
        "TMUX": "/tmp/tmux-501/default,1,0",
        "CLAUDE_CONFIG_DIR": "/home/x/.claude",
    }

    def test_identifying_variables_are_stripped(self):
        stripped = inherited_session_vars(self.PARENT_ENV)
        for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_TOKEN",
                     "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDECODE", "CLAUDE_PID",
                     "AI_AGENT", "TMUX"):
            self.assertIn(name, stripped)

    def test_unrelated_variables_are_untouched(self):
        stripped = inherited_session_vars(self.PARENT_ENV)
        self.assertNotIn("PATH", stripped)
        self.assertNotIn("HOME", stripped)

    def test_config_dir_is_preserved(self):
        """It names a directory the user chose, not a session."""
        self.assertNotIn("CLAUDE_CONFIG_DIR", inherited_session_vars(self.PARENT_ENV))

    def test_command_is_wrapped_in_env_unset(self):
        argv = sanitize(["claude", "--flag"], self.PARENT_ENV)
        self.assertEqual(argv[0], "env")
        self.assertIn("-u", argv)
        self.assertIn("CLAUDE_CODE_SESSION_ID", argv)
        # The command and its arguments survive, in order, at the end.
        self.assertEqual(argv[-2:], ["claude", "--flag"])

    def test_a_clean_environment_is_left_alone(self):
        argv = sanitize(["claude"], {"PATH": "/usr/bin"})
        self.assertEqual(argv, ["claude"])


class LaunchCommandTest(unittest.TestCase):
    """Assert on the command actually handed to tmux.

    Testing sanitize() alone is not enough: it passes even if main() forgets to
    call it. This asserts the wiring, which is where the bug actually was.
    """

    def _launch(self, extra_env):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return types.SimpleNamespace(returncode=0, stderr="", stdout="")

        with mock.patch.object(runner.subprocess, "run", fake_run), \
             mock.patch.object(runner.tmux, "available", lambda: True), \
             mock.patch.object(runner.tmux, "has_session", lambda name: False), \
             mock.patch.dict(os.environ, extra_env, clear=False):
            code = runner.main(["--detached", "--name", "probe", "--", "claude", "--x"])
        return code, captured.get("argv", [])

    def test_inherited_identity_is_cleared_before_exec(self):
        code, argv = self._launch({
            "CLAUDE_CODE_SESSION_ID": "abc",
            "CLAUDE_CODE_MESSAGING_TOKEN": "secret",
        })
        self.assertEqual(code, 0)
        self.assertIn("env", argv)
        self.assertIn("CLAUDE_CODE_SESSION_ID", argv)
        self.assertIn("CLAUDE_CODE_MESSAGING_TOKEN", argv)
        self.assertEqual(argv[-2:], ["claude", "--x"])

    def test_the_secret_value_is_never_placed_on_the_command_line(self):
        """`env -u NAME` unsets by name; the value must not appear anywhere."""
        _, argv = self._launch({"CLAUDE_CODE_MESSAGING_TOKEN": "super-secret-value"})
        self.assertNotIn("super-secret-value", " ".join(argv))

    def test_it_is_still_a_tmux_new_session(self):
        _, argv = self._launch({})
        self.assertEqual(argv[:4], ["tmux", "new-session", "-d", "-s"])


class ConcurrentLaunchTest(unittest.TestCase):
    """Two hubs can launch at the same moment. Asking tmux whether a name is free
    and then taking it are two steps, and the answer can go stale in between."""

    def test_a_duplicate_name_is_retried_rather_than_raised(self):
        attempts = []

        def fake_run(argv, **kwargs):
            attempts.append(argv[argv.index("-s") + 1])
            if len(attempts) == 1:
                return mock.Mock(returncode=1, stderr="duplicate session: agentview_x")
            return mock.Mock(returncode=0, stderr="")

        with mock.patch("agentview.runner.tmux.available", return_value=True), \
                mock.patch("agentview.runner.tmux.has_session", return_value=True), \
                mock.patch("agentview.runner.subprocess.run", side_effect=fake_run):
            session = runner.launch_detached(["sh"], "x")

        self.assertEqual(len(attempts), 2)
        self.assertNotEqual(attempts[0], attempts[1])  # a fresh name, not the same one
        self.assertEqual(session, attempts[1])

    def test_any_other_tmux_failure_is_surfaced_immediately(self):
        """Retrying a real error would turn one clear failure into five slow ones."""
        with mock.patch("agentview.runner.tmux.available", return_value=True), \
                mock.patch("agentview.runner.tmux.has_session", return_value=False), \
                mock.patch("agentview.runner.subprocess.run",
                           return_value=mock.Mock(returncode=1, stderr="no server running")):
            with self.assertRaises(RuntimeError) as caught:
                runner.launch_detached(["sh"], "x")
        self.assertIn("no server", str(caught.exception))

    def test_names_do_not_collide_when_generated_in_the_same_second(self):
        with mock.patch("agentview.runner.tmux.has_session", return_value=True):
            names = {runner.unique_session_name("x") for _ in range(50)}
        self.assertEqual(len(names), 50)
