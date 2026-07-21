import asyncio
import csv
import hashlib
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bench.config import ExperimentConfig, WorkloadBucket
from bench.monitors import VLLMMetricsScraper
from bench.client import (_extract_finish_reason, _extract_token_ids,
                          _extract_usage_output_tokens, RequestRecord,
                          send_request)
from bench.server_info import _cache_config, collect
from bench.workload import Workload

# plot_results.py lives in plotting/; put it on the path for direct import.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plotting"))
import plot_results  # noqa: E402


class WorkloadValidationTests(unittest.TestCase):
    def test_rejects_negative_bucket_weight(self):
        cfg = ExperimentConfig(tokenizer="")
        cfg.buckets = [WorkloadBucket(4, 4, -1.0), WorkloadBucket(4, 4, 2.0)]
        with self.assertRaisesRegex(ValueError, "weights"):
            Workload(cfg)

    def test_rejects_non_finite_bucket_weight(self):
        cfg = ExperimentConfig(tokenizer="")
        cfg.buckets = [WorkloadBucket(4, 4, math.inf)]
        with self.assertRaisesRegex(ValueError, "weights"):
            Workload(cfg)

    def test_balanced_bucket_mix_has_bounded_short_window_error(self):
        cfg = ExperimentConfig(tokenizer="", balanced_workload=True)
        cfg.buckets = [
            WorkloadBucket(128, 128, 1.0),
            WorkloadBucket(1024, 256, 1.0),
            WorkloadBucket(4096, 256, 1.0),
            WorkloadBucket(8192, 512, 0.5),
        ]
        wl = Workload(cfg)
        # Start at a non-cycle boundary, as a time-based measurement window does.
        for _ in range(9):
            wl.sample()
        counts = {}
        for _ in range(35):
            spec = wl.sample()
            key = (spec.input_len, spec.output_len)
            counts[key] = counts.get(key, 0) + 1
        self.assertEqual(counts, {
            (128, 128): 10,
            (1024, 256): 10,
            (4096, 256): 10,
            (8192, 512): 5,
        })


class StreamingTokenTests(unittest.TestCase):
    class _Content:
        def __init__(self, lines):
            self.lines = lines

        def __aiter__(self):
            self._it = iter(self.lines)
            return self

        async def __anext__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration

    class _Response:
        status = 200

        def __init__(self, lines):
            self.content = StreamingTokenTests._Content(lines)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    class _Session:
        def __init__(self, lines):
            self.lines = lines
            self.body = None

        def post(self, _url, json, **_kwargs):
            self.body = json
            return StreamingTokenTests._Response(self.lines)

    @staticmethod
    def _stream(usage=2):
        return [
            b'data: {"choices":[{"text":"x","token_ids":[7,8],'
            b'"finish_reason":null}]}\n',
            (f'data: {{"choices":[{{"text":"","token_ids":[],'
             f'"finish_reason":"length"}}],"usage":'
             f'{{"completion_tokens":{usage}}}}}\n').encode(),
            b'data: [DONE]\n',
        ]

    def test_extracts_exact_delta_ids_usage_and_finish_reason(self):
        chunk = {
            "choices": [{"text": "", "token_ids": [7, 8],
                         "finish_reason": "length"}],
            "usage": {"completion_tokens": 2},
        }
        self.assertEqual(_extract_token_ids(chunk), [7, 8])
        self.assertEqual(_extract_usage_output_tokens(chunk), 2)
        self.assertEqual(_extract_finish_reason(chunk), "length")

    def test_empty_decoded_text_does_not_hide_token_ids(self):
        self.assertEqual(
            _extract_token_ids({"choices": [{"text": "", "token_ids": [42]}]}),
            [42],
        )

    def test_request_requires_exact_ids_and_final_usage(self):
        from types import SimpleNamespace
        cfg = ExperimentConfig(model="model")
        spec = SimpleNamespace(input_len=1, output_len=2, prompt=[1])
        session = self._Session(self._stream())

        rec = asyncio.run(send_request(session, cfg, spec))

        self.assertTrue(rec.success)
        self.assertEqual(rec.output_tokens, 2)
        self.assertEqual(len(rec.token_times), 2)
        self.assertEqual(rec.token_times[0], rec.token_times[1])
        self.assertTrue(rec.stream_done)
        self.assertTrue(session.body["return_token_ids"])
        self.assertTrue(session.body["stream_options"]["include_usage"])

    def test_usage_mismatch_is_a_failed_request(self):
        from types import SimpleNamespace
        cfg = ExperimentConfig(model="model")
        spec = SimpleNamespace(input_len=1, output_len=2, prompt=[1])

        rec = asyncio.run(send_request(self._Session(self._stream(usage=1)),
                                       cfg, spec))

        self.assertFalse(rec.success)
        self.assertIn("does not match final usage", rec.error)


