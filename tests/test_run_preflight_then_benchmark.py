from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT_STAGES: tuple[str, ...] = (
    "output-writes",
    "plan",
    "connectx",
    "hardware-smoke",
    "campaign-canary",
    "purge-models",
)


class SerialCampaignRunnerTests(unittest.TestCase):
    def setUp(self: SerialCampaignRunnerTests) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.fixture_root = self.root / "repo"
        self.scripts_directory = self.fixture_root / "scripts"
        self.utils_directory = self.fixture_root / "utils"
        self.scripts_directory.mkdir(parents=True)
        self.utils_directory.mkdir()

        self.runner_path = self.scripts_directory / "run_preflight_then_benchmark.sh"
        shutil.copy2(ROOT / "scripts/run_preflight_then_benchmark.sh", self.runner_path)
        shutil.copy2(ROOT / "scripts/load_env.sh", self.scripts_directory)
        shutil.copy2(ROOT / "utils/campaign_output.py", self.utils_directory)
        self.runner_path.chmod(0o755)

        self.preflight_stage_log = self.root / "preflight-stages.log"
        self.preflight_out_dir_log = self.root / "preflight-out-dir.log"
        self.benchmark_log = self.root / "benchmark.log"
        self.mode_log = self.root / "mode.log"
        self.git_check_log = self.root / "git-check.log"
        self.campaign_out_dir = self.root / "campaign"

        self._write_executable(
            self.scripts_directory / "preflight.sh",
            """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$OUT_DIR" > "$PREFLIGHT_OUT_DIR_LOG"
printf '%s\\n' "$SINGLE_NODE_ONLY" > "$MODE_LOG"
printf '%s\\n' "$SKIP_GIT_CHECK" >> "$GIT_CHECK_LOG"
for stage in output-writes plan connectx hardware-smoke campaign-canary purge-models; do
  printf '%s\\n' "$stage" >> "$PREFLIGHT_STAGE_LOG"
  if [[ "${FAIL_STAGE:-}" == "$stage" ]]; then
    exit "$FAIL_STATUS"
  fi
done
""",
        )
        self._write_executable(
            self.scripts_directory / "run_job.sh",
            """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$SKIP_GIT_CHECK" >> "$GIT_CHECK_LOG"
printf '%s\\n' "$OUT_DIR" "$PLAN_ONLY" "$STOP_AFTER_CANARY" \\
  "$ALLOW_CANARY_FAILURES" "$SINGLE_NODE_ONLY" > "$BENCHMARK_LOG"
""",
        )

    def tearDown(self: SerialCampaignRunnerTests) -> None:
        self.temporary_directory.cleanup()

    def _write_executable(
        self: SerialCampaignRunnerTests, path: Path, contents: str
    ) -> None:
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o755)

    def _run_runner(
        self: SerialCampaignRunnerTests,
        *,
        fail_stage: str = "",
        fail_status: int = 1,
        single_node_only: str | None = None,
        campaign_configs: str | None = None,
        purge_worker: str | None = None,
        dotenv_mode: str | None = None,
        skip_git_check: str | None = None,
        dotenv_git_check: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.pop("SINGLE_NODE_ONLY", None)
        environment.pop("DGX_SPARK_ENV_LOADED", None)
        environment.pop("CAMPAIGN_CONFIGS", None)
        environment.pop("SKIP_GIT_CHECK", None)
        environment["ENV_FILE"] = str(self.fixture_root / ".env")
        if dotenv_mode is not None:
            (self.fixture_root / ".env").write_text(
                f"SINGLE_NODE_ONLY={dotenv_mode}\\n", encoding="utf-8"
            )
        else:
            (self.fixture_root / ".env").unlink(missing_ok=True)
        if dotenv_git_check is not None:
            with (self.fixture_root / ".env").open("a", encoding="utf-8") as dotenv:
                dotenv.write(f"SKIP_GIT_CHECK={dotenv_git_check}\n")
        if skip_git_check is not None:
            environment["SKIP_GIT_CHECK"] = skip_git_check
        if single_node_only is not None:
            environment["SINGLE_NODE_ONLY"] = single_node_only
        if campaign_configs is not None:
            environment["CAMPAIGN_CONFIGS"] = campaign_configs
        if purge_worker is not None:
            environment["PURGE_WORKER"] = purge_worker
        environment.update(
            {
                "OUT_DIR": str(self.campaign_out_dir),
                "PYTHON": sys.executable,
                "PREFLIGHT_STAGE_LOG": str(self.preflight_stage_log),
                "PREFLIGHT_OUT_DIR_LOG": str(self.preflight_out_dir_log),
                "BENCHMARK_LOG": str(self.benchmark_log),
                "MODE_LOG": str(self.mode_log),
                "GIT_CHECK_LOG": str(self.git_check_log),
                "FAIL_STAGE": fail_stage,
                "FAIL_STATUS": str(fail_status),
            }
        )
        return subprocess.run(
            [str(self.runner_path)],
            cwd=self.fixture_root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_preflight_failures_stop_before_the_benchmark(
        self: SerialCampaignRunnerTests,
    ) -> None:
        failure_cases: tuple[tuple[str, int], ...] = (
            ("output-writes", 11),
            ("plan", 12),
            ("connectx", 13),
            ("hardware-smoke", 14),
            ("campaign-canary", 15),
            ("purge-models", 16),
        )
        for fail_stage, expected_status in failure_cases:
            with self.subTest(fail_stage=fail_stage):
                self.preflight_stage_log.unlink(missing_ok=True)
                self.preflight_out_dir_log.unlink(missing_ok=True)
                self.benchmark_log.unlink(missing_ok=True)

                result = self._run_runner(
                    fail_stage=fail_stage,
                    fail_status=expected_status,
                )

                self.assertEqual(result.returncode, expected_status)
                self.assertIn("full benchmark was not started", result.stderr)
                self.assertEqual(
                    self.preflight_stage_log.read_text(encoding="utf-8").splitlines(),
                    list(PREFLIGHT_STAGES[: PREFLIGHT_STAGES.index(fail_stage) + 1]),
                )
                self.assertFalse(self.benchmark_log.exists())

    def test_successful_preflight_starts_full_benchmark_with_campaign_settings(
        self: SerialCampaignRunnerTests,
    ) -> None:
        result = self._run_runner()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            self.preflight_out_dir_log.read_text(encoding="utf-8").strip(),
            str(self.campaign_out_dir),
        )
        self.assertEqual(
            self.preflight_stage_log.read_text(encoding="utf-8").splitlines(),
            list(PREFLIGHT_STAGES),
        )
        self.assertEqual(
            self.benchmark_log.read_text(encoding="utf-8").splitlines(),
            [str(self.campaign_out_dir), "0", "0", "0", "0"],
        )

    def test_solo_mode_propagates_from_environment_or_dotenv(self) -> None:
        result = self._run_runner(single_node_only="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.mode_log.read_text(encoding="utf-8").strip(), "1")
        self.assertEqual(
            self.benchmark_log.read_text(encoding="utf-8").splitlines()[-1], "1"
        )

        result = self._run_runner(dotenv_mode="1", single_node_only="0")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.mode_log.read_text(encoding="utf-8").strip(), "0")

    def test_invalid_mode_and_dual_config_fail_before_preflight(self) -> None:
        for options in (
            {"single_node_only": ""},
            {"single_node_only": "false"},
            {"single_node_only": "1", "campaign_configs": "tp2"},
            {"single_node_only": "1", "purge_worker": "1"},
        ):
            with self.subTest(options=options):
                self.preflight_stage_log.unlink(missing_ok=True)
                result = self._run_runner(**options)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.preflight_stage_log.exists())

    def test_git_bypass_propagates_from_environment_and_dotenv(self: SerialCampaignRunnerTests) -> None:
        for options, expected in (
            ({"skip_git_check": "1"}, "1"),
            ({"dotenv_git_check": "1"}, "1"),
            ({"dotenv_git_check": "1", "skip_git_check": "0"}, "0"),
        ):
            with self.subTest(options=options):
                self.git_check_log.unlink(missing_ok=True)
                result = self._run_runner(**options)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.git_check_log.read_text().splitlines(), [expected, expected])

    def test_invalid_git_bypass_fails_before_preflight(self: SerialCampaignRunnerTests) -> None:
        for value in ("", "true", "2"):
            with self.subTest(value=value):
                result = self._run_runner(skip_git_check=value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SKIP_GIT_CHECK must be exactly 0 or 1", result.stderr)
                self.assertFalse(self.preflight_stage_log.exists())


if __name__ == "__main__":
    unittest.main()
