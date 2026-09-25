import io
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from hostdelta import config, daemon, health, tail
from hostdelta.archive import Archive
from hostdelta.conntrack import parse as parse_conntrack
from hostdelta.incidents import transition
from hostdelta.model import stamp, utcnow
from hostdelta.store import Store
from hostdelta.telemetry import StructuredLogger, bind_context, normalize, redact


def application(message="complete", **kwargs):
    return {"timestamp": stamp(utcnow() - timedelta(seconds=1)), "service": {"name": "orders"}, "level": "INFO", "message": message, **kwargs}


class TelemetryTests(unittest.TestCase):
    def test_nested_redaction_and_nonfinite(self):
        data = redact({"authorization": "Bearer topsecret", "nested": {"api_key": "hidden", "message": "password=hidden https://user:pass@example.test/path?key=hidden"}, "number": float("nan")})
        text = json.dumps(data)
        self.assertNotIn("hidden", text)
        self.assertNotIn("topsecret", text)
        self.assertNotIn("user:pass", text)
        self.assertIsNone(data["number"])

    def test_sdk_operation_trace_and_duration(self):
        stream = io.StringIO()
        logger = StructuredLogger("worker", stream)
        with bind_context(run_id="job-42"):
            with logger.operation("backup", password="secret"):
                logger.request("GET", "/ready?token=hidden", 503, 1200)
        rows = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({row["trace_id"] for row in rows}), 1)
        self.assertEqual(rows[-1]["event"]["name"], "backup.completed")
        self.assertIn("duration_ms", rows[-1]["attributes"])
        self.assertNotIn("hidden", stream.getvalue())
        self.assertNotIn('"secret"', stream.getvalue())
        for row in rows:
            self.assertEqual(normalize(row)["category"], "application")

    def test_operation_exception_is_logged_and_reraised(self):
        stream = io.StringIO()
        with self.assertRaises(RuntimeError):
            with StructuredLogger("worker", stream).operation("deploy"):
                raise RuntimeError("token=private")
        last = json.loads(stream.getvalue().splitlines()[-1])
        self.assertEqual(last["level"], "ERROR")
        self.assertNotIn("private", json.dumps(last))
        self.assertNotIn("traceback", last)

    def test_context_does_not_leak(self):
        stream = io.StringIO()
        logger = StructuredLogger("worker", stream)
        with bind_context(run_id="one"):
            logger.event("first")
        logger.event("second")
        self.assertNotIn("run_id", json.loads(stream.getvalue().splitlines()[-1]))

    def test_strict_trace_and_timestamp(self):
        for fields in ({"trace_id": "wrong"}, {"timestamp": "2026-01-01T00:00:00"}, {"level": "BOGUS"}):
            with self.assertRaises(ValueError):
                normalize(application(**fields))

    def test_http_attributes_allowlist(self):
        row = normalize(application(http={"method": "GET", "route": "/api?secret=x", "headers": {"x-custom": "do-not-retain"}, "status_code": 200}))
        self.assertEqual(row["attributes"]["http"]["route"], "/api")
        self.assertNotIn("do-not-retain", json.dumps(row))


class ConfigTests(unittest.TestCase):
    def test_defaults_and_unknown_keys(self):
        self.assertEqual(config.validate({})["retention_days"], 30)
        with self.assertRaises(ValueError):
            config.validate({"retention_dayz": 2})

    def test_invalid_bounds_and_opt_in(self):
        for value in ({"interval_seconds": True}, {"retention_days": 0}, {"application_logs": ["relative.log"]}, {"tcp": {"enabled": "yes"}}, {"services": ["--all"]}):
            with self.assertRaises(ValueError):
                config.validate(value)

    def test_no_inline_secrets_or_insecure_defaults(self):
        for adapter in ({"name": "cloud", "type": "openstack", "url": "http://cloud.test/v3"},
                        {"name": "cloud", "type": "openstack", "url": "https://cloud.test/v3", "password": "secret"}):
            with self.assertRaises(ValueError):
                config.validate({"adapters": [adapter]})


class TailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "application.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, message="first", mode="w"):
        with self.path.open(mode) as stream:
            stream.write(json.dumps(application(message)) + "\n")

    def test_incremental_no_duplicates_and_partial_line(self):
        self.write()
        first = tail.read(self.path)
        self.assertEqual(len(first["events"]), 1)
        with self.path.open("a") as stream:
            stream.write(json.dumps(application("partial")))
        partial = tail.read(self.path, first["checkpoint"])
        self.assertEqual(partial["events"], [])
        with self.path.open("a") as stream:
            stream.write("\n")
        final = tail.read(self.path, partial["checkpoint"])
        self.assertEqual(final["events"][0]["summary"], "partial")
        self.assertEqual(tail.read(self.path, final["checkpoint"])["events"], [])

    def test_rename_rotation_drains_previous_inode(self):
        self.write()
        first = tail.read(self.path)
        self.write("before-rotation", "a")
        self.path.rename(str(self.path) + ".1")
        self.write("after-rotation")
        second = tail.read(self.path, first["checkpoint"])
        self.assertEqual([e["summary"] for e in second["events"]], ["before-rotation", "after-rotation"])
        self.assertEqual(second["status"], "ok")

    def test_copytruncate_regrowth_detected(self):
        self.write("old")
        first = tail.read(self.path)
        self.write("a completely new and longer message")
        second = tail.read(self.path, first["checkpoint"])
        self.assertEqual(len(second["events"]), 1)
        self.assertEqual(second["status"], "partial")
        self.assertNotEqual(first["events"][0]["event_id"], second["events"][0]["event_id"])

    def test_inode_loss_is_explicit(self):
        self.write()
        first = tail.read(self.path)
        self.path.rename(str(self.path) + ".unfindable")
        self.write("second")
        Path(str(self.path) + ".unfindable").unlink()
        second = tail.read(self.path, first["checkpoint"])
        self.assertTrue(any("inode" in w for w in second["warnings"]))

    def test_malformed_and_future_records_never_store_raw(self):
        self.path.write_text("secret raw invalid JSON\n" + json.dumps(application(timestamp=stamp(utcnow() + timedelta(days=3)))) + "\n")
        result = tail.read(self.path)
        self.assertEqual(result["events"], [])
        self.assertEqual(result["status"], "partial")
        self.assertNotIn("secret raw", json.dumps(result))

    def test_oversized_record_suffix_never_parsed(self):
        self.path.write_text("x" * 600 + "\n" + json.dumps(application("safe")) + "\n")
        with patch.object(tail, "MAX_LINE", 256), patch.object(tail, "MAX_BYTES", 300):
            first = tail.read(self.path)
            second = tail.read(self.path, first["checkpoint"])
        self.assertEqual([e["summary"] for e in second["events"]], ["safe"])

    def test_http_persistence_removes_queries(self):
        self.path.write_text('192.0.2.1 - - [21/Sep/2026:00:00:00 +0000] "GET /api?token=SECRET HTTP/1.1" 503 10\n')
        result = tail.read(self.path, kind="http", now=__import__('datetime').datetime.fromisoformat('2026-09-22T00:00:00+00:00'))
        self.assertEqual(result["events"][0]["attributes"]["path"], "/api")
        self.assertNotIn("SECRET", json.dumps(result))


