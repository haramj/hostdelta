"""Versioned, strict, JSON configuration. Credentials are environment references."""

import copy
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT = {"version": 1, "interval_seconds": 30, "snapshot_interval_seconds": 900,
           "retention_days": 30, "journal": True, "application_logs": [], "http_logs": [],
           "services": [], "tcp": {"enabled": False, "mode": "sample"}, "adapters": [],
           "failure_threshold": 2, "recovery_threshold": 2}


def keys(value, allowed, context):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(f"Unknown fields or invalid object in {context}")


def integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer in {minimum}..{maximum}")


def url(value, allow_http=False):
    if not isinstance(value, str):
        raise ValueError("Endpoint URL must be a string")
    parts = urlsplit(value)
    if parts.scheme not in (["https", "http"] if allow_http else ["https"]) or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Endpoint must be HTTPS without credentials, query or fragment; HTTP requires allow_http=true")
    return value.rstrip("/")


def validate(raw):
    keys(raw, DEFAULT, "configuration")
    cfg = {**copy.deepcopy(DEFAULT), **raw}
    if type(cfg["version"]) is not int or cfg["version"] != 1:
        raise ValueError("Unsupported configuration version")
    for field, minimum, maximum in (("interval_seconds", 1, 3600), ("snapshot_interval_seconds", 1, 86400),
                                    ("retention_days", 1, 3650), ("failure_threshold", 1, 20), ("recovery_threshold", 1, 20)):
        integer(cfg[field], minimum, maximum, field)
    if type(cfg["journal"]) is not bool:
        raise ValueError("journal must be boolean")
    for field in ("application_logs", "http_logs", "services", "adapters"):
        if not isinstance(cfg[field], list) or len(cfg[field]) > 32:
            raise ValueError(f"{field} must be a list of at most 32 entries")
    for field in ("application_logs", "http_logs"):
        if any(not isinstance(p, str) or not Path(p).is_absolute() for p in cfg[field]):
            raise ValueError(f"{field} requires absolute paths")
        if len(set(cfg[field])) != len(cfg[field]):
            raise ValueError(f"Duplicate path in {field}")
    for service in cfg["services"]:
        if not isinstance(service, str) or not re.fullmatch(r"[A-Za-z0-9_.@:-]+\.service", service):
            raise ValueError("services must contain literal .service unit names")
    keys(cfg["tcp"], ["enabled", "mode"], "tcp")
    cfg["tcp"].setdefault("mode", "sample")
    if type(cfg["tcp"].get("enabled")) is not bool:
        raise ValueError("tcp.enabled must be boolean")
    if cfg["tcp"]["mode"] not in ("sample", "conntrack"):
        raise ValueError("tcp.mode must be sample or conntrack")
    names = set()
    for adapter in cfg["adapters"]:
        keys(adapter, ["name", "type", "url", "allow_http", "ca_file", "token_env", "token_id_env", "credential_id_env", "credential_secret_env", "services", "timeout_seconds"], "adapter")
        name = adapter.get("name", "")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name) or name in names:
            raise ValueError("Adapter names must be unique safe identifiers")
        names.add(name)
        if adapter.get("type") not in ("openstack", "proxmox"):
            raise ValueError("Adapter type must be openstack or proxmox")
        if type(adapter.get("allow_http", False)) is not bool:
            raise ValueError("allow_http must be boolean")
        url(adapter.get("url", ""), adapter.get("allow_http", False))
        integer(adapter.get("timeout_seconds", 5), 1, 30, "timeout_seconds")
        if "ca_file" in adapter and (not isinstance(adapter["ca_file"], str) or not Path(adapter["ca_file"]).is_absolute()):
            raise ValueError("ca_file requires an absolute path")
        for key, value in adapter.items():
            if key.endswith("_env") and (not isinstance(value, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", value)):
                raise ValueError("Credentials must reference environment variable names")
        endpoints = adapter.get("services", {})
        if not isinstance(endpoints, dict) or len(endpoints) > 16:
            raise ValueError("services must map at most 16 names to explicit trusted API URLs")
        for service, endpoint in endpoints.items():
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", service):
                raise ValueError("Invalid adapter service name")
            url(endpoint, adapter.get("allow_http", False))
    return cfg


def load(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(128 * 1024 + 1)
    if len(raw) > 128 * 1024:
        raise ValueError("Configuration exceeds 128 KiB")
    return validate(json.loads(raw))
