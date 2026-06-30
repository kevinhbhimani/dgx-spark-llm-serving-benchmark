"""Aggregate request records into the metrics reported in the paper."""
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


def aggregate(records, cfg, window_start, window_end, power=None, vmetrics=None):
    measure_s = window_end - window_start
    started = [r for r in records if window_start <= r.t_start < window_end]
    completed = [r for r in records if window_start <= r.t_end < window_end]
    in_win = [r for r in completed if r.success]
    failed = [r for r in completed if not r.success]

    n = len(in_win)
    total_out = sum(r.output_tokens for r in in_win)
    token_gaps = [gap for r in in_win for gap in r.inter_token_latencies]
    good = [r for r in in_win
            if r.ttft <= cfg.ttft_slo_s
            and (math.isnan(r.mean_itl) or r.mean_itl <= cfg.itl_slo_s)]

    summary = {
        "config_label": cfg.config_label,
        "mode": cfg.mode,
        "concurrency": cfg.concurrency if cfg.mode == "closed" else None,
        "target_qps": cfg.qps if cfg.mode == "open" else None,
        "measure_s": measure_s,
        "window_policy": "completed_in_window",
        "requests_started": len(started),
        "requests_completed": n,
        "requests_failed": len(failed),
        "total_output_tokens": total_out,
        "request_throughput_rps": n / measure_s,
        "output_token_throughput_tps": total_out / measure_s,
        "goodput_rps": len(good) / measure_s,
        "slo": {"ttft_s": cfg.ttft_slo_s, "itl_s": cfg.itl_slo_s},
        "ttft_s": _pctls([r.ttft for r in in_win]),
        "itl_s": _pctls(token_gaps),
        "mean_itl_s": _pctls([r.mean_itl for r in in_win]),
        "e2e_s": _pctls([r.e2e for r in in_win]),
    }

    if power is not None:
        e = power.energy_joules(window_start, window_end)
        mp = power.mean_power(window_start, window_end)
        summary["energy"] = {
            "mean_power_w": mp,
            "energy_j": e,
            "energy_per_token_j": (e / total_out) if total_out else float("nan"),
            "tokens_per_watt": ((total_out / measure_s) / mp)
            if mp and not math.isnan(mp) else float("nan"),
        }
    if vmetrics is not None:
        summary["vllm_metrics"] = vmetrics.summary(window_start, window_end)
    return summary
