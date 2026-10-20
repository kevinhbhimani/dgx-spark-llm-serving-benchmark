"""Regression coverage for launch-level replication and pre-launch validation."""
from __future__ import annotations

import csv
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

from scripts import validate_campaign as validator
from scripts import validate_microbench as microbench

ROOT = Path(__file__).resolve().parents[1]


class PlanningReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.models = self.root / "models.yaml"
        self.experiment = self.root / "experiment.yaml"
        self.model = {
            "name": "org/model", "label": "model", "revision": "a" * 40,
            "configs": ["single", "tp2", "pp2"], "max_model_len": 64,
        }
        self.configuration = {
            "buckets": [{"input_len": 16, "output_len": 16}], "measure_s": 10,
        }

    def plan(self):
        self.models.write_text(yaml.safe_dump({"models": [self.model]}))
        self.experiment.write_text(yaml.safe_dump(self.configuration))
        return validator.plan_campaign(self.models, self.experiment, [1, 2], 3)

    def test_plan_exercises_full_measurement_configuration(self):
        cases = (
            {"measure_s": -1}, {"measure_s": float("nan")},
            {"measure_seconds": 10}, {"mode": "invalid"},
            {"min_window_requests": 20, "max_measure_s": 5},
            {"require_power_data": True, "power_backend": "none"},
            {"require_vllm_metrics_data": True, "scrape_vllm_metrics": False},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                original = self.configuration.copy()
                self.configuration.update(overrides)
                with self.assertRaises(validator.CampaignValidationError):
                    self.plan()
                self.configuration = original

    def test_trace_context_limit_uses_requests_instead_of_bucket_maximum(self):
        self.configuration["trace_path"] = "trace.jsonl"
        trace = self.root / "trace.jsonl"
        trace.write_text('{"input_len":64,"output_len":1}\n')
        with self.assertRaisesRegex(validator.CampaignValidationError, "needs 65 tokens"):
            self.plan()
        # The trace overrides the buckets, so a smaller trace is also valid
        # when an unused bucket exceeds the configured context limit.
        self.configuration["buckets"][0]["input_len"] = 128
        trace.write_text('{"input_len":8,"max_tokens":8}\n')
        self.assertEqual(self.plan()["largest_request_tokens"], 16)

    def test_plan_rejects_malformed_or_empty_trace_before_a_launch(self):
        self.configuration["trace_path"] = "trace.jsonl"
        trace = self.root / "trace.jsonl"
        for content in ("", "\n", "not json\n", "[]\n", '{"input_len":1.5,"output_len":1}\n'):
            with self.subTest(content=content):
                trace.write_text(content)
                with self.assertRaises(validator.CampaignValidationError):
                    self.plan()

    def test_scheduler_limit_is_positive_fixed_model_policy(self):
        self.model["max_num_seqs"] = 4
        self.plan()
        specs = validator.validate_models_yaml(self.models)
        self.assertEqual(specs[0].max_num_seqs, 4)
        for config in self.model["configs"]:
            result = subprocess.run(
                [sys.executable, "scripts/models_for_config.py", str(self.models), config],
                cwd=ROOT, text=True, capture_output=True, check=True,
            )
            self.assertEqual(result.stdout.strip().split("|")[-1], "4")
        for value in (0, -1, True, None, "4", 4.5, {}):
            with self.subTest(value=value):
                self.model["max_num_seqs"] = value
                with self.assertRaisesRegex(validator.CampaignValidationError, "max_num_seqs"):
                    self.plan()


class MicrobenchArtifactTests(unittest.TestCase):
    @staticmethod
    def write_interconnect(path, *, transport="roce", mutate=None):
        rows = []
        for collective in ("allreduce", "sendrecv"):
            for size in validator.ALLREDUCE_SIZES:
                rows.append({
                    "collective": collective, "transport": transport,
                    "bytes": size, "avg_s": 0.0001, "min_s": 0.00009,
                    "algbw_GBps": 0, "busbw_GBps": 0,
                })
        if mutate:
            mutate(rows)
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=microbench.INTERCONNECT_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def test_valid_complete_artifact_accepts_rounded_zero_bandwidth(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "interconnect.csv"
            for mode, transport in (("roce", "roce"), ("tcp", "tcp-tuned-x4")):
                self.write_interconnect(path, transport=transport)
                microbench.validate_interconnect_csv(path, mode)

    def test_incomplete_mixed_and_nonfinite_artifacts_are_rejected(self):
        mutations = (
            lambda rows: rows.pop(),
            lambda rows: rows[0].update(transport="tcp"),
            lambda rows: rows[0].update(transport="roce?"),
            lambda rows: rows[0].update(avg_s=float("inf")),
            lambda rows: rows[0].update(min_s=1),
            lambda rows: rows[0].update(busbw_GBps=-1),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "interconnect.csv"
            for mutate in mutations:
                with self.subTest(mutate=mutate):
                    self.write_interconnect(path, mutate=mutate)
                    with self.assertRaises(validator.CampaignValidationError):
                        microbench.validate_interconnect_csv(path)
            self.write_interconnect(path, transport="tcp")
            with self.assertRaisesRegex(validator.CampaignValidationError, "differs from expected"):
                microbench.validate_interconnect_csv(path, "roce")

    def _exercise_gate(self, kind, *, matching=True, transport="roce"):
        function = "allreduce_preflight" if kind == "allreduce" else "sendrecv_once"
        source = (ROOT / "scripts/run_job.sh").read_text()
        body = source.split(f"{function}() {{", 1)[1].split("\n}\n", 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            scripts = work / "scripts"
            scripts.mkdir()
            for script in ("validate_campaign.py", "validate_microbench.py"):
                (scripts / script).symlink_to(ROOT / "scripts" / script)
            name = "ray_allreduce_bench.sh" if kind == "allreduce" else "interconnect_extra_bench.sh"
            wrapper = scripts / name
            wrapper.write_text('#!/bin/bash\nif [[ "${1:-}" == "--fingerprint-only" ]]; then echo identity; else echo RERUN; fi\n')
            wrapper.chmod(0o755)
            out = work / "results"
            out.mkdir()
            artifact_id = "identity" if matching else "old-identity"
            if kind == "allreduce":
                artifact = out / f"allreduce_ray_{artifact_id}_fixture.csv"
                artifact.write_text(
                    ",".join(validator.ALLREDUCE_FIELDS) + "\n" + "".join(
                        f"{size},0.0001,0.00009,0.000,0.000\n"
                        for size in validator.ALLREDUCE_SIZES
                    )
                )
            else:
                self.write_interconnect(out / f"interconnect_extra_{artifact_id}_fixture.csv", transport=transport)
            environment = os.environ.copy()
            environment.update(OUT_DIR=str(out), PYTHON=sys.executable, NCCL_IB_DISABLE="0")
            result = subprocess.run(
                ["bash", "-c", f"log() {{ :; }}\n{function}() {{{body}\n}}\n{function}"],
                cwd=work, env=environment, text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

    def test_allreduce_preflight_always_runs_with_matching_saved_artifact(self):
        self.assertIn("RERUN", self._exercise_gate("allreduce"))

    def test_interconnect_reuse_preserves_valid_small_transfer_rounding(self):
        self.assertNotIn("RERUN", self._exercise_gate("interconnect"))

    def test_stale_or_wrong_transport_interconnect_artifact_is_not_reused(self):
        self.assertIn("RERUN", self._exercise_gate("interconnect", matching=False))
        self.assertIn("RERUN", self._exercise_gate("interconnect", transport="tcp"))


class ReplicateLaunchTests(unittest.TestCase):
    def _launch_ids(self, **overrides):
        source = (ROOT / "scripts/run_models.sh").read_text()
        body = "FAILURES=0" + source.split("\nFAILURES=0", 1)[1]
        environment = os.environ.copy()
        # STATUS_MISSING is initialized above the extracted loop.
        environment.update(
            REPEATS="3", ONLY_REPEAT="", STATUS_ONLY="0", CANARY_ONLY="0",
            STATUS_MISSING="0", CONFIG="single", OUT_DIR="results",
            LAST_SERVER_LOG="",
        )
        environment.update(overrides)
        result = subprocess.run(
            ["bash", "-c", 'set -euo pipefail\nENTRIES=("m|org/model|revision|64|0.8|||||4")\n'
             'run_one() { printf "LAUNCH=%s LIMIT=%s\\n" "$ONLY_REPEAT" "${10}"; }\n' + body],
            cwd=ROOT, env=environment, text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return [line for line in result.stdout.splitlines() if line.startswith("LAUNCH=")]

    def test_direct_wrapper_starts_one_launch_per_replicate(self):
        self.assertEqual(self._launch_ids(), [f"LAUNCH={i} LIMIT=4" for i in (1, 2, 3)])
        self.assertEqual(self._launch_ids(ONLY_REPEAT="2"), ["LAUNCH=2 LIMIT=4"])

    def test_status_and_canary_are_not_repeated(self):
        self.assertEqual(self._launch_ids(STATUS_ONLY="1"), ["LAUNCH= LIMIT=4"])
        self.assertEqual(self._launch_ids(CANARY_ONLY="1"), ["LAUNCH= LIMIT=4"])


if __name__ == "__main__":
    unittest.main()
