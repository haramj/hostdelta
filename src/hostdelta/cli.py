"""CLI contract: JSON stdout, errors on stderr, no implicit cursor acknowledgement."""

import argparse
import fcntl
import json
import os
import platform
import re
import shlex
import shutil
import sqlite3
import sys
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from . import __version__, config, daemon
from .archive import Archive
from .brief import build, render, render_diff
from .collect import capture
from .demo import report as demo_report
from .events import journal
from .model import clean, compare, parse_time, stamp, utcnow
from .requests import analyze, discover
from .store import DEFAULT_WATCH, Store, default_dir


def name(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        raise argparse.ArgumentTypeError("Use 1–64 letters, digits, dots, underscores or hyphens.")
    return value


def parser(json_errors=False):
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            if json_errors:
                self.exit(1, json.dumps({"schema_version": 1, "type": "error", "error": message}) + "\n")
            super().error(message)
    p = Parser(prog="hostdelta", description="Catch up on your server. Local evidence, readable briefs, agent-friendly JSON.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--state-dir", type=Path, default=default_dir(), help="Private state directory (or HOSTDELTA_STATE_DIR)")
    sub = p.add_subparsers(dest="command")
    def output(command, help, full=False):
        s = sub.add_parser(command, help=help)
        s.add_argument("--json", action="store_true", help="Versioned JSON output")
        if full:
            s.add_argument("--full", action="store_true", help="Show all state changes and supported events")
        return s
    init = output("init", "Create a baseline; optionally configure watched files and HTTP logs")
    init.add_argument("--watch", action="append", metavar="ABSOLUTE_PATH", help="Replace watch list; repeat for multiple paths")
    init.add_argument("--access-log", action="append", metavar="ABSOLUTE_PATH", help="Replace access-log list; repeat for multiple paths")
    snap = output("snapshot", "Save a current state checkpoint")
    snap.add_argument("--label")
    output("snapshots", "List newest 100 checkpoints")
    diff = output("diff", "Compare saved snapshots; defaults to HEAD~1 and HEAD", True)
    diff.add_argument("before", nargs="?", default="HEAD~1")
    diff.add_argument("after", nargs="?", default="HEAD")
    for command, help in (("brief", "Brief since previous tracked SSH session or a specified time"),
                          ("session", "Print brief and record successful session start; used by SSH hook")):
        s = output(command, help, True)
        if command == "brief":
            s.add_argument("--archive", action="store_true", help="Read durable events and latest snapshot without live collection")
            group = s.add_mutually_exclusive_group()
            group.add_argument("--since", default="last-login", help="last-login, 2d, 8h, or ISO timestamp")
            group.add_argument("--consumer", type=name, help="Read this agent's independent acknowledged cursor")
        else:
            s.set_defaults(since="last-login", consumer=None, archive=False)
        s.add_argument("--fail-on", choices=["warning", "critical"], help="Exit 2 when findings meet this threshold")
        s.add_argument("--require-coverage", action="store_true", help="Exit 3 on detected collection gaps; retention completeness is never guaranteed")
    ack = output("ack", "Advance an agent's cursor after processing a report")
    ack.add_argument("--consumer", required=True, type=name)
    ack.add_argument("--until", required=True, help="Use window.until from the successfully processed report")
    record = output("record", "Record an agent's self-reported lifecycle event")
    record.add_argument("--actor", required=True, type=name)
    record.add_argument("--kind", required=True, choices=["start", "finish", "failure"])
    record.add_argument("--message", default="", help="Short non-secret summary; raw commands are unnecessary")
    record.add_argument("--run-id", type=name, help="Optional correlation ID")
    output("doctor", "Inspect platform, commands and configured collection readiness")
    hook = sub.add_parser("hook", help="Print an opt-in Bash/Zsh SSH hook; never edits shell files")
    hook.add_argument("shell", choices=["bash", "zsh"])
    output("demo", "Show a synthetic incident; no host access or initialization", True)
    config_cmd = output("config", "Print defaults or validate a collector configuration")
    config_cmd.add_argument("--check", type=Path)
    for command in ("collect", "daemon"):
        command_parser = output(command, "Run one collection cycle" if command == "collect" else "Run the foreground collector until SIGTERM")
        command_parser.add_argument("--config", type=Path, required=True)
    output("status", "Show durable collector status and source coverage")
    for command in ("events", "incidents"):
        command_parser = output(command, "Query retained " + command)
        command_parser.add_argument("--since", default="24h")
        if command == "events":
            command_parser.add_argument("--service")
            command_parser.add_argument("--severity", choices=["info", "warning", "critical"])
            command_parser.add_argument("--trace-id")
            command_parser.add_argument("--limit", type=int, default=1000)
    prune = output("prune", "Apply retention while preserving named checkpoints and open incidents")
    prune.add_argument("--keep-days", type=int, default=30)
    prune.add_argument("--dry-run", action="store_true")
    return p


def emit(payload, as_json, human):
    if as_json:
        print(json.dumps(payload, ensure_ascii=True, indent=2, allow_nan=False))
    else:
        print(human)


@contextmanager
def session_lock(store):
    path = store.directory / "session.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(fd)


def briefing(args, store, session=False):
    now = utcnow()
    fallback = now - timedelta(days=1)
    notes = []
    if args.consumer:
        raw = store.setting("consumer:" + args.consumer)
        since = parse_time(raw) if raw else fallback
        if not raw:
            notes.append(f"Consumer {args.consumer} has no acknowledged cursor; using the last 24 hours.")
    elif args.since == "last-login":
        raw = store.last_session() if session else store.last_since()
        since = parse_time(raw) if raw else fallback
        if not raw:
            notes.append("No previous tracked SSH session; using the last 24 hours. Install the opt-in hook to track future sessions.")
    else:
        since = parse_time(args.since, now)
    if since > now:
        raise ValueError("Window start is in the future; check the input or system clock.")
    archive = Archive(store)
    archived = getattr(args, "archive", False)
    current = store.resolve("HEAD") if archived else capture(store.setting("watch", DEFAULT_WATCH))
    until = utcnow()
    baseline = store.baseline(stamp(since))
    retained, retained_truncated = archive.query(stamp(since), stamp(until), limit=10000, exclude_http=True)
    if archived:
        runs = archive.coverage(stamp(since), stamp(until))
        logs = {"events": [], "status": "ok" if runs else "unavailable", "warnings": ["Archive reads do not collect live state. Review snapshot age, source runs, and collector heartbeat."]}
        requests = archive.http_summary(stamp(since), stamp(until))
        configured = store.setting("collector_config", {})
        if not configured.get("http_logs"):
            requests["status"] = "not_configured"
            requests["warnings"].append("Durable HTTP collection is not configured.")
    else:
        logs = journal(since, until)
        # Avoid reporting the same journald evidence twice after durable ingestion.
        cursors = {e.get("journal_cursor") for e in retained if e.get("journal_cursor")}
        logs["events"] = [e for e in logs["events"] if e.get("ref") not in cursors]
        requests = analyze(store.setting("access_logs", []), since, until)
    extra, truncated = store.events(stamp(since), stamp(until))
    if truncated:
        notes.append("Local agent events capped at 10000; later records omitted.")
    if retained_truncated:
        notes.append("Retained event timeline capped at 10000; query narrower windows with events.")
    floor = store.setting("retention_floor")
    if floor and stamp(since) < floor:
        notes.append("Requested window begins before the retention floor: " + floor)
    coverage = archive.coverage(stamp(since), stamp(until))
    if any(r["status"] != "ok" for r in coverage):
        notes.append("Durable collectors recorded partial or unavailable sources in this window; inspect status.")
    result = build(current, baseline, logs, requests, stamp(since), stamp(until), [*extra, *retained], notes)
    result["incidents"] = archive.incidents(stamp(since), stamp(until))
    result["coverage"]["source_runs"] = coverage
    result["coverage"]["collector"] = store.setting("daemon", {})
    # Saving observations does not acknowledge an agent cursor.
    result["snapshot_id"] = None if archived else store.save(current)
    result["local_state_updated"] = not archived
    result["consumer"] = args.consumer
    result["coverage"]["collection_ok"] = (
        baseline is not None and not result["state_diff"]["skipped"] and
        all(d["status"] == "ok" for d in current["domains"].values()) and
        logs["status"] == "ok" and len(logs["warnings"]) <= 1 and not truncated and
        requests["status"] == "ok" and len(requests["warnings"]) <= 1 and not retained_truncated and
        not any(r["status"] != "ok" for r in coverage) and not (floor and stamp(since) < floor)
    )
    if archived:
        heartbeat = store.setting("daemon", {})
        last = heartbeat.get("last_cycle_at")
        fresh = bool(last) and (until - parse_time(last)).total_seconds() <= heartbeat.get("interval_seconds", 30) * 3
        result["coverage"]["collection_ok"] = result["coverage"]["collection_ok"] and fresh and not heartbeat.get("stopped_at")
    if session:
        store.begin_session(stamp(now), stamp(fallback))
    emit(result, args.json, render(result, args.full))
    if args.require_coverage and not result["coverage"]["collection_ok"]:
        return 3
    rank = {"info": 0, "warning": 1, "critical": 2}
    return 2 if args.fail_on and rank[result["severity"]] >= rank[args.fail_on] else 0


def execute(args):
    if args.command == "config":
        data = config.load(args.check) if args.check else config.DEFAULT
        emit({"schema_version": 1, "type": "config", "config": data}, args.json, json.dumps(data, indent=2))
        return 0
    if args.command == "demo":
        result = demo_report()
        emit(result, args.json, render(result, args.full))
        return 0
    if args.command == "hook":
        executable = shutil.which("hostdelta")
        if not executable:
            raise ValueError("Install hostdelta first so the hook can use an absolute executable path.")
        invocation = f"{shlex.quote(executable)} --state-dir {shlex.quote(str(args.state_dir.expanduser().absolute()))} session"
        print(f'''# HostDelta: add to ~/.bashrc or ~/.zshrc after reviewing.
# Linux coreutils timeout bounds login latency. Missing timeout skips the hook.
if [ -n "${{SSH_CONNECTION:-}}" ] && [ -z "${{HOSTDELTA_SESSION_SEEN:-}}" ]; then
  case $- in
    *i*)
      if [ -t 1 ] && command -v timeout >/dev/null 2>&1; then
        export HOSTDELTA_SESSION_SEEN=1
        timeout --kill-after=1s 8s {invocation} || :
      fi
      ;;
  esac
fi''')
        return 0
    if args.command == "doctor":
        commands = {c: shutil.which(c) for c in ("journalctl", "systemctl", "ip", "dpkg-query", "rpm", "timeout")}
        data = {"schema_version": 1, "type": "doctor", "platform": platform.system(), "commands": commands,
                "state_dir": str(args.state_dir), "initialized": (args.state_dir / "state.sqlite3").is_file(),
                "live_collection_supported": platform.system() == "Linux"}
        data["ready"] = data["live_collection_supported"] and data["initialized"] and all(commands[c] for c in ("journalctl", "systemctl", "ip")) and bool(commands["dpkg-query"] or commands["rpm"])
        data["note"] = "Command availability does not verify log permissions or retention; inspect brief.coverage."
        emit(data, args.json, "\n".join(clean(f"{key}: {value}", 1000) for key, value in data.items()))
        return 0 if data["ready"] else 3
    # Refuse unsupported live initialization before creating any files.
    if args.command == "init" and platform.system() != "Linux":
        raise ValueError("Live collection requires Linux. Try hostdelta demo --full on macOS.")
    store = Store(args.state_dir, create=args.command == "init")
    try:
        if args.command in ("collect", "daemon"):
            data = daemon.run(store, config.load(args.config), once=args.command == "collect")
            if data is not None:
                emit(data, args.json, json.dumps(data, indent=2))
                return 0 if data["healthy"] else 3
            return 0
        if args.command == "status":
            data = Archive(store).status()
            emit({"schema_version": 1, "type": "status", **data}, args.json, json.dumps(data, indent=2))
            return 0 if data["ready"] else 3
        if args.command in ("events", "incidents"):
            until = utcnow()
            since = parse_time(args.since, until)
            if since > until:
                raise ValueError("Window start is in the future")
            archive = Archive(store)
            if args.command == "events":
                if not 1 <= args.limit <= 10000:
                    raise ValueError("Event limit must be in 1..10000")
                rows, truncated = archive.query(stamp(since), stamp(until), args.service, args.severity, args.trace_id, args.limit)
            else:
                rows, truncated = archive.incidents(stamp(since), stamp(until)), False
            emit({"schema_version": 1, "type": args.command, "window": {"since": stamp(since), "until": stamp(until)}, args.command: rows, "truncated": truncated}, args.json,
                 json.dumps(rows, indent=2, ensure_ascii=True))
            return 0
        if args.command == "prune":
            if not 1 <= args.keep_days <= 3650:
                raise ValueError("Retention must be in 1..3650 days")
            with daemon.collector_lock(store):
                data = Archive(store).prune(args.keep_days, args.dry_run)
            emit({"schema_version": 1, "type": "retention", **data}, args.json, json.dumps(data, indent=2))
            return 0
        if args.command == "init":
            watch = args.watch if args.watch is not None else store.setting("watch", DEFAULT_WATCH)
            access = args.access_log if args.access_log is not None else store.setting("access_logs")
            if access is None:
                access = discover()
            if any(not Path(path).is_absolute() for path in watch + access):
                raise ValueError("Watch and access-log paths must be absolute.")
            result = capture(watch)
            store.set_setting("watch", sorted(set(watch)))
            store.set_setting("access_logs", sorted(set(access)))
            index = store.save(result)
            errors = {key: d["error"] for key, d in result["domains"].items() if d["status"] != "ok"}
            payload = {"schema_version": 1, "type": "init", "snapshot_id": index, "state_dir": str(store.directory), "watch": watch, "access_logs": access, "unavailable": errors}
            emit(payload, args.json, f"Initialized HostDelta. Baseline #{index}\nState: {store.directory}\n" + ("Unavailable: " + clean(errors, 1000) if errors else "Run hostdelta brief, or hostdelta hook bash to set up SSH briefings."))
            return 0
        if args.command == "snapshot":
            result = capture(store.setting("watch", DEFAULT_WATCH))
            index = store.save(result, args.label)
            emit({"schema_version": 1, "type": "snapshot", "id": index, "snapshot": result}, args.json, f"Saved snapshot #{index} at {result['at']}")
        elif args.command == "snapshots":
            rows = store.snapshots()
            emit({"schema_version": 1, "type": "snapshots", "snapshots": rows}, args.json, "\n".join(f"{r['id']:>6}  {r['at']}  {clean(r['label'] or '')}" for r in rows))
        elif args.command == "diff":
            result = compare(store.resolve(args.before), store.resolve(args.after))
            emit({"schema_version": 1, "type": "diff", **result}, args.json, "\n".join(clean(line, 1500) for line in render_diff(result, args.full)))
        elif args.command == "brief":
            return briefing(args, store)
        elif args.command == "session":
            with session_lock(store) as acquired:
                if acquired:
                    return briefing(args, store, session=True)
        elif args.command == "ack":
            if re.fullmatch(r"\d+[smhdw]", args.until):
                raise ValueError("Acknowledgement requires the exact ISO timestamp in report.window.until.")
            until = parse_time(args.until)
            if until > utcnow():
                raise ValueError("Cannot acknowledge a future timestamp.")
            store.ack(args.consumer, stamp(until))
            emit({"schema_version": 1, "type": "ack", "consumer": args.consumer, "until": stamp(until)}, args.json, f"Acknowledged {args.consumer} through {stamp(until)}")
        elif args.command == "record":
            event = {"at": stamp(utcnow()), "category": "agents", "kind": "agent_" + args.kind,
                     "severity": "critical" if args.kind == "failure" else "info", "source": "local_agent", "actor": args.actor,
                     "run_id": args.run_id, "summary": f"{args.actor}: {args.kind}" + (" — " + clean(args.message, 500) if args.message else "")}
            index = store.record(event)
            emit({"schema_version": 1, "type": "record", "id": index, "event": event}, args.json, f"Recorded agent event #{index}")
        return 0
    finally:
        store.close()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Bare invocation is the human default. Global arguments must precede subcommands.
    if not argv:
        argv = ["brief"]
    p = parser(json_errors="--json" in argv)
    args = p.parse_args(argv)
    if args.command is None:
        p.print_help()
        return 1
    try:
        return execute(args)
    except (ValueError, OSError, sqlite3.Error) as exc:
        payload = {"schema_version": 1, "type": "error", "error": clean(exc, 1000)}
        print(json.dumps(payload) if getattr(args, "json", False) else f"hostdelta: {payload['error']}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
