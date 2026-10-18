import base64
from datetime import datetime, time, timedelta, timezone as dt_timezone
from unittest.mock import patch
import uuid
from django.contrib.auth import get_user_model
from django.test import TestCase, SimpleTestCase, override_settings
from django.utils import timezone
from portal.crypto import CredentialError, decrypt, encrypt
from portal.management.commands.runworker import claim, heartbeat, perform, recover_abandoned
from portal.models import Account, Run, WorkerState
from portal.runner import classify
from portal.services import enqueue_account, next_daily, normalize_session, save_account, schedule_due

# Synthetic fixtures only. These are not usable WorkBuddy credentials.
SESSION = {'accessToken': 'synthetic-not-a-real-token', 'uid': 'synthetic-user-123'}

class CryptoTests(SimpleTestCase):
    def test_random_authenticated_account_bound_encryption(self):
        ident = uuid.uuid4()
        payload = {'label': 'synthetic-label', 'session': SESSION}
        first, second = encrypt(payload, ident), encrypt(payload, ident)
        self.assertNotEqual(first, second)
        self.assertNotIn('synthetic', first)
        self.assertEqual(decrypt(first, ident), payload)
        with self.assertRaises(CredentialError):
            decrypt(first, uuid.uuid4())
        with self.assertRaises(CredentialError):
            decrypt(first[:-3] + 'AAA', ident)
    def test_rotation_old_key_retained(self):
        ident = uuid.uuid4()
        value = encrypt({'fixture': True}, ident)
        keys = {'test': base64.b64encode(bytes(range(32))).decode(), 'next': base64.b64encode(bytes(reversed(range(32)))).decode()}
        with override_settings(CREDENTIAL_KEYS=keys, ACTIVE_CREDENTIAL_KEY='next'):
            self.assertEqual(decrypt(value, ident), {'fixture': True})
            self.assertTrue(encrypt({'fixture': True}, ident).startswith('v1:next:'))

