from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from bench import provenance
from scripts.contracts.source_policy import skip_git_check


ROOT = Path(__file__).resolve().parents[1]


class SourcePolicyTests(unittest.TestCase):
    def test_bypass_never_invokes_git(self: SourcePolicyTests) -> None:
        with mock.patch.dict(os.environ, {"SKIP_GIT_CHECK": "1"}), mock.patch.object(provenance.subprocess, "run") as command:
            self.assertTrue(skip_git_check())
            self.assertIsNone(provenance.git_commit())
            command.assert_not_called()

    def test_invalid_bypass_is_rejected(self: SourcePolicyTests) -> None:
        for value in ("", "true", "2"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"SKIP_GIT_CHECK": value}):
                with self.assertRaisesRegex(ValueError, "SKIP_GIT_CHECK"):
                    provenance.git_commit()

    def test_campaign_source_gate_checks_hashes_without_git(self: SourcePolicyTests) -> None:
        source = (ROOT / "scripts/run_job.sh").read_text()
        function = source[source.index("source_tree_ok() {"):source.index("cluster_runtime_ok() {")]
        fingerprint = provenance.source_fingerprint()
        script = 'log() { echo "$*"; }\n' + function + "\nsource_tree_ok\n"
        with tempfile.TemporaryDirectory() as temporary:
            environment = os.environ.copy()
            environment.update({"SKIP_GIT_CHECK": "1", "SOURCE_IDENTITY": fingerprint, "HERE": str(ROOT), "PYTHON": sys.executable})
            result = subprocess.run(["bash", "-c", script], cwd=temporary, env=environment, capture_output=True, text=True, check=False, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Git requirement bypassed", result.stdout)
            environment["SOURCE_IDENTITY"] = "0" * 64
            result = subprocess.run(["bash", "-c", script], cwd=temporary, env=environment, capture_output=True, text=True, check=False, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("source changed during this campaign", result.stdout)
            environment["SKIP_GIT_CHECK"] = "0"
            result = subprocess.run(["bash", "-c", script], cwd=temporary, env=environment, capture_output=True, text=True, check=False, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no recoverable Git commit", result.stdout)

    def test_real_plan_runs_from_a_gitless_source_tree(self: SourcePolicyTests) -> None:
        with tempfile.TemporaryDirectory(prefix="gitless plan tests ") as temporary:
            directory = Path(temporary)
            repository = directory / "source"
            repository.mkdir()
            for name in ("bench", "scripts", "utils", "configs"):
                shutil.copytree(ROOT / name, repository / name, ignore=shutil.ignore_patterns("__pycache__", "results", "*.pyc"))
            for name in ("requirements.txt", "requirements.lock"):
                shutil.copy2(ROOT / name, repository / name)
            commands = directory / "commands"
            commands.mkdir()
            command_log = directory / "unexpected-command.log"
            for name in ("git", "docker", "ssh"):
                command = commands / name
                command.write_text('#!/bin/sh\nprintf called >> "$COMMAND_LOG"\nexit 91\n', encoding="utf-8")
                command.chmod(0o755)
            environment = os.environ.copy()
            environment.pop("DGX_SPARK_ENV_LOADED", None)
            environment.update({
                "PATH": str(commands) + os.pathsep + os.environ["PATH"],
                "COMMAND_LOG": str(command_log), "ENV_FILE": str(repository / "missing.env"),
                "PYTHON": sys.executable, "PYTHONPATH": str(repository),
                "PYTHONNOUSERSITE": "1", "SKIP_GIT_CHECK": "1",
                "SINGLE_NODE_ONLY": "1", "MODELS_ONDISK": "1", "PLAN_ONLY": "1",
                "CAMPAIGN_CONFIGS": "single", "CAMPAIGN_LABELS": "",
                "CONCURRENCIES": "1", "REPEATS": "1", "OUT_DIR": str(directory / "campaign"),
            })
            result = subprocess.run(
                ["bash", str(repository / "scripts/run_job.sh")], cwd=repository,
                env=environment, capture_output=True, text=True, check=False, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(command_log.exists(), "plan invoked Git, Docker, or SSH")
            self.assertIn("[plan]", result.stdout)
            self.assertFalse((directory / "campaign").exists())


if __name__ == "__main__":
    unittest.main()
