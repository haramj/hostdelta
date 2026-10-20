import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hostdelta import cli
from hostdelta.archive import Archive
from hostdelta.model import stamp
from hostdelta.store import Store


class ArchiveBriefTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "state", create=True)
        self.addCleanup(self.store.close)
        self.now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
        self.since = self.now - timedelta(hours=1)
        self.config = {"journal": False, "http_logs": ["/tmp/http.log"],
                       "application_logs": ["/tmp/app.jsonl"], "interval_seconds": 30}
        self.store.set_setting("collector_config", self.config)
        self.store.set_setting("daemon", {"last_cycle_at": stamp(self.now), "healthy": True})
        self.store.ack("worker", stamp(self.since))
        self.store.begin_session(stamp(self.since), stamp(self.since - timedelta(hours=1)))
        for at in (self.since, self.now):
            self.store.save({"at": stamp(at), "host": "synthetic", "domains": {
                "services": {"status": "ok", "data": {"demo.service": {"active": "failed"}}}}})
        for source in ("state:snapshot", "http:/tmp/http.log", "application:/tmp/app.jsonl"):
            self.run_source(source)

    def run_source(self, source, status="ok", age=0):
        with patch("hostdelta.archive.utcnow", return_value=self.now - timedelta(seconds=age)):
            Archive(self.store).ingest(source, [], status=status)

    def brief(self):
        before = (self.store.setting("consumer:worker"), self.store.last_session(), self.store.last_since())
        output = io.StringIO()
        with patch("hostdelta.cli.utcnow", return_value=self.now), redirect_stdout(output):
            code = cli.main(["--state-dir", str(self.store.directory), "brief", "--archive",
                             "--consumer", "worker", "--require-coverage", "--fail-on", "critical", "--json"])
        self.assertEqual(before, (self.store.setting("consumer:worker"), self.store.last_session(), self.store.last_since()))
        report = json.loads(output.getvalue())
        self.assertFalse(report["local_state_updated"])
        return code, report["coverage"]

    def test_ready_retained_evidence_still_returns_finding_exit(self):
        code, coverage = self.brief()
        self.assertEqual(code, 2)
        self.assertTrue(coverage["collection_ok"])
        self.assertTrue(coverage["readiness"]["ready"])

    def test_missing_source_overrides_finding_exit(self):
        self.config["application_logs"].append("/tmp/missing.jsonl")
        self.store.set_setting("collector_config", self.config)
        code, coverage = self.brief()
        self.assertEqual(code, 3)
        self.assertFalse(coverage["collection_ok"])
        missing = next(r for r in coverage["readiness"]["sources"] if r["source"] == "application:/tmp/missing.jsonl")
        self.assertEqual(missing["reason"], "never_collected")
        self.assertTrue(any("never_collected" in w for w in coverage["warnings"]))

    def test_invalid_future_stopped_and_unhealthy_heartbeat(self):
        cases = [({"last_cycle_at": stamp(self.now + timedelta(seconds=1)), "healthy": True}, "clock_skew"),
                 ({"last_cycle_at": "invalid", "healthy": True}, "invalid_timestamp"),
                 ({"last_cycle_at": stamp(self.now), "healthy": True, "stopped_at": stamp(self.now)}, "stopped"),
                 ({"last_cycle_at": stamp(self.now), "healthy": False}, "last_cycle_unhealthy")]
        for heartbeat, reason in cases:
            with self.subTest(reason=reason):
                self.store.set_setting("daemon", heartbeat)
                code, coverage = self.brief()
                self.assertEqual(code, 3)
                self.assertEqual(coverage["readiness"]["heartbeat_assessment"]["reason"], reason)

    def test_stale_source_blocks_coverage(self):
        self.run_source("application:/tmp/app.jsonl", age=91)
        code, coverage = self.brief()
        self.assertEqual(code, 3)
        self.assertTrue(any(r["reason"] == "stale" for r in coverage["readiness"]["sources"]))

    def test_current_readiness_does_not_erase_historical_failure(self):
        for status in ("partial", "unavailable"):
            with self.subTest(status=status):
                self.run_source("application:/tmp/app.jsonl", status, age=60)
                self.run_source("application:/tmp/app.jsonl")
                code, coverage = self.brief()
                self.assertEqual(code, 3)
                self.assertTrue(coverage["readiness"]["ready"])
                self.assertTrue(any(r["status"] == status for r in coverage["source_runs"]))

    def test_retention_gap_remains_visible(self):
        self.store.set_setting("retention_floor", stamp(self.since + timedelta(seconds=1)))
        code, coverage = self.brief()
        self.assertEqual(code, 3)
        self.assertTrue(coverage["readiness"]["ready"])
        self.assertTrue(any("retention floor" in w for w in coverage["warnings"]))

    def test_unavailable_state_remains_visible(self):
        self.store.save({"at": stamp(self.now), "host": "synthetic", "domains": {
            "services": {"status": "unavailable", "data": {}, "error": "synthetic gap"}}})
        code, coverage = self.brief()
        self.assertEqual(code, 3)
        self.assertTrue(coverage["readiness"]["ready"])
        self.assertTrue(any("synthetic gap" in w for w in coverage["warnings"]))
