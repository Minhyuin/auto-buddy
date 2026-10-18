#!/usr/bin/env bash
# Idempotent host bootstrap for AutoBuddy.
#
# This installs files, directories, secrets (optionally) and systemd units. It
# deliberately never starts containers, runs migrations or deploys a release.
# See deployment/README.md for the manual first-release bootstrap.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_DIR="/opt/auto-buddy"
CONFIG_DIR="/etc/auto-buddy"
SECRETS_DIR="${CONFIG_DIR}/secrets"
STATE_DIR="/var/lib/auto-buddy/updater"
BACKUP_DIR="/var/backups/auto-buddy"
UPDATER_DIR="/usr/local/lib/auto-buddy"
SYSTEMD_DIR="/etc/systemd/system"

GENERATE_SECRETS=0
SKIP_SYSTEMD=0

usage() {
    cat <<'EOF'
Usage: sudo ./deployment/install.sh [options]

  --generate-secrets   Create missing secret files with random values.
  --skip-systemd       Do not install/daemon-reload systemd units.
  -h, --help           Show this help.

The script is safe to re-run: existing configuration and secret files are
never overwritten. It does not start Docker containers.
EOF
}

while (($#)); do
    case "$1" in
        --generate-secrets) GENERATE_SECRETS=1 ;;
        --skip-systemd) SKIP_SYSTEMD=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if [[ "${EUID}" -ne 0 ]]; then
    echo "run as root (sudo)" >&2
    exit 1
fi

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "this installer targets Linux; current host is $(uname -s)" >&2
    exit 1
fi

for command in install openssl python3; do
    command -v "$command" >/dev/null 2>&1 || {
        echo "missing required command: $command" >&2
        exit 1
    }
done

if ! command -v docker >/dev/null 2>&1; then
    echo "missing Docker Engine; install Docker with the Compose v2 plugin" >&2
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "missing Docker Compose v2 plugin (docker compose)" >&2
    exit 1
fi

python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("the updater requires Python 3.10 or newer")
PY

if ! command -v cosign >/dev/null 2>&1; then
    echo "warning: cosign is not installed; the updater cannot verify releases" >&2
fi

echo "==> installing directories"
install -d -m 0755 -o root -g root "$PROJECT_DIR"
install -d -m 0755 -o root -g root "$PROJECT_DIR/deployment" "$PROJECT_DIR/deployment/caddy"
install -d -m 0750 -o root -g root "$CONFIG_DIR"
install -d -m 0700 -o root -g root "$SECRETS_DIR"
install -d -m 0750 -o root -g root /var/lib/auto-buddy "$STATE_DIR" "$BACKUP_DIR"
install -d -m 0755 -o root -g root "$UPDATER_DIR"

echo "==> installing Compose stack and Caddy configuration"
install -m 0644 -o root -g root "$REPO_ROOT/compose.yaml" "$PROJECT_DIR/compose.yaml"
install -m 0644 -o root -g root \
    "$REPO_ROOT/deployment/caddy/Caddyfile" "$PROJECT_DIR/deployment/caddy/Caddyfile"
install -m 0644 -o root -g root \
    "$REPO_ROOT/deployment/README.md" "$PROJECT_DIR/deployment/README.md"
install -m 0755 -o root -g root \
    "$REPO_ROOT/scripts/auto_buddy_updater.py" "$UPDATER_DIR/auto_buddy_updater.py"

echo "==> installing example configuration (existing files are kept)"
if [[ ! -e "$CONFIG_DIR/runtime.env" ]]; then
    install -m 0640 -o root -g root "$REPO_ROOT/deployment/runtime.env.example" "$CONFIG_DIR/runtime.env"
    echo "    created $CONFIG_DIR/runtime.env - edit APP_DOMAIN/ALLOWED_HOSTS/CSRF_TRUSTED_ORIGINS"
else
    echo "    kept existing $CONFIG_DIR/runtime.env"
