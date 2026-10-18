import uuid
from datetime import time
from django.conf import settings
from django.db import models
from django.utils import timezone
from .crypto import decrypt, CredentialError

class Account(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    encrypted_payload = models.TextField()
    enabled = models.BooleanField(default=True)
    mode = models.CharField(max_length=10, choices=[('signin', '仅签到'), ('full', '签到 + 成长中心')], default='full')
    schedule_time = models.TimeField(default=time(0, 5))
    poll_hours = models.PositiveSmallIntegerField(default=4)
    timezone = models.CharField(max_length=64, default='Asia/Shanghai')
    next_daily_at = models.DateTimeField(null=True, blank=True, db_index=True)
    next_poll_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def label(self):
        try:
            return decrypt(self.encrypted_payload, self.id)['label']
        except (CredentialError, KeyError):
            return '凭证不可读'

    @property
    def masked_uid(self):
        try:
            uid = decrypt(self.encrypted_payload, self.id)['session']['account']['uid']
            return '••••' + uid[-3:] if len(uid) > 6 else '••••'
        except (CredentialError, KeyError):
            return '••••'

    def __str__(self):
        return str(self.id)  # Never accidentally log an account name.

class Run(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='runs')
    trigger = models.CharField(max_length=10, choices=[(x, x) for x in ('manual', 'daily', 'poll', 'check')])
    status = models.CharField(max_length=10, default='queued', choices=[(x, x) for x in ('queued', 'running', 'success', 'partial', 'failed', 'unknown', 'skipped')], db_index=True)
    result_code = models.CharField(max_length=40, blank=True)
    summary = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    worker_id = models.CharField(max_length=64, blank=True)
    class Meta:
        ordering = ['-created_at']
        constraints = [models.UniqueConstraint(fields=['account'], condition=models.Q(status__in=['queued', 'running']), name='one_active_run_per_account')]

class AuditEvent(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=64)
    account = models.UUIDField(null=True)
    created_at = models.DateTimeField(default=timezone.now)
    class Meta:
        ordering = ['-created_at']

class WorkerState(models.Model):
    key = models.CharField(max_length=32, primary_key=True, default='main')
    heartbeat = models.DateTimeField(default=timezone.now)
    paused = models.BooleanField(default=False)
    owner = models.CharField(max_length=64, blank=True)

class LoginAttempt(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    failures = models.PositiveIntegerField(default=0)
    window_start = models.DateTimeField(default=timezone.now)
    blocked_until = models.DateTimeField(null=True)
