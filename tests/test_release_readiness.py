import hashlib
import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bench.config import ExperimentConfig
from bench.monitors import VLLMMetricsScraper, WorkerPowerSampler
from bench.run_benchmark import (
    _atomic_write,
    _capture_client_environment,
    _experiment_fingerprint,
    _load_serving_policy,
    _model_slug,
    _validate_result_quality,
    client_environment_fingerprint,
    config_file_fingerprint,
    deployment_fingerprint,
)


class ConfigurationReadinessTests(unittest.TestCase):
    def test_trace_bytes_are_resolved_hashed_and_fingerprinted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trace = root / "trace.jsonl"
            trace.write_text('{"input_len": 4, "output_len": 2}\n')
            config = root / "experiment.yaml"
            config.write_text("trace_path: trace.jsonl\n")

            cfg = ExperimentConfig.from_yaml(str(config))
            first = config_file_fingerprint(str(config))
            self.assertEqual(cfg.trace_path, str(trace.resolve()))
            self.assertEqual(
                cfg.trace_sha256, hashlib.sha256(trace.read_bytes()).hexdigest()
            )

            trace.write_text('{"input_len": 8, "output_len": 2}\n')
            second = config_file_fingerprint(str(config))
            self.assertNotEqual(first, second)

    def test_api_key_is_redacted_and_not_part_of_config_identity(self):
        first = ExperimentConfig(api_key="secret-one").to_dict()
        second = ExperimentConfig(api_key="secret-two").to_dict()
        self.assertEqual(first["api_key"], "<redacted>")
        self.assertEqual(first, second)

    def test_nonfinite_slos_and_invalid_power_interval_fail(self):
        for values in (
            {"ttft_slo_s": math.nan},
            {"itl_slo_s": 0.0},
            {"itl_slo_s": 0.2, "itl_slo_s2": 0.1},
            {"power_interval_ms": 0},
            {"power_backend": "sysfs", "power_sysfs_path": ""},
        ):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    ExperimentConfig(**values).validate()

    def test_output_labels_accept_only_safe_filename_components(self):
        ExperimentConfig(config_label="TP2.run-1", model_label="Model_8B").validate()
        ExperimentConfig(config_label="single", model_label="").validate()
        for values in (
            {"config_label": ""},
            {"config_label": "../tp2"},
            {"config_label": "tp2/run"},
            {"model_label": "two words"},
            {"model_label": ".hidden"},
            {"model_label": "mödél"},
        ):
            with self.subTest(values=values):
                with self.assertRaisesRegex(ValueError, "must match"):
                    ExperimentConfig(**values).validate()

    def test_derived_model_slug_is_safe_for_artifact_names(self):
        self.assertEqual(_model_slug("org/.Model Name/模型 8B"), "8b")

    def test_experiment_fingerprint_ignores_per_launch_bookkeeping(self):
        info = {
            "vllm_version": "1.0",
            "max_model_len": 8704,
            "gpu_blocks": 100,
            "total_kv_tokens": 1600,
            "gpu_memory_utilization": 0.8,
            "enable_prefix_caching": False,
        }
        first = ExperimentConfig(
            server_log="results/server_first.log",
            repeat_id=1,
            repeat_count=1,
            deployment_fingerprint="1" * 64,
        ).to_dict()
        resumed = dict(first)
        resumed.update({
            "server_log": "results/server_after_restart.log",
            "repeat_id": 3,
            "repeat_count": 3,
            "deployment_fingerprint": "2" * 64,
        })

        first_hash = _experiment_fingerprint(first, "a" * 64)
        resumed_hash = _experiment_fingerprint(resumed, "a" * 64)
        self.assertEqual(first_hash, resumed_hash)

        # A restart re-profiles memory and lands on a different KV capacity.
        # That is live state, not measurement identity: deployment_fingerprint
        # records it, and a repeat collected on the new launch stays the same
        # experiment.
        relaunched = dict(info, gpu_blocks=101, total_kv_tokens=1616)
        self.assertNotEqual(
            deployment_fingerprint("serving", info),
            deployment_fingerprint("serving", relaunched),
        )

        resumed["measure_s"] = first["measure_s"] + 1
        self.assertNotEqual(
            first_hash,
            _experiment_fingerprint(resumed, "a" * 64),
        )