class IncidentTests(unittest.TestCase):
    def observation(self, second, health_state, **kwargs):
        return {"entity": "service:demo", "source": "test", "at": stamp(self.start + timedelta(seconds=second)), "health": health_state, **kwargs}

    def setUp(self):
        self.start = utcnow()

    def test_confirmed_failure_and_recovery_bounds(self):
        state, _, _ = transition(None, self.observation(0, "up"))
        state, _, incident = transition(state, self.observation(10, "down"))
        self.assertIsNone(incident)
        state, events, incident = transition(state, self.observation(20, "down"))
        self.assertEqual(events[0]["kind"], "outage_opened")
        self.assertEqual(incident["start_bounds"], {"after": stamp(self.start), "by": stamp(self.start + timedelta(seconds=10))})
        state, _, incident = transition(state, self.observation(30, "up"))
        self.assertIsNone(incident["closed_at"])
        state, events, incident = transition(state, self.observation(40, "up"))
        self.assertEqual(events[0]["kind"], "outage_recovered")
        self.assertEqual(incident["closed_at"], stamp(self.start + timedelta(seconds=30)))
        self.assertEqual(incident["duration_lower_seconds"], 10)
        self.assertEqual(incident["duration_upper_seconds"], 30)

    def test_unknown_is_not_healthy_or_failed(self):
        state, _, _ = transition(None, self.observation(0, "down"))
        state, _, _ = transition(state, self.observation(10, "unknown"))
        state, _, incident = transition(state, self.observation(20, "down"))
        self.assertIsNone(incident)
        state, _, incident = transition(state, self.observation(30, "down"))
        self.assertTrue(incident["left_censored"])
        self.assertTrue(incident["coverage_gap"])

    def test_gap_does_not_claim_continuous_duration(self):
        state, _, _ = transition(None, self.observation(0, "down"), 1)
        state, _, _ = transition(state, self.observation(10, "unknown"))
        state, _, _ = transition(state, self.observation(20, "down"))
        state, _, _ = transition(state, self.observation(30, "up"))
        _, _, incident = transition(state, self.observation(40, "up"))
        self.assertTrue(incident["coverage_gap"])
        self.assertEqual(incident["duration_lower_seconds"], 0)

    def test_invocation_change_requires_same_boot(self):
        state, _, _ = transition(None, self.observation(0, "up", invocation_id="a", boot_id="boot1"))
        state, events, _ = transition(state, self.observation(1, "up", invocation_id="b", boot_id="boot1"))
        self.assertEqual(events[0]["kind"], "service_restart")
        _, events, _ = transition(state, self.observation(2, "up", invocation_id="c", boot_id="boot2"))
        self.assertNotIn("service_restart", [e["kind"] for e in events])

    def test_stale_observation_does_not_rewind(self):
        state, _, _ = transition(None, self.observation(20, "up"))
        after, events, _ = transition(state, self.observation(10, "down"), 1)
        self.assertEqual(state, after)
        self.assertEqual(events, [])


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state", create=True)
        self.archive = Archive(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_event_and_offset_transaction_rolls_back_together(self):
        good = normalize(application(), "app", "id1")
        bad = {"at": good["at"]}
        with self.assertRaises(KeyError):
            self.archive.ingest("app", [good, bad], {"offset": 99})
        self.assertEqual(self.archive.checkpoint("app"), {})
        self.assertEqual(self.archive.status()["events"], 0)

    def test_dedup_and_trace_query(self):
        event = normalize(application(trace_id="a" * 32), "app", "id1")
        self.archive.ingest("app", [event], {"offset": 1})
        self.archive.ingest("app", [event], {"offset": 1})
        rows, _ = self.archive.query(stamp(utcnow() - timedelta(hours=1)), stamp(utcnow()), trace_id="a" * 32)
        self.assertEqual(len(rows), 1)

    def test_retention_preserves_named_boundary_and_open_incident(self):
        now = utcnow()
        for days, label in ((60, "keep"), (50, None), (40, None), (1, None)):
            self.store.save({"at": stamp(now - timedelta(days=days)), "domains": {}}, label)
        event = normalize(application(), "app", "old")
        self.archive.ingest("app", [event], {"offset": 42})
        with self.store.db:
            self.store.db.execute("UPDATE archive_events SET observed_at=?", (stamp(now - timedelta(days=60)),))
        before = self.archive.prune(30, dry_run=True)
        self.assertEqual(before["rows"]["archive_events"], 1)
        self.assertEqual(self.archive.status()["events"], 1)
        self.archive.prune(30)
        self.assertEqual(self.archive.status()["events"], 0)
        self.assertEqual(len(self.store.snapshots()), 3)
        self.assertEqual(self.archive.checkpoint("app"), {"offset": 42})
        self.assertIsNotNone(self.store.resolve("keep"))

    def test_http_aggregate_dedup(self):
        from hostdelta.tail import parse_http
        line = '192.0.2.1 - - [20/Sep/2026:00:00:00 +0000] "GET /health HTTP/1.1" 503 1'
        event = parse_http(line, "http", "first")
        self.archive.ingest("http", [event, event])
        result = self.archive.http_summary("2026-01-01", "2027-01-01")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["errors_5xx"], 1)

    def test_collector_lock_excludes_second_writer(self):
        with daemon.collector_lock(self.store):
            with self.assertRaises(ValueError):
                with daemon.collector_lock(self.store):
                    pass

    def test_application_cycle_survives_restart_without_duplicates(self):
        path = Path(self.tmp.name) / "app.jsonl"
        path.write_text(json.dumps(application()) + "\n")
        cfg = config.validate({"journal": False, "application_logs": [str(path)]})
        with patch.object(daemon, "capture", return_value={"at": stamp(utcnow()), "host": "test", "domains": {}}):
            first = daemon.cycle(self.store, cfg)
            second = daemon.cycle(self.store, cfg)
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(second["inserted"], 0)


