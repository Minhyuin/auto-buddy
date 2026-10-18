#!/usr/bin/env python3
"""Compute the next release version from Conventional Commits.

Rules (Conventional Commits 1.0.0):
  * ``!`` in the header or a ``BREAKING CHANGE:`` / ``BREAKING-CHANGE:`` footer
    bumps the major version. While the major version is 0 a breaking change
    bumps the minor version instead (0.x compatibility convention).
  * ``feat`` bumps minor.
  * ``fix``, ``perf`` and ``revert`` bump patch.
  * Everything else (chore, ci, docs, style, test, build, refactor, ...) does
    not trigger a release on its own.

The current version is the highest strict ``vMAJOR.MINOR.PATCH`` git tag. A
manual ``--version`` may override the computed version, but it must be greater
than the current tag; tags are immutable release identifiers.

Used by .github/workflows/release.yml. Exits 0 on success, 2 on input/usage or
git errors.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

TAG_RE = re.compile(r"^v(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)$")
MANUAL_VERSION_RE = re.compile(r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)$")
CONVENTIONAL_RE = re.compile(
    r"^(?P<type>[A-Za-z][A-Za-z0-9_-]*)(?:\((?P<scope>[^()\r\n]+)\))?(?P<breaking>!)?:\s+(?P<description>\S.*)$"
)
BREAKING_FOOTER_RE = re.compile(r"(?im)^BREAKING[ -]CHANGE:\s*\S")

BUMP_RANK = {"none": 0, "patch": 1, "minor": 2, "major": 3}
NON_RELEASE_TYPES = frozenset(
    {"build", "chore", "ci", "docs", "refactor", "style", "test", "tests", "release"}
)
PATCH_TYPES = frozenset({"fix", "perf", "revert"})
FEATURE_TYPES = frozenset({"feat", "feature"})


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
        match = TAG_RE.match(value.strip())
        if not match:
            raise ValueError(f"not a strict vMAJOR.MINOR.PATCH version: {value!r}")
        return cls(int(match.group("major")), int(match.group("minor")), int(match.group("patch")))

    def bump(self, kind: str) -> "Version":
        if kind == "major":
            return Version(self.major + 1, 0, 0)
        if kind == "minor":
            return Version(self.major, self.minor + 1, 0)
        if kind == "patch":
            return Version(self.major, self.minor, self.patch + 1)
        if kind == "none":
            return self
        raise ValueError(f"unknown bump kind: {kind!r}")


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    body: str
    type: str | None
    scope: str | None
    breaking: bool
    description: str

    def to_dict(self) -> dict:
        return {
            "sha": self.sha,
            "subject": self.subject,
            "type": self.type,
            "scope": self.scope,
            "breaking": self.breaking,
            "description": self.description,
        }

    def bump_kind(self, current: Version) -> str:
        if self.breaking:
            return "minor" if current.major == 0 else "major"
        if self.type in FEATURE_TYPES:
            return "minor"
        if self.type in PATCH_TYPES:
            return "patch"
        return "none"


def parse_commit(sha: str, subject: str, body: str) -> Commit:
    header = CONVENTIONAL_RE.match(subject.strip())
    breaking = bool(BREAKING_FOOTER_RE.search(body or ""))
    if header:
        return Commit(
            sha=sha,
            subject=subject.strip(),
            body=body,
            type=header.group("type").lower(),
            scope=header.group("scope"),
            breaking=breaking or bool(header.group("breaking")),
            description=header.group("description").strip(),
        )
    return Commit(
        sha=sha,
        subject=subject.strip(),
        body=body,
        type=None,
        scope=None,
        breaking=breaking,
        description=subject.strip(),
    )


def parse_commit_log(raw: str) -> list[Commit]:
    commits: list[Commit] = []
    for record in raw.split("\x1e"):
        record = record.strip("\n\r")
        if not record:
            continue
        parts = record.split("\x1f", 2)
        if len(parts) != 3:
            continue
        sha, subject, body = parts
        commits.append(parse_commit(sha.strip(), subject, body))
    return commits


def highest_version(tags: Iterable[str]) -> Version | None:
    versions = []
    for tag in tags:
        try:
            versions.append(Version.parse(tag.strip()))
        except ValueError:
            continue
    return max(versions) if versions else None


def aggregate_bump(commits: Sequence[Commit], current: Version) -> str:
    best = "none"
    for commit in commits:
        kind = commit.bump_kind(current)
        if BUMP_RANK[kind] > BUMP_RANK[best]:
            best = kind
    return best


def compute_next(current: Version, commits: Sequence[Commit], manual: str | None) -> tuple[Version, str]:
    bump = aggregate_bump(commits, current)
    if manual is not None:
        match = MANUAL_VERSION_RE.match(manual.strip())
        if not match:
            raise ValueError(f"manual version must be MAJOR.MINOR.PATCH (optional leading v): {manual!r}")
        target = Version(int(match.group("major")), int(match.group("minor")), int(match.group("patch")))
        if target <= current:
            raise ValueError(f"manual version {target.tag} must be greater than current {current.tag}")
        return target, "manual"
    return current.bump(bump), bump


def _run_git(args: Sequence[str], cwd: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise RuntimeError("git executable not found") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}") from exc
    return completed.stdout


def read_tags(cwd: Path) -> list[str]:
    return [line.strip() for line in _run_git(["tag", "--list", "v*"], cwd).splitlines() if line.strip()]


def read_commits(cwd: Path, since: Version | None) -> list[Commit]:
    rev_range = f"{since.tag}..HEAD" if since else "HEAD"
    raw = _run_git(
        ["log", "--no-merges", "--format=%H%x1f%s%x1f%b%x1e", rev_range],
        cwd,
    )
    return parse_commit_log(raw)


def _write_github_output(path: Path, values: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise ValueError(f"GitHub output value for {key!r} contains a newline")
            handle.write(f"{key}={value}\n")


def build_payload(
    current: Version,
    target: Version,
    bump: str,
    commits: Sequence[Commit],
    manual: str | None,
) -> dict:
    release = bump != "none" or manual is not None
    return {
        "release": release,
        "version": target.tag,
        "previous_version": current.tag,
        "bump": bump,
        "manual": manual is not None,
        "commit_count": len(commits),
        "commits": [commit.to_dict() for commit in commits],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute the next semantic version from Conventional Commits.",
    )
    parser.add_argument("--version", dest="manual", default=None, help="manual MAJOR.MINOR.PATCH override")
    parser.add_argument(
        "--format",
        choices=("text", "json", "github"),
        default="text",
        help="text prints the version; json prints full details; github writes GITHUB_OUTPUT",
    )
    parser.add_argument("--repo-root", default=".", help="git repository root (default: current directory)")
    parser.add_argument(
        "--allow-no-release",
        action="store_true",
        help="exit 0 with release=false when no release-worthy commits exist (default)",
    )
    args = parser.parse_args(argv)
    del args.allow_no_release  # no-release is always a successful no-op

    cwd = Path(args.repo_root).resolve()
    try:
        tags = read_tags(cwd)
        current = highest_version(tags) or Version(0, 0, 0)
        commits = read_commits(cwd, highest_version(tags))
        target, bump = compute_next(current, commits, args.manual)
    except (RuntimeError, ValueError) as exc:
        print(f"next_version: {exc}", file=sys.stderr)
        return 2

    payload = build_payload(current, target, bump, commits, args.manual)

    if args.format == "json":
        print(json.dumps(payload, indent=2))
        return 0

    if args.format == "github":
        output_path = os.environ.get("GITHUB_OUTPUT")
        if not output_path:
            print("next_version: --format github requires GITHUB_OUTPUT", file=sys.stderr)
            return 2
        try:
            _write_github_output(
                Path(output_path),
                {
                    "version": payload["version"],
                    "previous_version": payload["previous_version"],
                    "bump": payload["bump"],
                    "release": "true" if payload["release"] else "false",
                },
            )
        except OSError as exc:
            print(f"next_version: cannot write GITHUB_OUTPUT: {exc}", file=sys.stderr)
            return 2
        print(payload["version"])
        return 0

    print(payload["version"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