class ClientEnvironmentTests(unittest.TestCase):
    def test_snapshot_is_captured_once_and_that_exact_object_is_hashed(self):
        environment = {
            "python_version": "3.12.3",
            "installed_distributions": ["example==1.0"],
        }
        expected = hashlib.sha256(
            json.dumps(
                environment,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
        ).hexdigest()
        with mock.patch(
            "bench.run_benchmark.client_environment", return_value=environment
        ) as capture:
            snapshot, fingerprint = _capture_client_environment()

        capture.assert_called_once_with()
        self.assertIs(snapshot, environment)
        self.assertEqual(fingerprint, expected)

    def test_public_fingerprint_api_remains_available(self):
        environment = {"python_version": "3.12.3"}
        expected = hashlib.sha256(
            json.dumps(
                environment,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
        ).hexdigest()
        with mock.patch(
            "bench.run_benchmark.client_environment", return_value=environment
        ) as capture:
            self.assertEqual(client_environment_fingerprint(), expected)
        capture.assert_called_once_with()


class ResultQualityTests(unittest.TestCase):
    @staticmethod
    def summary(*, completed=20, failed=0, tokens=100):
        return {
            "requests_started": completed + failed,
            "requests_completed": completed,
            "requests_failed": failed,
            "total_output_tokens": tokens,
        }

    def test_release_policy_rejects_any_request_failure(self):
        cfg = ExperimentConfig(
            require_zero_request_failures=True,
            min_successful_requests=20,
        )
        with self.assertRaisesRegex(RuntimeError, "failed request"):
            _validate_result_quality(self.summary(failed=1), cfg)

    def test_release_policy_rejects_low_n_and_zero_tokens(self):
        cfg = ExperimentConfig(
            require_zero_request_failures=True,
            min_successful_requests=20,
        )
        with self.assertRaisesRegex(RuntimeError, "fewer"):
            _validate_result_quality(self.summary(completed=19), cfg)
        with self.assertRaisesRegex(RuntimeError, "no output tokens"):
            _validate_result_quality(self.summary(tokens=0), cfg)

    def test_exploratory_policy_retains_failures_but_not_bad_accounting(self):
        cfg = ExperimentConfig(require_zero_request_failures=False)
        _validate_result_quality(self.summary(completed=0, failed=2, tokens=0), cfg)
        bad = self.summary()
        bad["requests_started"] += 1
        with self.assertRaisesRegex(RuntimeError, "accounting mismatch"):
            _validate_result_quality(bad, cfg)


class MonitorIdentityTests(unittest.TestCase):
    def test_worker_identity_line_is_recorded_separately_from_power(self):
        sampler = WorkerPowerSampler("container", "10.0.0.2")
        self.assertTrue(sampler._consume_line("IDENTITY 10.0.0.2 node-abc"))
        self.assertEqual(sampler.identity_ip, "10.0.0.2")
        self.assertEqual(sampler.identity_node_id, "node-abc")
        self.assertEqual(sampler.sample_count(), 0)

    def test_metric_summary_exposes_ambiguous_label_series(self):
        scraper = VLLMMetricsScraper("http://unused")
        scraper._parse(
            'vllm:num_requests_running{engine="0"} 1\n'
            'vllm:num_requests_running{engine="1"} 2\n',
            1.0,
        )
        summary = scraper.summary(0.0, 2.0)
        self.assertEqual(
            summary["vllm:num_requests_running"]["series_count"], 2
        )


class AtomicArtifactTests(unittest.TestCase):
    def test_atomic_write_replaces_complete_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            _atomic_write(str(path), json.dumps({"old": True}).encode())
            _atomic_write(str(path), json.dumps({"new": True}).encode())
            self.assertEqual(json.loads(path.read_text()), {"new": True})
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])


class ServingPolicyTests(unittest.TestCase):
    @staticmethod
    def make_policy():
        runtime_facts = {
            "gpus": [{"gpu_name": "GB10", "power_limit_w": "N/A"}],
            "source": "host-nvidia-smi",
        }
        runtime_hash = hashlib.sha256(
            json.dumps(
                runtime_facts, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        policy = {
            "schema": "serving-v1",
            "config": "single",
            "image": "example/vllm@sha256:" + "a" * 64,
            "model": "example/model",
            "revision": "b" * 40,
            "tokenizer": "example/model",
            "tokenizer_revision": "b" * 40,
            "dtype": "auto",
            "quant": "",
            "max_model_len": "8704",
            "gpu_util": "0.8",
            "prefix_caching": "0",
            "generation_config": "vllm",
            "torchinductor_autograd_cache": "0",
            "hf_hub_offline": "1",
            "trust_remote_code": "",
            "ray_version": "2.48.0",
            "connectx_iface": "enp1s0f0np0",
            "nccl_ib_disable": "0",
            "head_ip": "10.10.10.1",
            "worker_ip": "10.10.10.2",
            "power_backend": "sysfs",
            "cluster_runtime": runtime_hash,
            "runtime_facts": runtime_facts,
            "runtime_facts_sha256": runtime_hash,
            "runtime_scope": "single-host",
        }
        legacy = "".join((
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
        ))
        policy["serving_fingerprint"] = hashlib.sha256(legacy.encode()).hexdigest()
        return policy

    def test_policy_is_hashed_and_cross_checked_before_measurement(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = self.make_policy()
            path = Path(tmp) / "serving_policy.json"
            encoded = (
                json.dumps(policy, indent=2, sort_keys=True) + "\n"
            ).encode()
            path.write_bytes(encoded)
            cfg = SimpleNamespace(
                require_provenance=True,
                out_dir=tmp,
                config_label="single",
                model=policy["model"],
                model_revision=policy["revision"],
                tokenizer=policy["tokenizer"],
                tokenizer_revision=policy["tokenizer_revision"],
                serving_fingerprint=policy["serving_fingerprint"],
                trust_remote_code=False,
            )

            loaded, artifact = _load_serving_policy(str(path), cfg)
            self.assertEqual(loaded, policy)
            self.assertEqual(artifact["path"], path.name)
            self.assertEqual(
                artifact["sha256"], hashlib.sha256(encoded).hexdigest()
            )

            policy["model"] = "tampered/model"
            path.write_text(json.dumps(policy))
            with self.assertRaisesRegex(RuntimeError, "reproduce"):
                _load_serving_policy(str(path), cfg)

    def test_release_policy_file_is_mandatory(self):
        cfg = SimpleNamespace(require_provenance=True)
        with self.assertRaisesRegex(RuntimeError, "requires"):
            _load_serving_policy(None, cfg)


if __name__ == "__main__":
    unittest.main()
