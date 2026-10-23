"""Help stays usable before initialization and from outside the checkout."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class HelpBeforeInitializationTests(unittest.TestCase):
    def test_help_contract_without_state(self):
        source = Path(__file__).resolve().parents[1] / "src"
        cases = (
            ((), ("--state-dir", "init", "config", "collect", "brief", "ack")),
            (("init",), ("--watch", "--access-log")),
            (("config",), ("--check",)),
            (("collect",), ("--config",)),
            (("brief",), ("--archive", "--consumer", "--since")),
            (("ack",), ("--consumer", "--until")),
        )
        for command, expected_options in cases:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                home = root / "home"
                home.mkdir()
                working = root / "working"
                working.mkdir()
                state = root / "uninitialized-state"
                env = {
                    "HOME": str(home),
                    "USERPROFILE": str(home),
                    "HOSTDELTA_STATE_DIR": str(state),
                    "PYTHONPATH": str(source),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PATH": os.defpath,
                }
                result = subprocess.run(
                    [sys.executable, "-m", "hostdelta", *command, "--help"],
                    cwd=working,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertIn("usage: hostdelta", result.stdout)
                if command:
                    self.assertIn("hostdelta " + command[0], result.stdout)
                for option in expected_options:
                    self.assertIn(option, result.stdout)
                self.assertFalse(state.exists(), "Help must not initialize state")
                self.assertEqual(list(home.iterdir()), [], "Help must not write into HOME")
                self.assertEqual(list(working.iterdir()), [], "Help must not write into cwd")


if __name__ == "__main__":
    unittest.main()
