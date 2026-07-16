"""Aggregate request records into the metrics reported in the paper.

Accounting policy (chosen after the 60s- and 600s-window artifacts):

Throughput counts tokens *received during the measurement window* across all
requests, including requests that started in warmup and requests that are still
streaming when the window closes. This is a steady-state rate that stays honest
when a single request's end-to-end time is comparable to the window (at high
concurrency on large dense models, e2e p50 exceeded 500s in a 600s window and
completed-in-window throughput read near zero while the server was busy).

Latency percentiles and goodput are computed over requests *started in* the
window and tracked to completion, even when they finish after the window closes
(the runner drains in-flight requests before returning). Completed-in-window
counts and the completed-only rate are retained for the methodology diagnostic
figure, not as headline numbers.
"""
from __future__ import annotations
import math
import numpy as np


def _pctls(vals, ps=(50, 90, 95, 99)):
    arr = np.array([v for v in vals if not math.isnan(v)], dtype=float)
    if arr.size == 0:
        out = {f"p{p}": float("nan") for p in ps}
        out["mean"] = float("nan")
        return out
    out = {f"p{p}": float(np.percentile(arr, p)) for p in ps}
    out["mean"] = float(arr.mean())
    return out


def _tokens_in_window(records, window_start, window_end):
    total = 0
    for r in records:
        # token_times may be absent on minimal records (older raw files, tests)
        times = getattr(r, "token_times", None) or []
        total += sum(1 for t in times if window_start <= t < window_end)
    return total


