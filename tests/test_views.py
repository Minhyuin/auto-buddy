"""Tests for the one-time web administrator setup (``portal:setup``)."""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.test import (
    Client,
    TestCase,
    TransactionTestCase,
    override_settings,
    skipUnlessDBFeature,
)
from django.urls import reverse
from django.utils import timezone

from portal.forms import InitialSetupForm
from portal.models import AuditEvent, WorkerState

User = get_user_model()

USERNAME = "setup-admin"
VALID_PASSWORD = "Synthetic-Setup-Passphrase-9"


def deadline_from_now(seconds):
    return int(time.time()) + seconds


class SetupTestBase(TestCase):
    def setUp(self):
        # The worker row is created during deployment; setup locks this row.
        WorkerState.objects.create(key="main", owner="worker", heartbeat=timezone.now())

    def open_window(self, seconds=600):
        return override_settings(INITIAL_SETUP_DEADLINE=deadline_from_now(seconds))

    def get_setup(self, client=None, host="127.0.0.1"):
        return (client or self.client).get(reverse("portal:setup"), HTTP_HOST=host)

    def post_setup(self, client=None, host="127.0.0.1", **overrides):
        data = {
            "username": USERNAME,
            "password1": VALID_PASSWORD,
            "password2": VALID_PASSWORD,
        }
        data.update(overrides)
        return (client or self.client).post(
            reverse("portal:setup"), data, HTTP_HOST=host
        )


class SetupClosedTests(SetupTestBase):
    def test_setup_is_closed_by_default(self):
        self.assertEqual(self.get_setup().status_code, 404)
        self.assertEqual(self.post_setup().status_code, 404)
        self.assertFalse(User.objects.exists())

    def test_zero_deadline_is_closed(self):
        with override_settings(INITIAL_SETUP_DEADLINE=0):
            self.assertEqual(self.get_setup().status_code, 404)
        self.assertFalse(User.objects.exists())

    def test_expired_deadline_is_closed(self):
        with self.open_window(seconds=-1):
            self.assertEqual(self.get_setup().status_code, 404)
            self.assertEqual(self.post_setup().status_code, 404)
        self.assertFalse(User.objects.exists())

    def test_deadline_far_in_the_future_is_closed(self):
        with self.open_window(seconds=3600):
            self.assertEqual(self.get_setup().status_code, 404)
            self.assertEqual(self.post_setup().status_code, 404)
        self.assertFalse(User.objects.exists())

    def test_login_stays_normal_when_setup_is_closed(self):
        response = self.client.get(reverse("portal:login"), HTTP_HOST="127.0.0.1")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "管理员登录")


class SetupHostTests(SetupTestBase):
    def test_non_loopback_host_never_opens_setup(self):
        with self.open_window():
            # ``testserver`` is what Django's test client sends by default.
            self.assertEqual(self.get_setup(host="testserver").status_code, 404)
            self.assertEqual(self.post_setup(host="testserver").status_code, 404)
        self.assertFalse(User.objects.exists())

    def test_login_does_not_redirect_for_non_loopback_host(self):
        with self.open_window():
            response = self.client.get(reverse("portal:login"), HTTP_HOST="testserver")
        self.assertEqual(response.status_code, 200)

    def test_public_domain_never_opens_even_when_allowed_host(self):
        with override_settings(ALLOWED_HOSTS=["portal.example.com"]):
            with self.open_window():
                self.assertEqual(
                    self.get_setup(host="portal.example.com").status_code, 404
                )
                response = self.client.get(
                    reverse("portal:login"), HTTP_HOST="portal.example.com"
                )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "管理员登录")
        self.assertFalse(User.objects.exists())

    def test_loopback_hosts_with_port_open_setup(self):
        with self.open_window():
            for host in ("localhost:8443", "127.0.0.1:8443"):
                with self.subTest(host=host):
                    self.assertEqual(self.get_setup(host=host).status_code, 200)

    def test_ipv6_loopback_host_opens_setup(self):
        with override_settings(ALLOWED_HOSTS=["[::1]"]):
            with self.open_window():
                response = self.get_setup(host="[::1]:8443")
        self.assertEqual(response.status_code, 200)

    def test_login_redirects_to_setup_for_loopback_host(self):
        with self.open_window():
            response = self.client.get(
                reverse("portal:login"), HTTP_HOST="localhost:8443"
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("portal:setup"))


class SetupExistingUserTests(SetupTestBase):
    def setUp(self):
        super().setUp()
        self.existing = User.objects.create_user(
            username="existing-admin",
            password="Synthetic-Existing-Passphrase-1",
        )

    def test_any_existing_user_closes_setup_even_inside_the_window(self):
        with self.open_window():
            self.assertEqual(self.get_setup().status_code, 404)
            self.assertEqual(self.post_setup().status_code, 404)
        self.assertEqual(User.objects.count(), 1)

    def test_login_page_does_not_redirect_when_user_exists(self):
        with self.open_window():
            response = self.client.get(reverse("portal:login"), HTTP_HOST="127.0.0.1")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "管理员登录")


class SetupFormPageTests(SetupTestBase):
    def test_page_has_csrf_token_and_no_prefilled_password_value(self):
        with self.open_window():
            response = self.get_setup()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "首次初始化管理员")
        self.assertContains(response, "csrfmiddlewaretoken")
        rendered_values = re.findall(
            r'name="password[12]" value="([^"]*)"', response.content.decode("utf-8")
        )
        self.assertEqual(rendered_values, ["", ""])
        self.assertNotContains(response, VALID_PASSWORD)


