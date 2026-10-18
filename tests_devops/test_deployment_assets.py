"""Static checks for deployment assets and GitHub workflows.

These tests do not start Docker, systemd, Caddy or a PostgreSQL server. They
parse the files and assert the security/contract properties that CI later
exercises for real where a Linux runner is available.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def load_yaml(relative: str) -> dict:
    return yaml.safe_load(read(relative))


def triggers(workflow: dict) -> dict:
    value = workflow.get("on")
    if value is None:
        value = workflow.get(True)  # PyYAML 1.1 treats the bare key `on` as boolean.
    return value


class ComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.compose = load_yaml("compose.yaml")
        cls.services = cls.compose["services"]
        cls.text = read("compose.yaml")

    def test_expected_services(self) -> None:
        self.assertEqual(set(self.services), {"db", "migrate", "web", "worker", "caddy"})

    def test_database_is_postgres_16_internal_only(self) -> None:
        db = self.services["db"]
        self.assertEqual(db["image"], "postgres:16-bookworm")
        self.assertEqual(db["environment"]["POSTGRES_PASSWORD_FILE"], "/run/secrets/db_password")
        self.assertEqual(db["networks"], ["backend"])
        self.assertIs(self.compose["networks"]["backend"]["internal"], True)
        self.assertNotIn("ports", db)

    def test_application_containers_are_hardened(self) -> None:
        for name in ("web", "worker"):
            with self.subTest(service=name):
                service = self.services[name]
                self.assertIs(service["read_only"], True)
                self.assertIn("no-new-privileges:true", service["security_opt"])
                self.assertEqual(service["cap_drop"], ["ALL"])
                self.assertNotIn("privileged", service)
                self.assertNotIn("volumes", service)
        self.assertNotIn("docker.sock", self.text)

    def test_worker_tmpfs_and_grace_period(self) -> None:
        worker = self.services["worker"]
        self.assertIn("/run/auto-buddy:size=64m,mode=0700,uid=10001,gid=10001", worker["tmpfs"])
        self.assertEqual(worker["stop_grace_period"], "650s")
        self.assertEqual(worker["environment"]["AUTH_TEMP_ROOT"], "/run/auto-buddy")
        self.assertEqual(worker["environment"]["WORKBUDDY_SCRIPT"], "/app/workbuddy-auto-signin/signin.py")
        self.assertEqual(worker["command"], ["python", "manage.py", "runworker"])
        self.assertTrue(worker["healthcheck"]["disable"])

    def test_web_waits_for_migrations_and_uses_healthcheck(self) -> None:
        web = self.services["web"]
        self.assertEqual(web["depends_on"]["migrate"]["condition"], "service_completed_successfully")
        self.assertEqual(web["depends_on"]["db"]["condition"], "service_healthy")
        self.assertIn("deployment/healthcheck.py", " ".join(web["healthcheck"]["test"]))
        self.assertEqual(web["stop_grace_period"], "30s")
        self.assertIn("/app/staticfiles:size=256m,uid=10001,gid=10001,mode=0755", web["tmpfs"])

    def test_migrate_is_one_shot(self) -> None:
        migrate = self.services["migrate"]
        self.assertEqual(migrate["command"], ["python", "manage.py", "migrate", "--noinput"])
        self.assertEqual(migrate["restart"], "no")
        self.assertTrue(migrate["healthcheck"]["disable"])
        self.assertEqual(migrate["networks"], ["backend"])

    def test_caddy_owns_public_ports_and_no_database_network(self) -> None:
        caddy = self.services["caddy"]
        self.assertEqual(caddy["image"], "caddy:2.8.4-alpine")
        for port in ("80:80", "443:443", "443:443/udp", "127.0.0.1:8080:8080"):
            self.assertIn(port, caddy["ports"])
        self.assertEqual(caddy["networks"], ["edge"])
        self.assertEqual(caddy["cap_add"], ["NET_BIND_SERVICE"])
        self.assertEqual(caddy["depends_on"]["web"]["condition"], "service_healthy")

    def test_secrets_are_files_not_environment_values(self) -> None:
        secrets = self.compose["secrets"]
        for name, suffix in (
            ("django_key", "django_key"),
            ("credential_keys", "credential_keys"),
            ("db_password", "db_password"),
        ):
            with self.subTest(secret=name):
                self.assertTrue(secrets[name]["file"].endswith(suffix))
        for service in ("web", "worker", "migrate"):
            self.assertEqual(set(self.services[service]["secrets"]), {"django_key", "credential_keys", "db_password"})

    def test_runtime_env_is_explicit(self) -> None:
        for service in ("web", "worker", "migrate", "caddy"):
            self.assertIn("RUNTIME_ENV_FILE", self.text)
            self.assertIn("${RUNTIME_ENV_FILE:-./deployment/runtime.env}", self.services[service]["env_file"])


class DockerfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = read("Dockerfile")

    def test_python_and_stages(self) -> None:
        self.assertIn("ARG PYTHON_VERSION=3.12", self.text)
        self.assertIn("FROM python:${PYTHON_VERSION}-slim-bookworm AS builder", self.text)
        self.assertIn("FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime", self.text)

    def test_non_root_and_read_only_friendly(self) -> None:
        self.assertIn("groupadd --gid 10001", self.text)
        self.assertIn("useradd --uid 10001", self.text)
        self.assertIn("USER 10001:10001", self.text)
        self.assertIn("HOME=/tmp", self.text)

    def test_healthcheck_and_entrypoint(self) -> None:
        self.assertIn("HEALTHCHECK", self.text)
        self.assertIn("/app/deployment/healthcheck.py", self.text)
        self.assertIn('ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]', self.text)
        self.assertIn('CMD ["web"]', self.text)

    def test_release_labels(self) -> None:
        for label in (
            "org.opencontainers.image.version",
            "org.opencontainers.image.revision",
            "org.opencontainers.image.licenses=\"NOASSERTION\"",
            "org.auto-buddy.migrations-backward-compatible",
            "org.auto-buddy.manual-approval-required",
            "org.auto-buddy.upstream-commit",
        ):
            self.assertIn(label, self.text)

    def test_submodule_is_required_at_build_time(self) -> None:
        self.assertIn("workbuddy-auto-signin/signin.py", self.text)


class CaddyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = read("deployment/caddy/Caddyfile")

    def test_admin_off_and_no_access_log(self) -> None:
        self.assertIn("admin off", self.text)
        self.assertIsNone(re.search(r"(?m)^\s*log\s*(\{|$)", self.text))

    def test_security_headers_and_body_limits(self) -> None:
        self.assertIn("Strict-Transport-Security", self.text)
        self.assertIn("X-Content-Type-Options", self.text)
        self.assertIn('X-Frame-Options "DENY"', self.text)
        self.assertIn('Referrer-Policy "same-origin"', self.text)
        self.assertNotIn("SAMEORIGIN", self.text)
        self.assertNotIn("strict-origin-when-cross-origin", self.text)
        self.assertIn("request_body", self.text)
        self.assertIn("max_size 96KB", self.text)
        self.assertIn("max_size 16KB", self.text)

    def test_https_forwarding_and_health(self) -> None:
        self.assertEqual(self.text.count("header_up X-Forwarded-Proto https"), 2)
        self.assertIn("reverse_proxy web:8000", self.text)
        self.assertIn("health_uri /health/", self.text)
        self.assertIn("health_headers {", self.text)
        self.assertIn("Host {$APP_DOMAIN}", self.text)
        self.assertIn("{$APP_DOMAIN:localhost}", self.text)
        self.assertIn(":8080", self.text)


class SystemdAndInstallerTests(unittest.TestCase):
    def test_updater_service_is_oneshot_and_hardened(self) -> None:
        text = read("deployment/systemd/auto-buddy-updater.service")
        self.assertIn("Type=oneshot", text)
        self.assertIn("User=root", text)
        self.assertIn(
            "ExecStart=/usr/bin/python3 /usr/local/lib/auto-buddy/auto_buddy_updater.py "
            "--config /etc/auto-buddy/updater.json",
            text,
        )
        self.assertIn("TimeoutStartSec=1800", text)
        self.assertIn("NoNewPrivileges=yes", text)
        self.assertIn("Environment=HOME=/var/lib/auto-buddy", text)
        self.assertIn("ReadWritePaths=/var/lib/auto-buddy /var/backups/auto-buddy", text)

    def test_timer_schedule(self) -> None:
        text = read("deployment/systemd/auto-buddy-updater.timer")
        for entry in (
            "OnBootSec=3min",
            "OnUnitActiveSec=15min",
            "RandomizedDelaySec=60",
            "Persistent=true",
            "Unit=auto-buddy-updater.service",
        ):
            self.assertIn(entry, text)

    def test_installer_is_idempotent_and_never_starts_containers(self) -> None:
        text = read("deployment/install.sh")
        self.assertIn("set -euo pipefail", text)
        self.assertIn("--generate-secrets", text)
        self.assertIn("--skip-systemd", text)
        self.assertIn("existing", text.lower())
        self.assertIn("systemctl daemon-reload", text)
        self.assertNotRegex(text, r"(?m)^\s*docker\s+compose\s+(up|start|run)\b")
        self.assertIn("No containers were started", text)
        self.assertIn('"$REPO_ROOT/deployment/README.md"', text)
        self.assertIn('chmod 0700 "$SECRETS_DIR"', text)
        self.assertIn('chmod 0444 "$SECRETS_DIR/$file"', text)
        self.assertNotIn('chmod 0600 "$SECRETS_DIR/$file"', text)


class DocumentationTests(unittest.TestCase):
    def test_readme_documents_fixed_layout_and_limits(self) -> None:
        text = read("deployment/README.md")
        for value in (
            "/opt/auto-buddy",
            "/etc/auto-buddy/secrets",
            "/etc/auto-buddy/updater.json",
            "/var/lib/auto-buddy/updater",
            "/var/backups/auto-buddy",
            "/usr/local/lib/auto-buddy/auto_buddy_updater.py",
            "cosign",
            "v1.7.0",
            "growth partial-result",
            "ALLOWED_HOSTS=portal.example.com,127.0.0.1",
            "bind-mounted",
            "root-owned `0700`",
            "files `0444`",
            "root-owned mode `0700`",
            "read-only mode `0444`",
            "Docker image ID must exactly equal",
            "`web` and `worker` containers",
            "never the upstream gitlink SHA",
        ):
            self.assertIn(value, text)
        flattened = " ".join(text.lower().split())
        self.assertRegex(flattened, r"(never|not).{0,40}(replace|overwrite).{0,40}updater")

    def test_release_checklist_covers_gates_and_rollback(self) -> None:
        text = read("deployment/RELEASE_CHECKLIST.md")
        for value in (
            "release-manifest.json",
            "stable",
            "latest",
            "backward compatible",
            "manual-required.json",
            "--retry-failed",
            "cosign",
        ):
            self.assertIn(value, text)

    def test_runtime_env_example_has_required_non_secret_settings(self) -> None:
        text = read("deployment/runtime.env.example")
        for key in (
            "APP_DOMAIN=",
            "ALLOWED_HOSTS=",
            "CSRF_TRUSTED_ORIGINS=",
            "DATABASE_HOST=",
            "DATABASE_NAME=",
            "DATABASE_USER=",
            "DJANGO_DEBUG=false",
            "AUTH_TEMP_ROOT=",
            "WORKBUDDY_SCRIPT=",
            "ALLOWED_HOSTS=portal.example.com,127.0.0.1",
        ):
            self.assertIn(key, text)
        self.assertNotIn("SECRET_KEY=", text)
        self.assertNotIn("PASSWORD=", text)


class UpdaterExampleTests(unittest.TestCase):
    def test_fixed_paths_and_strict_cosign_identity(self) -> None:
        data = json.loads(read("deployment/updater.example.json"))
        self.assertEqual(data["project_directory"], "/opt/auto-buddy")
        self.assertEqual(data["compose_file"], "/opt/auto-buddy/compose.yaml")
        self.assertEqual(data["runtime_env_file"], "/etc/auto-buddy/runtime.env")
        self.assertEqual(data["secrets_dir"], "/etc/auto-buddy/secrets")
        self.assertEqual(data["state_dir"], "/var/lib/auto-buddy/updater")
        self.assertEqual(data["backup_dir"], "/var/backups/auto-buddy")
        self.assertEqual(data["cosign_issuer"], "https://token.actions.githubusercontent.com")
        for key in ("cosign_identity_regexp", "cosign_identity_subject_regexp"):
            self.assertTrue(data[key].startswith("^"))
            self.assertTrue(data[key].endswith("$"))
            self.assertIn("release\\.yml@refs/heads/main", data[key])
        self.assertTrue(data["require_asset_digest"])

    def test_schema_covers_manifest_contract(self) -> None:
        schema = json.loads(read("deployment/release-manifest.schema.json"))
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertFalse(schema["additionalProperties"])
        for field in ("schema_version", "version", "created_at", "commit", "image", "migrations", "upstream"):
            self.assertIn(field, schema["required"])
        self.assertEqual(schema["properties"]["image"]["properties"]["platform"]["const"], "linux/amd64")
        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)

    def test_secret_examples_are_placeholders_only(self) -> None:
        for name in ("django_key", "credential_keys", "db_password"):
            text = read(f"deployment/secrets/{name}.example")
            self.assertIn("REPLACE_WITH", text)
            self.assertNotIn("BEGIN PRIVATE KEY", text)


class WorkflowTests(unittest.TestCase):
    def test_ci_runs_all_required_suites(self) -> None:
        workflow = load_yaml(".github/workflows/ci.yml")
        self.assertIn("workflow_call", triggers(workflow))
        self.assertIn("pull_request", triggers(workflow))
        self.assertEqual(workflow["jobs"]["test"]["runs-on"], "ubuntu-22.04")
        self.assertEqual(workflow["jobs"]["test"]["services"]["postgres"]["image"], "postgres:16")
        text = read(".github/workflows/ci.yml")
        for command in (
            "manage.py test tests --noinput",
            "manage.py makemigrations --check --dry-run",
            "manage.py check --deploy",
            "tests_devops/requirements.txt",
            "python -m ruff check autobuddy portal tests scripts tests_devops deployment",
            "docker compose",
            "up -d --no-build db migrate web worker",
            "ps --all --quiet migrate",
            "{{.State.ExitCode}}",
            "path.chmod(0o444)",
            "root.mkdir(mode=0o700)",
            "container can read all file secrets",
            "trivy-action@v0.36.0",
            "workbuddy-auto-signin",
            "python -m unittest discover -s tests",
        ):
            self.assertIn(command, text)
        self.assertIn("AUTOBUDDY_TESTING", text)
        self.assertIn("TEST_DATABASE_HOST", text)

    def test_release_builds_signs_and_promotes_only_after_verification(self) -> None:
        workflow = load_yaml(".github/workflows/release.yml")
        self.assertIn("workflow_dispatch", triggers(workflow))
        self.assertIn("workflow_call", triggers(workflow))
        self.assertIn("push", triggers(workflow))
        self.assertIn("main", triggers(workflow)["push"]["branches"])
        text = read(".github/workflows/release.yml")
        self.assertIn("platforms: linux/amd64", text)
        self.assertIn("cosign sign", text)
        self.assertIn("cosign verify", text)
        self.assertIn("--certificate-identity-regexp", text)
        self.assertIn("https://token.actions.githubusercontent.com", text)
        self.assertIn("docker buildx imagetools create", text)
        self.assertIn("release-manifest.json", text)
        self.assertIn("git ls-tree HEAD workbuddy-auto-signin", text)
        self.assertLess(text.index("cosign verify"), text.index("docker buildx imagetools create"))
        self.assertLess(text.index("docker buildx imagetools create"), text.index("gh release create"))
        self.assertIn("stable", text)
        self.assertIn("latest", text)

    def test_release_promotion_checks_actual_manifest_bytes(self) -> None:
        workflow = load_yaml(".github/workflows/release.yml")
        step = next(item for item in workflow["jobs"]["publish"]["steps"]
                    if item.get("name") == "Validate manifest and promotion inputs")
        self.assertNotIn("hashFiles", json.dumps(step))
        code = step["run"].split("python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        expected_version = "v1.2.3"
        expected_digest = "sha256:" + "a" * 64
        for defect in (None, "checksum", "version", "image"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                manifest = {"version": expected_version, "image": {"digest": expected_digest}}
                if defect == "version":
                    manifest["version"] = "v1.2.4"
                if defect == "image":
                    manifest["image"]["digest"] = "sha256:" + "b" * 64
                raw = json.dumps(manifest).encode()
                root = Path(directory)
                (root / "release-manifest.json").write_bytes(raw)
                checksum = "0" * 64 if defect == "checksum" else hashlib.sha256(raw).hexdigest()
                (root / "manifest.sha256").write_text(checksum + "\n", encoding="utf-8")
                with contextlib.chdir(root), patch.dict(os.environ, {"VERSION": expected_version,
                                                                     "IMAGE_DIGEST": expected_digest}):
                    if defect:
                        with self.assertRaisesRegex(SystemExit, "mismatch"):
                            exec(compile(code, "release-promotion", "exec"), {})
                    else:
                        exec(compile(code, "release-promotion", "exec"), {})

    def test_upstream_sync_updates_only_the_gitlink(self) -> None:
        workflow = load_yaml(".github/workflows/upstream-sync.yml")
        trigger = triggers(workflow)
        self.assertIn("workflow_dispatch", trigger)
        self.assertIn("schedule", trigger)
        text = read(".github/workflows/upstream-sync.yml")
        for command in (
            "git ls-tree HEAD workbuddy-auto-signin",
            "git update-index --cacheinfo",
            "160000,${new},workbuddy-auto-signin",
            "fix(upstream)",
            "gh workflow run ci.yml",
            "gh run watch",
            "gh pr merge",
            "--auto",
            "gh pr view",
            "MERGED",
            "gh workflow run release.yml --ref main",
            "GITHUB_TOKEN",
            "GITHUB_RUN_ID",
            "GITHUB_RUN_ATTEMPT",
            "startswith($prefix)",
            "--event workflow_dispatch",
            '--commit "$parent_sha"',
            "steps.commit.outputs.sha",
        ):
            self.assertIn(command, text)
        self.assertNotIn("chore(upstream)", text)
        self.assertNotIn("gh pr list --head", text)
        self.assertNotIn("git add workbuddy-auto-signin", text)
        self.assertNotRegex(text, r"git\s+-C\s+workbuddy-auto-signin\s+(add|commit)")
        self.assertIn("actions: write", text)
        self.assertEqual(text.count("python -m unittest discover -s tests -v"), 1)
        self.assertLess(text.index("gh pr merge"), text.index("gh workflow run release.yml --ref main"))

        jobs = workflow["jobs"]
        self.assertEqual(jobs["verify"]["permissions"]["contents"], "read")
        self.assertEqual(jobs["sync"]["permissions"]["contents"], "write")
        self.assertEqual(jobs["sync"]["needs"], "verify")
        for job_name, job in (("verify", jobs["verify"]), ("sync", jobs["sync"])):
            checkouts = [step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/checkout")]
            self.assertEqual(len(checkouts), 1, job_name)
            with self.subTest(job=job_name):
                if job_name == "verify":
                    self.assertEqual(checkouts[0]["with"]["submodules"], "recursive")
                    self.assertIs(checkouts[0]["with"]["persist-credentials"], False)
                else:
                    self.assertNotIn("submodules", checkouts[0].get("with", {}))


class DockerIgnoreTests(unittest.TestCase):
    def test_required_build_inputs_are_not_excluded(self) -> None:
        text = read(".dockerignore")
        excluded = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
        for required in ("deployment/healthcheck.py", "scripts/docker-entrypoint.sh", "workbuddy-auto-signin/signin.py"):
            self.assertNotIn(required, excluded)
        self.assertIn("tests_devops", excluded)
        self.assertIn("secrets", excluded)
        self.assertIn(".ci-secrets", excluded)
        self.assertIn(".ci-runtime.env", excluded)
        for local_state in (".local", "artifacts", "staticfiles", "deployment/updater.env", "**/.env", "**/.env.*", "**/*.log"):
            self.assertIn(local_state, excluded)


if __name__ == "__main__":
    unittest.main()
