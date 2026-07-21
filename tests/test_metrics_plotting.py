import io
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import matplotlib.pyplot as plt
import pandas as pd

from bench.monitors import PowerSampler, VLLMMetricsScraper, WorkerPowerSampler

# plot_results.py lives in plotting/; put it on the path for direct import.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plotting"))
import plot_results  # noqa: E402


class PowerIntegrationTests(unittest.TestCase):
    def test_irregular_samples_are_interpolated_at_exact_window_bounds(self):
        sampler = PowerSampler()
        sampler._samples = [(0.0, 0.0), (2.0, 20.0),
                            (5.0, 20.0), (7.0, 0.0)]

        # 1--2: 15 J, 2--5: 60 J, 5--6: 15 J.
        self.assertAlmostEqual(sampler.energy_joules(1.0, 6.0), 90.0)
        self.assertAlmostEqual(sampler.mean_power(1.0, 6.0), 18.0)
        self.assertEqual(sampler.sample_count(1.0, 6.0), 2)

    def test_energy_is_missing_when_window_is_not_bracketed(self):
        sampler = PowerSampler()
        sampler._samples = [(2.0, 10.0), (3.0, 10.0)]

        self.assertTrue(math.isnan(sampler.energy_joules(1.0, 3.0)))


class VLLMCounterTests(unittest.TestCase):
    def test_reset_that_recovers_above_baseline_is_still_rejected(self):
        result = VLLMMetricsScraper._counter_delta(
            [(0.0, 20.0), (1.0, 5.0), (2.0, 30.0)], 0.0, 2.0
        )

        self.assertIsNone(result["delta"])
        self.assertIsNone(result["rate_per_s"])
        self.assertEqual(result["sample_count"], 3)

    def test_counter_samples_are_sorted_and_rate_uses_actual_span(self):
        result = VLLMMetricsScraper._counter_delta(
            [(9.0, 100.0), (0.0, 10.0), (4.0, 50.0)], 0.0, 10.0
        )

        self.assertEqual(result["delta"], 90.0)
        self.assertAlmostEqual(result["rate_per_s"], 10.0)
        self.assertEqual(result["sample_count"], 3)
        self.assertEqual(result["sample_span_s"], 9.0)
        self.assertEqual(result["max_gap_s"], 5.0)

    def test_nonfinite_prometheus_values_are_not_recorded(self):
        scraper = VLLMMetricsScraper("http://127.0.0.1:9")
        scraper._parse(
            "vllm:generation_tokens_total NaN\n"
            "vllm:kv_cache_usage_perc +Inf\n",
            1.0,
        )

        self.assertEqual(scraper._series["vllm:generation_tokens_total"], [])
        self.assertEqual(scraper._series["vllm:kv_cache_usage_perc"], [])


class WorkerPowerFreshnessTests(unittest.TestCase):
    def test_duplicate_stale_and_nonfinite_worker_samples_are_rejected(self):
        sampler = WorkerPowerSampler("container", "10.0.0.2", interval_ms=1000)

        self.assertTrue(sampler._consume_line("1 0.1 25.0"))
        self.assertFalse(sampler._consume_line("1 0.2 30.0"))
        self.assertFalse(sampler._consume_line("2 4.0 30.0"))
        self.assertFalse(sampler._consume_line("3 0.1 NaN"))
        self.assertTrue(sampler._consume_line("4 0.1 26.0"))

        self.assertEqual([watts for _, watts in sampler._samples], [25.0, 26.0])


