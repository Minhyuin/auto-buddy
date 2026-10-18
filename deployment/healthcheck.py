#!/usr/bin/env python3
"""Container health probe for the AutoBuddy web service.

Django's /health/ view performs a database round-trip and answers
``{"status": "ok"}`` with HTTP 200, or ``{"status": "unavailable"}`` with HTTP
503. This script only reports healthy on a 200 with that exact status value.

It deliberately uses the standard library (the image installs no HTTP client).
The host updater performs an equivalent probe with its own bounded HTTP client.

Environment:
    HEALTHCHECK_URL      default http://127.0.0.1:8000/health/
    HEALTHCHECK_TIMEOUT  seconds, default 5
    HEALTHCHECK_HOST     optional Host header (e.g. the public hostname)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8000/health/"
MAX_BODY = 64 * 1024


def probe(url: str, timeout: float = 5.0, host_header: str | None = None) -> tuple[bool, str]:
    """Perform one health request and return (healthy, human-readable detail)."""
    headers = {
        "Accept": "application/json",
        "User-Agent": "auto-buddy-healthcheck/1",
    }
    if host_header:
        headers["Host"] = host_header

    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            http_status = response.status
            body = response.read(MAX_BODY)
    except urllib.error.HTTPError as exc:
        http_status = exc.code
        body = exc.read(MAX_BODY)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"request failed: {exc}"

    try:
        payload = json.loads(body.decode("utf-8", "replace") or "{}")
    except ValueError:
        return False, f"HTTP {http_status} body is not JSON"

    status = payload.get("status") if isinstance(payload, dict) else None
    if http_status == 200 and status == "ok":
        return True, "HTTP 200 status=ok"
    return False, f"HTTP {http_status} status={status!r}"


def main(argv: list[str] | None = None) -> int:
    del argv  # no command-line arguments are used; env only
    url = os.environ.get("HEALTHCHECK_URL") or DEFAULT_URL
    host_header = os.environ.get("HEALTHCHECK_HOST") or None

    try:
        timeout = float(os.environ.get("HEALTHCHECK_TIMEOUT", "5"))
    except ValueError:
        timeout = 5.0
    if timeout <= 0:
        timeout = 5.0

    healthy, detail = probe(url, timeout=timeout, host_header=host_header)
    print(f"{'healthy' if healthy else 'unhealthy'}: {detail} ({url})", flush=True)
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
