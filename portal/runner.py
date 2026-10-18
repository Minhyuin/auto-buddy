"""Read-only subprocess integration with the unmodified upstream CLI.

No raw upstream message/body is persisted: even a future upstream version may
accidentally print a secret. Only locally defined outcome codes and prose leave
this module. Never automatically retry ambiguous write operations.
"""
import json
import os
import re
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from django.conf import settings
from .services import normalize_session

SUMMARIES = {
    'CLAIMED': '签到领取成功', 'ALREADY': '今日已经签到', 'INACTIVE': '当前无签到活动',
    'AUTH_REJECTED': '认证被拒绝，请替换凭证；账户已暂停',
    'FORBIDDEN': '服务端拒绝此操作，请检查权限或活动规则',
    'AUTH_ERROR': '凭证格式或兼容性错误', 'NO_SESSION': '会话不可用',
    'NO_AUTH': '未找到任务凭证', 'NETWORK': '网络不可达',
    'TIMEOUT': '上游时间预算耗尽，操作结果可能不完整',
    'ERROR': '上游执行错误', 'UNKNOWN': '无法确认操作结果，请检查后再执行',
    'STATUS_OK': '在线认证检查通过（未执行签到）',
}

def classify(returncode, output, trigger):
    if not isinstance(output, dict):
        return 'unknown', 'INVALID_OUTPUT', '上游返回格式不兼容，未保存原始输出'
    if output.get('http') in (401, 403):
        result = 'AUTH_REJECTED' if output['http'] == 401 else 'FORBIDDEN'
        return 'failed', result, SUMMARIES[result]
    if trigger == 'check':
        code = output.get('http')
        if type(code) is int and 200 <= code < 300 and returncode == 0 and output.get('result') not in ('AUTH_REJECTED', 'FORBIDDEN', 'AUTH_ERROR', 'NO_SESSION', 'NO_AUTH'):
            return 'success', 'STATUS_OK', SUMMARIES['STATUS_OK']
    result = output.get('result')
    if not isinstance(result, str) or result not in SUMMARIES:
        return 'unknown', 'INVALID_OUTPUT', '上游返回结果不兼容，未保存原始输出'
    summary = SUMMARIES[result]
    if result in ('CLAIMED', 'ALREADY', 'INACTIVE'):
        if returncode != 0 or output.get('needs_attention') is True:
            return 'partial', result, summary + '；成长中心或其他步骤需要处理'
        return 'success', result, summary
    return ('unknown' if result in ('TIMEOUT', 'UNKNOWN') else 'failed'), result, summary

def auth_root():
    root = Path(settings.AUTH_TEMP_ROOT).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != 'nt':
        stat = root.stat()
        if stat.st_mode & 0o077 or stat.st_uid != os.getuid():
            raise ValueError('临时凭证目录权限不安全')
    return root


def cleanup_stale_sessions(*, older_than=720):
    """Remove only our aged task directories, after acquiring the worker lease.

    A running process has a <=600s deadline; the default 720s age cannot remove
    its working directory. Symlinks and unrelated names are never followed.
    Production additionally stores this directory on container-private tmpfs.
    """
    root = auth_root()
    removed = 0
    for path in root.iterdir():
        if not re.fullmatch(r'task-[a-z0-9_]{8}', path.name) or path.is_symlink():
            continue
        if path.parent.resolve() != root or not path.is_dir():
            continue
        try:
            if path.stat().st_mtime < time.time() - older_than:
                shutil.rmtree(path)
                removed += 1
        except FileNotFoundError:
            pass
    return removed


def execute(session, *, mode, trigger):
    if mode not in ('signin', 'full') or trigger not in ('manual', 'daily', 'poll', 'check'):
        raise ValueError('任务模式或来源无效')
    session = normalize_session(session)
    if trigger == 'check':
        action = 'status'
    elif mode == 'signin':
        action = 'all'  # status + idempotent check-in; no growth actions
    else:
        action = 'auto'  # Deterministic Python, NOT an AI model invocation.
    root = auth_root()
    script = settings.WORKBUDDY_SCRIPT.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='task-', dir=root) as tmp:
        directory = Path(tmp)
        auth = directory / 'session.json'
        auth.write_text(json.dumps(session), encoding='utf-8')
        auth.chmod(0o600)
        # Minimal environment: no database secret paths or credential keyring.
        env = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'WINDIR', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR') if key in os.environ}
        env.update({'WORKBUDDY_AUTH_FILE': str(auth), 'WORKBUDDY_BUDGET_SECONDS': '420', 'HOME': tmp, 'TMPDIR': tmp, 'PYTHONIOENCODING': 'utf-8'})
        output_path = directory / 'output.jsonl'
        with output_path.open('wb') as stream:
            process = subprocess.Popen([sys.executable, '-I', str(script), action], stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.DEVNULL, cwd=tmp, env=env, shell=False)
            deadline = time.monotonic() + settings.WORKER_MAX_SECONDS
            try:
                while process.poll() is None:
                    if time.monotonic() >= deadline or output_path.stat().st_size > 262144:
                        process.kill()
                        process.wait(timeout=10)
                        return 'unknown', 'PROCESS_LIMIT', '执行超过时间或输出限制，结果不明；未自动重试'
                    time.sleep(0.2)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
        if output_path.stat().st_size > 262144:
            return 'unknown', 'INVALID_OUTPUT', '上游输出超过限制'
        try:
            lines = output_path.read_text(encoding='utf-8').splitlines()
            if action == 'all':
                # The upstream debugging command emits two independent JSON lines.
                results = [json.loads(line) for line in lines if line.strip()]
                if not results or not all(isinstance(item, dict) for item in results):
                    raise ValueError()
                auth_failure = next((item for item in results if item.get('http') in (401, 403) or item.get('result') in ('AUTH_REJECTED', 'FORBIDDEN', 'AUTH_ERROR', 'NO_SESSION')), None)
                if auth_failure:
                    return classify(process.returncode, auth_failure, trigger)
                statuses = [item for item in results if item.get('step') == 'status']
                claims = [item for item in results if item.get('step') == 'claim']
                if len(statuses) == 1 and len(claims) == 1 and process.returncode == 0 and claims[0].get('needs_attention') is False:
                    return 'success', 'SIGNIN_OK', '签到步骤完成（仅签到模式）'
                return 'unknown', 'SIGNIN_UNCONFIRMED', '签到结果未能确认，请检查后再执行'
            if len(lines) != 1:
                raise ValueError()
            return classify(process.returncode, json.loads(lines[0]), trigger)
        except (ValueError, UnicodeError, TypeError):
            return 'unknown', 'INVALID_OUTPUT', '上游返回格式不兼容，未保存原始输出'
