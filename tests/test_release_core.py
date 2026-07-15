import csv
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bench.config import ExperimentConfig, WorkloadBucket
from bench.monitors import VLLMMetricsScraper
from bench.server_info import collect
from bench.workload import Workload
import plot_results


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
        self.assertEqual(
            VLLMMetricsScraper._counter_delta([(1.0, 20.0), (2.0, 5.0)], 1.0, 2.0),
            {"delta": None, "rate_per_s": None},
        )


class ServerInfoTests(unittest.TestCase):
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


class GoodputTierTests(unittest.TestCase):
    """metrics.aggregate scores two ITL goodput tiers against one TTFT bound."""

    @staticmethod
    def _rec(ttft, itl):
        from types import SimpleNamespace
        return SimpleNamespace(t_start=0.5, t_end=1.0, success=True,
                               ttft=ttft, mean_itl=itl, e2e=5.0,
                               output_tokens=10, inter_token_latencies=[itl] * 9)

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
                ["qwen3-8b", "single", "closed", 2, 600.0, 0],     # nothing completed
                ["qwen3-8b", "single", "open",   4, 600.0, 40],    # open mode
                ["qwen3-8b", "tp2",    "closed", 8, 600.0, 40],    # other config
                ["qwen3-32b", "single", "closed", 8, 600.0, 40],   # other model
                ["qwen3-8b", "single", "closed", 16, 60.0, 40],    # stale short window
                ["qwen3-8b", "single", "closed", 32, 600.0, 40],   # done
            ])
            self.assertEqual(mod.swept(index, "qwen3-8b", "single", cfg), [1, 32])

    def test_missing_index_means_nothing_done(self):
        mod = self._load_module()
        with tempfile.TemporaryDirectory() as tmp:
            _, cfg = self._write(tmp, [])
            self.assertEqual(
                mod.swept(str(Path(tmp) / "absent.csv"), "x", "single", cfg), [])


if __name__ == "__main__":
    unittest.main()