class TCPTests(unittest.TestCase):
    def test_proc_ipv4_ipv6_addresses(self):
        self.assertEqual(health.address("0100007F:0016"), ("127.0.0.1", 22))
        self.assertEqual(health.address("00000000000000000000000001000000:01BB", True), ("::1", 443))

    def test_conntrack_real_tuple_not_handshake_claim(self):
        event = parse_conntrack("[1790000000.123456] [NEW] tcp 6 120 SYN_SENT src=192.0.2.1 dst=198.51.100.1 sport=40000 dport=443 src=198.51.100.1 dst=192.0.2.1 sport=443 dport=40000", "id")
        self.assertEqual(event["attributes"]["src"], "192.0.2.1")
        self.assertFalse(event["attributes"]["handshake_confirmed"])
        self.assertEqual(event["kind"], "tcp_flow_new")

    def test_conntrack_ipv6_keeps_original_tuple_for_new_and_destroy(self):
        original = "src=2001:db8::1 dst=2001:db8::2 sport=40000 dport=443"
        reply = "src=2001:db8::2 dst=2001:db8::1 sport=443 dport=40000"
        for action, expected_kind in (("NEW", "tcp_flow_new"), ("DESTROY", "tcp_flow_destroyed")):
            with self.subTest(action=action):
                event = parse_conntrack(f"[1790000000.123456] [{action}] tcp 6 120 ESTABLISHED {original} {reply}", action)
                self.assertEqual(
                    {key: event["attributes"][key] for key in ("src", "dst", "sport", "dport")},
                    {"src": "2001:db8::1", "dst": "2001:db8::2", "sport": 40000, "dport": 443},
                )
                self.assertEqual(event["attributes"]["direction"], "unknown")
                self.assertFalse(event["attributes"]["handshake_confirmed"])
                self.assertEqual(event["kind"], expected_kind)

    def test_conntrack_rejects_incomplete_or_invalid_tuple(self):
        malformed = (
            "[1790000000.123456] [NEW] tcp 6 120 SYN_SENT src=2001:db8::1 dst=2001:db8::2 sport=40000",
            "[1790000000.123456] [NEW] tcp 6 120 SYN_SENT src=not-an-ip dst=2001:db8::2 sport=40000 dport=443",
            "[1790000000.123456] [NEW] tcp 6 120 SYN_SENT src=2001:db8::1 dst=2001:db8::2 sport=65536 dport=443",
            "[1790000000.123456] [NEW] tcp 6 120 SYN_SENT src=2001:db8::1 dst=2001:db8::2 sport=40000 dport=-1",
        )
        for line in malformed:
            with self.subTest(line=line), self.assertRaises(ValueError):
                parse_conntrack(line, "malformed")

    def test_tcp_failure_never_infers_disappearance(self):
        with tempfile.TemporaryDirectory() as directory:
            result = health.tcp({"sockets": {"existing": {}}}, root=Path(directory))
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["checkpoint"])
        self.assertEqual(result["events"], [])


if __name__ == "__main__":
    unittest.main()
