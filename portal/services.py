import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from .crypto import decrypt, encrypt
from .models import Account, AuditEvent, Run

ENDPOINT = 'https://copilot.tencent.com'
TOKEN_PATTERN = re.compile(r'[A-Za-z0-9._~+/\-]+=*')

def normalize_session(value):
    """Allowlist only needed fields; no refresh tokens or arbitrary upstream endpoints."""
    if not isinstance(value, dict):
        raise ValueError('凭证必须是 JSON 对象')
    if 'auth' not in value and 'account' not in value:
        if set(value) - {'accessToken', 'uid', 'enterpriseId', 'domain', 'endpoint'}:
            raise ValueError('凭证字段不受支持')
        value = {'auth': {k: value[k] for k in ('accessToken', 'domain', 'endpoint') if k in value}, 'account': {k: value[k] for k in ('uid', 'enterpriseId') if k in value}}
    auth, account = value.get('auth'), value.get('account')
    if not isinstance(auth, dict) or not isinstance(account, dict):
        raise ValueError('缺少 auth 或 account 对象')
    token, uid = auth.get('accessToken'), account.get('uid')
    if isinstance(token, dict):
        raise ValueError('检测到客户端加密信封中的 accessToken（$wbEncrypted 等）。本入口仅支持 CodeBuddy 明文会话，不提供本机或服务器解密适配。请重新选择包含明文 accessToken 的 CodeBuddy 凭据文件，不要将 envelope 当作 Token。')
    if isinstance(token, str) and token.strip().lower() in {'xx', 'xxx', 'redacted'}:
        raise ValueError('accessToken 是脱敏占位值，不能恢复为真实凭据；请在安全连接下导入原始有效凭据。')
    if not isinstance(token, str) or not 0 < len(token) <= 32768 or not TOKEN_PATTERN.fullmatch(token):
        raise ValueError('accessToken 格式无效')
    if not isinstance(uid, str) or not re.fullmatch(r'[\x21-\x7e]{1,2048}', uid):
        raise ValueError('uid 格式无效')
    if auth.get('endpoint') not in (None, '', ENDPOINT, ENDPOINT + '/'):
        raise ValueError('仅允许官方服务地址')
    result = {'auth': {'accessToken': token, 'endpoint': ENDPOINT}, 'account': {'uid': uid}}
    for src, key, dest in ((auth, 'domain', 'auth'), (account, 'enterpriseId', 'account')):
        val = src.get(key)
        if val not in (None, ''):
            if not isinstance(val, str) or not re.fullmatch(r'[\x21-\x7e]{1,2048}', val):
                raise ValueError('可选凭证字段格式无效')
            result[dest][key] = val
    return result

def next_daily(account, now):
    zone = ZoneInfo(account.timezone)
    local = now.astimezone(zone)
    for offset in range(4):
        day = local.date() + timedelta(days=offset)
        candidate = datetime.combine(day, account.schedule_time, tzinfo=zone)
        # Skip a nonexistent DST wall time. Ambiguous times run once, fold=0.
        utc = candidate.astimezone(ZoneInfo('UTC'))
        if utc > now and utc.astimezone(zone).replace(tzinfo=None) == candidate.replace(tzinfo=None):
            return utc
    raise ValueError('无法计算下次执行时间')

def audit(actor, action, account=None):
    AuditEvent.objects.create(actor=actor if actor and actor.is_authenticated else None, action=action, account=account.pk if account else None)

@transaction.atomic
def save_account(*, account, label, session, actor):
    label = label.strip()
    if not label or len(label) > 80:
        raise ValueError('账户备注应为 1–80 个字符')
    try:
        ZoneInfo(account.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError('时区无效') from None
    if account.mode not in ('signin', 'full') or account.poll_hours not in (0, 1, 2, 4, 6, 8, 12, 24):
        raise ValueError('执行计划无效')
    creating = account._state.adding
    if not creating:
        # Serialize changes with queue claims/deletion; don't edit credentials while running.
        current = Account.objects.select_for_update().filter(pk=account.pk).first()
        if current is None:
            raise ValueError('账户已被删除，请刷新页面')
        if current.updated_at != account.updated_at:
            raise ValueError('账户状态已改变，请刷新页面后重新提交')
        if Run.objects.filter(account=account, status__in=['queued', 'running']).exists():
            raise ValueError('该账户尚有未完成任务，请稍后编辑')
    if session is None:
        if creating:
            raise ValueError('新账户必须提供凭证')
        session = decrypt(account.encrypted_payload, account.id)['session']
    session = normalize_session(session)
    account.encrypted_payload = encrypt({'label': label, 'session': session}, account.id)
    now = timezone.now()
    account.next_daily_at = next_daily(account, now) if account.enabled else None
    account.next_poll_at = now + timedelta(hours=account.poll_hours) if account.enabled and account.poll_hours else None
    account.save()
    audit(actor, 'account.create' if creating else 'account.update', account)
    return account

def enqueue_account(account, *, trigger='manual', actor=None):
    if trigger not in ('manual', 'daily', 'poll', 'check'):
        raise ValueError('任务类型无效')
    try:
        with transaction.atomic():
            locked = Account.objects.select_for_update().filter(pk=account.pk).first()
            if locked is None:
                raise ValueError('账户已被删除，请刷新页面')
            if not locked.enabled and trigger != 'check':
                raise ValueError('账户已停用')
            if Run.objects.filter(account=locked, status__in=['queued', 'running']).exists():
                raise ValueError('账户已有待执行任务')
            run = Run.objects.create(account=locked, trigger=trigger)
            if actor:
                audit(actor, 'task.' + trigger, locked)
            return run
    except IntegrityError:
        raise ValueError('账户已有待执行任务') from None

def schedule_due(now=None):
    now = now or timezone.now()
    # One account / one transaction, no historical replay after downtime.
    due = Account.objects.filter(enabled=True).filter(Q(next_daily_at__lte=now) | Q(next_poll_at__lte=now))
    for pk in list(due.values_list('pk', flat=True)[:100]):
        with transaction.atomic():
            account = Account.objects.select_for_update().filter(pk=pk).first()
            if account is None or not account.enabled:
                continue
            daily = account.next_daily_at and account.next_daily_at <= now
            poll = account.next_poll_at and account.next_poll_at <= now
            if not (daily or poll):
                continue
            if Run.objects.filter(account=account, status__in=['queued', 'running']).exists():
                # An auth-check/manual task does not consume a due daily slot.
                # Keep it due until the queue can accept the scheduled work.
                continue
            Run.objects.create(account=account, trigger='daily' if daily else 'poll')
            if daily:
                account.next_daily_at = next_daily(account, now)
            if poll:
                account.next_poll_at = now + timedelta(hours=account.poll_hours) if account.poll_hours else None
            account.save(update_fields=['next_daily_at', 'next_poll_at'])
