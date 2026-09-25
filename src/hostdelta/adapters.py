"""Read-only cloud probes. Only explicitly configured URLs receive credentials."""

import json
import os
import ssl
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

from .config import url as validate_url
from .model import stamp, utcnow

MAX_RESPONSE = 2 * 1024 * 1024


class ProbeError(Exception):
    def __init__(self, code, health="unknown"):
        super().__init__(code)
        self.code, self.health = code, health


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPClient:
    def __init__(self, config):
        self.config = config
        context = ssl.create_default_context(cafile=config.get("ca_file"))
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), urllib.request.HTTPSHandler(context=context))

    def request(self, url, headers=None, body=None):
        validate_url(url, self.config.get("allow_http", False))
        request = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Accept": "application/json", "Content-Type": "application/json", **(headers or {})},
                                         method="POST" if body is not None else "GET")
        try:
            with self.opener.open(request, timeout=self.config.get("timeout_seconds", 5)) as response:
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise ProbeError("response_too_large")
                try:
                    parsed = json.loads(raw)
                except (ValueError, UnicodeError) as exc:
                    raise ProbeError("invalid_json_response") from exc
                return parsed, response.headers
        except urllib.error.HTTPError as exc:
            # Never expose response bodies, URLs containing secrets, or request headers.
            status = exc.code
            exc.close()
            raise ProbeError(f"http_{status}", "down" if status >= 500 else "unknown") from None
        except urllib.error.URLError as exc:
            code = "tls_verification_failed" if isinstance(exc.reason, ssl.SSLError) else "connection_failed"
            raise ProbeError(code, "unknown" if code.startswith("tls") else "down") from None
        except (TimeoutError, OSError):
            raise ProbeError("connection_failed", "down") from None


def credential(config, key, default):
    value = os.environ.get(config.get(key, default), "")
    if not value or "\r" in value or "\n" in value:
        raise ProbeError("credential_missing_or_invalid")
    return value


def poll(config, previous=None, client=None, now=None):
    at = stamp(now or utcnow())
    source = "adapter:" + config["name"]
    observations, warnings, events = [], [], []
    checkpoint = dict(previous or {})
    def observe(suffix, health, reason=None):
        observations.append({"entity": source + ":" + suffix, "at": at, "source": source, "health": health, "reason": reason})
        if reason:
            warnings.append(suffix + ": " + reason)
    try:
        client = client or HTTPClient(config)
        if config["type"] == "openstack":
            identity = credential(config, "credential_id_env", "OS_APPLICATION_CREDENTIAL_ID")
            secret = credential(config, "credential_secret_env", "OS_APPLICATION_CREDENTIAL_SECRET")
            body = {"auth": {"identity": {"methods": ["application_credential"],
                                         "application_credential": {"id": identity, "secret": secret}}}}
            base = config["url"].rstrip("/")
            if not base.endswith("/v3"):
                base += "/v3"
            data, headers = client.request(base + "/auth/tokens", body=body)
            token = headers.get("X-Subject-Token")
            if not token or not isinstance(data, dict) or not isinstance(data.get("token"), dict):
                raise ProbeError("invalid_identity_response")
            observe("identity", "up")
            for name, endpoint in config.get("services", {}).items():
                try:
                    client.request(endpoint, headers={"X-Auth-Token": token})
                    observe(name, "up")
                except ProbeError as exc:
                    observe(name, exc.health, exc.code)
        else:
            identity = credential(config, "token_id_env", "PROXMOX_TOKEN_ID")
            secret = credential(config, "token_env", "PROXMOX_TOKEN_SECRET")
            headers = {"Authorization": "PVEAPIToken=" + identity + "=" + secret}
            base = config["url"].rstrip("/")
            if not base.endswith("/api2/json"):
                base += "/api2/json"
            data, _ = client.request(base + "/cluster/resources", headers=headers)
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise ProbeError("invalid_resource_response")
            resources = {}
            if len(data["data"]) > 2000:
                raise ProbeError("resource_limit_exceeded")
            for row in data["data"]:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                    raise ProbeError("invalid_resource_record")
                resources[row["id"]] = {k: row[k] for k in ("type", "name", "node", "status", "vmid") if k in row}
            observe("api", "up")
            old = checkpoint.get("resources")
            if old is not None and old != resources:
                changes = {"added": sorted(set(resources) - set(old)), "removed": sorted(set(old) - set(resources)),
                           "changed": sorted(k for k in set(old) & set(resources) if old[k] != resources[k])}
                events.append({"at": at, "source": source, "service": config["name"], "category": "infrastructure",
                               "kind": "inventory_changed", "severity": "info", "summary": "Proxmox resource inventory changed",
                               "attributes": {"counts": {k: len(v) for k, v in changes.items()}, "sample": {k: v[:20] for k, v in changes.items()}}})
            checkpoint["resources"] = resources
            try:
                cluster, _ = client.request(base + "/cluster/status", headers=headers)
                if not isinstance(cluster, dict) or not isinstance(cluster.get("data"), list):
                    raise ProbeError("invalid_cluster_response")
                quorum = [r.get("quorate") for r in cluster["data"] if isinstance(r, dict) and r.get("type") == "cluster"]
                observe("quorum", "down" if quorum and quorum[0] == 0 else "up")
            except ProbeError as exc:
                observe("quorum", exc.health, exc.code)
    except (ProbeError, OSError, ValueError) as exc:
        reason = exc.code if isinstance(exc, ProbeError) else "adapter_configuration_error"
        health = exc.health if isinstance(exc, ProbeError) else "unknown"
        observe("identity" if config["type"] == "openstack" else "api", health, reason)
        for name in config.get("services", {}):
            observe(name, "unknown", "identity_probe_unavailable")
        if config["type"] == "proxmox":
            observe("quorum", "unknown", "api_probe_unavailable")
    return {"events": events, "checkpoint": checkpoint, "observations": observations,
            "status": "partial" if warnings else "ok", "warnings": warnings}


def bounded_poll(config, previous=None):
    """Enforce a wall-clock budget even if a server drips HTTP headers/body forever."""
    timeout = min(45, config.get("timeout_seconds", 5) * (len(config.get("services", {})) + 3) + 2)
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
    try:
        child = subprocess.run([sys.executable, "-c", "from hostdelta.adapters import worker; worker()"],
                               input=json.dumps({"config": config, "previous": previous}), capture_output=True,
                               text=True, timeout=timeout, env=env, check=False)
        if child.returncode != 0:
            raise ValueError("adapter worker failed")
        return json.loads(child.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        names = (["identity", *config.get("services", {})] if config["type"] == "openstack" else ["api", "quorum"])
        return {"events": [], "checkpoint": None, "status": "unavailable", "warnings": ["Adapter worker failed or exceeded its wall-clock budget"],
                "observations": [{"entity": "adapter:" + config["name"] + ":" + name, "source": "adapter:" + config["name"], "at": stamp(utcnow()), "health": "unknown"} for name in names]}


def worker():
    request = json.load(sys.stdin)
    print(json.dumps(poll(request["config"], request.get("previous"))))
