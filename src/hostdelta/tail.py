"""Incremental JSONL/access-log reader with durable inode+offset checkpoints.

Rotation and copytruncate are best effort, explicitly reported when data is lost.
The caller must commit the returned records and checkpoint in one transaction.
"""

import hashlib
import json
import os
import stat
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from .model import utcnow
from .requests import parse_line
from .telemetry import normalize

MAX_LINE = 64 * 1024
MAX_BYTES = 4 * 1024 * 1024
MAX_RECORDS = 5000


def signature(stream, offset):
    stream.seek(max(0, offset - 64))
    return hashlib.sha256(stream.read(min(64, offset))).hexdigest()


def parse_http(text, source, identity):
    request = parse_line(text)
    event_id = hashlib.sha256(identity.encode()).hexdigest()
    return {"event_id": event_id, "ref": "event:" + event_id, "at": request["at"], "source": source,
            "category": "requests", "kind": "http_request", "severity": "warning" if request["status"] >= 400 else "info",
            "summary": f"{request['method']} {request['path']} → {request['status']}",
            "attributes": request}


def read(path, checkpoint=None, kind="application", now=None):
    path = Path(path)
    source = f"{kind}:{path}"
    checkpoint = json.loads(json.dumps(checkpoint or {}))
    now = now or utcnow()
    state = checkpoint.get("file")
    warnings, result = [], []
    consumed = 0
    # The active file and the last inode are enough for continuous logrotate.
    # Multi-rotation gaps are surfaced rather than silently scanning arbitrary history.
    candidates = [path]
    if state:
        try:
            candidates += sorted(path.parent.glob(path.name + ".*"))[:32]
        except OSError:
            pass
        old = None
        for candidate in candidates:
            try:
                info = candidate.stat()
                if [info.st_dev, info.st_ino] == state["identity"]:
                    old = candidate
                    break
            except OSError:
                pass
        if old is not None and old != path:
            candidates = [old, path]
        elif old is None:
            warnings.append("Previous log inode is unavailable (rotation/removal); unread bytes may be lost.")
            candidates = [path]
            state = None
        else:
            candidates = [path]
    for candidate in candidates:
        try:
            fd = os.open(candidate, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("Log must be a regular file, not a pipe or device")
                identity = [info.st_dev, info.st_ino]
                if not state or state["identity"] != identity:
                    state = {"identity": identity, "offset": 0, "generation": uuid.uuid4().hex}
                elif info.st_size < state["offset"] or (state.get("signature") and signature(stream, state["offset"]) != state["signature"]):
                    warnings.append("Log was truncated or rewritten; starting a new generation. Unread bytes may be lost.")
                    state = {"identity": identity, "offset": 0, "generation": uuid.uuid4().hex}
                stream.seek(state["offset"])
                bad = 0
                while consumed < MAX_BYTES and len(result) < MAX_RECORDS:
                    offset = stream.tell()
                    line = stream.readline(MAX_LINE + 1)
                    if not line:
                        break
                    consumed += len(line)
                    if len(line) > MAX_LINE:
                        # Discard the whole oversized record across cycles, never parse its suffix.
                        state["discard"] = not line.endswith(b"\n")
                        state["offset"] = stream.tell()
                        bad += 1
                        continue
                    if state.get("discard"):
                        state["discard"] = not line.endswith(b"\n")
                        state["offset"] = stream.tell()
                        continue
                    if not line.endswith(b"\n"):
                        if candidate != path:
                            warnings.append("Rotated log ended with an incomplete record; record omitted.")
                            state["offset"] = stream.tell()
                        else:
                            stream.seek(offset)
                        break
                    state["offset"] = stream.tell()
                    identifier = f"{source}:{identity}:{state['generation']}:{offset}"
                    try:
                        text = line.decode("utf-8")
                        event = normalize(json.loads(text), source, identifier) if kind == "application" else parse_http(text, source, identifier)
                        if datetime.fromisoformat(event["at"]) > now + timedelta(minutes=5):
                            raise ValueError("Event timestamp is over five minutes in the future")
                        result.append(event)
                    except (ValueError, KeyError, TypeError, OverflowError, OSError):
                        bad += 1
                if bad:
                    warnings.append(f"Skipped {bad} malformed, oversized, or future-dated records; raw content was not retained.")
                state["signature"] = signature(stream, state["offset"])
                checkpoint["file"] = state
                if consumed >= MAX_BYTES or len(result) >= MAX_RECORDS:
                    warnings.append("Per-cycle read budget reached; remaining bytes will be read next cycle.")
                    break
        except (OSError, ValueError) as exc:
            warnings.append(f"Cannot read configured log: {type(exc).__name__}")
            return {"events": result, "checkpoint": checkpoint, "status": "partial" if result else "unavailable", "warnings": warnings}
    return {"events": result, "checkpoint": checkpoint, "status": "partial" if warnings else "ok", "warnings": warnings}
