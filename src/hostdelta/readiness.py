"""Read-only freshness assessment; never equates missing evidence with health."""

from datetime import datetime


def observation(at, now, maximum_age, status="ok"):
    if not at:
        return {"fresh": False, "age_seconds": None, "reason": "never_collected"}
    try:
        if not isinstance(at, str):
            raise ValueError("Stored timestamp must be a string")
        observed = datetime.fromisoformat(at.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError("Stored timestamp must include a timezone")
        age = (now - observed).total_seconds()
    except (ValueError, TypeError, OverflowError):
        return {"fresh": False, "age_seconds": None, "reason": "invalid_timestamp"}
    if age < 0:
        reason = "clock_skew"
    elif age > maximum_age:
        reason = "stale"
    elif status != "ok":
        reason = "source_unhealthy"
    else:
        reason = "ok"
    return {"fresh": 0 <= age <= maximum_age, "age_seconds": age, "reason": reason}


def assess(sources, heartbeat, config, now):
    interval = config.get("interval_seconds", 30)
    expected = {"state:snapshot": max(interval, config.get("snapshot_interval_seconds", 900)) * 3}
    if config.get("journal", True):
        expected["journald"] = interval * 3
    for kind, field in (("application", "application_logs"), ("http", "http_logs")):
        expected.update({f"{kind}:{path}": interval * 3 for path in config.get(field, [])})
    if config.get("services"):
        expected["systemd:health"] = interval * 3
    tcp = config.get("tcp", {})
    if tcp.get("enabled"):
        expected["tcp:" + tcp.get("mode", "sample")] = interval * 3
    expected.update({"adapter:" + item["name"]: interval * 3 for item in config.get("adapters", [])})
    rows = {row["source"]: row for row in sources}
    result = []
    for source in sorted(set(rows) | set(expected)):
        row = dict(rows.get(source, {"source": source, "at": None, "status": "unavailable", "warnings": []}))
        configured = source in expected
        row.update(observation(row.get("at"), now, expected.get(source, interval * 3), row["status"]))
        row.update(configured=configured, max_age_seconds=expected.get(source))
        if not configured:
            row["reason"] = "not_configured"
        result.append(row)
    beat = observation(heartbeat.get("last_cycle_at"), now, interval * 3)
    if heartbeat.get("stopped_at"):
        beat.update(fresh=False, reason="stopped")
    elif beat["reason"] == "ok" and not heartbeat.get("healthy"):
        beat["reason"] = "last_cycle_unhealthy"
    ready = beat["reason"] == "ok" and all(row["reason"] == "ok" for row in result if row["configured"])
    return {"sources": result, "fresh": beat["fresh"], "ready": ready, "heartbeat_assessment": beat}