fi
if [[ ! -e "$CONFIG_DIR/updater.json" ]]; then
    install -m 0600 -o root -g root \
        "$REPO_ROOT/deployment/updater.example.json" "$CONFIG_DIR/updater.json"
    echo "    created $CONFIG_DIR/updater.json - replace OWNER and verify the cosign identity"
else
    echo "    kept existing $CONFIG_DIR/updater.json"
fi

make_django_key() {
    local path="$1"
    if [[ -e "$path" ]]; then
        echo "    kept existing $path"
        return
    fi
    (umask 077; openssl rand -base64 48 | tr -d '\n' > "$path")
    echo "    generated $path"
}

make_db_password() {
    local path="$1"
    if [[ -e "$path" ]]; then
        echo "    kept existing $path"
        return
    fi
    (umask 077; openssl rand -base64 32 | tr -d '\n' > "$path")
    echo "    generated $path"
}

make_credential_keys() {
    local path="$1"
    if [[ -e "$path" ]]; then
        echo "    kept existing $path"
        return
    fi
    python3 - "$path" <<'PY'
import base64
import json
import os
import sys

path = sys.argv[1]
value = {"active": base64.b64encode(os.urandom(32)).decode("ascii")}
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(value, handle)
    handle.write("\n")
PY
    echo "    generated $path"
}

if [[ "$GENERATE_SECRETS" -eq 1 ]]; then
    echo "==> generating missing secrets"
    make_django_key "$SECRETS_DIR/django_key"
    make_credential_keys "$SECRETS_DIR/credential_keys"
    make_db_password "$SECRETS_DIR/db_password"
else
    echo "==> no secrets generated (pass --generate-secrets or follow README.md)"
fi

# Compose file-backed secrets are bind-mounted files, not Docker-managed secret
# objects: their Compose `mode`/`uid`/`gid` fields are ignored for file sources.
# The container runs as uid 10001 (and PostgreSQL as uid 999), so a root-owned
# 0600 file would be unreadable. Make the directory root-only 0700, which stops
# ordinary host users traversing it, while the files are 0444 so any container
# that receives the bind mount can read them. The host directory remains the
# access-control boundary; Docker/root can read the files by design.
chown root:root "$SECRETS_DIR" 2>/dev/null || true
chmod 0700 "$SECRETS_DIR"
for file in django_key credential_keys db_password; do
    if [[ -e "$SECRETS_DIR/$file" ]]; then
        chown root:root "$SECRETS_DIR/$file"
        chmod 0444 "$SECRETS_DIR/$file"
    fi
done

if [[ "$SKIP_SYSTEMD" -eq 0 ]]; then
    if command -v systemctl >/dev/null 2>&1; then
        echo "==> installing systemd units (not enabling or starting them)"
        install -m 0644 -o root -g root \
            "$REPO_ROOT/deployment/systemd/auto-buddy-updater.service" \
            "$SYSTEMD_DIR/auto-buddy-updater.service"
        install -m 0644 -o root -g root \
            "$REPO_ROOT/deployment/systemd/auto-buddy-updater.timer" \
            "$SYSTEMD_DIR/auto-buddy-updater.timer"
        systemctl daemon-reload
    else
        echo "warning: systemctl not found; systemd units were not installed" >&2
    fi
else
    echo "==> skipped systemd installation"
fi

echo
echo "Installed. Next steps:"
echo "  1. Edit $CONFIG_DIR/runtime.env and $CONFIG_DIR/updater.json."
echo "  2. Ensure $SECRETS_DIR has django_key, credential_keys and db_password."
echo "  3. Bootstrap a published release manually; see deployment/README.md."
echo "  4. Record it with:"
echo "       python3 $UPDATER_DIR/auto_buddy_updater.py --config $CONFIG_DIR/updater.json --baseline --tag vX.Y.Z"
echo "  5. Enable the timer: systemctl enable --now auto-buddy-updater.timer"
echo
echo "No containers were started and no release was deployed by this script."
