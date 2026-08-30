"""Profiles isolate deployed code while deliberately sharing user state."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from agentview.hub import containers, remotes
from agentview.hub.overrides import Overrides
from agentview.hub.runtime import PROFILE_PORTS, instance_id
from agentview.hub.server import build_parser, run_paths


class ProfileTest(unittest.TestCase):
    def test_profile_ports_are_stable(self):
        self.assertEqual(PROFILE_PORTS, {"stable": 7788, "preview": 7789})

    def test_checkout_and_port_both_contribute_to_instance_id(self):
        one = instance_id("preview", 7789, Path("/tmp/one"), "host")
        self.assertNotEqual(one, instance_id("preview", 7790, Path("/tmp/one"), "host"))
        self.assertNotEqual(one, instance_id("preview", 7789, Path("/tmp/two"), "host"))
        self.assertEqual(one, instance_id("preview", 7789, Path("/tmp/one"), "host"))

    def test_deployments_never_use_the_legacy_global_paths(self):
        self.assertEqual(remotes.code_dir("abc"), "~/.agentview/code/abc")
        self.assertEqual(containers.code_dir("abc"), "/tmp/.agentview-code-abc")

    def test_daemon_mode_is_available_for_agent_launched_hubs(self):
        args = build_parser().parse_args(["--profile", "preview", "--daemon"])
        self.assertTrue(args.daemon)
        with mock.patch.dict("os.environ", {"AGENTVIEW_HOME": "/tmp/agentview-test"}):
            pid, log = run_paths("preview-id")
        self.assertEqual(str(pid), "/tmp/agentview-test/run/preview-id.pid")
        self.assertEqual(str(log), "/tmp/agentview-test/run/preview-id.log")


class SharedStateTest(unittest.TestCase):
    def test_external_override_changes_reload_and_unrelated_edits_survive(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "names.json"
            stable, preview = Overrides(path), Overrides(path)
            stable.set_name("a", "from stable")
            preview.set_colour("b", "blue")
            self.assertEqual(stable.get("b").get("color"), "blue")
            self.assertEqual(preview.get("a").get("name"), "from stable")

    def test_remote_updates_are_locked_read_modify_write(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "remotes.json"
            with mock.patch("agentview.hub.remotes.config_path", return_value=path):
                remotes.save_remotes(["box"])
                remotes.save_remotes(["devbox"])
                self.assertEqual(remotes.load_remotes(), ["box", "devbox"])


class DeploymentWiringTest(unittest.TestCase):
    def test_ssh_sync_uses_the_instance_directory(self):
        host = mock.Mock()
        host.run_input.return_value = (0, "")
        with mock.patch("agentview.hub.remotes.SshHost", return_value=host):
            remotes.sync_code("box", code_dir=remotes.code_dir("preview-id"))
        command = host.run_input.call_args.args[0]
        self.assertIn("~/.agentview/code/preview-id", command)

    def test_container_sync_uses_the_instance_directory(self):
        host = mock.Mock()
        host.run_input.return_value = (0, "")
        containers.sync_collector(
            host, "dev", b"bundle", code_dir=containers.code_dir("preview-id")
        )
        command = host.run_input.call_args.args[0]
        self.assertIn("/tmp/.agentview-code-preview-id", command)


if __name__ == "__main__":
    unittest.main()
