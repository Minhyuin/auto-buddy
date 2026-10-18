"""Django tests for importing native CodeBuddy ``.info`` files.

Every fixture in this module is synthetic and built in-process.  The tests
never read an operator's Downloads folder, client data directory or a real
credential file, and they never contact a network service: all interaction is
through Django's test client and the test database only.
"""

from __future__ import annotations

import json
import re

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from portal.crypto import decrypt
from portal.forms import AccountForm
from portal.models import Account, AuditEvent
from portal.services import ENDPOINT

User = get_user_model()

TOKEN = "synthetic-info-access-token-abc123"
UID = "synthetic-user-0001"
DOMAIN = "synthetic-codebuddy.example"
REFRESH_TOKEN = "synthetic-refresh-token-must-not-persist"
NICKNAME = "synthetic-nickname-must-not-persist"
PHONE = "00000000000"
ENCRYPTED_REFRESH = "c3ludGhldGljLXJlZnJlc2gtZW52ZWxvcGU="
ENCRYPTED_NICKNAME = "c3ludGhldGljLW5pY2tuYW1lLWVudmVsb3Bl"
ENCRYPTED_PHONE = "c3ludGhldGljLXBob25lLWVudmVsb3Bl"
ENVELOPE_SENTINEL = "c2VudGluZWwtZW52ZWxvcGUtZG8tbm90LWVjaG8="


def encrypted(value):
    """A synthetic ``$wbEncrypted`` wrapper; nothing here is decryptable."""
    return {"$wbEncrypted": 1, "envelope": value}


def codebuddy_native():
    """Synthetic CodeBuddy ``auth`` / ``account`` document with extra metadata."""
    return {
        "auth": {
            "accessToken": TOKEN,
            "domain": DOMAIN,
            "refreshToken": REFRESH_TOKEN,
        },
        "account": {
            "uid": UID,
            "nickname": NICKNAME,
            "phone": PHONE,
        },
        "accounts": [
            {"uid": "synthetic-other-account", "accessToken": "synthetic-other-token"}
        ],
        "allAccounts": [{"uid": "synthetic-other-account"}],
    }


def codebuddy_native_with_encrypted_metadata():
    """Synthetic CodeBuddy document with encrypted irrelevant metadata."""
    return {
        "auth": {
            "accessToken": TOKEN,
            "domain": DOMAIN,
            "refreshToken": encrypted(ENCRYPTED_REFRESH),
        },
        "account": {
            "uid": UID,
            "nickname": encrypted(ENCRYPTED_NICKNAME),
            "phone": encrypted(ENCRYPTED_PHONE),
        },
        "accounts": [{"uid": UID}],
        "allAccounts": [{"uid": UID}],
    }


def canonical_session(*, domain=DOMAIN):
    """The only session shape the portal may persist for the fixtures above."""
    auth = {"accessToken": TOKEN, "endpoint": ENDPOINT}
    if domain is not None:
        auth["domain"] = domain
    return {"auth": auth, "account": {"uid": UID}}


