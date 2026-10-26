from __future__ import annotations

import unittest
from unittest.mock import patch

from bench.metrics import temperature_summary
from bench.monitors import (
    PowerSampler,
    WorkerPowerSampler,
    _WORKER_POWER_SCRIPT,
    _parse_nvidia_smi_row,
)


class NvidiaSmiTemperatureTests(unittest.TestCase):
    def test_parses_gpu_identity_power_and_temperature(self) -> None:
        self.assertEqual(
            _parse_nvidia_smi_row("0, GPU-abc, 125.5, 71"),
            {
                "gpu_index": "0",
                "gpu_uuid": "GPU-abc",
                "watts": 125.5,
                "temperature_c": 71.0,
            },
        )

    def test_missing_or_invalid_sensor_values_remain_null(self) -> None:
        self.assertEqual(
            _parse_nvidia_smi_row("0, GPU-abc, 125.5, N/A"),
            {
                "gpu_index": "0",
                "gpu_uuid": "GPU-abc",
                "watts": 125.5,
                "temperature_c": None,
            },
        )
        self.assertEqual(
            _parse_nvidia_smi_row("0, GPU-abc, 125.5"),
            {
                "gpu_index": "0",
                "gpu_uuid": "GPU-abc",
                "watts": 125.5,
                "temperature_c": None,
            },
        )
        self.assertIsNone(_parse_nvidia_smi_row("not,a,sensor,row"))

    def test_summary_uses_only_valid_samples_in_measurement_window(self) -> None:
        sampler = PowerSampler("nvidia_smi")
        with patch("bench.monitors.time.perf_counter", side_effect=[1.0, 2.0, 3.0, 4.0]):
            sampler._add(100.0, 60.0, "0", "GPU-abc")
            sampler._add(101.0, 70.0, "0", "GPU-abc")
            sampler._add(102.0, 80.0, "0", "GPU-abc")
            sampler._add(103.0, None, "0", "GPU-abc")

        summary = sampler.temperature_summary(2.0, 3.0)
        self.assertEqual(summary["status"], "sampled")
        self.assertEqual(summary["units"], "C")
        self.assertEqual(summary["sample_count"], 2)
        self.assertEqual(summary["mean_c"], 75.0)
        self.assertEqual(summary["max_c"], 80.0)
        self.assertEqual(summary["max_gap_s"], 1.0)
        self.assertEqual(summary["gpus"][0]["gpu_uuid"], "GPU-abc")

    def test_no_valid_temperature_is_null_and_not_zero(self) -> None:
        sampler = PowerSampler("nvidia_smi")
        with patch("bench.monitors.time.perf_counter", return_value=2.0):
            sampler._add(100.0, None, "0", "GPU-abc")
        summary = sampler.temperature_summary(1.0, 3.0)
        self.assertEqual(summary["status"], "no_valid_samples")
        self.assertIsNone(summary["mean_c"])
        self.assertIsNone(summary["max_c"])
        self.assertEqual(summary["sample_count"], 0)

    def test_temperature_collection_preserves_power_samples_and_energy(self) -> None:
        sampler = PowerSampler("nvidia_smi")
        with patch("bench.monitors.time.perf_counter", side_effect=[1.0, 3.0]):
            sampler._add(100.0, 50.0, "0", "GPU-abc")
            sampler._add(300.0, 70.0, "0", "GPU-abc")

        self.assertEqual(sampler.samples(), [(1.0, 100.0), (3.0, 300.0)])
        self.assertEqual(sampler.energy_joules(1.0, 3.0), 400.0)

    def test_worker_sampler_program_is_valid_python(self) -> None:
        compile(_WORKER_POWER_SCRIPT, "worker_power_sampler.py", "exec")

    def test_worker_protocol_keeps_node_and_gpu_temperature(self) -> None:
        sampler = WorkerPowerSampler("container", "10.0.0.2")
        self.assertTrue(sampler._consume_line("IDENTITY 10.0.0.2 node-abc"))
        with patch("bench.monitors.time.perf_counter", return_value=1.0):
            self.assertTrue(sampler._consume_line(
                "SAMPLE\t1\t0.1\t95.0\t68.5\t0\tGPU-worker"
            ))
        self.assertEqual(sampler.sample_count(), 1)
        summary = sampler.temperature_summary(0.0, 10.0)
        self.assertEqual(summary["status"], "sampled")
        self.assertEqual(summary["mean_c"], 68.5)
        self.assertEqual(summary["gpus"][0]["gpu_index"], "0")
        self.assertEqual(sampler.identity_node_id, "node-abc")

    def test_non_nvidia_backend_reports_temperature_as_unsupported(self) -> None:
        sampler = PowerSampler("sysfs")
        summary = sampler.temperature_summary(1.0, 2.0)
        self.assertEqual(summary["status"], "unsupported_backend")
        self.assertEqual(summary["source"], "sysfs")
        self.assertIsNone(summary["mean_c"])


class TemperatureNodeCoverageTests(unittest.TestCase):
    def test_worker_missing_temperature_is_explicit_in_node_coverage(self) -> None:
        head = PowerSampler("nvidia_smi")
        worker = WorkerPowerSampler("container", "10.0.0.2")
        with patch("bench.monitors.time.perf_counter", side_effect=[1.0]):
            head._add(100.0, 65.0, "0", "GPU-head")

        summary = temperature_summary(
            head,
            worker,
            0.0,
            2.0,
            expected_nodes=2,
            head_node_ip="10.0.0.1",
            worker_node_ip="10.0.0.2",
        )
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["sampled_nodes"], 1)
        self.assertEqual(summary["nodes"]["head"]["node_ip"], "10.0.0.1")
        self.assertEqual(summary["nodes"]["worker"]["status"], "no_valid_samples")
        self.assertEqual(summary["nodes"]["worker"]["sample_count"], 0)

    def test_unconfigured_node_is_null_with_explicit_status(self) -> None:
        summary = temperature_summary(
            PowerSampler("none"), None, 1.0, 2.0, expected_nodes=1
        )
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["nodes"]["head"]["status"], "disabled")
        self.assertEqual(summary["nodes"]["worker"]["status"], "not_configured")
        self.assertIsNone(summary["nodes"]["head"]["mean_c"])


if __name__ == "__main__":
    unittest.main()
