"""Two hubs on one machine: isolated deployments, deliberately shared state.

A stable hub and a preview hub run side by side so a branch can be looked at
without losing sight of what is actually running. That only works if the code
they deploy to remotes is kept apart while the labels and colours you set stay
common to both.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from agentview.hub import containers, remotes
from agentview.hub.overrides import Overrides
from agentview.hub.runtime import PROFILE_PORTS, deployment_dir, instance_id
from agentview.hub.server import build_parser, run_paths


class ProfileTest(unittest.TestCase):
    def test_profile_ports_are_stable(self):
        self.assertEqual(PROFILE_PORTS, {"stable": 7788, "preview": 7789})

    def test_the_port_follows_the_profile_unless_overridden(self):
        self.assertIsNone(build_parser().parse_args([]).port)
        self.assertEqual(build_parser().parse_args(["--port", "9000"]).port, 9000)

    def test_checkout_and_port_both_contribute_to_instance_id(self):
        one = instance_id("preview", 7789, Path("/tmp/one"), "host")
        self.assertNotEqual(one, instance_id("preview", 7790, Path("/tmp/one"), "host"))
        self.assertNotEqual(one, instance_id("preview", 7789, Path("/tmp/two"), "host"))
        self.assertEqual(one, instance_id("preview", 7789, Path("/tmp/one"), "host"))

    def test_daemon_mode_is_available_for_agent_launched_hubs(self):
        args = build_parser().parse_args(["--profile", "preview", "--daemon"])
        self.assertTrue(args.daemon)
        with mock.patch.dict("os.environ", {"AGENTVIEW_HOME": "/tmp/agentview-test"}):
            pid, log = run_paths("preview-id")
        self.assertEqual(str(pid), "/tmp/agentview-test/run/preview-id.pid")
        self.assertEqual(str(log), "/tmp/agentview-test/run/preview-id.log")


class DeploymentIsolationTest(unittest.TestCase):
    """The failure this prevents is silent: two hubs unpacking different versions
    of the collector into one directory, so whichever synced last decides what
    *both* of them report. It reads as a code bug on one of the hubs."""

    def test_each_instance_gets_its_own_directory(self):
        self.assertEqual(deployment_dir("ssh", "abc"), "~/.agentview/code/abc")
        self.assertEqual(deployment_dir("container", "abc"), "/tmp/.agentview-code-abc")
        self.assertNotEqual(deployment_dir("ssh", "abc"), deployment_dir("ssh", "xyz"))

    def test_an_ssh_sync_and_collect_both_use_the_instance_directory(self):
        with mock.patch.object(remotes.SshHost, "run_input", return_value=(0, "")) as sync, \
                mock.patch.object(remotes, "run", return_value=(0, "{}", "")) as collect:
            remotes.sync_code("box", instance="preview-id")
            remotes.collect_once("box", instance="preview-id")
        self.assertIn("~/.agentview/code/preview-id", sync.call_args[0][0])
        self.assertIn("~/.agentview/code/preview-id", collect.call_args[0][1])

    def test_a_container_sync_and_collect_both_use_the_instance_directory(self):
        host = mock.Mock()
        host.run.return_value = (0, "{}", "")
        host.run_input.return_value = (0, "")
        containers.sync_collector(host, "abc", b"", instance="preview-id")
        containers.collect_once(host, "abc", "python3", instance="preview-id")
        self.assertIn("/tmp/.agentview-code-preview-id", host.run_input.call_args[0][0])
        self.assertIn("/tmp/.agentview-code-preview-id", host.run.call_args[0][0])

    def test_without_an_instance_the_paths_still_do_not_collide_with_a_named_one(self):
        self.assertNotEqual(remotes.code_dir(None), remotes.code_dir("preview-id"))
        self.assertNotEqual(containers.code_dir(None), containers.code_dir("preview-id"))


class SharedStateTest(unittest.TestCase):
    """Labels and colours are the user's, not a build's: renaming a row in one hub
    must show up in the other. Both processes read and write one file, so the
    thread lock inside a process is not enough on its own."""

    def test_an_edit_in_one_hub_is_visible_in_the_other(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "names.json"
            stable, preview = Overrides(path), Overrides(path)
            stable.set_name("a", "from stable")
            preview.set_colour("b", "blue")
            self.assertEqual(preview.get("a")["name"], "from stable")
            self.assertEqual(stable.get("b")["color"], "blue")

    def test_neither_hub_drops_what_the_other_wrote(self):
        """The regression that matters: a write that does not re-read first
        serialises its own stale copy and silently deletes the other's edits."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "names.json"
            stable, preview = Overrides(path), Overrides(path)
            for i in range(5):
                stable.set_name("stable-{}".format(i), "s{}".format(i))
                preview.set_name("preview-{}".format(i), "p{}".format(i))
            final = Overrides(path).all()
            for i in range(5):
                self.assertIn("stable-{}".format(i), final)
                self.assertIn("preview-{}".format(i), final)


if __name__ == "__main__":
    unittest.main()