class InfoImportTestBase(TestCase):
    """Authenticated portal client plus helpers for synthetic credential posts."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="synthetic-admin",
            password="synthetic-test-password-123",
            is_staff=True,
            is_superuser=True,
        )
        self.client.force_login(self.user)

    def create_payload(self, **overrides):
        payload = {
            "label": "合成导入账户",
            "mode": "full",
            "enabled": "on",
            "credentials": "",
            "schedule_time": "00:05",
            "poll_hours": "4",
            "timezone": "Asia/Shanghai",
        }
        payload.update(overrides)
        return payload

    def upload(self, content, *, filename="Tencent-Cloud.coding-copilot.info"):
        if isinstance(content, str):
            content = content.encode("utf-8")
        return SimpleUploadedFile(
            filename, content, content_type="application/octet-stream"
        )

    def post_upload(self, content, *, filename="Tencent-Cloud.coding-copilot.info", **overrides):
        payload = self.create_payload(**overrides)
        payload["credentials"] = ""
        payload["credentials_file"] = self.upload(content, filename=filename)
        return self.client.post(reverse("portal:account_create"), payload)

    def post_credentials(self, credentials, **overrides):
        return self.client.post(
            reverse("portal:account_create"),
            self.create_payload(credentials=credentials, **overrides),
        )

    def persisted_session(self):
        account = Account.objects.get()
        return decrypt(account.encrypted_payload, account.id)["session"]

    @staticmethod
    def field_errors(response, field):
        form = response.context["form"]
        return " ".join(str(error) for error in form.errors.get(field, []))


class NativeInfoImportTests(InfoImportTestBase):
    def test_file_field_accepts_info_uploads(self):
        accept = AccountForm().fields["credentials_file"].widget.attrs["accept"]
        extensions = {part.strip() for part in accept.split(",")}
        self.assertIn(".info", extensions)
        self.assertIn(".json", extensions)

        response = self.client.get(reverse("portal:account_create"))
        self.assertEqual(response.status_code, 200)
        tag = re.search(
            r'<input[^>]*name="credentials_file"[^>]*>',
            response.content.decode(),
        )
        self.assertIsNotNone(tag)
        self.assertIn(".info", tag.group(0))

    def test_create_page_prompts_codebuddy_only(self):
        response = self.client.get(reverse("portal:account_create"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "CodeBuddy")
        self.assertContains(response, "Tencent-Cloud.coding-copilot.info")
        self.assertNotContains(response, "workbuddy-desktop.info")

    def test_codebuddy_info_upload_persists_minimal_canonical_session(self):
        response = self.post_upload(
            json.dumps(codebuddy_native()),
            filename="Tencent-Cloud.coding-copilot.info",
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.persisted_session(), canonical_session())

        account = Account.objects.get()
        self.assertEqual(account.label, "合成导入账户")
        self.assertNotIn(TOKEN, account.encrypted_payload)
        self.assertNotIn(REFRESH_TOKEN, account.encrypted_payload)
        self.assertNotIn(UID, account.encrypted_payload)

        stored = json.dumps(self.persisted_session())
        for discarded in (
            "refreshToken",
            "nickname",
            "phone",
            "accounts",
            "allAccounts",
            "synthetic-other-account",
            "synthetic-other-token",
        ):
            self.assertNotIn(discarded, stored)

    def test_encrypted_irrelevant_metadata_is_discarded_without_decrypting(self):
        response = self.post_upload(json.dumps(codebuddy_native_with_encrypted_metadata()))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.persisted_session(), canonical_session())

        account = Account.objects.get()
        self.assertNotIn(TOKEN, account.encrypted_payload)
        for sentinel in (ENCRYPTED_REFRESH, ENCRYPTED_NICKNAME, ENCRYPTED_PHONE):
            self.assertNotIn(sentinel, account.encrypted_payload)
            self.assertNotIn(sentinel, json.dumps(self.persisted_session()))

    def test_utf8_bom_info_upload_is_accepted(self):
        raw = b"\xef\xbb\xbf" + json.dumps(
            codebuddy_native(), ensure_ascii=False
        ).encode("utf-8")
        response = self.post_upload(
            raw, filename="Tencent-Cloud.coding-copilot.info"
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.persisted_session(), canonical_session())

    def test_pasted_leading_bom_is_accepted(self):
        response = self.post_credentials(
            "\ufeff" + json.dumps(codebuddy_native(), ensure_ascii=False)
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.persisted_session(), canonical_session())

    def test_encrypted_access_token_error_is_actionable_and_not_echoed(self):
        native = codebuddy_native()
        native["auth"]["accessToken"] = encrypted(ENVELOPE_SENTINEL)
        document = json.dumps(native)
        for filename in (
            "Tencent-Cloud.coding-copilot.info",
            "renamed-copy.info",
            "credentials.json",
        ):
            with self.subTest(filename=filename):
                response = self.post_upload(document, filename=filename)
                self.assertEqual(response.status_code, 200)
                errors = self.field_errors(response, "credentials_file")
                self.assertIn("本机", errors)
                self.assertIn("$wbEncrypted", errors)
                html = response.content.decode()
                self.assertNotIn(ENVELOPE_SENTINEL, html)
                self.assertNotIn(TOKEN, html)
                self.assertEqual(Account.objects.count(), 0)
                self.assertEqual(AuditEvent.objects.count(), 0)

    def test_case_insensitive_xx_token_is_rejected_as_placeholder(self):
        for token in ("xx", "XX", "xX"):
            with self.subTest(token=token):
                native = codebuddy_native()
                native["auth"]["accessToken"] = token
                response = self.post_upload(json.dumps(native))
                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    "占位", self.field_errors(response, "credentials_file")
                )
                self.assertEqual(Account.objects.count(), 0)

    def test_longer_token_containing_xx_is_not_treated_as_placeholder(self):
        token = "synthetic-xx-token"
        response = self.post_credentials(
            json.dumps({"accessToken": token, "uid": UID})
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.persisted_session()["auth"]["accessToken"], token)

    def test_simplified_flat_session_accepts_official_endpoint(self):
        response = self.post_credentials(
            json.dumps({"accessToken": TOKEN, "uid": UID, "endpoint": ENDPOINT})
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.persisted_session(), canonical_session(domain=None))

    def test_simplified_flat_session_blocks_malicious_endpoint(self):
        for endpoint in (
            "https://evil.example/collect",
            "http://copilot.tencent.com",
            "https://copilot.tencent.com.evil.example",
        ):
            with self.subTest(endpoint=endpoint):
                response = self.post_credentials(
                    json.dumps(
                        {"accessToken": TOKEN, "uid": UID, "endpoint": endpoint}
                    )
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    "仅允许官方服务地址",
                    self.field_errors(response, "credentials"),
                )
                self.assertNotIn(endpoint, response.content.decode())
                self.assertEqual(Account.objects.count(), 0)
                self.assertEqual(AuditEvent.objects.count(), 0)

    def test_excessively_nested_json_returns_form_error(self):
        response = self.post_upload('{"unused":' + '[' * 2000 + '0' + ']' * 2000 + '}')
        self.assertEqual(response.status_code, 200)
        self.assertIn('嵌套过深', self.field_errors(response, 'credentials_file'))
        self.assertEqual(Account.objects.count(), 0)

    def test_http_upload_page_warns_about_plaintext_transport(self):
        response = self.client.get(reverse('portal:account_create'))
        self.assertContains(response, '当前连接不是 HTTPS')
        response = self.client.get(reverse('portal:account_create'), secure=True)
        self.assertNotContains(response, '当前连接不是 HTTPS')

    def test_failed_native_upload_never_echoes_token(self):
        response = self.post_upload(
            json.dumps(codebuddy_native()),
            filename="Tencent-Cloud.coding-copilot.info",
            timezone="Mars/Base",
        )
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertNotIn(TOKEN, html)
        self.assertNotIn(REFRESH_TOKEN, html)
        self.assertEqual(Account.objects.count(), 0)

    def test_dual_input_cleanup_never_echoes_or_retains_secrets(self):
        text_token = "synthetic-dual-text-token-123"
        file_token = "synthetic-dual-file-token-456"
        text_document = json.dumps({"accessToken": text_token, "uid": UID})
        native = codebuddy_native()
        native["auth"]["accessToken"] = file_token
        file_document = json.dumps(native)

        payload = self.create_payload(credentials=text_document)
        payload["credentials_file"] = self.upload(file_document)
        response = self.client.post(reverse("portal:account_create"), payload)

        self.assertEqual(response.status_code, 200)
        self.assertIn("请只提供一种凭据输入方式", self.field_errors(response, "credentials"))
        form = response.context["form"]
        self.assertIsNone(form.cleaned_data.get("credentials"))
        self.assertIsNone(form.cleaned_data.get("credentials_file"))
        self.assertIsNone(form._file_bytes)

        html = response.content.decode()
        self.assertNotIn(text_token, html)
        self.assertNotIn(file_token, html)
        self.assertNotIn(text_token, str(form.cleaned_data))
        self.assertNotIn(file_token, str(form.cleaned_data))
        self.assertEqual(Account.objects.count(), 0)
        self.assertEqual(AuditEvent.objects.count(), 0)
