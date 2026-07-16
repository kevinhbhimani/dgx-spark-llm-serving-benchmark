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


if __name__ == "__main__":
    unittest.main()
