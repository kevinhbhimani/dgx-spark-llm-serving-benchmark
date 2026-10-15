import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import yaml

from bench.config import ExperimentConfig
from bench.run_benchmark import (
    METHODOLOGY_VERSION,
    _capture_client_environment,
    _git_commit,
    _sha256_json,
    _source_fingerprint,
    _append_index,
    config_file_fingerprint,
    deployment_fingerprint,
)
from scripts import check_environment
from scripts import validate_campaign as validator


REVISION = "a" * 40
IMAGE = "nvcr.io/nvidia/vllm@sha256:" + "b" * 64


class CampaignFixture:
    _environment_manifest = None

    def __init__(self, root: Path, config: str = "single"):
        self.root = root
        self.config = config
        self.models = root / "models.yaml"
        self.experiment = root / "experiment.yaml"
        self.results = root / "results"
        self.results.mkdir()
        self.trace = root / "trace.jsonl"
        self.trace.write_text('{"input_len":8,"output_len":8}\n')
        self.models.write_text(
            yaml.safe_dump(
                {
                    "models": [
                        {
                            "name": "org/model",
                            "label": "model-a",
                            "revision": REVISION,
                            "configs": [config],
                            "max_model_len": 64,
                        }
                    ]
                }
            )
        )
        self.experiment.write_text(
            yaml.safe_dump(
                {
                    "buckets": [
                        {"input_len": 8, "output_len": 8, "weight": 1.0}
                    ],
                    "trace_path": self.trace.name,
                    "measure_s": 10.0,
                    "power_backend": "nvidia_smi",
                    "power_interval_ms": 100,
                    "scrape_vllm_metrics": True,
                    "require_power_data": True,
                    "require_vllm_metrics_data": True,
                    "require_provenance": True,
                    "require_zero_request_failures": True,
                    "min_successful_requests": 4,
                    "out_dir": str(self.results),
                }
            )
        )
        self.environment_path = self.results / "client_environment.json"
        self._write_environment_manifest()
        self.policy_path = None
        self.policy = None

    def _write_environment_manifest(self):
        if CampaignFixture._environment_manifest is None:
            lock = validator.ROOT / "requirements.lock"
            pins = check_environment.parse_lock(lock)
            issues, locked = check_environment.verify_locked_distributions(pins)
            if issues:
                raise RuntimeError("invalid locked test environment: " + "; ".join(issues))
            pip_check = check_environment.run_pip_check()
            issue = check_environment.pip_check_issue(pip_check)
            if issue is not None:
                raise RuntimeError(issue)
            CampaignFixture._environment_manifest = check_environment.build_manifest(
                lock, locked, pip_check
            )
        check_environment.atomic_write_json(
            self.environment_path, CampaignFixture._environment_manifest
        )

    def _write_serving_policy(self, cfg):
        runtime_facts = {
            "gpus": [
                {
                    "compute_capability": "12.1",
                    "driver": "fixture-driver",
                    "gpu_name": "NVIDIA GB10",
                    "gpu_uuid": "GPU-fixture",
                    "max_graphics_clock_mhz": "2500",
                    "power_limit_w": "N/A",
                }
            ],
            "query_fields": [
                "name",
                "uuid",
                "driver_version",
                "compute_cap",
                "clocks.max.graphics",
                "power.limit",
            ],
            "source": "test-fixture",
        }
        runtime_hash = hashlib.sha256(
            json.dumps(runtime_facts, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        policy = {
            "cluster_runtime": runtime_hash,
            "config": self.config,
            "connectx_iface": "enp1s0f0np0",
            "dtype": "auto",
            "generation_config": "vllm",
            "gpu_util": "0.8",
            "head_ip": "192.0.2.10",
            "hf_hub_offline": "1",
            "image": IMAGE,
            "max_model_len": "64",
            "model": "org/model",
            "nccl_ib_disable": "1",
            "power_backend": "nvidia_smi",
            "prefix_caching": "0",
            "quant": "",
            "ray_version": "2.54.0",
            "revision": REVISION,
            "runtime_facts": runtime_facts,
            "runtime_facts_sha256": runtime_hash,
            "runtime_scope": "single-host" if self.config == "single" else "dual-cluster",
            "schema": "serving-v1",
            "tokenizer": "org/model",
            "tokenizer_revision": REVISION,
            "torchinductor_autograd_cache": "0",
            "trust_remote_code": "false",
            "worker_ip": "192.0.2.11",
        }
        legacy_input = "".join(
            (
                f"schema={policy['schema']}\n",
                f"config={policy['config']}\n",
                f"image={policy['image']}\n",
                f"model={policy['model']}\nrevision={policy['revision']}\n",
                f"tokenizer={policy['tokenizer']}\n"
                f"tokenizer_revision={policy['tokenizer_revision']}\n",
                f"dtype={policy['dtype']}\nquant={policy['quant']}\n",
                f"max_model_len={policy['max_model_len']}\n"
                f"gpu_util={policy['gpu_util']}\n",
                f"prefix_caching={policy['prefix_caching']}\n"
                f"generation_config={policy['generation_config']}\n",
                "torchinductor_autograd_cache="
                f"{policy['torchinductor_autograd_cache']}\n"
                f"hf_hub_offline={policy['hf_hub_offline']}\n",
                f"trust_remote_code={policy['trust_remote_code']}\n"
                f"ray_version={policy['ray_version']}\n",
                f"connectx_iface={policy['connectx_iface']}\n"
                f"nccl_ib_disable={policy['nccl_ib_disable']}\n",
                f"head_ip={policy['head_ip']}\nworker_ip={policy['worker_ip']}\n"
                f"power_backend={policy['power_backend']}\n",
                f"cluster_runtime={runtime_hash}\n",
            )
        )
        policy["serving_fingerprint"] = hashlib.sha256(
            legacy_input.encode()
        ).hexdigest()
        encoded = (json.dumps(policy, indent=2, sort_keys=True) + "\n").encode()
        self.policy_path = self.results / (
            f"serving_policy_model-a_{self.config}_"
            f"{policy['serving_fingerprint']}.json"
        )
        self.policy_path.write_bytes(encoded)
        self.policy = policy
        cfg.serving_fingerprint = policy["serving_fingerprint"]
        return policy, {
            "path": self.policy_path.name,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "bytes": len(encoded),
        }

    def mutate_index(self, **updates):
        index = self.results / "index.csv"
        with index.open(newline="") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            fieldnames = reader.fieldnames
        rows[0].update({key: str(value) for key, value in updates.items()})
        with index.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def metrics():
        common = {
            "sample_count": 11,
            "sample_span_s": 10.0,
            "max_gap_s": 1.0,
        }
        return {
            "vllm:num_requests_running": dict(common, mean=1.0, max=1.0),
            "vllm:num_requests_waiting": dict(common, mean=0.0, max=0.0),
            "vllm:kv_cache_usage_perc": dict(common, mean=0.1, max=0.2),
            "vllm:prompt_tokens_total": dict(
                common, delta=80.0, rate_per_s=8.0
            ),
            "vllm:generation_tokens_total": dict(
                common, delta=40.0, rate_per_s=4.0
            ),
            "vllm:num_preemptions_total": dict(common, delta=0.0),
        }

    def energy(self):
        dual = self.config in {"tp2", "pp2"}
        return {
            "power_nodes": 2 if dual else 1,
            "energy_per_token_j": 2.0,
            "tokens_per_watt": 0.4,
            "mean_power_w": 200.0 if dual else 100.0,
            "mean_power_w_head": 100.0,
            "mean_power_w_worker": 100.0 if dual else None,
            "power_samples_head": 100,
            "power_samples_worker": 100 if dual else 0,
            "power_max_gap_s_head": 0.2,
            "power_max_gap_s_worker": 0.2 if dual else None,
            "power_scope": "gpu_devices",
        }

    def add_point(self, *, failed=0, run_tag="model-a-run", repeat_id=1,
                  repeat_count=1, gpu_blocks=10):
        cfg = ExperimentConfig.from_yaml(str(self.experiment))
        cfg.model = "org/model"
        cfg.model_label = "model-a"
        cfg.config_label = self.config
        cfg.model_revision = REVISION
        cfg.tokenizer = "org/model"
        cfg.tokenizer_revision = REVISION
        cfg.concurrency = 1
        cfg.expected_concurrencies = "1"
        cfg.repeat_id = repeat_id
        cfg.repeat_count = repeat_count
        log_name = f"server_model-a_{self.config}_r{repeat_id}_fixture.log"
        cfg.server_log = str(self.results / log_name)

        serving_policy, serving_policy_artifact = self._write_serving_policy(cfg)
        server_info = {
            "vllm_version": "1.0",
            "max_model_len": 64,
            "block_size": 16,
            "gpu_blocks": gpu_blocks,
            "total_kv_tokens": gpu_blocks * 16,
            "total_kv_tokens_source": "server-log",
            "kv_cache_dtype": "auto",
            "gpu_memory_utilization": 0.8,
            "enable_prefix_caching": False,
            "hybrid_cache": False,
            "worker_weight_gb": None,
            "worker_kv_cache_gb": None,
            "max_concurrency": 1.0,
        }
        cfg.deployment_fingerprint = deployment_fingerprint(
            cfg.serving_fingerprint, server_info
        )
        client_environment, client_environment_sha256 = (
            _capture_client_environment()
        )
        harness_source_sha256 = _source_fingerprint()
        git_commit = _git_commit()
        if git_commit is None:
            raise RuntimeError("campaign tests require a Git commit")

        summary = {
            "run_tag": run_tag,
            "methodology_version": METHODOLOGY_VERSION,
            "config_file_sha256": config_file_fingerprint(str(self.experiment)),
            "trace_sha256": cfg.trace_sha256,
            "harness_source_sha256": harness_source_sha256,
            "client_environment_sha256": client_environment_sha256,
            "client_environment": client_environment,
            "harness_git_commit": git_commit,
            "serving_fingerprint": cfg.serving_fingerprint,
            "serving_policy": serving_policy,
            "deployment_fingerprint": cfg.deployment_fingerprint,
            "repeat_id": repeat_id,
            "repeat_count": repeat_count,
            "expected_concurrencies": "1",
            "config_label": self.config,
            "model_label": "model-a",
            "model": "org/model",
            "mode": "closed",
            "concurrency": 1,
            "target_qps": 4.0,
            "measure_s": 10.0,
            "warmup_s": 1.0,
            "configured_warmup_s": 1.0,
            "warmup_extension_s": 0.0,
            "cooldown_s": 1.0,
            "load_extension_s": 0.0,
            "drain_s": 0.1,
            "requests_started": 4 + failed,
            "requests_completed": 4,
            "requests_failed": failed,
            "requests_completed_in_window": 4,
            "requests_started_and_completed_in_window": 4,
            "avg_input_tokens": 8.0,
            "avg_requested_output_tokens": 8.0,
            "total_output_tokens": 32,
            "request_throughput_rps": 0.4,
            "output_token_throughput_tps": 3.2,
            "completed_token_throughput_tps": 3.2,
            "goodput_rps": 0.4,
            "goodput_itl2_rps": 0.4,
            "ttft_s": {"p50": 0.1, "p99": 0.2},
            "itl_s": {"p50": 0.1, "p99": 0.2},
            "mean_itl_s": {"p50": 0.1, "p99": 0.2},
            "e2e_s": {"p50": 0.1, "p99": 0.2},
            "vllm_metrics": self.metrics(),
            "energy": self.energy(),
            "server_info": server_info,
            "config": cfg.to_dict(),
        }
        fingerprint_config = dict(summary["config"])
        fingerprint_config.pop("repeat_id", None)
        fingerprint_config.pop("repeat_count", None)
        fingerprint_config.pop("server_log", None)
        fingerprint_config.pop("deployment_fingerprint", None)
        summary["experiment_fingerprint"] = _sha256_json(
            {
                "methodology_version": METHODOLOGY_VERSION,
                "harness_source_sha256": harness_source_sha256,
                "resolved_config": fingerprint_config,
            }
        )
        def write_artifact(name, data):
            path = self.results / name
            path.write_bytes(data)
            return {
                "path": name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }

        artifacts = {
            "raw": write_artifact(
                f"{run_tag}.raw.jsonl",
                (json.dumps({"success": True, "output_tokens": 8}) + "\n").encode(),
            ),
            "power_samples": write_artifact(
                f"{run_tag}.power.jsonl",
                (json.dumps({"source": "head", "watts": 100.0}) + "\n").encode(),
            ),
            "vllm_samples": write_artifact(
                f"{run_tag}.vllm_metrics.jsonl",
                (json.dumps({"metric": "generation", "value": 40.0}) + "\n").encode(),
            ),
            "serving_policy": serving_policy_artifact,
        }
        summary["artifacts"] = artifacts
        summary_data = (json.dumps(summary, indent=2, sort_keys=True) + "\n").encode()
        summary_metadata = write_artifact(f"{run_tag}.summary.json", summary_data)
        index_summary = dict(summary)
        index_summary["artifacts"] = dict(artifacts)
        index_summary["artifacts"]["summary"] = summary_metadata
        _append_index(str(self.results), index_summary)
        (self.results / log_name).write_text(
            "server started\n"
        )

    def add_allreduce(self, valid=True):
        if self.policy is None:
            raise RuntimeError("add_point must create a serving policy first")
        fingerprint = validator._expected_allreduce_fingerprint(
            self.policy["runtime_facts_sha256"], self.policy
        )
        path = self.results / f"allreduce_ray_{fingerprint}_fixture.csv"
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=validator.ALLREDUCE_FIELDS)
            writer.writeheader()
            sizes = validator.ALLREDUCE_SIZES if valid else (8, 16)
            for size in sizes:
                writer.writerow(
                    {
                        "bytes": size,
                        "avg_s": 0.1,
                        "min_s": 0.09,
                        "algbw_GBps": 1.0,
                        "busbw_GBps": 1.5,
                    }
                )
        return path


class PlanTests(unittest.TestCase):
    def test_plan_validates_and_counts_the_cross_product(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            models = root / "models.yaml"
            experiment = root / "experiment.yaml"
            models.write_text(
                yaml.safe_dump(
                    {
                        "models": [
                            {
                                "name": "org/model",
                                "label": "safe-model",
                                "revision": REVISION,
                                "configs": ["single", "tp2"],
                                "max_model_len": 32,
                                "gpu_util": {"single": 0.8, "tp2": 0.8},
                            }
                        ]
                    }
                )
            )
            experiment.write_text(
                yaml.safe_dump(
                    {
                        "buckets": [
                            {"input_len": 16, "output_len": 16, "weight": 1}
                        ]
                    }
                )
            )

            plan = validator.plan_campaign(
                models, experiment, ["1 4"], repeats="2"
            )

        self.assertEqual(plan["largest_request_tokens"], 32)
        self.assertEqual(plan["model_config_count"], 2)
        self.assertEqual(plan["expected_count"], 8)
        self.assertEqual(plan["matrix"][0]["repeat_ids"], [1, 2])

    def test_plan_rejects_global_model_and_bucket_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            models = root / "models.yaml"
            experiment = root / "experiment.yaml"
            experiment.write_text(
                yaml.safe_dump(
                    {"buckets": [{"input_len": 32, "output_len": 1}]}
                )
            )
            base = {
                "name": "org/model",
                "label": "model-a",
                "revision": REVISION,
                "configs": ["single"],
                "max_model_len": 32,
            }
            cases = {
                "unsafe": dict(base, label="../model"),
                "revision": dict(base, revision="main"),
                "config": dict(base, configs=["tp4"]),
                "max_model_len": dict(base, max_model_len=0),
                "gpu_util": dict(base, gpu_util=1.1),
                "tokenizer_revision": dict(base, tokenizer="other/tokenizer"),
            }
            for message, entry in cases.items():
                with self.subTest(message=message):
                    models.write_text(yaml.safe_dump({"models": [entry]}))
                    with self.assertRaises(validator.CampaignValidationError):
                        validator.load_campaign(models, experiment)

            duplicate = dict(base, name="org/other")
            models.write_text(yaml.safe_dump({"models": [base, duplicate]}))
            with self.assertRaisesRegex(
                validator.CampaignValidationError, "duplicate label"
            ):
                validator.load_campaign(models, experiment)


class AllreduceCsvTests(unittest.TestCase):
    @staticmethod
    def _write_csv(path, *, small_bw="0.000", extra_rows=()):
        lines = [",".join(validator.ALLREDUCE_FIELDS)]
        for exponent in range(3, 31):
            size = 1 << exponent
            bw = small_bw if exponent < 20 else "2.000"
            lines.append(f"{size},1.0e-04,5.0e-05,{bw},{bw}")
        lines.extend(extra_rows)
        path.write_text("\n".join(lines) + "\n")

    def test_zero_bandwidth_small_transfers_are_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "allreduce.csv"
            self._write_csv(path, small_bw="0.000")
            validator.validate_allreduce_csv(path)  # must not raise

    def test_negative_bandwidth_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "allreduce.csv"
            self._write_csv(path, small_bw="-0.001")
            with self.assertRaises(validator.CampaignValidationError) as ctx:
                validator.validate_allreduce_csv(path)
            self.assertIn("non-negative", str(ctx.exception))

    def test_trailing_nondata_lines_are_still_rejected(self):
        # Log contamination (e.g. NCCL worker output) must fail, not pass.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "allreduce.csv"
            self._write_csv(
                path, extra_rows=("(_worker pid=1) NCCL WARN not initialized",)
            )
            with self.assertRaises(validator.CampaignValidationError):
                validator.validate_allreduce_csv(path)


class VerifyTests(unittest.TestCase):
    def test_verify_checks_artifacts_and_atomically_writes_hash_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = CampaignFixture(Path(tmp))
            fixture.add_point()
            unrelated_policy = fixture.results / "serving_policy_unrelated.json"
            unrelated_policy.write_text('{"unrelated":true}\n')
            manifest = fixture.results / "campaign-manifest.json"

            report = validator.verify_campaign(
                fixture.models,
                fixture.experiment,
                fixture.results,
                [1],
                1,
                manifest,
            )
            payload = json.loads(manifest.read_text())

            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["verified_count"], 1)
            self.assertEqual(len(payload["artifacts"]["raw"]), 1)
            index_digest = hashlib.sha256(
                (fixture.results / "index.csv").read_bytes()
            ).hexdigest()
            self.assertEqual(
                payload["artifacts"]["index"][0]["sha256"], index_digest
            )
            self.assertEqual(
                [item["path"] for item in payload["artifacts"]["serving_policy"]],
                [fixture.policy_path.name],
            )
            self.assertEqual(
                [item["path"] for item in payload["artifacts"]["environment"]],
                [fixture.environment_path.name],
            )
            self.assertNotIn(
                unrelated_policy.name,
                {
                    item["path"]
                    for items in payload["artifacts"].values()
                    for item in items
                },
            )
            self.assertEqual(list(fixture.results.glob(".*.tmp")), [])

    def test_repeats_may_come_from_different_server_launches(self):
        """Run-to-run variance requires one server launch per repeat.

        vLLM profiles memory on every boot and settles on a different KV block
        count, so the two repeats below carry different deployment
        fingerprints. That is live state, not a change of experiment: the
        campaign still verifies, and both rows still describe one experiment.
        """
        with tempfile.TemporaryDirectory() as tmp:
            fixture = CampaignFixture(Path(tmp))
            fixture.add_point(run_tag="model-a-r1", repeat_id=1,
                              repeat_count=2, gpu_blocks=10)
            fixture.add_point(run_tag="model-a-r2", repeat_id=2,
                              repeat_count=2, gpu_blocks=11)
            manifest = fixture.results / "campaign-manifest.json"

            report = validator.verify_campaign(
                fixture.models,
                fixture.experiment,
                fixture.results,
                [1],
                2,
                manifest,
            )

            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["verified_count"], 2)
            with (fixture.results / "index.csv").open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertEqual(
                len({row["deployment_fingerprint"] for row in rows}), 2
            )
            self.assertEqual(
                len({row["experiment_fingerprint"] for row in rows}), 1
            )

    def test_completed_only_verifies_present_combos_and_skips_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = CampaignFixture(Path(tmp))  # config="single"
            fixture.add_point()
            # Declare an extra config in the plan that was never run.
            fixture.models.write_text(
                yaml.safe_dump(
                    {
                        "models": [
                            {
                                "name": "org/model",
                                "label": "model-a",
                                "revision": REVISION,
                                "configs": ["single", "pp2"],
                                "max_model_len": 64,
                            }
                        ]
                    }
                )
            )
            # Full verification fails: the planned pp2 rectangle is absent.
            with self.assertRaises(validator.CampaignValidationError) as ctx:
                validator.verify_campaign(
                    fixture.models, fixture.experiment, fixture.results, [1], 1
                )
            self.assertIn("missing", str(ctx.exception))
            # completed-only verifies just the present single-node combo and
            # does not demand the (unrun) dual-node all-reduce artifact.
            report = validator.verify_campaign(
                fixture.models,
                fixture.experiment,
                fixture.results,
                [1],
                1,
                completed_only=True,
            )
            self.assertEqual(report["status"], "ok")
            self.assertTrue(report["completed_only"])
            self.assertEqual(report["skipped_combos"], ["model-a/pp2"])
            self.assertEqual(report["verified_count"], 1)
            self.assertEqual(report["expected_count"], 1)

    def test_completed_only_still_requires_a_full_rectangle_per_combo(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = CampaignFixture(Path(tmp))  # config="single"
            fixture.add_point()
            # The present combo ran only concurrency 1; the sweep asked for 1,2.
            with self.assertRaises(validator.CampaignValidationError) as ctx:
                validator.verify_campaign(
                    fixture.models,
                    fixture.experiment,
                    fixture.results,
                    [1, 2],
                    1,
                    completed_only=True,
                )
            self.assertIn("missing", str(ctx.exception))

    def test_allow_source_match_commits_accepts_commit_that_differs_from_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = CampaignFixture(Path(tmp))
            fixture.add_point()
            # Simulate verifying from a HEAD that advanced past the run's commit
            # (results committed incrementally) while the source is unchanged.
            with mock.patch("bench.run_benchmark._git_commit", return_value="b" * 40):
                with self.assertRaises(validator.CampaignValidationError) as ctx:
                    validator.verify_campaign(
                        fixture.models, fixture.experiment, fixture.results, [1], 1
                    )
                self.assertIn("harness_git_commit", str(ctx.exception))
                report = validator.verify_campaign(
                    fixture.models,
                    fixture.experiment,
                    fixture.results,
                    [1],
                    1,
                    allow_source_match_commits=True,
                )
            self.assertEqual(report["status"], "ok")
            self.assertTrue(report["allow_source_match_commits"])
            self.assertEqual(report["harness_git_commits"], [_git_commit()])

    def test_allow_source_match_commits_still_requires_matching_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = CampaignFixture(Path(tmp))
            fixture.add_point()
            # Even with the commit relaxation, a source-hash mismatch is fatal.
            fixture.mutate_index(harness_source_sha256="0" * 64)
            with self.assertRaises(validator.CampaignValidationError) as ctx:
                validator.verify_campaign(
                    fixture.models,
                    fixture.experiment,
                    fixture.results,
                    [1],
                    1,
                    allow_source_match_commits=True,
                )
            self.assertIn("harness_source_sha256", str(ctx.exception))

    def test_verify_rejects_noncurrent_source_and_trace_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = CampaignFixture(Path(tmp))
            fixture.add_point()
            fixture.mutate_index(harness_source_sha256="0" * 64)
            with self.assertRaisesRegex(
                validator.CampaignValidationError, "harness_source|source"
            ):
                validator.verify_campaign(
                    fixture.models, fixture.experiment, fixture.results, [1], 1
                )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = CampaignFixture(Path(tmp))
            fixture.add_point()
            fixture.mutate_index(trace_sha256="0" * 64)
            with self.assertRaisesRegex(
                validator.CampaignValidationError, "trace"
            ):
                validator.verify_campaign(
                    fixture.models, fixture.experiment, fixture.results, [1], 1
                )

    def test_verify_rejects_tampered_environment_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = CampaignFixture(Path(tmp))
            fixture.add_point()
            environment = json.loads(fixture.environment_path.read_text())
            environment["installed_distributions_sha256"] = "0" * 64
            fixture.environment_path.write_text(
                json.dumps(environment, indent=2, sort_keys=True) + "\n"
            )
            with self.assertRaisesRegex(
                validator.CampaignValidationError,
                "environment|installed_distributions",
            ):
                validator.verify_campaign(
                    fixture.models, fixture.experiment, fixture.results, [1], 1
                )

    def test_verify_rejects_policy_human_field_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = CampaignFixture(Path(tmp))
            fixture.add_point()
            fixture.mutate_index(
                vllm_image="nvcr.io/nvidia/vllm@sha256:" + "0" * 64
            )
            with self.assertRaisesRegex(
                validator.CampaignValidationError, "vllm_image|serving policy"
            ):
                validator.verify_campaign(
                    fixture.models, fixture.experiment, fixture.results, [1], 1
                )

    def test_verify_rejects_bad_results_and_missing_grid_points(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = CampaignFixture(Path(tmp))
            fixture.add_point(failed=1)
            with self.assertRaisesRegex(
                validator.CampaignValidationError, "failed requests"
            ):
                validator.verify_campaign(
                    fixture.models, fixture.experiment, fixture.results, [1], 1
                )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = CampaignFixture(Path(tmp))
            fixture.add_point()
            with self.assertRaisesRegex(
                validator.CampaignValidationError, "missing expected rows"
            ):
                validator.verify_campaign(
                    fixture.models, fixture.experiment, fixture.results, [1], 2
                )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = CampaignFixture(Path(tmp))
            fixture.add_point()
            (fixture.results / "model-a-run.raw.jsonl").unlink()
            with self.assertRaisesRegex(
                validator.CampaignValidationError, "artifact is missing"
            ):
                validator.verify_campaign(
                    fixture.models, fixture.experiment, fixture.results, [1], 1
                )

    def test_dual_campaign_requires_a_valid_allreduce_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = CampaignFixture(Path(tmp), config="tp2")
            fixture.add_point()
            fixture.add_allreduce(valid=False)
            with self.assertRaisesRegex(
                validator.CampaignValidationError, "valid all-reduce"
            ):
                validator.verify_campaign(
                    fixture.models, fixture.experiment, fixture.results, [1], 1
                )
            fixture.add_allreduce(valid=True)
            report = validator.verify_campaign(
                fixture.models, fixture.experiment, fixture.results, [1], 1
            )
            self.assertEqual(report["verified_count"], 1)

    def test_manifest_seals_microbenchmark_nccl_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = CampaignFixture(Path(tmp), config="tp2")
            fixture.add_point()
            fixture.add_allreduce(valid=True)
            (allreduce_csv,) = fixture.results.glob("allreduce_ray_*.csv")
            evidence = [
                allreduce_csv.with_name(f"{allreduce_csv.stem}_nccl.log"),
                fixture.results / "interconnect_extra_id_fixture_raw.log",
                fixture.results / "interconnect_extra_id_fixture_nccl.log",
            ]
            for path in evidence:
                path.write_text("NCCL INFO Channel 00/0 : 0 -> 1 via NET/IB/0\n")
            manifest = fixture.results / "campaign-manifest.json"
            validator.verify_campaign(
                fixture.models, fixture.experiment, fixture.results, [1], 1,
                manifest,
            )
            sealed = json.loads(manifest.read_text())["artifacts"]
            self.assertEqual(
                [record["path"] for record in sealed["allreduce_log"]],
                [evidence[0].name],
            )
            self.assertEqual(
                {record["path"] for record in sealed["interconnect_log"]},
                {evidence[1].name, evidence[2].name},
            )


if __name__ == "__main__":
    unittest.main()
