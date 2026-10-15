"""Run one measurement point: warmup, then a steady measurement window."""
from __future__ import annotations
import argparse
import asyncio
import csv
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import site
import subprocess
import sys
import tempfile
import time
import urllib.request

from .config import ExperimentConfig
from .workload import Workload
from .runner import run_phase
from .monitors import PowerSampler, VLLMMetricsScraper, WorkerPowerSampler
from .metrics import aggregate
from . import server_info


# v4: the closed-loop window may extend under the minimum-sample rule
# (min_window_requests), and the index records the configured window beside
# the actual one. Replicates are independent server launches.
METHODOLOGY_VERSION = "steady-exact-v4"


INDEX_FIELDS = [
    "run_tag",
    "raw_path",
    "raw_sha256",
    "raw_bytes",
    "summary_path",
    "summary_sha256",
    "summary_bytes",
    "power_samples_path",
    "power_samples_sha256",
    "power_samples_bytes",
    "vllm_samples_path",
    "vllm_samples_sha256",
    "vllm_samples_bytes",
    "serving_policy_path",
    "serving_policy_sha256",
    "serving_policy_bytes",
    "methodology_version",
    "config_file_sha256",
    "trace_sha256",
    "experiment_fingerprint",
    "harness_source_sha256",
    "client_environment_sha256",
    "harness_git_commit",
    "serving_fingerprint",
    "deployment_fingerprint",
    "model_revision",
    "tokenizer",
    "tokenizer_revision",
    "vllm_image",
    "dtype",
    "quantization",
    "tensor_parallel_size",
    "pipeline_parallel_size",
    "prefix_caching",
    "generation_config",
    "runtime_fingerprint",
    "repeat_id",
    "repeat_count",
    "config_label",
    "model_label",
    "model",
    "mode",
    "concurrency",
    "expected_concurrencies",
    "target_qps",
    "measure_s",
    "configured_measure_s",
    "measure_extension_s",
    "warmup_s",
    "configured_warmup_s",
    "warmup_extension_s",
    "cooldown_s",
    "load_extension_s",
    "drain_s",
    "requests_started",
    "requests_completed",
    "requests_failed",
    "requests_completed_in_window",
    "requests_started_and_completed_in_window",
    "avg_input_tokens",
    "avg_requested_output_tokens",
    "total_output_tokens",
    "req_thru_rps",
    "tok_thru_tps",
    "tok_thru_completed_tps",
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
    "server_prompt_tps",
    "server_metric_samples",
    "server_metric_sample_span_s",
    "server_metric_max_gap_s",
    "kv_metric_samples",
    "kv_metric_sample_span_s",
    "kv_metric_max_gap_s",
    "num_preemptions",
    # served-model capacity and launch configuration (constant per server)
    "max_model_len",
    "kv_cache_dtype",
    "gpu_memory_utilization",
    "block_size",
    "gpu_blocks",
    "total_kv_tokens",
    "total_kv_tokens_source",
    "hybrid_cache",
    "weight_gb",
    "kv_cache_gb",
    "worker_weight_gb",
    "worker_kv_cache_gb",
    "max_concurrency",
    "vllm_version",
    # energy. mean_power_w totals every sampled node; the head/worker split and
    # power_nodes make dual-node rows self-describing (power_nodes=1 on a tp2/pp2
    # row means the worker sampler failed and the energy is head-only again).
    "mean_power_w",
    "mean_power_w_head",
    "mean_power_w_worker",
    "power_nodes",
    "power_scope",
    "power_samples_head",
    "power_samples_worker",
    "power_max_gap_s_head",
    "power_max_gap_s_worker",
    "energy_per_token_j",
    "tokens_per_watt",
]


def _sha256_json(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _normalized_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def client_environment() -> dict:
    """Return the result-affecting client runtime in a stable form.

    Source hashes alone do not protect a resumed campaign from tokenizer,
    percentile, YAML, or streaming-client behavior changing after a package
    upgrade. Missing packages are recorded explicitly instead of being omitted.
    """
    packages = (
        "aiohttp", "numpy", "transformers", "tokenizers",
        "huggingface-hub", "PyYAML", "pandas", "matplotlib",
    )
    versions = {}
    for name in packages:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "<missing>"
    installed = {}
    for dist in importlib.metadata.distributions():
        name = dist.metadata.get("Name")
        if name:
            installed[_normalized_distribution_name(name)] = dist.version
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "virtual_environment": sys.prefix != sys.base_prefix,
        "user_site_enabled": bool(site.ENABLE_USER_SITE),
        "installed_distributions": [
            f"{name}=={version}" for name, version in sorted(installed.items())
        ],
        **versions,
    }


def _capture_client_environment() -> tuple[dict, str]:
    """Capture one environment object and hash those exact contents."""
    environment = client_environment()
    return environment, _sha256_json(environment)


def client_environment_fingerprint() -> str:
    return _capture_client_environment()[1]


DEPLOYMENT_INFO_FIELDS = (
    "vllm_version", "max_model_len", "block_size", "gpu_blocks",
    "total_kv_tokens", "total_kv_tokens_source", "kv_cache_dtype",
    "gpu_memory_utilization", "enable_prefix_caching", "hybrid_cache",
    "worker_weight_gb", "worker_kv_cache_gb", "max_concurrency",
)
DEPLOYMENT_REQUIRED_FIELDS = (
    "vllm_version", "max_model_len", "gpu_blocks", "total_kv_tokens",
    "gpu_memory_utilization", "enable_prefix_caching",
)


def deployment_state(info: dict) -> dict:
    """Return only stable, serving-relevant live capacity fields."""
    missing = [name for name in DEPLOYMENT_REQUIRED_FIELDS
               if info.get(name) in (None, "")]
    if missing:
        raise RuntimeError(
            "live deployment metadata is incomplete: " + ", ".join(missing)
        )
    return {name: info.get(name) for name in DEPLOYMENT_INFO_FIELDS}


def deployment_fingerprint(serving_fingerprint: str, info: dict) -> str:
    """Hash launch policy plus the capacity/runtime observed after startup."""
    return _sha256_json({
        "serving_fingerprint": serving_fingerprint,
        "deployment_state": deployment_state(info),
    })


_EXPERIMENT_FINGERPRINT_TRANSIENT_FIELDS = frozenset((
    "repeat_id",
    "repeat_count",
    "server_log",
    "deployment_fingerprint",
))


def _experiment_fingerprint(resolved_config: dict,
                            harness_source_sha256: str) -> str:
    """Hash measurement identity: what was requested, not what booted.

    Live capacity is deliberately excluded. vLLM's memory profiling settles on
    a slightly different KV block count on every launch, so folding it in here
    would give repeats of one arm different experiment identities whenever they
    span a server restart -- which every repeat does when repeats are collected
    across launches on purpose. deployment_fingerprint() records that live
    state separately, per row.
    """
    stable_config = {
        key: value for key, value in resolved_config.items()
        if key not in _EXPERIMENT_FINGERPRINT_TRANSIENT_FIELDS
    }
    return _sha256_json({
        "methodology_version": METHODOLOGY_VERSION,
        "harness_source_sha256": harness_source_sha256,
        "resolved_config": stable_config,
    })


