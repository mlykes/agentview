"""The ways you can actually invoke agentview.

The console script in pyproject.toml only exists after `pip install`, and this
project's whole premise is that you do not need one. The README used to document
`agentview ...` with nothing providing it, so a fresh clone hit "command not found"
on the first instruction. These pin the invocations that work from a bare checkout.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ModuleEntryPointTest(unittest.TestCase):
    def test_python_dash_m_agentview_works(self):
        result = subprocess.run(
            [sys.executable, "-m", "agentview"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("agentview", result.stdout)

    def test_it_works_from_any_directory(self):
        """PYTHONPATH, not the working directory, is what makes this importable."""
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        result = subprocess.run(
            [sys.executable, "-m", "agentview", "collect", "--once"],
            cwd=str(Path(ROOT).anchor), capture_output=True, text=True,
            timeout=60, env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"agents"', result.stdout)


class LauncherScriptTest(unittest.TestCase):
    SCRIPT = ROOT / "bin" / "agentview"

    def test_launcher_exists_and_is_executable(self):
        self.assertTrue(self.SCRIPT.exists(), "bin/agentview is missing")
        self.assertTrue(os.access(str(self.SCRIPT), os.X_OK), "bin/agentview is not executable")

    @unittest.skipIf(os.name == "nt", "POSIX shell script")
    def test_launcher_runs_from_another_directory(self):
        result = subprocess.run(
            [str(self.SCRIPT)],
            cwd=str(Path(ROOT).anchor), capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("agentview", result.stdout)

    @unittest.skipIf(os.name == "nt", "POSIX shell script")
    def test_launcher_works_through_a_symlink(self):
        """It must resolve its own path, since the documented setup is a symlink
        onto PATH -- naive $0 handling would look for the repo next to the link."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "agentview"
            link.symlink_to(self.SCRIPT)
            result = subprocess.run(
                [str(link)], cwd=tmp, capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("agentview", result.stdout)


if __name__ == "__main__":
    unittest.main()
