from __future__ import annotations

import base64
import hashlib
import json
import unittest
from dataclasses import replace
from pathlib import Path

from scripts.auto_buddy_updater import (
    ALLOWED_ISSUER,
    Config,
    ConfigError,
    CosignVerifier,
    HttpResponse,
    RegistryVerifier,
    Release,
    StateStore,
    VerificationError,
    Version,
    select_release,
    validate_release_manifest,
)
from tests_devops.support import (
    FakeHttp,
    FakeRunner,
    TemporaryDirectoryTestCase,
    make_release,
    make_updater,
)


def manifest_body(tag: str = "v1.2.3") -> dict:
    return {
        "schema_version": 1,
        "version": tag,
        "commit": "a" * 40,
        "image": {
            "repository": "ghcr.io/owner/auto-buddy",
            "digest": "sha256:" + "1" * 64,
            "platform": "linux/amd64",
            "tag": tag,
        },
        "migrations": {"backward_compatible": True, "manual_approval_required": False},
        "upstream": {"commit": "b" * 40},
    }


class VersionTests(unittest.TestCase):
    def test_parse_stable_tags(self) -> None:
        self.assertEqual(Version.parse("v1.2.3"), Version(1, 2, 3))
        self.assertEqual(Version.parse("v0.0.1"), Version(0, 0, 1))
        self.assertEqual(Version.parse("v10.20.30").tag, "v10.20.30")

    def test_reject_non_stable_tags(self) -> None:
        for value in ("1.2.3", "v1.2", "v1.2.3-rc1", "v01.2.3", "v1.2.3+build", "", "latest"):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    Version.parse(value)


class ReleaseSelectionTests(unittest.TestCase):
    def release(self, tag: str, *, draft: bool = False, prerelease: bool = False) -> Release:
        return make_release(tag) if not (draft or prerelease) else replace(
            make_release(tag), draft=draft, prerelease=prerelease
        )

    def test_latest_newer_is_selected(self) -> None:
        selected = select_release(
            [self.release("v1.2.2"), self.release("v1.3.0"), self.release("v1.2.9")],
            Version(1, 2, 2),
            None,
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.tag, "v1.3.0")

    def test_no_newer_returns_none(self) -> None:
        self.assertIsNone(select_release([self.release("v1.2.2")], Version(1, 2, 2), None))

    def test_prerelease_and_draft_are_ignored(self) -> None:
        selected = select_release(
            [
                self.release("v1.2.4"),
                self.release("v1.2.5", prerelease=True),
                self.release("v1.2.6", draft=True),
            ],
            Version(1, 2, 3),
            None,
        )
        assert selected is not None
        self.assertEqual(selected.tag, "v1.2.4")

    def test_explicit_older_tag_is_refused(self) -> None:
        with self.assertRaises(ConfigError):
            select_release([self.release("v1.2.1")], Version(1, 2, 2), "v1.2.1")

    def test_explicit_tag_must_be_stable(self) -> None:
        with self.assertRaises(ConfigError):
            select_release([], Version(1, 2, 2), "v1.2.4-rc1")


