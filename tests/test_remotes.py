"""Machines reached over SSH.

No network here: what is worth pinning down is the shape of the commands and how a
remote snapshot is rewritten, both of which are pure. The one thing that can only be
learned from a real host -- that agent CLIs live outside the non-login PATH -- is
encoded in the login-shell tests below.
"""

from __future__ import annotations

import io
import json
import tarfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from agentview.hub import remotes


class LoginShellTest(unittest.TestCase):
    """Every remote command runs through a login shell.

    Not a detail: on a stock Debian box SSH hands out
    `/usr/local/bin:/usr/bin:/bin:/usr/games`, while Claude Code installs to
    `~/.local/bin`. Without this, a working install probes as missing, the launch
    menu comes back empty, and the host looks unsupported.
    """

    def test_commands_are_wrapped(self):
        self.assertEqual(
            remotes.login_shell_command("command -v claude"),
            "bash -lc 'command -v claude'",
        )

    def test_the_command_reaches_the_shell_as_one_argument(self):
        """ssh joins its arguments and hands them to a shell, so the payload has to
        survive one round of splitting intact. This does not neutralise shell
        metacharacters -- it is a shell command by design -- which is why callers
        quote their own inputs before they get here (see LaunchTest)."""
        import shlex

        wrapped = remotes.login_shell_command("echo 'a b' && ls")
        parts = shlex.split(wrapped)
        self.assertEqual(parts[:2], ["bash", "-lc"])
        self.assertEqual(parts[2], "echo 'a b' && ls")
        self.assertEqual(len(parts), 3)

    def test_ssh_never_prompts(self):
        """A hub in the background cannot answer a password or host-key question, so
        a misconfigured host has to fail rather than hang."""
        argv = remotes.ssh_argv("h", "true")
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("ConnectTimeout=10", argv)

    def test_a_tty_is_requested_only_when_asked(self):
        self.assertNotIn("-t", remotes.ssh_argv("h", "true"))
        self.assertIn("-t", remotes.ssh_argv("h", "true", tty=True))


class PackageTarTest(unittest.TestCase):
    def setUp(self):
        self.names = tarfile.open(
            fileobj=io.BytesIO(remotes.package_tar())
        ).getnames()

    def test_the_collector_is_included(self):
        self.assertIn("agentview/collector/core.py", self.names)
        self.assertIn("agentview/collector/adapters/claude_code.py", self.names)

    def test_the_hub_is_not_shipped(self):
        """Only the collector runs over there; the hub's modules and vendored web
        assets would multiply the bytes crossing a slow link for nothing."""
        self.assertFalse([n for n in self.names if n.startswith("agentview/hub/")])

    def test_it_is_small_enough_to_send_every_time(self):
        self.assertLess(len(remotes.package_tar()), 200 * 1024)


class AttachRewriteTest(unittest.TestCase):
    """A remote snapshot describes commands for *its* machine."""

    def _snapshot(self, attach, extra=None):
        return {
            "context": {"id": "ctx-remote", "label": "SPU5-1-2-7-61358",
                        "hostname": "SPU5-1-2-7-61358"},
            "agents": [{"id": "a:1", "name": "one", "attach": attach,
                        "extra": extra or {}}],
            "warnings": [],
            "collected_at": 1.0,
        }

    def test_a_tmux_agent_is_reached_through_ssh(self):
        snap = remotes.rewrite_for_ssh(
            self._snapshot(
                {"available": True, "argv": ["tmux", "attach", "-t", "s1"]},
                {"tmux_session": "s1"},
            ),
            "pronto_server",
        )
        argv = snap["agents"][0]["attach"]["argv"]
        self.assertEqual(argv[0], "ssh")
        self.assertIn("pronto_server", argv)
        self.assertEqual(argv[-1], "tmux attach -t s1")

    def test_the_local_argv_is_never_left_in_place(self):
        """Running the collector's own argv here would attach to this machine, or to
        nothing -- the failure would look like an empty terminal."""
        snap = remotes.rewrite_for_ssh(
            self._snapshot(
                {"available": True, "argv": ["tmux", "attach", "-t", "s1"]},
                {"tmux_session": "s1"},
            ),
            "pronto_server",
        )
        self.assertNotEqual(snap["agents"][0]["attach"]["argv"][0], "tmux")

    def test_an_agent_with_no_tmux_session_is_marked_unreachable(self):
        """A background agent's `claude attach` works on its own box only. Offering
        it here would run the command locally against a job id that is not ours."""
        snap = remotes.rewrite_for_ssh(
            self._snapshot(
                {"available": True, "argv": ["claude", "attach", "abc"]}, {"job_id": "abc"}
            ),
            "pronto_server",
        )
        attach = snap["agents"][0]["attach"]
        self.assertFalse(attach["available"])
        self.assertIn("ssh", attach["reason"])

    def test_an_unattachable_agent_keeps_its_reason(self):
        snap = remotes.rewrite_for_ssh(
            self._snapshot({"available": False, "reason": "started outside tmux"}),
            "pronto_server",
        )
        self.assertEqual(snap["agents"][0]["attach"]["reason"], "started outside tmux")

    def test_agents_are_tagged_with_their_host(self):
        snap = remotes.rewrite_for_ssh(self._snapshot({"available": False}), "pronto_server")
        self.assertEqual(snap["agents"][0]["ssh_host"], "pronto_server")

    def test_the_context_is_labelled_with_the_name_you_type(self):
        """A work machine's own hostname is often an inventory code you would not
        recognise; the ssh alias is what you called it."""
        snap = remotes.rewrite_for_ssh(self._snapshot({"available": False}), "pronto_server")
        self.assertEqual(snap["context"]["label"], "pronto_server")
        self.assertEqual(snap["context"]["hostname"], "SPU5-1-2-7-61358")
        self.assertTrue(snap["context"]["via_ssh"])


class RemotesConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(
            "os.environ", {"AGENTVIEW_HOME": self.tmp.name}
        )
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_no_config_is_not_an_error(self):
        self.assertEqual(remotes.load_remotes(), [])

    def test_hosts_round_trip(self):
        remotes.save_remotes(["pronto_server"])
        self.assertEqual(remotes.load_remotes(), ["pronto_server"])

    def test_command_line_hosts_are_added(self):
        remotes.save_remotes(["a"])
        self.assertEqual(remotes.load_remotes(["b"]), ["a", "b"])

    def test_duplicates_collapse(self):
        remotes.save_remotes(["a"])
        self.assertEqual(remotes.load_remotes(["a"]), ["a"])

    def test_a_corrupt_config_loses_hosts_but_not_the_hub(self):
        Path(self.tmp.name, "remotes.json").write_text("{not json")
        self.assertEqual(remotes.load_remotes(["a"]), ["a"])

    def test_an_object_form_is_accepted_too(self):
        Path(self.tmp.name, "remotes.json").write_text(json.dumps({"hosts": ["a"]}))
        self.assertEqual(remotes.load_remotes(), ["a"])


class CollectFailureTest(unittest.TestCase):
    """A remote that misbehaves must report, not raise."""

    def test_a_non_zero_exit_is_reported(self):
        with mock.patch.object(remotes, "run", return_value=(1, "", "boom\n")):
            snap, err = remotes.collect_once("h")
        self.assertIsNone(snap)
        self.assertEqual(err, "boom")

    def test_output_that_is_not_json_is_reported(self):
        with mock.patch.object(remotes, "run", return_value=(0, "not json", "")):
            snap, err = remotes.collect_once("h")
        self.assertIsNone(snap)
        self.assertIn("JSON", err)

    def test_a_good_snapshot_comes_back(self):
        payload = json.dumps({"context": {"id": "x"}, "agents": []})
        with mock.patch.object(remotes, "run", return_value=(0, payload, "")):
            snap, err = remotes.collect_once("h")
        self.assertIsNone(err)
        self.assertEqual(snap["context"]["id"], "x")


class RemoteHarnessProbeTest(unittest.TestCase):
    def test_installed_commands_are_parsed(self):
        out = "claude=/home/mlykes/.local/bin/claude\n"
        with mock.patch.object(remotes, "run", return_value=(0, out, "")):
            found, err = remotes.remote_harnesses("h", ["claude", "opencode"])
        self.assertIsNone(err)
        self.assertEqual(found, {"claude": "/home/mlykes/.local/bin/claude"})

    def test_nothing_installed_is_not_an_error(self):
        """`command -v` exits non-zero when the last probe misses, which says
        nothing about whether the probe itself worked."""
        with mock.patch.object(remotes, "run", return_value=(1, "", "")):
            found, err = remotes.remote_harnesses("h", ["claude"])
        self.assertEqual(found, {})
        self.assertIsNone(err)


class LaunchTest(unittest.TestCase):
    def test_the_session_name_is_quoted(self):
        with mock.patch.object(remotes, "run", return_value=(0, "", "")) as run:
            remotes.launch("h", "/bin/claude", "agentview_a b")
        command = run.call_args[0][1]
        self.assertIn("'agentview_a b'", command)
        self.assertIn("tmux new-session -d", command)

    def test_a_failure_is_reported(self):
        with mock.patch.object(remotes, "run", return_value=(1, "", "duplicate session\n")):
            self.assertEqual(remotes.launch("h", "c", "s"), "duplicate session")


if __name__ == "__main__":
    unittest.main()
