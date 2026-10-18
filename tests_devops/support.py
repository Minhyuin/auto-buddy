"""Test helpers for the DevOps tooling."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Callable

from scripts.auto_buddy_updater import (
    CommandResult,
    Config,
    HttpResponse,
    Release,
    StateStore,
    Target,
    Updater,
    Version,
)


class TemporaryDirectoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)

    def write(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def make_config(self, **overrides) -> Config:
        project = self.root / "app"
        project.mkdir(exist_ok=True)
        payload = {
            "repository": "owner/auto-buddy",
            "cosign_identity_regexp": (
                "^https://github.com/owner/auto-buddy/"
                r"\.github/workflows/release\.yml@refs/tags/v[0-9]+\.[0-9]+\.[0-9]+$"
            ),
            "project_directory": str(project),
            "compose_file": str(project / "compose.yaml"),
            "runtime_env_file": str(self.root / "etc" / "runtime.env"),
            "secrets_dir": str(self.root / "etc" / "secrets"),
            "state_dir": str(self.root / "state"),
            "backup_dir": str(self.root / "backups"),
        }
        payload.update(overrides)
        (project / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
        (self.root / "etc").mkdir(parents=True, exist_ok=True)
        (self.root / "etc" / "runtime.env").write_text(
            "APP_DOMAIN=example.test\nALLOWED_HOSTS=example.test\n"
            "CSRF_TRUSTED_ORIGINS=https://example.test\nDATABASE_NAME=autobuddy\n"
            "DATABASE_USER=autobuddy\nAPP_VERSION=v0.0.0\n",
            encoding="utf-8",
        )
        config_path = self.write("updater.json", json.dumps(payload))
        return Config.load(config_path)


class FakeRunner:
    def __init__(self, handler: Callable[..., object], *, with_env: bool = False):
        self.handler = handler
        self.with_env = with_env
        self.calls: list[tuple[list[str], dict[str, str] | None, float]] = []

    def run(self, args, *, env=None, cwd=None, timeout=900):
        command = [str(part) for part in args]
        self.calls.append((command, env, timeout))
        result = self.handler(command, env) if self.with_env else self.handler(command)
        if isinstance(result, CommandResult):
            return result
        if isinstance(result, tuple):
            return CommandResult(command, result[0], result[1], result[2])
        return CommandResult(command, int(result or 0), b"", "")

    def joined(self) -> list[str]:
        return [" ".join(call[0]) for call in self.calls]


class FakeHttp:
    def __init__(self, handler: Callable[[str, dict[str, str] | None], HttpResponse]):
        self.handler = handler
        self.calls: list[tuple[str, str, dict[str, str] | None]] = []

    def request(self, method, url, *, headers=None, body=None, allow_redirects=True):
        self.calls.append((method, url, headers))
        return self.handler(url, headers)


class FakeLock:
    def __init__(self, events: list[str]):
        self.events = events
        self.path = None
        self.acquired = False

    def __call__(self, path):
        self.path = path
        return self

    def __enter__(self):
        self.acquired = True
        self.events.append("lock")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.events.append("unlock")


def make_release(tag: str = "v1.2.3", *, backward: bool = True, manual: bool = False) -> Release:
    version = Version.parse(tag)
    return Release(
        tag=tag,
        version=version,
        body="Release notes\nRelease-Manifest-SHA256: " + "0" * 64,
        html_url=f"https://github.com/owner/auto-buddy/releases/tag/{tag}",
        assets=(
            {
                "name": "release-manifest.json",
                "size": 512,
                "browser_download_url": "https://example.test/release-manifest.json",
                "digest": "sha256:" + "0" * 64,
            },
        ),
        draft=False,
        prerelease=False,
    )  # backward/manual are carried by the manifest, not the release object


def make_target(
    tag: str = "v1.2.3",
    *,
    digest_char: str = "1",
    backward: bool = True,
    manual: bool = False,
    commit_char: str = "a",
    upstream_char: str = "b",
) -> Target:
    digest = "sha256:" + digest_char * 64
    manifest = {
        "schema_version": 1,
        "version": tag,
        "commit": commit_char * 40,
        "image": {
            "repository": "ghcr.io/owner/auto-buddy",
            "digest": digest,
            "platform": "linux/amd64",
            "tag": tag,
        },
        "migrations": {
            "backward_compatible": backward,
            "manual_approval_required": manual,
        },
        "upstream": {"commit": upstream_char * 40},
    }
    return Target(
        release=make_release(tag, backward=backward, manual=manual),
        manifest=manifest,
        digest=digest,
        image=f"ghcr.io/owner/auto-buddy@{digest}",
    )


def make_old_state(tag: str = "v1.2.2") -> dict:
    digest = "sha256:" + "9" * 64
    return {
        "tag": tag,
        "digest": digest,
        "image": f"ghcr.io/owner/auto-buddy@{digest}",
        "commit": "c" * 40,
        "upstream_commit": "d" * 40,
        "backward_compatible": True,
        "deployed_at": "2026-01-01T00:00:00Z",
        "backup": None,
    }


def make_updater(
    testcase: TemporaryDirectoryTestCase,
    runner: FakeRunner,
    http: FakeHttp,
    *,
    config: Config | None = None,
) -> Updater:
    config = config or testcase.make_config()
    return Updater(
        config,
        runner=runner,  # type: ignore[arg-type]
        http=http,  # type: ignore[arg-type]
        state=StateStore(config.state_dir),
        monotonic=lambda: 0.0,
        sleep=lambda _: None,
    )
