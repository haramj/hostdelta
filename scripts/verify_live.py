#!/usr/bin/env python3
"""Read-only host/cloud acceptance check. Writes only its own state and result file.

Run against a separate, initialized HostDelta state directory, with the daemon stopped.
It never changes services, firewall rules, cloud resources, or application data.
"""

import argparse
import json
import os
import platform
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hostdelta import __version__, config, daemon
from hostdelta.archive import Archive
from hostdelta.model import stamp, utcnow
from hostdelta.store import Store
from hostdelta.telemetry import redact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=3)
    args = parser.parse_args()
    if not 2 <= args.cycles <= 20:
        parser.error("--cycles must be between 2 and 20")
    if platform.system() != "Linux":
        parser.error("Live acceptance requires Linux; use the automated test suite on macOS")
    started = stamp(utcnow())
    report = {"hostdelta_version": __version__, "started_at": started, "platform": platform.platform(),
              "checks": [], "cycles": [], "host_mutations": False}
    def check(name, passed, detail):
        report["checks"].append({"name": name, "passed": bool(passed), "detail": detail})
    try:
        cfg = config.load(args.config)
        check("configuration", True, "Strict configuration validation passed")
        with_store = Store(args.state_dir)
        try:
            archive = Archive(with_store)
            with daemon.collector_lock(with_store):
                for index in range(args.cycles):
                    result = daemon.cycle(with_store, cfg)
                    report["cycles"].append(result)
                    print(f"Cycle {index + 1}/{args.cycles}: {result['inserted']} new records; collector healthy={result['healthy']}", file=sys.stderr)
                    if index + 1 < args.cycles:
                        time.sleep(cfg["interval_seconds"])
            report["status"] = archive.status()
            report["incidents"] = archive.incidents(started, stamp(utcnow()))
            for source in report["status"]["sources"]:
                # An event subscription requires the supervised daemon and is tested separately.
                if source["source"] == "tcp:conntrack":
                    check("conntrack_subscription", False, "Use the daemon procedure in docs/live-validation.md")
                else:
                    check(source["source"], source["status"] == "ok", source["warnings"])
            checkpoint_count = with_store.db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
            check("sqlite_integrity", with_store.db.execute("PRAGMA quick_check").fetchone()[0] == "ok", "SQLite quick_check")
            check("durable_checkpoints", checkpoint_count > 0 or not (cfg["journal"] or cfg["application_logs"] or cfg["http_logs"] or cfg["adapters"]), f"{checkpoint_count} source checkpoints")
            check("no_open_probe_incidents", not report["incidents"], "Inspect incident bounds if a configured probe is unhealthy")
        finally:
            with_store.close()
    except Exception as exc:
        check("execution", False, f"{type(exc).__name__}: {exc}")
    report["finished_at"] = stamp(utcnow())
    report["passed"] = bool(report["checks"]) and all(check["passed"] for check in report["checks"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Do not overwrite symlinks; reports may contain hostnames and internal addresses.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(redact(report), stream, indent=2)
        stream.write("\n")
    print(f"{'PASS' if report['passed'] else 'REVIEW REQUIRED'}: {args.output}")
    return 0 if report["passed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
