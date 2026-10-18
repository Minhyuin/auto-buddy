from __future__ import annotations

import argparse
import json

from scripts.auto_buddy_updater import (
    CommandResult,
    ConfigError,
    DeployError,
    ManualApprovalRequired,
    StateStore,
    VerificationError,
)
from tests_devops.support import (
    FakeHttp,
    FakeRunner,
    HttpResponse,
    TemporaryDirectoryTestCase,
    make_old_state,
    make_target,
    make_updater,
)

SQL_DUMP = b"\n".join(b"CREATE TABLE public.t%d (id integer);" % index for index in range(6))


class LiveTargetVerificationTests(TemporaryDirectoryTestCase):
    @staticmethod
    def labels(target) -> dict[str, str]:
        return {
            "org.opencontainers.image.version": target.release.tag,
            "org.opencontainers.image.revision": target.manifest["commit"],
            "org.auto-buddy.upstream-commit": target.manifest["upstream"]["commit"],
            "org.auto-buddy.migrations-backward-compatible": "true",
            "org.auto-buddy.manual-approval-required": "false",
        }

    def runner(self, target, container_image: str) -> FakeRunner:
        image_id = "sha256:" + "a" * 64
        labels = self.labels(target)

        def handler(command):
            joined = " ".join(command)
            if "image inspect" in joined:
                return CommandResult(
                    command,
                    0,
                    json.dumps([{"Id": image_id, "Config": {"Labels": labels}}]).encode(),
                    "",
                )
            if "ps --status running --quiet" in joined:
                return CommandResult(command, 0, f"{command[-1]}-container\n".encode(), "")
            if joined.startswith("docker inspect "):
                return CommandResult(
                    command,
                    0,
                    json.dumps(
                        [
                            {
                                "Id": command[-1],
                                "Image": container_image,
                                "Config": {"Labels": labels},
                            }
                        ]
                    ).encode(),
                    "",
                )
            return 0

        return FakeRunner(handler)

    def test_baseline_refuses_mismatched_live_image(self) -> None:
        target = make_target()
        runner = self.runner(target, "sha256:" + "b" * 64)
        updater = make_updater(self, runner, FakeHttp(lambda url, headers: HttpResponse(200, {}, b"{}", url)))
        updater.preflight = lambda: None
        updater._prepare_target = lambda release, allow_manual: target
        updater.github.release_by_tag = lambda tag: target.release
        args = argparse.Namespace(tag=target.release.tag, dry_run=False, allow_unsafe_migrations=False)

        with self.assertRaisesRegex(VerificationError, "running web container uses image"):
            updater.baseline(args)
        self.assertIsNone(updater.state.read("current.json"))

    def test_prepare_target_passes_unqualified_repository_to_cosign(self) -> None:
        target = make_target()
        entries = [{
            "critical": {"image": {"docker-manifest-digest": target.digest}},
            "optional": {
                "Issuer": "https://token.actions.githubusercontent.com",
                "Subject": "https://github.com/owner/auto-buddy/.github/workflows/release.yml@refs/tags/v1.2.3",
            },
        }]
        runner = FakeRunner(lambda command: (0, json.dumps(entries).encode(), ""))
        updater = make_updater(self, runner, FakeHttp(lambda url, headers: HttpResponse(200, {}, b"{}", url)))
        updater._download_manifest = lambda release: target.manifest
        updater.registry.verify = lambda digest: None
        updater._pull = lambda image: None
        updater._verify_labels = lambda *args: None

        prepared = updater._prepare_target(target.release, allow_manual=False)

        self.assertEqual(prepared.image, target.image)
        self.assertEqual(len(runner.calls), 1)
        command = runner.calls[0][0]
        self.assertEqual(command[-1], target.image)
        self.assertEqual(command[-1].count("@"), 1)

    def test_live_image_id_and_labels_match(self) -> None:
        target = make_target()
        image_id = "sha256:" + "a" * 64
        runner = self.runner(target, image_id)
        updater = make_updater(self, runner, FakeHttp(lambda url, headers: HttpResponse(200, {}, b"{}", url)))

        updater._verify_live_target(target)


