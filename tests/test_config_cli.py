"""The pre-initialization config command keeps its JSON CLI contract."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ConfigCLITests(unittest.TestCase):
    def invoke(self, root, config_path):
        home = root / "home"
        home.mkdir()
        state = root / "uninitialized-state"
        xdg_state = root / "xdg-state"
        env = {
            **os.environ,
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_STATE_HOME": str(xdg_state),
            "HOSTDELTA_STATE_DIR": str(root / "environment-state"),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        result = subprocess.run(
            [sys.executable, "-m", "hostdelta", "--state-dir", str(state),
             "config", "--check", str(config_path), "--json"],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        for path in (state, xdg_state, root / "environment-state"):
            self.assertFalse(path.exists(), f"Config validation created state at {path}")
        self.assertEqual(list(home.iterdir()), [])
        return result

    def test_invalid_config_is_json_error_without_initialization(self):
        secret = "synthetic-secret-do-not-echo-43"
        cases = [
            ("missing", None, "missing.json"),
            ("malformed", '{"value": "' + secret + '",}', "line"),
            ("oversized", " " * (128 * 1024 + 1), "128 KiB"),
            ("version", '{"version": 2}', "version"),
            ("unknown", '{"unexpected": true}', "configuration"),
        ]
        for name, content, context in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config_path = root / "missing.json"
                if content is not None:
                    config_path.write_text(content, encoding="utf-8")
                result = self.invoke(root, config_path)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stdout, "")
                error = json.loads(result.stderr)
                self.assertEqual(error["schema_version"], 1)
                self.assertEqual(error["type"], "error")
                self.assertIsInstance(error["error"], str)
                self.assertIn(context.lower(), error["error"].lower())
                self.assertNotIn(secret, result.stdout + result.stderr)

    def test_empty_config_normalizes_defaults_without_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            result = self.invoke(root, config_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            payload = json.loads(result.stdout)
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["type"], "config")
            self.assertEqual(payload["config"], {
                "version": 1,
                "interval_seconds": 30,
                "snapshot_interval_seconds": 900,
                "retention_days": 30,
                "journal": True,
                "application_logs": [],
                "http_logs": [],
                "services": [],
                "tcp": {"enabled": False, "mode": "sample"},
                "adapters": [],
                "failure_threshold": 2,
                "recovery_threshold": 2,
            })


if __name__ == "__main__":
    unittest.main()
