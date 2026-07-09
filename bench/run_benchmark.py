"""Run one measurement point: warmup, then a steady measurement window."""
from __future__ import annotations
import argparse
import asyncio
import csv
import json
import math
import os
import time
import urllib.request

from .config import ExperimentConfig
from .workload import Workload
from .runner import run_phase
from .monitors import PowerSampler, VLLMMetricsScraper
from .metrics import aggregate
from . import server_info


INDEX_FIELDS = [
    "run_tag",
    "config_label",
    "model_label",
    "model",
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
    "goodput_itl2_rps",
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
    # server-side counters over the window (robust to a short measurement window)
    "server_prompt_tokens",
    "server_generation_tokens",
    "server_prompt_tokens_cached",
    "server_gen_tps",
    "num_preemptions",
    # served-model capacity and launch configuration (constant per server)
    "max_model_len",
    "kv_cache_dtype",
    "gpu_memory_utilization",
    "block_size",
    "gpu_blocks",
    "total_kv_tokens",
    "weight_gb",
    "kv_cache_gb",
    "worker_weight_gb",
    "worker_kv_cache_gb",
    "max_concurrency",
    # energy
    "mean_power_w",
    "energy_per_token_j",
    "tokens_per_watt",
]


def _ensure_writable_hf_cache():
    """Fall back to a client-local HF cache when the shared one is unwritable.

    The serving container downloads models into the mounted HF cache as root,
    leaving ~/.cache/huggingface root-owned on the host; the client's own
    tokenizer/config downloads then die with PermissionError mid-sweep. When
    the default hub cache is not writable (and the user has not pointed HF
    elsewhere), redirect this process to a separate user-owned cache. It only
    ever holds tokenizers and configs, a few MB per model.
    """
    if any(os.environ.get(v) for v in
           ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HF_HOME")):
        return
    hub = os.path.expanduser("~/.cache/huggingface/hub")
    probe = hub
    while probe != os.path.dirname(probe) and not os.path.exists(probe):
        probe = os.path.dirname(probe)
    if os.access(probe, os.W_OK):
        return
    alt = os.path.expanduser("~/.cache/huggingface-client/hub")
    os.makedirs(alt, exist_ok=True)
    os.environ["HF_HUB_CACHE"] = alt
    print(f"[bench] shared HF cache {hub} is not writable (owned by the "
          f"serving container's root user); using {alt} for client-side "
          "tokenizer/config downloads")


def _load_tokenizer(cfg):
    if not cfg.tokenizer:
        return None
    os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
    from transformers import AutoTokenizer
    return AutoTokenizer.from_pretrained(cfg.tokenizer,
                                         trust_remote_code=cfg.trust_remote_code)


def _resolve_model(cfg):
    if cfg.model:
        return cfg.model
    url = cfg.base_url.rstrip("/") + "/v1/models"
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode())["data"][0]["id"]


def _model_slug(model: str) -> str:
    """Short filesystem/plot-friendly slug from a served model name.

    e.g. "Qwen/Qwen3-8B" -> "qwen3-8b", "meta-llama/Llama-3.1-70B-Instruct"
    -> "llama-3.1-70b-instruct".
    """
    base = (model or "").rsplit("/", 1)[-1].lower()
    slug = "".join(c if (c.isalnum() or c in "-.") else "-" for c in base)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-") or "model"