class ConfigTests(TemporaryDirectoryTestCase):
    def test_defaults_from_minimal_config(self) -> None:
        config = self.make_config()
        self.assertEqual(config.image_repository, "ghcr.io/owner/auto-buddy")
        self.assertEqual(config.project_name, "auto-buddy")
        self.assertEqual(config.cosign_issuer, ALLOWED_ISSUER)
        self.assertTrue(config.require_asset_digest)
        self.assertEqual(config.health_retries, 30)

    def test_unknown_keys_are_rejected(self) -> None:
        project = self.root / "app"
        project.mkdir()
        config_path = self.write(
            "bad.json",
            json.dumps(
                {
                    "repository": "owner/auto-buddy",
                    "cosign_identity_regexp": "^x$",
                    "project_directory": str(project),
                    "unknown": True,
                }
            ),
        )
        with self.assertRaises(ConfigError):
            Config.load(config_path)

    def test_relative_paths_are_rejected(self) -> None:
        project = self.root / "app"
        project.mkdir()
        config_path = self.write(
            "bad.json",
            json.dumps(
                {
                    "repository": "owner/auto-buddy",
                    "cosign_identity_regexp": "^x$",
                    "project_directory": "relative",
                }
            ),
        )
        with self.assertRaises(ConfigError):
            Config.load(config_path)

    def test_identity_must_be_anchored(self) -> None:
        project = self.root / "app"
        project.mkdir()
        config_path = self.write(
            "bad.json",
            json.dumps(
                {
                    "repository": "owner/auto-buddy",
                    "cosign_identity_regexp": "github.com/owner/repo",
                    "project_directory": str(project),
                }
            ),
        )
        with self.assertRaises(ConfigError):
            Config.load(config_path)

    def test_issuer_is_pinned(self) -> None:
        project = self.root / "app"
        project.mkdir()
        config_path = self.write(
            "bad.json",
            json.dumps(
                {
                    "repository": "owner/auto-buddy",
                    "cosign_identity_regexp": "^x$",
                    "project_directory": str(project),
                    "cosign_issuer": "https://evil.example",
                }
            ),
        )
        with self.assertRaises(ConfigError):
            Config.load(config_path)

    def test_only_ghcr_images(self) -> None:
        with self.assertRaises(ConfigError):
            self.make_config(image_repository="docker.io/owner/auto-buddy")