def collect_deployment_info(base_url: str, model: str, server_log: str,
                            *, compute_from_hf: bool = False,
                            trust_remote_code: bool = False,
                            attempts: int = 3, delay_s: float = 1.0) -> dict:
    """Collect live identity with retries and merge transiently missing fields."""
    merged: dict = {}
    for attempt in range(attempts):
        current = server_info.collect(
            base_url, model, server_log, compute_from_hf=compute_from_hf,
            trust_remote_code=trust_remote_code,
        )
        merged.update({key: value for key, value in current.items()
                       if value is not None})
        if attempt + 1 < attempts:
            time.sleep(delay_s)
    deployment_state(merged)
    return merged


def config_file_fingerprint(path: str | None,
                            overrides: dict | None = None) -> str:
    """Hash the canonical effective campaign configuration.

    Applying overrides to the parsed dataclass makes an explicit override equal
    to the YAML value hash identically, while a genuinely different timing,
    metadata, or power policy cannot be resumed into the base campaign.
    """
    cfg = ExperimentConfig.from_yaml(path) if path else ExperimentConfig()
    effective_overrides = {
        key: value for key, value in (overrides or {}).items()
        if value is not None
    }
    for key, value in effective_overrides.items():
        if not hasattr(cfg, key):
            raise ValueError(f"unknown ExperimentConfig override {key!r}")
        setattr(cfg, key, value)
    cfg.validate()
    return _sha256_json(cfg.to_dict())


def _source_fingerprint() -> str:
    """Hash measurement-affecting source files, including uncommitted edits."""
    root = Path(__file__).resolve().parents[1]
    paths = list((root / "bench").glob("*.py"))
    paths += [root / "scripts" / name for name in (
        "launch_server.sh", "start_ray.sh", "run_all.sh", "run_models.sh",
        "models_for_config.py", "swept_concurrencies.py",
        "cluster_runtime_fingerprint.py",
    )]
    h = hashlib.sha256()
    for path in sorted((p for p in paths if p.exists()), key=lambda p: str(p)):
        h.update(str(path.relative_to(root)).encode() + b"\0")
        h.update(path.read_bytes() + b"\0")
    return h.hexdigest()


def _git_commit() -> str | None:
    try:
        root = Path(__file__).resolve().parents[1]
        p = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                           text=True, capture_output=True, timeout=5, check=True)
        return p.stdout.strip() or None
    except Exception:
        return None


_INDEX_COUNT_FIELDS = (
    "requests_started", "requests_completed", "requests_failed",
    "requests_completed_in_window",
    "requests_started_and_completed_in_window", "total_output_tokens",
    "server_metric_samples", "kv_metric_samples", "power_samples_head",
    "power_samples_worker", "power_nodes",
)
_ARTIFACT_FIELDS = (
    ("raw_path", "raw_sha256", "raw_bytes", True),
    ("summary_path", "summary_sha256", "summary_bytes", True),
    ("power_samples_path", "power_samples_sha256", "power_samples_bytes", False),
    ("vllm_samples_path", "vllm_samples_sha256", "vllm_samples_bytes", False),
    ("serving_policy_path", "serving_policy_sha256", "serving_policy_bytes", False),
)


def _parse_expected_concurrencies(value, context: str) -> tuple[int, ...]:
    tokens = str(value or "").split()
    if not tokens:
        raise RuntimeError(f"{context} has no expected_concurrencies")
    values = []
    for token in tokens:
        if not re.fullmatch(r"[1-9][0-9]*", token):
            raise RuntimeError(
                f"{context} has invalid expected concurrency {token!r}"
            )
        number = int(token)
        if number in values:
            raise RuntimeError(
                f"{context} repeats expected concurrency {number}"
            )
        values.append(number)
    return tuple(values)


def _integral_field(row: dict, field: str, row_number: int, *,
                    positive: bool = False, required: bool = True) -> int | None:
    raw = row.get(field)
    if raw in (None, ""):
        if required:
            raise RuntimeError(
                f"index row {row_number} has no value for {field}"
            )
        return None
    try:
        number = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        number = Decimal("NaN")
    integral = (number.is_finite()
                and number == number.to_integral_value())
    minimum_ok = (integral
                  and (number > 0 if positive else number >= 0))
    if not minimum_ok:
        qualifier = "positive " if positive else "non-negative "
        raise RuntimeError(
            f"index row {row_number} has invalid {field}={raw!r}; "
            f"expected a {qualifier}integer"
        )
    return int(number)


def _validate_index_rows(rows: list[dict], idx: str) -> None:
    required = (
        "run_tag", "methodology_version", "config_file_sha256",
        "experiment_fingerprint", "harness_source_sha256",
        "client_environment_sha256", "deployment_fingerprint",
        "config_label", "mode", "measure_s",
    )
    point_ids = set()
    run_tags = set()
    for row_number, row in enumerate(rows, start=2):
        missing = [field for field in required if row.get(field) in (None, "")]
        if not (row.get("model_label") or row.get("model")):
            missing.append("model_label/model")
        if missing:
            raise RuntimeError(
                f"index row {row_number} in {idx} is missing required values: "
                + ", ".join(missing)
            )
        for path_field, hash_field, bytes_field, required_artifact in _ARTIFACT_FIELDS:
            artifact_path = row.get(path_field) or ""
            artifact_hash = row.get(hash_field) or ""
            artifact_bytes = row.get(bytes_field)
            if required_artifact and not artifact_path:
                raise RuntimeError(
                    f"index row {row_number} has no value for {path_field}"
                )
            if not artifact_path:
                if artifact_hash or artifact_bytes not in (None, ""):
                    raise RuntimeError(
                        f"index row {row_number} has partial artifact metadata "
                        f"for {path_field}"
                    )
                continue
            if Path(artifact_path).name != artifact_path:
                raise RuntimeError(
                    f"index row {row_number} has unsafe artifact path "
                    f"{artifact_path!r}"
                )
            if not re.fullmatch(r"[0-9a-f]{64}", artifact_hash):
                raise RuntimeError(
                    f"index row {row_number} has invalid {hash_field}"
                )
            expected_size = _integral_field(row, bytes_field, row_number)
            artifact = Path(idx).parent / artifact_path
            try:
                if artifact.is_symlink() or not artifact.is_file():
                    raise OSError("artifact is not a regular, non-symlink file")
                actual_size = artifact.stat().st_size
            except OSError as exc:
                raise RuntimeError(
                    f"index row {row_number} artifact is missing: {artifact}"
                ) from exc
            if actual_size != expected_size:
                raise RuntimeError(
                    f"index row {row_number} artifact size mismatch for "
                    f"{artifact}: {actual_size} != {expected_size}"
                )
            # Policy files are tiny, immutable, and consulted during resume;
            # verify their content on every schema check. The final campaign
            # verifier streams and hashes every artifact, including large raw
            # request traces, without making each point append quadratic.
            if path_field == "serving_policy_path":
                actual_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
                if actual_hash != artifact_hash:
                    raise RuntimeError(
                        f"index row {row_number} artifact hash mismatch for "
                        f"{artifact}"
                    )
        if row.get("serving_policy_path"):
            policy_required = (
                "model_revision", "tokenizer", "tokenizer_revision",
                "vllm_image", "dtype", "tensor_parallel_size",
                "pipeline_parallel_size", "prefix_caching",
                "generation_config", "runtime_fingerprint",
            )
            missing_policy = [
                field for field in policy_required
                if row.get(field) in (None, "")
            ]
            if missing_policy:
                raise RuntimeError(
                    f"index row {row_number} is missing serving-policy "
                    "values: " + ", ".join(missing_policy)
                )
            _integral_field(
                row, "tensor_parallel_size", row_number, positive=True
            )
            _integral_field(
                row, "pipeline_parallel_size", row_number, positive=True
            )
        repeat_id = _integral_field(
            row, "repeat_id", row_number, positive=True
        )
        repeat_count = _integral_field(
            row, "repeat_count", row_number, positive=True
        )
        if repeat_id > repeat_count:
            raise RuntimeError(
                f"index row {row_number} has repeat_id {repeat_id} greater "
                f"than repeat_count {repeat_count}"
            )
        for field in _INDEX_COUNT_FIELDS:
            _integral_field(
                row, field, row_number,
                required=field in {
                    "requests_started", "requests_completed",
                    "requests_failed", "requests_completed_in_window",
                    "requests_started_and_completed_in_window",
                    "total_output_tokens",
                },
            )
        mode = row["mode"]
        if mode == "closed":
            load = _integral_field(
                row, "concurrency", row_number, positive=True
            )
            expected_loads = _parse_expected_concurrencies(
                row.get("expected_concurrencies"), f"index row {row_number}"
            )
            if load not in expected_loads:
                raise RuntimeError(
                    f"index row {row_number} concurrency {load} is outside "
                    f"expected_concurrencies {expected_loads}"
                )
        elif mode == "open":
            try:
                load = float(row.get("target_qps"))
            except (TypeError, ValueError):
                load = float("nan")
            if not math.isfinite(load) or load <= 0:
                raise RuntimeError(
                    f"index row {row_number} has invalid target_qps="
                    f"{row.get('target_qps')!r}"
                )
        else:
            raise RuntimeError(
                f"index row {row_number} has invalid mode={mode!r}"
            )
        run_tag = row["run_tag"]
        if run_tag in run_tags:
            raise RuntimeError(f"index contains duplicate run_tag {run_tag!r}")
        run_tags.add(run_tag)
        model = row.get("model_label") or row.get("model")
        point_id = (model, row["config_label"], mode, load, repeat_id)
        if point_id in point_ids:
            raise RuntimeError(
                "index contains duplicate model/config/load/repeat identity: "
                f"{point_id!r}"
            )
        point_ids.add(point_id)