class MetricsScraperTests(unittest.TestCase):
    def test_prometheus_parser_ignores_created_suffix_and_normalizes_alias(self):
        text1 = """\
vllm:num_requests_running{model_name="m"} 2
vllm:num_requests_waiting{model_name="m"} 3
vllm:gpu_cache_usage_perc{model_name="m"} 0.4
vllm:generation_tokens_total{model_name="m"} 20
vllm:generation_tokens_total_created{model_name="m"} 999999
"""
        text2 = text1.replace(
            'vllm:generation_tokens_total{model_name="m"} 20',
            'vllm:generation_tokens_total{model_name="m"} 35',
        )
        scraper = VLLMMetricsScraper("http://127.0.0.1:9")
        scraper._parse(text1, 1.0)
        scraper._parse(text2, 2.0)

        summary = scraper.summary(1.0, 2.0)
        self.assertEqual(summary["vllm:generation_tokens_total"]["delta"], 15.0)
        self.assertEqual(len(scraper._series["vllm:generation_tokens_total"]), 2)
        self.assertEqual(summary["vllm:kv_cache_usage_perc"]["max"], 0.4)

    def test_counter_reset_returns_missing_delta(self):
        result = VLLMMetricsScraper._counter_delta(
            [(1.0, 20.0), (2.0, 5.0)], 1.0, 2.0
        )
        self.assertIsNone(result["delta"])
        self.assertIsNone(result["rate_per_s"])


class ServerInfoTests(unittest.TestCase):
    def test_live_hybrid_metric_does_not_multiply_physical_blocks(self):
        metric = (
            'vllm:cache_config_info{block_size="4176",'
            'num_gpu_blocks="6293",mamba_page_size_padded="256",'
            'cache_dtype="auto",gpu_memory_utilization="0.8"} 1\n'
        )
        with mock.patch("bench.server_info._get_text", return_value=metric):
            info = _cache_config("http://server")

        self.assertTrue(info["hybrid_cache"])
        self.assertIsNone(info["total_kv_tokens"])
        self.assertIsNone(info["total_kv_tokens_source"])

    def test_collect_uses_log_worker_fields_without_hf_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "server.log"
            log.write_text(
                "Model weights take 12.5 GiB\n"
                "GPU KV cache size (memory): 34.0 GiB\n"
                "# GPU blocks: 1,234\n"
                "Maximum concurrency for 8,704 tokens per request: 2.3x\n"
            )
            with mock.patch("bench.server_info._max_model_len", return_value=8704), \
                 mock.patch("bench.server_info._server_version",
                            return_value="0.11.0"), \
                 mock.patch("bench.server_info._cache_config",
                            return_value={"block_size": 16, "total_kv_tokens": None}), \
                 mock.patch("bench.server_info.compute_weight_gb") as weight:
                info = collect("http://server", "model/id", str(log),
                               compute_from_hf=False)

        weight.assert_not_called()
        self.assertEqual(info["worker_weight_gb"], 12.5)
        self.assertEqual(info["worker_kv_cache_gb"], 34.0)
        self.assertEqual(info["gpu_blocks"], 1234)
        self.assertEqual(info["total_kv_tokens"], 1234 * 16)
        self.assertEqual(info["max_concurrency"], 2.3)

    def test_hybrid_cache_uses_group_aware_startup_token_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "server.log"
            log.write_text(
                "GPU KV cache size: 5,253,408 tokens\n"
                "Maximum concurrency for 8,704 tokens per request: 899.00x\n"
            )
            cache = {
                "block_size": 4176,
                "gpu_blocks": 6293,
                "total_kv_tokens": None,
                "hybrid_cache": True,
            }
            with mock.patch("bench.server_info._max_model_len", return_value=8704), \
                 mock.patch("bench.server_info._server_version",
                            return_value="0.11.0"), \
                 mock.patch("bench.server_info._cache_config", return_value=cache):
                info = collect("http://server", "model/id", str(log),
                               compute_from_hf=False)

        self.assertEqual(info["total_kv_tokens"], 5_253_408)
        self.assertEqual(info["total_kv_tokens_source"], "vllm_startup_log")
        self.assertNotEqual(info["total_kv_tokens"], 4176 * 6293)

    def test_hybrid_log_blocks_without_token_capacity_stays_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "server.log"
            log.write_text("# GPU blocks: 6,293\n")
            cache = {
                "block_size": 4176,
                "gpu_blocks": None,
                "total_kv_tokens": None,
                "total_kv_tokens_source": None,
                "hybrid_cache": True,
            }
            with mock.patch("bench.server_info._max_model_len", return_value=8704), \
                 mock.patch("bench.server_info._server_version",
                            return_value="0.11.0"), \
                 mock.patch("bench.server_info._cache_config", return_value=cache):
                info = collect("http://server", "model/id", str(log),
                               compute_from_hf=False)

        self.assertEqual(info["gpu_blocks"], 6293)
        self.assertIsNone(info["total_kv_tokens"])
        self.assertIsNone(info["total_kv_tokens_source"])


