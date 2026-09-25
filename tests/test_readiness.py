from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from hostdelta.readiness import assess
from hostdelta.model import utcnow, stamp
from hostdelta.archive import Archive
from hostdelta.store import Store
from hostdelta import cli
import io
import json
from contextlib import redirect_stdout


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.now = utcnow()
        self.config = {"journal": False, "interval_seconds": 30, "snapshot_interval_seconds": 900,
                       "application_logs": ["/tmp/app.jsonl"]}
        self.beat = {"last_cycle_at": stamp(self.now), "healthy": True}
        self.rows = [{"source": "state:snapshot", "at": stamp(self.now - timedelta(seconds=100)), "status": "ok"},
                     {"source": "application:/tmp/app.jsonl", "at": stamp(self.now), "status": "ok"}]

    def check(self):
        return assess(self.rows, self.beat, self.config, self.now)

    def test_ready_and_snapshot_cadence(self):
        self.assertTrue(self.check()["ready"])
        self.assertEqual(self.check()["sources"][1]["max_age_seconds"], 2700)

    def test_missing_stale_and_failed_source(self):
        for status, at, reason in (("ok", None, "never_collected"),
                                   ("ok", stamp(self.now - timedelta(seconds=91)), "stale"),
                                   ("partial", stamp(self.now), "source_unhealthy")):
            self.rows[1].update(status=status, at=at)
            result = self.check()
            self.assertFalse(result["ready"])
            self.assertEqual(result["sources"][0]["reason"], reason)

    def test_future_invalid_and_stopped_heartbeat(self):
        for value, reason in ((stamp(self.now + timedelta(seconds=1)), "clock_skew"), ("bad", "invalid_timestamp"),
                              ("2s", "invalid_timestamp"), ("2026-01-01T00:00:00", "invalid_timestamp")):
            self.beat["last_cycle_at"] = value
            self.assertEqual(self.check()["heartbeat_assessment"]["reason"], reason)
            self.assertFalse(self.check()["ready"])
        self.beat.update(last_cycle_at=stamp(self.now), stopped_at=stamp(self.now))
        self.assertEqual(self.check()["heartbeat_assessment"]["reason"], "stopped")

    def test_removed_source_does_not_block(self):
        self.rows.append({"source": "adapter:removed", "at": None, "status": "unavailable"})
        self.assertTrue(self.check()["ready"])
        self.assertEqual(self.check()["sources"][0]["reason"], "not_configured")

    def test_enabled_sources_without_runs_are_listed(self):
        self.config.update(journal=True, services=["test.service"], tcp={"enabled": True, "mode": "conntrack"},
                           adapters=[{"name": "cloud"}], http_logs=["/tmp/http.log"])
        result = self.check()
        missing = {r["source"] for r in result["sources"] if r["reason"] == "never_collected"}
        self.assertEqual(missing, {"journald", "systemd:health", "tcp:conntrack", "adapter:cloud", "http:/tmp/http.log"})

    def test_archive_and_cli_missing_source_exit_three(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state", create=True)
            store.set_setting("collector_config", self.config)
            store.set_setting("daemon", self.beat)
            self.assertFalse(Archive(store).status()["ready"])
            directory = str(store.directory)
            store.close()
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.main(["--state-dir", directory, "status", "--json"])
            self.assertEqual(code, 3)
            self.assertFalse(json.loads(output.getvalue())["ready"])

    def test_freshness_boundary_and_last_cycle_failure(self):
        self.rows[1]["at"] = stamp(self.now - timedelta(seconds=90))
        self.assertTrue(self.check()["ready"])
        self.rows[1]["at"] = stamp(self.now - timedelta(seconds=90, microseconds=1))
        self.assertFalse(self.check()["ready"])
        self.rows[1]["at"] = stamp(self.now)
        self.beat["healthy"] = False
        self.assertTrue(self.check()["fresh"])
        self.assertFalse(self.check()["ready"])
        self.assertEqual(self.check()["heartbeat_assessment"]["reason"], "last_cycle_unhealthy")

    def test_cli_healthy_sources_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state", create=True)
            store.set_setting("collector_config", self.config)
            store.set_setting("daemon", self.beat)
            archive = Archive(store)
            for row in self.rows:
                archive.ingest(row["source"], [])
            directory = str(store.directory)
            store.close()
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.main(["--state-dir", directory, "status", "--json"])
            self.assertEqual(code, 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["schema_version"], 1)
            self.assertTrue(report["ready"])
            self.assertEqual(report["events"], 0)

    def test_snapshot_freshness_cannot_require_faster_than_collection(self):
        self.config["snapshot_interval_seconds"] = 1
        self.rows[0]["at"] = stamp(self.now - timedelta(seconds=60))
        self.assertTrue(self.check()["ready"])
        self.assertEqual(self.check()["sources"][1]["max_age_seconds"], 90)
