import asyncio
import csv
import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from types import ModuleType
from unittest import mock

import yaml

from bench.client import RequestRecord
from bench.config import ExperimentConfig, WorkloadBucket
from bench.run_benchmark import (
    INDEX_FIELDS,
    METHODOLOGY_VERSION,
    _append_index,
    _load_tokenizer,
    _validate_monitor_config,
    collect_deployment_info,
    deployment_fingerprint,
    deployment_state,
    _validate_observability,
    _validate_provenance,
    config_file_fingerprint,
    validate_index_schema,
)
from bench.runner import _open_loop, run_phase
from bench.server_info import _server_version, collect, parse_server_log
from bench.workload import Workload


class PrefixIdSpaceTests(unittest.TestCase):
    class SixteenTokenTokenizer:
        def __call__(self, _text, add_special_tokens=False):
            return {"input_ids": [i % 16 for i in range(512)]}

        def decode(self, ids):
            return " ".join(map(str, ids))

    def test_first_block_remains_unique_beyond_old_4192_request_sweep(self):
        cfg = ExperimentConfig(tokenizer="fake", use_token_ids=True)
        cfg.buckets = [WorkloadBucket(16, 1, 1.0)]
        workload = Workload(cfg, tokenizer=self.SixteenTokenTokenizer())

        first_blocks = {
            tuple(workload.sample().prompt[:8])
            for _ in range(5_000)
        }

        self.assertEqual(len(first_blocks), 5_000)

    def test_small_prefix_id_space_fails_instead_of_wrapping(self):
        class TwoTokenTokenizer:
            def __call__(self, _text, add_special_tokens=False):
                return {"input_ids": [0, 1] * 256}

            def decode(self, ids):
                return " ".join(map(str, ids))

        cfg = ExperimentConfig(tokenizer="fake", use_token_ids=True)
        cfg.buckets = [WorkloadBucket(1, 1, 1.0)]
        workload = Workload(cfg, tokenizer=TwoTokenTokenizer())

        workload.sample()
        with self.assertRaisesRegex(RuntimeError, "prefix id space exhausted"):
            workload.sample()


class OpenLoopAdmissionTests(unittest.TestCase):
    class Workload:
        @staticmethod
        def sample():
            return SimpleNamespace(input_len=1, output_len=1, prompt=[1])

    @staticmethod
    def _success(cfg, spec, t_start):
        now = time.perf_counter()
        return RequestRecord(
            cfg.config_label,
            spec.input_len,
            spec.output_len,
            t_start=t_start,
            t_first_token=now,
            t_end=now,
            output_tokens=1,
            token_times=[now],
            usage_output_tokens=1,
            finish_reason="length",
            stream_done=True,
            success=True,
        )

    def test_all_outstanding_requests_are_bounded_and_overflow_is_recorded(self):
        cfg = ExperimentConfig(
            mode="open",
            qps=2_000.0,
            max_inflight=2,
            warmup_s=0.0,
            measure_s=0.004,
            cooldown_s=0.0,
        )
        active = 0
        max_active = 0

        async def slow_send(_session, request_cfg, spec, t_start=None):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            try:
                await asyncio.sleep(0.012)
                return self._success(request_cfg, spec, t_start)
            finally:
                active -= 1

        async def run():
            records = []
            start = time.perf_counter()
            with mock.patch("bench.runner.send_request", side_effect=slow_send), \
                    mock.patch("bench.runner.CONSECUTIVE_FAILURE_LIMIT", 100_000):
                state = await _open_loop(
                    cfg, self.Workload(), None, start, records
                )
            return records, state

        records, state = asyncio.run(run())

        self.assertLessEqual(max_active, cfg.max_inflight)
        self.assertTrue(any(r.error == "client max_inflight exceeded"
                            for r in records))
        self.assertGreaterEqual(state["load_stop"], state["nominal_stop"])

    def test_nonfinite_or_fractional_phase_settings_fail_before_network(self):
        invalid = [
            {"measure_s": float("nan")},
            {"warmup_s": float("inf")},
            {"cooldown_s": float("-inf")},
            {"request_timeout_s": float("nan")},
            {"concurrency": 1.5},
        ]
        for values in invalid:
            with self.subTest(values=values):
                cfg = ExperimentConfig(**values)
                with self.assertRaises(ValueError):
                    asyncio.run(run_phase(cfg, self.Workload()))

    def test_sustained_max_inflight_overflow_trips_failure_breaker(self):
        cfg = ExperimentConfig(
            mode="open",
            qps=1_000_000.0,
            max_inflight=1,
            warmup_s=0.0,
            measure_s=0.02,
            cooldown_s=0.0,
        )

        async def slow_send(_session, request_cfg, spec, t_start=None):
            await asyncio.sleep(0.01)
            return self._success(request_cfg, spec, t_start)

        async def run():
            start = time.perf_counter()
            with mock.patch("bench.runner.send_request", side_effect=slow_send), \
                    mock.patch("bench.runner.CONSECUTIVE_FAILURE_LIMIT", 3):
                await _open_loop(cfg, self.Workload(), None, start, [])

        with self.assertRaisesRegex(RuntimeError, "max_inflight exceeded"):
            asyncio.run(run())


