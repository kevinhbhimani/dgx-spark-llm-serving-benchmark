import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RunJobContractTests(unittest.TestCase):
    def test_plan_clears_inherited_campaign_overrides(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = os.environ.copy()
            environment.update({
                "PYTHON": "/bin/true",
                "PLAN_ONLY": "1",
                "OUT_DIR": temporary,
                "CONCURRENCIES": "1 2",
                "EXPECTED_CONCURRENCIES": "2 1",
                "MODELS_YAML": "/poison/models.yaml",
                "CONFIG_YAML": "/poison/experiment.yaml",
                "POWER_BACKEND": "none",
                "CANARY_ONLY": "1",
                "MODEL_LABELS": "one-model-only",
            })
            result = subprocess.run(
                ["bash", "scripts/run_job.sh"],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MODELS_YAML=configs/models.yaml", result.stdout)
        self.assertIn("CONFIG_YAML=configs/experiment.yaml", result.stdout)
        self.assertIn("POWER_BACKEND=<experiment.yaml>", result.stdout)
        self.assertIn("MODEL_LABELS=<all>", result.stdout)
        self.assertIn("CANARY_ONLY=0", result.stdout)
        self.assertIn("expected concurrency order: 1 2", result.stdout)
        # Replicates are the default design, not an opt-in extra rectangle.
        self.assertIn("REPEATS=3 independent launches", result.stdout)
        self.assertIn("PURGE_WEIGHTS=1", result.stdout)

    def _plan(self, **overrides):
        with tempfile.TemporaryDirectory() as temporary:
            environment = os.environ.copy()
            for name in ("FULL_REPEATS", "REPEAT_CONCS", "REPEAT_TOTAL",
                         "CAMPAIGN_CONFIGS", "CAMPAIGN_LABELS", "REPEATS"):
                environment.pop(name, None)
            environment.update({"PYTHON": "/bin/true", "PLAN_ONLY": "1",
                                "OUT_DIR": temporary, **overrides})
            return subprocess.run(
                ["bash", "scripts/run_job.sh"], cwd=ROOT, env=environment,
                text=True, capture_output=True, timeout=15, check=False,
            )

    def test_retired_repeat_knobs_fail_closed(self):
        # A stale FULL_REPEATS=1 must not silently be read as "one repeat".
        for name in ("FULL_REPEATS", "REPEAT_CONCS", "REPEAT_TOTAL"):
            with self.subTest(name=name):
                result = self._plan(**{name: "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("replaced by REPEATS", result.stderr)

    def test_campaign_subset_knobs_are_validated(self):
        result = self._plan(CAMPAIGN_CONFIGS="tp2 tp4")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CAMPAIGN_CONFIGS", result.stderr)

        result = self._plan(CAMPAIGN_CONFIGS="tp2 pp2",
                            CAMPAIGN_LABELS="llama3.1-8b")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("selection: labels=llama3.1-8b configs=tp2 pp2",
                      result.stdout)

        result = self._plan(REPEATS="0")
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
