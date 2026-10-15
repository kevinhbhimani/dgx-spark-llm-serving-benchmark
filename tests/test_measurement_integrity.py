"""Regression checks for independently verified workload and token counts."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from bench.client import (_extract_usage_input_tokens,
                          _extract_usage_output_tokens, RequestRecord, send_request)
from bench.config import ExperimentConfig
from bench.metrics import aggregate
from bench.run_benchmark import _validate_result_quality
from bench.workload import RequestSpec


class PromptUsageTests(unittest.TestCase):
    class Response:
        status = 200

        def __init__(self, usage):
            self.usage = usage
            self.content = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def __aiter__(self):
            event = {
                "choices": [{"token_ids": [7, 8], "finish_reason": "length"}],
                "usage": self.usage,
            }
            yield ("data: " + json.dumps(event) + "\n\n").encode()
            yield b"data: [DONE]\n\n"

    class Session:
        def __init__(self, usage):
            self.usage = usage

        def post(self, *_args, **_kwargs):
            return PromptUsageTests.Response(self.usage)

    def request(self, usage, **overrides):
        cfg = ExperimentConfig(model="model", **overrides)
        spec = RequestSpec(input_len=3, output_len=2, prompt=[1, 2, 3])
        return asyncio.run(send_request(self.Session(usage), cfg, spec))

    def test_server_prompt_count_is_preserved_in_raw_record(self):
        rec = self.request({"prompt_tokens": 3, "completion_tokens": 2},
                           require_provenance=True)
        self.assertTrue(rec.success, rec.error)
        self.assertEqual(rec.usage_input_tokens, 3)
        self.assertEqual(rec.to_json()["usage_input_tokens"], 3)

    def test_token_id_request_rejects_actual_prompt_length_mismatch(self):
        rec = self.request({"prompt_tokens": 4, "completion_tokens": 2})
        self.assertFalse(rec.success)
        self.assertIn("prompt token usage does not match", rec.error)
        self.assertEqual(rec.usage_input_tokens, 4)

    def test_release_request_requires_independent_prompt_usage(self):
        rec = self.request({"completion_tokens": 2}, require_provenance=True)
        self.assertFalse(rec.success)
        self.assertIn("final prompt token usage", rec.error)

    def test_release_text_request_also_requires_exact_input_length(self):
        rec = self.request({"prompt_tokens": 4, "completion_tokens": 2},
                           require_provenance=True, use_token_ids=False)
        self.assertFalse(rec.success)
        self.assertIn("prompt token usage does not match", rec.error)

    def test_smoke_text_request_can_keep_approximate_input_lengths(self):
        rec = self.request({"prompt_tokens": 4, "completion_tokens": 2},
                           use_token_ids=False)
        self.assertTrue(rec.success, rec.error)
        self.assertEqual(rec.usage_input_tokens, 4)

    def test_invalid_usage_counts_cannot_pass_as_integer_tokens(self):
        for value in (True, False, -1, 2.5, "2", None):
            with self.subTest(value=value):
                usage = {"prompt_tokens": value, "completion_tokens": value}
                self.assertIsNone(_extract_usage_input_tokens({"usage": usage}))
                self.assertIsNone(_extract_usage_output_tokens({"usage": usage}))


class BooleanPolicyTests(unittest.TestCase):
    def test_policy_flags_require_yaml_booleans(self):
        for name in (
            "trust_remote_code", "use_token_ids", "balanced_workload",
            "scrape_vllm_metrics", "require_power_data",
            "require_vllm_metrics_data", "require_provenance",
            "require_zero_request_failures", "collect_hf_metadata",
        ):
            for value in ("false", "true", 0, 1, None):
                with self.subTest(name=name, value=value):
                    cfg = ExperimentConfig(**{name: value})
                    with self.assertRaisesRegex(ValueError, name + " must be a boolean"):
                        cfg.validate()

    def test_quoted_false_in_yaml_is_rejected_before_benchmarking(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "experiment.yaml"
            path.write_text('require_provenance: "false"\n')
            with self.assertRaisesRegex(ValueError, "require_provenance must be a boolean"):
                ExperimentConfig.from_yaml(str(path))


class WindowFailureTests(unittest.TestCase):
    def test_failed_warmup_stream_cannot_supply_release_throughput(self):
        cfg = ExperimentConfig(require_zero_request_failures=True)
        success = RequestRecord("single", 3, 2, t_start=11.0,
                                t_first_token=12.0, t_end=13.0,
                                output_tokens=2, token_times=[12.0, 12.5],
                                success=True)
        warmup_failure = RequestRecord("single", 3, 2, t_start=1.0,
                                       t_first_token=11.0, t_end=12.0,
                                       output_tokens=1, token_times=[11.0],
                                       error="stream ended without [DONE]")
        summary = aggregate([success, warmup_failure], cfg, 10.0, 20.0)

        self.assertEqual(summary["requests_failed"], 0)
        self.assertEqual(summary["total_output_tokens"], 3)
        self.assertEqual(summary["failed_requests_with_window_tokens"], 1)
        with self.assertRaisesRegex(RuntimeError, "window tokens from failed"):
            _validate_result_quality(summary, cfg)

        # A failure entirely outside the measured cohort does not contribute
        # output to the result and does not invalidate an otherwise good point.
        warmup_failure.token_times = [9.0]
        summary = aggregate([success, warmup_failure], cfg, 10.0, 20.0)
        self.assertEqual(summary["failed_requests_with_window_tokens"], 0)
        _validate_result_quality(summary, cfg)


if __name__ == "__main__":
    unittest.main()