def validate_index_schema(out_dir: str) -> None:
    """Validate the exact schema and every existing row before a costly run."""
    idx = os.path.join(out_dir, "index.csv")
    if not os.path.exists(idx) or os.path.getsize(idx) == 0:
        return
    with open(idx, "r", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            return
        if header != INDEX_FIELDS:
            raise RuntimeError(
                f"index schema mismatch in {idx}: its header does not match "
                "this build's INDEX_FIELDS. Use a fresh OUT_DIR; appending "
                "would silently misalign or mix methodologies."
            )
        rows = []
        for row_number, values in enumerate(reader, start=2):
            if len(values) != len(INDEX_FIELDS):
                raise RuntimeError(
                    f"index row {row_number} in {idx} has {len(values)} fields; "
                    f"expected {len(INDEX_FIELDS)}"
                )
            rows.append(dict(zip(INDEX_FIELDS, values)))
    _validate_index_rows(rows, idx)


def _validate_observability(summary, cfg) -> None:
    """Reject paper runs whose required monitor data is incomplete."""

    def finite_number(value) -> bool:
        return (not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(value))

    if cfg.require_power_data:
        if cfg.power_backend == "none":
            raise RuntimeError(
                "require_power_data is true but power_backend is none"
            )
        energy = summary.get("energy") or {}
        expected_nodes = 2 if cfg.config_label in {"tp2", "pp2"} else 1
        if energy.get("power_nodes") != expected_nodes:
            raise RuntimeError(
                f"required power coverage is incomplete: expected "
                f"{expected_nodes} node(s), got {energy.get('power_nodes', 0)}"
            )

        energy_per_token = energy.get("energy_per_token_j")
        if not finite_number(energy_per_token) or energy_per_token <= 0:
            raise RuntimeError(
                "required power samples do not produce positive window energy"
            )

        allowed_power_gap = max(
            5.0, 10.0 * cfg.power_interval_ms / 1000.0
        )
        gaps = [energy.get("power_max_gap_s_head")]
        counts = [energy.get("power_samples_head")]
        node_fields = ["mean_power_w_head"]
        if expected_nodes == 2:
            gaps.append(energy.get("power_max_gap_s_worker"))
            counts.append(energy.get("power_samples_worker"))
            node_fields.append("mean_power_w_worker")

        if any(not finite_number(count) or count < 2 for count in counts):
            raise RuntimeError(
                "required power sampler has fewer than two window samples"
            )
        if any(not finite_number(gap) or gap > allowed_power_gap
               for gap in gaps):
            raise RuntimeError(
                f"required power sampler gap exceeds {allowed_power_gap:g}s"
            )
        for field in node_fields:
            mean_power = energy.get(field)
            if not finite_number(mean_power) or mean_power <= 0:
                raise RuntimeError(
                    f"required power sampler reported invalid "
                    f"{field}={mean_power!r}"
                )

    if cfg.require_vllm_metrics_data:
        if not cfg.scrape_vllm_metrics:
            raise RuntimeError(
                "require_vllm_metrics_data is true but scraping is disabled"
            )
        metrics = summary.get("vllm_metrics") or {}
        allowed_metric_gap = 5.0
        min_span = max(
            0.0, summary.get("measure_s", 0.0) - allowed_metric_gap
        )

        for name, stat in VLLMMetricsScraper.REQUIRED_RELEASE_STATS.items():
            series = metrics.get(name) or {}
            # Custom/test scraper adapters predating series identities are
            # treated as one series; the built-in scraper always reports it.
            if series.get("series_count", 1) != 1:
                raise RuntimeError(
                    f"required vLLM metric {name} has "
                    f"{series.get('series_count', 0)} label series; release "
                    "aggregation requires exactly one"
                )
            value = series.get(stat)
            if not finite_number(value):
                raise RuntimeError(
                    f"required vLLM metric {name}.{stat} is missing or "
                    "non-finite for the measurement window"
                )
            count = series.get("sample_count")
            if not finite_number(count) or count < 2:
                raise RuntimeError(
                    f"required vLLM metric {name} has too few window samples"
                )
            gap = series.get("max_gap_s")
            if not finite_number(gap) or gap > allowed_metric_gap:
                raise RuntimeError(
                    f"required vLLM metric {name} scrape gap exceeds 5s"
                )
            span = series.get("sample_span_s")
            if not finite_number(span) or span < min_span:
                raise RuntimeError(
                    f"required vLLM metric {name} samples do not span the window"
                )

        generation = metrics["vllm:generation_tokens_total"]
        if (summary.get("total_output_tokens", 0) > 0
                and generation.get("delta", 0.0) <= 0):
            raise RuntimeError(
                "client received tokens but the server generation counter "
                "did not advance"
            )


def _validate_result_quality(summary, cfg) -> None:
    """Reject unusable release measurements before publishing any artifact."""
    started = summary.get("requests_started")
    completed = summary.get("requests_completed")
    failed = summary.get("requests_failed")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
           for value in (started, completed, failed)):
        raise RuntimeError("request accounting contains invalid counts")
    if started != completed + failed:
        raise RuntimeError(
            "request accounting mismatch: requests_started must equal "
            "requests_completed + requests_failed"
        )
    if not cfg.require_zero_request_failures:
        return
    if failed:
        raise RuntimeError(
            f"release point has {failed} failed request(s); no row was published"
        )
    if completed < cfg.min_successful_requests:
        raise RuntimeError(
            f"release point has {completed} successful request(s), fewer than "
            f"min_successful_requests={cfg.min_successful_requests}"
        )
    if summary.get("total_output_tokens", 0) <= 0:
        raise RuntimeError("release point received no output tokens in the window")


