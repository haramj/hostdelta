#!/usr/bin/env python3
"""A runnable application instrumentation example; emits real JSONL to stdout."""
import logging

from hostdelta.telemetry import StructuredLogger, bind_context

log = StructuredLogger("backup-worker", environment="development", version="1.0.0")
with bind_context(run_id="backup-42", attributes={"region": "lab"}):
    with log.operation("backup.verify", resource="volume-123"):
        log.request("GET", "/v1/volumes/volume-123?token=not-retained", 200, 42.5)
        log.event("backup.checksum.valid", "Checksum verified", attributes={"bytes": 1048576})
log.event("worker.ready", "Ready for the next job", level=logging.INFO)
