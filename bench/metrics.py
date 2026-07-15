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

    n = len(ok)
    tokens_in_window = _tokens_in_window(records, window_start, window_end)
    completed_tokens = sum(r.output_tokens for r in completed_in_window)
    token_gaps = [gap for r in ok for gap in r.inter_token_latencies]

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
        have_worker = not math.isnan(worker_mp)
        total_mp = head_mp + (worker_mp if have_worker else 0.0)
        total_e = head_e + (worker_e if have_worker else 0.0)
        summary["energy"] = {
            # totals over every sampled node; head/worker splits alongside so
            # dual-node rows are never silently head-only again
            "mean_power_w": total_mp,
            "mean_power_w_head": head_mp,
            "mean_power_w_worker": worker_mp if have_worker else float("nan"),
            "power_nodes": 2 if have_worker else 1,
            "energy_j": total_e,
            "energy_per_token_j": (total_e / tokens_in_window)
            if tokens_in_window else float("nan"),
            "tokens_per_watt": ((tokens_in_window / measure_s) / total_mp)
            if total_mp and not math.isnan(total_mp) else float("nan"),
        }
    if vmetrics is not None:
        summary["vllm_metrics"] = vmetrics.summary(window_start, window_end)
    return summary