class StateStoreTests(TemporaryDirectoryTestCase):
    def test_round_trip_and_delete(self) -> None:
        store = StateStore(self.root / "state")
        self.assertIsNone(store.read("current.json"))
        store.write("current.json", {"tag": "v1.2.3"})
        self.assertEqual(store.read("current.json"), {"tag": "v1.2.3"})
        store.delete("current.json")
        self.assertIsNone(store.read("current.json"))
        store.delete("current.json")

    def test_corrupt_state_is_a_config_error(self) -> None:
        store = StateStore(self.root / "state")
        store.state_dir.mkdir()
        (store.state_dir / "current.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(ConfigError):
            store.read("current.json")


class ManifestValidationTests(unittest.TestCase):
    def test_valid_manifest(self) -> None:
        self.assertEqual(validate_release_manifest(manifest_body(), "v1.2.3")["version"], "v1.2.3")

    def test_wrong_version(self) -> None:
        with self.assertRaises(VerificationError):
            validate_release_manifest(manifest_body("v1.2.4"), "v1.2.3")

    def test_bad_digest(self) -> None:
        payload = manifest_body()
        payload["image"]["digest"] = "sha256:xyz"
        with self.assertRaises(VerificationError):
            validate_release_manifest(payload, "v1.2.3")

    def test_bad_upstream_commit(self) -> None:
        payload = manifest_body()
        payload["upstream"]["commit"] = "HEAD"
        with self.assertRaises(VerificationError):
            validate_release_manifest(payload, "v1.2.3")

    def test_manual_flag_must_be_bool(self) -> None:
        payload = manifest_body()
        payload["migrations"]["manual_approval_required"] = "yes"
        with self.assertRaises(VerificationError):
            validate_release_manifest(payload, "v1.2.3")


class ManifestDownloadTests(TemporaryDirectoryTestCase):
    def test_download_and_checksum(self) -> None:
        payload = json.dumps(manifest_body()).encode()
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()

        def handler(url: str, headers):
            return HttpResponse(200, {}, payload, url)

        http = FakeHttp(handler)
        updater = make_updater(self, FakeRunner(lambda args: 0), http)
        release = replace(
            make_release(),
            assets=(
                {
                    "name": "release-manifest.json",
                    "size": len(payload),
                    "browser_download_url": "https://example.test/release-manifest.json",
                    "digest": digest,
                },
            ),
        )
        manifest = updater._download_manifest(release)
        self.assertEqual(manifest["version"], "v1.2.3")
        self.assertEqual(len(http.calls), 1)

    def test_checksum_mismatch(self) -> None:
        payload = json.dumps(manifest_body()).encode()
        http = FakeHttp(lambda url, headers: HttpResponse(200, {}, payload, url))
        updater = make_updater(self, FakeRunner(lambda args: 0), http)
        release = replace(
            make_release(),
            assets=(
                {
                    "name": "release-manifest.json",
                    "size": len(payload),
                    "browser_download_url": "https://example.test/release-manifest.json",
                    "digest": "sha256:" + "f" * 64,
                },
            ),
        )
        with self.assertRaises(VerificationError):
            updater._download_manifest(release)

    def test_missing_digest_is_rejected_by_default(self) -> None:
        payload = json.dumps(manifest_body()).encode()
        http = FakeHttp(lambda url, headers: HttpResponse(200, {}, payload, url))
        updater = make_updater(self, FakeRunner(lambda args: 0), http)
        release = replace(
            make_release(),
            body="no checksum here",
            assets=(
                {
                    "name": "release-manifest.json",
                    "size": len(payload),
                    "browser_download_url": "https://example.test/release-manifest.json",
                },
            ),
        )
        with self.assertRaises(VerificationError):
            updater._download_manifest(release)


class RegistryVerifierTests(TemporaryDirectoryTestCase):
    def test_digest_matches_body(self) -> None:
        body = b'{"schemaVersion": 2}'
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        http = FakeHttp(
            lambda url, headers: HttpResponse(200, {"Docker-Content-Digest": digest}, body, url)
        )
        RegistryVerifier(http, "owner/auto-buddy", None, None).verify(digest)
        self.assertIn("Accept", http.calls[0][2] or {})

    def test_digest_header_mismatch(self) -> None:
        body = b"{}"
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        http = FakeHttp(
            lambda url, headers: HttpResponse(
                200, {"Docker-Content-Digest": "sha256:" + "0" * 64}, body, url
            )
        )
        with self.assertRaises(VerificationError):
            RegistryVerifier(http, "owner/auto-buddy", None, None).verify(digest)

    def test_anonymous_token_challenge(self) -> None:
        body = b'{"schemaVersion": 2}'
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        calls: list[tuple[str, dict[str, str] | None]] = []

        def handler(url: str, headers):
            calls.append((url, headers))
            if url.endswith("/token?service=ghcr.io&scope=repository%3Aowner%2Fauto-buddy%3Apull"):
                return HttpResponse(200, {}, b'{"token": "anonymous-token"}', url)
            if len(calls) == 1:
                return HttpResponse(
                    401,
                    {
                        "WWW-Authenticate": (
                            'Bearer realm="https://ghcr.io/token",service="ghcr.io",'
                            'scope="repository:owner/auto-buddy:pull"'
                        )
                    },
                    b"",
                    url,
                )
            return HttpResponse(200, {"Docker-Content-Digest": digest}, body, url)

        http = FakeHttp(handler)
        RegistryVerifier(http, "owner/auto-buddy", None, None).verify(digest)
        self.assertEqual(calls[2][1]["Authorization"], "Bearer anonymous-token")  # type: ignore[index]

    def test_non_ghcr_realm_is_refused(self) -> None:
        http = FakeHttp(
            lambda url, headers: HttpResponse(
                401,
                {"WWW-Authenticate": 'Bearer realm="https://evil.example/token",service="x"'},
                b"",
                url,
            )
        )
        with self.assertRaises(VerificationError):
            RegistryVerifier(http, "owner/auto-buddy", None, None).verify("sha256:" + "1" * 64)


class CosignVerifierTests(unittest.TestCase):
    def entries(self, digest: str, subject: str = "https://github.com/owner/auto-buddy/.github/workflows/release.yml@refs/tags/v1.2.3"):
        return json.dumps(
            [
                {
                    "critical": {
                        "identity": {"docker-reference": "ghcr.io/owner/auto-buddy"},
                        "image": {"docker-manifest-digest": digest},
                        "type": "cosign container image signature",
                    },
                    "optional": {
                        "Issuer": ALLOWED_ISSUER,
                        "Subject": subject,
                    },
                }
            ]
        ).encode()

    def test_valid_signature(self) -> None:
        digest = "sha256:" + "1" * 64
        runner = FakeRunner(lambda args: (0, self.entries(digest), ""))
        verifier = CosignVerifier(
            runner,  # type: ignore[arg-type]
            "cosign",
            "^https://github.com/owner/auto-buddy/.*$",
            "^https://github.com/owner/auto-buddy/.*$",
            ALLOWED_ISSUER,
        )
        verifier.verify("ghcr.io/owner/auto-buddy", digest)
        command = runner.calls[0][0]
        self.assertIn("--certificate-oidc-issuer", command)
        self.assertIn(ALLOWED_ISSUER, command)
        self.assertIn(f"ghcr.io/owner/auto-buddy@{digest}", command)

    def test_wrong_subject(self) -> None:
        digest = "sha256:" + "1" * 64
        runner = FakeRunner(lambda args: (0, self.entries(digest, "https://github.com/evil/repo"), ""))
        verifier = CosignVerifier(
            runner,  # type: ignore[arg-type]
            "cosign",
            "^https://github.com/owner/auto-buddy/.*$",
            "^https://github.com/owner/auto-buddy/.*$",
            ALLOWED_ISSUER,
        )
        with self.assertRaises(VerificationError):
            verifier.verify("ghcr.io/owner/auto-buddy", digest)

    def test_failed_command(self) -> None:
        runner = FakeRunner(lambda args: (1, b"", "no signatures found"))
        verifier = CosignVerifier(
            runner,  # type: ignore[arg-type]
            "cosign",
            "^x$",
            "^x$",
            ALLOWED_ISSUER,
        )
        with self.assertRaises(VerificationError):
            verifier.verify("ghcr.io/owner/auto-buddy", "sha256:" + "1" * 64)


class SecretCheckTests(TemporaryDirectoryTestCase):
    def test_valid_secrets(self) -> None:
        secrets = self.root / "etc" / "secrets"
        secrets.mkdir(parents=True)
        (secrets / "django_key").write_text("k" * 60, encoding="utf-8")
        (secrets / "credential_keys").write_text(
            json.dumps({"k1": base64.b64encode(bytes(range(32))).decode()}),
            encoding="utf-8",
        )
        (secrets / "db_password").write_text("synthetic-password", encoding="utf-8")
        updater = make_updater(self, FakeRunner(lambda args: 0), FakeHttp(lambda url, headers: HttpResponse(200, {}, b"", url)))
        checks = updater._check_secrets()
        self.assertTrue(all(check.ok for check in checks), checks)

    def test_invalid_secrets(self) -> None:
        secrets = self.root / "etc" / "secrets"
        secrets.mkdir(parents=True)
        (secrets / "django_key").write_text("short", encoding="utf-8")
        (secrets / "credential_keys").write_text('{"key": "not-base64"}', encoding="utf-8")
        (secrets / "db_password").write_text("", encoding="utf-8")
        updater = make_updater(self, FakeRunner(lambda args: 0), FakeHttp(lambda url, headers: HttpResponse(200, {}, b"", url)))
        checks = {check.name: check for check in updater._check_secrets()}
        self.assertFalse(checks["django_key"].ok)
        self.assertFalse(checks["credential_keys"].ok)
        self.assertFalse(checks["db_password"].ok)

    def test_runtime_env_requires_safe_debug(self) -> None:
        config = self.make_config()
        runtime = Path(config.runtime_env_file)
        runtime.write_text(
            "APP_DOMAIN=example.test\nALLOWED_HOSTS=example.test\n"
            "CSRF_TRUSTED_ORIGINS=https://example.test\nDATABASE_NAME=autobuddy\n"
            "DATABASE_USER=autobuddy\nAPP_VERSION=v1.0.0\nDJANGO_DEBUG=true\n",
            encoding="utf-8",
        )
        updater = make_updater(self, FakeRunner(lambda args: 0), FakeHttp(lambda url, headers: HttpResponse(200, {}, b"", url)))
        checks = {check.name: check for check in updater._check_runtime_env()}
        self.assertFalse(checks["DJANGO_DEBUG is false"].ok)


if __name__ == "__main__":
    unittest.main()
