#!/usr/bin/env python3
"""Classify Django migrations and produce the signed-release manifest input.

The host updater refuses to roll a release forward across pending migrations
unless the release manifest declares them backward compatible. That decision is
made here, statically, without a database:

  safe    New tables, nullable/defaulted columns, indexes and merges. Old code
          continues to work against the new schema.
  review  Operations whose rollback safety cannot be proven statically
          (AlterField, constraints, unknown/custom operations). Treated as not
          backward compatible and requiring manual approval.
  unsafe  Destructive or arbitrary operations: DeleteModel, RemoveField,
          Rename*, RunPython, RunSQL, RemoveConstraint, separate-state hacks.

`write` emits release-manifest.json (validated against
deployment/release-manifest.schema.json and the updater's own checks). `policy`
prints the classification for humans/CI. `validate` checks an existing
manifest.

Exit codes: 0 success, 2 invalid input/manifest, 1 when --fail-on-review sees a
non-safe operation.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

VERSION_RE = re.compile(r"^v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REPOSITORY_RE = re.compile(r"^ghcr\.io/[a-z0-9]+(?:[._-][a-z0-9]+)*/[a-z0-9]+(?:[._-][a-z0-9]+)*$")
SAFETY_RANK = {"safe": 0, "review": 1, "unsafe": 2}

SAFE_OPERATIONS = frozenset(
    {
        "CreateModel",
        "AddIndex",
        "RemoveIndex",
        "Merge",
        "AlterModelOptions",
    }
)
REVIEW_OPERATIONS = frozenset(
    {
        "AlterField",
        "AddConstraint",
        "AlterUniqueTogether",
        "AlterIndexTogether",
        "AlterModelTable",
    }
)
UNSAFE_OPERATIONS = frozenset(
    {
        "DeleteModel",
        "RemoveField",
        "RenameModel",
        "RenameField",
        "RunPython",
        "RunSQL",
        "RemoveConstraint",
        "SeparateDatabaseAndState",
    }
)


@dataclass
class Operation:
    file: str
    line: int
    operation: str
    safety: str
    reason: str

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "line": self.line,
            "operation": self.operation,
            "safety": self.safety,
            "reason": self.reason,
        }


@dataclass
class PolicyResult:
    files: list[str] = field(default_factory=list)
    operations: list[Operation] = field(default_factory=list)

    @property
    def backward_compatible(self) -> bool:
        return all(operation.safety == "safe" for operation in self.operations)

    @property
    def manual_approval_required(self) -> bool:
        return any(operation.safety != "safe" for operation in self.operations)

    @property
    def review_operations(self) -> list[Operation]:
        return [operation for operation in self.operations if operation.safety != "safe"]

    def to_dict(self) -> dict:
        summary = {"safe": 0, "review": 0, "unsafe": 0}
        for operation in self.operations:
            summary[operation.safety] += 1
        return {
            "backward_compatible": self.backward_compatible,
            "manual_approval_required": self.manual_approval_required,
            "files": self.files,
            "operations": [operation.to_dict() for operation in self.operations],
            "review": [operation.to_dict() for operation in self.review_operations],
            "summary": summary,
        }


class MigrationParseError(Exception):
    pass


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
    return None


def _keyword(node: ast.Call, name: str) -> ast.AST | None:
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _is_true(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _field_is_safe_to_add(operation: ast.Call) -> tuple[str, str]:
    """Classify an AddField without guessing about row contents.

    Only NULL or an explicit database default survives old-image INSERTs that
    omit the new column.  A Django ``default=`` is applied by the migration only
    (the temporary default is then removed), so it is not a persistent database
    default and must not be treated as statically backward compatible.
    """
    field_node = _keyword(operation, "field")
    if not isinstance(field_node, ast.Call):
        return "unsafe", "cannot find the field definition"
    calls = [field_node, operation]
    for call in calls:
        if _is_true(_keyword(call, "null")):
            return "safe", "field is nullable"
    for call in calls:
        if _keyword(call, "db_default") is not None:
            return "safe", "field has an explicit database default"
    for call in calls:
        if _keyword(call, "default") is not None:
            return (
                "review",
                "Python default is migration-time only, not a database default; "
                "old code inserting without the new NOT NULL column can fail, so manual review is required",
            )
    return "unsafe", "adding a non-null field without a persistent default fails on existing rows"


def _classify_call(file: str, call: ast.Call) -> Operation | None:
    name = _call_name(call)
    if name is None:
        return None
    line = getattr(call, "lineno", 0)
    if name == "AddField":
        safety, reason = _field_is_safe_to_add(call)
        return Operation(file, line, name, safety, reason)
    if name in SAFE_OPERATIONS:
        return Operation(file, line, name, "safe", "non-destructive schema change")
    if name in REVIEW_OPERATIONS:
        return Operation(file, line, name, "review", "rollback safety needs manual review")
    if name in UNSAFE_OPERATIONS:
        return Operation(file, line, name, "unsafe", "destructive or arbitrary database operation")
    return Operation(file, line, name, "review", "unknown operation; review required")


def classify_file(path: Path, display_name: str | None = None) -> list[Operation]:
    name = display_name or path.name
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [Operation(name, 0, "<unreadable>", "unsafe", f"cannot read file: {exc}")]
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [Operation(name, exc.lineno or 0, "<syntax-error>", "unsafe", f"cannot parse migration: {exc.msg}")]

    operations: list[Operation] = []
    migration_classes = [
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Migration"
    ]
    if not migration_classes:
        return [Operation(name, 0, "<no-migration-class>", "unsafe", "file has no Migration class")]

    for migration in migration_classes:
        for statement in migration.body:
            if not isinstance(statement, ast.Assign):
                continue
            if not any(isinstance(target, ast.Name) and target.id == "operations" for target in statement.targets):
                continue
            if not isinstance(statement.value, (ast.List, ast.Tuple)):
                operations.append(
                    Operation(name, statement.lineno, "<ops-not-list>", "unsafe", "operations is not a literal list")
                )
                continue
            for element in statement.value.elts:
                if not isinstance(element, ast.Call):
                    operations.append(
                        Operation(name, getattr(element, "lineno", 0), "<unknown>", "unsafe", "operation is not a call")
                    )
                    continue
                classified = _classify_call(name, element)
                if classified is not None:
                    operations.append(classified)
    if not operations:
        return [Operation(name, 0, "<no-operations>", "unsafe", "no operations found in Migration")]
    return operations


def migration_files(migrations_dir: Path) -> list[Path]:
    if not migrations_dir.exists():
        return []
    if not migrations_dir.is_dir():
        raise MigrationParseError(f"migrations path is not a directory: {migrations_dir}")
    return sorted(
        path
        for path in migrations_dir.glob("*.py")
        if path.name != "__init__.py" and not path.name.startswith(".")
    )


def classify_migrations(migrations_dir: Path) -> PolicyResult:
    files = migration_files(migrations_dir)
    result = PolicyResult(files=[path.name for path in files])
    for path in files:
        result.operations.extend(classify_file(path))
    return result


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_manifest(
    *,
    version: str,
    commit: str,
    image_repository: str,
    digest: str,
    upstream_commit: str,
    policy: PolicyResult,
    created_at: str | None = None,
    source_url: str | None = None,
    upstream_repository: str = "88lin/workbuddy-auto-signin",
    upstream_tag: str | None = None,
    migrations_dir: str = "portal/migrations",
    run_id: str | None = None,
    workflow_ref: str | None = None,
) -> dict:
    if not VERSION_RE.match(version):
        raise ValueError(f"version must be vMAJOR.MINOR.PATCH: {version!r}")
    if not COMMIT_RE.match(commit):
        raise ValueError(f"commit must be a 40-character lowercase SHA: {commit!r}")
    if not DIGEST_RE.match(digest):
        raise ValueError(f"image digest must be sha256:<64 hex>: {digest!r}")
    if not REPOSITORY_RE.match(image_repository):
        raise ValueError(f"image repository must look like ghcr.io/owner/name: {image_repository!r}")
    if not COMMIT_RE.match(upstream_commit):
        raise ValueError(f"upstream commit must be a 40-character lowercase SHA: {upstream_commit!r}")

    manifest = {
        "schema_version": 1,
        "version": version,
        "created_at": created_at or _iso_now(),
        "commit": commit,
        "image": {
            "repository": image_repository,
            "digest": digest,
            "platform": "linux/amd64",
            "tag": version,
        },
        "migrations": {
            "directory": migrations_dir,
            "backward_compatible": policy.backward_compatible,
            "manual_approval_required": policy.manual_approval_required,
            "files": policy.files,
            "operations": [operation.to_dict() for operation in policy.operations],
            "review": [operation.to_dict() for operation in policy.review_operations],
        },
        "upstream": {
            "repository": upstream_repository,
            "commit": upstream_commit,
        },
    }
    if upstream_tag:
        manifest["upstream"]["tag"] = upstream_tag
    if source_url:
        manifest["source"] = source_url
    if run_id:
        manifest["build"] = {"run_id": str(run_id)}
        if workflow_ref:
            manifest["build"]["workflow_ref"] = workflow_ref
    errors = validate_manifest(manifest)
    if errors:
        raise ValueError("generated manifest is invalid: " + "; ".join(errors))
    return manifest


def _validate_operation_list(value: object, where: str, errors: list[str]) -> None:
    if not isinstance(value, list):
        errors.append(f"{where} must be a list")
        return
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            errors.append(f"{where}[{index}] must be an object")
            continue
        for key in ("file", "operation", "safety", "reason"):
            if not isinstance(item.get(key), str):
                errors.append(f"{where}[{index}].{key} must be a string")
        if item.get("safety") not in SAFETY_RANK:
            errors.append(f"{where}[{index}].safety must be safe/review/unsafe")
        if not isinstance(item.get("line"), int) or item.get("line", -1) < 0:
            errors.append(f"{where}[{index}].line must be a non-negative integer")


def validate_manifest(data: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["manifest must be a JSON object"]

    required = {"schema_version", "version", "created_at", "commit", "image", "migrations", "upstream"}
    missing = required - set(data)
    if missing:
        errors.append(f"missing fields: {', '.join(sorted(missing))}")
    if data.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if not isinstance(data.get("version"), str) or not VERSION_RE.match(data["version"]):
        errors.append("version must be vMAJOR.MINOR.PATCH")
    if not isinstance(data.get("commit"), str) or not COMMIT_RE.match(data["commit"]):
        errors.append("commit must be a 40-character lowercase SHA")
    created_at = data.get("created_at")
    if not isinstance(created_at, str):
        errors.append("created_at must be an RFC3339 string")
    else:
        try:
            datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            errors.append("created_at must be an RFC3339 timestamp")

    image = data.get("image")
    if not isinstance(image, dict):
        errors.append("image must be an object")
    else:
        repository = image.get("repository")
        if not isinstance(repository, str) or not REPOSITORY_RE.match(repository):
            errors.append("image.repository must be ghcr.io/owner/name")
        if not isinstance(image.get("digest"), str) or not DIGEST_RE.match(image["digest"]):
            errors.append("image.digest must be sha256:<64 hex>")
        if image.get("platform") != "linux/amd64":
            errors.append("image.platform must be linux/amd64")

    migrations = data.get("migrations")
    if not isinstance(migrations, dict):
        errors.append("migrations must be an object")
    else:
        for key in ("backward_compatible", "manual_approval_required"):
            if not isinstance(migrations.get(key), bool):
                errors.append(f"migrations.{key} must be a boolean")
        files = migrations.get("files")
        if not isinstance(files, list) or any(not isinstance(item, str) for item in files):
            errors.append("migrations.files must be a list of strings")
        _validate_operation_list(migrations.get("operations"), "migrations.operations", errors)
        _validate_operation_list(migrations.get("review"), "migrations.review", errors)
        if isinstance(migrations.get("backward_compatible"), bool) and isinstance(
            migrations.get("manual_approval_required"), bool
        ):
            operations = migrations.get("operations")
            if isinstance(operations, list):
                expected = all(
                    isinstance(item, dict) and item.get("safety") == "safe" for item in operations
                )
                if migrations["backward_compatible"] != expected:
                    errors.append("migrations.backward_compatible is inconsistent with operations")
                has_non_safe = any(
                    isinstance(item, dict) and item.get("safety") != "safe" for item in operations
                )
                if migrations["manual_approval_required"] != has_non_safe:
                    errors.append("migrations.manual_approval_required is inconsistent with operations")

    upstream = data.get("upstream")
    if not isinstance(upstream, dict):
        errors.append("upstream must be an object")
    else:
        if not isinstance(upstream.get("repository"), str) or not upstream["repository"]:
            errors.append("upstream.repository must be a non-empty string")
        if not isinstance(upstream.get("commit"), str) or not COMMIT_RE.match(upstream["commit"]):
            errors.append("upstream.commit must be a 40-character lowercase SHA")

    source = data.get("source")
    if source is not None and (not isinstance(source, str) or not source.startswith(("https://", "http://"))):
        errors.append("source must be an http(s) URL")
    return errors


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _print_policy_text(policy: PolicyResult) -> None:
    for operation in policy.operations:
        print(f"{operation.safety:6} {operation.file}:{operation.line} {operation.operation} - {operation.reason}")
    print(
        f"files={len(policy.files)} operations={len(policy.operations)} "
        f"backward_compatible={str(policy.backward_compatible).lower()} "
        f"manual_approval_required={str(policy.manual_approval_required).lower()}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Django migration policy and release manifest tooling.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    policy_parser = subparsers.add_parser("policy", help="classify migrations")
    policy_parser.add_argument("--migrations-dir", default="portal/migrations")
    policy_parser.add_argument("--format", choices=("json", "text"), default="json")
    policy_parser.add_argument("--fail-on-review", action="store_true")

    write_parser = subparsers.add_parser("write", help="write release-manifest.json")
    write_parser.add_argument("--version", required=True)
    write_parser.add_argument("--commit", required=True)
    write_parser.add_argument("--image-repository", required=True)
    write_parser.add_argument("--digest", required=True)
    write_parser.add_argument("--upstream-commit", required=True)
    write_parser.add_argument("--upstream-repository", default="88lin/workbuddy-auto-signin")
    write_parser.add_argument("--upstream-tag", default=None)
    write_parser.add_argument("--migrations-dir", default="portal/migrations")
    write_parser.add_argument("--source-url", default=None)
    write_parser.add_argument("--created-at", default=None)
    write_parser.add_argument("--run-id", default=None)
    write_parser.add_argument("--workflow-ref", default=None)
    write_parser.add_argument("--output", default="release-manifest.json")

    validate_parser = subparsers.add_parser("validate", help="validate an existing manifest")
    validate_parser.add_argument("--manifest", required=True)

    args = parser.parse_args(argv)

    try:
        if args.command == "policy":
            policy = classify_migrations(Path(args.migrations_dir))
            payload = policy.to_dict()
            if args.format == "json":
                print(json.dumps(payload, indent=2))
            else:
                _print_policy_text(policy)
            if args.fail_on_review and policy.manual_approval_required:
                return 1
            return 0

        if args.command == "write":
            policy = classify_migrations(Path(args.migrations_dir))
            manifest = build_manifest(
                version=args.version,
                commit=args.commit,
                image_repository=args.image_repository,
                digest=args.digest,
                upstream_commit=args.upstream_commit,
                policy=policy,
                created_at=args.created_at,
                source_url=args.source_url,
                upstream_repository=args.upstream_repository,
                upstream_tag=args.upstream_tag,
                migrations_dir=args.migrations_dir,
                run_id=args.run_id,
                workflow_ref=args.workflow_ref,
            )
            output = Path(args.output)
            _write_json(output, manifest)
            print(output)
            return 0

        if args.command == "validate":
            data = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
            errors = validate_manifest(data)
            if errors:
                for error in errors:
                    print(f"invalid: {error}", file=sys.stderr)
                return 2
            print(f"valid: {args.manifest}")
            return 0
    except (OSError, ValueError, MigrationParseError, json.JSONDecodeError) as exc:
        print(f"release_manifest: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