class ProvenanceAndSchemaTests(unittest.TestCase):
    @staticmethod
    def _summary():
        pct = {"p50": 0.1, "p99": 0.2}
        return {
            "run_tag": "run",
            "methodology_version": METHODOLOGY_VERSION,
            "config_file_sha256": "a" * 64,
            "experiment_fingerprint": "b" * 64,
            "harness_source_sha256": "c" * 64,
            "client_environment_sha256": "f" * 64,
            "harness_git_commit": "d" * 40,
            "serving_fingerprint": "e" * 64,
            "deployment_fingerprint": "9" * 64,
            "repeat_id": 3,
            "repeat_count": 5,
            "config_label": "single",
            "model_label": "model-label",
            "model": "org/model",
            "mode": "closed",
            "concurrency": 1,
            "target_qps": None,
            "measure_s": 10.0,
            "warmup_s": 2.0,
            "configured_warmup_s": 2.0,
            "warmup_extension_s": 0.0,
            "cooldown_s": 1.0,
            "load_extension_s": 0.0,
            "drain_s": 0.5,
            "requests_started": 2,
            "requests_completed": 2,
            "requests_failed": 0,
            "requests_completed_in_window": 2,
            "requests_started_and_completed_in_window": 2,
            "avg_input_tokens": 16.0,
            "avg_requested_output_tokens": 8.0,
            "total_output_tokens": 16,
            "request_throughput_rps": 0.2,
            "output_token_throughput_tps": 1.6,
            "completed_token_throughput_tps": 1.6,
            "goodput_rps": 0.2,
            "goodput_itl2_rps": 0.2,
            "ttft_s": pct,
            "itl_s": pct,
            "mean_itl_s": pct,
            "e2e_s": pct,
            "vllm_metrics": {
                "vllm:generation_tokens_total": {
                    "delta": 16.0,
                    "rate_per_s": 1.55,
                },
                "vllm:kv_cache_usage_perc": {
                    "sample_count": 12,
                    "sample_span_s": 9.25,
                },
            },
            "server_info": {"vllm_version": "0.11.0"},
            "energy": {},
        }

    def test_index_row_preserves_all_resume_provenance(self):
        summary = self._summary()
        with tempfile.TemporaryDirectory() as tmp:
            _append_index(tmp, summary)
            validate_index_schema(tmp)
            with (Path(tmp) / "index.csv").open(newline="") as handle:
                row = next(csv.DictReader(handle))

        for field in (
            "methodology_version",
            "config_file_sha256",
            "experiment_fingerprint",
            "harness_source_sha256",
            "client_environment_sha256",
            "harness_git_commit",
            "serving_fingerprint",
            "deployment_fingerprint",
            "repeat_id",
            "repeat_count",
        ):
            self.assertEqual(row[field], str(summary[field]))
        self.assertEqual(row["server_gen_tps"], "1.55")
        self.assertEqual(row["kv_metric_sample_span_s"], "9.25")
        self.assertEqual(row["vllm_version"], "0.11.0")

    def test_old_or_reordered_index_header_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / "index.csv"
            index.write_text(",".join(INDEX_FIELDS[:-1]) + "\n")
            with self.assertRaisesRegex(RuntimeError, "index schema mismatch"):
                validate_index_schema(tmp)

            index.write_text(",".join(reversed(INDEX_FIELDS)) + "\n")
            with self.assertRaisesRegex(RuntimeError, "index schema mismatch"):
                validate_index_schema(tmp)

    def test_truncated_and_malformed_index_rows_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / "index.csv"
            index.write_text(",".join(INDEX_FIELDS) + "\nrun-only\n")
            with self.assertRaisesRegex(RuntimeError, "has 1 fields"):
                validate_index_schema(tmp)

        summary = self._summary()
        summary["requests_completed"] = float("nan")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "requests_completed"):
                _append_index(tmp, summary)

        summary = self._summary()
        summary["repeat_count"] = summary["repeat_id"] - 1
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "greater than repeat_count"):
                _append_index(tmp, summary)

    def test_duplicate_or_interrupted_append_preserves_existing_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self._summary()
            _append_index(tmp, first)
            index = Path(tmp) / "index.csv"
            original = index.read_bytes()

            duplicate = dict(first)
            duplicate["run_tag"] = "different-tag"
            with self.assertRaisesRegex(RuntimeError, "duplicate"):
                _append_index(tmp, duplicate)
            self.assertEqual(index.read_bytes(), original)

            second = dict(first)
            second["run_tag"] = "second"
            second["concurrency"] = 2
            with mock.patch(
                "bench.run_benchmark.os.replace",
                side_effect=OSError("simulated interruption"),
            ):
                with self.assertRaisesRegex(OSError, "interruption"):
                    _append_index(tmp, second)
            self.assertEqual(index.read_bytes(), original)
            self.assertEqual(list(Path(tmp).glob(".index.*.tmp")), [])

    def test_deployment_identity_is_stable_and_collection_retries(self):
        stable = {
            "vllm_version": "1.0",
            "max_model_len": 8704,
            "block_size": 16,
            "gpu_blocks": 100,
            "total_kv_tokens": 1600,
            "total_kv_tokens_source": "startup_log",
            "kv_cache_dtype": "auto",
            "gpu_memory_utilization": 0.8,
            "enable_prefix_caching": "False",
            "hybrid_cache": False,
            "worker_weight_gb": 10.0,
            "worker_kv_cache_gb": 20.0,
            "max_concurrency": 1.5,
        }
        noisy = dict(stable, model="org/model", num_cpu_blocks=123)
        other_noise = dict(stable, model="renamed", num_cpu_blocks=999)
        self.assertEqual(
            deployment_fingerprint("serving", noisy),
            deployment_fingerprint("serving", other_noise),
        )
        changed = dict(stable, gpu_blocks=101)
        self.assertNotEqual(
            deployment_fingerprint("serving", stable),
            deployment_fingerprint("serving", changed),
        )

        pieces = [
            {"vllm_version": "1.0", "max_model_len": 8704},
            {
                "gpu_blocks": 100,
                "total_kv_tokens": 1600,
                "gpu_memory_utilization": 0.8,
            },
            {"enable_prefix_caching": "False", "block_size": 16},
        ]
        with mock.patch(
            "bench.run_benchmark.server_info.collect", side_effect=pieces
        ) as collect_mock:
            info = collect_deployment_info(
                "http://server", "org/model", "server.log",
                attempts=3, delay_s=0,
            )
        self.assertEqual(collect_mock.call_count, 3)
        self.assertEqual(deployment_state(info)["total_kv_tokens"], 1600)

        with mock.patch(
            "bench.run_benchmark.server_info.collect",
            return_value={"vllm_version": "1.0"},
        ):
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                collect_deployment_info(
                    "http://server", "org/model", "server.log",
                    attempts=2, delay_s=0,
                )

    def test_config_hash_is_canonical_and_revision_sensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.yaml"
            second = Path(tmp) / "second.yaml"
            changed = Path(tmp) / "changed.yaml"
            first.write_text(
                "model: org/model\nmodel_revision: " + "1" * 40 + "\nseed: 7\n"
            )
            second.write_text(
                "seed: 7\nmodel_revision: " + "1" * 40 + "\nmodel: org/model\n"
            )
            changed.write_text(
                "model: org/model\nmodel_revision: " + "2" * 40 + "\nseed: 7\n"
            )

            self.assertEqual(
                config_file_fingerprint(str(first)),
                config_file_fingerprint(str(second)),
            )
            self.assertNotEqual(
                config_file_fingerprint(str(first)),
                config_file_fingerprint(str(changed)),
            )

    def test_resume_schedule_is_repeat_and_fingerprint_exact(self):
        path = (Path(__file__).resolve().parents[1]
                / "scripts" / "swept_concurrencies.py")
        spec = importlib.util.spec_from_file_location("resume_helper", path)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "experiment.yaml"
            config.write_text("measure_s: 7200\n")
            config_hash = config_file_fingerprint(str(config))
            index = Path(tmp) / "index.csv"
            fields = [
                "model_label", "config_label", "mode", "concurrency",
                "expected_concurrencies",
                "requests_completed", "methodology_version",
                "config_file_sha256", "harness_source_sha256",
                "client_environment_sha256", "serving_fingerprint",
                "deployment_fingerprint", "experiment_fingerprint", "repeat_id",
            ]
            common = {
                "model_label": "m", "config_label": "single", "mode": "closed",
                "expected_concurrencies": "1 2",
                "requests_completed": 0,
                "methodology_version": METHODOLOGY_VERSION,
                "config_file_sha256": config_hash,
                "harness_source_sha256": "source",
                "client_environment_sha256": "client",
                "serving_fingerprint": "serving",
                "deployment_fingerprint": "deployment",
                "experiment_fingerprint": "experiment",
            }
            rows = [
                dict(common, concurrency=1, repeat_id=1),
                dict(common, concurrency=1, repeat_id=2,
                     serving_fingerprint="old-server"),
                dict(common, concurrency=2, repeat_id=1,
                     harness_source_sha256="old-source"),
                dict(common, concurrency=2, repeat_id=2),
            ]
            with index.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            with mock.patch.object(helper, "_source_fingerprint",
                                   return_value="source"), \
                    mock.patch.object(helper, "client_environment_fingerprint",
                                      return_value="client"):
                missing = helper.missing_schedule(
                    str(index), "m", "single", str(config), "serving", 2,
                    [1, 2], "deployment",
                )
            self.assertEqual(missing, [(1, 2), (2, 1)])

    def test_resume_rejects_a_different_live_deployment(self):
        path = (Path(__file__).resolve().parents[1]
                / "scripts" / "swept_concurrencies.py")
        spec = importlib.util.spec_from_file_location("resume_helper_deploy", path)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "experiment.yaml"
            config.write_text("measure_s: 10\n")
            index = Path(tmp) / "index.csv"
            fields = [
                "model_label", "config_label", "mode", "concurrency",
                "expected_concurrencies",
                "requests_completed", "methodology_version",
                "config_file_sha256", "harness_source_sha256",
                "client_environment_sha256", "serving_fingerprint",
                "deployment_fingerprint", "experiment_fingerprint", "repeat_id",
            ]
            row = {
                "model_label": "m", "config_label": "single", "mode": "closed",
                "expected_concurrencies": "1",
                "concurrency": 1, "requests_completed": 10, "repeat_id": 1,
                "methodology_version": METHODOLOGY_VERSION,
                "config_file_sha256": config_file_fingerprint(str(config)),
                "harness_source_sha256": "source",
                "client_environment_sha256": "client",
                "serving_fingerprint": "serving",
                "deployment_fingerprint": "old-deployment",
                "experiment_fingerprint": "experiment",
            }
            with index.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow(row)
            with self.assertRaisesRegex(ValueError, "deployment"):
                with mock.patch.object(helper, "_source_fingerprint",
                                       return_value="source"), \
                        mock.patch.object(helper, "client_environment_fingerprint",
                                          return_value="client"):
                    helper.missing_schedule(
                        str(index), "m", "single", str(config), "serving", 1,
                        [1], "new-deployment",
                    )

    def test_resume_rejects_malformed_and_duplicate_current_rows(self):
        path = (Path(__file__).resolve().parents[1]
                / "scripts" / "swept_concurrencies.py")
        spec = importlib.util.spec_from_file_location(
            "resume_helper_strict", path)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "experiment.yaml"
            config.write_text("measure_s: 10\n")
            index = Path(tmp) / "index.csv"
            fields = [
                "model_label", "config_label", "mode", "concurrency",
                "expected_concurrencies",
                "requests_completed", "requests_failed",
                "methodology_version", "config_file_sha256",
                "harness_source_sha256", "client_environment_sha256",
                "serving_fingerprint", "deployment_fingerprint",
                "experiment_fingerprint", "repeat_id",
            ]
            common = {
                "model_label": "m",
                "config_label": "single",
                "mode": "closed",
                "expected_concurrencies": "1",
                "concurrency": 1,
                "requests_completed": 0,
                "requests_failed": 0,
                "methodology_version": METHODOLOGY_VERSION,
                "config_file_sha256": config_file_fingerprint(str(config)),
                "harness_source_sha256": "source",
                "client_environment_sha256": "client",
                "serving_fingerprint": "serving",
                "deployment_fingerprint": "deployment",
                "experiment_fingerprint": "experiment",
                "repeat_id": 1,
            }

            def write(rows):
                with index.open("w", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(rows)

            def schedule():
                return helper.missing_schedule(
                    str(index), "m", "single", str(config), "serving", 2,
                    [1], "deployment",
                )

            invalid = [
                ("concurrency", "2.9", "concurrency"),
                ("concurrency", "inf", "concurrency"),
                ("repeat_id", "1.5", "repeat_id"),
                ("repeat_id", "nan", "repeat_id"),
                ("requests_completed", "nan", "requests_completed"),
                ("requests_completed", -1, "requests_completed"),
                ("requests_failed", "inf", "requests_failed"),
                ("requests_failed", -1, "requests_failed"),
            ]
            with mock.patch.object(helper, "_source_fingerprint",
                                   return_value="source"), \
                    mock.patch.object(helper, "client_environment_fingerprint",
                                      return_value="client"):
                for field, value, message in invalid:
                    with self.subTest(field=field, value=value):
                        write([dict(common, **{field: value})])
                        with self.assertRaisesRegex(ValueError, message):
                            schedule()

                write([dict(common), dict(common)])
                with self.assertRaisesRegex(ValueError, "duplicate"):
                    schedule()

                with self.assertRaisesRegex(ValueError, "repeats"):
                    helper.missing_schedule(
                        str(index), "m", "single", str(config), "serving",
                        1.5, [1], "deployment",
                    )
                with self.assertRaisesRegex(ValueError, "concurrency"):
                    helper.missing_schedule(
                        str(index), "m", "single", str(config), "serving",
                        1, [2.9], "deployment",
                    )


class ObservabilityValidationTests(unittest.TestCase):
    @staticmethod
    def _complete_summary(nodes=1):
        return {
            "measure_s": 10.0,
            "total_output_tokens": 10,
            "energy": {
                "power_nodes": nodes,
                "energy_per_token_j": 1.0,
                "mean_power_w": 100.0,
                "mean_power_w_head": 50.0 if nodes == 2 else 100.0,
                "mean_power_w_worker": 50.0 if nodes == 2 else None,
                "power_samples_head": 100,
                "power_samples_worker": 100 if nodes == 2 else 0,
                "power_max_gap_s_head": 0.2,
                "power_max_gap_s_worker": 0.2 if nodes == 2 else None,
            },
            "vllm_metrics": {
                "vllm:num_requests_running": {
                    "mean": 1.0, "sample_count": 10,
                    "sample_span_s": 9.0, "max_gap_s": 1.0,
                },
                "vllm:num_requests_waiting": {
                    "mean": 0.0, "sample_count": 10,
                    "sample_span_s": 9.0, "max_gap_s": 1.0,
                },
                "vllm:generation_tokens_total": {
                    "delta": 10.0, "sample_count": 11,
                    "sample_span_s": 10.0, "max_gap_s": 1.0,
                },
                "vllm:prompt_tokens_total": {
                    "delta": 10.0, "sample_count": 11,
                    "sample_span_s": 10.0, "max_gap_s": 1.0,
                },
                "vllm:num_preemptions_total": {
                    "delta": 0.0, "sample_count": 11,
                    "sample_span_s": 10.0, "max_gap_s": 1.0,
                },
                "vllm:kv_cache_usage_perc": {
                    "mean": 0.1, "sample_count": 10,
                    "sample_span_s": 9.0, "max_gap_s": 1.0,
                },
            },
        }

    def test_complete_dense_monitor_data_is_valid(self):
        cfg = ExperimentConfig(
            power_backend="sysfs",
            require_power_data=True,
            scrape_vllm_metrics=True,
            require_vllm_metrics_data=True,
        )
        _validate_observability(self._complete_summary(), cfg)

    def test_active_run_rejects_zero_power_or_generation(self):
        cfg = ExperimentConfig(
            power_backend="sysfs", require_power_data=True,
            scrape_vllm_metrics=True, require_vllm_metrics_data=True,
        )
        summary = self._complete_summary()
        summary["energy"]["mean_power_w"] = 0.0
        summary["energy"]["mean_power_w_head"] = 0.0
        with self.assertRaisesRegex(RuntimeError, "mean_power_w_head"):
            _validate_observability(summary, cfg)

        summary = self._complete_summary()
        summary["vllm_metrics"]["vllm:generation_tokens_total"]["delta"] = 0.0
        with self.assertRaisesRegex(RuntimeError, "did not advance"):
            _validate_observability(summary, cfg)

    def test_sparse_required_monitor_data_is_rejected(self):
        cfg = ExperimentConfig(
            power_backend="sysfs", require_power_data=True,
            scrape_vllm_metrics=True, require_vllm_metrics_data=True,
        )
        summary = self._complete_summary()
        summary["energy"]["power_max_gap_s_head"] = 6.0
        with self.assertRaisesRegex(RuntimeError, "power sampler gap"):
            _validate_observability(summary, cfg)

        summary = self._complete_summary()
        summary["vllm_metrics"]["vllm:generation_tokens_total"]["max_gap_s"] = 6.0
        with self.assertRaisesRegex(RuntimeError, "scrape gap"):
            _validate_observability(summary, cfg)

    def test_dual_node_run_requires_both_power_samplers(self):
        cfg = ExperimentConfig(
            config_label="tp2",
            power_backend="sysfs",
            require_power_data=True,
        )
        with self.assertRaisesRegex(RuntimeError, "expected 2 node"):
            _validate_observability(self._complete_summary(nodes=1), cfg)

    def test_required_power_cannot_use_disabled_backend(self):
        cfg = ExperimentConfig(power_backend="none", require_power_data=True)
        with self.assertRaisesRegex(RuntimeError, "power_backend is none"):
            _validate_observability(self._complete_summary(), cfg)
        with self.assertRaisesRegex(RuntimeError, "power_backend is none"):
            _validate_monitor_config(cfg)

    def test_missing_required_vllm_window_samples_are_rejected(self):
        cfg = ExperimentConfig(
            scrape_vllm_metrics=True,
            require_vllm_metrics_data=True,
        )
        summary = self._complete_summary()
        summary["vllm_metrics"]["vllm:kv_cache_usage_perc"] = {}
        with self.assertRaisesRegex(RuntimeError, "missing or non-finite"):
            _validate_observability(summary, cfg)


class WorkerCacheManifestTests(unittest.TestCase):
    @staticmethod
    def _load_helper():
        path = (Path(__file__).resolve().parents[1]
                / "scripts" / "mirror_worker_cache.py")
        ray = ModuleType("ray")
        ray.remote = lambda **_kwargs: lambda obj: obj
        ray_exceptions = ModuleType("ray.exceptions")
        ray_exceptions.GetTimeoutError = type("GetTimeoutError", (Exception,), {})
        spec = importlib.util.spec_from_file_location("mirror_helper", path)
        helper = importlib.util.module_from_spec(spec)
        with mock.patch.dict(
            sys.modules,
            {"ray": ray, "ray.exceptions": ray_exceptions},
        ):
            spec.loader.exec_module(helper)
        return helper

    def test_manifest_contains_only_exact_snapshot_and_referenced_blobs(self):
        helper = self._load_helper()
        revision = "a" * 40
        with tempfile.TemporaryDirectory() as tmp:
            helper.HUB = str(Path(tmp) / "hub")
            root = Path(helper.HUB) / "models--org--model"
            blobs = root / "blobs"
            snapshot = root / "snapshots" / revision
            old_snapshot = root / "snapshots" / ("b" * 40)
            blobs.mkdir(parents=True)
            snapshot.mkdir(parents=True)
            old_snapshot.mkdir(parents=True)
            (blobs / "wanted").write_bytes(b"weights")
            (blobs / "unrelated").write_bytes(b"old revision data")
            os.symlink("../../blobs/wanted", snapshot / "model.safetensors")
            os.symlink("../../blobs/unrelated", old_snapshot / "old.bin")

            files, links = helper.snapshot_manifest("org/model", revision)

        self.assertEqual(
            files,
            {"models--org--model/blobs/wanted": len(b"weights")},
        )
        self.assertEqual(
            links,
            {f"models--org--model/snapshots/{revision}/model.safetensors":
             "../../blobs/wanted"},
        )

    def test_manifest_rejects_unpinned_or_unsafe_snapshot_entries(self):
        helper = self._load_helper()
        with self.assertRaisesRegex(ValueError, "invalid pinned"):
            helper.parse_spec("org/model|main|model")

        revision = "a" * 40
        with tempfile.TemporaryDirectory() as tmp:
            helper.HUB = str(Path(tmp) / "hub")
            snapshot = (Path(helper.HUB) / "models--org--model"
                        / "snapshots" / revision)
            snapshot.mkdir(parents=True)
            os.symlink("/etc/passwd", snapshot / "unsafe")
            with self.assertRaisesRegex(RuntimeError, "unsafe snapshot link"):
                helper.snapshot_manifest("org/model", revision)


class ProvenanceValidationTests(unittest.TestCase):
    def test_release_config_requires_exact_commits_and_serving_hash(self):
        cfg = ExperimentConfig(
            require_provenance=True,
            model="org/model",
            tokenizer="org/model",
            use_token_ids=True,
        )
        with self.assertRaisesRegex(RuntimeError, "model_revision"):
            _validate_provenance(cfg)

        cfg.model_revision = "a" * 40
        cfg.tokenizer_revision = "a" * 40
        cfg.serving_fingerprint = "b" * 64
        _validate_provenance(cfg)

    def test_smoke_config_allows_unpinned_exploration(self):
        _validate_provenance(ExperimentConfig(require_provenance=False))


class RevisionAndServerMetadataTests(unittest.TestCase):
    def test_every_active_model_uses_a_pinned_commit_revision(self):
        path = Path(__file__).resolve().parents[1] / "configs" / "models.yaml"
        models = yaml.safe_load(path.read_text())["models"]
        revisions = {}
        for model in models:
            revision = model.get("revision", "")
            self.assertRegex(revision, r"^[0-9a-f]{40}$", model["label"])
            name = model["name"]
            if name in revisions:
                self.assertEqual(revisions[name], revision)
            revisions[name] = revision

    def test_client_tokenizer_load_uses_pinned_revision(self):
        loader = mock.Mock(return_value=object())
        transformers = SimpleNamespace(
            AutoTokenizer=SimpleNamespace(from_pretrained=loader)
        )
        cfg = ExperimentConfig(
            tokenizer="org/model",
            tokenizer_revision="a" * 40,
            trust_remote_code=True,
        )
        with mock.patch.dict(sys.modules, {"transformers": transformers}):
            _load_tokenizer(cfg)

        loader.assert_called_once_with(
            "org/model", trust_remote_code=True, revision="a" * 40
        )

    def test_server_version_endpoint_is_recorded_by_collect(self):
        with mock.patch("bench.server_info._server_version",
                        return_value="0.11.0"), \
                mock.patch("bench.server_info._max_model_len",
                           return_value=8704), \
                mock.patch("bench.server_info._cache_config", return_value={}):
            info = collect(
                "http://server", "org/model", compute_from_hf=False
            )

        self.assertEqual(info["vllm_version"], "0.11.0")

    def test_server_version_endpoint_is_best_effort(self):
        with mock.patch("bench.server_info._get_json",
                        return_value={"version": "0.11.0"}) as get_json:
            self.assertEqual(_server_version("http://server/"), "0.11.0")
        get_json.assert_called_once_with("http://server/version")

        with mock.patch("bench.server_info._get_json",
                        side_effect=OSError("offline")):
            self.assertIsNone(_server_version("http://server"))

    def test_current_model_loading_memory_log_phrase_is_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "server.log"
            log.write_text("Model loading took 42.125 GiB memory\n")
            self.assertEqual(parse_server_log(str(log))["weight_gb"], 42.125)


if __name__ == "__main__":
    unittest.main()
