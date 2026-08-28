"""Agents running inside containers.

No docker here: what is worth pinning down is the shape of the commands and how a
container snapshot is nested and rewritten. The behaviour that can only be learned
from a real host -- that a container bind-mounting the host's ~/.claude does not
double-report -- is noted where it matters and covered by the collector's own
liveness tests.
"""

from __future__ import annotations

import unittest
from unittest import mock

from agentview.hub import containers
from agentview.hub.hosts import LocalHost, SshHost


class ListContainersTest(unittest.TestCase):
    def test_docker_ps_output_is_parsed(self):
        out = "abc123\tmlykes-web-dev\tnginx:alpine\ndef456\tdb\tpostgres:14\n"
        host = mock.Mock()
        host.run.return_value = (0, out, "")
        found, err = containers.list_containers(host)
        self.assertIsNone(err)
        self.assertEqual([c["name"] for c in found], ["mlykes-web-dev", "db"])

    def test_a_failure_is_reported_not_raised(self):
        host = mock.Mock()
        host.run.return_value = (1, "", "Cannot connect to the Docker daemon\n")
        found, err = containers.list_containers(host)
        self.assertEqual(found, [])
        self.assertIn("Docker daemon", err)

    def test_a_container_with_no_python_is_reported_as_such(self):
        """Most images on a box are services -- nginx, postgres -- with no
        interpreter to run the collector."""
        host = mock.Mock()
        host.run.return_value = (1, "", "")
        self.assertIsNone(containers.python_in(host, "abc"))

    def test_python_is_found_when_present(self):
        host = mock.Mock()
        host.run.return_value = (0, "/usr/bin/python3\n", "")
        self.assertEqual(containers.python_in(host, "abc"), "/usr/bin/python3")


class AttachArgvTest(unittest.TestCase):
    def test_a_local_container_is_reached_with_docker_exec(self):
        argv = containers.attach_argv(LocalHost(), "abc123", "s1")
        self.assertEqual(argv, ["docker", "exec", "-it", "abc123", "tmux", "attach", "-t", "s1"])

    def test_a_remote_container_adds_exactly_one_more_wrapper(self):
        """Attach is just an argv: a container on another machine is ssh around
        docker around tmux, not a separate code path."""
        argv = containers.attach_argv(SshHost("pronto_server"), "abc123", "s1")
        self.assertEqual(argv[0], "ssh")
        self.assertIn("pronto_server", argv)
        self.assertIn("docker exec -it abc123 tmux attach -t s1", argv[-1])

    def test_the_read_only_variant_passes_minus_r(self):
        argv = containers.attach_argv(LocalHost(), "abc", "s1", readonly=True)
        self.assertIn("-r", argv)


class RewriteTest(unittest.TestCase):
    def _snapshot(self, attach, extra=None):
        return {
            "context": {"id": "ctr-1", "kind": "container",
                        "label": "devcontainer: Opetopic"},
            "agents": [{"id": "a:1", "name": "one", "attach": attach,
                        "extra": extra or {}}],
            "warnings": [], "collected_at": 1.0,
        }

    def test_the_container_nests_under_its_machine(self):
        snap = containers.rewrite(
            self._snapshot({"available": False}), LocalHost(), "abc", "host-1"
        )
        self.assertEqual(snap["context"]["parent_id"], "host-1")

    def test_a_tmux_agent_becomes_reachable_through_docker(self):
        snap = containers.rewrite(
            self._snapshot({"available": True, "argv": ["tmux", "attach", "-t", "s1"]},
                           {"tmux_session": "s1"}),
            LocalHost(), "abc", "host-1",
        )
        self.assertEqual(snap["agents"][0]["attach"]["argv"][:3], ["docker", "exec", "-it"])

    def test_the_inner_argv_is_never_left_in_place(self):
        """`tmux attach -t s1` is correct inside the container and reaches nothing
        outside it -- the failure would look like an empty terminal."""
        snap = containers.rewrite(
            self._snapshot({"available": True, "argv": ["tmux", "attach", "-t", "s1"]},
                           {"tmux_session": "s1"}),
            LocalHost(), "abc", "host-1",
        )
        self.assertNotEqual(snap["agents"][0]["attach"]["argv"][0], "tmux")

    def test_a_background_agent_inside_a_container_is_not_offered(self):
        """Its `claude attach` talks to a unix socket in there; run out here it would
        target a job id that does not exist on this side."""
        snap = containers.rewrite(
            self._snapshot({"available": True, "argv": ["claude", "attach", "x"]},
                           {"job_id": "x"}),
            LocalHost(), "abc", "host-1",
        )
        attach = snap["agents"][0]["attach"]
        self.assertFalse(attach["available"])
        self.assertIn("container", attach["reason"])

    def test_agents_carry_their_container(self):
        snap = containers.rewrite(
            self._snapshot({"available": False}), LocalHost(), "abc", "host-1"
        )
        self.assertEqual(snap["agents"][0]["container_id"], "abc")

    def test_a_container_on_a_remote_is_tagged_with_both(self):
        snap = containers.rewrite(
            self._snapshot({"available": False}), SshHost("pronto_server"), "abc", "host-1"
        )
        self.assertTrue(snap["context"]["via_ssh"])
        self.assertEqual(snap["context"]["ssh_host"], "pronto_server")
        self.assertEqual(snap["agents"][0]["ssh_host"], "pronto_server")


class HostExecutorTest(unittest.TestCase):
    def test_local_commands_go_through_a_login_shell(self):
        """Matched to the ssh path deliberately: agent CLIs live in ~/.local/bin, and
        the two should not resolve commands differently."""
        self.assertEqual(LocalHost().argv("true")[:2], ["bash", "-lc"])

    def test_ssh_commands_are_one_argument(self):
        argv = SshHost("h").argv("echo 'a b'")
        self.assertEqual(argv[-2], "h")
        self.assertTrue(argv[-1].startswith("bash -lc "))

    def test_wrapping_an_argv_quotes_each_part(self):
        argv = SshHost("h").wrap(["tmux", "attach", "-t", "a b"])
        self.assertIn("'a b'", argv[-1])

    def test_a_local_host_is_not_remote(self):
        self.assertIsNone(LocalHost().ssh_host)
        self.assertEqual(SshHost("h").ssh_host, "h")


if __name__ == "__main__":
    unittest.main()
