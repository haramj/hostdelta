"""Optional Linux netfilter event stream; no packet payloads or automatic elevation."""

import hashlib
import os
import re
import selectors
import subprocess
import uuid
from datetime import datetime, timezone

from .model import stamp, utcnow


def parse(line, identity):
    match = re.search(r"\[(NEW|DESTROY)\]", line)
    if not match:
        return None
    fields = re.findall(r"\b(src|dst|sport|dport)=([^\s]+)", line)
    original = {}
    for key, value in fields:
        original.setdefault(key, value)
    if not all(k in original for k in ("src", "dst", "sport", "dport")):
        raise ValueError("Incomplete conntrack tuple")
    import ipaddress
    ipaddress.ip_address(original["src"])
    ipaddress.ip_address(original["dst"])
    for key in ("sport", "dport"):
        original[key] = int(original[key])
        if not 0 <= original[key] <= 65535:
            raise ValueError("Invalid TCP port")
    timestamp = re.match(r"\[([0-9]+\.[0-9]+)\]", line)
    at = stamp(datetime.fromtimestamp(float(timestamp[1]), timezone.utc)) if timestamp else stamp(utcnow())
    kind = "tcp_flow_new" if match[1] == "NEW" else "tcp_flow_destroyed"
    return {"at": at, "event_id": hashlib.sha256(identity.encode()).hexdigest(), "source": "tcp:conntrack",
            "category": "network", "kind": kind, "severity": "info", "summary": "Netfilter TCP flow " + match[1].lower(),
            "attributes": {**original, "direction": "unknown", "origin": "netfilter_conntrack", "handshake_confirmed": False}}


class Stream:
    def __init__(self):
        self.process = None
        self.selector = selectors.DefaultSelector()
        self.buffer = b""
        self.sequence = 0
        self.generation = uuid.uuid4().hex
        self.start_error = None
        self.started = False
        try:
            self.process = subprocess.Popen(["conntrack", "-E", "-p", "tcp", "-e", "NEW,DESTROY", "-o", "timestamp"],
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            for pipe, label in ((self.process.stdout, "out"), (self.process.stderr, "err")):
                os.set_blocking(pipe.fileno(), False)
                self.selector.register(pipe, selectors.EVENT_READ, label)
        except OSError:
            self.start_error = "conntrack is unavailable; install the optional tool and grant only the required network-admin capability"

    def drain(self):
        warnings, events = [], []
        if self.start_error:
            return {"events": [], "status": "unavailable", "warnings": [self.start_error]}
        if not self.started:
            warnings.append("Live conntrack subscription started. Events before subscription or during downtime cannot be recovered.")
            self.started = True
        read_bytes = 0
        while read_bytes < 4 * 1024 * 1024:
            ready = self.selector.select(timeout=0)
            if not ready:
                break
            for key, _ in ready:
                try:
                    blob = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not blob:
                    self.selector.unregister(key.fileobj)
                    continue
                read_bytes += len(blob)
                if key.data == "err":
                    warnings.append("conntrack reported diagnostics; check permissions, kernel support, and netlink buffer loss")
                    continue
                self.buffer += blob
                while b"\n" in self.buffer:
                    line, self.buffer = self.buffer.split(b"\n", 1)
                    self.sequence += 1
                    try:
                        event = parse(line.decode("utf-8"), f"{self.generation}:{self.sequence}")
                        if event:
                            events.append(event)
                    except (ValueError, OverflowError, OSError):
                        warnings.append("Malformed conntrack event skipped")
                if len(self.buffer) > 65536:
                    self.buffer = b""
                    warnings.append("Oversized conntrack record discarded")
        if read_bytes >= 4 * 1024 * 1024:
            warnings.append("Per-cycle conntrack read budget reached; kernel/pipe buffers may overflow")
        if self.process.poll() is not None:
            warnings.append("conntrack process exited; the collector will reopen the subscription next cycle")
        status = "unavailable" if self.process.poll() is not None else "partial" if warnings else "ok"
        return {"events": events, "status": status, "warnings": list(dict.fromkeys(warnings))}

    def close(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            self.process.stdout.close()
            self.process.stderr.close()
        self.selector.close()
