"""Extract supported event types; preserve source references, not raw secret-bearing logs."""

import json
import os
import re
import subprocess
from datetime import datetime, timezone

from .model import stamp
from .process import bounded_run

JOURNAL_LIMIT = 10000


def classify(row):
    message = row.get("MESSAGE", "")
    if not isinstance(message, str):
        return None
    lower = message.lower()
    unit = row.get("_SYSTEMD_UNIT", "")
    ident = row.get("SYSLOG_IDENTIFIER", "")
    category, kind, severity, summary = None, None, "info", None
    # Never include raw sudo COMMAND= or SSH usernames: those can contain secrets.
    if ident in ("sshd", "sshd-session") and lower.startswith("accepted "):
        category, kind, summary = "access", "ssh_success", "SSH authentication accepted"
    elif ident in ("sshd", "sshd-session") and ("failed password" in lower or "invalid user" in lower or "authentication failure" in lower):
        category, kind, severity, summary = "access", "ssh_failure", "warning", "SSH authentication failure recorded"
    elif ident == "sudo" and "COMMAND=" in message:
        category, kind, summary = "access", "sudo", "sudo command recorded (arguments omitted)"
    elif row.get("_TRANSPORT") == "kernel" and ("out of memory:" in lower or "oom-kill:" in lower or "killed process" in lower):
        category, kind, severity, summary = "system", "oom", "critical", "Kernel out-of-memory event"
    elif row.get("_TRANSPORT") == "kernel" and re.search(r"\bIN=\S+", message) and "SRC=" in message and any(x in lower for x in ("drop", "reject", "block")):
        category, kind, severity, summary = "network", "firewall_block", "warning", "Firewall blocked inbound traffic (log record)"
    elif ident == "systemd" and ("failed with result" in lower or lower.startswith("failed to start ")):
        category, kind, severity = "services", "service_failure", "critical"
        summary = message[:240]
    elif ident == "systemd" and lower.startswith(("started ", "stopped ", "starting ", "stopping ")):
        category, kind, summary = "services", "service_lifecycle", message[:240]
    elif "unattended-upgrade" in str(unit) or "unattended-upgrade" in str(ident):
        category, kind, summary = "system", "auto_update_activity", "Unattended-upgrades emitted a log record"
    elif ident in ("systemd-networkd", "NetworkManager") and any(s in lower for s in ("lost carrier", "link down", "disconnected", "failed")):
        category, kind, severity, summary = "network", "network_warning", "warning", message[:240]
    if not category:
        return None
    try:
        at = datetime.fromtimestamp(int(row["__REALTIME_TIMESTAMP"]) / 1_000_000, timezone.utc)
    except (KeyError, ValueError, TypeError, OverflowError, OSError):
        return None
    return {"at": stamp(at), "category": category, "kind": kind, "severity": severity,
            "summary": summary, "unit": unit, "source": "journald", "ref": row.get("__CURSOR", "")}


def journal(since, until):
    warnings = ["Journal visibility depends on user permissions and retention; absence of events does not prove inactivity."]
    args = ["journalctl", "--no-pager", "--output=json", "--reverse", f"--lines={JOURNAL_LIMIT}",
            f"--since=@{since.timestamp():.6f}", f"--until=@{until.timestamp():.6f}"]
    try:
        result = bounded_run(args, timeout=8, env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"events": [], "status": "unavailable", "warnings": warnings + [str(exc)]}
    if result.returncode:
        return {"events": [], "status": "unavailable", "warnings": warnings + [result.stderr.strip()[:400]]}
    if result.stderr.strip():
        warnings.append(result.stderr.strip()[:400])
    lines = result.stdout.splitlines()
    if len(lines) >= JOURNAL_LIMIT:
        warnings.append(f"Journal capped at newest {JOURNAL_LIMIT} records; earlier events may be omitted.")
    events, bad = [], 0
    for line in lines:
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("not an object")
            event = classify(row)
            if event and stamp(since) < event["at"] <= stamp(until):
                events.append(event)
        except (ValueError, TypeError):
            bad += 1
    if bad:
        warnings.append(f"Skipped {bad} malformed journal records.")
    return {"events": sorted(events, key=lambda e: e["at"]), "status": "ok", "warnings": warnings}
