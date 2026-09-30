import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hostdelta.telemetry import normalize


class StdlibLoggingExampleTests(unittest.TestCase):
    def test_example_runs_and_emits_valid_json(self):
        root = Path(__file__).resolve().parents[1]

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            home = temp_root / "home"
            home.mkdir()

            env = os.environ.copy()
            env["PYTHONPATH"] = str(root / "src")
            env["HOME"] = str(home)
            env["USERPROFILE"] = str(home)
            env["XDG_STATE_HOME"] = str(temp_root / "xdg-state")
            env["HOSTDELTA_STATE_DIR"] = str(temp_root / "hostdelta-state")

            result = subprocess.run(
                [sys.executable, str(root / "examples" / "stdlib_logging.py")],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=root,
                env=env,
            )

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stderr, "")

            lines = [line for line in result.stdout.splitlines() if line.strip()]
            self.assertEqual(len(lines), 1)

            record = json.loads(lines[0])

            self.assertEqual(
                record["event"]["name"],
                "inventory.stock.checked",
            )
            self.assertEqual(
                record["service"]["name"],
                "inventory-worker",
            )
            self.assertEqual(
                record["attributes"]["sku"],
                "demo-widget",
            )
            self.assertEqual(
                record["attributes"]["available"],
                12,
            )

            normalized = normalize(record)

            self.assertEqual(
                normalized["event_name"],
                "inventory.stock.checked",
            )
            self.assertEqual(
                normalized["service"],
                "inventory-worker",
            )

            self.assertFalse(
                (temp_root / "hostdelta-state").exists()
            )
