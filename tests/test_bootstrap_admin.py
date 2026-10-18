"""CLI bootstrap administrator tests; synthetic credentials only."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
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

from portal.models import AuditEvent, WorkerState

User = get_user_model()

USERNAME = "cli-admin"
OTHER_USERNAME = "cli-other-admin"
PASSWORD = "Synthetic-CLI-Passphrase-9"
NEW_PASSWORD = "Synthetic-CLI-Reset-Passphrase-7"


def run_bootstrap(username=USERNAME, password=PASSWORD):
    answers = iter([username, password, password])
    stdout = StringIO()
    with patch("builtins.input", side_effect=lambda prompt="": next(answers)):
        with patch(
            "portal.management.commands.bootstrap_admin.getpass",
            side_effect=lambda prompt="": next(answers),
        ):
            call_command("bootstrap_admin", stdout=stdout)
    return stdout.getvalue()


class BootstrapAdminCommandTests(TestCase):
    def test_creates_exactly_one_administrator(self):
        output = run_bootstrap()

        self.assertEqual(User.objects.count(), 1)
        admin = User.objects.get()
        self.assertEqual(admin.username, USERNAME)
        self.assertTrue(admin.is_active)
        self.assertTrue(admin.is_staff)
        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.check_password(PASSWORD))
        self.assertNotIn(USERNAME, output)
        self.assertNotIn(PASSWORD, output)

    def test_refuses_to_create_a_second_user(self):
        User.objects.create_user(username=OTHER_USERNAME, password=PASSWORD)

        with self.assertRaises(CommandError):
            run_bootstrap()

        self.assertEqual(User.objects.count(), 1)
        self.assertFalse(User.objects.filter(username=USERNAME).exists())

    def test_resets_the_existing_same_username(self):
        user = User.objects.create_user(
            username=USERNAME, password="Old-Synthetic-Passphrase-1"
        )
        self.assertFalse(user.is_staff)

        run_bootstrap(password=NEW_PASSWORD)

        self.assertEqual(User.objects.count(), 1)
        user.refresh_from_db()
        self.assertTrue(user.check_password(NEW_PASSWORD))
        self.assertTrue(user.is_active)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)


@skipUnlessDBFeature("has_select_for_update")
class BootstrapAdminPostgresConcurrencyTests(TransactionTestCase):
    def setUp(self):
        WorkerState.objects.create(
            key="main", owner="worker", heartbeat=timezone.now()
        )

    def test_cli_and_web_first_setup_keep_one_administrator(self):
        barrier = Barrier(2)

        def cli():
            close_old_connections()
            try:
                barrier.wait(timeout=20)
                try:
                    run_bootstrap(username="cli-concurrent-admin")
                    return "created"
                except CommandError:
                    return "rejected"
            finally:
                close_old_connections()

        def web():
            close_old_connections()
            try:
                barrier.wait(timeout=20)
                return Client().post(
                    reverse("portal:setup"),
                    {
                        "username": "web-concurrent-admin",
                        "password1": PASSWORD,
                        "password2": PASSWORD,
                    },
                    HTTP_HOST="127.0.0.1",
                ).status_code
            finally:
                close_old_connections()

        with override_settings(INITIAL_SETUP_DEADLINE=int(time.time()) + 600):
            with ThreadPoolExecutor(max_workers=2) as executor:
                cli_future = executor.submit(cli)
                web_future = executor.submit(web)
                cli_result = cli_future.result()
                web_status = web_future.result()

        self.assertEqual(User.objects.count(), 1)
        self.assertIn(cli_result, {"created", "rejected"})
        self.assertIn(web_status, {302, 404})
        self.assertEqual(
            int(cli_result == "created") + int(web_status == 302), 1
        )
        self.assertEqual(
            AuditEvent.objects.count(), 1 if web_status == 302 else 0
        )