class RecordingDeploy(TemporaryDirectoryTestCase):
    def runner(self, overrides=None):
        overrides = overrides or {}

        def handler(args):
            command = " ".join(args)
            for needle, result in overrides.items():
                if needle in command:
                    if isinstance(result, Exception):
                        raise result
                    if callable(result):
                        return result(args)
                    return result
            if "pg_dump" in command:
                return CommandResult(args, 0, SQL_DUMP, "")
            return 0

        return FakeRunner(handler)

    def http(self, statuses=(200,)):
        responses = list(statuses)

        def handler(url, headers):
            status = responses.pop(0) if len(responses) > 1 else responses[0]
            body = b'{"status": "ok"}' if status == 200 else b'{"status": "unavailable"}'
            return HttpResponse(status, {}, body, url)

        return FakeHttp(handler)

    @staticmethod
    def image(call) -> str:
        return (call[1] or {}).get("APP_IMAGE", "")

    def test_successful_deploy_sequence_and_state(self) -> None:
        config = self.make_config()
        runner = self.runner()
        updater = make_updater(self, runner, self.http(), config=config)
        target = make_target()
        old = make_old_state()
        updater.deploy(target, old)

        commands = runner.joined()
        self.assertTrue(commands)
        for command in commands:
            self.assertIn("--env-file", command)
        pause = next(i for i, command in enumerate(commands) if "worker_control pause" in command)
        drain = next(i for i, command in enumerate(commands) if "worker_control drained" in command)
        stop_worker = next(i for i, command in enumerate(commands) if "stop -t 650 worker" in command)
        stop_web = next(i for i, command in enumerate(commands) if "stop -t 30 web" in command)
        backup = next(i for i, command in enumerate(commands) if "pg_dump" in command)
        migrate = next(i for i, command in enumerate(commands) if "migrate --noinput" in command)
        web = next(
            i
            for i, command in enumerate(commands)
            if "up -d --no-build --no-deps web" in command
        )
        worker = next(
            i
            for i, command in enumerate(commands)
            if "up -d --no-build --no-deps worker" in command
        )
        resume = next(i for i, command in enumerate(commands) if "worker_control resume" in command)
        self.assertLess(pause, drain)
        self.assertLess(drain, stop_worker)
        self.assertLess(stop_worker, stop_web)
        self.assertLess(stop_web, backup)
        self.assertLess(backup, migrate)
        self.assertLess(migrate, web)
        self.assertLess(web, worker)
        self.assertLess(worker, resume)
        self.assertEqual(self.image(runner.calls[web]), target.image)
        self.assertEqual(self.image(runner.calls[migrate]), target.image)
        self.assertEqual(self.image(runner.calls[worker]), target.image)

        state = StateStore(config.state_dir)
        current = state.read("current.json")
        previous = state.read("previous.json")
        assert current is not None and previous is not None
        self.assertEqual(current["tag"], "v1.2.3")
        self.assertEqual(current["digest"], target.digest)
        self.assertEqual(previous["tag"], "v1.2.2")
        self.assertIsNone(state.read("failed.json"))
        backups = list(config.backup_dir.glob("auto-buddy-*.sql.gz"))
        self.assertEqual(len(backups), 1)
        self.assertIn("backup", current)

    def test_backup_failure_restores_services_without_recording_failed(self) -> None:
        config = self.make_config()
        runner = self.runner({"pg_dump": (1, b"", "dump failed")})
        updater = make_updater(self, runner, self.http(), config=config)
        with self.assertRaises(DeployError):
            updater.deploy(make_target(), make_old_state())
        commands = runner.joined()
        self.assertFalse(any("migrate --noinput" in command for command in commands))
        old = make_old_state()
        old_web = [
            call
            for command, call in zip(commands, runner.calls)
            if "up -d --no-build --no-deps web" in command and self.image(call) == old["image"]
        ]
        self.assertTrue(old_web)
        self.assertEqual(old_web[0][1]["APP_VERSION"], old["tag"])
        self.assertEqual(old_web[0][1]["VCS_REF"], old["commit"])
        self.assertEqual(old_web[0][1]["UPSTREAM_COMMIT"], old["upstream_commit"])
        self.assertTrue(any("worker_control resume" in command for command in commands))
        self.assertIsNone(StateStore(config.state_dir).read("failed.json"))
        self.assertIsNone(StateStore(config.state_dir).read("current.json"))

    def test_unsafe_migration_failure_requires_manual_recovery(self) -> None:
        config = self.make_config()
        runner = self.runner({"migrate --noinput": (1, b"", "migration failed")})
        updater = make_updater(self, runner, self.http(), config=config)
        target = make_target(backward=False)
        with self.assertRaises(ManualApprovalRequired):
            updater.deploy(target, make_old_state())
        commands = runner.joined()
        self.assertFalse(any("up -d --no-build --no-deps web" in command for command in commands))
        self.assertFalse(any("worker_control resume" in command for command in commands))
        manual = StateStore(config.state_dir).read("manual-required.json")
        assert manual is not None
        self.assertEqual(manual["tag"], "v1.2.3")
        self.assertEqual(manual["stage"], "migrate")

    def test_health_failure_rolls_back_when_manifest_is_backward_compatible(self) -> None:
        config = self.make_config(health_retries=2, health_interval_seconds=1)
        http = self.http((503, 503, 200))
        runner = self.runner()
        updater = make_updater(self, runner, http, config=config)
        with self.assertRaises(DeployError):
            updater.deploy(make_target(), make_old_state())
        commands = runner.joined()
        old = make_old_state()
        old_services = [
            (command, call)
            for command, call in zip(commands, runner.calls)
            if "up -d --no-build --no-deps" in command
            and ("web" in command or "worker" in command)
            and self.image(call) == old["image"]
        ]
        self.assertTrue(any("web" in command for command, _ in old_services))
        self.assertTrue(any("worker" in command for command, _ in old_services))
        for _, call in old_services:
            self.assertEqual(call[1]["APP_VERSION"], old["tag"])
            self.assertEqual(call[1]["VCS_REF"], old["commit"])
            self.assertEqual(call[1]["UPSTREAM_COMMIT"], old["upstream_commit"])
        failed = StateStore(config.state_dir).read("failed.json")
        assert failed is not None
        self.assertEqual(failed["stage"], "health")
        self.assertIsNone(StateStore(config.state_dir).read("current.json"))

    def test_health_failure_with_unsafe_migrations_stops_for_manual_recovery(self) -> None:
        config = self.make_config(health_retries=1)
        http = self.http((503,))
        updater = make_updater(self, self.runner(), http, config=config)
        target = make_target(backward=False)
        with self.assertRaises(ManualApprovalRequired):
            updater.deploy(target, make_old_state())
        commands = updater.runner.joined()
        self.assertFalse(any("worker_control resume" in command for command in commands))
        manual = StateStore(config.state_dir).read("manual-required.json")
        assert manual is not None
        self.assertEqual(manual["stage"], "health")

    def test_drain_failure_resumes_and_touches_nothing_else(self) -> None:
        config = self.make_config(drain_seconds=0)
        runner = self.runner({"worker_control drained": 1})
        updater = make_updater(self, runner, self.http(), config=config)
        with self.assertRaises(DeployError):
            updater.deploy(make_target(), make_old_state())
        commands = runner.joined()
        self.assertTrue(any("worker_control pause" in command for command in commands))
        self.assertTrue(any("worker_control resume" in command for command in commands))
        self.assertFalse(any("stop -t" in command for command in commands))
        self.assertFalse(any("pg_dump" in command for command in commands))
        self.assertIsNone(StateStore(config.state_dir).read("current.json"))

    def test_new_worker_failure_rolls_back(self) -> None:
        config = self.make_config()
        target = make_target()

        def handler(args, env):
            command = " ".join(args)
            if "pg_dump" in command:
                return CommandResult(args, 0, SQL_DUMP, "")
            if "up -d --no-build --no-deps worker" in command and (env or {}).get("APP_IMAGE") == target.image:
                return (1, b"", "worker start failed")
            return 0

        runner = FakeRunner(handler, with_env=True)
        updater = make_updater(self, runner, self.http(), config=config)
        with self.assertRaises(DeployError):
            updater.deploy(target, make_old_state())
        failed = StateStore(config.state_dir).read("failed.json")
        assert failed is not None
        self.assertEqual(failed["stage"], "start-worker")
        old_image = make_old_state()["image"]
        self.assertTrue(
            any(
                "up -d --no-build --no-deps web" in command and self.image(call) == old_image
                for command, call in zip(runner.joined(), runner.calls)
            )
        )

    def test_no_current_state_requires_baseline(self) -> None:
        updater = make_updater(self, self.runner(), self.http())
        args = type(
            "Args",
            (),
            {
                "tag": None,
                "retry_failed": False,
                "dry_run": True,
                "allow_unsafe_migrations": False,
            },
        )()
        with self.assertRaises(ConfigError):
            updater.update(args)
        self.assertEqual(updater.runner.joined(), [])