class SessionTests(SimpleTestCase):
    def test_normalize_and_allowlist(self):
        normalized = normalize_session(SESSION)
        self.assertEqual(normalized['auth']['endpoint'], 'https://copilot.tencent.com')
        normalized['auth']['refreshToken'] = 'discard'
        self.assertNotIn('refreshToken', normalize_session(normalized)['auth'])
    def test_reject_endpoint_injection_and_unusable_envelopes(self):
        for bad in ({'accessToken': {'encrypted': 'x'}, 'uid': 'a'}, {'accessToken': 'x\r\nHeader:evil', 'uid': 'a'}, {'accessToken': 'abc', 'uid': 'a', 'url': 'http://localhost'}, {'auth': {'accessToken': 'abc', 'endpoint': 'https://evil.invalid'}, 'account': {'uid': 'x'}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_session(bad)
    def test_next_daily_and_dst(self):
        account = Account(schedule_time=time(0, 5), timezone='Asia/Shanghai')
        now = datetime(2026, 1, 1, 16, 4, tzinfo=dt_timezone.utc)
        self.assertEqual(next_daily(account, now), now + timedelta(minutes=1))
        self.assertEqual(next_daily(account, now + timedelta(minutes=1)), now + timedelta(days=1, minutes=1))
        account.timezone, account.schedule_time = 'America/New_York', time(2, 30)
        now = datetime(2026, 3, 8, 6, 0, tzinfo=dt_timezone.utc)
        self.assertEqual(next_daily(account, now), datetime(2026, 3, 9, 6, 30, tzinfo=dt_timezone.utc))

class SchedulingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('synthetic-admin')
        self.account = save_account(account=Account(), label='synthetic-label', session=SESSION, actor=self.user)
    def test_plaintext_absent_from_database_columns(self):
        self.assertNotIn('synthetic', self.account.encrypted_payload)
        self.assertEqual(self.account.label, 'synthetic-label')
        self.assertEqual(self.account.masked_uid, '••••123')
    def test_queue_dedup_and_locked_edit(self):
        enqueue_account(self.account)
        with self.assertRaises(ValueError):
            enqueue_account(self.account)
        with self.assertRaises(ValueError):
            save_account(account=self.account, label='new', session=None, actor=self.user)
        self.assertEqual(Run.objects.count(), 1)
    def test_daily_poll_coalesce_without_downtime_replay(self):
        now = timezone.now()
        Account.objects.filter(pk=self.account.pk).update(next_daily_at=now - timedelta(days=10), next_poll_at=now - timedelta(days=10))
        schedule_due(now)
        schedule_due(now)
        self.assertEqual(Run.objects.count(), 1)
        self.account.refresh_from_db()
        self.assertGreater(self.account.next_daily_at, now)
        self.assertGreater(self.account.next_poll_at, now)
    def test_due_slot_is_not_lost_behind_auth_check(self):
        now = timezone.now()
        due = now - timedelta(minutes=1)
        Account.objects.filter(pk=self.account.pk).update(next_daily_at=due, next_poll_at=None, poll_hours=0)
        check = enqueue_account(self.account, trigger='check')
        schedule_due(now)
        self.account.refresh_from_db()
        self.assertEqual(self.account.next_daily_at, due)
        Run.objects.filter(pk=check.pk).update(status='success')
        schedule_due(now)
        self.assertEqual(Run.objects.filter(status='queued', trigger='daily').count(), 1)
        self.account.refresh_from_db()
        self.assertGreater(self.account.next_daily_at, now)
    def test_deleted_account_queue_and_edit_fail_cleanly(self):
        stale = Account.objects.get(pk=self.account.pk)
        self.account.delete()
        with self.assertRaisesMessage(ValueError, '账户已被删除'):
            enqueue_account(stale)
        with self.assertRaisesMessage(ValueError, '账户已被删除'):
            save_account(account=stale, label='synthetic-label', session=None, actor=self.user)
    def test_stale_edit_cannot_reenable_automatically_disabled_account(self):
        Account.objects.filter(pk=self.account.pk).update(enabled=False, updated_at=timezone.now())
        with self.assertRaisesMessage(ValueError, '账户状态已改变'):
            save_account(account=self.account, label='synthetic-label', session=None, actor=self.user)
        self.account.refresh_from_db()
        self.assertFalse(self.account.enabled)
    @patch('portal.management.commands.runworker.execute', return_value=('failed', 'AUTH_REJECTED', '认证被拒绝'))
    def test_stale_worker_result_cannot_pause_account(self, execute):
        enqueue_account(self.account)
        run = claim('worker')
        Run.objects.filter(pk=run.pk).update(status='unknown')
        perform(run)
        self.account.refresh_from_db()
        self.assertTrue(self.account.enabled)
        self.assertEqual(Run.objects.get().status, 'unknown')
    def test_disabled_skip_except_auth_check(self):
        run = enqueue_account(self.account)
        Account.objects.filter(pk=self.account.pk).update(enabled=False)
        self.assertIsNone(claim('owner'))
        run.refresh_from_db()
        self.assertEqual(run.status, 'skipped')
        enqueue_account(self.account, trigger='check')
        self.assertEqual(claim('owner').trigger, 'check')
    def test_single_worker_and_recovery(self):
        self.assertTrue(heartbeat('first')[0])
        self.assertFalse(heartbeat('second')[0])
        WorkerState.objects.update(heartbeat=timezone.now() - timedelta(seconds=721))
        self.assertTrue(heartbeat('second')[0])
        run = enqueue_account(self.account)
        claimed = claim('second')
        self.assertEqual(claimed.status, 'running')
        Run.objects.filter(pk=run.pk).update(started_at=timezone.now() - timedelta(seconds=721))
        recover_abandoned()
        run.refresh_from_db()
        self.assertEqual(run.status, 'unknown')
        self.assertEqual(run.result_code, 'WORKER_LOST')
    @patch('portal.management.commands.runworker.execute', return_value=('failed', 'AUTH_REJECTED', '认证被拒绝'))
    def test_expired_session_pauses_account(self, execute):
        enqueue_account(self.account)
        perform(claim('worker'))
        self.account.refresh_from_db()
        self.assertFalse(self.account.enabled)
        self.assertIsNone(self.account.next_poll_at)
    @patch('portal.management.commands.runworker.execute', side_effect=RuntimeError('synthetic-secret-do-not-persist'))
    def test_error_messages_cannot_persist_secrets(self, execute):
        enqueue_account(self.account)
        perform(claim('worker'))
        self.assertNotIn('synthetic-secret', Run.objects.get().summary)

class OutcomeTests(SimpleTestCase):
    def test_results_are_allowlisted_and_partial_is_visible(self):
        self.assertEqual(classify(0, {'result': 'CLAIMED'}, 'manual')[0], 'success')
        self.assertEqual(classify(0, {'result': 'ALREADY', 'needs_attention': True}, 'manual')[0], 'partial')
        self.assertEqual(classify(1, {'result': 'TIMEOUT'}, 'manual')[0], 'unknown')
        result = classify(0, {'result': 'SECRET_RAW_ERROR', 'message': 'synthetic-secret'}, 'manual')
        self.assertNotIn('synthetic', ' '.join(result))
        self.assertEqual(classify(0, {'http': 200}, 'check')[1], 'STATUS_OK')
        self.assertNotEqual(classify(0, {'http': True}, 'check')[0], 'success')
