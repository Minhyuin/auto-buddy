#!/usr/bin/env python3
"""AutoBuddy host updater.

Runs on the Docker host (systemd timer) and performs a verified, reversible
release rollout:

  1. pick the newest stable GitHub release (or --tag)
  2. verify the release manifest checksum, image registry digest and cosign
     keyless signature against the pinned workflow identity
  3. pull the immutable image and cross-check its OCI labels
  4. pause scheduling, wait for the worker to drain, stop worker and web
  5. take a verified PostgreSQL dump
  6. run migrations with the new image, start web, wait for /health/
  7. start the worker and resume scheduling
  8. persist current/previous state so the next run can roll back

Safety rules:
  * never auto-downgrade a version;
  * never deploy a release whose migrations are not marked backward compatible
    while migrations are pending, unless --allow-unsafe-migrations is given;
  * a failed version is recorded and skipped on later timer runs until
    --retry-failed or --clear-failed;
  * the database is never restored automatically. Backups are retained for
    manual recovery only;
  * the updater never modifies its own file or the checked-out release tree.

The script is standard-library only and targets the Python shipped with the
Linux host (3.10+). It is intentionally not a general-purpose deployment tool.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

try:  # POSIX only; the updater cannot run on Windows.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    fcntl = None  # type: ignore[assignment]

LOG = logging.getLogger("auto_buddy_updater")

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2
EXIT_LOCKED = 3
EXIT_MANUAL = 5

VERSION_RE = re.compile(r"^v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
IMAGE_REPOSITORY_RE = re.compile(r"^ghcr\.io/[a-z0-9]+(?:[._-][a-z0-9]+)*/[a-z0-9]+(?:[._-][a-z0-9]+)*$")
MAX_HTTP_BODY = 4 * 1024 * 1024
MAX_MANIFEST_BODY = 2 * 1024 * 1024
ALLOWED_ISSUER = "https://token.actions.githubusercontent.com"
MANIFEST_ASSET = "release-manifest.json"
CHECKSUM_IN_RELEASE_RE = re.compile(r"Release-Manifest-SHA256:\s*([0-9a-f]{64})", re.IGNORECASE)


class UpdaterError(Exception):
    """Base class for expected, reported failures."""

    exit_code = EXIT_FAILURE


class ConfigError(UpdaterError):
    exit_code = EXIT_CONFIG


class LockError(UpdaterError):
    exit_code = EXIT_LOCKED


class VerificationError(UpdaterError):
    pass


class DeployError(UpdaterError):
    pass


class ManualApprovalRequired(UpdaterError):
    exit_code = EXIT_MANUAL


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @property
    def tag(self) -> str:
        return f"v{self.major}.{self.minor}.{self.patch}"

    @classmethod
    def parse(cls, value: str) -> "Version":
        match = VERSION_RE.match(value.strip())
        if not match:
            raise ConfigError(f"not a stable semantic version tag: {value!r}")
        major, minor, patch = (int(part) for part in value.strip().lstrip("v").split("."))
        return cls(major, minor, patch)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: bytes = b""
    stderr: str = ""

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandRunner:
    """Small subprocess wrapper; injectable so deployment logic is testable."""

    def run(
        self,
        args: Sequence[str],
        *,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
        timeout: float = 900,
    ) -> CommandResult:
        command = [str(part) for part in args]
        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd) if cwd else None,
                env=env,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(command, 124, b"", f"timed out after {timeout:g}s")
        except FileNotFoundError as exc:
            raise ConfigError(f"executable not found: {command[0]}") from exc
        except OSError as exc:
            raise ConfigError(f"cannot execute {command[0]}: {exc}") from exc
        return CommandResult(
            command,
            completed.returncode,
            completed.stdout or b"",
            (completed.stderr or b"").decode("utf-8", "replace"),
        )


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str = ""

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        for key, value in self.headers.items():
            if key.lower() == wanted:
                return value
        return None

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VerificationError(f"response from {self.url} is not JSON: {exc}") from exc


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class HttpClient:
    """urllib-based client with bounded bodies and optional redirect following."""

    def __init__(self, timeout: float = 30.0, user_agent: str = "auto-buddy-updater/1"):
        self.timeout = timeout
        self.user_agent = user_agent
        context = ssl.create_default_context()
        self._opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))
        self._no_redirect = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context),
            _NoRedirect,
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        allow_redirects: bool = True,
    ) -> HttpResponse:
        request_headers = {"User-Agent": self.user_agent}
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(
            url,
            data=body,
            headers=request_headers,
            method=method.upper(),
        )
        opener = self._opener if allow_redirects else self._no_redirect
        try:
            response = opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            payload = exc.read(MAX_HTTP_BODY) if exc.fp else b""
            return HttpResponse(exc.code, dict(exc.headers), payload, url)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise VerificationError(f"HTTP {method} {url} failed: {exc}") from exc
        with response:
            payload = response.read(MAX_HTTP_BODY)
            return HttpResponse(response.status, dict(response.headers), payload, response.geturl())


class FileLock:
    """Non-blocking flock around a state file. Raises LockError if held."""

    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def acquire(self) -> "FileLock":
        if fcntl is None:
            raise ConfigError("file locking requires POSIX fcntl; run the updater on Linux")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._handle.close()
            self._handle = None
            raise LockError(f"another updater is running (lock {self.path})") from exc
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(f"{os.getpid()}\n")
        self._handle.flush()
        return self

    def release(self) -> None:
        if self._handle is not None:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._handle.close()
                self._handle = None

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.release()


class StateStore:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir

    def path(self, name: str) -> Path:
        return self.state_dir / name

    def read(self, name: str) -> dict | None:
        path = self.path(name)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"cannot read state {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"state {path} is not a JSON object")
        return data

    def write(self, name: str, data: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.path(name)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def delete(self, name: str) -> None:
        try:
            self.path(name).unlink()
        except FileNotFoundError:
            pass


@dataclass
class Config:
    repository: str
    image_repository: str
    compose_file: Path
    project_directory: Path
    project_name: str
    runtime_env_file: Path
    secrets_dir: Path
    state_dir: Path
    backup_dir: Path
    cosign_identity_regexp: str
    health_url: str = "http://127.0.0.1:8080/health/"
    health_retries: int = 30
    health_interval_seconds: float = 2.0
    drain_seconds: int = 650
    drain_poll_seconds: float = 5.0
    stop_web_grace_seconds: int = 30
    stop_worker_grace_seconds: int = 650
    backup_min_tables: int = 5
    backup_retention: int = 5
    cosign_identity_subject_regexp: str = ""
    cosign_issuer: str = ALLOWED_ISSUER
    release_asset: str = MANIFEST_ASSET
    github_api_url: str = "https://api.github.com"
    github_token_file: Path | None = None
    github_username: str | None = None
    require_asset_digest: bool = True
    docker_bin: str = "docker"
    cosign_bin: str = "cosign"
    command_timeout_seconds: int = 900

    @classmethod
    def load(cls, path: Path) -> "Config":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigError(f"config file not found: {path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"cannot read config {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("config must be a JSON object")

        known = {item.name for item in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        unknown = set(raw) - known
        if unknown:
            raise ConfigError(f"unknown config keys: {', '.join(sorted(unknown))}")

        repository = _required_str(raw, "repository")
        if not REPOSITORY_RE.match(repository):
            raise ConfigError("repository must be GitHub owner/name")
        cosign_identity_regexp = _required_str(raw, "cosign_identity_regexp")
        if not cosign_identity_regexp.startswith("^") or not cosign_identity_regexp.endswith("$"):
            raise ConfigError("cosign_identity_regexp must be anchored with ^...$")

        project_directory = _absolute_path(raw.get("project_directory", "/opt/auto-buddy"), "project_directory")
        compose_file = _absolute_path(
            raw.get("compose_file", str(project_directory / "compose.yaml")),
            "compose_file",
        )
        runtime_env_file = _absolute_path(
            raw.get("runtime_env_file", "/etc/auto-buddy/runtime.env"),
            "runtime_env_file",
        )
        secrets_dir = _absolute_path(raw.get("secrets_dir", "/etc/auto-buddy/secrets"), "secrets_dir")
        state_dir = _absolute_path(raw.get("state_dir", "/var/lib/auto-buddy/updater"), "state_dir")
        backup_dir = _absolute_path(raw.get("backup_dir", "/var/backups/auto-buddy"), "backup_dir")

        image_repository = raw.get("image_repository") or f"ghcr.io/{repository.lower()}"
        if not isinstance(image_repository, str) or not IMAGE_REPOSITORY_RE.match(image_repository):
            raise ConfigError("image_repository must look like ghcr.io/owner/name (lowercase)")
        if not image_repository.startswith("ghcr.io/"):
            raise ConfigError("only ghcr.io images are supported")

        issuer = raw.get("cosign_issuer", ALLOWED_ISSUER)
        if issuer != ALLOWED_ISSUER:
            raise ConfigError(f"cosign_issuer must be exactly {ALLOWED_ISSUER}")

        subject_regexp = raw.get("cosign_identity_subject_regexp", cosign_identity_regexp)
        if not isinstance(subject_regexp, str) or not subject_regexp.startswith("^") or not subject_regexp.endswith("$"):
            raise ConfigError("cosign_identity_subject_regexp must be anchored with ^...$")

        token_file = raw.get("github_token_file")
        if token_file is not None:
            token_file = _absolute_path(token_file, "github_token_file")

        config = cls(
            repository=repository,
            image_repository=image_repository,
            compose_file=compose_file,
            project_directory=project_directory,
            project_name=str(raw.get("project_name", "auto-buddy")),
            runtime_env_file=runtime_env_file,
            secrets_dir=secrets_dir,
            state_dir=state_dir,
            backup_dir=backup_dir,
            cosign_identity_regexp=cosign_identity_regexp,
            health_url=str(raw.get("health_url", "http://127.0.0.1:8080/health/")),
            health_retries=_positive_int(raw, "health_retries", 30),
            health_interval_seconds=_positive_float(raw, "health_interval_seconds", 2.0),
            drain_seconds=_nonnegative_int(raw, "drain_seconds", 650),
            drain_poll_seconds=_positive_float(raw, "drain_poll_seconds", 5.0),
            stop_web_grace_seconds=_nonnegative_int(raw, "stop_web_grace_seconds", 30),
            stop_worker_grace_seconds=_nonnegative_int(raw, "stop_worker_grace_seconds", 650),
            backup_min_tables=_positive_int(raw, "backup_min_tables", 5),
            backup_retention=_positive_int(raw, "backup_retention", 5),
            cosign_identity_subject_regexp=subject_regexp,
            cosign_issuer=issuer,
            release_asset=str(raw.get("release_asset", MANIFEST_ASSET)),
            github_api_url=str(raw.get("github_api_url", "https://api.github.com")).rstrip("/"),
            github_token_file=token_file,
            github_username=raw.get("github_username"),
            require_asset_digest=bool(raw.get("require_asset_digest", True)),
            docker_bin=str(raw.get("docker_bin", "docker")),
            cosign_bin=str(raw.get("cosign_bin", "cosign")),
            command_timeout_seconds=_positive_int(raw, "command_timeout_seconds", 900),
        )
        if not config.health_url.startswith(("http://", "https://")):
            raise ConfigError("health_url must be http(s)")
        if config.release_asset != Path(config.release_asset).name:
            raise ConfigError("release_asset must be a plain file name")
        if config.github_username is not None and not isinstance(config.github_username, str):
            raise ConfigError("github_username must be a string")
        return config

    def public_dict(self) -> dict:
        return {
            "repository": self.repository,
            "image_repository": self.image_repository,
            "project_directory": str(self.project_directory),
            "compose_file": str(self.compose_file),
            "runtime_env_file": str(self.runtime_env_file),
            "secrets_dir": str(self.secrets_dir),
            "state_dir": str(self.state_dir),
            "backup_dir": str(self.backup_dir),
            "health_url": self.health_url,
        }


def _required_str(raw: dict, key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} is required and must be a non-empty string")
    return value.strip()


def _absolute_path(value: object, key: str) -> Path:
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise ConfigError(f"{key} must be a non-empty path")
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        raise ConfigError(f"{key} must be an absolute path: {path}")
    return path


def _positive_int(raw: dict, key: str, default: int) -> int:
    value = raw.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{key} must be a positive integer")
    return value


def _nonnegative_int(raw: dict, key: str, default: int) -> int:
    value = raw.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ConfigError(f"{key} must be a non-negative integer")
    return value


def _positive_float(raw: dict, key: str, default: float) -> float:
    value = raw.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{key} must be a positive number")
    return float(value)


@dataclass(frozen=True)
class Release:
    tag: str
    version: Version
    body: str
    html_url: str
    assets: tuple[dict, ...]
    draft: bool
    prerelease: bool

    def asset(self, name: str) -> dict | None:
        for asset in self.assets:
            if asset.get("name") == name:
                return asset
        return None


@dataclass
class Target:
    release: Release
    manifest: dict
    digest: str
    image: str


def _release_from_payload(payload: dict) -> Release:
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not VERSION_RE.match(tag):
        raise VerificationError(f"release has no stable semantic version tag: {tag!r}")
    assets = payload.get("assets") or ()
    return Release(
        tag=tag,
        version=Version.parse(tag),
        body=payload.get("body") or "",
        html_url=payload.get("html_url") or "",
        assets=tuple(asset for asset in assets if isinstance(asset, dict)),
        draft=bool(payload.get("draft")),
        prerelease=bool(payload.get("prerelease")),
    )


def select_release(releases: Iterable[Release], current: Version | None, requested: str | None) -> Release | None:
    stable = [release for release in releases if not release.draft and not release.prerelease]
    if requested:
        for release in stable:
            if release.tag == requested:
                if current is not None and release.version < current:
                    raise ConfigError(f"refusing downgrade from {current.tag} to {release.tag}")
                return release
        raise ConfigError(f"release {requested} is not a stable, published release")
    candidates = [release for release in stable if current is None or release.version > current]
    return max(candidates, key=lambda release: release.version) if candidates else None


def validate_release_manifest(data: object, tag: str) -> dict:
    if not isinstance(data, dict):
        raise VerificationError("release manifest must be a JSON object")
    errors: list[str] = []
    if data.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if data.get("version") != tag:
        errors.append(f"version must equal {tag}")
    if not isinstance(data.get("commit"), str) or not COMMIT_RE.match(data["commit"]):
        errors.append("commit must be a 40-character lowercase SHA")
    image = data.get("image")
    if not isinstance(image, dict):
        errors.append("image must be an object")
    else:
        if not isinstance(image.get("repository"), str) or not IMAGE_REPOSITORY_RE.match(image["repository"]):
            errors.append("image.repository must be ghcr.io/owner/name")
        if not isinstance(image.get("digest"), str) or not DIGEST_RE.match(image["digest"]):
            errors.append("image.digest must be sha256:<64 hex>")
        if image.get("platform") != "linux/amd64":
            errors.append("image.platform must be linux/amd64")
        if "tag" in image and image.get("tag") != tag:
            errors.append(f"image.tag must equal {tag}")
    migrations = data.get("migrations")
    if not isinstance(migrations, dict):
        errors.append("migrations must be an object")
    else:
        for key in ("backward_compatible", "manual_approval_required"):
            if not isinstance(migrations.get(key), bool):
                errors.append(f"migrations.{key} must be a boolean")
    upstream = data.get("upstream")
    if not isinstance(upstream, dict):
        errors.append("upstream must be an object")
    else:
        if not isinstance(upstream.get("commit"), str) or not COMMIT_RE.match(upstream["commit"]):
            errors.append("upstream.commit must be a 40-character lowercase SHA")
    if errors:
        raise VerificationError("invalid release manifest: " + "; ".join(errors))
    return data


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _label(labels: dict, name: str) -> str | None:
    value = labels.get(name)
    return value if isinstance(value, str) else None


class GitHubClient:
    def __init__(self, http: HttpClient, api_url: str, repository: str, token_file: Path | None):
        self.http = http
        self.api_url = api_url.rstrip("/")
        self.repository = repository
        self.token_file = token_file

    def _token(self) -> str | None:
        if self.token_file is None:
            return None
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"cannot read github_token_file: {exc}") from exc
        if not token:
            raise ConfigError("github_token_file is empty")
        return token

    def _headers(self, accept: str) -> dict[str, str]:
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = self._token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _get(self, url: str) -> HttpResponse:
        response = self.http.request("GET", url, headers=self._headers("application/vnd.github+json"))
        if response.status == 404:
            return response
        if response.status != 200:
            raise VerificationError(f"GitHub API returned HTTP {response.status} for {url}")
        return response

    def list_releases(self) -> list[Release]:
        url = f"{self.api_url}/repos/{self.repository}/releases?per_page=100"
        response = self._get(url)
        if response.status == 404:
            raise ConfigError(f"GitHub repository not found: {self.repository}")
        payload = response.json()
        if not isinstance(payload, list):
            raise VerificationError("GitHub releases response is not a list")
        releases: list[Release] = []
        for item in payload:
            if isinstance(item, dict):
                try:
                    releases.append(_release_from_payload(item))
                except VerificationError:
                    continue  # Ignore tags that are not stable release tags.
        return releases

    def release_by_tag(self, tag: str) -> Release | None:
        if not VERSION_RE.match(tag):
            raise ConfigError(f"not a stable semantic version tag: {tag!r}")
        url = f"{self.api_url}/repos/{self.repository}/releases/tags/{urllib.parse.quote(tag)}"
        response = self._get(url)
        if response.status == 404:
            return None
        payload = response.json()
        if not isinstance(payload, dict):
            raise VerificationError("GitHub release response is not an object")
        release = _release_from_payload(payload)
        if release.draft or release.prerelease:
            raise ConfigError(f"release {tag} is a draft or prerelease")
        return release


class RegistryVerifier:
    """Verify that an immutable GHCR digest resolves to exactly those bytes."""

    def __init__(self, http: HttpClient, repository: str, token_file: Path | None, username: str | None):
        self.http = http
        self.repository = repository
        self.token_file = token_file
        self.username = username

    def _basic_auth(self) -> dict[str, str]:
        if self.token_file is None:
            return {}
        if not self.username:
            raise ConfigError("github_username is required when github_token_file is set")
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"cannot read github_token_file: {exc}") from exc
        credentials = base64.b64encode(f"{self.username}:{token}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {credentials}"}

    def verify(self, digest: str) -> None:
        if not DIGEST_RE.match(digest):
            raise VerificationError(f"invalid image digest: {digest!r}")
        url = f"https://ghcr.io/v2/{self.repository}/manifests/{urllib.parse.quote(digest, safe=':')}"
        accept = ", ".join(
            (
                "application/vnd.oci.image.index.v1+json",
                "application/vnd.docker.distribution.manifest.list.v2+json",
                "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.docker.distribution.manifest.v2+json",
            )
        )
        headers = {"Accept": accept}
        response = self.http.request("GET", url, headers=headers)
        if response.status == 401:
            challenge = response.header("WWW-Authenticate") or ""
            token = self._fetch_token(challenge)
            headers["Authorization"] = f"Bearer {token}"
            response = self.http.request("GET", url, headers=headers)
        if response.status != 200:
            raise VerificationError(f"GHCR manifest request failed with HTTP {response.status}")
        body_digest = "sha256:" + _sha256_hex(response.body)
        reported = response.header("Docker-Content-Digest")
        if reported is None:
            raise VerificationError("GHCR response has no Docker-Content-Digest header")
        if reported.lower() != body_digest or reported.lower() != digest:
            raise VerificationError(
                f"GHCR digest mismatch: requested {digest}, header {reported}, body {body_digest}"
            )
        if len(response.body) == 0:
            raise VerificationError("GHCR returned an empty manifest")

    def _fetch_token(self, challenge: str) -> str:
        match = re.search(r"Bearer\s+(.+)", challenge, re.IGNORECASE)
        if not match:
            raise VerificationError("GHCR did not send a Bearer challenge")
        params = {
            key.lower(): value
            for key, value in re.findall(r'(\w+)="([^"]*)"', match.group(1))
        }
        realm = params.get("realm")
        if not realm:
            raise VerificationError("GHCR Bearer challenge has no realm")
        parsed = urllib.parse.urlparse(realm)
        if parsed.scheme != "https" or parsed.hostname != "ghcr.io":
            raise VerificationError(f"refusing unexpected GHCR token realm: {realm}")
        query = {
            "service": params.get("service", "ghcr.io"),
            "scope": params.get("scope", f"repository:{self.repository}:pull"),
        }
        token_url = realm + ("&" if "?" in realm else "?") + urllib.parse.urlencode(query)
        response = self.http.request("GET", token_url, headers=self._basic_auth())
        if response.status != 200:
            raise VerificationError(f"GHCR token request failed with HTTP {response.status}")
        payload = response.json()
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise VerificationError("GHCR token response has no token")
        return token


class CosignVerifier:
    """Verify keyless cosign signatures with strict GitHub Actions identity."""

    def __init__(
        self,
        runner: CommandRunner,
        cosign_bin: str,
        identity_regexp: str,
        subject_regexp: str,
        issuer: str,
    ):
        self.runner = runner
        self.cosign_bin = cosign_bin
        self.identity_regexp = identity_regexp
        self.subject_regexp = subject_regexp
        self.issuer = issuer

    def verify(self, image: str, digest: str) -> None:
        command = [
            self.cosign_bin,
            "verify",
            "--output",
            "json",
            "--certificate-identity-regexp",
            self.identity_regexp,
            "--certificate-oidc-issuer",
            self.issuer,
            f"{image}@{digest}",
        ]
        result = self.runner.run(command, timeout=120)
        if result.returncode != 0:
            detail = (result.stderr or result.text).strip()[:1000]
            raise VerificationError(f"cosign verification failed: {detail}")
        try:
            entries = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise VerificationError(f"cosign did not return JSON: {exc}") from exc
        if not isinstance(entries, list) or not entries:
            raise VerificationError("cosign returned no verification entries")
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            critical = entry.get("critical")
            optional = entry.get("optional")
            if not isinstance(critical, dict) or not isinstance(optional, dict):
                continue
            image_info = critical.get("image")
            if not isinstance(image_info, dict):
                continue
            if image_info.get("docker-manifest-digest") != digest:
                continue
            issuer = optional.get("Issuer") or optional.get("issuer")
            subject = optional.get("Subject") or optional.get("subject")
            if issuer != self.issuer:
                continue
            if not isinstance(subject, str) or not re.search(self.subject_regexp, subject):
                continue
            return
        raise VerificationError(
            "cosign verified a signature, but no entry matched the digest, issuer and subject policy"
        )


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


class Updater:
    def __init__(
        self,
        config: Config,
        *,
        runner: CommandRunner | None = None,
        http: HttpClient | None = None,
        state: StateStore | None = None,
        lock_factory: Callable[[Path], Any] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.runner = runner or CommandRunner()
        self.http = http or HttpClient()
        self.state = state or StateStore(config.state_dir)
        self.lock_factory = lock_factory
        self.monotonic = monotonic
        self.sleep = sleep
        self.github = GitHubClient(
            self.http,
            config.github_api_url,
            config.repository,
            config.github_token_file,
        )
        self.registry = RegistryVerifier(
            self.http,
            config.image_repository,
            config.github_token_file,
            config.github_username,
        )
        self.cosign = CosignVerifier(
            self.runner,
            config.cosign_bin,
            config.cosign_identity_regexp,
            config.cosign_identity_subject_regexp,
            config.cosign_issuer,
        )

    # ------------------------------------------------------------------
    # host / config checks
    # ------------------------------------------------------------------
    def ensure_dirs(self) -> None:
        for directory, mode in ((self.config.state_dir, 0o750), (self.config.backup_dir, 0o750)):
            try:
                directory.mkdir(parents=True, exist_ok=True)
                os.chmod(directory, mode)
            except OSError as exc:
                raise ConfigError(f"cannot create {directory}: {exc}") from exc

    def _secret_path(self, name: str) -> Path:
        return self.config.secrets_dir / name

    def _read_secret(self, name: str) -> bytes:
        path = self._secret_path(name)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ConfigError(f"cannot read secret {path}: {exc}") from exc
        if not data.strip():
            raise ConfigError(f"secret {path} is empty")
        return data

    def _check_paths(self) -> list[CheckResult]:
        checks: list[CheckResult] = []
        checks.append(
            CheckResult(
                "project directory",
                self.config.project_directory.is_dir(),
                str(self.config.project_directory),
            )
        )
        checks.append(
            CheckResult(
                "compose file",
                self.config.compose_file.is_file(),
                str(self.config.compose_file),
            )
        )
        checks.append(
            CheckResult(
                "runtime env file",
                self.config.runtime_env_file.is_file(),
                str(self.config.runtime_env_file),
            )
        )
        checks.append(
            CheckResult(
                "secrets directory",
                self.config.secrets_dir.is_dir(),
                str(self.config.secrets_dir),
            )
        )
        return checks

    def _check_secrets(self) -> list[CheckResult]:
        checks: list[CheckResult] = []
        try:
            key = self._read_secret("django_key").decode("utf-8").strip()
            checks.append(CheckResult("django_key", len(key) >= 50, f"{len(key)} characters"))
        except (UnicodeDecodeError, ConfigError) as exc:
            checks.append(CheckResult("django_key", False, str(exc)))
        try:
            raw = self._read_secret("credential_keys").decode("utf-8")
            mapping = json.loads(raw)
            valid = isinstance(mapping, dict) and bool(mapping)
            detail = "JSON object of key-id:base64 AES-256 keys"
            if valid:
                for key_id, encoded in mapping.items():
                    if not isinstance(key_id, str) or not re.fullmatch(r"[A-Za-z0-9]{1,32}", key_id):
                        valid, detail = False, f"invalid key id {key_id!r}"
                        break
                    if not isinstance(encoded, str):
                        valid, detail = False, f"key {key_id} is not a string"
                        break
                    try:
                        decoded = base64.b64decode(encoded, validate=True)
                    except (binascii.Error, ValueError):
                        valid, detail = False, f"key {key_id} is not valid base64"
                        break
                    if len(decoded) != 32:
                        valid, detail = False, f"key {key_id} is not 32 bytes"
                        break
            else:
                detail = "expected a non-empty JSON object"
            checks.append(CheckResult("credential_keys", valid, detail))
        except (UnicodeDecodeError, json.JSONDecodeError, ConfigError) as exc:
            checks.append(CheckResult("credential_keys", False, str(exc)))
        try:
            password = self._read_secret("db_password")
            checks.append(CheckResult("db_password", bool(password.strip()), "non-empty"))
        except ConfigError as exc:
            checks.append(CheckResult("db_password", False, str(exc)))
        return checks

    def _runtime_env(self) -> dict[str, str]:
        try:
            text = self.config.runtime_env_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read runtime env file: {exc}") from exc
        values: dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
        return values

    def _check_runtime_env(self) -> list[CheckResult]:
        checks: list[CheckResult] = []
        try:
            values = self._runtime_env()
        except ConfigError as exc:
            return [CheckResult("runtime env", False, str(exc))]
        required = (
            "APP_DOMAIN",
            "ALLOWED_HOSTS",
            "CSRF_TRUSTED_ORIGINS",
            "DATABASE_NAME",
            "DATABASE_USER",
            "APP_VERSION",
        )
        missing = [key for key in required if not values.get(key)]
        checks.append(
            CheckResult(
                "runtime env keys",
                not missing,
                "all required keys present" if not missing else f"missing: {', '.join(missing)}",
            )
        )
        debug = values.get("DJANGO_DEBUG", "").lower()
        checks.append(CheckResult("DJANGO_DEBUG is false", debug in ("false", "0"), debug or "(unset)"))
        return checks

    def _check_dirs(self) -> list[CheckResult]:
        checks: list[CheckResult] = []
        for name, directory in (("state_dir", self.config.state_dir), ("backup_dir", self.config.backup_dir)):
            try:
                directory.mkdir(parents=True, exist_ok=True)
                probe = directory / ".write-probe"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
                checks.append(CheckResult(name, True, str(directory)))
            except OSError as exc:
                checks.append(CheckResult(name, False, f"{directory}: {exc}"))
        return checks

    def _check_binaries(self) -> list[CheckResult]:
        checks: list[CheckResult] = []
        for name, binary in (("docker", self.config.docker_bin), ("cosign", self.config.cosign_bin)):
            found = shutil.which(binary)
            checks.append(CheckResult(f"{name} executable", bool(found), found or f"not found: {binary}"))
        if shutil.which(self.config.docker_bin):
            server = self.runner.run([self.config.docker_bin, "version", "--format", "{{.Server.Version}}"], timeout=30)
            checks.append(
                CheckResult(
                    "docker daemon",
                    server.ok and bool(server.text.strip()),
                    server.text.strip() if server.ok else server.stderr.strip()[:300],
                )
            )
            compose = self.runner.run([self.config.docker_bin, "compose", "version", "--short"], timeout=30)
            checks.append(
                CheckResult(
                    "docker compose plugin",
                    compose.ok,
                    compose.text.strip() if compose.ok else compose.stderr.strip()[:300],
                )
            )
        if shutil.which(self.config.cosign_bin):
            version = self.runner.run([self.config.cosign_bin, "version"], timeout=30)
            checks.append(
                CheckResult(
                    "cosign executable works",
                    version.ok,
                    version.text.strip()[:200] if version.ok else version.stderr.strip()[:300],
                )
            )
        return checks

    def _compose_environment(
        self,
        image: str,
        *,
        version: str = "0.0.0",
        revision: str = "0" * 40,
        upstream: str = "0" * 40,
        backward_compatible: bool = True,
        manual_approval_required: bool = False,
    ) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "APP_IMAGE": image,
                "APP_VERSION": version,
                "VCS_REF": revision,
                "UPSTREAM_COMMIT": upstream,
                "MIGRATIONS_BACKWARD_COMPATIBLE": str(backward_compatible).lower(),
                "MANUAL_APPROVAL_REQUIRED": str(manual_approval_required).lower(),
                "RUNTIME_ENV_FILE": str(self.config.runtime_env_file),
                "SECRETS_DIR": str(self.config.secrets_dir),
                "COMPOSE_PROJECT_NAME": self.config.project_name,
            }
        )
        return env

    def _compose(
        self,
        args: Sequence[str],
        *,
        image: str,
        version: str = "0.0.0",
        revision: str = "0" * 40,
        upstream: str = "0" * 40,
        backward_compatible: bool = True,
        manual_approval_required: bool = False,
        timeout: float = 300,
    ) -> CommandResult:
        command = [
            self.config.docker_bin,
            "compose",
            "--project-directory",
            str(self.config.project_directory),
            "--project-name",
            self.config.project_name,
            "--env-file",
            str(self.config.runtime_env_file),
            "-f",
            str(self.config.compose_file),
            *args,
        ]
        return self.runner.run(
            command,
            env=self._compose_environment(
                image,
                version=version,
                revision=revision,
                upstream=upstream,
                backward_compatible=backward_compatible,
                manual_approval_required=manual_approval_required,
            ),
            timeout=timeout,
        )

    def _require(self, result: CommandResult, action: str, error: type[UpdaterError] = DeployError) -> None:
        if result.ok:
            return
        detail = (result.stderr or result.text).strip()[:1000]
        raise error(f"{action} failed (exit {result.returncode})" + (f": {detail}" if detail else ""))

    def _placeholder_image(self) -> str:
        return f"{self.config.image_repository}@sha256:{'0' * 64}"

    def _check_compose_config(self) -> CheckResult:
        result = self._compose(
            ["config", "--quiet"],
            image=self._placeholder_image(),
            timeout=60,
        )
        return CheckResult(
            "compose config",
            result.ok,
            "valid" if result.ok else (result.stderr or result.text).strip()[:500],
        )

    def _service_running(self, service: str) -> bool:
        result = self._compose(
            ["ps", "--status", "running", "--quiet", service],
            image=self._placeholder_image(),
            timeout=60,
        )
        return result.ok and bool(result.text.strip())

    def _db_ready(self) -> bool:
        result = self._compose(
            ["exec", "-T", "db", "sh", "-c", "pg_isready -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\""],
            image=self._placeholder_image(),
            timeout=60,
        )
        return result.ok

    def check(self) -> list[CheckResult]:
        checks: list[CheckResult] = []
        checks += self._check_paths()
        checks += self._check_secrets()
        checks += self._check_runtime_env()
        checks += self._check_dirs()
        checks += self._check_binaries()
        if all(check.ok for check in checks):
            checks.append(self._check_compose_config())
        for service in ("db", "web", "worker", "caddy"):
            try:
                running = self._service_running(service)
            except UpdaterError as exc:
                checks.append(CheckResult(f"{service} running", False, str(exc)))
                continue
            checks.append(CheckResult(f"{service} running", running, "running" if running else "not running"))
        try:
            ready = self._db_ready()
        except UpdaterError as exc:
            checks.append(CheckResult("database ready", False, str(exc)))
        else:
            checks.append(CheckResult("database ready", ready, "pg_isready"))
        return checks

    def preflight(self) -> None:
        checks = self._check_paths() + self._check_secrets() + self._check_runtime_env() + self._check_dirs()
        checks += self._check_binaries()
        failures = [check for check in checks if not check.ok]
        if not failures:
            config_check = self._check_compose_config()
            checks.append(config_check)
            if not config_check.ok:
                failures.append(config_check)
        try:
            missing_services = [
                service for service in ("db", "web", "worker", "caddy") if not self._service_running(service)
            ]
        except UpdaterError as exc:
            failures.append(CheckResult("services running", False, str(exc)))
        else:
            if missing_services:
                failures.append(
                    CheckResult("services running", False, f"not running: {', '.join(missing_services)}")
                )
        try:
            ready = self._db_ready()
        except UpdaterError as exc:
            failures.append(CheckResult("database ready", False, str(exc)))
        else:
            if not ready:
                failures.append(CheckResult("database ready", False, "pg_isready failed"))
        if failures:
            detail = "; ".join(f"{check.name}: {check.detail}" for check in failures)
            raise ConfigError(f"host preflight failed: {detail}")

    # ------------------------------------------------------------------
    # release selection and verification
    # ------------------------------------------------------------------
    def _release_for(self, requested: str | None, current: Version | None) -> Release | None:
        if requested:
            release = self.github.release_by_tag(requested)
            if release is None:
                raise ConfigError(f"release {requested} does not exist")
            if current is not None and release.version < current:
                raise ConfigError(f"refusing downgrade from {current.tag} to {release.tag}")
            return release
        return select_release(self.github.list_releases(), current, None)

    def _download_manifest(self, release: Release) -> dict:
        asset = release.asset(self.config.release_asset)
        if asset is None:
            raise VerificationError(f"release {release.tag} has no {self.config.release_asset} asset")
        size = asset.get("size")
        if isinstance(size, int) and size > MAX_MANIFEST_BODY:
            raise VerificationError(f"release manifest is too large: {size} bytes")
        browser_url = asset.get("browser_download_url")
        if not isinstance(browser_url, str) or not browser_url.startswith("https://"):
            raise VerificationError("release manifest asset has no HTTPS browser_download_url")
        token = self.github._token()
        headers = {"Accept": "application/octet-stream"}
        if token and isinstance(asset.get("url"), str):
            headers["Authorization"] = f"Bearer {token}"
            response = self.http.request(
                "GET",
                asset["url"],
                headers=headers,
                allow_redirects=False,
            )
            if response.status in (301, 302, 303, 307, 308):
                location = response.header("Location")
                if not location or not location.startswith("https://"):
                    raise VerificationError("GitHub asset redirect is not HTTPS")
                response = self.http.request(
                    "GET",
                    location,
                    headers={"Accept": "application/octet-stream"},
                    allow_redirects=True,
                )
        else:
            response = self.http.request("GET", browser_url, headers=headers, allow_redirects=True)
        if response.status == 404:
            raise VerificationError("release manifest asset download returned HTTP 404")
        if response.status != 200:
            raise VerificationError(f"release manifest download returned HTTP {response.status}")
        if len(response.body) > MAX_MANIFEST_BODY:
            raise VerificationError("release manifest exceeds size limit")
        expected_digest = asset.get("digest")
        expected_hex: str | None = None
        if isinstance(expected_digest, str) and expected_digest.startswith("sha256:"):
            expected_hex = expected_digest.split(":", 1)[1]
        if expected_hex is None:
            match = CHECKSUM_IN_RELEASE_RE.search(release.body)
            if match:
                expected_hex = match.group(1)
        if expected_hex is None:
            if self.config.require_asset_digest:
                raise VerificationError(
                    "release manifest asset has no digest and release notes have no Release-Manifest-SHA256"
                )
            LOG.warning("release manifest checksum is not published; continuing because require_asset_digest=false")
        elif not hmac_compare(_sha256_hex(response.body), expected_hex):
            raise VerificationError("release manifest checksum does not match")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VerificationError(f"release manifest is not JSON: {exc}") from exc
        return validate_release_manifest(payload, release.tag)

    def _pull(self, image: str) -> None:
        result = self.runner.run(
            [self.config.docker_bin, "pull", image],
            timeout=max(600, self.config.command_timeout_seconds),
        )
        self._require(result, f"pulling {image}", VerificationError)

    def _expected_labels(self, release: Release, manifest: dict) -> dict[str, str]:
        return {
            "org.opencontainers.image.version": release.tag,
            "org.opencontainers.image.revision": manifest["commit"],
            "org.auto-buddy.upstream-commit": manifest["upstream"]["commit"],
            "org.auto-buddy.migrations-backward-compatible": str(
                manifest["migrations"]["backward_compatible"]
            ).lower(),
            "org.auto-buddy.manual-approval-required": str(
                manifest["migrations"]["manual_approval_required"]
            ).lower(),
        }

    def _verify_labels(self, image: str, release: Release, manifest: dict, allow_manual: bool) -> None:
        result = self.runner.run(
            [
                self.config.docker_bin,
                "image",
                "inspect",
                image,
                "--format",
                "{{json .Config.Labels}}",
            ],
            timeout=60,
        )
        self._require(result, "inspecting image labels", VerificationError)
        try:
            labels = json.loads(result.text or "null")
        except json.JSONDecodeError as exc:
            raise VerificationError(f"image labels are not JSON: {exc}") from exc
        if not isinstance(labels, dict):
            raise VerificationError("image has no OCI labels")
        required = self._expected_labels(release, manifest)
        for name, expected in required.items():
            actual = _label(labels, name)
            if actual != expected:
                raise VerificationError(f"image label {name} is {actual!r}, expected {expected!r}")
        if required["org.auto-buddy.manual-approval-required"] == "true" and not allow_manual:
            raise ManualApprovalRequired(
                f"release {release.tag} requires manual approval (image label); pass --allow-unsafe-migrations"
            )

    def _docker_json(self, args: list[str], action: str) -> dict:
        result = self.runner.run([self.config.docker_bin, *args], timeout=60)
        self._require(result, action, VerificationError)
        try:
            payload = json.loads(result.text or "null")
        except json.JSONDecodeError as exc:
            raise VerificationError(f"{action} returned invalid JSON: {exc}") from exc
        if isinstance(payload, list):
            if len(payload) != 1 or not isinstance(payload[0], dict):
                raise VerificationError(f"{action} returned {len(payload)} objects; expected exactly one")
            payload = payload[0]
        if not isinstance(payload, dict):
            raise VerificationError(f"{action} did not return an object")
        return payload

    def _running_container_id(self, service: str) -> str:
        result = self._compose(
            ["ps", "--status", "running", "--quiet", service],
            image=self._placeholder_image(),
            timeout=60,
        )
        self._require(result, f"locating the running {service} container", VerificationError)
        ids = [line.strip() for line in result.text.splitlines() if line.strip()]
        if len(ids) != 1:
            raise VerificationError(
                f"expected exactly one running {service} container, found {len(ids)}"
            )
        return ids[0]

    def _verify_live_target(self, target: Target) -> None:
        """Require the running web/worker containers to be the verified image ID."""
        image = self._docker_json(
            ["image", "inspect", target.image],
            "inspecting the target image",
        )
        image_id = image.get("Id")
        if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
            raise VerificationError(f"target image has an unexpected ID: {image_id!r}")
        image_config = image.get("Config")
        image_labels = image_config.get("Labels") if isinstance(image_config, dict) else None
        if not isinstance(image_labels, dict):
            raise VerificationError("target image has no labels")
        required = self._expected_labels(target.release, target.manifest)
        for name, expected in required.items():
            actual = _label(image_labels, name)
            if actual != expected:
                raise VerificationError(f"target image label {name} is {actual!r}, expected {expected!r}")

        for service in ("web", "worker"):
            container_id = self._running_container_id(service)
            container = self._docker_json(
                ["inspect", container_id],
                f"inspecting the running {service} container",
            )
            actual_image = container.get("Image")
            if actual_image != image_id:
                raise VerificationError(
                    f"running {service} container uses image {actual_image!r}, "
                    f"but the verified target image ID is {image_id!r}"
                )
            config = container.get("Config")
            labels = config.get("Labels") if isinstance(config, dict) else None
            if not isinstance(labels, dict):
                raise VerificationError(f"running {service} container has no labels")
            for name, expected in required.items():
                actual = _label(labels, name)
                if actual != expected:
                    raise VerificationError(
                        f"running {service} container label {name} is {actual!r}, expected {expected!r}"
                    )
        LOG.info("live web/worker containers match target image %s", image_id)

    def _prepare_target(self, release: Release, *, allow_manual: bool) -> Target:
        manifest = self._download_manifest(release)
        repository = manifest["image"]["repository"]
        if repository != self.config.image_repository:
            raise VerificationError(
                f"manifest image repository {repository} does not match configured {self.config.image_repository}"
            )
        digest = manifest["image"]["digest"]
        self.registry.verify(digest)
        image = f"{repository}@{digest}"
        self.cosign.verify(repository, digest)
        self._pull(image)
        self._verify_labels(image, release, manifest, allow_manual)
        return Target(release=release, manifest=manifest, digest=digest, image=image)

    def _pending_migrations(self, target: Target) -> bool:
        result = self._compose(
            ["run", "--rm", "--no-deps", "migrate", "python", "manage.py", "migrate", "--check"],
            image=target.image,
            version=target.release.tag,
            revision=target.manifest["commit"],
            upstream=target.manifest["upstream"]["commit"],
            backward_compatible=target.manifest["migrations"]["backward_compatible"],
            manual_approval_required=target.manifest["migrations"]["manual_approval_required"],
            timeout=self.config.command_timeout_seconds,
        )
        if result.returncode == 0:
            return False
        if result.returncode == 1:
            return True
        self._require(result, "checking pending migrations")

    # ------------------------------------------------------------------
    # deployment mechanics
    # ------------------------------------------------------------------
    def _exec_worker(self, action: str, *, timeout: float = 60) -> CommandResult:
        return self._compose(
            ["exec", "-T", "worker", "python", "manage.py", "worker_control", action],
            image=self._placeholder_image(),
            timeout=timeout,
        )

    def _wait_drained(self) -> bool:
        deadline = self.monotonic() + self.config.drain_seconds
        while True:
            result = self._exec_worker("drained")
            if result.ok:
                return True
            if self.monotonic() >= deadline:
                LOG.error("worker did not drain: %s", (result.stderr or result.text).strip()[:500])
                return False
            self.sleep(self.config.drain_poll_seconds)

    def _compose_stop(self, service: str, grace: int) -> None:
        result = self._compose(
            ["stop", "-t", str(grace), service],
            image=self._placeholder_image(),
            timeout=grace + 60,
        )
        self._require(result, f"stopping {service}")

    def _wait_health(self) -> None:
        last = ""
        for attempt in range(1, self.config.health_retries + 1):
            try:
                response = self.http.request(
                    "GET",
                    self.config.health_url,
                    headers={"Accept": "application/json", "X-Forwarded-Proto": "https"},
                )
                if response.status == 200:
                    payload = response.json()
                    if isinstance(payload, dict) and payload.get("status") == "ok":
                        return
                    last = "HTTP 200 with unexpected body"
                else:
                    last = f"HTTP {response.status}"
            except UpdaterError as exc:
                last = str(exc)
            LOG.info("health attempt %s/%s: %s", attempt, self.config.health_retries, last)
            if attempt < self.config.health_retries:
                self.sleep(self.config.health_interval_seconds)
        raise DeployError(f"health check failed at {self.config.health_url}: {last}")

    def _resume_worker(self) -> None:
        last = ""
        for _ in range(3):
            result = self._exec_worker("resume")
            if result.ok:
                return
            last = (result.stderr or result.text).strip()[:500]
            self.sleep(2)
        raise DeployError(f"could not resume worker: {last}")

    def _backup(self, tag: str) -> Path:
        result = self._compose(
            [
                "exec",
                "-T",
                "db",
                "sh",
                "-c",
                'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges',
            ],
            image=self._placeholder_image(),
            timeout=self.config.command_timeout_seconds,
        )
        if not result.ok:
            detail = (result.stderr or result.text).strip()[:1000]
            raise DeployError(f"database backup failed (exit {result.returncode}): {detail}")
        raw = result.stdout
        if not raw.strip():
            raise DeployError("database backup is empty")
        tables = raw.count(b"CREATE TABLE ")
        if tables < self.config.backup_min_tables:
            raise DeployError(
                f"database backup contains only {tables} CREATE TABLE statements "
                f"(minimum {self.config.backup_min_tables}); refusing to continue"
            )
        compressed = gzip.compress(raw, compresslevel=6, mtime=0)
        try:
            if gzip.decompress(compressed) != raw:
                raise DeployError("database backup failed gzip round-trip verification")
        except OSError as exc:
            raise DeployError(f"database backup gzip verification failed: {exc}") from exc
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_tag = re.sub(r"[^A-Za-z0-9_.-]", "_", tag)
        path = self.config.backup_dir / f"auto-buddy-{stamp}-{safe_tag}.sql.gz"
        self.config.backup_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(compressed)
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            raise DeployError(f"could not restrict backup permissions: {exc}") from exc
        LOG.info("database backup written: %s (%s tables, %s bytes)", path, tables, path.stat().st_size)
        return path

    def _prune_backups(self) -> None:
        backups = sorted(
            self.config.backup_dir.glob("auto-buddy-*.sql.gz"),
            key=lambda path: path.stat().st_mtime,
        )
        keep = self.config.backup_retention
        for path in backups[:-keep] if keep else []:
            try:
                path.unlink()
            except OSError as exc:
                LOG.warning("could not prune backup %s: %s", path, exc)

    def _state_payload(self, target: Target, backup: Path | None) -> dict:
        return {
            "tag": target.release.tag,
            "digest": target.digest,
            "image": target.image,
            "commit": target.manifest["commit"],
            "upstream_commit": target.manifest["upstream"]["commit"],
            "backward_compatible": target.manifest["migrations"]["backward_compatible"],
            "deployed_at": utc_now(),
            "backup": str(backup) if backup else None,
        }

    def _write_success_state(self, old: dict, target: Target, backup: Path) -> None:
        new_state = self._state_payload(target, backup)
        self.state.write("previous.json", old)
        self.state.write("current.json", new_state)
        self.state.delete("failed.json")
        self.state.delete("manual-required.json")

    def _record_failed(self, target: Target, stage: str, reason: str) -> None:
        self.state.write(
            "failed.json",
            {
                "tag": target.release.tag,
                "digest": target.digest,
                "stage": stage,
                "reason": reason[:2000],
                "at": utc_now(),
            },
        )

    def _record_manual(self, target: Target, stage: str, reason: str) -> None:
        self.state.write(
            "manual-required.json",
            {
                "tag": target.release.tag,
                "digest": target.digest,
                "stage": stage,
                "reason": reason[:2000],
                "at": utc_now(),
            },
        )

    def _start_old(self, old: dict) -> None:
        image = old["image"]
        identity = {
            "version": old["tag"],
            "revision": old["commit"],
            "upstream": old["upstream_commit"],
            "backward_compatible": old["backward_compatible"],
        }
        caddy = self._compose(["up", "-d", "--no-build", "--no-deps", "caddy"], image=image)
        self._require(caddy, "starting caddy during rollback")
        web = self._compose(
            ["up", "-d", "--no-build", "--no-deps", "web"],
            image=image,
            **identity,
        )
        self._require(web, "starting old web during rollback")
        self._wait_health()
        worker = self._compose(
            ["up", "-d", "--no-build", "--no-deps", "worker"],
            image=image,
            **identity,
        )
        self._require(worker, "starting old worker during rollback")
        self._resume_worker()

    def _handle_deploy_failure(
        self,
        target: Target,
        old: dict,
        stage: str,
        exc: Exception,
        *,
        stopped: bool,
        paused: bool,
        migrated: bool,
    ) -> None:
        if not stopped:
            if paused:
                try:
                    self._resume_worker()
                except UpdaterError as resume_exc:
                    LOG.error("could not resume worker after %s failure: %s", stage, resume_exc)
            raise exc
        # No schema work happened for failures before migration. A failure during
        # `migrate` may have applied some operations, so only the manifest's
        # backward-compatible guarantee makes an old-image rollback safe there.
        if not migrated and stage != "migrate":
            safe = True
        else:
            safe = target.manifest["migrations"]["backward_compatible"]
        if not safe:
            reason = f"failure at {stage} with non-backward-compatible migrations; database not restored"
            self._record_manual(target, stage, reason)
            raise ManualApprovalRequired(f"{reason}: {exc}") from exc
        try:
            self._start_old(old)
        except UpdaterError as rollback_exc:
            reason = f"failure at {stage} and rollback failed: {rollback_exc}"
            self._record_manual(target, stage, reason)
            raise ManualApprovalRequired(f"{reason}; original error: {exc}") from exc
        if stage not in ("backup", "stop"):
            self._record_failed(target, stage, str(exc))
        raise DeployError(f"deployment failed at {stage}; rolled back to {old['tag']}: {exc}") from exc

    def deploy(self, target: Target, old: dict) -> None:
        stage = "pause"
        paused = False
        stopped = False
        migrated = False
        backup: Path | None = None
        try:
            pause = self._exec_worker("pause")
            self._require(pause, "pausing worker scheduling")
            paused = True
            stage = "drain"
            if not self._wait_drained():
                raise DeployError("worker did not drain before stop timeout")
            stage = "stop"
            self._compose_stop("worker", self.config.stop_worker_grace_seconds)
            stopped = True
            self._compose_stop("web", self.config.stop_web_grace_seconds)
            stage = "backup"
            backup = self._backup(target.release.tag)
            stage = "migrate"
            migrate = self._compose(
                ["run", "--rm", "--no-deps", "migrate", "python", "manage.py", "migrate", "--noinput"],
                image=target.image,
                version=target.release.tag,
                revision=target.manifest["commit"],
                upstream=target.manifest["upstream"]["commit"],
                backward_compatible=target.manifest["migrations"]["backward_compatible"],
                manual_approval_required=target.manifest["migrations"]["manual_approval_required"],
                timeout=self.config.command_timeout_seconds,
            )
            self._require(migrate, "running database migrations")
            migrated = True
            stage = "start-web"
            caddy = self._compose(["up", "-d", "--no-build", "--no-deps", "caddy"], image=target.image)
            self._require(caddy, "starting caddy")
            web = self._compose(["up", "-d", "--no-build", "--no-deps", "web"], image=target.image)
            self._require(web, "starting new web")
            stage = "health"
            self._wait_health()
            stage = "start-worker"
            worker = self._compose(
                ["up", "-d", "--no-build", "--no-deps", "worker"],
                image=target.image,
                version=target.release.tag,
                revision=target.manifest["commit"],
                upstream=target.manifest["upstream"]["commit"],
                backward_compatible=target.manifest["migrations"]["backward_compatible"],
                manual_approval_required=target.manifest["migrations"]["manual_approval_required"],
            )
            self._require(worker, "starting new worker")
            stage = "resume"
            self._resume_worker()
            paused = False
            self._write_success_state(old, target, backup)
            self._prune_backups()
            LOG.info("deployed %s (%s)", target.release.tag, target.digest)
        except Exception as exc:
            self._handle_deploy_failure(
                target,
                old,
                stage,
                exc,
                stopped=stopped,
                paused=paused,
                migrated=migrated,
            )

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------
    def status(self) -> dict:
        return {
            "current": self.state.read("current.json"),
            "previous": self.state.read("previous.json"),
            "failed": self.state.read("failed.json"),
            "manual": self.state.read("manual-required.json"),
        }

    def _select_and_prepare(self, args: argparse.Namespace, current: dict) -> Target | None:
        current_version = Version.parse(current["tag"])
        release = self._release_for(args.tag, current_version)
        if release is None:
            LOG.info("no newer stable release; current is %s", current["tag"])
            return None
        if release.tag == current["tag"]:
            LOG.info("release %s is already current", release.tag)
            return None
        failed = self.state.read("failed.json")
        if failed and failed.get("tag") == release.tag and not args.retry_failed:
            LOG.warning(
                "release %s previously failed at %s; skipping until --retry-failed or --clear-failed",
                release.tag,
                failed.get("stage"),
            )
            return None
        self.preflight()
        target = self._prepare_target(release, allow_manual=args.allow_unsafe_migrations)
        pending = self._pending_migrations(target)
        if pending and not target.manifest["migrations"]["backward_compatible"]:
            if not args.allow_unsafe_migrations:
                reason = (
                    f"release {release.tag} has unapplied migrations that are not marked "
                    "backward compatible; manual approval required"
                )
                self._record_manual(target, "migrations", reason)
                raise ManualApprovalRequired(reason + "; pass --allow-unsafe-migrations")
            LOG.warning("continuing with non-backward-compatible migrations due to explicit override")
        LOG.info("target %s digest %s backup=%s", target.release.tag, target.digest, target.manifest["migrations"]["backward_compatible"])
        return target

    def update(self, args: argparse.Namespace) -> int:
        current = self.state.read("current.json")
        if current is None:
            raise ConfigError("no current release state; run --baseline <tag> on a fresh host first")
        target = self._select_and_prepare(args, current)
        if target is None:
            return EXIT_OK
        if args.dry_run:
            LOG.info("dry run: would deploy %s (%s); services untouched", target.release.tag, target.digest)
            return EXIT_OK
        self.deploy(target, current)
        return EXIT_OK

    def baseline(self, args: argparse.Namespace) -> int:
        if self.state.read("current.json") is not None:
            raise ConfigError("baseline is only allowed when no current state exists")
        if not args.tag:
            raise ConfigError("--baseline requires --tag")
        release = self.github.release_by_tag(args.tag)
        if release is None:
            raise ConfigError(f"release {args.tag} does not exist")
        self.preflight()
        target = self._prepare_target(release, allow_manual=args.allow_unsafe_migrations)
        # Baseline records what is actually running, so verify the live image
        # ID and labels as well as the pulled artifact.
        self._verify_live_target(target)
        if args.dry_run:
            LOG.info("dry run: would record baseline %s (%s)", target.release.tag, target.digest)
            return EXIT_OK
        self.state.write("current.json", self._state_payload(target, None))
        self.state.delete("previous.json")
        self.state.delete("failed.json")
        self.state.delete("manual-required.json")
        LOG.info("recorded baseline %s without changing running services", target.release.tag)
        return EXIT_OK

    def clear_failed(self) -> int:
        self.state.delete("failed.json")
        LOG.info("cleared failed release state")
        return EXIT_OK

    def execute(self, args: argparse.Namespace) -> int:
        if args.status:
            print(json.dumps(self.status(), indent=2, sort_keys=True))
            return EXIT_OK
        if args.check:
            checks = self.check()
            for check in checks:
                print(f"{'OK' if check.ok else 'FAIL'}  {check.name}: {check.detail}")
            return EXIT_OK if all(check.ok for check in checks) else EXIT_FAILURE
        self.ensure_dirs()
        lock_factory = self.lock_factory or FileLock
        lock = lock_factory(self.config.state_dir / "updater.lock")
        with lock:
            if args.clear_failed:
                return self.clear_failed()
            if args.baseline:
                return self.baseline(args)
            return self.update(args)


def hmac_compare(left: str, right: str) -> bool:
    """Constant-time comparison without importing secrets for one call."""
    import hmac

    return hmac.compare_digest(left.lower(), right.lower())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify and deploy an AutoBuddy release on the Docker host.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", default="/etc/auto-buddy/updater.json", help="updater JSON config")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="validate host prerequisites and exit")
    mode.add_argument("--status", action="store_true", help="print persisted release state as JSON")
    mode.add_argument("--clear-failed", action="store_true", help="forget a previously failed release")
    mode.add_argument("--baseline", action="store_true", help="record the already-running release")
    parser.add_argument("--tag", help="explicit stable tag (vX.Y.Z) to deploy or baseline")
    parser.add_argument("--dry-run", action="store_true", help="verify the release but do not touch services")
    parser.add_argument("--retry-failed", action="store_true", help="retry a tag recorded as failed")
    parser.add_argument(
        "--allow-unsafe-migrations",
        action="store_true",
        help="allow non-backward-compatible or manual-approval migrations",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    try:
        config = Config.load(Path(args.config))
        updater = Updater(config)
        return updater.execute(args)
    except UpdaterError as exc:
        LOG.error("%s", exc)
        return exc.exit_code
    except KeyboardInterrupt:
        LOG.error("interrupted")
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
