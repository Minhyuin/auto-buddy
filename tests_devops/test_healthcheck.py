"""Tests for the standard-library container health probe."""

from __future__ import annotations

import contextlib
import io
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from deployment import healthcheck


class _Handler(BaseHTTPRequestHandler):
    status = 200
    body = b'{"status":"ok"}'
    last_host = None

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).last_host = self.headers.get("Host")
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args: object) -> None:
        pass


class HealthcheckTests(unittest.TestCase):
    def setUp(self) -> None:
        _Handler.status = 200
        _Handler.body = b'{"status":"ok"}'
        _Handler.last_host = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/health/"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_healthy_response(self) -> None:
        healthy, detail = healthcheck.probe(self.url, timeout=2)
        self.assertTrue(healthy)
        self.assertIn("HTTP 200", detail)

    def test_unavailable_response(self) -> None:
        _Handler.status = 503
        _Handler.body = b'{"status":"unavailable"}'
        healthy, detail = healthcheck.probe(self.url, timeout=2)
        self.assertFalse(healthy)
        self.assertIn("503", detail)

    def test_non_json_response(self) -> None:
        _Handler.body = b"<html>oops</html>"
        healthy, detail = healthcheck.probe(self.url, timeout=2)
        self.assertFalse(healthy)
        self.assertIn("not JSON", detail)

    def test_connection_failure(self) -> None:
        healthy, _ = healthcheck.probe("http://127.0.0.1:1/health/", timeout=0.5)
        self.assertFalse(healthy)

    def test_host_header_is_forwarded(self) -> None:
        healthy, _ = healthcheck.probe(self.url, timeout=2, host_header="portal.example.com")
        self.assertTrue(healthy)
        self.assertEqual(_Handler.last_host, "portal.example.com")

    def test_main_uses_environment_and_returns_status(self) -> None:
        old_url = os.environ.get("HEALTHCHECK_URL")
        old_timeout = os.environ.get("HEALTHCHECK_TIMEOUT")
        os.environ["HEALTHCHECK_URL"] = self.url
        os.environ["HEALTHCHECK_TIMEOUT"] = "2"
        try:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(healthcheck.main([]), 0)
            self.assertIn("healthy", stdout.getvalue())
        finally:
            for key, value in (("HEALTHCHECK_URL", old_url), ("HEALTHCHECK_TIMEOUT", old_timeout)):
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_invalid_status_body_is_unhealthy(self) -> None:
        _Handler.body = json.dumps({"status": "starting"}).encode()
        healthy, _ = healthcheck.probe(self.url, timeout=2)
        self.assertFalse(healthy)


if __name__ == "__main__":
    unittest.main()
