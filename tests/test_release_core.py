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
