# AutoBuddy deployment and operations

This directory contains the production deployment assets for AutoBuddy:
Docker Compose stack, Caddy TLS terminator, host updater, systemd units and
the install bootstrap. It is intentionally **not** a one-click deployment and
nothing here has been run against a live Linux host in this repository's
development environment. See [Validation status](#validation-status).

The supported topology is a single Linux host running Docker Engine:

| Component | Image / command | Exposure |
| --- | --- | --- |
| `db` | `postgres:16-bookworm` | internal Docker network only |
| `migrate` | one-shot `python manage.py migrate --noinput` | none |
| `web` | gunicorn, `autobuddy.wsgi:application`, port 8000 | through Caddy |
| `worker` | `python manage.py runworker` | none |
| `caddy` | `caddy:2.8.4-alpine` | host ports 80/443; health on `127.0.0.1:8080` |

Application containers run as uid/gid `10001`, read-only, with all Linux
capabilities dropped and no Docker socket. The worker alone gets a tmpfs at
`/run/auto-buddy` for decrypted session material.

## Host layout

The updater and the manual commands below assume these fixed paths. Keep them
consistent if you copy the example configuration.

| Path | Purpose |
| --- | --- |
| `/opt/auto-buddy/compose.yaml` | deployed Compose file |
| `/opt/auto-buddy/deployment/caddy/Caddyfile` | mounted Caddy config |
| `/etc/auto-buddy/runtime.env` | non-secret runtime settings, mode `0640` |
| `/etc/auto-buddy/secrets` | secret directory, root-owned mode `0700` |
| `/etc/auto-buddy/secrets/django_key` | Django `SECRET_KEY`, read-only mode `0444` |
| `/etc/auto-buddy/secrets/credential_keys` | JSON credential keyring, read-only mode `0444` |
| `/etc/auto-buddy/secrets/db_password` | PostgreSQL password, read-only mode `0444` |
| `/etc/auto-buddy/updater.json` | updater configuration, mode `0600` |
| `/var/lib/auto-buddy/updater` | current/previous/failed release state |
| `/var/backups/auto-buddy` | verified PostgreSQL dumps |
| `/usr/local/lib/auto-buddy/auto_buddy_updater.py` | updater, installed separately |

The updater is deliberately outside the release artifact. A release can never
replace the updater executable. `deployment/install.sh` installs the updater,
Compose file, Caddyfile and systemd units; it does **not** start containers,
run migrations or deploy a release.

## Prerequisites

* Linux x86_64 host (the published image is `linux/amd64` only).
* Docker Engine with the Compose v2 plugin (`docker compose version`).
* `cosign` v2 (`cosign version`).
* Python 3.10 or newer on the host for the updater (standard library only).
* `openssl`, `gzip`, `curl` and outbound HTTPS to `github.com`,
  `ghcr.io`, `api.github.com` and `token.actions.githubusercontent.com`.
* A DNS name with A/AAAA records pointing at the host. TCP 80 and 443 (and UDP
  443 for HTTP/3) must reach the host for ACME certificate issuance.
* A GitHub repository with the release workflow enabled and permission to pull
  the resulting GHCR image. Public images need no registry credentials. For a
  private image, `github_token_file` authenticates the updater's GitHub API and
  registry-manifest requests, but `docker pull` uses Docker's own credential
  store; the updater does not run `docker login`. Log Docker in for root once,
  using a read-only token and the same `HOME` as the unit:
  `sudo DOCKER_CONFIG=/var/lib/auto-buddy/.docker docker login ghcr.io -u USER`.
  Never put that token in `runtime.env` or a compose build arg.

## 1. Bootstrap secrets and configuration

From a trusted checkout of this repository:

```sh
sudo apt-get install -y docker.io docker-compose-v2 cosign openssl
sudo ./deployment/install.sh --generate-secrets
```

The installer is idempotent. `--generate-secrets` creates any missing secret
files with random values and never overwrites an existing file. Without that
flag it creates no secret files; create them manually as shown below.

If you prefer to create secrets manually:

```sh
sudo install -d -m 0750 -o root -g root /etc/auto-buddy
sudo install -d -m 0700 -o root -g root /etc/auto-buddy/secrets

# Django SECRET_KEY: at least 50 characters, no newline required.
sudo sh -c 'openssl rand -base64 48 | tr -d "\n" > /etc/auto-buddy/secrets/django_key'

# One active AES-256 credential key, JSON key-id -> base64(32 random bytes).
sudo python3 - <<'PY'
import base64, json, os
path = "/etc/auto-buddy/secrets/credential_keys"
with open(path, "w", encoding="utf-8") as handle:
    json.dump({"active": base64.b64encode(os.urandom(32)).decode("ascii")}, handle)
    handle.write("\n")
PY

# PostgreSQL password. A trailing newline is stripped by the app settings, but
# removing it avoids surprises in psql and pg_dump.
sudo sh -c 'openssl rand -base64 32 | tr -d "\n" > /etc/auto-buddy/secrets/db_password'

sudo chown -R root:root /etc/auto-buddy/secrets
sudo chmod 0700 /etc/auto-buddy/secrets
sudo chmod 0444 /etc/auto-buddy/secrets/*
```

Compose `file:` secrets are bind-mounted into the containers, not Docker-managed
secret objects. The `mode`, `uid` and `gid` fields in a Compose file secret are
ignored for a bind-mounted host file, and the container processes run as uid
`10001` (web/worker/migrate) and uid `999` (PostgreSQL). A root-owned `0600`
file is therefore unreadable inside those containers. Keep the host directory
root-owned `0700` — that is the access-control boundary that prevents ordinary
host users from reaching the files — and make the files `0444` so each
container that receives the bind mount can read its copy. Do not widen the
directory permissions to compensate.

The keyring may contain old keys during rotation. Set `ACTIVE_CREDENTIAL_KEY`
to the key id used for new encryptions; the default is the first key. Key ids
must be alphanumeric only, at most 32 characters, and values must be
base64-encoded exactly 32 bytes. Never commit or paste these files into a chat.

Edit `/etc/auto-buddy/runtime.env` and set at minimum:

```dotenv
APP_DOMAIN=portal.example.com
ALLOWED_HOSTS=portal.example.com,127.0.0.1
CSRF_TRUSTED_ORIGINS=https://portal.example.com
DATABASE_HOST=db
DATABASE_NAME=autobuddy
DATABASE_USER=autobuddy
POSTGRES_DB=autobuddy
POSTGRES_USER=autobuddy
TIME_ZONE=Asia/Shanghai
DJANGO_DEBUG=false
```

`APP_DOMAIN` is used by Caddy for automatic HTTPS. `ALLOWED_HOSTS` must contain
the public hostname and `127.0.0.1` (for container and updater loopback health
probes); `CSRF_TRUSTED_ORIGINS` must match the public origin exactly. Keep
`DJANGO_DEBUG=false`; production settings then enforce secure cookies, HSTS
and HTTPS redirects. Do not put secrets in this file.

Copy and edit the updater configuration:

```sh
sudo cp ./deployment/updater.example.json /etc/auto-buddy/updater.json
sudo chmod 0600 /etc/auto-buddy/updater.json
sudoedit /etc/auto-buddy/updater.json
```

Replace `OWNER`, the repository and the cosign identity with your repository.
The example expects a release workflow at
`.github/workflows/release.yml` on `refs/heads/main`; see
[Release identity](#release-identity).

## 2. Bootstrap the first release

The updater refuses to deploy without a recorded current release, so a fresh
host is bootstrapped once by hand. Choose a published stable release, not a
local build.

1. Read the release's `release-manifest.json` and note its `image.digest`.
2. Verify the digest and signature from the release page:

   ```sh
   cosign verify \
     --certificate-identity-regexp '^https://github\.com/OWNER/auto-buddy/\.github/workflows/release\.yml@refs/heads/main$' \
     --certificate-oidc-issuer https://token.actions.githubusercontent.com \
     ghcr.io/owner/auto-buddy@sha256:...
   ```

3. Deploy that exact digest with Compose. `APP_IMAGE` must include the digest;
   never use a mutable tag for this step:

   ```sh
   export SECRETS_DIR=/etc/auto-buddy/secrets
   export RUNTIME_ENV_FILE=/etc/auto-buddy/runtime.env
   export COMPOSE_PROJECT_NAME=auto-buddy
   export APP_IMAGE='ghcr.io/owner/auto-buddy@sha256:...'
   export APP_VERSION='v1.2.3'
   export VCS_REF='<40-char commit>'
   export MIGRATIONS_BACKWARD_COMPATIBLE=true
   export MANUAL_APPROVAL_REQUIRED=false

   docker compose \
     --project-directory /opt/auto-buddy \
     -f /opt/auto-buddy/compose.yaml \
     --env-file "$RUNTIME_ENV_FILE" \
     pull

   docker compose \
     --project-directory /opt/auto-buddy \
     -f /opt/auto-buddy/compose.yaml \
     --env-file "$RUNTIME_ENV_FILE" \
     up -d --wait
   ```

4. Record the running release so the timer will not downgrade it:

   ```sh
   sudo /usr/bin/python3 /usr/local/lib/auto-buddy/auto_buddy_updater.py \
     --config /etc/auto-buddy/updater.json \
     --baseline --tag v1.2.3
   ```

   `--baseline` verifies the release manifest, image digest, cosign signature
   and image labels, then inspects the running `web` and `worker` containers.
   Their Docker image ID must exactly equal the verified target image ID, and
   their required OCI labels must match the release; a mismatch aborts without
   recording `current.json`. The command does not start or stop containers.

5. Create the first administrator:

   ```sh
   docker compose ... exec web python manage.py bootstrap_admin
   ```

   This interactive command remains the default for a public-domain deployment;
   passwords are never command-line arguments or source defaults.

   Alternatively, a **loopback HTTPS deployment reached through an SSH tunnel**
   can use the first-visit web form. This is not a public registration endpoint:
   bind every published Caddy port to `127.0.0.1`, use `localhost` as the site
   hostname, and only then set `INITIAL_SETUP_DEADLINE` in the external runtime
   env file to the current Unix time plus 900 seconds. Recreate the web container
   with that env file. Do not enable this mode on a shared/untrusted host or a
   public listener. The login page redirects to setup only while that deadline
   is at most 1800 seconds in the future and there are no users. Enter a username,
   password and confirmation in the CSRF-protected form; success creates the sole
   administrator and signs in. Any existing user permanently closes the form,
   even if the deadline has not expired. Set the external deadline back to `0`
   and recreate web after use. Do not send passwords to chat, URLs or shell history.

6. Confirm the public site and `/health/`, and that the loopback listener at
   `http://127.0.0.1:8080/health/` returns Django's 503 when the database is
   unreachable. (The public site can return 502 while Caddy's active health
   check has marked the upstream down.) Then enable the updater timer (see
   below).

## 3. Start, stop and inspect the stack

Always pass the same project directory, file, env file and secrets directory.
The updater does this internally; for manual operations export:

```sh
export SECRETS_DIR=/etc/auto-buddy/secrets
export RUNTIME_ENV_FILE=/etc/auto-buddy/runtime.env
export COMPOSE_PROJECT_NAME=auto-buddy
# After baseline/automatic upgrades, preserve the actually deployed release.
# runtime.env may still describe the image used for the original installation.
if [ -f /var/lib/auto-buddy/updater/current.json ]; then
  export APP_IMAGE="$(python3 -c 'import json; print(json.load(open("/var/lib/auto-buddy/updater/current.json"))["image"])')"
  export APP_VERSION="$(python3 -c 'import json; print(json.load(open("/var/lib/auto-buddy/updater/current.json"))["tag"])')"
fi

docker compose --project-directory /opt/auto-buddy \
  -f /opt/auto-buddy/compose.yaml --env-file "$RUNTIME_ENV_FILE" ps
docker compose ... logs --tail=200 web worker caddy
docker compose ... up -d --wait
docker compose ... down            # keeps named volumes
docker compose ... down -v         # DANGER: deletes the database
```

The `migrate` service runs once per `up` and gates web/worker startup. On a
normal update the updater stops web and worker, runs migrations explicitly,
then starts web, waits for the loopback health endpoint, and only then starts
the worker.

### Backups

The updater takes a `pg_dump` after pausing/draining the worker and stopping
web. It validates gzip integrity and requires at least
`backup_min_tables` (default 5) `CREATE TABLE` statements before continuing.
Dumps live in `/var/backups/auto-buddy/auto-buddy-<tag>-<UTC>.sql.gz` and are
pruned to `backup_retention` (default 5).

The updater **never restores a dump automatically**. A failed deploy leaves
the previous or new containers in a documented state and a backup for an
operator. Manual restore is destructive:

```sh
# Stop writers first. This is deliberately manual and not automated.
docker compose ... stop worker web
gunzip -c /var/backups/auto-buddy/auto-buddy-v1.2.3-....sql.gz \
  | docker compose ... exec -T db \
      psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" --set ON_ERROR_STOP=1
docker compose ... up -d --wait
```

A `CREATE TABLE` count is a sanity check, not proof that every table, row or
sequence was restored. Rehearse restore on a disposable copy before relying on
it. Named volumes (`db_data`, `caddy_data`, `caddy_config`) persist across
`down`; back up the volume/disk separately if you need disaster recovery.

## 4. Updates

The updater is a standard-library Python script with a systemd timer. It:

1. selects the newest non-draft, non-prerelease GitHub release matching
   `vMAJOR.MINOR.PATCH` (or `--tag`);
2. refuses downgrades and a version recorded in `failed.json`;
3. verifies the release manifest, its SHA-256 checksum, the GHCR manifest
   digest, cosign keyless identity and the image OCI labels;
4. checks whether Django migrations are pending with the new image;
5. pauses worker scheduling, waits for `worker_control drained`, stops worker
   (650 s grace) and web (30 s grace);
6. takes and verifies a database dump;
7. runs `migrate --noinput` with the new image, starts Caddy/web, probes
   `http://127.0.0.1:8080/health/`, starts the worker and resumes scheduling;
8. records `current.json`/`previous.json`, or `failed.json` plus
   `manual-required.json` when automatic recovery is unsafe.

Install/enable the units (the installer copies them but does not enable them):

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now auto-buddy-updater.timer
systemctl list-timers auto-buddy-updater.timer
```

The timer runs 3 minutes after boot and every 5 minutes thereafter, with a
randomized delay. The service is `Type=oneshot`, root, no-new-privileges, with
a strict read-only system (`ProtectSystem=strict`) and write access only to the
state, Docker config/cache and backup directories.
It never edits `/opt/auto-buddy` or its own executable.

Useful commands:

```sh
UPDATER=/usr/local/lib/auto-buddy/auto_buddy_updater.py
sudo python3 "$UPDATER" --config /etc/auto-buddy/updater.json --check
sudo python3 "$UPDATER" --config /etc/auto-buddy/updater.json --status
sudo python3 "$UPDATER" --config /etc/auto-buddy/updater.json --dry-run
sudo python3 "$UPDATER" --config /etc/auto-buddy/updater.json --tag v1.2.4 --dry-run
sudo python3 "$UPDATER" --config /etc/auto-buddy/updater.json --retry-failed
sudo python3 "$UPDATER" --config /etc/auto-buddy/updater.json --clear-failed
sudo python3 "$UPDATER" --config /etc/auto-buddy/updater.json --allow-unsafe-migrations
```

Exit codes: `0` success/no-op, `1` deployment or verification failure, `2`
configuration error, `3` another updater holds the lock, `5` manual approval is
required. A systemd exit 5 therefore means a human must inspect
`/var/lib/auto-buddy/updater/manual-required.json` and the logs before doing
anything else.

### Failed and unsafe releases

`failed.json` records the tag and stage that failed. Later timer runs skip that
tag until an operator either fixes the release and runs `--retry-failed` or
runs `--clear-failed` to allow a newer release to be considered. Do not
blindly clear state: inspect the backup and service logs first.

`--allow-unsafe-migrations` is an explicit override for a release whose
manifest reports non-backward-compatible operations or manual approval. The
updater still stops services and takes a backup, but rollback may be
impossible after an irreversible migration. Prefer a forward fix; never let a
timer pass this flag. Non-backward-compatible releases should be deployed in a
maintenance window with a tested restore plan.

### Automatic rollback rules

* Failure before migrations are applied: the old image is restarted, health is
  checked, the worker is resumed, and no failed-version state is recorded.
* Failure after migrations when the manifest says migrations are backward
  compatible: the old image is restarted and health-checked, the old worker is
  started, and the new version is recorded as failed.
* Failure after migrations when migrations are **not** backward compatible:
  the updater stops and writes `manual-required.json`; it does not start the
  old image and does not restore the database.

The migration policy is deliberately conservative. `AddField` is statically
backward compatible only when the new field is nullable (`null=True`) or has an
explicit persistent `db_default`. A Django `default=` is applied only while the
migration runs and is then removed; it does not make old-image INSERTs that
omit the new `NOT NULL` column safe. Such fields are classified `review`, so a
release containing them requires manual approval (`exit 5`) and cannot
auto-rollback after migrations.
* Health failures before the worker starts roll back web first; the worker is
  not resumed against an unhealthy web.

Manual rollback of an image is possible only when the previous release can read
the migrated schema. There is no automatic database downgrade.

## 5. TLS, Caddy and the health listener

Caddy reads `/etc/caddy/Caddyfile` from the image mount and obtains
certificates automatically for `APP_DOMAIN`. TCP 80 must remain reachable for
ACME HTTP-01 and redirects; TCP/UDP 443 serve HTTPS/HTTP/3. Caddy's data and
config volumes persist certificates. The admin API is disabled; reload by
restarting the container.

The public site caps request bodies at 96 KB and streams them in memory.
Django additionally limits each file/form field to 64–70 KB. Caddy does not
spool request bodies to disk. Access logging is intentionally disabled at both
Caddy and gunicorn because request paths can contain credential identifiers;
runtime errors still go to stderr and are captured by Docker.

The loopback site on `127.0.0.1:8080` exists only for the updater and Docker
health checks. It forwards `X-Forwarded-Proto: https`; do not publish it on a
public interface. Django exempts only `^health/$` from the HTTPS redirect so
the container-local probe works over plain HTTP.

## 6. Release identity and supply chain

The release workflow builds `linux/amd64`, pushes an immutable digest to GHCR,
signs it with cosign keyless OIDC, verifies the signature itself, and only then
creates the version, `stable` and `latest` tags. The updater pulls by digest,
never by a mutable tag, and cross-checks the image labels
`org.auto-buddy.*` against the signed manifest.

`updater.json` requires an anchored `cosign_identity_regexp` and issuer
exactly `https://token.actions.githubusercontent.com`. The example identity is
the release workflow on `main`:

```text
^https://github\.com/OWNER/auto-buddy/\.github/workflows/release\.yml@refs/heads/main$
```

This is intentional: releases are created by a push to `main`, a
`workflow_dispatch`, or a reusable `workflow_call` on `main`, not by a tag push,
so the OIDC certificate identity is branch-bound, not tag-bound. A push to
`main` runs the release workflow, which computes the next semantic version from
Conventional Commits (`feat` → minor, `fix`/`perf`/`revert` → patch, breaking
change → major for 1.x+). If you change the workflow path or branch,
update the regexp and the subject regexp together. The updater does not trust
a mutable `stable`/`latest` tag; it resolves the release's manifest digest and
pulls `repository@sha256:...`.

The upstream `workbuddy-auto-signin` submodule is pinned by gitlink. Release
metadata records the **actual gitlink commit**, not the upstream branch HEAD.
The project only consumes upstream stable releases; production does not modify
upstream code. See `deployment/RELEASE_CHECKLIST.md` and
`deployment/updater.example.json`.

### Upstream caveat

Upstream stable `v1.7.0` has a known growth partial-result masking issue that is
fixed only in later unreleased `main` commits. We do not patch the submodule in
production. Document any growth/report anomaly as an upstream limitation and
wait for a formal upstream release; do not point the gitlink at unreleased
commits to obtain a fix.

## 7. Upstream release sync

`.github/workflows/upstream-sync.yml` polls upstream stable releases on a
schedule (and on manual dispatch). It checks out the release tag inside the
submodule, stages **only** the gitlink in the parent repository, pushes a
branch, opens a PR titled `fix(upstream): ...`, and explicitly dispatches CI for
that branch. The commit type is `fix`, not `chore`, so merging it produces a
patch release. It attempts `gh pr merge --auto --squash`; auto-merge may be
unavailable on personal repositories or when branch protection does not allow
it. In that case the PR is left open for a human to review and merge; the
workflow logs a clear fallback message and never force-merges.

The bot branch name includes the stable tag plus the workflow run and attempt,
so a stale closed PR/branch cannot cause a non-fast-forward retry; open PRs are
matched by the stable tag prefix. The CI watcher selects the explicit
`workflow_dispatch` run for the **parent commit that changed the gitlink**,
never the upstream gitlink SHA (which belongs to another repository).

After auto-merge reports success, the workflow polls the PR until it is
confirmed `MERGED`, then explicitly dispatches `release.yml` on `main`. This
dispatch is necessary because a merge performed with `GITHUB_TOKEN` suppresses
push-triggered workflows. Dispatching before the merge exists would release
unrelated main content, so the workflow never does that. If the wait window
expires, the PR stays open with a comment; a human merge triggers the normal
push release.

## 8. Troubleshooting

| Symptom | Check |
| --- | --- |
| Caddy cannot obtain a certificate | DNS A/AAAA, ports 80/443, `docker compose logs caddy`; rate limits may delay retries |
| `/health/` returns 503 | database container health, `DATABASE_*` values, secret file readability, `docker compose logs db web` |
| web restarts with secret errors | `/run/secrets/*` mounts, file mode/owner, `SECRET_KEY` length ≥50, keyring JSON/base64/32-byte values |
| updater exits 2 | config JSON, anchored identity regexp, absolute paths, `cosign`/`docker` binaries |
| updater exits 3 | another timer/manual run holds `updater.lock`; check `systemctl status` |
| updater exits 5 | read `manual-required.json`; do not delete state or restore the database without a plan |
| image verification fails | release workflow identity/branch, GHCR visibility, `cosign` version, clock skew |
| worker will not stop within grace | check `manage.py worker_control drained`; max Run duration is 600 s, grace is 650 s |

Logs: `journalctl -u auto-buddy-updater.service`, `docker compose logs`.
State: `/var/lib/auto-buddy/updater/{current,previous,failed,manual-required}.json`.
Never post secrets, session files or full environment dumps in issues.

## 9. Security notes

* Secrets are files under `/etc/auto-buddy/secrets`, mounted read-only into
  containers; they are never build args or image layers. The host directory is
  root-owned `0700`; files are `0444` because Compose bind-mounts file secrets
  and ignores their `mode`/`uid`/`gid` for a file source. The container UIDs
  (`10001`, PostgreSQL `999`) must be able to read the mounted copy.
* Application containers are non-root, read-only, cap-drop ALL,
  no-new-privileges, and have no Docker socket.
* The worker's `/run/auto-buddy` tmpfs is mode 0700, uid/gid 10001, and is
  destroyed when the container stops.
* Credential plaintext is handled in memory; edge and application limits keep
  request bodies bounded. Do not add disk-backed upload handlers or access
  logs that record request paths/body.
* The updater trusts only the pinned cosign workflow identity and an immutable
  digest. It refuses mutable tags for pulls and verifies the registry manifest
  digest against the signed digest.
* Backups contain all application data. Protect `/var/backups/auto-buddy` as
  sensitive data and test restores on an isolated host.

## Validation status

This repository's development host is Windows without a Docker engine, so the
following were **not executed here** and must be validated in CI or on a
staging Linux host before production:

* `docker compose config`, image build, container smoke test and Trivy scan
  (CI has jobs for these on `ubuntu-22.04`);
* systemd `ProtectSystem=strict` behavior (Docker/cosign writes under the
  allowed paths), timer installation and the full updater deploy,
  backup, rollback and manual-recovery paths (unit tests use fakes; they do
  not prove Linux/Docker/systemd behavior);
* Caddy ACME issuance, firewall/NAT behavior and real browser/CSRF flows;
* cosign verification against a real GHCR signature and the actual release
  workflow identity.

The updater unit tests, migration policy tests, version tests and deployment
asset checks run without Docker. Treat a green CI run as necessary but not
sufficient; perform one staging deploy and one restore rehearsal before
pointing the timer at production.

## Files

| File | Purpose |
| --- | --- |
| `compose.yaml` | production stack |
| `Dockerfile` | multi-stage image |
| `deployment/caddy/Caddyfile` | TLS/headers/health proxy |
| `deployment/runtime.env.example` | non-secret runtime settings |
| `deployment/secrets/*.example` | secret shapes; never use as-is |
| `deployment/updater.example.json` | updater configuration |
| `deployment/systemd/auto-buddy-updater.{service,timer}` | timer units |
| `deployment/install.sh` | idempotent host bootstrap |
| `deployment/RELEASE_CHECKLIST.md` | release gate checklist |
| `deployment/healthcheck.py` | image/Compose health probe |
| `scripts/auto_buddy_updater.py` | host updater |
| `scripts/release_manifest.py` | migration policy + signed manifest |
| `scripts/next_version.py` | conventional-commit versioning |
