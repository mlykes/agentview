"""Profiles isolate deployed code while deliberately sharing user state."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from agentview.hub.overrides import Overrides
from agentview.hub.runtime import PROFILE_PORTS, instance_id
from agentview.hub.server import build_parser, run_paths
from agentview.hub.topology import RemoteStore, TopologyPoller, deployment_dir


class ProfileTest(unittest.TestCase):
    def test_profile_ports_are_stable(self):
        self.assertEqual(PROFILE_PORTS, {"stable": 7788, "preview": 7789})

    def test_checkout_and_port_both_contribute_to_instance_id(self):
        one = instance_id("preview", 7789, Path("/tmp/one"), "host")
        self.assertNotEqual(one, instance_id("preview", 7790, Path("/tmp/one"), "host"))
        self.assertNotEqual(one, instance_id("preview", 7789, Path("/tmp/two"), "host"))
        self.assertEqual(one, instance_id("preview", 7789, Path("/tmp/one"), "host"))

    def test_deployments_never_use_the_legacy_global_paths(self):
        self.assertEqual(deployment_dir("ssh", "abc"), "~/.agentview/code/abc")
        self.assertEqual(deployment_dir("container", "abc"), "/tmp/.agentview-code-abc")

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
            self.assertEqual(stable.get("b"), {"color": "blue"})
            self.assertEqual(preview.get("a"), {"name": "from stable"})

    def test_remote_updates_are_locked_read_modify_write(self):
        with TemporaryDirectory() as tmp:
            store = RemoteStore(Path(tmp) / "remotes.json")
            store.update(lambda data: dict(data, ssh=["box"]))
            store.update(lambda data: dict(data, containers=["dev"]))
            self.assertEqual(store.read()["ssh"], [{"host": "box"}])
            self.assertEqual(store.read()["containers"], [{"name": "dev"}])


class DeploymentWiringTest(unittest.TestCase):
    def test_ssh_and_container_commands_carry_the_instance_directory(self):
        registry = mock.Mock()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "agentview"
            package.mkdir()
            (package / "__init__.py").write_text("VERSION = 'preview'\n")
            poller = TopologyPoller(registry, RemoteStore(root / "r.json"), root, "preview-id")
            completed = mock.Mock(returncode=0, stdout=json.dumps({"context": {"id": "x"}}))
            with mock.patch("agentview.hub.topology.subprocess.run", return_value=completed) as run:
                poller.sync_and_collect_ssh({"host": "box"})
                ssh_calls = [call.args[0] for call in run.call_args_list]
            self.assertTrue(all("~/.agentview/code/preview-id" in " ".join(c)
                                for c in ssh_calls))

            with mock.patch("agentview.hub.topology.subprocess.run", return_value=completed) as run:
                poller.sync_and_collect_container({"name": "dev"})
                container_calls = [call.args[0] for call in run.call_args_list]
            self.assertTrue(all("/tmp/.agentview-code-preview-id" in " ".join(c)
                                for c in container_calls))


if __name__ == "__main__":
    unittest.main()
