"""End-to-end tests for the administrator portal.

Run with ``AUTOBUDDY_TESTING=1 python manage.py test``.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import time, timedelta

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from portal.crypto import decrypt
from portal.models import Account, LoginAttempt, Run, WorkerState
from portal.services import normalize_session, save_account

User = get_user_model()

TOKEN = "test-access-token-abc123"
UID = "user-123456"
SESSION = {"accessToken": TOKEN, "uid": UID}


def flat_session(token=TOKEN, uid=UID, **extra):
    data = {"accessToken": token, "uid": uid}
    data.update(extra)
    return json.dumps(data)


class PortalTestBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="admin",
            password="correct-horse-battery-staple",
            is_staff=True,
            is_superuser=True,
        )
        self.client.force_login(self.user)

    def make_account(self, *, label="主账户", enabled=True, mode="full", poll_hours=4):
        account = Account(
            mode=mode,
            enabled=enabled,
            schedule_time=time(0, 5),
            poll_hours=poll_hours,
            timezone="Asia/Shanghai",
        )
        save_account(
            account=account,
            label=label,
            session={"accessToken": TOKEN, "uid": UID},
            actor=self.user,
        )
        return account

    def create_payload(self, **overrides):
        payload = {
            "label": "工作号",
            "mode": "full",
            "enabled": "on",
            "credentials": flat_session(),
            "schedule_time": "00:05",
            "poll_hours": "4",
            "timezone": "Asia/Shanghai",
        }
        payload.update(overrides)
        return payload


class AuthenticationTests(PortalTestBase):
    def test_login_page_sets_security_headers(self):
        self.client.logout()
        response = self.client.get(reverse("portal:login"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("default-src 'self'", response["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", response["Content-Security-Policy"])
        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertContains(response, "管理员登录")

    def test_dashboard_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse("portal:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("portal:login"), response["Location"])

    def test_normal_user_is_forbidden(self):
        normal = User.objects.create_user(username="operator", password="irrelevant-password")
        self.client.force_login(normal)
        response = self.client.get(reverse("portal:dashboard"))
        self.assertEqual(response.status_code, 403)

    def test_login_rate_limit_blocks_correct_password(self):
        self.client.logout()
        for _ in range(5):
            response = self.client.post(
                reverse("portal:login"),
                {"username": "admin", "password": "wrong-password"},
            )
            self.assertEqual(response.status_code, 200)

        attempt = LoginAttempt.objects.get()
        self.assertEqual(attempt.failures, 5)
        self.assertIsNotNone(attempt.blocked_until)
        self.assertEqual(attempt.key, hashlib.sha256(b"auto-buddy:login:v1:127.0.0.1").hexdigest())

        response = self.client.post(
            reverse("portal:login"),
            {"username": "admin", "password": "correct-horse-battery-staple"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertContains(response, "尝试次数过多")

    def test_login_recovers_after_window_expires(self):
        self.client.logout()
        for _ in range(5):
            self.client.post(
                reverse("portal:login"),
                {"username": "admin", "password": "wrong-password"},
            )
        attempt = LoginAttempt.objects.get()
        attempt.window_start = timezone.now() - timedelta(minutes=16)
        attempt.blocked_until = timezone.now() - timedelta(minutes=1)
        attempt.save(update_fields=["window_start", "blocked_until"])

        response = self.client.post(
            reverse("portal:login"),
            {"username": "admin", "password": "correct-horse-battery-staple"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/")
        self.assertIn("_auth_user_id", self.client.session)

    def test_login_error_is_generic(self):
        self.client.logout()
        response = self.client.post(
            reverse("portal:login"),
            {"username": "does-not-exist", "password": "nope"},
        )
        self.assertContains(response, "账号或密码不正确")
        self.assertNotContains(response, "不存在")
        self.assertNotContains(response, "密码错误")

    def test_logout_requires_post(self):
        self.assertEqual(self.client.get(reverse("portal:logout")).status_code, 405)
        response = self.client.post(reverse("portal:logout"))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)


class AccountCreateTests(PortalTestBase):
    def test_create_account_encrypts_credentials_and_never_renders_token(self):
        response = self.client.post(reverse("portal:account_create"), self.create_payload())
        self.assertEqual(response.status_code, 302)

        account = Account.objects.get()
        self.assertEqual(account.label, "工作号")
        self.assertNotIn(TOKEN, account.encrypted_payload)
        self.assertEqual(decrypt(account.encrypted_payload, account.id)["session"]["auth"]["accessToken"], TOKEN)

        page = self.client.get(reverse("portal:dashboard"))
        self.assertContains(page, "工作号")
        self.assertContains(page, "••••456")
        self.assertNotContains(page, TOKEN)
        self.assertNotContains(page, "user-123456")

    def test_edit_without_new_credentials_keeps_existing_session(self):
        account = self.make_account(label="旧备注")
        response = self.client.post(
            reverse("portal:account_edit", args=[account.pk]),
            self.create_payload(label="新备注", credentials=""),
        )
        self.assertEqual(response.status_code, 302)
        account.refresh_from_db()
        self.assertEqual(account.label, "新备注")
        self.assertEqual(decrypt(account.encrypted_payload, account.id)["session"]["auth"]["accessToken"], TOKEN)

    def test_create_requires_credentials(self):
        payload = self.create_payload(credentials="")
        response = self.client.post(reverse("portal:account_create"), payload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "新建账户时必须提供凭据")
        self.assertEqual(Account.objects.count(), 0)

    def test_file_upload_is_accepted(self):
        upload = SimpleUploadedFile(
            "credentials.json",
            flat_session().encode("utf-8"),
            content_type="application/json",
        )
        payload = self.create_payload(credentials="")
        payload["credentials_file"] = upload
        response = self.client.post(reverse("portal:account_create"), payload)
        self.assertEqual(response.status_code, 302)
        account = Account.objects.get()
        self.assertEqual(decrypt(account.encrypted_payload, account.id)["session"]["account"]["uid"], UID)

    def test_text_and_file_together_are_rejected(self):
        upload = SimpleUploadedFile("credentials.json", flat_session().encode(), content_type="application/json")
        payload = self.create_payload()
        payload["credentials_file"] = upload
        response = self.client.post(reverse("portal:account_create"), payload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "请只提供一种凭据输入方式")
        self.assertEqual(Account.objects.count(), 0)

    def test_unknown_flat_credential_field_is_rejected(self):
        payload = self.create_payload(credentials=flat_session(refreshToken="should-not-be-accepted"))
        response = self.client.post(reverse("portal:account_create"), payload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "凭证字段不受支持")
        self.assertEqual(Account.objects.count(), 0)

    def test_native_nested_credentials_ignore_extra_top_level_fields(self):
        nested = {
            "auth": {"accessToken": TOKEN},
            "account": {"uid": UID},
            "nativeMetadata": {"ignored": True},
        }
        payload = self.create_payload(credentials=json.dumps(nested))
        response = self.client.post(reverse("portal:account_create"), payload)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Account.objects.count(), 1)

    def test_invalid_form_never_echoes_submitted_credential(self):
        token = "SECRET_TOKEN_SHOULD_NOT_ECHO_123"
        payload = self.create_payload(
            credentials=flat_session(token=token),
            timezone="Mars/Base",
        )
        response = self.client.post(reverse("portal:account_create"), payload)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertNotIn(token, body)
        textarea = re.search(
            r'<textarea[^>]*name="credentials"[^>]*>(.*?)</textarea>', body, re.S
        )
        self.assertIsNotNone(textarea)
        self.assertEqual(textarea.group(1).strip(), "")

    def test_invalid_upload_does_not_echo_file_content(self):
        token = "SECRET_FILE_TOKEN_SHOULD_NOT_ECHO"
        upload = SimpleUploadedFile(
            "credentials.json",
            flat_session(token=token).encode(),
            content_type="application/json",
        )
        payload = self.create_payload(credentials="", timezone="Mars/Base")
        payload["credentials_file"] = upload
        response = self.client.post(reverse("portal:account_create"), payload)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(token, response.content.decode())

    def test_edit_form_uses_account_initial_values(self):
        account = self.make_account(label="仅签到账户", enabled=False, mode="signin", poll_hours=0)
        response = self.client.get(reverse("portal:account_edit", args=[account.pk]))
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(form.initial["label"], "仅签到账户")
        self.assertEqual(form.initial["mode"], "signin")
        self.assertFalse(form.initial["enabled"])
        self.assertEqual(form.initial["poll_hours"], 0)


class RunTests(PortalTestBase):
    def test_run_enqueues_manual_task_and_duplicate_is_rejected(self):
        account = self.make_account()
        response = self.client.post(reverse("portal:account_run", args=[account.pk]))
        self.assertEqual(response.status_code, 302)
        run = Run.objects.get()
        self.assertEqual(run.trigger, "manual")
        self.assertEqual(run.status, "queued")

        response = self.client.post(reverse("portal:account_run", args=[account.pk]), follow=True)
        self.assertContains(response, "账户已有待执行任务")
        self.assertEqual(Run.objects.count(), 1)

    def test_run_rejects_disabled_account(self):
        account = self.make_account(enabled=False)
        response = self.client.post(reverse("portal:account_run", args=[account.pk]), follow=True)
        self.assertContains(response, "账户已停用")
        self.assertEqual(Run.objects.count(), 0)

    def test_run_requires_post(self):
        account = self.make_account()
        self.assertEqual(self.client.get(reverse("portal:account_run", args=[account.pk])).status_code, 405)


class ScheduleTests(PortalTestBase):
    def test_schedule_can_be_updated(self):
        account = self.make_account()
        response = self.client.post(
            reverse("portal:account_schedule", args=[account.pk]),
            {"schedule_time": "07:30", "poll_hours": "0", "timezone": "Europe/Berlin"},
        )
        self.assertEqual(response.status_code, 302)
        account.refresh_from_db()
        self.assertEqual(account.schedule_time, time(7, 30))
        self.assertEqual(account.poll_hours, 0)
        self.assertEqual(account.timezone, "Europe/Berlin")
        self.assertIsNotNone(account.next_daily_at)
        self.assertIsNone(account.next_poll_at)

    def test_invalid_timezone_is_rejected(self):
        account = self.make_account()
        response = self.client.post(
            reverse("portal:account_schedule", args=[account.pk]),
            {"schedule_time": "07:30", "poll_hours": "4", "timezone": "Invalid/Zone"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "请输入有效的 IANA 时区")
        account.refresh_from_db()
        self.assertEqual(account.timezone, "Asia/Shanghai")


class DeleteTests(PortalTestBase):
    def test_delete_is_rejected_while_run_is_active(self):
        account = self.make_account()
        Run.objects.create(account=account, trigger="manual", status="queued")
        response = self.client.post(
            reverse("portal:account_delete", args=[account.pk]),
            {"confirm": "yes"},
            follow=True,
        )
        self.assertContains(response, "暂不能删除")
        self.assertTrue(Account.objects.filter(pk=account.pk).exists())

    def test_enabled_account_requires_confirmation(self):
        account = self.make_account()
        response = self.client.post(reverse("portal:account_delete", args=[account.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "请勾选确认")
        self.assertTrue(Account.objects.filter(pk=account.pk).exists())

        response = self.client.post(
            reverse("portal:account_delete", args=[account.pk]),
            {"confirm": "yes"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Account.objects.filter(pk=account.pk).exists())

    def test_disabled_account_purges_history_without_confirmation(self):
        account = self.make_account(enabled=False)
        Run.objects.create(account=account, trigger="manual", status="failed")
        self.assertEqual(Run.objects.count(), 1)
        response = self.client.post(reverse("portal:account_delete", args=[account.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Account.objects.filter(pk=account.pk).exists())
        self.assertEqual(Run.objects.count(), 0)


class SessionNormalizationTests(TestCase):
    def test_flat_session_is_canonicalized(self):
        result = normalize_session({"accessToken": TOKEN, "uid": UID})
        self.assertEqual(result["auth"]["endpoint"], "https://copilot.tencent.com")
        self.assertEqual(result["account"]["uid"], UID)

    def test_encrypted_envelope_is_rejected(self):
        with self.assertRaisesMessage(ValueError, "加密信封"):
            normalize_session({"auth": {"accessToken": {"ciphertext": "x"}}, "account": {"uid": UID}})

    def test_non_official_endpoint_is_rejected(self):
        with self.assertRaisesMessage(ValueError, "仅允许官方服务地址"):
            normalize_session(
                {
                    "auth": {"accessToken": TOKEN, "endpoint": "https://evil.example"},
                    "account": {"uid": UID},
                }
            )


class WorkerStatusTests(PortalTestBase):
    def test_released_lease_is_offline(self):
        WorkerState.objects.create(key="main", owner="", heartbeat=timezone.now())
        response = self.client.get(reverse("portal:dashboard"))
        self.assertFalse(response.context["worker_online"])

    def test_owned_fresh_lease_is_online(self):
        WorkerState.objects.create(key="main", owner="worker-1", heartbeat=timezone.now())
        response = self.client.get(reverse("portal:dashboard"))
        self.assertTrue(response.context["worker_online"])

    def test_stale_owned_lease_is_offline(self):
        WorkerState.objects.create(
            key="main",
            owner="worker-1",
            heartbeat=timezone.now() - timedelta(seconds=721),
        )
        response = self.client.get(reverse("portal:dashboard"))
        self.assertFalse(response.context["worker_online"])


class CheckTests(PortalTestBase):
    def test_check_can_be_queued_for_disabled_account(self):
        account = self.make_account(enabled=False)
        response = self.client.post(reverse("portal:account_check", args=[account.pk]))
        self.assertEqual(response.status_code, 302)
        run = Run.objects.get()
        self.assertEqual(run.trigger, "check")
        self.assertEqual(run.status, "queued")
        account.refresh_from_db()
        self.assertFalse(account.enabled)

    def test_duplicate_check_is_rejected(self):
        account = self.make_account()
        self.client.post(reverse("portal:account_check", args=[account.pk]))
        response = self.client.post(
            reverse("portal:account_check", args=[account.pk]), follow=True
        )
        self.assertContains(response, "账户已有待执行任务")
        self.assertEqual(Run.objects.count(), 1)

    def test_check_requires_post(self):
        account = self.make_account()
        self.assertEqual(
            self.client.get(reverse("portal:account_check", args=[account.pk])).status_code,
            405,
        )


class HealthTests(TestCase):
    def test_health_is_public(self):
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
