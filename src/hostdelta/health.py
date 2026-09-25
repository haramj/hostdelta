"""Systemd invocation tracking and optional sampled Linux TCP connection events."""

import hashlib
from pathlib import Path
import socket
import struct
import subprocess

from .collect import run
from .model import stamp, utcnow


def service_observations(units, now=None):
    at = stamp(now or utcnow())
    source = "systemd:health"
    if not units:
        return {"observations": [], "events": [], "status": "ok", "warnings": []}
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        output = run(["systemctl", "show", "--no-pager", "--property=Id,LoadState,ActiveState,SubState,InvocationID,NRestarts", *units], timeout=8)
        records = [dict(line.split("=", 1) for line in block.splitlines() if "=" in line) for block in output.strip().split("\n\n")]
        data = {row["Id"]: row for row in records if "Id" in row}
        observations, warnings = [], []
        for unit in units:
            row = data.get(unit, {})
            active = row.get("ActiveState")
            health = "unknown"
            if row.get("LoadState") == "loaded":
                health = "up" if active == "active" else "down" if active in ("failed", "inactive") else "unknown"
            if health == "unknown":
                warnings.append(f"{unit}: state unavailable or transitional")
            observations.append({"entity": "service:" + unit, "at": at, "source": source, "health": health,
                                 "invocation_id": row.get("InvocationID"), "boot_id": boot,
                                 "automatic_restarts": row.get("NRestarts"), "active_state": active})
        return {"observations": observations, "events": [], "status": "partial" if warnings else "ok", "warnings": warnings}
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return {"observations": [{"entity": "service:" + u, "at": at, "source": source, "health": "unknown"} for u in units],
                "events": [], "status": "unavailable", "warnings": [f"Systemd health collection failed: {type(exc).__name__}"]}


def address(raw, ipv6=False):
    host, port = raw.split(":")
    packed = bytes.fromhex(host)
    # Linux /proc stores each 32-bit word in host byte order.
    packed = b"".join(struct.pack("!I", struct.unpack("=I", packed[i:i+4])[0]) for i in range(0, len(packed), 4))
    return socket.inet_ntop(socket.AF_INET6 if ipv6 else socket.AF_INET, packed), int(port, 16)


def parse_tcp(text, ipv6=False):
    sockets, listeners = {}, set()
    for line in text.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 10:
            raise ValueError("Malformed /proc TCP row")
        local, remote = address(cols[1], ipv6), address(cols[2], ipv6)
        state, inode = cols[3], cols[9]
        if state == "0A":
            listeners.add(local)
        elif state == "01":
            key = f"{inode}:{local}:{remote}"
            sockets[key] = {"local_address": local[0], "local_port": local[1], "remote_address": remote[0], "remote_port": remote[1], "inode": inode}
    return sockets, listeners


def tcp(previous=None, root=Path("/proc/net"), now=None):
    at = stamp(now or utcnow())
    previous = previous or {}
    try:
        sockets, listeners = {}, set()
        for name, ipv6 in (("tcp", False), ("tcp6", True)):
            path = root / name
            if ipv6 and not path.exists():
                continue
            with path.open("rb") as stream:
                raw = stream.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError("TCP table exceeds 4 MiB")
            rows, ports = parse_tcp(raw.decode(), ipv6)
            sockets.update(rows)
            listeners |= ports
        if len(sockets) > 10000:
            raise ValueError("TCP sample exceeds 10000 sockets")
        for item in sockets.values():
            local = (item["local_address"], item["local_port"])
            item["direction"] = "likely_inbound" if local in listeners or ("0.0.0.0", local[1]) in listeners or ("::", local[1]) in listeners else "unknown"  # nosec B104
        events = []
        old = previous.get("sockets")
        if old is not None:
            for kind, keys, data in (("tcp_appeared", set(sockets) - set(old), sockets), ("tcp_disappeared", set(old) - set(sockets), old)):
                for key in sorted(keys):
                    events.append({"at": at, "source": "tcp:sample", "category": "network", "kind": kind, "severity": "info",
                                   "event_id": hashlib.sha256(f"{kind}:{key}:{at}".encode()).hexdigest(),
                                   "summary": "Established TCP socket " + ("appeared" if kind == "tcp_appeared" else "disappeared") + " between samples",
                                   "attributes": {**data[key], "previous_sample_at": previous.get("at"), "sampled": True}})
        checkpoint = {"at": at, "sockets": sockets}
        return {"events": events, "checkpoint": checkpoint, "status": "ok", "warnings": []}
    except (OSError, ValueError, struct.error):
        return {"events": [], "checkpoint": None, "status": "unavailable", "warnings": ["TCP sample unavailable; previous sample preserved. No socket disappearance is inferred."]}
