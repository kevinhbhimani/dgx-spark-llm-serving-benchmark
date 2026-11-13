import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from utils.campaign_output import fingerprint_identity, prepare_campaign


ROOT = Path(__file__).resolve().parents[1]


def write_test_topology(directory: Path) -> Path:
    hostname = socket.gethostname().split(".", maxsplit=1)[0]
    topology = directory / "cluster.yaml"
    topology.write_text(f"""schema_version: 1
head_node: spark_a
rendezvous_rail: rail_0
ray_port: 6379
nodes:
  - id: spark_a
    hostname: {hostname}
    ssh_target: {hostname}
    interfaces:
      - rail: rail_0
        name: cx0
        ipv4: 192.0.2.10/24
  - id: spark_b
    hostname: spark-b
    ssh_target: spark-b
    interfaces:
      - rail: rail_0
        name: cx0
        ipv4: 192.0.2.11/24
""")
    return topology


class RunJobContractTests(unittest.TestCase):
    def test_explicit_out_dir_rejects_incompatible_campaign_in_plan_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            output = directory / "campaign"
            prepare_campaign(output, fingerprint_identity([], ["run=previous"]))
            marker = output / "results" / "campaign_run.json"
            marker_before = marker.read_bytes()
            environment = os.environ.copy()
            environment.pop("SINGLE_NODE_ONLY", None)
            environment.pop("DGX_SPARK_ENV_LOADED", None)
            environment.update({
                "PYTHON": sys.executable,
                "TOPOLOGY_PYTHON": sys.executable,
                "PLAN_ONLY": "1",
                "OUT_DIR": str(output),
                "CLUSTER_CONFIG": str(write_test_topology(directory)),
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

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not a compatible resume", result.stderr)
            self.assertEqual(marker.read_bytes(), marker_before)
            self.assertFalse((output / "logs").exists())

    def test_plan_honors_model_yaml_and_clears_other_inherited_overrides(self):
        with tempfile.TemporaryDirectory() as temporary:
            topology = write_test_topology(Path(temporary))
            model_yaml = Path(temporary) / "alternate-models.yaml"
            model_yaml.write_bytes((ROOT / "configs/models.yaml").read_bytes())
            environment = os.environ.copy()
            environment.pop("SINGLE_NODE_ONLY", None)
            environment.pop("DGX_SPARK_ENV_LOADED", None)
            environment.update({
                "PYTHON": sys.executable,
                "TOPOLOGY_PYTHON": sys.executable,
                "PLAN_ONLY": "1",
                "OUT_DIR": str(Path(temporary) / "campaign"),
                "CLUSTER_CONFIG": str(topology),
                "CONCURRENCIES": "1 2",
                "EXPECTED_CONCURRENCIES": "2 1",
                "MODELS_YAML": str(model_yaml),
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
        self.assertIn(f"MODELS_YAML={model_yaml}", result.stdout)
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
            topology = write_test_topology(Path(temporary))
            environment = os.environ.copy()
            environment.pop("SINGLE_NODE_ONLY", None)
            environment.pop("DGX_SPARK_ENV_LOADED", None)
            for name in ("FULL_REPEATS", "REPEAT_CONCS", "REPEAT_TOTAL",
                         "CAMPAIGN_CONFIGS", "CAMPAIGN_LABELS", "REPEATS"):
                environment.pop(name, None)
            environment.update({"PYTHON": sys.executable,
                                "TOPOLOGY_PYTHON": sys.executable,
                                "PLAN_ONLY": "1",
                                "OUT_DIR": str(Path(temporary) / "campaign"),
                                "CLUSTER_CONFIG": str(topology), **overrides})
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

    def test_solo_plan_does_not_read_cluster_yaml(self):
        result = self._plan(
            SINGLE_NODE_ONLY="1",
            CLUSTER_CONFIG="/does/not/exist/cluster.yaml",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("configs=single", result.stdout)
        self.assertIn("SINGLE_NODE_ONLY=1", result.stdout)

    def test_solo_rejects_dual_configs_before_campaign_planning(self):
        result = self._plan(SINGLE_NODE_ONLY="1", CAMPAIGN_CONFIGS="tp2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CAMPAIGN_CONFIGS", result.stderr)

    def test_solo_rejects_worker_purge_before_campaign_planning(self):
        result = self._plan(SINGLE_NODE_ONLY="1", PURGE_WORKER="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PURGE_WORKER=1", result.stderr)

    def _run_with_dead_worker(self, function_name: str, call: str) -> subprocess.CompletedProcess[str]:
        """Run one run_job.sh function while acquire/mirror and purge both
        fail, as they do after the worker node stops."""
        source = (ROOT / "scripts/run_job.sh").read_text()
        start = source.index(f"\n{function_name}() {{\n") + 1
        end = source.index("\n}\n", start) + 3
        script = (
            "set -uo pipefail\n"
            'log() { echo "[job] $*"; }\n'
            "ray_ok() { return 0; }\n"
            'run_child() { "$@"; }\n'
            "acquire_group() { return 1; }\n"
            "purge_group() { return 1; }\n"
            "group_status() { return 3; }\n"
            "PURGE_WEIGHTS=1\n"
            "SINGLE_NODE_ONLY=0\n"
            "FAILED=()\n"
            "MODEL_GROUPS=('model-a|1|1:tp2:model-a')\n"
            + source[start:end]
            + call + "\n"
        )
        return subprocess.run(
            ["bash", "-c", script], cwd=ROOT, text=True,
            capture_output=True, timeout=15, check=False,
        )

    def test_acquire_failure_is_logged_before_a_purge_abort(self):
        for function_name, call in (
            ("canary_pass", "canary_pass"),
            ("measure_group", "measure_group model-a 1 1:tp2:model-a"),
        ):
            with self.subTest(function=function_name):
                result = self._run_with_dead_worker(function_name, call)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("acquire/mirror FAILED for model-a", result.stdout)
                self.assertLess(result.stdout.index("acquire/mirror FAILED"),
                                result.stdout.index("cache purge failed"))


if __name__ == "__main__":
    unittest.main()
