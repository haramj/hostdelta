"""Foreground collector for systemd: single writer, graceful shutdown, durable checkpoints."""

import fcntl
import hashlib
import os
import signal
import threading
import time
from contextlib import contextmanager
from datetime import timedelta

from . import adapters, conntrack, health, tail
from .archive import Archive
from .collect import capture
from .events import journal
from .model import parse_time, stamp, utcnow
from .store import DEFAULT_WATCH
from .telemetry import StructuredLogger


@contextmanager
def collector_lock(store):
    fd = os.open(store.directory / "collector.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("A collector already owns this state directory") from None
        yield
    finally:
        os.close(fd)


def cycle(store, config, logger=None, stream=None, stop=None):
    archive = Archive(store)
    now = utcnow()
    sources = []
    def active():
        return stop is None or not stop.is_set()
    def persist(source, result):
        count = archive.ingest(source, result.get("events", []), result.get("checkpoint"), result["status"],
                               result.get("warnings", []), result.get("observations", []), config)
        sources.append({"source": source, "status": result["status"], "inserted": count, "warnings": result.get("warnings", [])})
        if logger:
            logger.event("collector.source.completed", attributes=sources[-1])
    if config["journal"] and active():
        previous = archive.checkpoint("journald")
        # Overlap catches some delayed writes; journal cursors deduplicate retained events.
        since = parse_time(previous["until"]) - timedelta(seconds=60) if previous.get("until") else now - timedelta(hours=24)
        result = journal(since, now)
        for event in result["events"]:
            event["journal_cursor"] = event["ref"]
            event["event_id"] = hashlib.sha256(("journal:" + (event["ref"] or str(event))).encode()).hexdigest()
        if result["status"] == "ok":
            result["checkpoint"] = {"until": stamp(now)}
            # Retention caveat is informational; all other warnings are detectable gaps.
            if len(result["warnings"]) > 1:
                result["status"] = "partial"
        persist("journald", result)
    for kind, paths in (("application", config["application_logs"]), ("http", config["http_logs"])):
        for path in paths:
            if not active():
                break
            source = f"{kind}:{path}"
            persist(source, tail.read(path, archive.checkpoint(source), kind, now))
    if config["services"] and active():
        persist("systemd:health", health.service_observations(config["services"], now))
    if config["tcp"]["enabled"] and active():
        if config["tcp"].get("mode", "sample") == "sample":
            persist("tcp:sample", health.tcp(archive.checkpoint("tcp:sample"), now=now))
        else:
            persist("tcp:conntrack", stream.drain() if stream else {"events": [], "status": "unavailable", "warnings": ["conntrack requires a running daemon, not a single collection cycle"]})
    for adapter in config["adapters"]:
        if not active():
            break
        source = "adapter:" + adapter["name"]
        persist(source, adapters.bounded_poll(adapter, archive.checkpoint(source)))
    last_snapshot = store.setting("collector_snapshot_at")
    if active() and (not last_snapshot or (now - parse_time(last_snapshot)).total_seconds() >= config["snapshot_interval_seconds"]):
        try:
            snapshot = capture(store.setting("watch", DEFAULT_WATCH))
            store.save(snapshot)
            store.set_setting("collector_snapshot_at", stamp(now))
            errors = [name for name, domain in snapshot["domains"].items() if domain["status"] != "ok"]
            persist("state:snapshot", {"events": [], "status": "partial" if errors else "ok", "warnings": errors})
        except ValueError as exc:
            persist("state:snapshot", {"events": [], "status": "unavailable", "warnings": [str(exc)]})
    last_prune = store.setting("collector_pruned_at")
    if not last_prune or (now - parse_time(last_prune)).total_seconds() >= 3600:
        archive.prune(config["retention_days"])
        store.set_setting("collector_pruned_at", stamp(now))
    report = {"schema_version": 1, "type": "collection", "at": stamp(now), "sources": sources,
              "inserted": sum(s["inserted"] for s in sources), "healthy": active() and all(s["status"] == "ok" for s in sources), "interrupted": not active()}
    store.set_setting("collector_config", config)
    store.set_setting("daemon", {"last_cycle_at": stamp(utcnow()), "interval_seconds": config["interval_seconds"],
                                  "healthy": report["healthy"], "pid": os.getpid()})
    return report


def run(store, config, once=False):
    with collector_lock(store):
        if once:
            return cycle(store, config)
        stop = threading.Event()
        previous = {}
        stream = None
        logger = StructuredLogger("hostdelta.collector", stream=__import__("sys").stderr, version="0.2.0")
        def shutdown(signum, frame):
            stop.set()
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, shutdown)
        try:
            logger.event("collector.started", attributes={"interval_seconds": config["interval_seconds"]})
            while not stop.is_set():
                started = time.monotonic()
                if config["tcp"]["enabled"] and config["tcp"].get("mode") == "conntrack":
                    if stream is None or (stream.process and stream.process.poll() is not None):
                        if stream:
                            stream.close()
                        stream = conntrack.Stream()
                cycle(store, config, logger, stream, stop)
                stop.wait(max(0, config["interval_seconds"] - (time.monotonic() - started)))
        finally:
            if stream:
                stream.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            state = store.setting("daemon", {})
            state["stopped_at"] = stamp(utcnow())
            store.set_setting("daemon", state)
            logger.event("collector.stopped")
