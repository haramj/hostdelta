import gzip
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hostdelta import cli, collect, events, requests
from hostdelta.brief import build, render
from hostdelta.demo import report
from hostdelta.model import clean, compare, parse_time, stamp, utcnow
from hostdelta.store import Store

NOW = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
START = NOW - timedelta(days=1)


def snapshot(at=START):
    return {"at": stamp(at), "host": "test-host", "domains": {
        "packages": {"status": "ok", "data": {"pkg": "1.0"}},
        "services": {"status": "ok", "data": {}},
        "files": {"status": "ok", "scope": ["/etc/example"], "data": {}}}}


class ModelTests(unittest.TestCase):
    def test_duration_and_timezone(self):
        self.assertEqual(parse_time("2d", NOW), NOW - timedelta(days=2))
        self.assertEqual(parse_time("2026-09-21T21:00+09:00"), NOW)
        with self.assertRaises(ValueError):
            parse_time("yesterday-ish")

    def test_cannot_turn_collection_failure_into_deletions(self):
        before, after = snapshot(), snapshot(NOW)
        after["domains"]["packages"] = {"status": "unavailable", "data": {}, "error": "denied"}
        diff = compare(before, after)
        self.assertNotIn("packages", diff["changes"])
        self.assertIn("packages", diff["skipped"])

    def test_watch_scope_change_is_not_file_deletion(self):
        before, after = snapshot(), snapshot(NOW)
        before["domains"]["files"]["data"] = {"/etc/example": "hash"}
        after["domains"]["files"]["scope"] = ["/etc/other"]
        self.assertIn("files", compare(before, after)["skipped"])

    def test_actual_add_remove_modify(self):
        before, after = snapshot(), snapshot(NOW)
        before["domains"]["packages"]["data"] = {"gone": "1", "pkg": "1"}
        after["domains"]["packages"]["data"] = {"new": "1", "pkg": "2"}
        self.assertEqual([x["kind"] for x in compare(before, after)["changes"]["packages"]], ["removed", "added", "changed"])

    def test_terminal_controls_cannot_escape(self):
        text = clean("\x1b[2J\nspoof\u202e")
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\n", text)
        self.assertNotIn("\u202e", text)

    def test_extreme_duration_is_a_user_error(self):
        with self.assertRaises(ValueError):
            parse_time("999999999999999999999999999d", NOW)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "private"
        self.store = Store(self.path, create=True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_labels_head_and_baseline(self):
        self.store.save(snapshot(), "initial")
        self.store.save(snapshot(NOW), "later")
        self.assertEqual(self.store.resolve("HEAD~1"), self.store.resolve("initial"))
        self.assertEqual(self.store.resolve("2"), self.store.resolve("HEAD"))
        self.assertIsNone(self.store.baseline(stamp(START - timedelta(seconds=1))))
        self.assertEqual(self.store.baseline(stamp(START)), snapshot())
        with self.assertRaises(ValueError):
            self.store.save(snapshot(), "initial")
        with self.assertRaises(ValueError):
            self.store.save(snapshot(), "HEAD")

    def test_atomic_session_dedup_and_previous_window(self):
        older = stamp(START - timedelta(days=1))
        self.assertEqual(self.store.begin_session(stamp(START), older, "connection-a"), older)
        self.assertIsNone(self.store.begin_session(stamp(NOW), older, "connection-a"))
        self.assertEqual(self.store.begin_session(stamp(NOW), older, "connection-b"), stamp(START))
        self.assertEqual(self.store.last_since(), stamp(START))
        self.assertEqual(self.store.last_session(), stamp(NOW))

    def test_consumer_isolation_and_monotonicity(self):
        self.store.ack("alpha", stamp(NOW))
        self.store.ack("beta", stamp(START))
        self.assertEqual(self.store.setting("consumer:alpha"), stamp(NOW))
        self.assertEqual(self.store.setting("consumer:beta"), stamp(START))
        with self.assertRaises(ValueError):
            self.store.ack("alpha", stamp(START))

    def test_private_permissions(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.path / "state.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_event_cursor_excludes_already_acknowledged_boundary(self):
        self.store.record({"at": stamp(START), "summary": "first"})
        self.store.record({"at": stamp(NOW), "summary": "second"})
        rows, truncated = self.store.events(stamp(START), stamp(NOW))
        self.assertEqual([row["summary"] for row in rows], ["second"])
        self.assertFalse(truncated)

    def test_no_state_created_on_read(self):
        path = Path(self.tmp.name) / "absent"
        with self.assertRaises(ValueError):
            Store(path)
        self.assertFalse(path.exists())


class DeploymentReviewWalkthroughTests(unittest.TestCase):
    def test_synthetic_walkthrough_compares_state_and_event_window(self):
        root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(root / "src"), env.get("PYTHONPATH"))))
        result = subprocess.run(
            [sys.executable, "examples/deployment_review.py"],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SYNTHETIC DEPLOYMENT REVIEW", result.stdout)
        self.assertRegex(result.stdout, r"State comparison: .* → .*")
        self.assertIn("1. Compare the saved before/after service observations:", result.stdout)
        self.assertIn("SERVICES: 1 changed", result.stdout)
        self.assertIn("EVENT TIMELINE", result.stdout)
        self.assertIn("synthetic deployment started", result.stdout)
        self.assertIn("Snapshots are observations, not VM restore points", result.stdout)


class RequestTests(unittest.TestCase):
    def combined(self, target="/health?token=secret", status=200):
        return f'192.0.2.1 - - [21/Sep/2026:11:00:00 +0000] "GET {target} HTTP/1.1" {status} 12 "-" "agent"\n'

    def test_combined_never_retains_query(self):
        row = requests.parse_line(self.combined())
        self.assertEqual(row["path"], "/health")
        self.assertNotIn("secret", json.dumps(row))

    def test_json_timezone_and_timing(self):
        row = requests.parse_line(json.dumps({"time": "2026-09-21T20:00:00+09:00", "remote_addr": "::1", "method": "POST", "path": "/v1/run?key=secret", "status": "503", "request_time": "1.42"}))
        self.assertTrue(row["slow"])
        self.assertEqual(row["path"], "/v1/run")
        self.assertEqual(row["at"], "2026-09-21T11:00:00.000000+00:00")

    def test_invalid_line(self):
        for line in ("garbage", "{}", self.combined(status=999)):
            with self.assertRaises((ValueError, KeyError)):
                requests.parse_line(line)

    def test_rotation_missing_and_window(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "access.log"
            path.write_text(self.combined() + "bad record\n")
            with gzip.open(str(path) + ".1.gz", "wt") as stream:
                stream.write(self.combined("/api?secret=1", 503))
            result = requests.analyze([str(path), str(path)], START, NOW)
            self.assertEqual(result["total"], 2)
            self.assertEqual(result["errors_5xx"], 1)
            self.assertEqual(result["files_read"], 2)
            self.assertTrue(any("malformed" in w for w in result["warnings"]))
            self.assertNotIn("secret", json.dumps(result))
            empty = requests.analyze([str(path)], NOW, NOW + timedelta(hours=1))
            self.assertEqual(empty["total"], 0)
            missing = requests.analyze([str(path) + "-missing"], START, NOW)
            self.assertEqual(missing["status"], "unavailable")

    def test_unconfigured_is_not_zero_requests_claim(self):
        self.assertEqual(requests.analyze([], START, NOW)["status"], "not_configured")

    def test_fifo_is_not_opened(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "access.log"
            os.mkfifo(path)
            result = requests.analyze([str(path)], START, NOW)
            self.assertEqual(result["status"], "unavailable")
            self.assertTrue(any("Not a regular" in w for w in result["warnings"]))

    def test_acknowledged_request_boundary_is_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "access.log"
            path.write_text(self.combined())
            result = requests.analyze([str(path)], NOW - timedelta(hours=1), NOW)
            self.assertEqual(result["total"], 0)

    def test_standard_log_discovery(self):
        with patch.object(Path, "is_file", return_value=True):
            self.assertEqual(len(requests.discover()), 3)

    def test_bounded_plain_log_warns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "access.log"
            path.write_text(self.combined() * 20)
            with patch.object(requests, "MAX_BYTES", 300):
                result = requests.analyze([str(path)], START, NOW)
            self.assertLess(result["total"], 20)
            self.assertTrue(any("newest" in w for w in result["warnings"]))


    def test_combined_positive_offset_normalizes_to_utc(self):
        """A +09:00 combined-log line is compared at its UTC instant."""
        line = (
            "203.0.113.7 - - [21/Sep/2026:11:30:00 +09:00] "
            '"GET /reports?token=secret HTTP/1.1" 200 80 "-" "agent"\n'
        )
        row = requests.parse_line(line)
        self.assertEqual(row["at"], "2026-09-21T02:30:00.000000+00:00")
        self.assertEqual(row["path"], "/reports")
        self.assertEqual(row["ip"], "203.0.113.7")
        self.assertNotIn("secret", json.dumps(row))

    def test_combined_positive_offset_crosses_date_boundary(self):
        """A +09:00 early-morning line is the PREVIOUS UTC date."""
        line = (
            "203.0.113.7 - - [21/Sep/2026:03:00:00 +09:00] "
            '"GET /edge HTTP/1.1" 200 12 "-" "agent"\n'
        )
        row = requests.parse_line(line)
        # 03:00 +09:00 is 18:00 UTC the day before, not the 21st in UTC.
        self.assertEqual(row["at"], "2026-09-20T18:00:00.000000+00:00")

    def test_combined_negative_offset_crosses_date_boundary(self):
        """A -05:00 late-evening line is the NEXT UTC date."""
        line = (
            "198.51.100.23 - - [22/Sep/2026:20:00:00 -05:00] "
            '"GET /edge HTTP/1.1" 503 12 "-" "agent"\n'
        )
        row = requests.parse_line(line)
        # 20:00 -05:00 is 01:00 UTC the day after, not the 22nd in UTC.
        self.assertEqual(row["at"], "2026-09-23T01:00:00.000000+00:00")
        self.assertEqual(row["status"], 503)

    def test_combined_offset_window_filters_on_utc_instant(self):
        """analyze() includes a +05:00 line only when its UTC instant is in range."""
        line = (
            "192.0.2.44 - - [21/Sep/2026:08:00:00 +05:00] "
            '"GET /window HTTP/1.1" 200 10 "-" "agent"\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "access.log"
            path.write_text(line, encoding="utf-8")
            # 03:00 UTC is inside [START=20th 12:00, NOW=21st 12:00]
            result = requests.analyze([str(path)], START, NOW)
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["top_paths"][0][0], "GET /window")
            self.assertEqual(result["first_request"], "2026-09-21T03:00:00.000000+00:00")
            # Shift the window entirely above that instant: nothing matches.
            empty = requests.analyze([str(path)], NOW + timedelta(hours=1), NOW + timedelta(hours=2))
            self.assertEqual(empty["total"], 0)

    def test_combined_ipv6_client(self):
        """An IPv6 client address is retained and validated."""
        line = (
            "2001:db8::1 - - [21/Sep/2026:11:00:00 +0000] "
            '"GET /v6 HTTP/1.1" 200 42 "-" "agent"\n'
        )
        row = requests.parse_line(line)
        self.assertEqual(row["ip"], "2001:db8::1")
        self.assertEqual(row["path"], "/v6")


class JournalTests(unittest.TestCase):
    def row(self, message, **fields):
        return {"MESSAGE": message, "__REALTIME_TIMESTAMP": str(int(NOW.timestamp() * 1e6)), "__CURSOR": "cursor:1", **fields}

    def test_sensitive_command_redacted(self):
        row = self.row("user : COMMAND=/usr/bin/curl -H token=secret", SYSLOG_IDENTIFIER="sudo")
        event = events.classify(row)
        self.assertEqual(event["kind"], "sudo")
        self.assertNotIn("secret", json.dumps(event))

    def test_failed_and_successful_ssh(self):
        for message, kind in (("Failed password for secretuser", "ssh_failure"), ("Accepted publickey for someone", "ssh_success")):
            event = events.classify(self.row(message, SYSLOG_IDENTIFIER="sshd"))
            self.assertEqual(event["kind"], kind)
            self.assertNotIn("secretuser", json.dumps(event))

    def test_firewall_and_oom(self):
        self.assertEqual(events.classify(self.row("[UFW BLOCK] IN=eth0 SRC=1.2.3.4", _TRANSPORT="kernel"))["kind"], "firewall_block")
        self.assertEqual(events.classify(self.row("Out of memory: Killed process 7", _TRANSPORT="kernel"))["severity"], "critical")
        self.assertIsNone(events.classify(self.row("[UFW BLOCK] IN= OUT=eth0 SRC=1.2.3.4", _TRANSPORT="kernel")))

    def test_unknown_and_bad_time_are_ignored(self):
        self.assertIsNone(events.classify(self.row("regular app debug line")))
        self.assertIsNone(events.classify({"MESSAGE": "Failed password", "SYSLOG_IDENTIFIER": "sshd"}))

    def test_journal_denied(self):
        result = subprocess.CompletedProcess([], 1, "", "Permission denied")
        with patch.object(events, "bounded_run", return_value=result):
            actual = events.journal(START, NOW)
        self.assertEqual(actual["status"], "unavailable")

    def test_newest_records_sorted_and_malformed_visible(self):
        row = self.row("Failed to start backup.service", SYSLOG_IDENTIFIER="systemd")
        stdout = json.dumps(row) + "\nnot-json\n"
        with patch.object(events, "bounded_run", return_value=subprocess.CompletedProcess([], 0, stdout, "")):
            result = events.journal(START, NOW)
        self.assertEqual(len(result["events"]), 1)
        self.assertTrue(any("malformed" in w for w in result["warnings"]))


class CollectorTests(unittest.TestCase):
    def test_file_hash_changes_and_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / "config"
            file.write_text("before")
            (root / "link").symlink_to("/not/read")
            old = collect.files([directory])
            file.write_text("after")
            new = collect.files([directory])
            self.assertNotEqual(old[str(file)]["sha256"], new[str(file)]["sha256"])
            self.assertEqual(new[str(root / "link")], {"symlink": "/not/read"})
            self.assertNotIn("after", json.dumps(new))

    def test_dpkg_ignores_removed_packages(self):
        with patch.object(collect.shutil, "which", return_value="/usr/bin/dpkg-query"), patch.object(collect, "run", return_value="a\t1\tinstalled\nb\t2\tconfig-files\n"):
            self.assertEqual(collect.packages(), {"a": "1"})

    def test_network_ignores_address_lifetime(self):
        links = [{"ifname": "eth0", "operstate": "UP", "mtu": 1500, "addr_info": [{"local": "10.0.0.1", "prefixlen": 24, "valid_life_time": 123}]}]
        with patch.object(collect, "run", side_effect=[json.dumps(links), "[]", "[]"]):
            self.assertNotIn("valid_life_time", json.dumps(collect.network()))

    def test_unsupported_platform_actionable(self):
        with patch.object(collect.platform, "system", return_value="Darwin"):
            with self.assertRaisesRegex(ValueError, "demo"):
                collect.capture([])


class BriefTests(unittest.TestCase):
    def test_demo_correlation_never_claims_cause(self):
        data = report()
        finding = next(f for f in data["findings"] if f["confidence"] == "temporal_correlation")
        self.assertIn("Causation is unconfirmed", finding["summary"])
        self.assertEqual(len(finding["evidence"]), 2)
        self.assertEqual(data["severity"], "critical")
        self.assertIn("synthetic", render(data))

    def test_no_baseline_does_not_invent_diff(self):
        data = build(snapshot(NOW), None, {"events": [], "status": "unavailable", "warnings": ["denied"]},
                     requests.analyze([], START, NOW), stamp(START), stamp(NOW))
        self.assertIsNone(data["state_diff"])
        self.assertTrue(any("No snapshot" in w for w in data["coverage"]["warnings"]))


class CLITests(unittest.TestCase):
    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = cli.main(list(args))
        return status, stdout.getvalue(), stderr.getvalue()

    def test_demo_json_no_state_created(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "absent")
            status, output, error = self.invoke("--state-dir", path, "demo", "--json")
            self.assertEqual(status, 0)
            self.assertEqual(json.loads(output)["schema_version"], 1)
            self.assertFalse(Path(path).exists())
            self.assertEqual(error, "")

    def test_machine_workflow_cursor_moves_only_on_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state")
            store = Store(path, create=True)
            store.save(snapshot(utcnow() - timedelta(days=2)))
            store.close()
            with patch.object(cli, "capture", side_effect=lambda watch: snapshot(utcnow())), patch.object(cli, "journal", return_value={"events": [], "status": "ok", "warnings": []}):
                code, output, _ = self.invoke("--state-dir", path, "brief", "--consumer", "agent-a", "--json")
                self.assertEqual(code, 0)
                brief = json.loads(output)
                store = Store(path)
                self.assertIsNone(store.setting("consumer:agent-a"))
                store.close()
                self.assertEqual(self.invoke("--state-dir", path, "ack", "--consumer", "agent-a", "--until", brief["window"]["until"], "--json")[0], 0)
                _, output2, _ = self.invoke("--state-dir", path, "brief", "--consumer", "agent-a", "--json")
                self.assertEqual(json.loads(output2)["window"]["since"], brief["window"]["until"])

    def test_agent_failure_exit_and_coverage_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state")
            store = Store(path, create=True)
            store.save(snapshot(utcnow() - timedelta(days=2)))
            store.close()
            self.assertEqual(self.invoke("--state-dir", path, "record", "--actor", "bot", "--kind", "failure", "--json")[0], 0)
            with patch.object(cli, "capture", side_effect=lambda watch: snapshot(utcnow())), patch.object(cli, "journal", return_value={"events": [], "status": "ok", "warnings": []}):
                self.assertEqual(self.invoke("--state-dir", path, "brief", "--since", "1h", "--fail-on", "critical", "--json")[0], 2)
                self.assertEqual(self.invoke("--state-dir", path, "brief", "--since", "1h", "--require-coverage", "--json")[0], 3)

    def test_session_failure_does_not_advance_login(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state")
            store = Store(path, create=True)
            store.close()
            with patch.object(cli, "capture", side_effect=ValueError("failed")):
                self.assertEqual(self.invoke("--state-dir", path, "session", "--json")[0], 1)
            store = Store(path)
            self.assertIsNone(store.last_session())
            store.close()

    def test_json_error_on_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            code, out, err = self.invoke("--state-dir", str(Path(directory) / "missing"), "brief", "--json")
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertEqual(json.loads(err)["type"], "error")

    def test_json_usage_error_is_machine_readable(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as stopped:
            cli.main(["ack", "--json"])
        self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(json.loads(stderr.getvalue())["type"], "error")

    def test_successful_session_preserves_brief_window(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state")
            store = Store(path, create=True)
            previous = utcnow() - timedelta(hours=1)
            store.begin_session(stamp(previous), stamp(previous - timedelta(hours=1)))
            store.save(snapshot(previous - timedelta(hours=1)))
            store.close()
            with patch.object(cli, "capture", side_effect=lambda watch: snapshot(utcnow())), patch.object(cli, "journal", return_value={"events": [], "status": "ok", "warnings": []}):
                code, output, _ = self.invoke("--state-dir", path, "session", "--json")
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(output)["window"]["since"], stamp(previous))
                _, output2, _ = self.invoke("--state-dir", path, "brief", "--json")
                self.assertEqual(json.loads(output2)["window"]["since"], stamp(previous))

    def test_hook_is_opt_in_quoted_and_bounded(self):
        with patch.object(cli.shutil, "which", return_value="/opt/my apps/hostdelta"):
            code, output, _ = self.invoke("hook", "bash")
        self.assertEqual(code, 0)
        self.assertIn("'/opt/my apps/hostdelta'", output)
        self.assertIn("SSH_CONNECTION", output)
        self.assertIn("--kill-after=1s 8s", output)


if __name__ == "__main__":
    unittest.main()
