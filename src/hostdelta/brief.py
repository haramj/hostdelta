"""Evidence-first briefing, shared by terminal and automation consumers."""

import json
from collections import Counter
from datetime import datetime

from .model import clean, compare


def build(current, baseline, journal, requests, since, until, extra_events=(), notes=()):
    diff = compare(baseline, current) if baseline else None
    events = sorted([*journal["events"], *extra_events], key=lambda e: e["at"])
    findings, coverage = [], list(notes)
    for name, domain in current["domains"].items():
        if domain["status"] != "ok":
            coverage.append(f"{name}: {domain.get('error', 'unavailable')}")
    coverage.extend(journal["warnings"])
    coverage.extend(requests["warnings"])
    if not baseline:
        coverage.append("No snapshot at or before the window start. State changes cannot be reconstructed; showing current state and retained events.")
    elif diff["skipped"]:
        coverage.append("State comparison skipped for unavailable sources or changed watch scope: " + ", ".join(diff["skipped"]))
    if baseline and baseline["at"] < since:
        coverage.append(f"State baseline is {baseline['at']}; state changes may predate the event window.")
    system = current["domains"].get("system", {})
    if system.get("status") == "ok" and system["data"].get("reboot_required"):
        findings.append({"severity": "warning", "summary": "Reboot required marker exists", "evidence": ["state:system:reboot_required"], "confidence": "observed"})
    services = current["domains"].get("services", {})
    failed = [name for name, value in services.get("data", {}).items() if value.get("active") == "failed"] if services.get("status") == "ok" else []
    if failed:
        findings.append({"severity": "critical", "summary": "Currently failed services: " + ", ".join(failed),
                         "evidence": ["state:services:" + name for name in failed], "confidence": "observed"})
    for kind, label in (("service_failure", "Service failure log records"), ("oom", "Kernel OOM log records"),
                        ("ssh_failure", "SSH authentication failure log records"), ("firewall_block", "Firewall block log records"),
                        ("network_warning", "Network warning log records"),
                        ("outage_opened", "Confirmed unhealthy episodes"),
                        ("agent_failure", "Agent-reported failures")):
        matched = [event for event in events if event["kind"] == kind]
        if matched:
            findings.append({"severity": matched[0]["severity"], "summary": f"{label}: {len(matched)}",
                             "evidence": [e["ref"] for e in matched[:20]], "confidence": "observed"})
    app_errors = [e for e in events if e["category"] == "application" and e["severity"] == "critical"]
    if app_errors:
        findings.append({"severity": "critical", "summary": f"Application error records: {len(app_errors)}",
                         "evidence": [e["ref"] for e in app_errors[:20]], "confidence": "observed"})
    if requests["errors_5xx"]:
        findings.append({"severity": "warning", "summary": f"HTTP 5xx responses: {requests['errors_5xx']} / {requests['total']}",
                         "evidence": ["requests:status_codes"], "confidence": "observed"})
    updates = [e for e in events if e["kind"] == "auto_update_activity"]
    service_events = [e for e in events if e["kind"] in ("service_lifecycle", "service_failure", "network_warning")]
    nearby = None
    for update in updates:
        match = next((e for e in service_events if abs((datetime.fromisoformat(e["at"]) - datetime.fromisoformat(update["at"])).total_seconds()) <= 300), None)
        if match:
            nearby = (update, match)
            break
    if nearby:
        findings.append({"severity": "info", "summary": "Automatic-update activity and service/network events occurred within 5 minutes. Causation is unconfirmed.",
                         "evidence": [e["ref"] for e in nearby], "confidence": "temporal_correlation"})
    counts = dict(Counter(e["kind"] for e in events))
    rank = {"info": 0, "warning": 1, "critical": 2}
    findings.sort(key=lambda item: -rank[item["severity"]])
    return {"schema_version": 1, "type": "brief", "host": current["host"],
            "window": {"since": since, "until": until}, "state_diff": diff,
            "current_state": current, "events": events, "event_counts": counts, "requests": requests,
            "findings": findings, "severity": max((f["severity"] for f in findings), key=lambda s: rank[s], default="info"),
            "coverage": {"journal_status": journal["status"], "warnings": list(dict.fromkeys(coverage))},
            "host_mutations": False, "local_state_updated": False}


