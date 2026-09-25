"""Bounded, local HTTP access-log analysis. No bodies, queries or headers are retained."""

import gzip
import ipaddress
import json
import re
import stat
import zlib
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from .model import stamp

MAX_BYTES = 8 * 1024 * 1024
MAX_FILES = 32
COMBINED = re.compile(r'^(?P<ip>\S+) \S+ \S+ \[(?P<time>[^]]+)\] "(?P<method>[A-Z]+) (?P<path>\S+) [^"]+" (?P<status>\d{3}) (?P<bytes>\d+|-)')


def discover():
    """Conservative defaults for common Linux web servers; never scan arbitrary files."""
    result = []
    for raw in ("/var/log/nginx/access.log", "/var/log/apache2/access.log", "/var/log/httpd/access_log"):
        try:
            if Path(raw).is_file():
                result.append(raw)
        except OSError:
            pass
    return result


def parse_line(line):
    if line.startswith("{"):
        row = json.loads(line)
        at = datetime.fromisoformat(str(row["time"]).replace("Z", "+00:00"))
        ip, method, target, status = row["remote_addr"], row["method"], row["path"], int(row["status"])
        elapsed = row.get("request_time")
        elapsed = float(elapsed) if elapsed not in (None, "", "-") else None
    else:
        match = COMBINED.match(line)
        if not match:
            raise ValueError("Unsupported access log format")
        at = datetime.strptime(match["time"], "%d/%b/%Y:%H:%M:%S %z")
        ip, method, target, status, elapsed = match["ip"], match["method"], match["path"], int(match["status"]), None
    if at.tzinfo is None:
        raise ValueError("Request timestamp requires timezone")
    ipaddress.ip_address(ip)
    if not 100 <= status <= 599 or not re.fullmatch(r"[A-Z]{1,20}", method):
        raise ValueError("Invalid request method/status")
    # Absolute-form proxy requests are allowed; userinfo, query and fragment are discarded.
    path = urlsplit(target).path or "/"
    return {"at": stamp(at), "ip": ip, "method": method, "path": path[:500], "status": status,
            "slow": elapsed is not None and elapsed >= 1.0}


def log_files(configured):
    result, warnings = [], []
    seen = set()
    for raw in configured:
        path = Path(raw)
        if not path.is_absolute():
            raise ValueError("Access log paths must be absolute")
        # Include common logrotate names: access.log.1, access.log.2.gz.
        candidates = [path] + sorted(path.parent.glob(path.name + ".[0-9]*"))
        for candidate in candidates:
            try:
                info = candidate.stat()
                if not stat.S_ISREG(info.st_mode):
                    warnings.append(f"Not a regular log file: {candidate}")
                    continue
                identity = (info.st_dev, info.st_ino)
            except OSError as exc:
                warnings.append(f"Cannot read {candidate}: {exc}")
                continue
            if identity not in seen:
                seen.add(identity)
                result.append(candidate)
    if len(result) > MAX_FILES:
        warnings.append(f"Access logs capped at {MAX_FILES} files; some rotations omitted.")
    return result[:MAX_FILES], warnings


def analyze(configured, since, until):
    paths, warnings = log_files(configured)
    summary = {"status": "not_configured" if not configured else "unavailable", "total": 0, "status_codes": {},
               "top_paths": [], "top_clients": [], "slow_requests": 0, "errors_5xx": 0, "warnings": warnings,
               "files_read": 0, "first_request": None, "last_request": None}
    if not configured:
        warnings.append("HTTP request logs are not configured. Run init --access-log /var/log/nginx/access.log.")
        return summary
    codes, endpoints, clients = Counter(), Counter(), Counter()
    start, end = stamp(since), stamp(until)
    malformed = 0
    for path in paths:
        try:
            opener = gzip.open if path.suffix == ".gz" else open
            with opener(path, "rb") as stream:
                if path.suffix != ".gz":
                    size = path.stat().st_size
                    if size > MAX_BYTES:
                        stream.seek(size - MAX_BYTES)
                        stream.readline()  # discard incomplete first record
                        warnings.append(f"{path}: scanned only the newest 8 MiB.")
                blob = stream.read(MAX_BYTES + 1)
                if len(blob) > MAX_BYTES:
                    warnings.append(f"{path}: decompressed scan capped at 8 MiB; later records omitted.")
                    blob = blob[:MAX_BYTES].rsplit(b"\n", 1)[0]
            summary["files_read"] += 1
            for line in blob.decode("utf-8", errors="replace").splitlines():
                try:
                    request = parse_line(line)
                except (ValueError, KeyError, TypeError, OverflowError):
                    malformed += 1
                    continue
                if not start < request["at"] <= end:
                    continue
                summary["total"] += 1
                summary["slow_requests"] += int(request["slow"])
                summary["errors_5xx"] += int(request["status"] >= 500)
                summary["first_request"] = min(summary["first_request"] or request["at"], request["at"])
                summary["last_request"] = max(summary["last_request"] or request["at"], request["at"])
                codes[str(request["status"])] += 1
                endpoints[f"{request['method']} {request['path']}"] += 1
                clients[request["ip"]] += 1
        except (OSError, EOFError, zlib.error) as exc:
            warnings.append(f"Cannot read {path}: {exc}")
    if malformed:
        warnings.append(f"Skipped {malformed} malformed/unsupported HTTP log lines.")
    summary.update(status="ok" if summary["files_read"] else "unavailable", status_codes=dict(sorted(codes.items())),
                   top_paths=endpoints.most_common(10), top_clients=clients.most_common(10))
    warnings.append("Counts cover readable retained access logs only. Client IP is the logged peer; proxy headers are not trusted automatically.")
    return summary