class PlotCompatibilityTests(unittest.TestCase):
    def test_models_present_falls_back_when_model_labels_are_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "index.csv"
            with path.open("w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "run_tag", "config_label", "model_label", "model", "mode",
                    "concurrency", "measure_s", "requests_completed",
                    "tok_thru_tps",
                ])
                writer.writerow(["r1", "single", "", "Qwen/Qwen3-8B", "closed",
                                 1, 10, 20, 100])

            df = plot_results.load_index(str(path))

        self.assertEqual(plot_results.models_present(df), ["Qwen/Qwen3-8B"])

    def test_fresh_index_carries_corrected_methodology_fields(self):
        from bench.run_benchmark import (
            _append_index, INDEX_FIELDS, METHODOLOGY_VERSION,
        )
        pct = {"p50": 0.1, "p99": 0.2}
        summary = {
            "run_tag": "r1", "methodology_version": METHODOLOGY_VERSION,
            "config_file_sha256": "a" * 64,
            "experiment_fingerprint": "b" * 64,
            "harness_source_sha256": "c" * 64,
            "client_environment_sha256": "d" * 64,
            "deployment_fingerprint": "e" * 64,
            "repeat_id": 1, "repeat_count": 1,
            "config_label": "single", "model_label": "m",
            "model": "model", "mode": "closed", "concurrency": 1,
            "target_qps": None, "measure_s": 10.0, "warmup_s": 12.0,
            "configured_warmup_s": 10.0, "warmup_extension_s": 2.0,
            "cooldown_s": 3.0, "load_extension_s": 4.0,
            "requests_started": 12, "requests_completed": 12,
            "requests_failed": 0, "requests_completed_in_window": 10,
            "requests_started_and_completed_in_window": 9,
            "avg_input_tokens": 128.0, "avg_requested_output_tokens": 64.0,
            "total_output_tokens": 640, "request_throughput_rps": 1.2,
            "output_token_throughput_tps": 64.0,
            "completed_token_throughput_tps": 60.0,
            "goodput_rps": 1.0, "goodput_itl2_rps": 1.1,
            "ttft_s": pct, "itl_s": pct, "mean_itl_s": pct, "e2e_s": pct,
            "server_info": {
                "total_kv_tokens": 1000,
                "total_kv_tokens_source": "vllm_startup_log",
                "hybrid_cache": True,
            },
            "energy": {"power_scope": "gpu_devices"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            raw = b"{}\n"
            summary_file = b"{}\n"
            (Path(tmp) / "r1.raw.jsonl").write_bytes(raw)
            (Path(tmp) / "r1.summary.json").write_bytes(summary_file)
            summary["artifacts"] = {
                "raw": {
                    "path": "r1.raw.jsonl",
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "bytes": len(raw),
                },
                "summary": {
                    "path": "r1.summary.json",
                    "sha256": hashlib.sha256(summary_file).hexdigest(),
                    "bytes": len(summary_file),
                },
            }
            _append_index(tmp, summary)
            path = Path(tmp) / "index.csv"
            with path.open(newline="") as f:
                rows = list(csv.DictReader(f))

        self.assertEqual(list(rows[0]), INDEX_FIELDS)
        self.assertEqual(rows[0]["requests_started_and_completed_in_window"], "9")
        self.assertEqual(rows[0]["total_kv_tokens_source"], "vllm_startup_log")
        self.assertEqual(rows[0]["hybrid_cache"], "True")
        self.assertEqual(rows[0]["power_scope"], "gpu_devices")


class GoodputTierTests(unittest.TestCase):
    """metrics.aggregate scores two ITL goodput tiers against one TTFT bound."""

    @staticmethod
    def _rec(ttft, itl):
        from types import SimpleNamespace
        return SimpleNamespace(t_start=0.5, t_end=1.0, success=True,
                               ttft=ttft, mean_itl=itl, e2e=5.0,
                               input_len=128, output_len=10,
                               output_tokens=10, token_times=[],
                               inter_token_latencies=[itl] * 9)

    def test_strict_and_loose_tiers_count_independently(self):
        from bench.metrics import aggregate
        cfg = ExperimentConfig(ttft_slo_s=3.0, itl_slo_s=0.10, itl_slo_s2=0.20)
        recs = [
            self._rec(0.5, 0.08),   # passes both tiers
            self._rec(0.5, 0.15),   # fails strict 0.10, passes loose 0.20
            self._rec(0.5, 0.30),   # fails both
            self._rec(5.0, 0.05),   # fast ITL but TTFT over bound: fails both
        ]
        s = aggregate(recs, cfg, window_start=0.0, window_end=10.0)
        self.assertEqual(round(s["goodput_rps"] * 10), 1)
        self.assertEqual(round(s["goodput_itl2_rps"] * 10), 2)
        self.assertEqual(s["slo"], {"ttft_s": 3.0, "itl_s": 0.10, "itl_s2": 0.20})

    def test_nan_itl_single_token_output_is_not_a_violation(self):
        from bench.metrics import aggregate
        cfg = ExperimentConfig(ttft_slo_s=3.0, itl_slo_s=0.10, itl_slo_s2=0.20)
        rec = self._rec(0.5, float("nan"))
        rec.inter_token_latencies = []
        s = aggregate([rec], cfg, window_start=0.0, window_end=10.0)
        self.assertEqual(round(s["goodput_rps"] * 10), 1)
        self.assertEqual(round(s["goodput_itl2_rps"] * 10), 1)


class WindowAccountingTests(unittest.TestCase):
    """Throughput counts tokens received in-window; latency uses started-in-
    window requests tracked to completion (the 60s/600s windowing-artifact
    fixes)."""

    @staticmethod
    def _rec(t_start, t_end, token_times, success=True):
        from types import SimpleNamespace
        gaps = [b - a for a, b in zip(token_times, token_times[1:])]
        return SimpleNamespace(
            t_start=t_start, t_end=t_end, success=success,
            input_len=128, output_len=len(token_times),
            output_tokens=len(token_times), token_times=token_times,
            ttft=(token_times[0] - t_start) if token_times else float("nan"),
            e2e=t_end - t_start,
            mean_itl=(sum(gaps) / len(gaps)) if gaps else float("nan"),
            inter_token_latencies=gaps)

    def test_tokens_in_window_count_partial_streams(self):
        from bench.metrics import aggregate
        cfg = ExperimentConfig()
        recs = [
            # started in warmup, streams through the window: only the 3
            # in-window tokens count
            self._rec(1.0, 25.0, [5.0, 12.0, 14.0, 16.0, 22.0]),
            # started and finished inside the window: both tokens count
            self._rec(11.0, 13.0, [11.5, 12.5]),
            # started in-window, finishes after it: 1 of 2 tokens in-window
            self._rec(18.0, 30.0, [19.0, 21.0]),
        ]
        s = aggregate(recs, cfg, window_start=10.0, window_end=20.0)
        self.assertEqual(s["total_output_tokens"], 6)
        self.assertAlmostEqual(s["output_token_throughput_tps"], 0.6)
        # completed-only accounting sees just the middle request
        self.assertEqual(s["completed_output_tokens"], 2)
        self.assertEqual(s["requests_completed_in_window"], 1)
        self.assertEqual(s["requests_started_and_completed_in_window"], 1)

    def test_completion_in_window_and_started_then_completed_are_distinct(self):
        from bench.metrics import aggregate
        cfg = ExperimentConfig()
        warmup_completion = self._rec(1.0, 12.0, [2.0, 11.0])
        in_window_completion = self._rec(11.0, 13.0, [11.5, 12.5])
        slow = self._rec(18.0, 30.0, [19.0, 21.0])
        s = aggregate([warmup_completion, in_window_completion, slow], cfg,
                      window_start=10.0, window_end=20.0)
        self.assertEqual(s["requests_completed_in_window"], 2)
        self.assertEqual(s["requests_started_and_completed_in_window"], 1)

    def test_latency_set_is_started_in_window_tracked_to_completion(self):
        from bench.metrics import aggregate
        cfg = ExperimentConfig()
        warmup_req = self._rec(1.0, 12.0, [2.0, 11.0])       # excluded: early start
        slow_req = self._rec(11.0, 40.0, [15.0, 39.0])       # counted, ends late
        s = aggregate([warmup_req, slow_req], cfg,
                      window_start=10.0, window_end=20.0)
        self.assertEqual(s["requests_started"], 1)
        self.assertEqual(s["requests_completed"], 1)
        self.assertAlmostEqual(s["e2e_s"]["p50"], 29.0)

    def test_energy_totals_head_and_worker(self):
        from types import SimpleNamespace
        from bench.metrics import aggregate
        cfg = ExperimentConfig()
        head = SimpleNamespace(energy_joules=lambda a, b: 100.0,
                               mean_power=lambda a, b: 10.0)
        worker = SimpleNamespace(energy_joules=lambda a, b: 50.0,
                                 mean_power=lambda a, b: 5.0)
        rec = self._rec(11.0, 13.0, [11.5, 12.5])
        s = aggregate([rec], cfg, 10.0, 20.0, power=head, worker_power=worker)
        self.assertEqual(s["energy"]["power_nodes"], 2)
        self.assertAlmostEqual(s["energy"]["mean_power_w"], 15.0)
        self.assertAlmostEqual(s["energy"]["energy_j"], 150.0)
        self.assertAlmostEqual(s["energy"]["energy_per_token_j"], 75.0)

    def test_energy_falls_back_to_head_when_worker_has_no_samples(self):
        from types import SimpleNamespace
        from bench.metrics import aggregate
        cfg = ExperimentConfig()
        head = SimpleNamespace(energy_joules=lambda a, b: 100.0,
                               mean_power=lambda a, b: 10.0)
        dead = SimpleNamespace(energy_joules=lambda a, b: float("nan"),
                               mean_power=lambda a, b: float("nan"))
        rec = self._rec(11.0, 13.0, [11.5, 12.5])
        s = aggregate([rec], cfg, 10.0, 20.0, power=head, worker_power=dead)
        self.assertEqual(s["energy"]["power_nodes"], 1)
        self.assertAlmostEqual(s["energy"]["mean_power_w"], 10.0)


class SteadyBoundaryTests(unittest.TestCase):
    def test_closed_loop_extends_warmup_and_holds_load_for_measurement_requests(self):
        from types import SimpleNamespace
        from bench.runner import _closed_loop

        cfg = ExperimentConfig(mode="closed", concurrency=2, warmup_s=0.0,
                               measure_s=0.02, cooldown_s=0.0)

        class WL:
            @staticmethod
            def sample():
                return SimpleNamespace(input_len=1, output_len=1, prompt=[1])

        async def fake_send(session, cfg, spec, t_start=None):
            await asyncio.sleep(0.03)
            rec = RequestRecord(cfg.config_label, spec.input_len, spec.output_len,
                                t_start=t_start, t_first_token=t_start + 0.01,
                                t_end=t_start + 0.03, output_tokens=1,
                                token_times=[t_start + 0.01],
                                usage_output_tokens=1, finish_reason="length",
                                stream_done=True, success=True)
            return rec

        async def run():
            records = []
            start = __import__("time").perf_counter()
            with mock.patch("bench.runner.send_request", side_effect=fake_send):
                state = await _closed_loop(cfg, WL(), None, start, records)
            return state, start

        state, start = asyncio.run(run())
        self.assertGreaterEqual(state["window_start"] - start, 0.025)
        self.assertAlmostEqual(state["nominal_stop"] - state["window_start"],
                               cfg.measure_s, delta=0.005)
        self.assertGreater(state["load_stop"], state["nominal_stop"])


class PromptUniquenessTests(unittest.TestCase):
    """Every generated prompt must differ from its first block (prefix-cache
    defeating), including across identical buckets."""

    class FakeTok:
        def __call__(self, text, add_special_tokens=False):
            return {"input_ids": [j % 97 for j in range(2048)]}

        def decode(self, ids):
            return " ".join(str(i) for i in ids)

    def test_token_id_prompts_are_unique(self):
        cfg = ExperimentConfig(tokenizer="fake", use_token_ids=True)
        cfg.buckets = [WorkloadBucket(16, 8, 1.0)]
        wl = Workload(cfg, tokenizer=self.FakeTok())
        prompts = [tuple(wl.sample().prompt) for _ in range(200)]
        self.assertEqual(len(set(p[:2] for p in prompts)), 200)
        self.assertTrue(all(len(p) == 16 for p in prompts))

    def test_fallback_text_prompts_are_unique(self):
        cfg = ExperimentConfig(tokenizer="")
        cfg.buckets = [WorkloadBucket(8, 4, 1.0)]
        wl = Workload(cfg)
        prompts = [wl.sample().prompt for _ in range(50)]
        self.assertEqual(len(set(prompts)), 50)


class SweptConcurrenciesTests(unittest.TestCase):
    """scripts/swept_concurrencies.py backs run_models.sh --skip-done."""

    INDEX_COLS = ["model_label", "config_label", "mode", "concurrency",
                  "measure_s", "requests_completed"]

    @staticmethod
    def _load_module():
        import importlib.util
        path = Path(__file__).resolve().parents[1] / "scripts" / "swept_concurrencies.py"
        spec = importlib.util.spec_from_file_location("swept_concurrencies", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _write(self, tmp, rows, measure_s=600.0):
        index = Path(tmp) / "index.csv"
        with index.open("w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(self.INDEX_COLS)
            writer.writerows(rows)
        cfg = Path(tmp) / "experiment.yaml"
        cfg.write_text(f"measure_s: {measure_s}\n")
        return str(index), str(cfg)

    def test_counts_only_matching_completed_closed_rows(self):
        mod = self._load_module()
        with tempfile.TemporaryDirectory() as tmp:
            index, cfg = self._write(tmp, [
                ["qwen3-8b", "single", "closed", 1, 600.0, 35],    # done
                ["qwen3-8b", "single", "closed", 2, 600.0, 0],     # zero-success done
                ["qwen3-8b", "single", "open",   4, 600.0, 40],    # open mode
                ["qwen3-8b", "tp2",    "closed", 8, 600.0, 40],    # other config
                ["qwen3-32b", "single", "closed", 8, 600.0, 40],   # other model
                ["qwen3-8b", "single", "closed", 16, 60.0, 40],    # stale short window
                ["qwen3-8b", "single", "closed", 32, 600.0, 40],   # done
            ])
            self.assertEqual(
                mod.swept(index, "qwen3-8b", "single", cfg), [1, 2, 32])

    def test_missing_index_means_nothing_done(self):
        mod = self._load_module()
        with tempfile.TemporaryDirectory() as tmp:
            _, cfg = self._write(tmp, [])
            self.assertEqual(
                mod.swept(str(Path(tmp) / "absent.csv"), "x", "single", cfg), [])


if __name__ == "__main__":
    unittest.main()