def render_diff(diff, full=False):
    lines = [f"State comparison: {diff['from']} → {diff['to']}"]
    for domain, rows in diff["changes"].items():
        counts = Counter(row["kind"] for row in rows)
        lines.append(f"  {domain.upper()}: " + ", ".join(f"{n} {k}" for k, n in counts.items()))
        for row in rows[:None if full else 5]:
            detail = ""
            if domain != "files":
                detail = f"  {json.dumps(row['before'], ensure_ascii=False)} → {json.dumps(row['after'], ensure_ascii=False)}"
            lines.append(f"    {row['kind']:7} {row['key']}{detail}")
        if not full and len(rows) > 5:
            lines.append(f"    … {len(rows) - 5} more; use --full")
    if not diff["changes"]:
        lines.append("  No changes in comparable collected state.")
    if diff["skipped"]:
        lines.append("  Not compared: " + ", ".join(diff["skipped"]))
    return lines


def render(report, full=False):
    lines = ["HostDelta — catch up on your server", f"Host: {report['host']}",
             f"Since: {report['window']['since']}", f"Until: {report['window']['until']}", "", "ATTENTION"]
    for finding in report["findings"]:
        lines.append(f"  [{finding['severity'].upper()}] {finding['summary']}")
        if full:
            lines.append("    Evidence: " + ", ".join(finding["evidence"]))
    if not report["findings"]:
        lines.append("  No supported alerts found in collected evidence. See coverage below.")
    if report["state_diff"]:
        lines.extend(["", *render_diff(report["state_diff"], full)])
    req = report["requests"]
    lines.extend(["", "INBOUND HTTP REQUESTS"])
    if req["status"] == "ok":
        lines.append(f"  {req['total']} requests · {req['errors_5xx']} server errors · {req['slow_requests']} ≥1s (when timing is logged)")
        lines.append("  Responses: " + (", ".join(f"{k}: {v}" for k, v in req["status_codes"].items()) or "none in window"))
        for label, field in (("Paths", "top_paths"), ("Clients", "top_clients")):
            lines.append(f"  {label}:")
            lines.extend(f"    {count:>6}  {key}" for key, count in req[field][:10 if full else 3])
    else:
        lines.append(f"  {req['status']}; see coverage.")
    if report.get("incidents"):
        lines.extend(["", "HEALTH INCIDENTS (SAMPLED BOUNDS)"])
        for incident in report["incidents"][:None if full else 10]:
            lines.append(f"  {incident['entity']}: {incident['opened_at']} → {incident['closed_at'] or 'OPEN'}")
            lines.append(f"    start={incident['start_bounds']} end={incident['end_bounds']} coverage_gap={incident['coverage_gap']}")
    app = [e for e in report["events"] if e["category"] == "application"]
    if app:
        lines.extend(["", "APPLICATIONS"])
        counts = Counter((e.get("service", "application"), e["severity"]) for e in app)
        lines.extend(f"  {service}: {count} {severity} records" for (service, severity), count in counts.most_common(20))
    lines.extend(["", "EVENT TIMELINE"])
    if report["event_counts"]:
        lines.append("  " + ", ".join(f"{key}: {count}" for key, count in report["event_counts"].items()))
    selected = report["events"] if full else report["events"][-12:]
    lines.extend(f"  {e['at']} [{e['category']}] {e['summary']}" for e in selected)
    if not selected:
        lines.append("  No supported events found in available records.")
    if not full and len(report["events"]) > 12:
        lines.append(f"  Showing newest 12 of {len(report['events'])} events; use --full.")
    lines.extend(["", "COVERAGE"])
    lines.extend("  • " + warning for warning in report["coverage"]["warnings"])
    return "\n".join(clean(line, 1500 if full else 500) for line in lines)
