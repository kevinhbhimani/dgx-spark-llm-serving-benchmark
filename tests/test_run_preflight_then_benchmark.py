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
        shutil.copy2(ROOT / "utils/campaign_output.py", self.utils_directory)
        self.runner_path.chmod(0o755)

        self.preflight_stage_log = self.root / "preflight-stages.log"
        self.preflight_out_dir_log = self.root / "preflight-out-dir.log"
        self.benchmark_log = self.root / "benchmark.log"
        self.campaign_out_dir = self.root / "campaign"

        self._write_executable(
            self.scripts_directory / "preflight.sh",
            """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$OUT_DIR" > "$PREFLIGHT_OUT_DIR_LOG"
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
printf '%s\\n' "$OUT_DIR" "$PLAN_ONLY" "$STOP_AFTER_CANARY" \\
  "$ALLOW_CANARY_FAILURES" > "$BENCHMARK_LOG"
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
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "OUT_DIR": str(self.campaign_out_dir),
                "PYTHON": sys.executable,
                "PREFLIGHT_STAGE_LOG": str(self.preflight_stage_log),
                "PREFLIGHT_OUT_DIR_LOG": str(self.preflight_out_dir_log),
                "BENCHMARK_LOG": str(self.benchmark_log),
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
            f"{self.campaign_out_dir}.preflight",
        )
        self.assertEqual(
            self.preflight_stage_log.read_text(encoding="utf-8").splitlines(),
            list(PREFLIGHT_STAGES),
        )
        self.assertEqual(
            self.benchmark_log.read_text(encoding="utf-8").splitlines(),
            [str(self.campaign_out_dir), "0", "0", "0"],
        )


if __name__ == "__main__":
    unittest.main()
