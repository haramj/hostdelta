"""Portable data model and deterministic comparison; no collectors or side effects."""

import re
import unicodedata
from datetime import datetime, timedelta, timezone


def utcnow():
    return datetime.now(timezone.utc)


def stamp(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_time(value, now=None):
    now = now or utcnow()
    match = re.fullmatch(r"(\d+)([smhdw])", value)
    if match:
        try:
            return now - timedelta(seconds=int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match[2]])
        except OverflowError as exc:
            raise ValueError("Duration exceeds the supported date range.") from exc
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Use a duration (2d, 8h) or ISO date/time (2026-09-20T10:00+09:00).") from exc
    # Naive user input is local time. All stored timestamps are UTC.
    return dt.astimezone(timezone.utc)


def clean(value, limit=240):
    """Do not replay terminal escapes, newlines or bidi controls from host logs."""
    value = str(value)
    text = "".join(" " if unicodedata.category(c).startswith("C") else c for c in value)
    return text[:limit] + ("…" if len(text) > limit else "")


def compare(before, after):
    changes, skipped = {}, []
    for name in sorted(set(before["domains"]) | set(after["domains"])):
        a, b = before["domains"].get(name), after["domains"].get(name)
        if not a or not b or a["status"] != "ok" or b["status"] != "ok":
            skipped.append(name)
            continue
        if name == "files" and a.get("scope") != b.get("scope"):
            skipped.append(name)
            continue
        old, new = a["data"], b["data"]
        rows = []
        for key in sorted(set(old) | set(new)):
            if key not in old:
                rows.append({"kind": "added", "key": key, "before": None, "after": new[key]})
            elif key not in new:
                rows.append({"kind": "removed", "key": key, "before": old[key], "after": None})
            elif old[key] != new[key]:
                rows.append({"kind": "changed", "key": key, "before": old[key], "after": new[key]})
        if rows:
            changes[name] = rows
    return {"from": before["at"], "to": after["at"], "changes": changes, "skipped": skipped}