class SetupSuccessTests(SetupTestBase):
    def test_creates_staff_superuser_signs_in_and_audits(self):
        with self.open_window():
            response = self.post_setup()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/")
        self.assertIn("_auth_user_id", self.client.session)

        user = User.objects.get()
        self.assertEqual(user.username, USERNAME)
        self.assertTrue(user.is_active)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.check_password(VALID_PASSWORD))

        event = AuditEvent.objects.get()
        self.assertEqual(event.action, "admin.initial_setup")
        self.assertEqual(event.actor_id, user.pk)
        self.assertIsNone(event.account)

    def test_dashboard_is_reachable_after_setup(self):
        with self.open_window():
            self.post_setup()
            response = self.client.get(reverse("portal:dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_setup_closes_immediately_and_second_admin_is_impossible(self):
        with self.open_window():
            self.assertEqual(self.post_setup().status_code, 302)
            self.assertEqual(User.objects.count(), 1)
            self.assertEqual(self.get_setup().status_code, 404)
            self.assertEqual(
                self.post_setup(username="second-admin").status_code, 404
            )
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.count(), 1)

    def test_new_admin_can_log_in_after_logout(self):
        with self.open_window():
            self.post_setup()
        self.client.logout()
        response = self.client.post(
            reverse("portal:login"),
            {"username": USERNAME, "password": VALID_PASSWORD},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/")


class SetupValidationTests(SetupTestBase):
    def test_short_password_is_rejected_and_not_echoed(self):
        password = "Synthetic1"
        with self.open_window():
            response = self.post_setup(password1=password, password2=password)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.exists())
        self.assertFalse(AuditEvent.objects.exists())
        self.assertIn("password1", response.context["form"].errors)
        self.assertNotContains(response, password)

    def test_numeric_password_is_rejected_by_django_validator(self):
        password = "123456789012"
        with self.open_window():
            response = self.post_setup(password1=password, password2=password)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.exists())
        self.assertIn("password2", response.context["form"].errors)
        self.assertNotContains(response, password)

    def test_mismatched_passwords_are_rejected_without_echoing_either(self):
        first = "Synthetic-First-Passphrase-9"
        second = "Synthetic-Second-Passphrase-9"
        with self.open_window():
            response = self.post_setup(password1=first, password2=second)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.exists())
        self.assertIn("password2", response.context["form"].errors)
        self.assertNotContains(response, first)
        self.assertNotContains(response, second)

    def test_invalid_username_is_rejected(self):
        with self.open_window():
            response = self.post_setup(username="bad name!")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.exists())
        self.assertIn("username", response.context["form"].errors)

    def test_failed_post_keeps_the_window_usable(self):
        with self.open_window():
            self.assertEqual(
                self.post_setup(password1="short", password2="short").status_code,
                200,
            )
            self.assertEqual(self.post_setup().status_code, 302)
        self.assertEqual(User.objects.count(), 1)


class SetupCsrfTests(SetupTestBase):
    def test_post_without_csrf_token_is_rejected(self):
        client = Client(enforce_csrf_checks=True)
        with self.open_window():
            response = self.post_setup(client)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.exists())

    def test_post_with_csrf_token_succeeds(self):
        client = Client(enforce_csrf_checks=True)
        with self.open_window():
            page = self.get_setup(client)
            match = re.search(
                r'name="csrfmiddlewaretoken" value="([^"]+)"',
                page.content.decode("utf-8"),
            )
            self.assertIsNotNone(match)
            response = self.post_setup(
                client, csrfmiddlewaretoken=match.group(1)
            )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.exists())


@skipUnlessDBFeature("has_select_for_update")
class SetupPostgresConcurrencyTests(TransactionTestCase):
    """Both requests validate before one takes the WorkerState lock."""

    def setUp(self):
        WorkerState.objects.create(
            key="main", owner="worker", heartbeat=timezone.now()
        )

    def open_window(self):
        return override_settings(INITIAL_SETUP_DEADLINE=deadline_from_now(600))

    def test_simultaneous_posts_create_exactly_one_administrator(self):
        barrier = Barrier(2)
        original_is_valid = InitialSetupForm.is_valid

        def synchronized_is_valid(form):
            valid = original_is_valid(form)
            # Both requests have now passed the unlocked pre-check and form
            # validation; from here on only the WorkerState row lock may
            # decide which one creates the administrator.
            barrier.wait(timeout=20)
            return valid

        def post(username):
            close_old_connections()
            try:
                client = Client()
                return client.post(
                    reverse("portal:setup"),
                    {
                        "username": username,
                        "password1": VALID_PASSWORD,
                        "password2": VALID_PASSWORD,
                    },
                    HTTP_HOST="127.0.0.1",
                ).status_code
            finally:
                close_old_connections()

        usernames = ["concurrent-admin-a", "concurrent-admin-b"]
        with self.open_window():
            with patch.object(
                InitialSetupForm, "is_valid", synchronized_is_valid
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    statuses = list(executor.map(post, usernames))

        self.assertCountEqual(statuses, [302, 404])
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.count(), 1)
        created = User.objects.get()
        self.assertIn(created.username, usernames)
        self.assertTrue(created.is_staff)
        self.assertTrue(created.is_superuser)
