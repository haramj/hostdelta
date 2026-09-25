from contextlib import contextmanager
import io
import json
import os
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

from hostdelta.adapters import HTTPClient, ProbeError, poll, bounded_poll


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, url, headers=None, body=None):
        self.calls.append((url, headers, body))
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value


class AdapterTests(unittest.TestCase):
    def test_openstack_credentials_only_to_configured_endpoints(self):
        cfg = {"name": "cloud", "type": "openstack", "url": "https://identity.test/v3", "services": {"nova": "https://nova.test/v2.1"}}
        client = FakeClient([({"token": {"catalog": [{"endpoints": [{"url": "https://untrusted.test"}]}]}}, {"X-Subject-Token": "secret-token"}), ({"versions": []}, {})])
        with patch.dict(os.environ, {"OS_APPLICATION_CREDENTIAL_ID": "id", "OS_APPLICATION_CREDENTIAL_SECRET": "secret-value"}):
            result = poll(cfg, client=client)
        self.assertEqual([c[0] for c in client.calls], ["https://identity.test/v3/auth/tokens", "https://nova.test/v2.1"])
        self.assertEqual(client.calls[1][1], {"X-Auth-Token": "secret-token"})
        self.assertEqual(result["status"], "ok")
        self.assertNotIn("secret-value", json.dumps(result))
        self.assertNotIn("secret-token", json.dumps(result))

    def test_auth_failure_is_unknown_not_cloud_outage(self):
        cfg = {"name": "cloud", "type": "openstack", "url": "https://identity.test/v3", "services": {"nova": "https://nova.test/v2.1"}}
        with patch.dict(os.environ, {"OS_APPLICATION_CREDENTIAL_ID": "id", "OS_APPLICATION_CREDENTIAL_SECRET": "secret"}):
            result = poll(cfg, client=FakeClient([ProbeError("http_401")]))
        self.assertTrue(all(o["health"] == "unknown" for o in result["observations"]))

    def test_connection_failure_is_observer_unreachable(self):
        cfg = {"name": "cloud", "type": "openstack", "url": "https://identity.test/v3"}
        with patch.dict(os.environ, {"OS_APPLICATION_CREDENTIAL_ID": "id", "OS_APPLICATION_CREDENTIAL_SECRET": "secret"}):
            result = poll(cfg, client=FakeClient([ProbeError("connection_failed", "down")]))
        self.assertEqual(result["observations"][0]["health"], "down")

    def test_missing_credentials_do_not_make_request(self):
        client = FakeClient([])
        cfg = {"name": "cloud", "type": "openstack", "url": "https://identity.test", "credential_id_env": "HD_TEST_MISSING"}
        with patch.dict(os.environ, {}, clear=True):
            result = poll(cfg, client=client)
        self.assertEqual(client.calls, [])
        self.assertEqual(result["observations"][0]["health"], "unknown")

    def test_proxmox_inventory_and_quorum(self):
        cfg = {"name": "pve", "type": "proxmox", "url": "https://pve.test:8006"}
        responses = [({"data": [{"id": "qemu/100", "type": "qemu", "name": "worker", "status": "running", "node": "pve1"}]}, {}), ({"data": [{"type": "cluster", "quorate": 0}]}, {})]
        client = FakeClient(responses)
        with patch.dict(os.environ, {"PROXMOX_TOKEN_ID": "monitor@pve!hostdelta", "PROXMOX_TOKEN_SECRET": "secret"}):
            result = poll(cfg, previous={"resources": {}}, client=client)
        self.assertEqual(client.calls[0][1]["Authorization"], "PVEAPIToken=monitor@pve!hostdelta=secret")
        self.assertEqual(result["events"][0]["kind"], "inventory_changed")
        self.assertEqual(result["observations"][-1]["health"], "down")
        self.assertNotIn("secret", json.dumps(result))

    def test_worker_timeout_preserves_checkpoint(self):
        import subprocess
        cfg = {"name": "pve", "type": "proxmox", "url": "https://pve.test"}
        with patch("hostdelta.adapters.subprocess.run", side_effect=subprocess.TimeoutExpired("worker", 1)):
            result = bounded_poll(cfg, {"resources": {}})
        self.assertIsNone(result["checkpoint"])
        self.assertTrue(all(o["health"] == "unknown" for o in result["observations"]))