def _preflight_observability(power, worker_power, vmetrics, cfg) -> None:
    """Fail in seconds when a required sampler is dead or misidentified."""
    if cfg.require_power_data:
        if not power.wait_for_samples(minimum=2, timeout_s=10.0):
            raise RuntimeError(
                f"power preflight failed for backend {cfg.power_backend!r}: "
                "fewer than two live samples"
            )
        if cfg.config_label in {"tp2", "pp2"}:
            if worker_power is None:
                raise RuntimeError("dual-node power preflight has no worker sampler")
            if not worker_power.wait_for_identity(timeout_s=10.0):
                raise RuntimeError(
                    "worker power preflight did not receive a Ray node identity"
                )
            if worker_power.identity_ip != cfg.worker_power_node_ip:
                raise RuntimeError(
                    "worker power sampler ran on the wrong Ray node: expected "
                    f"{cfg.worker_power_node_ip}, got {worker_power.identity_ip}"
                )
            if (cfg.head_node_ip
                    and worker_power.identity_ip == cfg.head_node_ip):
                raise RuntimeError(
                    "worker power sampler resolved to the head node; refusing "
                    "to double-count head power"
                )
            if not worker_power.wait_for_samples(minimum=2, timeout_s=10.0):
                raise RuntimeError(
                    "worker power preflight returned fewer than two live samples"
                )
    if cfg.require_vllm_metrics_data:
        if vmetrics is None or not vmetrics.wait_for_release_metrics(10.0):
            raise RuntimeError(
                "vLLM metrics preflight did not expose every required metric"
            )


def _validate_monitor_config(cfg) -> None:
    """Reject impossible required-monitor policies before starting a phase."""
    if cfg.require_power_data and cfg.power_backend == "none":
        raise RuntimeError("require_power_data is true but power_backend is none")
    if cfg.require_vllm_metrics_data and not cfg.scrape_vllm_metrics:
        raise RuntimeError(
            "require_vllm_metrics_data is true but scraping is disabled"
        )
    if cfg.config_label in {"tp2", "pp2"} and cfg.require_power_data:
        if not cfg.head_node_ip:
            raise RuntimeError("dual-node release power requires head_node_ip")
        if cfg.worker_power_node_ip == cfg.head_node_ip:
            raise RuntimeError(
                "worker_power_node_ip must be distinct from head_node_ip"
            )


def _validate_provenance(cfg) -> None:
    """Require immutable model/server identity for release measurements."""
    if not cfg.require_provenance:
        return
    commit = re.compile(r"[0-9a-fA-F]{40}")
    digest = re.compile(r"[0-9a-fA-F]{64}")
    problems = []
    if not commit.fullmatch(cfg.model_revision or ""):
        problems.append("model_revision must be an exact 40-hex commit")
    if cfg.use_token_ids:
        if not cfg.tokenizer:
            problems.append("exact token prompts require a tokenizer")
        if not commit.fullmatch(cfg.tokenizer_revision or ""):
            problems.append("tokenizer_revision must be an exact 40-hex commit")
    if not digest.fullmatch(cfg.serving_fingerprint or ""):
        problems.append("serving_fingerprint must be a 64-hex policy hash")
    if (cfg.deployment_fingerprint
            and not digest.fullmatch(cfg.deployment_fingerprint)):
        problems.append("deployment_fingerprint must be a 64-hex live-state hash")
    if problems:
        raise RuntimeError("required provenance is incomplete: " + "; ".join(problems))


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
    kwargs = {"trust_remote_code": cfg.trust_remote_code}
    if cfg.tokenizer_revision:
        kwargs["revision"] = cfg.tokenizer_revision
    return AutoTokenizer.from_pretrained(cfg.tokenizer, **kwargs)


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
    slug = re.sub(r"[^a-z0-9._-]", "-", base)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("._-") or "model"


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


