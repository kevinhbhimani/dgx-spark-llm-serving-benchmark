"""Index schema, row validation, and durable benchmark artifact writes."""
from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Sequence


INDEX_FIELDS: list[str] = [
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


def parse_expected_concurrencies(value: object, context: str) -> tuple[int, ...]:
    """Parse the declared positive closed-loop load set."""
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


def integral_field(row: Mapping[str, object], field: str, row_number: int, *,
                    positive: bool = False, required: bool = True) -> int | None:
    """Read an exact integer from an index row."""
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


def validate_index_rows(rows: Sequence[Mapping[str, object]], idx: str) -> None:
    """Reject malformed or conflicting index rows and artifact metadata."""
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
            expected_size = integral_field(row, bytes_field, row_number)
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
            integral_field(
                row, "tensor_parallel_size", row_number, positive=True
            )
            integral_field(
                row, "pipeline_parallel_size", row_number, positive=True
            )
        repeat_id = integral_field(
            row, "repeat_id", row_number, positive=True
        )
        repeat_count = integral_field(
            row, "repeat_count", row_number, positive=True
        )
        if repeat_id > repeat_count:
            raise RuntimeError(
                f"index row {row_number} has repeat_id {repeat_id} greater "
                f"than repeat_count {repeat_count}"
            )
        for field in _INDEX_COUNT_FIELDS:
            integral_field(
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
            load = integral_field(
                row, "concurrency", row_number, positive=True
            )
            expected_loads = parse_expected_concurrencies(
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
    validate_index_rows(rows, idx)


def atomic_write(path: str, data: bytes) -> None:
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


def artifact_metadata(name: str, data: bytes) -> dict[str, str | int]:
    """Describe one artifact with its basename, digest, and byte count."""
    return {
        "path": name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }


def commit_index_row(out_dir: str, idx: str, row: dict[str, object]) -> None:
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
            validate_index_rows(candidate, idx)

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


def append_index(out_dir: str, s: Mapping[str, Any]) -> None:
    """Build and atomically append the index row for one summary."""
    # A summary contains heterogeneous nested JSON objects.
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

    def vmetric(name: str, stat: str) -> object:
        return vllm.get(name, {}).get(stat)

    def vcounter(name: str, stat: str = "delta") -> object:
        return vllm.get(name, {}).get(stat)

    def artifact(kind: str, field: str) -> object:
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
    commit_index_row(out_dir, idx, row)


