"""Plain-HTTP mode is an explicit, opt-in transport exception.

The settings module is imported in isolated subprocesses so one case cannot
observe another case's ``AUTOBUDDY_*`` environment or mutate the settings of
the running test process. Request-level tests then show that the flag relaxes
only transport enforcement, not login, CSRF or session handling.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

REPO_ROOT = Path(__file__).resolve().parent.parent
ADMIN_USERNAME = "synthetic-admin"
ADMIN_PASSWORD = "Synthetic-Plain-Http-Passphrase-4"

# Values observed after a clean import of autobuddy.settings.
PROBE_KEYS = (
    "DEBUG",
    "ALLOW_PLAIN_HTTP",
    "ENFORCE_HTTPS",
    "SECURE_SSL_REDIRECT",
    "SECURE_HSTS_SECONDS",
    "SECURE_PROXY_SSL_HEADER",
    "SESSION_COOKIE_SECURE",
    "SESSION_COOKIE_HTTPONLY",
    "SESSION_COOKIE_SAMESITE",
    "SESSION_COOKIE_AGE",
    "SESSION_COOKIE_NAME",
    "CSRF_COOKIE_SECURE",
    "CSRF_COOKIE_HTTPONLY",
    "CSRF_COOKIE_NAME",
    "LOGIN_URL",
    "MIDDLEWARE",
)
PROBE = (
    "import json\n"
    "from autobuddy import settings\n"
    f"keys = {PROBE_KEYS!r}\n"
    "print(json.dumps({key: getattr(settings, key) for key in keys}))\n"
)
# Reload in one interpreter to check every documented spelling of the flag.
FLAG_MATRIX_PROBE = (
    "import importlib, json, os\n"
    "from autobuddy import settings\n"
    "results = {}\n"
    "for value in ('', '0', 'false', 'no', 'off', '1', 'true', 'yes', 'on'):\n"
    "    os.environ['AUTOBUDDY_ALLOW_HTTP'] = value\n"
    "    results[value] = importlib.reload(settings).SECURE_SSL_REDIRECT\n"
    "print(json.dumps(results))\n"
)
# The outer test run's environment must never decide what a probe observes.
INHERITED_PREFIXES = ("AUTOBUDDY_", "DJANGO_", "TEST_DATABASE_")
SECRET_VARIABLES = {
    "SECRET_KEY_FILE",
    "CREDENTIAL_KEYS_FILE",
    "DATABASE_PASSWORD_FILE",
    "ACTIVE_CREDENTIAL_KEY",
}


def _environment(overrides: dict[str, str]) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(INHERITED_PREFIXES) and key not in SECRET_VARIABLES
    }
    env.update({"PYTHONPATH": str(REPO_ROOT), "DJANGO_DEBUG": "false"})
    env.update(overrides)
    return env


def _write_synthetic_secrets(directory: Path) -> dict[str, str]:
    """Create throwaway secret files so a production-shaped import can run."""
    (directory / "django_key").write_text(
        base64.b64encode(os.urandom(48)).decode("ascii"), encoding="utf-8"
    )
    credential_keys = {"test": base64.b64encode(os.urandom(32)).decode("ascii")}
    (directory / "credential_keys").write_text(
        json.dumps(credential_keys), encoding="utf-8"
    )
    (directory / "db_password").write_text(
        base64.b64encode(os.urandom(32)).decode("ascii"), encoding="utf-8"
    )
    return {
        "SECRET_KEY_FILE": str(directory / "django_key"),
        "CREDENTIAL_KEYS_FILE": str(directory / "credential_keys"),
        "DATABASE_PASSWORD_FILE": str(directory / "db_password"),
    }


def _import_settings(
    overrides: dict[str, str] | None = None,
    *,
    source: str = PROBE,
    synthetic: bool = False,
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        env = _environment(overrides or {})
        if not synthetic:
            env.update(_write_synthetic_secrets(directory))
        return subprocess.run(
            [sys.executable, "-c", source],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )


def load_settings(
    overrides: dict[str, str] | None = None,
    *,
    source: str = PROBE,
    synthetic: bool = False,
) -> dict:
    completed = _import_settings(overrides, source=source, synthetic=synthetic)
    if completed.returncode:
        raise AssertionError(
            f"settings import failed unexpectedly:\n{completed.stderr}"
        )
    return json.loads(completed.stdout)


class SettingsImportTests(unittest.TestCase):
    """Import-time behaviour of the AUTOBUDDY_ALLOW_HTTP switch."""

    def test_default_is_https_enforced(self):
        values = load_settings()
        self.assertFalse(values["ALLOW_PLAIN_HTTP"])
        self.assertTrue(values["ENFORCE_HTTPS"])
        self.assertTrue(values["SECURE_SSL_REDIRECT"])
        self.assertTrue(values["SESSION_COOKIE_SECURE"])
        self.assertTrue(values["CSRF_COOKIE_SECURE"])
        self.assertEqual(values["SECURE_HSTS_SECONDS"], 31536000)
        self.assertEqual(
            values["SECURE_PROXY_SSL_HEADER"],
            ["HTTP_X_FORWARDED_PROTO", "https"],
        )
        self.assertEqual(values["SESSION_COOKIE_NAME"], "autobuddy_session")
        self.assertEqual(values["CSRF_COOKIE_NAME"], "autobuddy_csrftoken")

    def test_explicit_plain_http_mode_relaxes_transport_only(self):
        values = load_settings({"AUTOBUDDY_ALLOW_HTTP": "1"})
        self.assertTrue(values["ALLOW_PLAIN_HTTP"])
        self.assertFalse(values["ENFORCE_HTTPS"])
        self.assertFalse(values["SECURE_SSL_REDIRECT"])
        self.assertFalse(values["SESSION_COOKIE_SECURE"])
        self.assertFalse(values["CSRF_COOKIE_SECURE"])
        self.assertEqual(values["SECURE_HSTS_SECONDS"], 0)
        # No trusted proxy sits in front of a direct plain-HTTP listener, so a
        # client-supplied X-Forwarded-Proto must not make is_secure() true.
        self.assertIsNone(values["SECURE_PROXY_SSL_HEADER"])
        # Login, CSRF, session and cookie hardening are not transport flags.
        self.assertTrue(values["SESSION_COOKIE_HTTPONLY"])
        self.assertTrue(values["CSRF_COOKIE_HTTPONLY"])
        self.assertEqual(values["SESSION_COOKIE_SAMESITE"], "Strict")
        self.assertEqual(values["SESSION_COOKIE_AGE"], 1800)
        # Namespaced names avoid cookie collisions with other portals on the
        # same public IP and apply in every mode, not just plain HTTP.
        self.assertEqual(values["SESSION_COOKIE_NAME"], "autobuddy_session")
        self.assertEqual(values["CSRF_COOKIE_NAME"], "autobuddy_csrftoken")
        self.assertEqual(values["LOGIN_URL"], "/login/")
        self.assertIn(
            "django.middleware.csrf.CsrfViewMiddleware", values["MIDDLEWARE"]
        )
        self.assertIn(
            "django.contrib.auth.middleware.AuthenticationMiddleware",
            values["MIDDLEWARE"],
        )

    def test_flag_spellings_are_a_closed_set(self):
        results = load_settings(source=FLAG_MATRIX_PROBE)
        for value in ("", "0", "false", "no", "off"):
            with self.subTest(value=value):
                self.assertTrue(results[value])
        for value in ("1", "true", "yes", "on"):
            with self.subTest(value=value):
                self.assertFalse(results[value])

    def test_unknown_flag_value_fails_closed(self):
        for value in ("2", "enabled", "yes please"):
            with self.subTest(value=value):
                completed = _import_settings({"AUTOBUDDY_ALLOW_HTTP": value})
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(
                    "AUTOBUDDY_ALLOW_HTTP must be 0 or 1", completed.stderr
                )

    def test_production_debug_is_still_rejected_in_plain_http_mode(self):
        for overrides in (
            {"DJANGO_DEBUG": "true"},
            {"DJANGO_DEBUG": "true", "AUTOBUDDY_ALLOW_HTTP": "1"},
        ):
            with self.subTest(overrides=overrides):
                completed = _import_settings(overrides)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(
                    "DJANGO_DEBUG=true is forbidden outside synthetic tests",
                    completed.stderr,
                )

    def test_synthetic_testing_mode_needs_no_flag_or_secret_files(self):
        values = load_settings({"AUTOBUDDY_TESTING": "1"}, synthetic=True)
        self.assertFalse(values["DEBUG"])
        self.assertFalse(values["ALLOW_PLAIN_HTTP"])
        self.assertFalse(values["SECURE_SSL_REDIRECT"])
        self.assertEqual(values["SECURE_HSTS_SECONDS"], 0)


# Transport values produced by ALLOW_PLAIN_HTTP=1 in production.
PLAIN_HTTP_SETTINGS = {
    "SECURE_SSL_REDIRECT": False,
    "SESSION_COOKIE_SECURE": False,
    "CSRF_COOKIE_SECURE": False,
    "SECURE_HSTS_SECONDS": 0,
    "SECURE_PROXY_SSL_HEADER": None,
}


class PlainHttpRequestTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username=ADMIN_USERNAME,
            password=ADMIN_PASSWORD,
            is_staff=True,
            is_superuser=True,
        )

    def test_https_enforcement_still_redirects_when_enabled(self):
        with override_settings(
            SECURE_SSL_REDIRECT=True,
            SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
        ):
            response = self.client.get("/login/")
            self.assertEqual(response.status_code, 301)
            self.assertEqual(response["Location"], "https://testserver/login/")
            # The container health probe stays exempt from the redirect.
            self.assertEqual(self.client.get("/health/").status_code, 200)

    @override_settings(**PLAIN_HTTP_SETTINGS)
    def test_plain_http_login_flow_keeps_csrf_and_session_handling(self):
        client = Client(enforce_csrf_checks=True)
        page = client.get("/login/")
        self.assertEqual(page.status_code, 200)
        self.assertFalse(page.cookies[settings.CSRF_COOKIE_NAME]["secure"])
        # No public entry point bypasses the administrator login.
        self.assertEqual(client.get("/").status_code, 302)
        token = client.cookies[settings.CSRF_COOKIE_NAME].value
        response = client.post(
            "/login/",
            {
                "username": ADMIN_USERNAME,
                "password": ADMIN_PASSWORD,
                "csrfmiddlewaretoken": token,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/")
        self.assertFalse(response.cookies[settings.SESSION_COOKIE_NAME]["secure"])
        self.assertEqual(client.get("/").status_code, 200)

    @override_settings(**PLAIN_HTTP_SETTINGS)
    def test_cookie_names_are_namespaced_for_colocated_portals(self):
        """Browsers ignore the port, so the names must not clash with defaults."""
        client = Client(enforce_csrf_checks=True)
        client.get("/login/")
        self.assertIn("autobuddy_csrftoken", client.cookies)
        self.assertNotIn("csrftoken", client.cookies)
        token = client.cookies["autobuddy_csrftoken"].value

        response = client.post(
            "/login/",
            {
                "username": ADMIN_USERNAME,
                "password": ADMIN_PASSWORD,
                "csrfmiddlewaretoken": token,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("autobuddy_session", client.cookies)
        self.assertNotIn("sessionid", client.cookies)

    @override_settings(**PLAIN_HTTP_SETTINGS)
    def test_plain_http_mode_does_not_bypass_csrf(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(
            "/login/", {"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
        )
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)
        self.assertEqual(client.get("/").status_code, 302)

    @override_settings(
        ALLOWED_HOSTS=["portal.example.com", "127.0.0.1"],
        CSRF_TRUSTED_ORIGINS=["http://portal.example.com:9443"],
        **PLAIN_HTTP_SETTINGS,
    )
    def test_reverse_proxy_origin_with_port_is_accepted_when_configured(self):
        """Host $host drops the port, so the exact http:// origin must be trusted."""
        client = Client(enforce_csrf_checks=True)
        # The proxy forwards a port-less Host ($host); the browser Origin keeps
        # the public port, so only the trusted origin can verify the POST.
        host = "portal.example.com"
        page = client.get("/login/", HTTP_HOST=host)
        self.assertEqual(page.status_code, 200)
        token = client.cookies[settings.CSRF_COOKIE_NAME].value
        response = client.post(
            "/login/",
            {
                "username": ADMIN_USERNAME,
                "password": ADMIN_PASSWORD,
                "csrfmiddlewaretoken": token,
            },
            HTTP_HOST=host,
            HTTP_ORIGIN="http://portal.example.com:9443",
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/")

    @override_settings(
        ALLOWED_HOSTS=["portal.example.com", "127.0.0.1"],
        **PLAIN_HTTP_SETTINGS,
    )
    def test_port_bearing_origin_is_rejected_without_a_trusted_origin(self):
        client = Client(enforce_csrf_checks=True)
        host = "portal.example.com"
        client.get("/login/", HTTP_HOST=host)
        token = client.cookies[settings.CSRF_COOKIE_NAME].value
        response = client.post(
            "/login/",
            {
                "username": ADMIN_USERNAME,
                "password": ADMIN_PASSWORD,
                "csrfmiddlewaretoken": token,
            },
            HTTP_HOST=host,
            HTTP_ORIGIN="http://portal.example.com:9443",
        )
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)
