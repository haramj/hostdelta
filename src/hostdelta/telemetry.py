"""Structured application logging and normalization with safe, bounded attributes.

The SDK has no global logging configuration or network side effects.
"""

import hashlib
import json
import logging
import math
import re
import sys
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from types import MappingProxyType
from urllib.parse import urlsplit, urlunsplit

from .model import clean, stamp

CONTEXT = ContextVar("hostdelta_log_context", default=MappingProxyType({}))
SECRET = re.compile(r"password|passwd|secret|token|authorization|cookie|api.?key|private.?key|credential|request.?body|response.?body", re.I)
LEVELS = {"TRACE": "info", "DEBUG": "info", "INFO": "info", "NOTICE": "info", "WARN": "warning", "WARNING": "warning", "ERROR": "critical", "FATAL": "critical", "CRITICAL": "critical"}


def scrub_text(value):
    value = str(value)
    value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*", "[REDACTED PRIVATE KEY]", value, flags=re.S)
    value = re.sub(r"(?i)\b(Bearer|Basic)\s+\S+", r"\1 [REDACTED]", value)
    value = re.sub(r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)\s*[=:]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)", r"\1=[REDACTED]", value)
    value = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED JWT]", value)
    def url(match):
        try:
            parts = urlsplit(match[0])
            return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))
        except ValueError:
            return "[REDACTED URL]"
    value = re.sub(r"https?://[^\s\"<>]+", url, value)
    return clean(value, 2048)


def redact(value, depth=0):
    if depth > 6:
        return "[DEPTH LIMIT]"
    if isinstance(value, dict):
        return {clean(k, 80): "[REDACTED]" if SECRET.search(str(k)) else redact(v, depth + 1)
                for k, v in list(value.items())[:64]}
    if isinstance(value, (tuple, list)):
        return [redact(v, depth + 1) for v in value[:64]]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return scrub_text(value)


def timestamp(value):
    if isinstance(value, bool):
        raise ValueError("Invalid event timestamp")
    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value, timezone.utc)
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("Event timestamp is required")
    if dt.tzinfo is None:
        raise ValueError("Application timestamps must include a timezone")
    return stamp(dt)


def normalize(row, source="application", identity=None):
    if not isinstance(row, dict):
        raise ValueError("Application record must be a JSON object")
    at = timestamp(row.get("timestamp", row.get("@timestamp", row.get("time"))))
    service = row.get("service", row.get("service.name", row.get("logger", "application")))
    service = service.get("name", "application") if isinstance(service, dict) else service
    if not isinstance(service, str) or not service:
        raise ValueError("service must be a name or object with a name")
    level = str(row.get("level", row.get("severity", "INFO"))).upper()
    if level not in LEVELS:
        raise ValueError("Unsupported application log level")
    event = row.get("event", {})
    event_name = event.get("name", "application.log") if isinstance(event, dict) else str(event)
    message = row.get("message", row.get("msg", event_name))
    if not isinstance(message, str):
        raise ValueError("Application message must be a string")
    attributes = redact(row.get("attributes", {}))
    if not isinstance(attributes, dict):
        raise ValueError("attributes must be an object")
    for field in ("http", "error", "duration_ms", "run_id"):
        if field in row:
            attributes[field] = redact(row[field])
    if isinstance(attributes.get("http"), dict):
        http = attributes["http"]
        # Only retain HTTP fields with a documented purpose.
        attributes["http"] = {k: v for k, v in http.items() if k in ("method", "route", "status_code", "duration_ms")}
        if "route" in attributes["http"]:
            attributes["http"]["route"] = urlsplit(str(http["route"])).path[:500]
    trace = row.get("trace_id")
    span = row.get("span_id")
    if trace is not None and not re.fullmatch(r"[0-9a-f]{32}", str(trace)):
        raise ValueError("trace_id must be 32 lowercase hexadecimal characters")
    if span is not None and not re.fullmatch(r"[0-9a-f]{16}", str(span)):
        raise ValueError("span_id must be 16 lowercase hexadecimal characters")
    result = {"at": at, "category": "application", "kind": "application_log", "source": source,
              "service": clean(service, 128), "event_name": clean(event_name, 128),
              "severity": LEVELS[level], "level": level, "summary": scrub_text(message),
              "attributes": attributes, "trace_id": trace, "span_id": span}
    if identity:
        result["event_id"] = hashlib.sha256(identity.encode()).hexdigest()
        result["ref"] = "event:" + result["event_id"]
    return result


@contextmanager
def bind_context(**fields):
    """Attach request/run correlation to nested code; works across asyncio tasks."""
    token = CONTEXT.set({**CONTEXT.get(), **fields})
    try:
        yield
    finally:
        CONTEXT.reset(token)


class JsonFormatter(logging.Formatter):
    def __init__(self, service, environment="production", version=None):
        super().__init__()
        self.service = {"name": service, "environment": environment, "version": version}

    def format(self, record):
        context = {**CONTEXT.get(), **getattr(record, "context", {})}
        payload = {"schema_version": 1, "timestamp": stamp(datetime.fromtimestamp(record.created, timezone.utc)),
                   "level": record.levelname, "service": self.service,
                   "event": {"name": getattr(record, "event_name", "application.log")},
                   "message": record.getMessage(), "attributes": {**context.pop("attributes", {}), **getattr(record, "attributes", {})}}
        for key in ("trace_id", "span_id", "run_id"):
            if key in context:
                payload[key] = context.pop(key)
        payload["attributes"].update(context)
        if record.exc_info:
            exc = record.exc_info[1]
            payload["error"] = {"type": type(exc).__name__, "message": str(exc)}
        return json.dumps(redact(payload), ensure_ascii=True, allow_nan=False, separators=(",", ":"))


class StructuredLogger:
    def __init__(self, service, stream=None, environment="production", version=None, level=logging.INFO):
        self.logger = logging.Logger("hostdelta.application." + service, level)
        handler = logging.StreamHandler(sys.stdout if stream is None else stream)
        handler.setFormatter(JsonFormatter(service, environment, version))
        self.logger.addHandler(handler)
        self.logger.propagate = False

    def event(self, name, message="", *, level=logging.INFO, attributes=None, exc_info=False):
        self.logger.log(level, message or name, extra={"event_name": name, "attributes": attributes or {}}, exc_info=exc_info)

    def request(self, method, route, status_code, duration_ms):
        self.event("http.server.request", f"{method} {urlsplit(route).path} → {status_code}",
                   level=logging.ERROR if status_code >= 500 else logging.INFO,
                   attributes={"http": {"method": method, "route": urlsplit(route).path,
                                        "status_code": status_code, "duration_ms": duration_ms}})

    @contextmanager
    def operation(self, name, **attributes):
        started = time.monotonic()
        parent = CONTEXT.get()
        with bind_context(trace_id=parent.get("trace_id", uuid.uuid4().hex), span_id=uuid.uuid4().hex[:16]):
            self.event(name + ".started", attributes=attributes)
            try:
                yield
            except Exception:
                self.event(name + ".failed", level=logging.ERROR, exc_info=True,
                           attributes={**attributes, "duration_ms": round((time.monotonic() - started) * 1000, 3)})
                raise
            else:
                self.event(name + ".completed", attributes={**attributes, "duration_ms": round((time.monotonic() - started) * 1000, 3)})
