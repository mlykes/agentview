"""Deciding whether a recorded pid is still the agent that recorded it.

The registry outlives the process, so every session file has to be checked against
the live process table or the HUD reports agents that exited days ago. The check has
to survive both shapes of `ps -o comm=`, which differ by platform.
"""

from __future__ import annotations

import unittest
from unittest import mock

from agentview.collector.procs import pid_matches

#: What Claude Code's background agents actually exec: a version-named binary under
#: the install directory. macOS `ps -o comm=` prints the whole path, Linux prints
#: only the basename.
MACOS_COMM = "/home/mlykes/.local/share/claude/versions/2.1.251"
LINUX_COMM = "2.1.251"
ARGV = ("/home/mlykes/.local/share/claude/versions/2.1.251 "
        "--session-id a77efdfa-2aad-4a0e-b4a7-a83b86a50e85 --agent claude")


class PidMatchesTest(unittest.TestCase):
    def setUp(self):
        alive = mock.patch("agentview.collector.procs.pid_alive", return_value=True)
        alive.start()
        self.addCleanup(alive.stop)

    def test_a_dead_pid_never_matches(self):
        with mock.patch("agentview.collector.procs.pid_alive", return_value=False):
            self.assertFalse(pid_matches(1, "claude", {1: "claude"}))

    def test_an_executable_name_carrying_the_harness_is_enough(self):
        self.assertTrue(pid_matches(1, "claude", {1: MACOS_COMM}))

    def test_a_pid_missing_from_a_populated_table_is_gone(self):
        self.assertFalse(pid_matches(1, "claude", {2: "claude"}))

    def test_no_table_falls_back_to_bare_liveness(self):
        """Better to trust liveness than to report every agent dead because `ps`
        failed."""
        self.assertTrue(pid_matches(1, "claude", {}))

    def test_a_linux_background_agent_is_recognised_by_its_install_path(self):
        """The regression: on Linux `comm` is the basename -- "2.1.251" -- which
        carries no trace of the harness, so every background agent on a Linux host
        was dropped as a ghost."""
        self.assertFalse(pid_matches(1, "claude", {1: LINUX_COMM}))
        self.assertTrue(pid_matches(1, "claude", {1: LINUX_COMM}, {1: ARGV}))

    def test_only_argv0_is_consulted_not_the_whole_command_line(self):
        """`--agent claude` appears in the background agent's own arguments, so
        matching the full command line would let anything merely mentioning the
        harness impersonate it."""
        self.assertFalse(
            pid_matches(1, "claude", {1: "grep"}, {1: "grep -r claude /etc"})
        )
        self.assertFalse(
            pid_matches(1, "claude", {1: "vim"}, {1: "vim /home/me/.claude/settings.json"})
        )

    def test_an_unrelated_process_is_still_rejected_with_argv_available(self):
        self.assertFalse(pid_matches(1, "claude", {1: "sshd"}, {1: "sshd: mlykes@pts/0"}))


if __name__ == "__main__":
    unittest.main()
