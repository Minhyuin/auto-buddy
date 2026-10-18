"""Forms for the administrator portal.

Credentials are handled in memory only.  They are never put into form
initial data, messages, the session, logs or an error response.  The textarea
widget deliberately renders an empty value even after a failed POST so a
secret cannot be echoed back to the browser.
"""

from __future__ import annotations

import json
import re
from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django import forms
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError

from .models import Account
from .services import normalize_session

MAX_CREDENTIAL_BYTES = 64 * 1024
LABEL_MAX_LENGTH = 80
UNREADABLE_LABEL = "凭证不可读"
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# Must stay in sync with portal.services.save_account's allow-list.  Keeping
# this as a select prevents invalid values from being submitted at all.
POLL_CHOICES = [
    (0, "关闭轮询"),
    (1, "每 1 小时"),
    (2, "每 2 小时"),
    (4, "每 4 小时"),
    (6, "每 6 小时"),
    (8, "每 8 小时"),
    (12, "每 12 小时"),
    (24, "每 24 小时"),
]

# Suggestions only; the server validates any IANA name with zoneinfo.
TIMEZONE_OPTIONS = [
    "Asia/Shanghai",
    "Asia/Hong_Kong",
    "Asia/Taipei",
    "Asia/Tokyo",
    "Asia/Singapore",
    "Asia/Seoul",
    "UTC",
    "Europe/London",
    "Europe/Berlin",
    "America/New_York",
    "America/Chicago",
    "America/Los_Angeles",
    "Australia/Sydney",
]


class CredentialInputError(Exception):
    """A credential error that is safe to show to the administrator."""


class SecretTextarea(forms.Textarea):
    """A textarea that never re-renders its submitted/initial value."""

    def format_value(self, value):  # noqa: ARG002 - value is intentionally discarded
        return ""


def _decode_payload(raw):
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise CredentialInputError("凭据文件必须是 UTF-8 编码的 JSON。") from None
    if not isinstance(raw, str) or not raw.strip():
        raise CredentialInputError("凭据内容为空。")
    # Count bytes, not characters: the upload limit is a byte limit.
    if len(raw.encode("utf-8")) > MAX_CREDENTIAL_BYTES:
        raise CredentialInputError("凭据内容超过 64 KB 上限。")
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise CredentialInputError("凭据 JSON 格式不正确。") from None
    if not isinstance(value, dict):
        raise CredentialInputError("凭据必须是 JSON 对象。")
    return value


def _normalized_session(raw):
    """Parse JSON and let the parent-owned service validate/normalize it."""
    value = _decode_payload(raw)
    try:
        return normalize_session(value)
    except ValueError as exc:
        # normalize_session's messages are curated and never include secret
        # values.  Do not interpolate the submitted document itself.
        raise CredentialInputError(str(exc) or "凭据格式无效。") from None


class ScheduleForm(forms.Form):
    """Schedule/plan fields, shared by the full form and the schedule editor."""

    schedule_time = forms.TimeField(
        label="每日启动时间",
        initial=time(0, 5),
        input_formats=["%H:%M", "%H:%M:%S"],
        widget=forms.TimeInput(
            format="%H:%M",
            attrs={"class": "input", "type": "time", "step": "60"},
        ),
        help_text="按账户所在时区执行。",
    )
    poll_hours = forms.TypedChoiceField(
        label="轮询间隔",
        choices=POLL_CHOICES,
        coerce=int,
        initial=4,
        widget=forms.Select(attrs={"class": "input"}),
        help_text="控制成长中心任务的轮询频率；关闭后只保留每日签到。",
    )
    timezone = forms.CharField(
        label="时区",
        max_length=64,
        initial="Asia/Shanghai",
        widget=forms.TextInput(
            attrs={
                "class": "input",
                "list": "timezone-options",
                "autocomplete": "off",
                "spellcheck": "false",
            }
        ),
        help_text="使用 IANA 时区名称，例如 Asia/Shanghai。",
    )

    field_order = ["schedule_time", "poll_hours", "timezone"]

    def __init__(self, *args, account=None, **kwargs):
        self.account = account
        initial = dict(kwargs.pop("initial", {}) or {})
        super().__init__(*args, initial=initial, **kwargs)
        if not self.is_bound:
            self.initial.setdefault("schedule_time", time(0, 5))
            self.initial.setdefault("poll_hours", 4)
            self.initial.setdefault("timezone", getattr(settings, "TIME_ZONE", "Asia/Shanghai"))
            if account is not None:
                self.initial["schedule_time"] = account.schedule_time
                self.initial["poll_hours"] = account.poll_hours
                self.initial["timezone"] = account.timezone

    def clean_timezone(self):
        value = (self.cleaned_data.get("timezone") or "").strip()
        if not value:
            raise ValidationError("请输入 IANA 时区，例如 Asia/Shanghai。")
        if _CONTROL_CHARS.search(value):
            raise ValidationError("时区名称包含无效字符。")
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise ValidationError("请输入有效的 IANA 时区，例如 Asia/Shanghai。") from None
        return value


