"""Exercise actual subprocess lifetime and durable recovery without host mutation."""

from contextlib import closing
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from hostdelta.archive import Archive
from hostdelta.model import stamp, utcnow
from hostdelta.store import Store


class ProcessTests(unittest.TestCase):
    def test_sigterm_and_restart_keep_offsets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            store = Store(state, create=True)
            store.close()
            log = root / "app.jsonl"
            log.write_text(json.dumps({"timestamp": stamp(utcnow()), "service": "test", "message": "persist me"}) + "\n")
            cfg = root / "config.json"
            cfg.write_text(json.dumps({"version": 1, "interval_seconds": 1, "journal": False, "application_logs": [str(log)]}))
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
            command = [sys.executable, "-m", "hostdelta", "--state-dir", str(state)]
            process = subprocess.Popen([*command, "daemon", "--config", str(cfg)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            try:
                deadline = time.monotonic() + 10
                count = 0
                while time.monotonic() < deadline:
                    with closing(sqlite3.connect(state / "state.sqlite3")) as db:
                        count = db.execute("SELECT COUNT(*) FROM archive_events").fetchone()[0]
                    if count == 1:
                        break
                    if process.poll() is not None:
                        self.fail("Collector exited before persisting the application event")
                    time.sleep(0.05)
                self.assertEqual(count, 1)
                process.send_signal(signal.SIGTERM)
                _, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertTrue(all(isinstance(json.loads(line), dict) for line in stderr.splitlines()))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
            second = subprocess.run([*command, "collect", "--config", str(cfg), "--json"], env=env, capture_output=True, text=True, timeout=30)
            self.assertIn(second.returncode, (0, 3), second.stderr)
            store = Store(state)
            try:
                self.assertEqual(Archive(store).status()["events"], 1)
            finally:
                store.close()

    def test_v1_database_migrates_without_losing_legacy_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            root.mkdir(mode=0o700)
            with closing(sqlite3.connect(root / "state.sqlite3")) as db, db:
                db.executescript("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL); PRAGMA user_version=1;")
                db.execute("INSERT INTO settings VALUES (?,?)", ("consumer:legacy", json.dumps("2026-01-01T00:00:00.000000+00:00")))
            store = Store(root)
            try:
                self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertEqual(store.setting("consumer:legacy"), "2026-01-01T00:00:00.000000+00:00")
                self.assertEqual(Archive(store).status()["events"], 0)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
