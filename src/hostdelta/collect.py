"""Read-only Linux state collectors. A failed source never becomes an empty state."""

import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .model import stamp, utcnow


def run(args, timeout=5):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False,
                            env={**os.environ, "LC_ALL": "C", "SYSTEMD_COLORS": "0"})
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[:300] or f"{args[0]} exited {result.returncode}")
    return result.stdout


def packages():
    if shutil.which("dpkg-query"):
        lines = run(["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${db:Status-Status}\n"])
        return {parts[0]: parts[1] for line in lines.splitlines()
                if len(parts := line.split("\t")) == 3 and parts[2] == "installed"}
    if shutil.which("rpm"):
        lines = run(["rpm", "-qa", "--qf", "%{NAME}.%{ARCH}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\n"])
        return dict(line.split("\t", 1) for line in lines.splitlines() if "\t" in line)
    raise RuntimeError("Neither dpkg-query nor rpm found")


def services():
    lines = run(["systemctl", "list-units", "--type=service", "--all", "--no-pager", "--no-legend", "--plain"])
    result = {}
    for line in lines.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            result[parts[0]] = {"load": parts[1], "active": parts[2], "sub": parts[3]}
    return result


def network():
    links = json.loads(run(["ip", "-j", "address", "show"]))
    routes = json.loads(run(["ip", "-j", "route", "show", "table", "all"]))
    routes += json.loads(run(["ip", "-j", "-6", "route", "show", "table", "all"]))
    data = {}
    for link in links:
        data["interface:" + link["ifname"]] = {
            "state": link.get("operstate"), "mtu": link.get("mtu"),
            "addresses": sorted(f"{a['local']}/{a['prefixlen']}" for a in link.get("addr_info", []) if "local" in a)}
    # Keep stable route identity; ignore counters, expiration timers and cache churn.
    for route in routes:
        item = {k: route[k] for k in ("dst", "gateway", "dev", "table", "metric", "type", "prefsrc", "nexthops") if k in route}
        key = json.dumps(item, sort_keys=True)
        data["route:" + key] = item
    return data


def files(watch):
    data = {}
    count = 0
    def inspect(path):
        nonlocal count
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            data[str(path)] = {"symlink": os.readlink(path)}
        elif stat.S_ISREG(info.st_mode):
            count += 1
            if count > 3000 or info.st_size > 2 * 1024 * 1024:
                raise RuntimeError("Watch limit exceeded (3000 files, 2 MiB per file)")
            # Do not follow symlinks, including a replacement between lstat and open.
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                actual = os.fstat(stream.fileno())
                if not stat.S_ISREG(actual.st_mode):
                    raise RuntimeError(f"Watch target is no longer a regular file: {path}")
                content = stream.read(2 * 1024 * 1024 + 1)
                if len(content) > 2 * 1024 * 1024:
                    raise RuntimeError(f"Watch file grew beyond limit: {path}")
            data[str(path)] = {"sha256": hashlib.sha256(content).hexdigest(),
                               "mode": oct(stat.S_IMODE(actual.st_mode)), "uid": actual.st_uid, "gid": actual.st_gid}
    for raw in watch:
        root = Path(raw)
        if not root.is_absolute():
            raise ValueError("Watched paths must be absolute")
        try:
            mode = root.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(mode):
            def onerror(error):
                raise error
            for base, dirs, names in os.walk(root, followlinks=False, onerror=onerror):
                for name in list(dirs):
                    child = Path(base) / name
                    if child.is_symlink():
                        inspect(child)
                        dirs.remove(name)
                for name in names:
                    inspect(Path(base) / name)
        else:
            inspect(root)
    return data


def system():
    return {"kernel": platform.release(), "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "reboot_required": Path("/var/run/reboot-required").exists()}


def capture(watch):
    if platform.system() != "Linux":
        raise ValueError("Live collection requires Linux. On this machine, try: hostdelta demo --full")
    collectors = {"packages": packages, "services": services, "network": network,
                  "files": lambda: files(watch), "system": system}
    def guarded(item):
        name, fn = item
        try:
            result = {"status": "ok", "data": fn()}
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            result = {"status": "unavailable", "data": {}, "error": str(exc)}
        if name == "files":
            result["scope"] = sorted(watch)
        return name, result
    started = stamp(utcnow())
    with ThreadPoolExecutor(max_workers=5) as pool:
        domains = dict(pool.map(guarded, collectors.items()))
    return {"at": started, "host": platform.node(), "domains": domains}
