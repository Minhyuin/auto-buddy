"""Views for the administrator portal.

The portal is deliberately small: one staff user manages accounts, schedules
and run history.  Credentials are only accepted on the create/update form and
are immediately normalized and encrypted by portal.services; no view puts a
credential into a template, message or log record.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .crypto import CredentialError, decrypt
from .forms import AccountForm, PortalAuthenticationForm, ScheduleForm
from .models import Account, LoginAttempt, Run, WorkerState
from .services import audit, enqueue_account, next_daily, save_account

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW = timedelta(minutes=15)
LOGIN_BLOCK = timedelta(minutes=15)
WORKER_LEASE_SECONDS = 720
ACTIVE_RUN_STATUSES = ("queued", "running")


def _login_key(request):
    """Key login throttling on REMOTE_ADDR only; X-Forwarded-For is untrusted."""
    remote = (request.META.get("REMOTE_ADDR") or "unknown").encode("utf-8", "replace")
    return hashlib.sha256(b"auto-buddy:login:v1:" + remote).hexdigest()


def _login_is_blocked(key, now=None):
    now = now or timezone.now()
    try:
        attempt = LoginAttempt.objects.get(pk=key)
    except LoginAttempt.DoesNotExist:
        return False
    if attempt.blocked_until and attempt.blocked_until > now:
        return True
    if attempt.window_start <= now - LOGIN_WINDOW:
        # The window has elapsed; clear stale state before the next attempt.
        LoginAttempt.objects.filter(pk=key).delete()
    return False


@transaction.atomic
def _record_login_failure(key, now=None):
    now = now or timezone.now()
    attempt, _ = LoginAttempt.objects.select_for_update().get_or_create(pk=key)
    if attempt.blocked_until and attempt.blocked_until > now:
        return
    if attempt.window_start <= now - LOGIN_WINDOW:
        attempt.failures = 0
        attempt.window_start = now
        attempt.blocked_until = None
    attempt.failures += 1
    if attempt.failures >= LOGIN_MAX_FAILURES:
        attempt.blocked_until = now + LOGIN_BLOCK
    attempt.save(update_fields=["failures", "window_start", "blocked_until"])


def _clear_login_failures(key):
    LoginAttempt.objects.filter(pk=key).delete()


def staff_required(view):
    """Require an authenticated, active staff account."""

    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_active or not request.user.is_staff:
            raise PermissionDenied("仅管理员可访问该页面。")
        return view(request, *args, **kwargs)

    return wrapped


def _base_context(**extra):
    context = {"app_version": getattr(settings, "APP_VERSION", "")}
    context.update(extra)
    return context


def _worker_context():
    worker = WorkerState.objects.filter(pk="main").first()
    now = timezone.now()
    online = bool(
        worker
        and worker.owner
        and worker.heartbeat
        and worker.heartbeat >= now - timedelta(seconds=WORKER_LEASE_SECONDS)
    )
    return {"worker": worker, "worker_online": online}


@never_cache
@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated:
        return redirect(settings.LOGIN_REDIRECT_URL)

    next_url = request.POST.get("next") or request.GET.get("next") or ""
    key = _login_key(request)
    form = PortalAuthenticationForm(
        request, data=request.POST if request.method == "POST" else None
    )

    if request.method == "POST":
        if _login_is_blocked(key):
            form.add_error(None, "尝试次数过多，请 15 分钟后再试。")
        elif form.is_valid():
            _clear_login_failures(key)
            auth_login(request, form.get_user())
            if next_url and url_has_allowed_host_and_scheme(
                next_url,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure(),
            ):
                return redirect(next_url)
            return redirect(settings.LOGIN_REDIRECT_URL)
        else:
            _record_login_failure(key)
            # AuthenticationForm already reports a generic invalid-login error;
            # never distinguish unknown accounts from wrong passwords.

    return render(
        request,
        "portal/login.html",
        _base_context(form=form, next=next_url),
    )


@require_POST
def logout_view(request):
    auth_logout(request)
    return redirect(settings.LOGOUT_REDIRECT_URL)


@staff_required
@require_GET
def dashboard(request):
    accounts = list(Account.objects.order_by("created_at"))
    active_ids = set(
        Run.objects.filter(status__in=ACTIVE_RUN_STATUSES).values_list(
            "account_id", flat=True
        )
    )
    counts = Account.objects.aggregate(
        total=Count("id"), enabled=Count("id", filter=Q(enabled=True))
    )
    recent_runs = (
        Run.objects.select_related("account").order_by("-created_at")[:25]
    )
    active_runs = Run.objects.filter(status__in=ACTIVE_RUN_STATUSES).count()
    return render(
        request,
        "portal/dashboard.html",
        _base_context(
            accounts=accounts,
            active_account_ids=active_ids,
            total_accounts=counts["total"] or 0,
            enabled_accounts=counts["enabled"] or 0,
            active_runs=active_runs,
            recent_runs=recent_runs,
            **_worker_context(),
        ),
    )


@staff_required
@require_http_methods(["GET", "POST"])
def account_create(request):
    form = AccountForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
    )
    if request.method == "POST" and form.is_valid():
        account = Account()
        cleaned = form.cleaned_data
        account.mode = cleaned["mode"]
        account.enabled = cleaned["enabled"]
        account.schedule_time = cleaned["schedule_time"]
        account.poll_hours = cleaned["poll_hours"]
        account.timezone = cleaned["timezone"]
        try:
            save_account(
                account=account,
                label=cleaned["label"],
                session=cleaned.get("session"),
                actor=request.user,
            )
        except ValueError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "账户已创建。")
            return redirect("portal:dashboard")
    return render(
        request,
        "portal/account_form.html",
        _base_context(form=form, is_create=True, account=None),
    )


@staff_required
@require_http_methods(["GET", "POST"])
def account_edit(request, pk):
    account = get_object_or_404(Account, pk=pk)
    form = AccountForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
        account=account,
    )
    if request.method == "POST" and form.is_valid():
        cleaned = form.cleaned_data
        account.mode = cleaned["mode"]
        account.enabled = cleaned["enabled"]
        account.schedule_time = cleaned["schedule_time"]
        account.poll_hours = cleaned["poll_hours"]
        account.timezone = cleaned["timezone"]
        try:
            save_account(
                account=account,
                label=cleaned["label"],
                session=cleaned.get("session"),
                actor=request.user,
            )
        except ValueError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "账户已更新。")
            return redirect("portal:account_detail", pk=account.pk)
    return render(
        request,
        "portal/account_form.html",
        _base_context(form=form, is_create=False, account=account),
    )


@staff_required
@require_GET
def account_detail(request, pk):
    account = get_object_or_404(Account, pk=pk)
    try:
        decrypt(account.encrypted_payload, account.id)
        credentials_readable = True
    except CredentialError:
        credentials_readable = False
    runs = account.runs.order_by("-created_at")[:50]
    active_run = account.runs.filter(status__in=ACTIVE_RUN_STATUSES).exists()
    return render(
        request,
        "portal/account_detail.html",
        _base_context(
            account=account,
            runs=runs,
            active_run=active_run,
            credentials_readable=credentials_readable,
            schedule_form=ScheduleForm(account=account),
        ),
    )


@staff_required
@require_POST
def account_run(request, pk):
    account = get_object_or_404(Account, pk=pk)
    try:
        enqueue_account(account, trigger="manual", actor=request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "任务已加入执行队列。")
    return redirect("portal:account_detail", pk=account.pk)


@staff_required
@require_POST
def account_check(request, pk):
    """Queue a read-only authentication/online-status check.

    A check is allowed for disabled accounts so an expired session can be
    diagnosed without re-enabling scheduled execution.
    """

    account = get_object_or_404(Account, pk=pk)
    try:
        enqueue_account(account, trigger="check", actor=request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "在线状态检查已加入执行队列。")
    return redirect("portal:account_detail", pk=account.pk)


@staff_required
@require_http_methods(["GET", "POST"])
def account_schedule(request, pk):
    account = get_object_or_404(Account, pk=pk)
    form = ScheduleForm(
        request.POST if request.method == "POST" else None, account=account
    )
    if request.method == "POST" and form.is_valid():
        cleaned = form.cleaned_data
        with transaction.atomic():
            # Serialize with scheduler advancement and edits so a stale form
            # cannot move next_daily_at backwards or discard a due transition.
            account = get_object_or_404(
                Account.objects.select_for_update(), pk=pk
            )
            account.schedule_time = cleaned["schedule_time"]
            account.poll_hours = cleaned["poll_hours"]
            account.timezone = cleaned["timezone"]
            now = timezone.now()
            try:
                account.next_daily_at = (
                    next_daily(account, now) if account.enabled else None
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                account.next_poll_at = (
                    now + timedelta(hours=account.poll_hours)
                    if account.enabled and account.poll_hours
                    else None
                )
                account.save(
                    update_fields=[
                        "schedule_time",
                        "poll_hours",
                        "timezone",
                        "next_daily_at",
                        "next_poll_at",
                        "updated_at",
                    ]
                )
                audit(request.user, "account.schedule", account)
                messages.success(request, "执行计划已更新。")
                return redirect("portal:account_detail", pk=account.pk)
    return render(
        request,
        "portal/account_schedule.html",
        _base_context(account=account, form=form),
    )


@staff_required
@require_http_methods(["GET", "POST"])
def account_delete(request, pk):
    if request.method == "POST":
        with transaction.atomic():
            # Lock before checking for active runs.  The scheduler and worker
            # both take the account lock before touching a run; checking
            # afterwards closes the claim-vs-delete race.
            account = get_object_or_404(
                Account.objects.select_for_update(), pk=pk
            )
            active_runs = account.runs.filter(
                status__in=ACTIVE_RUN_STATUSES
            ).exists()
            run_count = account.runs.count()
            if active_runs:
                messages.error(request, "该账户仍有待执行或执行中的任务，暂不能删除。")
                return redirect("portal:account_detail", pk=account.pk)
            # A disabled account is a deliberate purge: deleting it also removes
            # its run history through the FK CASCADE.  An enabled account needs
            # the explicit confirmation checkbox rendered in the delete form.
            if not account.enabled or request.POST.get("confirm") == "yes":
                audit(request.user, "account.delete", account)
                account.delete()
                messages.success(request, "账户及其历史记录已删除。")
                return redirect("portal:dashboard")
            messages.error(request, "请勾选确认后再删除已启用的账户。")
    else:
        account = get_object_or_404(Account, pk=pk)
        active_runs = account.runs.filter(status__in=ACTIVE_RUN_STATUSES).exists()
        run_count = account.runs.count()

    return render(
        request,
        "portal/account_confirm_delete.html",
        _base_context(account=account, active_runs=active_runs, run_count=run_count),
    )
