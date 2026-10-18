"""Unit tests for scripts/next_version.py (stdlib only)."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import next_version


class VersionTests(unittest.TestCase):
    def test_parse_and_tag(self) -> None:
        version = next_version.Version.parse("v1.2.3")
        self.assertEqual((version.major, version.minor, version.patch), (1, 2, 3))
        self.assertEqual(version.tag, "v1.2.3")

    def test_rejects_leading_zero_and_prerelease(self) -> None:
        for value in ("1.2.3", "v01.2.3", "v1.2", "v1.2.3-rc1", "v1.2.3.4"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    next_version.Version.parse(value)

    def test_bump(self) -> None:
        self.assertEqual(next_version.Version(1, 2, 3).bump("major"), next_version.Version(2, 0, 0))
        self.assertEqual(next_version.Version(1, 2, 3).bump("minor"), next_version.Version(1, 3, 0))
        self.assertEqual(next_version.Version(1, 2, 3).bump("patch"), next_version.Version(1, 2, 4))
        self.assertEqual(next_version.Version(1, 2, 3).bump("none"), next_version.Version(1, 2, 3))
        with self.assertRaises(ValueError):
            next_version.Version(1, 2, 3).bump("banana")


class CommitParsingTests(unittest.TestCase):
    def test_conventional_header(self) -> None:
        commit = next_version.parse_commit("a" * 40, "feat(portal): add health widget", "")
        self.assertEqual(commit.type, "feat")
        self.assertEqual(commit.scope, "portal")
        self.assertEqual(commit.description, "add health widget")
        self.assertEqual(commit.bump_kind(next_version.Version(1, 0, 0)), "minor")

    def test_bang_is_breaking(self) -> None:
        commit = next_version.parse_commit("a" * 40, "fix!: remove legacy route", "")
        self.assertTrue(commit.breaking)
        self.assertEqual(commit.bump_kind(next_version.Version(1, 0, 0)), "major")

    def test_breaking_footer(self) -> None:
        commit = next_version.parse_commit(
            "a" * 40,
            "chore: prepare",
            "Some context.\n\nBREAKING CHANGE: the old endpoint is gone",
        )
        self.assertTrue(commit.breaking)
        self.assertEqual(commit.bump_kind(next_version.Version(0, 4, 0)), "minor")

    def test_non_conventional_message(self) -> None:
        commit = next_version.parse_commit("a" * 40, "Merge something", "")
        self.assertIsNone(commit.type)
        self.assertEqual(commit.bump_kind(next_version.Version(1, 0, 0)), "none")

    def test_parse_commit_log(self) -> None:
        raw = (
            "a" * 40 + "\x1ffeat: one\x1fbody\n\x1e"
            + "b" * 40 + "\x1ffix: two\x1f\x1e"
        )
        commits = next_version.parse_commit_log(raw)
        self.assertEqual([commit.type for commit in commits], ["feat", "fix"])


class ComputeNextTests(unittest.TestCase):
    def test_feature_minor(self) -> None:
        current = next_version.Version(1, 2, 3)
        commits = [next_version.parse_commit("a" * 40, "feat: new", "")]
        self.assertEqual(
            next_version.compute_next(current, commits, None),
            (next_version.Version(1, 3, 0), "minor"),
        )

    def test_fix_patch(self) -> None:
        current = next_version.Version(1, 2, 3)
        commits = [next_version.parse_commit("a" * 40, "fix: bug", "")]
        self.assertEqual(
            next_version.compute_next(current, commits, None),
            (next_version.Version(1, 2, 4), "patch"),
        )

    def test_no_release(self) -> None:
        current = next_version.Version(1, 2, 3)
        commits = [next_version.parse_commit("a" * 40, "docs: readme", "")]
        self.assertEqual(next_version.compute_next(current, commits, None), (current, "none"))

    def test_manual_override(self) -> None:
        current = next_version.Version(1, 2, 3)
        self.assertEqual(
            next_version.compute_next(current, [], "2.0.0"),
            (next_version.Version(2, 0, 0), "manual"),
        )
        self.assertEqual(
            next_version.compute_next(current, [], "v1.2.4"),
            (next_version.Version(1, 2, 4), "manual"),
        )

    def test_manual_must_be_newer(self) -> None:
        current = next_version.Version(1, 2, 3)
        for value in ("1.2.3", "v1.2.2", "1.2"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    next_version.compute_next(current, [], value)

    def test_build_payload_marks_no_release(self) -> None:
        current = next_version.Version(1, 2, 3)
        payload = next_version.build_payload(current, current, "none", [], None)
        self.assertFalse(payload["release"])
        self.assertEqual(payload["version"], "v1.2.3")
        self.assertEqual(payload["previous_version"], "v1.2.3")
        self.assertEqual(payload["commit_count"], 0)


class HighestVersionTests(unittest.TestCase):
    def test_ignores_invalid_tags(self) -> None:
        self.assertEqual(
            next_version.highest_version(["v1.2.3", "nightly", "v1.10.0", "v1.9.9"]),
            next_version.Version(1, 10, 0),
        )
        self.assertIsNone(next_version.highest_version(["nightly", "v1.2"]))


class GitHubOutputTests(unittest.TestCase):
    def test_writes_expected_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "output"
            next_version._write_github_output(path, {"version": "v1.2.3", "release": "true"})
            self.assertEqual(path.read_text(encoding="utf-8"), "version=v1.2.3\nrelease=true\n")

    def test_rejects_newlines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                next_version._write_github_output(Path(tmp) / "output", {"version": "v1\nv2"})


class CliTests(unittest.TestCase):
    def _git(self, cwd: Path, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
        )

    def test_cli_computes_version_from_a_real_repository(self) -> None:
        try:
            self._git(Path(tempfile.gettempdir()), "--version")
        except (FileNotFoundError, subprocess.CalledProcessError):
            self.skipTest("git is not available")
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._git(repo, "init", "--quiet")
            self._git(repo, "config", "user.email", "tests@example.invalid")
            self._git(repo, "config", "user.name", "Tests")
            (repo / "file.txt").write_text("one\n", encoding="utf-8")
            self._git(repo, "add", "file.txt")
            self._git(repo, "commit", "--quiet", "-m", "chore: initial")
            self._git(repo, "tag", "v1.0.0")
            (repo / "file.txt").write_text("two\n", encoding="utf-8")
            self._git(repo, "commit", "--quiet", "-am", "feat: add a feature")

            output = repo / "github-output"
            old = os.environ.get("GITHUB_OUTPUT")
            os.environ["GITHUB_OUTPUT"] = str(output)
            try:
                self.assertEqual(
                    next_version.main(["--repo-root", str(repo), "--format", "github"]),
                    0,
                )
            finally:
                if old is None:
                    os.environ.pop("GITHUB_OUTPUT", None)
                else:
                    os.environ["GITHUB_OUTPUT"] = old
            values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
            self.assertEqual(values["version"], "v1.1.0")
            self.assertEqual(values["previous_version"], "v1.0.0")
            self.assertEqual(values["bump"], "minor")
            self.assertEqual(values["release"], "true")


if __name__ == "__main__":
    unittest.main()
