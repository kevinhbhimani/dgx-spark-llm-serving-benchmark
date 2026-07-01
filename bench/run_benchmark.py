"""Run one measurement point: warmup, then a steady measurement window."""
from __future__ import annotations
import argparse
import asyncio
import csv
import json
import os
import time
import urllib.request

from .config import ExperimentConfig
from .workload import Workload
from .runner import run_phase
from .monitors import PowerSampler, VLLMMetricsScraper
from .metrics import aggregate


INDEX_FIELDS = [
    "run_tag",
    "config_label",
    "mode",
    "concurrency",
    "target_qps",
    "measure_s",
    "requests_started",
    "requests_completed",
    "requests_failed",
    "total_output_tokens",
    "req_thru_rps",
    "tok_thru_tps",
    "goodput_rps",
    "ttft_p50",
    "ttft_p99",
    "itl_p50",
    "itl_p99",
    "mean_itl_p50",
    "e2e_p50",
    "e2e_p99",
    "kv_cache_usage_mean",
    "kv_cache_usage_max",
    "requests_running_mean",
    "requests_running_max",
    "requests_waiting_mean",
    "requests_waiting_max",
    "mean_power_w",
    "energy_per_token_j",
    "tokens_per_watt",
]


def _load_tokenizer(cfg):
    if not cfg.tokenizer:
        return None
    os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
    from transformers import AutoTokenizer
    return AutoTokenizer.from_pretrained(cfg.tokenizer)


def _resolve_model(cfg):
    if cfg.model:
        return cfg.model
    url = cfg.base_url.rstrip("/") + "/v1/models"
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode())["data"][0]["id"]


