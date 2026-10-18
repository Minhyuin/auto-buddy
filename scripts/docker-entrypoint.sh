#!/bin/sh
# AutoBuddy container entrypoint.
#
# Roles:
#   web      collect static files, then exec gunicorn on $PORT
#   anything else (migrate, worker, bootstrap_admin, ...)  exec the command as-is
#
# Every role first waits for the database TCP endpoint with a bounded timeout so
# `depends_on: service_healthy` failures are reported as a clear startup error
# instead of a Django traceback. Set DATABASE_WAIT_SECONDS=0 to skip.
set -eu

log() {
    printf '%s docker-entrypoint: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

wait_for_database() {
    host="${DATABASE_HOST:-}"
    seconds="${DATABASE_WAIT_SECONDS:-60}"
    port="${DATABASE_PORT:-5432}"

    case "$seconds" in
        ''|*[!0-9]*) log "invalid DATABASE_WAIT_SECONDS='$seconds'; using 60"; seconds=60 ;;
    esac
    [ "$seconds" -gt 0 ] || { log "database wait disabled"; return 0; }
    [ -n "$host" ] || { log "DATABASE_HOST is not set; skipping database wait"; return 0; }

    log "waiting up to ${seconds}s for database ${host}:${port}"
    python - "$host" "$port" "$seconds" <<'PY'
import socket
import sys
import time

host, port, seconds = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
deadline = time.monotonic() + seconds
last_error = None
while time.monotonic() < deadline:
    try:
        with socket.create_connection((host, port), timeout=3):
            print(f"database {host}:{port} reachable", file=sys.stderr)
            raise SystemExit(0)
    except OSError as exc:
        last_error = exc
        time.sleep(2)
print(
    f"database {host}:{port} not reachable within {seconds}s: {last_error}",
    file=sys.stderr,
)
raise SystemExit(1)
PY
}

role="${1:-web}"

if [ "$role" = "web" ]; then
    wait_for_database

    static_root="${STATIC_ROOT:-/app/staticfiles}"
    log "collecting static files into ${static_root}"
    if [ "${COLLECTSTATIC_CLEAR:-0}" = "1" ]; then
        python manage.py collectstatic --noinput --clear
    else
        python manage.py collectstatic --noinput
    fi

    port="${PORT:-8000}"
    log "starting gunicorn on 0.0.0.0:${port}"
    exec python -m gunicorn \
        --bind "0.0.0.0:${port}" \
        --workers "${GUNICORN_WORKERS:-3}" \
        --threads "${GUNICORN_THREADS:-4}" \
        --timeout "${GUNICORN_TIMEOUT:-60}" \
        --graceful-timeout "${GUNICORN_GRACEFUL_TIMEOUT:-30}" \
        --max-requests "${GUNICORN_MAX_REQUESTS:-1000}" \
        --max-requests-jitter "${GUNICORN_MAX_REQUESTS_JITTER:-100}" \
        --error-logfile - \
        autobuddy.wsgi:application
fi

wait_for_database
log "running: $*"
exec "$@"