class AccountForm(ScheduleForm):
    """Create/update form for an account.

    The encrypted payload is owned by ``portal.services.save_account``.  This
    form only collects the display label and a canonicalized session.
    """

    label = forms.CharField(
        label="账户备注",
        max_length=LABEL_MAX_LENGTH,
        widget=forms.TextInput(
            attrs={
                "class": "input",
                "autocomplete": "off",
                "placeholder": "例如：工作号 / 备用号",
            }
        ),
        help_text="仅用于门户中辨识账户，会随凭据一起加密保存。",
    )
    mode = forms.ChoiceField(
        label="执行模式",
        choices=(),
        initial="full",
        widget=forms.Select(attrs={"class": "input"}),
    )
    enabled = forms.BooleanField(
        label="启用自动执行",
        required=False,
        initial=True,
        help_text="停用后不再生成新的计划任务，已有任务会继续完成。",
    )
    credentials = forms.CharField(
        label="凭据 JSON",
        required=False,
        widget=SecretTextarea(
            attrs={
                "class": "input textarea mono",
                "rows": "7",
                "autocomplete": "off",
                "autocapitalize": "off",
                "spellcheck": "false",
                "placeholder": '{"accessToken": "…", "uid": "…"}',
            }
        ),
        help_text="支持扁平会话 {\"accessToken\": \"…\", \"uid\": \"…\"} 或 WorkBuddy 原生 auth/account JSON。",
    )
    credentials_file = forms.FileField(
        label="或上传凭据文件",
        required=False,
        widget=forms.ClearableFileInput(
            attrs={"class": "file-input", "accept": ".json,application/json"}
        ),
        help_text="UTF-8 JSON，最大 64 KB。文件内容不会被回显。",
    )

    field_order = [
        "label",
        "mode",
        "enabled",
        "credentials",
        "credentials_file",
        "schedule_time",
        "poll_hours",
        "timezone",
    ]

    def __init__(self, *args, account=None, **kwargs):
        initial = dict(kwargs.pop("initial", {}) or {})
        unbound = not kwargs.get("data") and not kwargs.get("files")
        if account is not None and unbound:
            initial.setdefault("mode", account.mode)
            initial.setdefault("enabled", account.enabled)
            # label may be the model's placeholder if decryption failed; do
            # not silently overwrite it with that placeholder.
            label = getattr(account, "label", "")
            if label and label != UNREADABLE_LABEL:
                initial.setdefault("label", label)
        elif unbound:
            initial.setdefault("mode", "full")
            initial.setdefault("enabled", True)
        super().__init__(*args, account=account, initial=initial, **kwargs)
        field = self.fields["mode"]
        field.choices = tuple(Account._meta.get_field("mode").choices)
        self._file_bytes = None

    def clean_label(self):
        value = (self.cleaned_data.get("label") or "").strip()
        if not value:
            raise ValidationError("请输入账户备注。")
        if len(value) > LABEL_MAX_LENGTH:
            raise ValidationError("账户备注不能超过 80 个字符。")
        if _CONTROL_CHARS.search(value):
            raise ValidationError("账户备注包含无效字符。")
        return value

    def clean_credentials(self):
        value = self.cleaned_data.get("credentials") or ""
        if len(value.encode("utf-8")) > MAX_CREDENTIAL_BYTES:
            raise ValidationError("凭据内容超过 64 KB 上限。")
        return value

    def clean_credentials_file(self):
        upload = self.cleaned_data.get("credentials_file")
        if not upload:
            return None
        if upload.size > MAX_CREDENTIAL_BYTES:
            raise ValidationError("凭据文件超过 64 KB 上限。")
        try:
            raw = upload.read(MAX_CREDENTIAL_BYTES + 1)
        except OSError:
            raise ValidationError("无法读取凭据文件。") from None
        if len(raw) > MAX_CREDENTIAL_BYTES:
            raise ValidationError("凭据文件超过 64 KB 上限。")
        self._file_bytes = raw
        return upload

    def clean(self):
        cleaned = super().clean()
        text = cleaned.get("credentials") or ""
        upload = cleaned.get("credentials_file")
        if text and upload:
            self.add_error("credentials", "请只提供一种凭据输入方式：粘贴 JSON 或上传文件。")
            cleaned["session"] = None
            return cleaned
        if not text and not upload:
            cleaned["session"] = None
            if self.account is None:
                self.add_error("credentials", "新建账户时必须提供凭据。")
            return cleaned
        try:
            raw = self._file_bytes if upload else text
            cleaned["session"] = _normalized_session(raw)
        except CredentialInputError as exc:
            self.add_error("credentials_file" if upload else "credentials", str(exc))
            cleaned["session"] = None
        # The secret has been converted to a session or rejected.  Drop the
        # raw value so it cannot accidentally reach a template, log or context.
        cleaned["credentials"] = ""
        cleaned["credentials_file"] = None
        self._file_bytes = None
        return cleaned


class PortalAuthenticationForm(AuthenticationForm):
    username = forms.CharField(
        label="管理员账号",
        max_length=150,
        widget=forms.TextInput(
            attrs={
                "class": "input",
                "autocomplete": "username",
                "autofocus": True,
                "placeholder": "请输入管理员账号",
            }
        ),
    )
    password = forms.CharField(
        label="密码",
        strip=False,
        widget=forms.PasswordInput(
            attrs={
                "class": "input",
                "autocomplete": "current-password",
                "placeholder": "请输入密码",
            }
        ),
    )

    error_messages = {
        **AuthenticationForm.error_messages,
        "invalid_login": "账号或密码不正确。",
        "inactive": "该账号不可用，请联系系统管理员。",
    }
