from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class OfflineBundleTests(unittest.TestCase):
    def setUp(self: OfflineBundleTests) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="offline bundle tests ")
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.repository = self.root / "repository"
        self.offline_directory = self.repository / "offline"
        self.scripts_directory = self.repository / "scripts"
        self.commands_directory = self.root / "commands"
        for directory in (self.offline_directory, self.scripts_directory, self.commands_directory):
            directory.mkdir(parents=True)
        for name in ("build_bundle.sh", "Dockerfile.ray", "install_offline.sh", "source_release.py"):
            shutil.copy2(ROOT / "offline" / name, self.offline_directory / name)
        for name in ("load_env.sh", "runtime_defaults.sh", "ray-runtime-constraints.txt"):
            shutil.copy2(ROOT / "scripts" / name, self.scripts_directory / name)
        shutil.copy2(ROOT / "requirements.lock", self.repository / "requirements.lock")
        self.command_log = self.root / "commands.jsonl"
        self.output_directory = self.offline_directory / "offline-bundle"
        self._write_command(
            "docker",
            f"#!{sys.executable}\n" + r'''
import json
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
with Path(os.environ["COMMAND_LOG"]).open("a", encoding="utf-8") as command_log:
    command_log.write(json.dumps(["docker", *arguments]) + "\n")

if arguments == ["buildx", "version"]:
    print("synthetic Buildx version fixture")
elif arguments[:1] == ["info"]:
    if os.environ.get("DOCKER_UNAVAILABLE") == "1":
        raise SystemExit(1)
    print(os.environ.get("DOCKER_TEST_PLATFORM", "linux/aarch64"))
elif arguments == ["context", "show"]:
    print("fixture-context")
elif arguments[:2] == ["buildx", "inspect"]:
    if "--bootstrap" not in arguments and os.environ.get("EXISTING_BUILDER") != "1":
        raise SystemExit(1)
    print("Driver: " + os.environ.get("BUILDER_DRIVER", "docker-container"))
    print("Platforms: " + os.environ.get("BUILDER_PLATFORMS", "linux/amd64, linux/arm64"))
elif arguments[:2] == ["buildx", "create"]:
    print(arguments[arguments.index("--name") + 1])
elif arguments[:1] == ["pull"]:
    pass
elif arguments[:2] == ["image", "inspect"]:
    if "{{.Id}}" in arguments:
        print("sha256:" + ("1" if "--platform" in arguments else "0") * 64)
    else:
        image_kind = "DERIVED" if arguments[-1].startswith("dgx-spark-vllm-ray:") else "BASE"
        architecture = os.environ.get(image_kind + "_IMAGE_ARCH", "arm64")
        if "--platform" not in arguments:
            architecture = os.environ.get("UNQUALIFIED_IMAGE_ARCH", architecture)
        print(architecture)
elif arguments[:2] == ["buildx", "build"]:
    if os.environ.get("FAIL_BUILD") == "1":
        raise SystemExit(17)
    destination = arguments[arguments.index("--output") + 1].removeprefix("type=docker,dest=")
    Path(destination).write_bytes(b"synthetic Docker archive fixture\n")
elif arguments[:1] == ["load"]:
    if not Path(arguments[arguments.index("--input") + 1]).is_file():
        raise SystemExit("image archive is missing")
elif arguments[:1] == ["run"]:
    if os.environ.get("FAIL_RUN") == "1":
        raise SystemExit(19)
else:
    raise SystemExit("unexpected Docker command: " + repr(arguments))
''',
        )
        self._write_command(
            "python3",
            '#!/bin/sh\n'
            'if [ "$1" = -m ] && [ "$2" = pip ]; then\n'
            '  exec "$REAL_PYTHON" "$PIP_FIXTURE" "$@"\n'
            'fi\n'
            'exec "$REAL_PYTHON" "$@"\n',
        )
        self.pip_fixture = self.root / "pip_fixture.py"
        self.pip_fixture.write_text(
            r'''
import json
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
with Path(os.environ["COMMAND_LOG"]).open("a", encoding="utf-8") as command_log:
    command_log.write(json.dumps(["python3", *arguments]) + "\n")
destination = Path(arguments[arguments.index("--dest") + 1])
(destination / "fixture_client-1.0-py3-none-any.whl").write_bytes(b"synthetic wheel fixture\n")
''',
            encoding="utf-8",
        )

    def _write_command(self: OfflineBundleTests, name: str, contents: str) -> None:
        command = self.commands_directory / name
        command.write_text(contents, encoding="utf-8")
        command.chmod(0o755)

    def _run_builder(
        self: OfflineBundleTests,
        arguments: tuple[str, ...] = (),
        overrides: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            "PATH": str(self.commands_directory) + os.pathsep + os.defpath,
            "HOME": str(self.root),
            "ENV_FILE": str(self.root / "missing.env"),
            "COMMAND_LOG": str(self.command_log),
            "REAL_PYTHON": sys.executable,
            "PIP_FIXTURE": str(self.pip_fixture),
        }
        if overrides:
            environment.update(overrides)
        return subprocess.run(
            ["/bin/bash", str(self.offline_directory / "build_bundle.sh"), *arguments],
            cwd=self.repository,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )

    def _commands(self: OfflineBundleTests) -> list[list[str]]:
        if not self.command_log.exists():
            return []
        return [json.loads(line) for line in self.command_log.read_text(encoding="utf-8").splitlines()]

    def _assert_bundle_complete(self: OfflineBundleTests) -> None:
        result = subprocess.run(
            ["sha256sum", "-c", "SHA256SUMS"],
            cwd=self.output_directory,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "target_platform=linux/arm64\n",
            (self.output_directory / "BUNDLE_INFO").read_text(encoding="utf-8"),
        )
        self.assertEqual(list(self.offline_directory.glob(".offline-bundle.*")), [])

    def test_native_arm_build_needs_no_flag(self: OfflineBundleTests) -> None:
        result = self._run_builder()
        self.assertEqual(result.returncode, 0, result.stderr)
        self._assert_bundle_complete()
        commands = self._commands()
        self.assertTrue(any(command[:2] == ["docker", "run"] for command in commands))
        self.assertTrue(any(command[:3] == ["docker", "buildx", "create"] for command in commands))
        self.assertFalse(any("--use" in command for command in commands))

    def test_emulated_build_targets_arm_without_host_execution(self: OfflineBundleTests) -> None:
        result = self._run_builder(("--emulated",), {"DOCKER_TEST_PLATFORM": "linux/x86_64"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self._assert_bundle_complete()
        commands = self._commands()
        self.assertFalse(any(command[:2] == ["docker", "run"] for command in commands))
        self.assertFalse(any("--privileged" in command for command in commands))
        build_command = next(command for command in commands if command[:3] == ["docker", "buildx", "build"])
        self.assertEqual(build_command[build_command.index("--platform") + 1], "linux/arm64")
        self.assertIn("--builder", build_command)
        wheel_command = next(command for command in commands if command[:4] == ["python3", "-m", "pip", "download"])
        self.assertIn("--only-binary=:all:", wheel_command)
        self.assertIn("manylinux_2_28_aarch64", wheel_command)
        self.assertNotIn("manylinux_2_28_x86_64", wheel_command)
        self.assertIn("Ray import verified during the emulated image build", result.stdout)

    def test_cross_build_without_flag_stops_before_builder_creation(self: OfflineBundleTests) -> None:
        result = self._run_builder(overrides={"DOCKER_TEST_PLATFORM": "linux/amd64"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pass --emulated", result.stderr)
        self.assertFalse(any(command[:2] == ["docker", "pull"] for command in self._commands()))
        self.assertFalse(any(command[:3] == ["docker", "buildx", "create"] for command in self._commands()))
        self.assertFalse(self.output_directory.exists())

    def test_emulated_inspection_selects_arm_variant(self: OfflineBundleTests) -> None:
        for index, unqualified_architecture in enumerate(("", "amd64")):
            with self.subTest(unqualified_architecture=unqualified_architecture):
                output_directory = self.root / f"multiplatform-bundle-{index}"
                result = self._run_builder(("--emulated", str(output_directory)), {
                    "DOCKER_TEST_PLATFORM": "linux/amd64",
                    "UNQUALIFIED_IMAGE_ARCH": unqualified_architecture,
                })
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(
                    "base_image_id=sha256:" + "1" * 64 + "\n",
                    (output_directory / "BUNDLE_INFO").read_text(encoding="utf-8"),
                )
        inspections = [
            command for command in self._commands()
            if command[:3] == ["docker", "image", "inspect"]
        ]
        self.assertEqual(len(inspections), 6)
        for command in inspections:
            self.assertIn("--platform", command)
            self.assertEqual(command[command.index("--platform") + 1], "linux/arm64")

    def test_emulated_flag_rejects_native_arm_and_x86_target(self: OfflineBundleTests) -> None:
        for overrides in (
            {},
            {"DOCKER_TEST_PLATFORM": "linux/amd64", "TARGET_PLATFORM": "linux/amd64"},
        ):
            with self.subTest(overrides=overrides):
                result = self._run_builder(("--emulated",), overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("--emulated requires", result.stderr)
        self.assertFalse(any(command[:2] == ["docker", "pull"] for command in self._commands()))

    def test_native_x86_target_remains_supported(self: OfflineBundleTests) -> None:
        result = self._run_builder(overrides={
            "DOCKER_TEST_PLATFORM": "linux/amd64",
            "TARGET_PLATFORM": "linux/amd64",
            "BASE_IMAGE_ARCH": "amd64",
            "DERIVED_IMAGE_ARCH": "amd64",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("target_platform=linux/amd64\n", (self.output_directory / "BUNDLE_INFO").read_text())

    def test_existing_builder_is_reused(self: OfflineBundleTests) -> None:
        result = self._run_builder(overrides={"EXISTING_BUILDER": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(command[:3] == ["docker", "buildx", "create"] for command in self._commands()))

    def test_missing_arm_support_and_wrong_driver_stop_before_pull(self: OfflineBundleTests) -> None:
        for overrides, error in (
            ({"BUILDER_PLATFORMS": "linux/amd64, linux/arm/v7"}, "cannot execute linux/arm64"),
            ({"EXISTING_BUILDER": "1", "BUILDER_DRIVER": "docker"}, "must use the docker-container driver"),
        ):
            with self.subTest(overrides=overrides):
                result = self._run_builder(("--emulated",), {
                    "DOCKER_TEST_PLATFORM": "linux/amd64", **overrides,
                })
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error, result.stderr)
        self.assertFalse(any(command[:2] == ["docker", "pull"] for command in self._commands()))

    def test_arm_variant_is_accepted(self: OfflineBundleTests) -> None:
        result = self._run_builder(("--emulated",), {
            "DOCKER_TEST_PLATFORM": "linux/amd64",
            "BUILDER_PLATFORMS": "linux/amd64, linux/arm64/v8",
        })
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_wrong_base_or_derived_image_architecture_fails(self: OfflineBundleTests) -> None:
        for image_kind in ("BASE", "DERIVED"):
            with self.subTest(image_kind=image_kind):
                result = self._run_builder(overrides={image_kind + "_IMAGE_ARCH": "amd64"})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("image architecture is amd64, expected arm64", result.stderr)
                self.assertFalse(self.output_directory.exists())
                self.assertEqual(list(self.offline_directory.glob(".offline-bundle.*")), [])

    def test_failed_build_does_not_publish_bundle(self: OfflineBundleTests) -> None:
        result = self._run_builder(("--emulated",), {
            "DOCKER_TEST_PLATFORM": "linux/amd64", "FAIL_BUILD": "1",
        })
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertFalse(self.output_directory.exists())
        self.assertEqual(list(self.offline_directory.glob(".offline-bundle.*")), [])

    def test_native_ray_verification_failure_is_fatal(self: OfflineBundleTests) -> None:
        result = self._run_builder(overrides={"FAIL_RUN": "1"})
        self.assertEqual(result.returncode, 19, result.stderr)
        self.assertFalse(self.output_directory.exists())

    def test_existing_output_is_preserved_before_docker_access(self: OfflineBundleTests) -> None:
        self.output_directory.mkdir()
        existing_file = self.output_directory / "existing.txt"
        existing_file.write_text("preserve this file", encoding="utf-8")
        result = self._run_builder()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("output path already exists", result.stderr)
        self.assertEqual(existing_file.read_text(encoding="utf-8"), "preserve this file")
        self.assertFalse(any(command[:2] == ["docker", "info"] for command in self._commands()))

    def test_docker_access_failure_reports_error(self: OfflineBundleTests) -> None:
        result = self._run_builder(overrides={"DOCKER_UNAVAILABLE": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot access the Docker daemon", result.stderr)
        self.assertFalse(self.output_directory.exists())

    def test_emulated_build_can_include_existing_model_cache(self: OfflineBundleTests) -> None:
        cache_directory = self.root / "model-cache"
        hub_directory = cache_directory / "hub"
        hub_directory.mkdir(parents=True)
        (hub_directory / "fixture-model.txt").write_text("synthetic model cache fixture", encoding="utf-8")
        (hub_directory / "token").write_text("synthetic credential fixture", encoding="utf-8")
        result = self._run_builder(("--include-models", "--emulated"), {
            "DOCKER_TEST_PLATFORM": "linux/amd64", "MODEL_CACHE_DIR": str(cache_directory),
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self._assert_bundle_complete()
        with tarfile.open(self.output_directory / "model-cache.tar") as archive:
            self.assertIn("hub/fixture-model.txt", archive.getnames())
            self.assertNotIn("hub/token", archive.getnames())

    def test_help_lists_emulated_flag_without_docker_access(self: OfflineBundleTests) -> None:
        result = self._run_builder(("--help",))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--emulated", result.stdout)
        self.assertEqual(self._commands(), [])


if __name__ == "__main__":
    unittest.main()