class RepeatAggregationTests(unittest.TestCase):
    @staticmethod
    def _rows():
        common = {
            "model_label": "m", "config_label": "single", "mode": "closed",
            "concurrency": 1, "methodology_version": "v2",
            "config_file_sha256": "cfg", "harness_source_sha256": "source",
            "client_environment_sha256": "client",
            "serving_fingerprint": "serving",
            "deployment_fingerprint": "deployment",
            "experiment_fingerprint": "experiment", "measure_s": 10.0,
            "configured_warmup_s": 2.0, "cooldown_s": 1.0,
            "power_nodes": 1, "power_scope": "gpu_devices",
        }
        return [
            {**common, "repeat_id": 1, "repeat_count": 2, "requests_completed": 10,
             "requests_failed": 0, "tok_thru_tps": 100.0},
            {**common, "repeat_id": 2, "repeat_count": 2, "requests_completed": 0,
             "requests_failed": 50, "tok_thru_tps": 0.0},
        ]

    def test_zero_success_repeat_is_retained_in_aggregate(self):
        result = plot_results.collapse_repeats(pd.DataFrame(self._rows()))

        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["n_repeats"], 2)
        self.assertEqual(result.iloc[0]["tok_thru_tps"], 50.0)
        self.assertEqual(result.iloc[0]["requests_completed__min"], 0)
        self.assertEqual(result.iloc[0]["requests_failed__max"], 50)

    def test_different_power_coverage_cannot_be_averaged(self):
        rows = self._rows()
        rows[1]["power_nodes"] = 2

        with self.assertRaisesRegex(SystemExit, "power_nodes differ"):
            plot_results.collapse_repeats(pd.DataFrame(rows))

    def test_curve_cannot_mix_serving_identity_at_different_loads(self):
        rows = self._rows()
        rows[1]["concurrency"] = 2
        rows[1]["serving_fingerprint"] = "other"

        with self.assertRaisesRegex(SystemExit, "serving_fingerprint"):
            plot_results.collapse_repeats(pd.DataFrame(rows))

    def test_curve_cannot_mix_source_identity_at_different_loads(self):
        rows = self._rows()
        rows[1]["concurrency"] = 2
        rows[1]["harness_source_sha256"] = "other"

        with self.assertRaisesRegex(SystemExit, "harness_source_sha256"):
            plot_results.collapse_repeats(pd.DataFrame(rows))

    def test_duplicate_repeat_id_at_one_load_is_rejected(self):
        rows = self._rows()
        rows[1]["repeat_id"] = 1

        with self.assertRaisesRegex(SystemExit, "duplicate repeat_id"):
            plot_results.collapse_repeats(pd.DataFrame(rows))

    def test_incomplete_repeat_rectangle_warns(self):
        rows = self._rows()
        rows.extend([
            {**rows[0], "concurrency": 2},
            {**rows[1], "concurrency": 2},
        ])
        rows.pop()  # concurrency=2 has only repeat 1
        stderr = io.StringIO()

        with mock.patch("sys.stderr", stderr):
            result = plot_results.collapse_repeats(
                pd.DataFrame(rows), allow_incomplete=True)

        self.assertEqual(len(result), 2)
        self.assertIn("incomplete repeat rectangle", stderr.getvalue())

    def test_uniformly_incomplete_declared_repeat_rectangle_warns(self):
        rows = self._rows()
        for row in rows:
            row["repeat_count"] = 3
        rows.extend([
            {**rows[0], "concurrency": 2},
            {**rows[1], "concurrency": 2},
        ])
        stderr = io.StringIO()

        with mock.patch("sys.stderr", stderr):
            result = plot_results.collapse_repeats(
                pd.DataFrame(rows), allow_incomplete=True)

        self.assertEqual(len(result), 2)
        self.assertIn("incomplete repeat rectangle", stderr.getvalue())
        self.assertIn("intended {1,2,3}", stderr.getvalue())

    def test_incomplete_rectangles_fail_without_diagnostic_override(self):
        rows = self._rows()
        rows[0]["expected_concurrencies"] = "1 2"
        rows[1]["expected_concurrencies"] = "1 2"
        with self.assertRaisesRegex(SystemExit, "incomplete load rectangle"):
            plot_results.collapse_repeats(pd.DataFrame(rows))

        rows = self._rows()
        rows.pop()
        with self.assertRaisesRegex(SystemExit, "incomplete repeat rectangle"):
            plot_results.collapse_repeats(pd.DataFrame(rows))

    def test_missing_repeat_metric_is_not_averaged_away(self):
        rows = self._rows()
        rows[1]["tok_thru_tps"] = None
        result = plot_results.collapse_repeats(pd.DataFrame(rows))
        self.assertTrue(math.isnan(result.iloc[0]["tok_thru_tps"]))
        self.assertEqual(result.iloc[0]["n_repeats"], 2)

    def test_repeat_count_must_be_a_positive_integer(self):
        for bad in (0, 1.5, float("nan"), float("inf"), "bad"):
            with self.subTest(bad=bad):
                rows = self._rows()
                rows[0]["repeat_count"] = 3
                rows[1]["repeat_count"] = bad
                with self.assertRaisesRegex(SystemExit, "repeat_count"):
                    plot_results.collapse_repeats(pd.DataFrame(rows))

    def test_repeat_id_cannot_exceed_its_declared_count(self):
        rows = self._rows()
        for row in rows:
            row["repeat_count"] = 1
        with self.assertRaisesRegex(SystemExit, "exceeds repeat_count"):
            plot_results.collapse_repeats(pd.DataFrame(rows))

    def test_repeat_count_can_increase_without_changing_family_identity(self):
        rows = self._rows()
        rows[0]["repeat_count"] = 2
        rows[1]["repeat_count"] = 3
        stderr = io.StringIO()

        with mock.patch("sys.stderr", stderr):
            result = plot_results.collapse_repeats(
                pd.DataFrame(rows), allow_incomplete=True
            )

        self.assertEqual(result.iloc[0]["repeat_count"], 3)
        self.assertIn("incomplete repeat rectangle", stderr.getvalue())

    def test_experiment_fingerprint_may_differ_between_loads(self):
        rows = self._rows()
        rows.extend([
            {**rows[0], "concurrency": 2,
             "experiment_fingerprint": "experiment-at-two"},
            {**rows[1], "concurrency": 2,
             "experiment_fingerprint": "experiment-at-two"},
        ])

        result = plot_results.collapse_repeats(pd.DataFrame(rows))

        self.assertEqual(len(result), 2)
        self.assertTrue((result["n_repeats"] == 2).all())


