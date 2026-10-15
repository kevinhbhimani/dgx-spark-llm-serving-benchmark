"""Experiment configuration with optional YAML loading."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
import hashlib
import math
from pathlib import Path
import re
import yaml


_SAFE_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


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
    model_revision: str = ""      # pinned HF commit used by the server
    api_key: str = "EMPTY"

    # prompt construction
    tokenizer: str = ""           # HF id or local path
    tokenizer_revision: str = ""  # pinned HF commit; defaults to model revision
    trust_remote_code: bool = False  # for tokenizer/config loads of custom-code models
    use_token_ids: bool = True    # send token ids for exact input length

    # workload
    trace_path: str = ""          # JSONL trace overrides the buckets if set
    # Populated from trace_path by from_yaml. The trace bytes are part of the
    # campaign identity so editing a trace cannot be resumed into an older run.
    trace_sha256: str = ""
    buckets: list = field(default_factory=lambda: [
        WorkloadBucket(128, 128, 1.0),
        WorkloadBucket(1024, 256, 1.0),
        WorkloadBucket(4096, 256, 1.0),
        WorkloadBucket(8192, 512, 0.5),
    ])
    seed: int = 1234
    # Smooth weighted round robin keeps every contiguous request slice close to
    # the configured bucket proportions. This matters at slow points where only
    # a few dozen requests start in the measurement window.
    balanced_workload: bool = True

    # Load pattern. Warmup, measure, and cooldown run as one continuous phase.
    # In closed loop, warmup is extended until every worker's first request has
    # finished. Cooldown is a minimum: replacement load continues until every
    # measurement-started request finishes, then remaining replacements drain.
    mode: str = "closed"          # closed for fixed concurrency, open for Poisson
    concurrency: int = 16
    # Canonical intended load rectangle for fail-closed campaign plotting.
    # scripts/run_all.sh supplies this even when a targeted repeat runs a subset.
    expected_concurrencies: str = ""
    qps: float = 4.0
    max_inflight: int = 512
    warmup_s: float = 15.0
    measure_s: float = 60.0
    cooldown_s: float = 0.0
    # Minimum-sample stopping rule (closed loop only). measure_s is then the
    # minimum window: while fewer than min_window_requests requests have
    # started inside it, the window end moves out in window_extension_step_s
    # steps, never past max_measure_s. 0 disables the rule.
    min_window_requests: int = 0
    max_measure_s: float = 0.0
    window_extension_step_s: float = 60.0
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
    # Release runs fail instead of silently writing incomplete observability.
    # Smoke tests and ad-hoc users can leave these false.
    require_power_data: bool = False
    require_vllm_metrics_data: bool = False
    require_provenance: bool = False
    # Release rows must be usable measurements, not merely well-formed files.
    require_zero_request_failures: bool = False
    min_successful_requests: int = 1
    # Dual-node runs: sample the worker node's GPU power too, via a Ray actor
    # pinned to node:<ip> inside the serving container (no SSH path exists from
    # head to worker). Both must be set; scripts/run_all.sh fills them in for
    # tp2/pp2 labels. Empty means head-node power only, which metrics flag.
    worker_power_container: str = ""
    worker_power_node_ip: str = ""
    head_node_ip: str = ""

    # labeling and output
    config_label: str = "single"
    model_label: str = ""         # short model slug for tags/plots; defaults from model
    serving_fingerprint: str = "" # launch-policy hash supplied by run_models.sh
    deployment_fingerprint: str = ""  # live server-state hash checked on resume
    repeat_id: int = 1             # repeated measurement-window number
    repeat_count: int = 1          # intended total windows in this campaign
    server_log: str = ""          # optional vLLM stdout log for exact capacity figures
    collect_hf_metadata: bool = False  # optional HF Hub lookups for weight/KV fallback
    out_dir: str = "results"
    run_note: str = ""

    @staticmethod
    def from_yaml(path: str) -> "ExperimentConfig":
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"experiment config must be a mapping: {path}")
        bucket_raw = raw.pop("buckets", None)
        trace_path = str(raw.get("trace_path") or "")
        if trace_path:
            trace = Path(trace_path).expanduser()
            if not trace.is_absolute():
                trace = Path(path).resolve().parent / trace
            trace = trace.resolve()
            if not trace.is_file():
                raise ValueError(f"trace file does not exist: {trace}")
            raw["trace_path"] = str(trace)
            raw["trace_sha256"] = hashlib.sha256(trace.read_bytes()).hexdigest()
        cfg = ExperimentConfig(**raw)
        if bucket_raw is not None:
            if not isinstance(bucket_raw, list):
                raise ValueError("buckets must be a list")
            cfg.buckets = [WorkloadBucket(**b) for b in bucket_raw]
        cfg.validate()
        return cfg

    def to_dict(self) -> dict:
        d = asdict(self)
        d["buckets"] = [asdict(WorkloadBucket(**b)) if isinstance(b, dict)
                        else asdict(b) for b in self.buckets]
        # Authentication is not a measurement parameter and must never land in
        # a summary artifact. Redaction also avoids secret-derived config hashes.
        d["api_key"] = "<redacted>" if d.get("api_key") else ""
        return d

    def validate(self) -> None:
        """Reject invalid measurement policy before network or GPU work."""

        def finite_number(value, name, *, positive=False, nonnegative=False):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value)):
                raise ValueError(f"{name} must be finite")
            if positive and value <= 0:
                raise ValueError(f"{name} must be positive")
            if nonnegative and value < 0:
                raise ValueError(f"{name} must be non-negative")

        if not isinstance(self.base_url, str) or not self.base_url.strip():
            raise ValueError("base_url must be non-empty")
        if not isinstance(self.endpoint, str) or not self.endpoint.startswith("/"):
            raise ValueError("endpoint must start with '/'")
        for name in ("config_label", "model_label"):
            value = getattr(self, name)
            if (not isinstance(value, str)
                    or (not value and name == "config_label")
                    or (value and _SAFE_LABEL_RE.fullmatch(value) is None)):
                raise ValueError(
                    f"{name} must match [A-Za-z0-9][A-Za-z0-9._-]*"
                )
        if self.mode not in {"closed", "open"}:
            raise ValueError("mode must be 'closed' or 'open'")
        if (isinstance(self.concurrency, bool)
                or not isinstance(self.concurrency, int)
                or self.concurrency < 1):
            raise ValueError("concurrency must be a positive integer")
        finite_number(self.qps, "qps", positive=True)
        if (isinstance(self.max_inflight, bool)
                or not isinstance(self.max_inflight, int)
                or self.max_inflight < 1):
            raise ValueError("max_inflight must be a positive integer")
        finite_number(self.warmup_s, "warmup_s", nonnegative=True)
        finite_number(self.measure_s, "measure_s", positive=True)
        finite_number(self.cooldown_s, "cooldown_s", nonnegative=True)
        if (isinstance(self.min_window_requests, bool)
                or not isinstance(self.min_window_requests, int)
                or self.min_window_requests < 0):
            raise ValueError("min_window_requests must be a non-negative integer")
        finite_number(self.max_measure_s, "max_measure_s", nonnegative=True)
        finite_number(self.window_extension_step_s, "window_extension_step_s",
                      positive=True)
        if self.min_window_requests:
            if self.mode != "closed":
                raise ValueError(
                    "min_window_requests is implemented for closed-loop sweeps only"
                )
            if self.max_measure_s < self.measure_s:
                raise ValueError(
                    "max_measure_s must be at least measure_s when "
                    "min_window_requests is set"
                )
        finite_number(self.request_timeout_s, "request_timeout_s", positive=True)
        finite_number(self.ttft_slo_s, "ttft_slo_s", positive=True)
        finite_number(self.itl_slo_s, "itl_slo_s", positive=True)
        finite_number(self.itl_slo_s2, "itl_slo_s2", positive=True)
        if self.itl_slo_s2 < self.itl_slo_s:
            raise ValueError("itl_slo_s2 must be at least itl_slo_s")
        if self.power_backend not in {"none", "nvidia_smi", "sysfs", "tegrastats"}:
            raise ValueError(f"unknown power_backend {self.power_backend!r}")
        if (isinstance(self.power_interval_ms, bool)
                or not isinstance(self.power_interval_ms, int)
                or self.power_interval_ms < 1):
            raise ValueError("power_interval_ms must be a positive integer")
        if self.power_backend == "sysfs" and not self.power_sysfs_path:
            raise ValueError("sysfs power backend requires power_sysfs_path")
        for name in ("repeat_id", "repeat_count", "min_successful_requests"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.repeat_id > self.repeat_count:
            raise ValueError("repeat_id cannot exceed repeat_count")
        if self.trace_path:
            trace = Path(self.trace_path)
            if not trace.is_file():
                raise ValueError(f"trace file does not exist: {trace}")
            digest = hashlib.sha256(trace.read_bytes()).hexdigest()
            if self.trace_sha256 and self.trace_sha256 != digest:
                raise ValueError("trace contents changed after configuration load")
            self.trace_sha256 = digest
        elif self.trace_sha256:
            raise ValueError("trace_sha256 is set without trace_path")
