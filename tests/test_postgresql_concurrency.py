"""Real PostgreSQL lock tests; skipped on the lightweight SQLite local backend."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from django.db import close_old_connections
from django.test import TransactionTestCase, skipUnlessDBFeature
from portal.management.commands.runworker import claim, heartbeat
from portal.models import Account, Run, WorkerState
from portal.services import enqueue_account, save_account

@skipUnlessDBFeature('has_select_for_update')
class PostgreSQLConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.account = save_account(account=Account(), label='synthetic-concurrency', session={'accessToken': 'synthetic-not-valid', 'uid': 'synthetic-concurrency-user'}, actor=None)

    def parallel(self, operation):
        barrier = Barrier(2)
        def work(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return operation(index)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(work, range(2)))

    def test_concurrent_enqueue_has_one_winner(self):
        def enqueue(index):
            account = Account.objects.get(pk=self.account.pk)
            try:
                enqueue_account(account)
                return 'queued'
            except ValueError:
                return 'duplicate'
        self.assertCountEqual(self.parallel(enqueue), ['queued', 'duplicate'])
        self.assertEqual(Run.objects.filter(status='queued').count(), 1)

    def test_concurrent_claim_cannot_execute_twice(self):
        enqueue_account(self.account)
        results = self.parallel(lambda index: bool(claim(f'synthetic-worker-{index}')))
        self.assertCountEqual(results, [True, False])
        self.assertEqual(Run.objects.filter(status='running').count(), 1)

    def test_worker_lease_has_one_owner(self):
        WorkerState.objects.create(key='main')
        results = self.parallel(lambda index: heartbeat(f'synthetic-worker-{index}')[0])
        self.assertCountEqual(results, [True, False])