class PlotInputHardeningTests(unittest.TestCase):
    def test_mixed_model_labels_fall_back_per_row_without_dropping_rows(self):
        frame = pd.DataFrame([
            {
                "model_label": "friendly-a", "model": "org/model-a",
                "config_label": "single", "mode": "closed", "concurrency": 1,
            },
            {
                "model_label": "  ", "model": "org/model-b",
                "config_label": "single", "mode": "closed", "concurrency": 1,
            },
        ])

        self.assertEqual(
            plot_results.models_present(frame), ["friendly-a", "org/model-b"]
        )
        frames = {model: len(sub) for model, sub in plot_results.iter_model_frames(frame)}
        self.assertEqual(frames, {"friendly-a": 1, "org/model-b": 1})

        collapsed = plot_results.collapse_repeats(frame)
        self.assertEqual(len(collapsed), 2)

    def test_repeat_count_and_kv_metric_span_load_as_numeric(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "index.csv"
            pd.DataFrame([{
                "config_label": "single",
                "repeat_count": "3", "kv_metric_sample_span_s": "19.5",
            }]).to_csv(path, index=False)
            loaded = plot_results.load_index(str(path))

        self.assertEqual(loaded.iloc[0]["repeat_count"], 3)
        self.assertEqual(loaded.iloc[0]["kv_metric_sample_span_s"], 19.5)


class ReliabilityPlotTests(unittest.TestCase):
    def test_missing_failure_column_is_not_reported_as_zero_failures(self):
        frame = pd.DataFrame([{
            "config_label": "single", "mode": "closed", "concurrency": 1,
            "requests_completed": 25,
            "requests_started_and_completed_in_window": 20,
        }])

        with mock.patch.object(plot_results, "save") as save:
            plot_results.fig_reliability(frame, "unused", ["png"])
            fig = save.call_args.args[0]

        try:
            left = fig.axes[0]
            self.assertEqual(len(left.lines), 0)
            labels = [text.get_text() for text in left.texts]
            self.assertIn("unavailable in index schema", labels)
            self.assertNotIn("no request failures\nacross the sweep", labels)
        finally:
            plt.close(fig)


class SummaryTableTests(unittest.TestCase):
    def test_old_index_uses_server_throughput_fallback_in_table(self):
        frame = pd.DataFrame([{
            "config_label": "single", "mode": "closed", "concurrency": 1,
            "measure_s": 10.0, "requests_completed": 100,
            "server_gen_tps": 42.0,
        }])

        with tempfile.TemporaryDirectory() as tmp:
            plot_results.write_summary_table(frame, tmp)
            table = Path(tmp, "summary_table.md").read_text()

        self.assertIn("| 42 |", table)


if __name__ == "__main__":
    unittest.main()