def _atomic_write(path: str, data: bytes) -> None:
    """Durably publish one artifact with write/fsync/replace semantics."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _artifact_metadata(name: str, data: bytes) -> dict:
    return {
        "path": name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }


def _load_serving_policy(path: str | None, cfg) -> tuple[dict | None, dict | None]:
    """Load and cross-check the immutable launch policy before measurement."""
    if not path:
        if cfg.require_provenance:
            raise RuntimeError(
                "release provenance requires --serving-policy-file"
            )
        return None, None

    supplied = Path(path)
    source = supplied.resolve()
    output = Path(cfg.out_dir).resolve()
    if source.parent != output:
        raise RuntimeError(
            "serving policy must be a direct child of the benchmark OUT_DIR"
        )
    if supplied.is_symlink() or source.is_symlink() or not source.is_file():
        raise RuntimeError(
            f"serving policy is not a regular, non-symlink file: {source}"
        )
    data = source.read_bytes()
    if not data:
        raise RuntimeError(f"serving policy is empty: {source}")

    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        policy = json.loads(data, object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"invalid serving policy {source}: {exc}") from exc
    if not isinstance(policy, dict):
        raise RuntimeError(f"serving policy {source} must contain an object")

    legacy_fields = (
        "schema", "config", "image", "model", "revision", "tokenizer",
        "tokenizer_revision", "dtype", "quant", "max_model_len",
        "gpu_util", "prefix_caching", "generation_config",
        "torchinductor_autograd_cache", "hf_hub_offline",
        "trust_remote_code", "ray_version", "connectx_iface",
        "nccl_ib_disable", "head_ip", "worker_ip", "power_backend",
        "runtime_facts", "runtime_facts_sha256", "serving_fingerprint",
    )
    missing = [field for field in legacy_fields if field not in policy]
    if missing:
        raise RuntimeError(
            f"serving policy {source} is missing: " + ", ".join(missing)
        )
    if policy["schema"] != "serving-v1":
        raise RuntimeError(
            f"unsupported serving policy schema {policy['schema']!r}"
        )
    fingerprint = str(policy["serving_fingerprint"])
    runtime_hash = str(policy["runtime_facts_sha256"])
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise RuntimeError("serving policy fingerprint is not a SHA-256 digest")
    if not re.fullmatch(r"[0-9a-f]{64}", runtime_hash):
        raise RuntimeError("serving runtime fingerprint is not a SHA-256 digest")
    if not re.fullmatch(r".+@sha256:[0-9a-fA-F]{64}", str(policy["image"])):
        raise RuntimeError("serving policy image is not pinned by digest")

    runtime_canonical = json.dumps(
        policy["runtime_facts"], sort_keys=True, separators=(",", ":")
    ).encode()
    if hashlib.sha256(runtime_canonical).hexdigest() != runtime_hash:
        raise RuntimeError("serving policy runtime facts do not match their hash")
    if str(policy.get("cluster_runtime")) != runtime_hash:
        raise RuntimeError("serving policy cluster_runtime hash is inconsistent")

    legacy_input = "".join((
        f"schema={policy['schema']}\n",
        f"config={policy['config']}\n",
        f"image={policy['image']}\n",
        f"model={policy['model']}\nrevision={policy['revision']}\n",
        f"tokenizer={policy['tokenizer']}\n"
        f"tokenizer_revision={policy['tokenizer_revision']}\n",
        f"dtype={policy['dtype']}\nquant={policy['quant']}\n",
        f"max_model_len={policy['max_model_len']}\n"
        f"gpu_util={policy['gpu_util']}\n",
        f"prefix_caching={policy['prefix_caching']}\n"
        f"generation_config={policy['generation_config']}\n",
        "torchinductor_autograd_cache="
        f"{policy['torchinductor_autograd_cache']}\n"
        f"hf_hub_offline={policy['hf_hub_offline']}\n",
        f"trust_remote_code={policy['trust_remote_code']}\n"
        f"ray_version={policy['ray_version']}\n",
        f"connectx_iface={policy['connectx_iface']}\n"
        f"nccl_ib_disable={policy['nccl_ib_disable']}\n",
        f"head_ip={policy['head_ip']}\nworker_ip={policy['worker_ip']}\n"
        f"power_backend={policy['power_backend']}\n",
        f"cluster_runtime={runtime_hash}\n",
    ))
    if hashlib.sha256(legacy_input.encode()).hexdigest() != fingerprint:
        raise RuntimeError(
            "serving policy fields do not reproduce serving_fingerprint"
        )

    expected = {
        "config": cfg.config_label,
        "model": cfg.model,
        "revision": cfg.model_revision,
        "tokenizer": cfg.tokenizer,
        "tokenizer_revision": cfg.tokenizer_revision,
        "serving_fingerprint": cfg.serving_fingerprint,
    }
    mismatches = [
        f"{field}: {policy.get(field)!r} != {value!r}"
        for field, value in expected.items()
        if str(policy.get(field, "")) != str(value or "")
    ]
    policy_trust = str(policy["trust_remote_code"]).strip().lower()
    policy_trust_bool = policy_trust in {"1", "true", "yes", "on"}
    if policy_trust_bool != bool(cfg.trust_remote_code):
        mismatches.append(
            "trust_remote_code: "
            f"{policy['trust_remote_code']!r} != {cfg.trust_remote_code!r}"
        )
    if mismatches:
        raise RuntimeError(
            "serving policy does not match benchmark arguments: "
            + "; ".join(mismatches)
        )
    return policy, _artifact_metadata(source.name, data)


def _commit_index_row(out_dir: str, idx: str, row: dict) -> None:
    """Validate and atomically append one row while holding a writer lock.

    The lock covers the read/validate/replace transaction, so two benchmark
    processes cannot both read the same index and silently lose one append.
    """
    lock_path = os.path.join(out_dir, ".index.lock")
    with open(lock_path, "a+") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            existing = []
            if os.path.exists(idx) and os.path.getsize(idx) > 0:
                validate_index_schema(out_dir)
                with open(idx, "r", newline="") as handle:
                    existing = list(csv.DictReader(handle))
            candidate = existing + [row]
            _validate_index_rows(candidate, idx)

            # Rewrite in the same directory and atomically replace the index. A
            # killed process can leave an unreferenced tmp file, never a partial CSV.
            fd, tmp = tempfile.mkstemp(
                prefix=".index.", suffix=".tmp", dir=out_dir, text=True
            )
            try:
                with os.fdopen(fd, "w", newline="") as handle:
                    writer = csv.DictWriter(
                        handle, fieldnames=INDEX_FIELDS, extrasaction="ignore"
                    )
                    writer.writeheader()
                    writer.writerows(candidate)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, idx)
                directory_fd = os.open(out_dir, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _append_index(out_dir, s):
    idx = os.path.join(out_dir, "index.csv")
    vllm = s.get("vllm_metrics", {})
    energy = s.get("energy", {})
    info = s.get("server_info", {})
    artifacts = s.get("artifacts", {})
    policy = s.get("serving_policy") or {}
    config = s.get("config") or {}
    parallel_sizes = {
        "single": (1, 1),
        "tp2": (2, 1),
        "pp2": (1, 2),
    }
    tensor_parallel_size, pipeline_parallel_size = parallel_sizes.get(
        s.get("config_label"), (None, None)
    )

    def vmetric(name, stat):
        return vllm.get(name, {}).get(stat)

    def vcounter(name, stat="delta"):
        return vllm.get(name, {}).get(stat)

    def artifact(kind, field):
        return (artifacts.get(kind) or {}).get(field)

    gen_delta = vcounter("vllm:generation_tokens_total")
    measure_s = s["measure_s"]
    row = {
        "run_tag": s["run_tag"],
        "raw_path": artifact("raw", "path"),
        "raw_sha256": artifact("raw", "sha256"),
        "raw_bytes": artifact("raw", "bytes"),
        "summary_path": artifact("summary", "path"),
        "summary_sha256": artifact("summary", "sha256"),
        "summary_bytes": artifact("summary", "bytes"),
        "power_samples_path": artifact("power_samples", "path"),
        "power_samples_sha256": artifact("power_samples", "sha256"),
        "power_samples_bytes": artifact("power_samples", "bytes"),
        "vllm_samples_path": artifact("vllm_samples", "path"),
        "vllm_samples_sha256": artifact("vllm_samples", "sha256"),
        "vllm_samples_bytes": artifact("vllm_samples", "bytes"),
        "serving_policy_path": artifact("serving_policy", "path"),
        "serving_policy_sha256": artifact("serving_policy", "sha256"),
        "serving_policy_bytes": artifact("serving_policy", "bytes"),
        "methodology_version": s.get("methodology_version"),
        "config_file_sha256": s.get("config_file_sha256"),
        "trace_sha256": s.get("trace_sha256"),
        "experiment_fingerprint": s.get("experiment_fingerprint"),
        "harness_source_sha256": s.get("harness_source_sha256"),
        "client_environment_sha256": s.get("client_environment_sha256"),
        "harness_git_commit": s.get("harness_git_commit"),
        "serving_fingerprint": s.get("serving_fingerprint"),
        "deployment_fingerprint": s.get("deployment_fingerprint"),
        "model_revision": policy.get("revision", config.get("model_revision")),
        "tokenizer": policy.get("tokenizer", config.get("tokenizer")),
        "tokenizer_revision": policy.get(
            "tokenizer_revision", config.get("tokenizer_revision")
        ),
        "vllm_image": policy.get("image"),
        "dtype": policy.get("dtype"),
        "quantization": policy.get("quant"),
        "tensor_parallel_size": tensor_parallel_size,
        "pipeline_parallel_size": pipeline_parallel_size,
        "prefix_caching": policy.get("prefix_caching"),
        "generation_config": policy.get("generation_config"),
        "runtime_fingerprint": policy.get(
            "runtime_facts_sha256", policy.get("cluster_runtime")
        ),
        "repeat_id": s.get("repeat_id"),
        "repeat_count": s.get("repeat_count", s.get("repeat_id", 1)),
        "config_label": s["config_label"],
        "model_label": s.get("model_label", ""),
        "model": s.get("model", ""),
        "mode": s["mode"],
        "concurrency": s["concurrency"],
        "expected_concurrencies": (
            s.get("expected_concurrencies") or str(s["concurrency"])
        ),
        "target_qps": s["target_qps"],
        "measure_s": measure_s,
        "configured_measure_s": s.get("configured_measure_s"),
        "measure_extension_s": s.get("measure_extension_s"),
        "warmup_s": s.get("warmup_s"),
        "configured_warmup_s": s.get("configured_warmup_s"),
        "warmup_extension_s": s.get("warmup_extension_s"),
        "cooldown_s": s.get("cooldown_s"),
        "load_extension_s": s.get("load_extension_s"),
        "drain_s": s.get("drain_s"),
        "requests_started": s["requests_started"],
        "requests_completed": s["requests_completed"],
        "requests_failed": s["requests_failed"],
        "requests_completed_in_window": s.get("requests_completed_in_window"),
        "requests_started_and_completed_in_window":
            s.get("requests_started_and_completed_in_window"),
        "avg_input_tokens": s.get("avg_input_tokens"),
        "avg_requested_output_tokens": s.get("avg_requested_output_tokens"),
        "total_output_tokens": s["total_output_tokens"],
        "req_thru_rps": s["request_throughput_rps"],
        "tok_thru_tps": s["output_token_throughput_tps"],
        "tok_thru_completed_tps": s.get("completed_token_throughput_tps"),
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
        "server_gen_tps": vcounter("vllm:generation_tokens_total",
                                    "rate_per_s"),
        "server_prompt_tps": vcounter("vllm:prompt_tokens_total",
                                       "rate_per_s"),
        "server_metric_samples": vcounter("vllm:generation_tokens_total",
                                           "sample_count"),
        "server_metric_sample_span_s": vcounter(
            "vllm:generation_tokens_total", "sample_span_s"),
        "server_metric_max_gap_s": vcounter("vllm:generation_tokens_total",
                                             "max_gap_s"),
        "kv_metric_samples": vmetric("vllm:kv_cache_usage_perc", "sample_count"),
        "kv_metric_sample_span_s": vmetric(
            "vllm:kv_cache_usage_perc", "sample_span_s"),
        "kv_metric_max_gap_s": vmetric("vllm:kv_cache_usage_perc", "max_gap_s"),
        "num_preemptions": vcounter("vllm:num_preemptions_total"),
        "max_model_len": info.get("max_model_len"),
        "kv_cache_dtype": info.get("kv_cache_dtype"),
        "gpu_memory_utilization": info.get("gpu_memory_utilization"),
        "block_size": info.get("block_size"),
        "gpu_blocks": info.get("gpu_blocks"),
        "total_kv_tokens": info.get("total_kv_tokens"),
        "total_kv_tokens_source": info.get("total_kv_tokens_source"),
        "hybrid_cache": info.get("hybrid_cache"),
        "weight_gb": info.get("weight_gb"),
        "kv_cache_gb": info.get("kv_cache_gb"),
        "worker_weight_gb": info.get("worker_weight_gb"),
        "worker_kv_cache_gb": info.get("worker_kv_cache_gb"),
        "max_concurrency": info.get("max_concurrency"),
        "vllm_version": info.get("vllm_version"),
        "mean_power_w": energy.get("mean_power_w"),
        "mean_power_w_head": energy.get("mean_power_w_head"),
        "mean_power_w_worker": energy.get("mean_power_w_worker"),
        "power_nodes": energy.get("power_nodes"),
        "power_scope": energy.get("power_scope"),
        "power_samples_head": energy.get("power_samples_head"),
        "power_samples_worker": energy.get("power_samples_worker"),
        "power_max_gap_s_head": energy.get("power_max_gap_s_head"),
        "power_max_gap_s_worker": energy.get("power_max_gap_s_worker"),
        "energy_per_token_j": energy.get("energy_per_token_j"),
        "tokens_per_watt": energy.get("tokens_per_watt"),
    }
    _commit_index_row(out_dir, idx, row)


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
    ap.add_argument("--model-revision", help="pinned Hugging Face model commit")
    ap.add_argument("--tokenizer", help="HF tokenizer id or path; empty string disables")
    ap.add_argument("--tokenizer-revision", help="pinned tokenizer commit")
    ap.add_argument("--serving-fingerprint",
                    help="hash of the server image and launch policy")
    ap.add_argument(
        "--serving-policy-file",
        help="immutable canonical launch-policy JSON (required for release runs)",
    )
    ap.add_argument("--deployment-fingerprint",
                    help="expected hash of the live server runtime/capacity")
    ap.add_argument("--repeat-id", type=int,
                    help="one-based repeated measurement-window number")
    ap.add_argument("--repeat-count", type=int,
                    help="intended total repeated windows for this campaign")
    ap.add_argument("--trust-remote-code", action="store_true", default=None,
                    help="pass trust_remote_code=True to the tokenizer and HF "
                    "config loads (models that ship custom code, e.g. Nemotron)")
    ap.add_argument("--mode", choices=["closed", "open"])
    ap.add_argument("--concurrency", type=int)
    ap.add_argument("--expected-concurrencies",
                    help="space-separated intended closed-loop load rectangle")
    ap.add_argument("--qps", type=float)
    ap.add_argument("--measure-s", type=float)
    ap.add_argument("--warmup-s", type=float)
    ap.add_argument("--cooldown-s", type=float)
    ap.add_argument("--base-url")
    ap.add_argument("--out-dir")
    ap.add_argument("--power-backend",
                    choices=["none", "nvidia_smi", "sysfs", "tegrastats"])
    ap.add_argument("--worker-power-container",
                    help="docker container holding the Ray cluster; with "
                    "--worker-power-node-ip, samples the worker node's GPU "
                    "power so dual-node energy covers both nodes")
    ap.add_argument("--worker-power-node-ip",
                    help="Ray node IP of the worker to sample (e.g. 10.10.10.2)")
    ap.add_argument("--head-node-ip",
                    help="Ray/head node IP; required with release-grade dual "
                    "power sampling to prove the worker is distinct")
    args = ap.parse_args()

    cfg = ExperimentConfig.from_yaml(args.config) if args.config else ExperimentConfig()
    if args.config_label:
        cfg.config_label = args.config_label
    if args.model is not None:
        cfg.model = args.model
    if args.model_revision is not None:
        cfg.model_revision = args.model_revision
    if args.tokenizer is not None:
        cfg.tokenizer = args.tokenizer
    if args.tokenizer_revision is not None:
        cfg.tokenizer_revision = args.tokenizer_revision
    if args.serving_fingerprint is not None:
        cfg.serving_fingerprint = args.serving_fingerprint
    if args.deployment_fingerprint is not None:
        cfg.deployment_fingerprint = args.deployment_fingerprint
    if args.repeat_id is not None:
        cfg.repeat_id = args.repeat_id
    if args.repeat_count is not None:
        cfg.repeat_count = args.repeat_count
    if args.trust_remote_code:
        cfg.trust_remote_code = True
    if args.mode:
        cfg.mode = args.mode
    if args.concurrency is not None:
        cfg.concurrency = args.concurrency
    if args.expected_concurrencies is not None:
        cfg.expected_concurrencies = args.expected_concurrencies
    if args.qps is not None:
        cfg.qps = args.qps
    if args.measure_s is not None:
        cfg.measure_s = args.measure_s
    if args.warmup_s is not None:
        cfg.warmup_s = args.warmup_s
    if args.cooldown_s is not None:
        cfg.cooldown_s = args.cooldown_s
    if args.base_url:
        cfg.base_url = args.base_url
    if args.out_dir:
        cfg.out_dir = args.out_dir
    if args.power_backend:
        cfg.power_backend = args.power_backend
    if args.worker_power_container:
        cfg.worker_power_container = args.worker_power_container
    if args.worker_power_node_ip:
        cfg.worker_power_node_ip = args.worker_power_node_ip
    if args.head_node_ip:
        cfg.head_node_ip = args.head_node_ip
    if args.model_label is not None:
        cfg.model_label = args.model_label
    if args.server_log is not None:
        cfg.server_log = args.server_log
    if args.collect_hf_metadata is not None:
        cfg.collect_hf_metadata = args.collect_hf_metadata

    if cfg.mode == "closed":
        expected_loads = _parse_expected_concurrencies(
            cfg.expected_concurrencies or str(cfg.concurrency),
            "benchmark config",
        )
        if cfg.concurrency not in expected_loads:
            raise SystemExit(
                f"--concurrency {cfg.concurrency} is not present in "
                "--expected-concurrencies"
            )
        cfg.expected_concurrencies = " ".join(map(str, expected_loads))
    elif cfg.expected_concurrencies:
        raise SystemExit("--expected-concurrencies is only valid in closed mode")
    if cfg.repeat_id < 1:
        raise SystemExit("--repeat-id must be at least 1")
    if cfg.repeat_count < 1:
        raise SystemExit("--repeat-count must be at least 1")
    if cfg.repeat_id > cfg.repeat_count:
        raise SystemExit("--repeat-id cannot exceed --repeat-count")
    cfg.validate()
    _validate_monitor_config(cfg)
    if (cfg.require_power_data and cfg.config_label in {"tp2", "pp2"}
            and not (cfg.worker_power_container and cfg.worker_power_node_ip)):
        raise SystemExit(
            "dual-node require_power_data needs both worker power arguments"
        )
    os.makedirs(cfg.out_dir, exist_ok=True)
    validate_index_schema(cfg.out_dir)
    config_file_sha256 = config_file_fingerprint(
        args.config,
        {
            "measure_s": args.measure_s,
            "warmup_s": args.warmup_s,
            "cooldown_s": args.cooldown_s,
            "collect_hf_metadata": args.collect_hf_metadata,
            "power_backend": args.power_backend,
        },
    )
    harness_source_sha256 = _source_fingerprint()
    harness_git_commit = _git_commit()

    cfg.model = _resolve_model(cfg)
    if (not cfg.tokenizer_revision and cfg.model_revision
            and cfg.tokenizer == cfg.model):
        cfg.tokenizer_revision = cfg.model_revision
    _validate_provenance(cfg)
    serving_policy, serving_policy_artifact = _load_serving_policy(
        args.serving_policy_file, cfg
    )
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
    # Explicit labels were checked before any output-directory work. Validate
    # the derived model slug too, immediately before it can enter a run tag.
    cfg.validate()
    captured_client_environment, client_environment_sha256 = (
        _capture_client_environment()
    )
    # Before the first transformers/huggingface_hub import reads the HF env.
    if cfg.tokenizer or cfg.collect_hf_metadata:
        _ensure_writable_hf_cache()
    workload = Workload(cfg, _load_tokenizer(cfg))

    point = cfg.concurrency if cfg.mode == "closed" else cfg.qps
    prefix = f"{cfg.model_label}_" if explicit_label else ""
    now_ns = time.time_ns()
    timestamp = (
        time.strftime("%Y%m%d-%H%M%S", time.localtime(now_ns // 1_000_000_000))
        + f"-{now_ns % 1_000_000_000:09d}"
    )
    tag = (f"{prefix}{cfg.config_label}_{cfg.mode}_{point}_r{cfg.repeat_id}_"
           f"{timestamp}")
    print(f"[bench] model={cfg.model} config={cfg.config_label} "
          f"mode={cfg.mode} point={point}")

    power = PowerSampler(cfg.power_backend, cfg.power_interval_ms,
                         cfg.power_sysfs_path, cfg.power_rails)
    wpower = None
    if (cfg.power_backend != "none" and cfg.worker_power_container
            and cfg.worker_power_node_ip):
        wpower = WorkerPowerSampler(cfg.worker_power_container,
                                    cfg.worker_power_node_ip)
    vmet = VLLMMetricsScraper(cfg.base_url) if cfg.scrape_vllm_metrics else None
    # One continuous phase. Closed-loop warmup is automatically extended until
    # every worker's initial request has completed. Cooldown is a minimum: load
    # remains at its configured level until every measurement request finishes,
    # then the non-measurement replacements drain.
    minimum_s = cfg.warmup_s + cfg.measure_s + cfg.cooldown_s
    print(f"[bench] continuous phase: minimum warmup {cfg.warmup_s:g}s + "
          f"measure {cfg.measure_s:g}s + minimum cooldown {cfg.cooldown_s:g}s "
          f"= {minimum_s:g}s, with automatic steady-state extensions")
    if cfg.min_window_requests:
        print(f"[bench] window extends in {cfg.window_extension_step_s:g}s steps "
              f"until {cfg.min_window_requests} requests have started in it "
              f"(cap {cfg.max_measure_s:g}s)")
    try:
        power.start()
        if wpower:
            wpower.start()
        if vmet:
            vmet.start()
            # Seed a baseline before window_start; the background thread interval
            # is longer than some smoke-test windows.
            vmet.scrape_once()
        _preflight_observability(power, wpower, vmet, cfg)
        wall_anchor = time.time()
        perf_anchor = time.perf_counter()
        records, phase = asyncio.run(run_phase(cfg, workload))
    finally:
        # Sampler subprocesses must not survive a failed/aborted phase and
        # contaminate the next point.
        power.stop()
        if wpower:
            wpower.stop()
        if vmet:
            vmet.stop()
    window_start = phase["window_start"]
    window_end = phase["window_end"]
    actual_warmup_s = window_start - phase["phase_start"]
    warmup_extension_s = max(0.0, actual_warmup_s - cfg.warmup_s)
    load_extension_s = max(0.0, phase["load_stop"] - phase["nominal_stop"])
    drain_s = max(0.0, phase["actual_end"] - phase["load_stop"])
    window_start_unix = wall_anchor + (window_start - perf_anchor)

    if wpower:
        if wpower.sample_count() == 0:
            print("[bench] WARNING: worker power sampler returned no samples; "
                  "this row's energy covers the head node only "
                  "(power_nodes=1). Check that Ray is up in "
                  f"{cfg.worker_power_container} and node "
                  f"{cfg.worker_power_node_ip} is alive.")
    # Re-collect the serving identity after the window. Required capacity fields
    # are retried and must still match the launch-time deployment fingerprint.
    info = collect_deployment_info(
        cfg.base_url, cfg.model, cfg.server_log,
        compute_from_hf=cfg.collect_hf_metadata,
        trust_remote_code=cfg.trust_remote_code,
    )
    actual_deployment_fingerprint = deployment_fingerprint(
        cfg.serving_fingerprint, info
    )
    if (cfg.deployment_fingerprint
            and cfg.deployment_fingerprint != actual_deployment_fingerprint):
        raise RuntimeError(
            "live server state changed after resume scheduling: expected "
            f"deployment {cfg.deployment_fingerprint}, observed "
            f"{actual_deployment_fingerprint}"
        )

    summary = aggregate(records, cfg, window_start, window_end,
                        power if cfg.power_backend != "none" else None, vmet,
                        worker_power=wpower)
    _validate_observability(summary, cfg)
    _validate_result_quality(summary, cfg)
    summary["run_tag"] = tag
    summary["methodology_version"] = METHODOLOGY_VERSION
    summary["config_file_sha256"] = config_file_sha256
    summary["trace_sha256"] = cfg.trace_sha256
    summary["harness_source_sha256"] = harness_source_sha256
    summary["client_environment_sha256"] = client_environment_sha256
    summary["client_environment"] = captured_client_environment
    summary["harness_git_commit"] = harness_git_commit
    summary["serving_fingerprint"] = cfg.serving_fingerprint
    if serving_policy is not None:
        summary["serving_policy"] = serving_policy
    summary["deployment_fingerprint"] = actual_deployment_fingerprint
    summary["repeat_id"] = cfg.repeat_id
    summary["repeat_count"] = cfg.repeat_count
    summary["expected_concurrencies"] = cfg.expected_concurrencies
    summary["model"] = cfg.model
    summary["model_label"] = cfg.model_label
    summary["warmup_s"] = actual_warmup_s
    summary["configured_warmup_s"] = cfg.warmup_s
    summary["warmup_extension_s"] = warmup_extension_s
    # aggregate() already set measure_s to the actual window length.
    summary["configured_measure_s"] = cfg.measure_s
    summary["measure_extension_s"] = max(0.0, summary["measure_s"] - cfg.measure_s)
    summary["window_extensions"] = phase.get("window_extensions", 0)
    summary["min_window_requests"] = cfg.min_window_requests
    summary["cooldown_s"] = cfg.cooldown_s
    summary["load_extension_s"] = load_extension_s
    # Wall-clock anchor for the perf_counter window bounds, so raw.jsonl can be
    # re-aggregated post hoc and cross-referenced with server logs.
    summary["window_start_unix"] = window_start_unix
    summary["server_info"] = info
    if wpower:
        summary["worker_power_identity"] = {
            "requested_ip": cfg.worker_power_node_ip,
            "observed_ip": wpower.identity_ip,
            "ray_node_id": wpower.identity_node_id,
        }
    summary["config"] = cfg.to_dict()
    summary["drain_s"] = drain_s
    summary["experiment_fingerprint"] = _experiment_fingerprint(
        summary["config"], harness_source_sha256
    )
    # Build every artifact in memory, then fsync+rename it before the index row
    # can make the point resumable.
    raw_name = f"{tag}.raw.jsonl"
    summary_name = f"{tag}.summary.json"
    raw_data = "".join(
        json.dumps(_json_safe(record.to_json()), sort_keys=True) + "\n"
        for record in records
    ).encode()
    artifacts = {"raw": _artifact_metadata(raw_name, raw_data)}
    if serving_policy_artifact is not None:
        artifacts["serving_policy"] = serving_policy_artifact

    power_data = b""
    if cfg.power_backend != "none":
        power_name = f"{tag}.power.jsonl"
        power_rows = [
            {"source": "head", "t_perf": t, "watts": watts}
            for t, watts in power.samples()
        ]
        if wpower:
            power_rows.extend(
                {"source": "worker", "t_perf": t, "watts": watts,
                 "node_ip": wpower.identity_ip,
                 "ray_node_id": wpower.identity_node_id}
                for t, watts in wpower.samples()
            )
        power_rows.sort(key=lambda row: (row["t_perf"], row["source"]))
        power_data = "".join(
            json.dumps(_json_safe(row), sort_keys=True) + "\n"
            for row in power_rows
        ).encode()
        artifacts["power_samples"] = _artifact_metadata(power_name, power_data)

    vllm_data = b""
    if vmet is not None:
        vllm_name = f"{tag}.vllm_metrics.jsonl"
        metric_rows = []
        for metric, payload in vmet.raw_samples().items():
            for t, value in payload["samples"]:
                metric_rows.append({
                    "metric": metric,
                    "labels": payload["labels"],
                    "t_perf": t,
                    "value": value,
                })
        metric_rows.sort(key=lambda row: (row["t_perf"], row["metric"]))
        vllm_data = "".join(
            json.dumps(_json_safe(row), sort_keys=True) + "\n"
            for row in metric_rows
        ).encode()
        artifacts["vllm_samples"] = _artifact_metadata(vllm_name, vllm_data)

    summary["artifacts"] = artifacts
    safe_summary = _json_safe(summary)
    summary_data = (
        json.dumps(safe_summary, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode()
    summary_artifact = _artifact_metadata(summary_name, summary_data)

    _atomic_write(os.path.join(cfg.out_dir, raw_name), raw_data)
    if "power_samples" in artifacts:
        _atomic_write(
            os.path.join(cfg.out_dir, artifacts["power_samples"]["path"]),
            power_data,
        )
    if "vllm_samples" in artifacts:
        _atomic_write(
            os.path.join(cfg.out_dir, artifacts["vllm_samples"]["path"]),
            vllm_data,
        )
    _atomic_write(os.path.join(cfg.out_dir, summary_name), summary_data)

    # The summary cannot contain its own digest without becoming circular; add
    # that final metadata only to the in-memory index representation.
    safe_summary["artifacts"]["summary"] = summary_artifact
    _append_index(cfg.out_dir, safe_summary)

    print(f"[bench] tok_thru={summary['output_token_throughput_tps']:.1f} tok/s  "
          f"ttft_p50={summary['ttft_s']['p50'] * 1000:.0f}ms  "
          f"itl_p50={summary['itl_s']['p50'] * 1000:.1f}ms  "
          f"goodput={summary['goodput_rps']:.2f} rps")


if __name__ == "__main__":
    main()
