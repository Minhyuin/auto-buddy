"""Single active worker with a database lease and bounded subprocess execution."""
import signal
import time
import uuid
from datetime import timedelta
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, transaction
from django.utils import timezone
from portal.crypto import decrypt
from portal.models import Account, Run, WorkerState
from portal.runner import cleanup_stale_sessions, execute
from portal.services import schedule_due

LEASE_SECONDS = 720  # Greater than the hard 600s subprocess timeout.

def heartbeat(owner):
    with transaction.atomic():
        state, _ = WorkerState.objects.select_for_update().get_or_create(key='main')
        now = timezone.now()
        if state.owner and state.owner != owner and state.heartbeat > now - timedelta(seconds=LEASE_SECONDS):
            return False, state.paused
        state.owner, state.heartbeat = owner, now
        state.save(update_fields=['owner', 'heartbeat'])
        return True, state.paused

def recover_abandoned():
    Run.objects.filter(status='running', started_at__lt=timezone.now() - timedelta(seconds=LEASE_SECONDS)).update(status='unknown', result_code='WORKER_LOST', summary='执行器中断，操作结果不明；未自动重试', finished_at=timezone.now())

def claim(owner):
    with transaction.atomic():
        candidate = Run.objects.filter(status='queued').order_by('created_at').values('pk', 'account_id').first()
        if not candidate:
            return None
        # Consistent account -> run lock order with edits/deletes; never invert it.
        account = Account.objects.select_for_update().filter(pk=candidate['account_id']).first()
        if account is None:
            return None
        run = Run.objects.select_for_update().filter(pk=candidate['pk'], status='queued').first()
        if run is None:
            return None
        run.started_at = timezone.now()
        if not account.enabled and run.trigger != 'check':
            run.status, run.result_code, run.summary = 'skipped', 'DISABLED', '账户已停用'
            run.finished_at = timezone.now()
            run.save()
            return None
        run.status, run.worker_id = 'running', owner
        run.save(update_fields=['status', 'worker_id', 'started_at'])
        return run

def perform(run):
    try:
        session = decrypt(run.account.encrypted_payload, run.account_id)['session']
        status, code, summary = execute(session, mode=run.account.mode, trigger=run.trigger)
    except Exception:
        # Exceptions can embed raw payloads. Do NOT log exception or traceback.
        status, code, summary = 'failed', 'EXECUTION_ERROR', '执行器或凭证配置错误，请检查系统配置'
    with transaction.atomic():
        # Keep the account -> run lock order even when disabling bad credentials.
        account = Account.objects.select_for_update().filter(pk=run.account_id).first()
        if account is None:
            return
        updated = Run.objects.filter(pk=run.pk, status='running', worker_id=run.worker_id).update(status=status, result_code=code, summary=summary, finished_at=timezone.now())
        if updated == 1 and code == 'AUTH_REJECTED':
            Account.objects.filter(pk=run.account_id).update(enabled=False, next_daily_at=None, next_poll_at=None, updated_at=timezone.now())

class Command(BaseCommand):
    help = 'Run the no-AI scheduler/worker (one active database lease).'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='Process at most one task and exit')
        parser.add_argument('--interval', type=float, default=5)

    def handle(self, *args, **options):
        if options['interval'] < 0.1:
            raise CommandError('Interval must be >= 0.1')
        owner = uuid.uuid4().hex
        stopping = False
        def stop(signum, frame):
            nonlocal stopping
            stopping = True
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, stop)
        try:
            while not stopping:
                close_old_connections()
                acquired, paused = heartbeat(owner)
                if not acquired:
                    if options['once']:
                        return
                    time.sleep(options['interval'])
                    continue
                recover_abandoned()
                cleanup_stale_sessions(older_than=LEASE_SECONDS)
                if not paused:
                    schedule_due()
                    run = claim(owner)
                    if run:
                        perform(run)
                if options['once']:
                    return
                time.sleep(options['interval'])
        finally:
            WorkerState.objects.filter(key='main', owner=owner).update(owner='', heartbeat=timezone.now())
