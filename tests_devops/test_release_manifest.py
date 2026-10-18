"""Unit tests for scripts/release_manifest.py (stdlib only)."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from scripts import release_manifest


COMMIT = "a" * 40
UPSTREAM = "b" * 40
DIGEST = "sha256:" + "c" * 64
REPOSITORY = "ghcr.io/acme/auto-buddy"


def migration_source(operation: str) -> str:
    return (
        "from django.db import migrations, models\n\n"
        "class Migration(migrations.Migration):\n"
        "    dependencies = []\n"
        "    operations = [\n"
        f"        {operation},\n"
        "    ]\n"
    )


class ClassifyFileTests(unittest.TestCase):
    def _classify(self, source: str) -> list[release_manifest.Operation]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "0001_test.py"
            path.write_text(source, encoding="utf-8")
            return release_manifest.classify_file(path)

    def test_create_model_is_safe(self) -> None:
        operations = self._classify(migration_source('migrations.CreateModel(name="Thing", fields=[])'))
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0].operation, "CreateModel")
        self.assertEqual(operations[0].safety, "safe")
        self.assertGreater(operations[0].line, 0)

    def test_nullable_add_field_is_safe(self) -> None:
        operations = self._classify(
            migration_source(
                'migrations.AddField(model_name="thing", name="note", '
                "field=models.CharField(max_length=10, null=True))"
            )
        )
        self.assertEqual(operations[0].safety, "safe")

    def test_database_defaulted_add_field_is_safe(self) -> None:
        operations = self._classify(
            migration_source(
                'migrations.AddField(model_name="thing", name="count", '
                "field=models.IntegerField(db_default=0))"
            )
        )
        self.assertEqual(operations[0].safety, "safe")
        self.assertIn("database default", operations[0].reason)

    def test_python_default_add_field_requires_review(self) -> None:
        operations = self._classify(
            migration_source(
                'migrations.AddField(model_name="thing", name="count", '
                "field=models.IntegerField(default=0))"
            )
        )
        self.assertEqual(operations[0].safety, "review")
        self.assertIn("migration-time", operations[0].reason)
        self.assertFalse(release_manifest.PolicyResult(files=[], operations=operations).backward_compatible)

    def test_non_null_add_field_without_default_is_unsafe(self) -> None:
        operations = self._classify(
            migration_source(
                'migrations.AddField(model_name="thing", name="count", '
                "field=models.IntegerField())"
            )
        )
        self.assertEqual(operations[0].safety, "unsafe")

    def test_alter_field_requires_review(self) -> None:
        operations = self._classify(
            migration_source(
                'migrations.AlterField(model_name="thing", name="name", field=models.TextField())'
            )
        )
        self.assertEqual(operations[0].safety, "review")

    def test_run_python_is_unsafe(self) -> None:
        operations = self._classify(migration_source("migrations.RunPython(foo, bar)"))
        self.assertEqual(operations[0].safety, "unsafe")

    def test_unknown_operation_requires_review(self) -> None:
        operations = self._classify(migration_source("migrations.CustomOperation()"))
        self.assertEqual(operations[0].safety, "review")

    def test_missing_migration_class_is_unsafe(self) -> None:
        operations = self._classify("VALUE = 1\n")
        self.assertEqual(operations[0].safety, "unsafe")
        self.assertEqual(operations[0].operation, "<no-migration-class>")

    def test_syntax_error_is_unsafe(self) -> None:
        operations = self._classify("def broken(:\n")
        self.assertEqual(operations[0].safety, "unsafe")
        self.assertEqual(operations[0].operation, "<syntax-error>")

    def test_unreadable_file_is_unsafe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            operations = release_manifest.classify_file(Path(tmp) / "missing.py")
        self.assertEqual(operations[0].safety, "unsafe")
        self.assertEqual(operations[0].operation, "<unreadable>")


class ClassifyMigrationsTests(unittest.TestCase):
    def test_sorted_files_and_ignored_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "0002_second.py").write_text(
                migration_source("migrations.AddIndex(model_name='thing', index=models.Index(fields=['id'], name='idx'))"),
                encoding="utf-8",
            )
            (directory / "0001_first.py").write_text(
                migration_source("migrations.CreateModel(name='Thing', fields=[])"),
                encoding="utf-8",
            )
            (directory / "__init__.py").write_text("", encoding="utf-8")
            (directory / ".hidden.py").write_text("", encoding="utf-8")
            policy = release_manifest.classify_migrations(directory)
        self.assertEqual(policy.files, ["0001_first.py", "0002_second.py"])
        self.assertTrue(policy.backward_compatible)
        self.assertFalse(policy.manual_approval_required)

    def test_missing_directory_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = release_manifest.classify_migrations(Path(tmp) / "missing")
        self.assertEqual(policy.files, [])
        self.assertEqual(policy.operations, [])
        self.assertTrue(policy.backward_compatible)
        self.assertFalse(policy.manual_approval_required)

    def test_unsafe_operation_marks_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "0001_delete.py").write_text(
                migration_source('migrations.DeleteModel(name="Thing")'),
                encoding="utf-8",
            )
            policy = release_manifest.classify_migrations(Path(tmp))
        self.assertFalse(policy.backward_compatible)
        self.assertTrue(policy.manual_approval_required)
        payload = policy.to_dict()
        self.assertEqual(payload["summary"], {"safe": 0, "review": 0, "unsafe": 1})
        self.assertEqual(len(payload["review"]), 1)


class BuildManifestTests(unittest.TestCase):
    def _policy(self) -> release_manifest.PolicyResult:
        return release_manifest.PolicyResult(
            files=["0001_initial.py"],
            operations=[
                release_manifest.Operation(
                    file="0001_initial.py",
                    line=6,
                    operation="CreateModel",
                    safety="safe",
                    reason="non-destructive schema change",
                )
            ],
        )

    def test_builds_a_valid_manifest(self) -> None:
        manifest = release_manifest.build_manifest(
            version="v1.2.3",
            commit=COMMIT,
            image_repository=REPOSITORY,
            digest=DIGEST,
            upstream_commit=UPSTREAM,
            policy=self._policy(),
            created_at="2026-10-18T00:00:00Z",
            source_url="https://github.com/acme/auto-buddy",
            upstream_tag="v1.7.0",
            run_id="12345",
            workflow_ref="acme/auto-buddy/.github/workflows/release.yml@refs/heads/main",
        )
        self.assertEqual(release_manifest.validate_manifest(manifest), [])
        self.assertEqual(manifest["image"]["tag"], "v1.2.3")
        self.assertEqual(manifest["upstream"]["tag"], "v1.7.0")
        self.assertEqual(manifest["build"]["run_id"], "12345")

    def test_rejects_bad_inputs(self) -> None:
        common = dict(
            version="v1.2.3",
            commit=COMMIT,
            image_repository=REPOSITORY,
            digest=DIGEST,
            upstream_commit=UPSTREAM,
            policy=self._policy(),
        )
        for field, value in (
            ("version", "1.2.3"),
            ("commit", "abc"),
            ("image_repository", "docker.io/acme/auto-buddy"),
            ("digest", "sha256:abc"),
            ("upstream_commit", "xyz"),
        ):
            with self.subTest(field=field):
                arguments = dict(common)
                arguments[field] = value
                with self.assertRaises(ValueError):
                    release_manifest.build_manifest(**arguments)

    def test_validate_detects_inconsistent_migration_flags(self) -> None:
        manifest = release_manifest.build_manifest(
            version="v1.2.3",
            commit=COMMIT,
            image_repository=REPOSITORY,
            digest=DIGEST,
            upstream_commit=UPSTREAM,
            policy=self._policy(),
            created_at="2026-10-18T00:00:00Z",
        )
        manifest["migrations"]["manual_approval_required"] = True
        errors = release_manifest.validate_manifest(manifest)
        self.assertIn("migrations.manual_approval_required is inconsistent with operations", errors)

    def test_validate_requires_core_fields(self) -> None:
        errors = release_manifest.validate_manifest({})
        self.assertTrue(any("missing fields" in error for error in errors))
        self.assertIn("schema_version must be 1", errors)


class CliTests(unittest.TestCase):
    def test_policy_fail_on_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "0001_run.py").write_text(
                migration_source("migrations.RunSQL('SELECT 1')"),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = release_manifest.main(
                    ["policy", "--migrations-dir", tmp, "--format", "text", "--fail-on-review"]
                )
        self.assertEqual(code, 1)
        self.assertIn("manual_approval_required=true", stdout.getvalue())

    def test_write_then_validate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            migrations = Path(tmp) / "migrations"
            migrations.mkdir()
            (migrations / "0001_initial.py").write_text(
                migration_source("migrations.CreateModel(name='Thing', fields=[])"),
                encoding="utf-8",
            )
            output = Path(tmp) / "release-manifest.json"
            with contextlib.redirect_stdout(io.StringIO()):
                code = release_manifest.main(
                    [
                        "write",
                        "--version",
                        "v1.2.3",
                        "--commit",
                        COMMIT,
                        "--image-repository",
                        REPOSITORY,
                        "--digest",
                        DIGEST,
                        "--upstream-commit",
                        UPSTREAM,
                        "--migrations-dir",
                        str(migrations),
                        "--created-at",
                        "2026-10-18T00:00:00Z",
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertTrue(output.exists())
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    release_manifest.main(["validate", "--manifest", str(output)]),
                    0,
                )

    def test_write_rejects_bad_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = release_manifest.main(
                    [
                        "write",
                        "--version",
                        "v1.2.3",
                        "--commit",
                        COMMIT,
                        "--image-repository",
                        REPOSITORY,
                        "--digest",
                        "sha256:bad",
                        "--upstream-commit",
                        UPSTREAM,
                        "--migrations-dir",
                        tmp,
                        "--output",
                        str(Path(tmp) / "manifest.json"),
                    ]
                )
        self.assertEqual(code, 2)
        self.assertIn("image digest", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