class HTTPIntegrationTests(unittest.TestCase):
    @contextmanager
    def server(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append((self.path, body))
                self.send_response(201)
                self.send_header("X-Subject-Token", "fixture-token")
                self.end_headers()
                self.wfile.write(b'{"token":{"catalog":[]}}')

            def do_GET(self):
                calls.append((self.path, self.headers.get("X-Auth-Token")))
                if self.path == "/redirect":
                    status = 302
                elif self.path == "/broken":
                    status = 503
                else:
                    status = {"/unauthorized": 401, "/forbidden": 403}.get(self.path, 200)
                self.send_response(status)
                if self.path == "/redirect":
                    self.send_header("Location", "/should-not-receive-token")
                payload = b'{"versions":[]}' if status == 200 else b"FAKE-response-body"
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", calls
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_real_http_auth_probe_and_worker(self):
        with self.server() as (base, calls), patch.dict(os.environ, {"OS_APPLICATION_CREDENTIAL_ID": "fixture-id", "OS_APPLICATION_CREDENTIAL_SECRET": "fixture-secret"}):
            cfg = {"name": "fixture", "type": "openstack", "url": base + "/v3", "allow_http": True, "services": {"compute": base + "/compute"}}
            result = bounded_poll(cfg)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls[0][0], "/v3/auth/tokens")
        self.assertEqual(calls[1], ("/compute", "fixture-token"))
        self.assertNotIn("fixture-secret", json.dumps(result))

    def test_redirect_cannot_forward_token(self):
        with self.server() as (base, calls):
            for _ in range(3):
                with self.assertRaises(ProbeError) as failure:
                    HTTPClient({"allow_http": True}).request(base + "/redirect", {"X-Auth-Token": "FAKE-request-secret"})
                self.assertEqual(failure.exception.code, "http_302")
                self.assertNotIn("FAKE-request-secret", str(failure.exception))
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(path == "/redirect" and token == "FAKE-request-secret" for path, token in calls))

    def test_repeated_http_errors_are_closed_and_sanitized(self):
        statuses = {"/unauthorized": (401, "unknown"), "/forbidden": (403, "unknown"), "/broken": (503, "down")}
        with self.server() as (base, calls):
            client = HTTPClient({"allow_http": True})
            for path, (status, health) in statuses.items():
                for _ in range(3):
                    with self.assertRaises(ProbeError) as failure:
                        client.request(base + path, {"X-Auth-Token": "FAKE-request-secret"})
                    self.assertEqual(failure.exception.code, f"http_{status}")
                    self.assertEqual(failure.exception.health, health)
                    self.assertNotIn("FAKE-request-secret", str(failure.exception))
                    self.assertNotIn("FAKE-response-body", str(failure.exception))
        self.assertEqual(len(calls), 9)
        self.assertTrue(all(token == "FAKE-request-secret" for _, token in calls))

    def test_http_error_fixture_response_is_closed(self):
        class CloseTrackingBody(io.BytesIO):
            pass

        for status in (401, 403, 302, 503):
            body = CloseTrackingBody(b"FAKE-response-body")
            response = urllib.error.HTTPError(
                "http://identity.test/probe",
                status,
                "synthetic failure",
                Message(),
                body,
            )
            client = HTTPClient({"allow_http": True})
            with patch.object(client.opener, "open", side_effect=response):
                with self.assertRaises(ProbeError) as failure:
                    client.request(
                        "http://identity.test/probe",
                        {"X-Auth-Token": "FAKE-request-secret"},
                    )
            self.assertTrue(body.closed, f"HTTP {status} response body was not closed")
            self.assertEqual(failure.exception.code, f"http_{status}")
            self.assertNotIn("FAKE-response-body", str(failure.exception))
            self.assertNotIn("identity.test", str(failure.exception))
            self.assertNotIn("FAKE-request-secret", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