def aggregate(records, cfg, window_start, window_end, power=None, vmetrics=None,
              worker_power=None):
    measure_s = window_end - window_start
    started = [r for r in records if window_start <= r.t_start < window_end]
    ok = [r for r in started if r.success]
    failed = [r for r in started if not r.success]
    completed_in_window = [r for r in records
                           if r.success and window_start <= r.t_end < window_end]
    started_and_completed_in_window = [
        r for r in ok if r.t_end < window_end
    ]

    n = len(ok)
    tokens_in_window = _tokens_in_window(records, window_start, window_end)
    completed_tokens = sum(r.output_tokens for r in completed_in_window)
    token_gaps = [gap for r in ok for gap in r.inter_token_latencies]
    avg_input = (sum(r.input_len for r in started) / len(started)) if started else float("nan")
    avg_requested_output = (
        sum(r.output_len for r in started) / len(started)
        if started else float("nan")
    )

    bucket_summaries = []
    for input_len, output_len in sorted(
            {(r.input_len, r.output_len) for r in started}):
        bucket_started = [r for r in started
                          if r.input_len == input_len and r.output_len == output_len]
        bucket_ok = [r for r in bucket_started if r.success]
        bucket_summaries.append({
            "input_len": input_len,
            "output_len": output_len,
            "requests_started": len(bucket_started),
            "requests_completed": len(bucket_ok),
            "requests_failed": len(bucket_started) - len(bucket_ok),
            "ttft_s": _pctls([r.ttft for r in bucket_ok]),
            "mean_itl_s": _pctls([r.mean_itl for r in bucket_ok]),
            "e2e_s": _pctls([r.e2e for r in bucket_ok]),
        })

    def _goodput(itl_slo):
        # A request is "good" if it met the TTFT bound and its mean ITL cleared
        # the tier. NaN mean_itl (single-token outputs have no gaps) is not an
        # ITL violation, so it does not disqualify.
        return [r for r in ok
                if r.ttft <= cfg.ttft_slo_s
                and (math.isnan(r.mean_itl) or r.mean_itl <= itl_slo)]

    good = _goodput(cfg.itl_slo_s)
    good2 = _goodput(cfg.itl_slo_s2)

    summary = {
        "config_label": cfg.config_label,
        "mode": cfg.mode,
        "concurrency": cfg.concurrency if cfg.mode == "closed" else None,
        "target_qps": cfg.qps if cfg.mode == "open" else None,
        "measure_s": measure_s,
        "window_policy": ("throughput: tokens received in window (all requests); "
                          "latency/goodput: requests started in window, tracked "
                          "to completion"),
        # perf_counter bounds let raw.jsonl token_times be re-aggregated post hoc
        "window_start_perf": window_start,
        "window_end_perf": window_end,
        "requests_started": len(started),
        "requests_completed": n,
        "requests_failed": len(failed),
        "requests_completed_in_window": len(completed_in_window),
        "requests_started_and_completed_in_window":
            len(started_and_completed_in_window),
        "avg_input_tokens": avg_input,
        "avg_requested_output_tokens": avg_requested_output,
        "workload_buckets": bucket_summaries,
        "total_output_tokens": tokens_in_window,
        "completed_output_tokens": completed_tokens,
        "request_throughput_rps": n / measure_s,
        "output_token_throughput_tps": tokens_in_window / measure_s,
        "completed_token_throughput_tps": completed_tokens / measure_s,
        "goodput_rps": len(good) / measure_s,
        "goodput_itl2_rps": len(good2) / measure_s,
        "slo": {"ttft_s": cfg.ttft_slo_s, "itl_s": cfg.itl_slo_s,
                "itl_s2": cfg.itl_slo_s2},
        "ttft_s": _pctls([r.ttft for r in ok]),
        "itl_s": _pctls(token_gaps),
        "mean_itl_s": _pctls([r.mean_itl for r in ok]),
        "e2e_s": _pctls([r.e2e for r in ok]),
    }

    if power is not None:
        head_e = power.energy_joules(window_start, window_end)
        head_mp = power.mean_power(window_start, window_end)
        worker_e = worker_mp = float("nan")
        if worker_power is not None:
            worker_e = worker_power.energy_joules(window_start, window_end)
            worker_mp = worker_power.mean_power(window_start, window_end)
        have_head = not math.isnan(head_mp) and not math.isnan(head_e)
        have_worker = not math.isnan(worker_mp) and not math.isnan(worker_e)
        total_mp = ((head_mp if have_head else 0.0)
                    + (worker_mp if have_worker else 0.0))
        total_e = ((head_e if have_head else 0.0)
                   + (worker_e if have_worker else 0.0))
        power_nodes = int(have_head) + int(have_worker)
        def coverage(sampler):
            if hasattr(sampler, "coverage"):
                return sampler.coverage(window_start, window_end)
            # Compatibility for custom sampler adapters: their energy remains
            # usable for exploratory summaries, while strict release validation
            # rejects the missing density metadata.
            return {"sample_count": 0, "max_gap_s": float("nan")}

        head_coverage = coverage(power)
        worker_coverage = (coverage(worker_power) if worker_power is not None else
                           {"sample_count": 0, "max_gap_s": float("nan")})
        summary["energy"] = {
            # totals over every sampled node; head/worker splits alongside so
            # dual-node rows are never silently head-only again
            "mean_power_w": total_mp if power_nodes else float("nan"),
            "mean_power_w_head": head_mp if have_head else float("nan"),
            "mean_power_w_worker": worker_mp if have_worker else float("nan"),
            "power_nodes": power_nodes,
            "power_samples_head": head_coverage["sample_count"],
            "power_samples_worker": worker_coverage["sample_count"],
            "power_max_gap_s_head": head_coverage["max_gap_s"],
            "power_max_gap_s_worker": worker_coverage["max_gap_s"],
            "power_scope": {
                "nvidia_smi": "gpu_devices",
                "tegrastats": "configured_rails",
                "sysfs": "configured_sensor",
            }.get(cfg.power_backend, "configured_sensor"),
            "energy_j": total_e if power_nodes else float("nan"),
            "energy_per_token_j": (total_e / tokens_in_window)
            if tokens_in_window and power_nodes else float("nan"),
            "tokens_per_watt": ((tokens_in_window / measure_s) / total_mp)
            if total_mp and power_nodes else float("nan"),
        }
    if vmetrics is not None:
        summary["vllm_metrics"] = vmetrics.summary(window_start, window_end)
    return summary