def _append_index(out_dir, s):
    idx = os.path.join(out_dir, "index.csv")
    vllm = s.get("vllm_metrics", {})
    energy = s.get("energy", {})

    def vmetric(name, stat):
        return vllm.get(name, {}).get(stat)

    row = {
        "run_tag": s["run_tag"],
        "config_label": s["config_label"],
        "mode": s["mode"],
        "concurrency": s["concurrency"],
        "target_qps": s["target_qps"],
        "measure_s": s["measure_s"],
        "requests_started": s["requests_started"],
        "requests_completed": s["requests_completed"],
        "requests_failed": s["requests_failed"],
        "total_output_tokens": s["total_output_tokens"],
        "req_thru_rps": s["request_throughput_rps"],
        "tok_thru_tps": s["output_token_throughput_tps"],
        "goodput_rps": s["goodput_rps"],
        "ttft_p50": s["ttft_s"]["p50"],
        "ttft_p99": s["ttft_s"]["p99"],
        "itl_p50": s["itl_s"]["p50"],
        "itl_p99": s["itl_s"]["p99"],
        "mean_itl_p50": s["mean_itl_s"]["p50"],
        "e2e_p50": s["e2e_s"]["p50"],
        "e2e_p99": s["e2e_s"]["p99"],
        "kv_cache_usage_mean": vmetric("vllm:kv_cache_usage_perc", "mean"),
        "kv_cache_usage_max": vmetric("vllm:kv_cache_usage_perc", "max"),
        "requests_running_mean": vmetric("vllm:num_requests_running", "mean"),
        "requests_running_max": vmetric("vllm:num_requests_running", "max"),
        "requests_waiting_mean": vmetric("vllm:num_requests_waiting", "mean"),
        "requests_waiting_max": vmetric("vllm:num_requests_waiting", "max"),
        "mean_power_w": energy.get("mean_power_w"),
        "energy_per_token_j": energy.get("energy_per_token_j"),
        "tokens_per_watt": energy.get("tokens_per_watt"),
    }
    new = not os.path.exists(idx)
    if not new and os.path.getsize(idx) > 0:
        with open(idx, "rb+") as f:
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                f.write(b"\n")
    with open(idx, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=INDEX_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


def main():
    ap = argparse.ArgumentParser(description="LLM serving benchmark for DGX Spark")
    ap.add_argument("--config", help="YAML config path")
    ap.add_argument("--config-label")
    ap.add_argument("--model", help="served model name; overrides config and auto-resolve")
    ap.add_argument("--tokenizer", help="HF tokenizer id or path; empty string disables")
    ap.add_argument("--mode", choices=["closed", "open"])
    ap.add_argument("--concurrency", type=int)
    ap.add_argument("--qps", type=float)
    ap.add_argument("--measure-s", type=float)
    ap.add_argument("--warmup-s", type=float)
    ap.add_argument("--base-url")
    ap.add_argument("--out-dir")
    ap.add_argument("--power-backend",
                    choices=["none", "nvidia_smi", "sysfs", "tegrastats"])
    args = ap.parse_args()

    cfg = ExperimentConfig.from_yaml(args.config) if args.config else ExperimentConfig()
    if args.config_label:
        cfg.config_label = args.config_label
    if args.model is not None:
        cfg.model = args.model
    if args.tokenizer is not None:
        cfg.tokenizer = args.tokenizer
    if args.mode:
        cfg.mode = args.mode
    if args.concurrency is not None:
        cfg.concurrency = args.concurrency
    if args.qps is not None:
        cfg.qps = args.qps
    if args.measure_s is not None:
        cfg.measure_s = args.measure_s
    if args.warmup_s is not None:
        cfg.warmup_s = args.warmup_s
    if args.base_url:
        cfg.base_url = args.base_url
    if args.out_dir:
        cfg.out_dir = args.out_dir
    if args.power_backend:
        cfg.power_backend = args.power_backend

    cfg.model = _resolve_model(cfg)
    workload = Workload(cfg, _load_tokenizer(cfg))

    os.makedirs(cfg.out_dir, exist_ok=True)
    point = cfg.concurrency if cfg.mode == "closed" else cfg.qps
    tag = f"{cfg.config_label}_{cfg.mode}_{point}_{time.strftime('%Y%m%d-%H%M%S')}"
    print(f"[bench] model={cfg.model} config={cfg.config_label} "
          f"mode={cfg.mode} point={point}")

    if cfg.warmup_s > 0:
        print(f"[bench] warmup {cfg.warmup_s}s")
        asyncio.run(run_phase(cfg, workload, cfg.warmup_s))

    power = PowerSampler(cfg.power_backend, cfg.power_interval_ms,
                         cfg.power_sysfs_path, cfg.power_rails)
    vmet = VLLMMetricsScraper(cfg.base_url) if cfg.scrape_vllm_metrics else None
    power.start()
    if vmet:
        vmet.start()

    print(f"[bench] measure {cfg.measure_s}s")
    window_start = time.perf_counter()
    records = asyncio.run(run_phase(cfg, workload, cfg.measure_s))
    logical_window_end = window_start + cfg.measure_s
    actual_end = time.perf_counter()

    power.stop()
    if vmet:
        vmet.stop()

    summary = aggregate(records, cfg, window_start, logical_window_end,
                        power if cfg.power_backend != "none" else None, vmet)
    summary["run_tag"] = tag
    summary["config"] = cfg.to_dict()
    summary["drain_s"] = max(0.0, actual_end - logical_window_end)

    with open(os.path.join(cfg.out_dir, f"{tag}.raw.jsonl"), "w") as f:
        for r in records:
            f.write(json.dumps(r.to_json()) + "\n")
    with open(os.path.join(cfg.out_dir, f"{tag}.summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    _append_index(cfg.out_dir, summary)

    print(f"[bench] tok_thru={summary['output_token_throughput_tps']:.1f} tok/s  "
          f"ttft_p50={summary['ttft_s']['p50'] * 1000:.0f}ms  "
          f"itl_p50={summary['itl_s']['p50'] * 1000:.1f}ms  "
          f"goodput={summary['goodput_rps']:.2f} rps")


if __name__ == "__main__":
    main()
