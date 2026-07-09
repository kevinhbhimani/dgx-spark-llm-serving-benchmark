"""Experiment configuration with optional YAML loading."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
import yaml


@dataclass
class WorkloadBucket:
    input_len: int
    output_len: int
    weight: float = 1.0


@dataclass
class ExperimentConfig:
    # server
    base_url: str = "http://127.0.0.1:8000"
    endpoint: str = "/v1/completions"
    model: str = ""               # served name; resolved from /v1/models if empty
    api_key: str = "EMPTY"

    # prompt construction
    tokenizer: str = ""           # HF id or local path
    trust_remote_code: bool = False  # for tokenizer/config loads of custom-code models
    use_token_ids: bool = True    # send token ids for exact input length

    # workload
    trace_path: str = ""          # JSONL trace overrides the buckets if set
    buckets: list = field(default_factory=lambda: [
        WorkloadBucket(128, 128, 1.0),
        WorkloadBucket(1024, 256, 1.0),
        WorkloadBucket(4096, 256, 1.0),
        WorkloadBucket(8192, 512, 0.5),
    ])
    seed: int = 1234

    # load pattern
    mode: str = "closed"          # closed or open
    concurrency: int = 16
    qps: float = 4.0
    max_inflight: int = 512
    warmup_s: float = 15.0
    measure_s: float = 60.0
    request_timeout_s: float = 600.0

    # goodput service level objective. Two ITL tiers scored against one TTFT
    # bound: itl_slo_s is the strict interactive target, itl_slo_s2 a looser
    # tier that still separates bandwidth-bound models under load (their strict
    # goodput floors at ~0). goodput_rps uses itl_slo_s, goodput_itl2_rps uses
    # itl_slo_s2.
    ttft_slo_s: float = 1.0
    itl_slo_s: float = 0.05
    itl_slo_s2: float = 0.10

    # monitors
    power_backend: str = "none"   # none, nvidia_smi, sysfs, tegrastats
    power_sysfs_path: str = ""     # microwatt power file for the sysfs backend
    power_rails: list = field(default_factory=list)
    power_interval_ms: int = 100
    scrape_vllm_metrics: bool = True

    # labeling and output
    config_label: str = "single"
    model_label: str = ""         # short model slug for tags/plots; defaults from model
    server_log: str = ""          # optional vLLM stdout log for exact capacity figures
    collect_hf_metadata: bool = False  # optional HF Hub lookups for weight/KV fallback
    out_dir: str = "results"
    run_note: str = ""

    @staticmethod
    def from_yaml(path: str) -> "ExperimentConfig":
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        bucket_raw = raw.pop("buckets", None)
        cfg = ExperimentConfig(**raw)
        if bucket_raw is not None:
            cfg.buckets = [WorkloadBucket(**b) for b in bucket_raw]
        return cfg

    def to_dict(self) -> dict:
        d = asdict(self)
        d["buckets"] = [asdict(WorkloadBucket(**b)) if isinstance(b, dict)
                        else asdict(b) for b in self.buckets]
        return d