def _json_safe(obj):
    """Replace NaN/Inf with None so the output files are strict JSON.

    json.dump writes bare ``NaN``, which Python reads back but jq, JavaScript,
    and most non-Python parsers reject; empty percentile sets and failed
    requests would otherwise poison every consumer of the release artifacts.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


def _append_index(out_dir, s):
    idx = os.path.join(out_dir, "index.csv")
    vllm = s.get("vllm_metrics", {})
    energy = s.get("energy", {})
    info = s.get("server_info", {})

    def vmetric(name, stat):
        return vllm.get(name, {}).get(stat)

    def vcounter(name, stat="delta"):
        return vllm.get(name, {}).get(stat)

    gen_delta = vcounter("vllm:generation_tokens_total")
    measure_s = s["measure_s"]
    row = {
        "run_tag": s["run_tag"],
        "config_label": s["config_label"],
        "model_label": s.get("model_label", ""),
        "model": s.get("model", ""),
        "mode": s["mode"],
        "concurrency": s["concurrency"],
        "target_qps": s["target_qps"],
        "measure_s": measure_s,
        "requests_started": s["requests_started"],
        "requests_completed": s["requests_completed"],
        "requests_failed": s["requests_failed"],
        "total_output_tokens": s["total_output_tokens"],
        "req_thru_rps": s["request_throughput_rps"],
        "tok_thru_tps": s["output_token_throughput_tps"],
        "goodput_rps": s["goodput_rps"],
        "goodput_itl2_rps": s["goodput_itl2_rps"],
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
        "server_prompt_tokens": vcounter("vllm:prompt_tokens_total"),
        "server_generation_tokens": gen_delta,
        "server_prompt_tokens_cached": vcounter("vllm:prompt_tokens_cached_total"),
        "server_gen_tps": (gen_delta / measure_s)
        if (gen_delta is not None and measure_s) else None,
        "num_preemptions": vcounter("vllm:num_preemptions_total"),
        "max_model_len": info.get("max_model_len"),
        "kv_cache_dtype": info.get("kv_cache_dtype"),
        "gpu_memory_utilization": info.get("gpu_memory_utilization"),
        "block_size": info.get("block_size"),
        "gpu_blocks": info.get("gpu_blocks"),
        "total_kv_tokens": info.get("total_kv_tokens"),
        "weight_gb": info.get("weight_gb"),
        "kv_cache_gb": info.get("kv_cache_gb"),
        "worker_weight_gb": info.get("worker_weight_gb"),
        "worker_kv_cache_gb": info.get("worker_kv_cache_gb"),
        "max_concurrency": info.get("max_concurrency"),
        "mean_power_w": energy.get("mean_power_w"),
        "energy_per_token_j": energy.get("energy_per_token_j"),
        "tokens_per_watt": energy.get("tokens_per_watt"),
    }
    # An existing but zero-byte index (touch, or a crash before the first write)
    # still needs its header, or every subsequent append is misaligned.
    new = not os.path.exists(idx) or os.path.getsize(idx) == 0
    if not new:
        with open(idx, "r", newline="") as f:
            header = f.readline().rstrip("\r\n")
        if header != ",".join(INDEX_FIELDS):
            raise SystemExit(
                f"index schema mismatch in {idx}: its header does not match this "
                "build's INDEX_FIELDS (the columns changed between versions). "
                "Appending would misalign columns. Write to a fresh OUT_DIR, or "
                "move the old index aside, and re-plot the old and new indexes "
                "separately."
            )
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
    ap.add_argument("--model-label", help="short model slug for tags/plots; "
                    "defaults to a slug of the served model name")
    ap.add_argument("--server-log", help="path to a captured vLLM stdout log for "
                    "exact capacity figures (max concurrency, gpu blocks)")
    ap.add_argument("--collect-hf-metadata", action="store_true", default=None,
                    help="query Hugging Face for model config/checkpoint metadata "
                    "when server logs do not provide capacity facts")
    ap.add_argument("--no-hf-metadata", action="store_false",
                    dest="collect_hf_metadata",
                    help="skip Hugging Face metadata lookups after measurement")
    ap.add_argument("--model", help="served model name; overrides config and auto-resolve")
    ap.add_argument("--tokenizer", help="HF tokenizer id or path; empty string disables")
    ap.add_argument("--trust-remote-code", action="store_true", default=None,
                    help="pass trust_remote_code=True to the tokenizer and HF "
                    "config loads (models that ship custom code, e.g. Nemotron)")
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
    if args.trust_remote_code:
        cfg.trust_remote_code = True
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
    if args.model_label is not None:
        cfg.model_label = args.model_label
    if args.server_log is not None:
        cfg.server_log = args.server_log
    if args.collect_hf_metadata is not None:
        cfg.collect_hf_metadata = args.collect_hf_metadata

    cfg.model = _resolve_model(cfg)
    if cfg.tokenizer and cfg.use_token_ids and cfg.tokenizer != cfg.model:
        print(f"[bench] WARNING: tokenizer '{cfg.tokenizer}' differs from the "
              f"served model '{cfg.model}'. Token-id prompts are only exact "
              "with the served model's own tokenizer; a mismatched vocabulary "
              "gives wrong input lengths or request errors. Pass --tokenizer "
              "matching the model unless this is intentional (e.g. a local "
              "copy of the same tokenizer).")
    # A model slug feeds the index/plots. Only an explicit label prefixes the run
    # tag (multi-model sweeps set one); the single-model workflow keeps its tags.
    explicit_label = bool(cfg.model_label)
    if not cfg.model_label:
        cfg.model_label = _model_slug(cfg.model)
    # Before the first transformers/huggingface_hub import reads the HF env.
    if cfg.tokenizer or cfg.collect_hf_metadata:
        _ensure_writable_hf_cache()
    workload = Workload(cfg, _load_tokenizer(cfg))

    os.makedirs(cfg.out_dir, exist_ok=True)
    point = cfg.concurrency if cfg.mode == "closed" else cfg.qps
    prefix = f"{cfg.model_label}_" if explicit_label else ""
    tag = f"{prefix}{cfg.config_label}_{cfg.mode}_{point}_{time.strftime('%Y%m%d-%H%M%S')}"
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
        # Seed a baseline before window_start; the background thread interval is
        # one second, which is longer than some smoke-test windows.
        vmet.scrape_once()

    print(f"[bench] measure {cfg.measure_s}s")
    window_start = time.perf_counter()
    records = asyncio.run(run_phase(cfg, workload, cfg.measure_s))
    logical_window_end = window_start + cfg.measure_s
    actual_end = time.perf_counter()

    power.stop()
    if vmet:
        vmet.stop()

    # Snapshot the served model's capacity/launch configuration. Best effort:
    # any field that cannot be resolved is left out rather than failing the run.
    info = server_info.collect(cfg.base_url, cfg.model, cfg.server_log,
                               compute_from_hf=cfg.collect_hf_metadata,
                               trust_remote_code=cfg.trust_remote_code)

    summary = aggregate(records, cfg, window_start, logical_window_end,
                        power if cfg.power_backend != "none" else None, vmet)
    summary["run_tag"] = tag
    summary["model"] = cfg.model
    summary["model_label"] = cfg.model_label
    summary["server_info"] = info
    summary["config"] = cfg.to_dict()
    summary["drain_s"] = max(0.0, actual_end - logical_window_end)
    # NaN -> null for the files; the console print below keeps the raw values.
    safe_summary = _json_safe(summary)

    with open(os.path.join(cfg.out_dir, f"{tag}.raw.jsonl"), "w") as f:
        for r in records:
            f.write(json.dumps(_json_safe(r.to_json())) + "\n")
    with open(os.path.join(cfg.out_dir, f"{tag}.summary.json"), "w") as f:
        json.dump(safe_summary, f, indent=2)
    _append_index(cfg.out_dir, safe_summary)

    print(f"[bench] tok_thru={summary['output_token_throughput_tps']:.1f} tok/s  "
          f"ttft_p50={summary['ttft_s']['p50'] * 1000:.0f}ms  "
          f"itl_p50={summary['itl_s']['p50'] * 1000:.1f}ms  "
          f"goodput={summary['goodput_rps']:.2f} rps")


if __name__ == "__main__":
    main()
