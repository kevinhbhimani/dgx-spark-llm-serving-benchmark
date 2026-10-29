#!/usr/bin/env python3
"""Plan and verify a complete, publication-ready benchmark campaign.

``plan`` validates the campaign YAML files and prints the exact model/config
matrix and expected row count. ``verify`` validates ``index.csv`` against the
shared artifact contract, checks the complete result grid and its artifacts,
and can seal the campaign with an atomic SHA-256 manifest.

The module is intentionally importable: :func:`plan_campaign` and
:func:`verify_campaign` provide the same behavior as the CLI.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from typing import Iterable, Mapping, Sequence

import yaml


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.campaign_output import (
    resolve_campaign_dir,
    resolve_campaign_logs_dir,
    resolve_campaign_results_dir,
)
from bench.config import ExperimentConfig
from scripts.campaign.manifest import build_manifest, sha256_file, write_json_atomic
from scripts.campaign.plan import (
    COMMIT_RE,
    Campaign,
    CampaignValidationError,
    ExpectedPoint,
    ModelSpec,
    finite_float,
    nonnegative_float,
    nonnegative_integer,
    positive_float,
    positive_integer,
    build_expected_grid,
    load_campaign,
    parse_concurrencies,
    plan_campaign,
    validate_experiment_yaml,
    validate_models_yaml,
)
from scripts.contracts.dependency_lock import (
    LockFileError,
    normalize_distribution_name,
    parse_lock,
)


DIGEST_RE = re.compile(r"[0-9a-fA-F]{64}")
SAFE_RUN_TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

PROVENANCE_DIGEST_FIELDS = (
    "config_file_sha256",
    "experiment_fingerprint",
    "harness_source_sha256",
    "client_environment_sha256",
    "serving_fingerprint",
    "deployment_fingerprint",
)

ALLREDUCE_FIELDS = (
    "bytes",
    "avg_s",
    "min_s",
    "algbw_GBps",
    "busbw_GBps",
)
ALLREDUCE_SIZES = tuple(1 << exponent for exponent in range(3, 31))


def _format_points(points: Iterable[tuple[str, str, int, int]]) -> str:
    ordered = sorted(points)
    preview = ", ".join(
        f"{model}/{config}/c{concurrency}/r{repeat}"
        for model, config, concurrency, repeat in ordered[:12]
    )
    if len(ordered) > 12:
        preview += f", ... ({len(ordered)} total)"
    return preview


def _row_identity(row: Mapping[str, str], row_number: int) -> tuple[str, str, int, int]:
    if row.get("mode") != "closed":
        raise CampaignValidationError(
            f"index row {row_number} has mode {row.get('mode')!r}; "
            "campaign verification requires closed-mode rows"
        )
    return (
        str(row.get("model_label") or ""),
        str(row.get("config_label") or ""),
        positive_integer(row.get("concurrency"), f"index row {row_number} concurrency"),
        positive_integer(row.get("repeat_id"), f"index row {row_number} repeat_id"),
    )


def _row_concurrencies(row: Mapping[str, str], row_number: int) -> tuple[int, ...]:
    try:
        return parse_concurrencies(str(row.get("expected_concurrencies") or ""))
    except CampaignValidationError as exc:
        raise CampaignValidationError(
            f"index row {row_number} has invalid expected_concurrencies: {exc}"
        ) from exc


def _require_digest(row: Mapping[str, str], field: str, row_number: int) -> str:
    value = str(row.get(field) or "")
    if not DIGEST_RE.fullmatch(value):
        raise CampaignValidationError(
            f"index row {row_number} is missing valid provenance field {field}"
        )
    return value


def _validate_index_observability(
    row: Mapping[str, str], row_number: int, config: SimpleNamespace
) -> None:
    context = f"index row {row_number}"
    measure_s = positive_float(row.get("measure_s"), f"{context} measure_s")
    if config.require_power_data:
        expected_nodes = 2 if config.config_label in {"tp2", "pp2"} else 1
        nodes = positive_integer(row.get("power_nodes"), f"{context} power_nodes")
        if nodes != expected_nodes:
            raise CampaignValidationError(
                f"{context} has power_nodes={nodes}, expected {expected_nodes}"
            )
        for field in ("mean_power_w", "mean_power_w_head", "energy_per_token_j", "tokens_per_watt"):
            positive_float(row.get(field), f"{context} {field}")
        power_fields = [("power_samples_head", "power_max_gap_s_head")]
        if expected_nodes == 2:
            positive_float(row.get("mean_power_w_worker"), f"{context} mean_power_w_worker")
            power_fields.append(("power_samples_worker", "power_max_gap_s_worker"))
        allowed_gap = max(5.0, 10.0 * config.power_interval_ms / 1000.0)
        for count_field, gap_field in power_fields:
            count = nonnegative_integer(row.get(count_field), f"{context} {count_field}")
            gap = finite_float(row.get(gap_field), f"{context} {gap_field}")
            if count < 2:
                raise CampaignValidationError(f"{context} {count_field} must be at least 2")
            if gap < 0 or gap > allowed_gap:
                raise CampaignValidationError(
                    f"{context} {gap_field} exceeds {allowed_gap:g}s"
                )

    if config.require_vllm_metrics_data:
        for field in (
            "requests_running_mean",
            "requests_waiting_mean",
            "kv_cache_usage_mean",
            "server_prompt_tokens",
            "num_preemptions",
        ):
            value = finite_float(row.get(field), f"{context} {field}")
            if value < 0:
                raise CampaignValidationError(f"{context} {field} must be non-negative")
        positive_float(row.get("server_generation_tokens"), f"{context} server_generation_tokens")
        minimum_span = max(0.0, measure_s - 5.0)
        for prefix in ("server_metric", "kv_metric"):
            samples = nonnegative_integer(
                row.get(f"{prefix}_samples"), f"{context} {prefix}_samples"
            )
            span = finite_float(
                row.get(f"{prefix}_sample_span_s"),
                f"{context} {prefix}_sample_span_s",
            )
            gap = finite_float(
                row.get(f"{prefix}_max_gap_s"),
                f"{context} {prefix}_max_gap_s",
            )
            if samples < 2:
                raise CampaignValidationError(
                    f"{context} {prefix}_samples must be at least 2"
                )
            if span < minimum_span:
                raise CampaignValidationError(
                    f"{context} {prefix}_sample_span_s does not span the window"
                )
            if gap < 0 or gap > 5.0:
                raise CampaignValidationError(
                    f"{context} {prefix}_max_gap_s exceeds 5s"
                )


def _require_nonempty_file(path: Path, description: str) -> Path:
    try:
        valid = path.is_file() and path.stat().st_size > 0
    except OSError:
        valid = False
    if not valid:
        raise CampaignValidationError(
            f"missing or empty {description}: {path}"
        )
    return path


def _load_json_object_strict(path: Path, description: str) -> dict[str, object]:
    """Load one JSON object while rejecting duplicate keys at every level."""
    _require_nonempty_file(path, description)
    if path.is_symlink():
        raise CampaignValidationError(
            f"{description} must not be a symlink: {path}"
        )

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_bytes(), object_pairs_hook=reject_duplicates
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise CampaignValidationError(
            f"invalid {description} {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise CampaignValidationError(
            f"{description} {path} must contain a JSON object"
        )
    return value


def _locked_distributions_from_file(path: Path) -> list[dict[str, str]]:
    """Convert shared exact pins to the prep manifest's record format."""
    try:
        return [pin.as_dict() for pin in parse_lock(path)]
    except LockFileError as exc:
        if exc.code == "invalid_pin":
            message = f"{path}:{exc.line_number} is not an exact name==version pin"
        elif exc.code == "duplicate":
            message = (
                f"{path}:{exc.line_number} repeats distribution "
                f"{exc.normalized_name!r}"
            )
        elif exc.code == "empty":
            message = f"dependency lock has no pins: {path}"
        elif isinstance(exc.__cause__, (OSError, UnicodeDecodeError)):
            raise exc.__cause__
        else:
            message = str(exc)
        raise CampaignValidationError(message) from exc


def _validate_client_environment_manifest(path: Path) -> dict[str, object]:
    """Prove the prep manifest, current runtime, lock, and row hash agree."""
    from bench import provenance

    manifest = _load_json_object_strict(path, "client environment manifest")
    if manifest.get("schema_version") != 1:
        raise CampaignValidationError(
            "client environment manifest schema_version must be 1"
        )
    current = provenance.client_environment()
    python_info = manifest.get("python")
    platform_info = manifest.get("platform")
    virtual = manifest.get("virtual_environment")
    lock_info = manifest.get("requirements_lock")
    installed = manifest.get("installed_distributions")
    locked = manifest.get("locked_distributions")
    pip_check = manifest.get("pip_check")
    if not all(isinstance(value, dict) for value in (
        python_info, platform_info, virtual, lock_info, pip_check
    )):
        raise CampaignValidationError(
            "client environment manifest has malformed runtime sections"
        )
    if not isinstance(installed, list) or not isinstance(locked, list):
        raise CampaignValidationError(
            "client environment manifest distribution lists are malformed"
        )
    expected_runtime = {
        "python.implementation": (
            python_info.get("implementation"),
            current["python_implementation"],
        ),
        "python.version": (
            python_info.get("version"),
            current["python_version"],
        ),
        "platform.platform": (
            platform_info.get("platform"),
            current["platform"],
        ),
        "platform.machine": (
            platform_info.get("machine"),
            current["machine"],
        ),
        "virtual_environment.real": (
            virtual.get("real"),
            current["virtual_environment"],
        ),
        "virtual_environment.user_site_enabled": (
            virtual.get("user_site_enabled"),
            current["user_site_enabled"],
        ),
    }
    mismatched_runtime = [
        f"{field}: {actual!r} != {expected!r}"
        for field, (actual, expected) in expected_runtime.items()
        if actual != expected
    ]
    if mismatched_runtime:
        raise CampaignValidationError(
            "client environment manifest does not match the active runtime: "
            + "; ".join(mismatched_runtime)
        )
    if virtual.get("real") is not True or virtual.get("user_site_enabled") is not False:
        raise CampaignValidationError(
            "client environment manifest is not an isolated real virtual environment"
        )

    normalized_versions: dict[str, str] = {}
    checked_installed: list[dict[str, str]] = []
    for index, item in enumerate(installed, start=1):
        if not isinstance(item, dict):
            raise CampaignValidationError(
                f"installed distribution {index} is not an object"
            )
        name = item.get("name")
        normalized = item.get("normalized_name")
        version = item.get("version")
        if not all(isinstance(value, str) and value for value in (
            name, normalized, version
        )):
            raise CampaignValidationError(
                f"installed distribution {index} has empty fields"
            )
        if normalized != normalize_distribution_name(name):
            raise CampaignValidationError(
                f"installed distribution {name!r} has wrong normalized_name"
            )
        previous = normalized_versions.setdefault(normalized, version)
        if previous != version:
            raise CampaignValidationError(
                f"installed distribution {normalized!r} has conflicting versions"
            )
        checked_installed.append({
            "name": name,
            "normalized_name": normalized,
            "version": version,
        })
    sorted_installed = sorted(
        checked_installed,
        key=lambda item: (
            item["normalized_name"],
            item["name"].casefold(),
            item["version"],
        ),
    )
    if checked_installed != sorted_installed:
        raise CampaignValidationError(
            "client environment installed distributions are not canonically sorted"
        )
    installed_digest = hashlib.sha256(
        json.dumps(
            checked_installed,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    if manifest.get("installed_distributions_sha256") != installed_digest:
        raise CampaignValidationError(
            "client environment installed-distribution hash does not match"
        )

    lock_path = ROOT / "requirements.lock"
    _require_nonempty_file(lock_path, "dependency lock")
    current_locked = _locked_distributions_from_file(lock_path)
    if locked != current_locked:
        raise CampaignValidationError(
            "client environment locked distributions differ from requirements.lock"
        )
    if lock_info.get("sha256") != sha256_file(lock_path):
        raise CampaignValidationError(
            "client environment requirements.lock hash does not match"
        )
    for pin in current_locked:
        if normalized_versions.get(pin["normalized_name"]) != pin["version"]:
            raise CampaignValidationError(
                f"locked distribution {pin['normalized_name']}=={pin['version']} "
                "is not installed"
            )
    current_distribution_list = [
        f"{name}=={version}"
        for name, version in sorted(normalized_versions.items())
    ]
    if current_distribution_list != current["installed_distributions"]:
        raise CampaignValidationError(
            "client environment manifest distribution closure does not match "
            "the active benchmark interpreter"
        )
    if pip_check.get("ok") is not True or pip_check.get("returncode") != 0:
        raise CampaignValidationError(
            "client environment manifest does not record a passing pip check"
        )
    return manifest

def _indexed_artifact(
    row: Mapping[str, str],
    row_number: int,
    out_dir: Path,
    kind: str,
    *,
    required: bool,
) -> Path | None:
    """Resolve and cryptographically verify one index-declared artifact."""
    path_field = f"{kind}_path"
    digest_field = f"{kind}_sha256"
    bytes_field = f"{kind}_bytes"
    raw_path = str(row.get(path_field) or "")
    if not raw_path:
        if required:
            raise CampaignValidationError(
                f"index row {row_number} is missing required {path_field}"
            )
        return None
    if Path(raw_path).name != raw_path:
        raise CampaignValidationError(
            f"index row {row_number} has unsafe {path_field}={raw_path!r}"
        )
    expected_digest = str(row.get(digest_field) or "")
    if not DIGEST_RE.fullmatch(expected_digest):
        raise CampaignValidationError(
            f"index row {row_number} has invalid {digest_field}"
        )
    expected_bytes = nonnegative_integer(
        row.get(bytes_field), f"index row {row_number} {bytes_field}"
    )
    path = _require_nonempty_file(out_dir / raw_path, f"{kind} artifact")
    actual_bytes = path.stat().st_size
    if actual_bytes != expected_bytes:
        raise CampaignValidationError(
            f"index row {row_number} {kind} artifact size is {actual_bytes}, "
            f"expected {expected_bytes}"
        )
    actual_digest = sha256_file(path)
    if actual_digest != expected_digest.lower():
        raise CampaignValidationError(
            f"index row {row_number} {kind} artifact SHA-256 does not match"
        )
    return path



def _load_summary(path: Path, run_tag: str) -> dict:
    _require_nonempty_file(path, "summary artifact")
    try:
        with path.open(encoding="utf-8") as handle:
            summary = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CampaignValidationError(f"invalid summary artifact {path}: {exc}") from exc
    if not isinstance(summary, dict):
        raise CampaignValidationError(f"summary artifact {path} must contain an object")
    if summary.get("run_tag") != run_tag:
        raise CampaignValidationError(
            f"summary artifact {path} has run_tag {summary.get('run_tag')!r}, "
            f"expected {run_tag!r}"
        )
    return summary

def _validate_serving_policy(
    path: Path,
    row: Mapping[str, str],
    point: ExpectedPoint,
    model: ModelSpec,
    row_number: int,
    out_dir: Path,
) -> dict[str, object]:
    """Check an indexed serving policy against its model and index row."""
    from bench.release_contracts import load_serving_policy

    context = f"serving policy for index row {row_number}"
    policy_config = SimpleNamespace(
        require_provenance=True,
        out_dir=str(out_dir),
        config_label=point.config_label,
        model=point.model,
        model_revision=model.revision,
        tokenizer=model.tokenizer,
        tokenizer_revision=model.tokenizer_revision,
        serving_fingerprint=row.get("serving_fingerprint"),
        trust_remote_code=model.trust_remote_code,
    )
    try:
        policy, artifact = load_serving_policy(str(path), policy_config)
    except RuntimeError as exc:
        raise CampaignValidationError(f"{context}: {exc}") from exc
    if policy is None or artifact is None:
        raise CampaignValidationError(f"{context} did not load")
    for field in ("path", "sha256", "bytes"):
        expected = row.get(f"serving_policy_{field}")
        actual = artifact.get(field)
        if field == "bytes":
            expected = nonnegative_integer(expected, f"{context} bytes")
        if str(actual) != str(expected):
            raise CampaignValidationError(
                f"{context} metadata {field} does not match index.csv"
            )
    expected_policy = {
        "revision": model.revision,
        "tokenizer": model.tokenizer,
        "tokenizer_revision": model.tokenizer_revision,
        "quant": model.quantization,
    }
    for field, expected in expected_policy.items():
        if str(policy.get(field) or "") != expected:
            raise CampaignValidationError(
                f"{context} has {field}={policy.get(field)!r}, expected {expected!r}"
            )
    policy_max_len = positive_integer(
        policy.get("max_model_len"), f"{context} max_model_len"
    )
    if policy_max_len != model.max_model_len:
        raise CampaignValidationError(
            f"{context} has max_model_len={policy_max_len}, expected "
            f"{model.max_model_len} from models.yaml"
        )
    policy_gpu_util = positive_float(
        policy.get("gpu_util"), f"{context} gpu_util"
    )
    if not math.isclose(
        policy_gpu_util, model.gpu_utilization, rel_tol=0.0, abs_tol=1e-12
    ):
        raise CampaignValidationError(
            f"{context} has gpu_util={policy_gpu_util}, expected "
            f"{model.gpu_utilization} from models.yaml"
        )
    policy_max_num_seqs = policy.get("max_num_seqs")
    if model.max_num_seqs is not None:
        if positive_integer(policy_max_num_seqs, f"{context} max_num_seqs") != model.max_num_seqs:
            raise CampaignValidationError(
                f"{context} max_num_seqs does not match models.yaml"
            )
    elif policy_max_num_seqs not in (None, ""):
        raise CampaignValidationError(
            f"{context} sets max_num_seqs without a models.yaml policy"
        )
    expected_human = {
        "model_revision": model.revision,
        "tokenizer": model.tokenizer,
        "tokenizer_revision": model.tokenizer_revision,
        "vllm_image": str(policy["image"]),
        "dtype": str(policy["dtype"]),
        "quantization": str(policy.get("quant") or ""),
        "prefix_caching": str(policy["prefix_caching"]),
        "generation_config": str(policy["generation_config"]),
        "runtime_fingerprint": str(policy["runtime_facts_sha256"]),
    }
    for field, expected in expected_human.items():
        if str(row.get(field) or "") != expected:
            raise CampaignValidationError(
                f"index row {row_number} has {field}={row.get(field)!r}, "
                f"expected {expected!r} from its serving policy"
            )
    parallel = {"single": (1, 1), "tp2": (2, 1), "pp2": (1, 2)}[point.config_label]
    tensor = positive_integer(row.get("tensor_parallel_size"), f"{context} TP")
    pipeline = positive_integer(row.get("pipeline_parallel_size"), f"{context} PP")
    if (tensor, pipeline) != parallel:
        raise CampaignValidationError(
            f"{context} has TP/PP={(tensor, pipeline)}, expected {parallel}"
        )
    return policy


def _validate_summary(
    summary: Mapping[str, object],
    row: Mapping[str, str],
    point: ExpectedPoint,
    model: ModelSpec,
    row_number: int,
    base_config: ExperimentConfig,
    logs_dir: Path,
    serving_policy: Mapping[str, object],
) -> Path:
    """Cross-check a summary with its row, policy, and release gates."""
    from bench.release_contracts import (
        validate_observability,
        validate_provenance,
        validate_result_quality,
    )

    context = f"summary for index row {row_number}"
    for field in ("model_label", "model", "config_label"):
        expected = {
            "model_label": point.model_label,
            "model": point.model,
            "config_label": point.config_label,
        }[field]
        if summary.get(field) != expected:
            raise CampaignValidationError(
                f"{context} has {field}={summary.get(field)!r}, expected {expected!r}"
            )
    for field, expected in (
        ("concurrency", point.concurrency),
        ("repeat_id", point.repeat_id),
    ):
        actual = positive_integer(summary.get(field), f"{context} {field}")
        if actual != expected:
            raise CampaignValidationError(
                f"{context} has {field}={actual}, expected {expected}"
            )

    for field in PROVENANCE_DIGEST_FIELDS:
        if summary.get(field) != row.get(field):
            raise CampaignValidationError(
                f"{context} provenance {field} does not match index.csv"
            )
    if summary.get("methodology_version") != row.get("methodology_version"):
        raise CampaignValidationError(
            f"{context} methodology_version does not match index.csv"
        )
    if summary.get("harness_git_commit") != row.get("harness_git_commit"):
        raise CampaignValidationError(
            f"{context} harness_git_commit does not match index.csv"
        )
    if str(summary.get("trace_sha256") or "") != str(
        row.get("trace_sha256") or ""
    ):
        raise CampaignValidationError(
            f"{context} trace_sha256 does not match index.csv"
        )

    if summary.get("serving_policy") != dict(serving_policy):
        raise CampaignValidationError(
            f"{context} embedded serving_policy does not match its indexed file"
        )
    summary_artifacts = summary.get("artifacts")
    if not isinstance(summary_artifacts, dict):
        raise CampaignValidationError(f"{context} is missing artifact metadata")
    policy_artifact = summary_artifacts.get("serving_policy")
    if not isinstance(policy_artifact, dict):
        raise CampaignValidationError(f"{context} is missing serving-policy metadata")
    for suffix in ("path", "sha256", "bytes"):
        if str(policy_artifact.get(suffix)) != str(row.get(f"serving_policy_{suffix}")):
            raise CampaignValidationError(
                f"{context} serving-policy {suffix} does not match index.csv"
            )

    raw_config = summary.get("config")
    if not isinstance(raw_config, dict):
        raise CampaignValidationError(f"{context} is missing its resolved config")
    expected_config_values = {
        "model": point.model,
        "model_label": point.model_label,
        "config_label": point.config_label,
        "model_revision": model.revision,
        "tokenizer": model.tokenizer,
        "tokenizer_revision": model.tokenizer_revision,
        "serving_fingerprint": row.get("serving_fingerprint"),
        "deployment_fingerprint": row.get("deployment_fingerprint"),
    }
    for field, expected in expected_config_values.items():
        if raw_config.get(field) != expected:
            raise CampaignValidationError(
                f"{context} resolved config has {field}={raw_config.get(field)!r}, "
                f"expected {expected!r}"
            )
    server_info = summary.get("server_info")
    if not isinstance(server_info, dict):
        raise CampaignValidationError(f"{context} is missing server_info")
    live_max_len = positive_integer(
        server_info.get("max_model_len"), f"{context} live max_model_len"
    )
    if live_max_len != model.max_model_len:
        raise CampaignValidationError(
            f"{context} live max_model_len={live_max_len}, expected "
            f"{model.max_model_len}"
        )
    live_gpu_util = positive_float(
        server_info.get("gpu_memory_utilization"),
        f"{context} live gpu_memory_utilization",
    )
    if not math.isclose(
        live_gpu_util, model.gpu_utilization, rel_tol=0.0, abs_tol=1e-12
    ):
        raise CampaignValidationError(
            f"{context} live gpu_memory_utilization={live_gpu_util}, expected "
            f"{model.gpu_utilization}"
        )
    server_log_value = str(raw_config.get("server_log") or "")
    server_log_name = Path(server_log_value).name
    expected_log_prefix = f"server_{model.label}_{point.config_label}"
    if (
        not server_log_value
        or not server_log_name.endswith(".log")
        or not (
            server_log_name == f"{expected_log_prefix}.log"
            or server_log_name.startswith(f"{expected_log_prefix}_")
        )
    ):
        raise CampaignValidationError(
            f"{context} has invalid server_log={server_log_value!r}"
        )
    server_log = _require_nonempty_file(
        logs_dir / server_log_name, "server log artifact"
    )

    provenance_config = SimpleNamespace(
        require_provenance=True,
        model_revision=raw_config.get("model_revision"),
        use_token_ids=raw_config.get("use_token_ids", True),
        tokenizer=raw_config.get("tokenizer"),
        tokenizer_revision=raw_config.get("tokenizer_revision"),
        serving_fingerprint=raw_config.get("serving_fingerprint"),
        deployment_fingerprint=raw_config.get("deployment_fingerprint"),
    )
    try:
        validate_provenance(provenance_config)
    except RuntimeError as exc:
        raise CampaignValidationError(f"{context}: {exc}") from exc

    monitor_config = SimpleNamespace(
        require_power_data=base_config.require_power_data,
        power_backend=base_config.power_backend,
        power_interval_ms=base_config.power_interval_ms,
        require_vllm_metrics_data=base_config.require_vllm_metrics_data,
        scrape_vllm_metrics=base_config.scrape_vllm_metrics,
        config_label=point.config_label,
    )
    try:
        validate_observability(summary, monitor_config)
    except RuntimeError as exc:
        raise CampaignValidationError(f"{context}: {exc}") from exc
    _validate_index_observability(row, row_number, monitor_config)
    try:
        validate_result_quality(summary, base_config)
    except RuntimeError as exc:
        raise CampaignValidationError(f"{context}: {exc}") from exc
    return server_log


def validate_allreduce_csv(path: str | os.PathLike[str]) -> None:
    """Validate one Ray all-reduce CSV against the benchmark artifact schema."""
    source = Path(path)
    _require_nonempty_file(source, "allreduce artifact")
    try:
        with source.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise CampaignValidationError(f"invalid allreduce artifact {source}: {exc}") from exc
    if tuple(reader.fieldnames or ()) != ALLREDUCE_FIELDS:
        raise CampaignValidationError(
            f"allreduce artifact {source} has the wrong header"
        )
    try:
        sizes = [
            positive_integer(row.get("bytes"), f"{source} bytes")
            for row in rows
        ]
        for row_number, row in enumerate(rows, start=2):
            if None in row or any(value is None for value in row.values()):
                raise CampaignValidationError(
                    f"{source} row {row_number} has the wrong number of columns"
                )
            # Latencies must be strictly positive; bandwidths may be exactly
            # zero at the smallest transfer sizes, where GB/s rounds to 0.000.
            # The real 1 GiB busbw floor is enforced by ray_allreduce_bench.sh
            # before the artifact is ever written.
            for field in ("avg_s", "min_s"):
                positive_float(
                    row.get(field), f"{source} row {row_number} {field}"
                )
            for field in ("algbw_GBps", "busbw_GBps"):
                nonnegative_float(
                    row.get(field), f"{source} row {row_number} {field}"
                )
            if float(row["min_s"]) > float(row["avg_s"]):
                raise CampaignValidationError(
                    f"{source} row {row_number} min_s exceeds avg_s"
                )
    except CampaignValidationError:
        raise
    if tuple(sizes) != ALLREDUCE_SIZES:
        raise CampaignValidationError(
            f"allreduce artifact {source} must contain sizes 2^3 through 2^30 "
            "exactly once in order"
        )


def _expected_allreduce_fingerprint(
    runtime_fingerprint: str, policy: Mapping[str, object]
) -> str:
    """Reproduce ray_allreduce_bench.sh's artifact identity without live Ray."""
    source_lines = bytearray()
    for path in (
        ROOT / "scripts/ray_allreduce_bench.py",
        ROOT / "scripts/ray_allreduce_bench.sh",
    ):
        digest = sha256_file(path)
        source_lines.extend(f"{digest}  {path.resolve()}\n".encode())
    source_fingerprint = hashlib.sha256(source_lines).hexdigest()
    values = (
        runtime_fingerprint,
        source_fingerprint,
        os.environ.get("ALLREDUCE_PORT", "29517"),
        str(policy["connectx_iface"]),
        str(policy["nccl_ib_disable"]),
        os.environ.get("ALLREDUCE_MIN_1G_BUSBW_GBPS", "16"),
        os.environ.get("ALLREDUCE_MAX_8B_LATENCY_S", "0.0001"),
    )
    serialized = "".join(f"{value}\n" for value in values).encode()
    return hashlib.sha256(serialized).hexdigest()


def verify_campaign(
    models_path: str | os.PathLike[str],
    experiment_path: str | os.PathLike[str],
    out_dir: str | os.PathLike[str],
    concurrencies: Sequence[object] | str,
    repeats: object,
    manifest_path: str | os.PathLike[str] | None = None,
    completed_only: bool = False,
    allow_source_match_commits: bool = False,
) -> dict[str, object]:
    """Verify an exact finished campaign and optionally write its manifest.

    With ``completed_only``, restrict the expected grid to the
    (model_label, config_label) combos that actually appear in the index, so
    combos that were intentionally not run are not required. Each present combo
    must still carry its full concurrency/repeat rectangle, and every artifact,
    provenance, and quality check runs unchanged on the rows that are present.

    With ``allow_source_match_commits``, do not require every row to share one
    ``harness_git_commit`` equal to current HEAD. The stronger, measurement
    affecting guarantee still holds strictly: every row's
    ``harness_source_sha256`` must be identical and equal the current source
    fingerprint. Use for a campaign whose results were committed incrementally
    without changing the fingerprinted harness source.
    """
    campaign = load_campaign(models_path, experiment_path)
    loads = parse_concurrencies(concurrencies)
    repeat_count = positive_integer(repeats, "repeats")
    grid = build_expected_grid(campaign.models, loads, repeat_count)
    expected = {point.key: point for point in grid}
    campaign_root = Path(out_dir).expanduser().resolve()
    output = resolve_campaign_results_dir(campaign_root)
    logs_directory = resolve_campaign_logs_dir(campaign_root)
    index_path = _require_nonempty_file(output / "index.csv", "index artifact")

    from bench import artifacts, provenance
    try:
        artifacts.validate_index_schema(str(output))
    except RuntimeError as exc:
        raise CampaignValidationError(str(exc)) from exc

    try:
        with index_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise CampaignValidationError(f"cannot read {index_path}: {exc}") from exc

    actual: dict[tuple[str, str, int, int], tuple[int, dict[str, str]]] = {}
    duplicates: set[tuple[str, str, int, int]] = set()
    for row_number, row in enumerate(rows, start=2):
        identity = _row_identity(row, row_number)
        if identity in actual:
            duplicates.add(identity)
        else:
            actual[identity] = (row_number, row)
    if duplicates:
        raise CampaignValidationError(
            "index contains duplicate expected identities: " + _format_points(duplicates)
        )

    skipped_combos: list[str] = []
    if completed_only:
        present_combos = {(key[0], key[1]) for key in actual}
        if not present_combos:
            raise CampaignValidationError(
                "completed-only verification found no rows in the index"
            )
        planned_combos = {
            (model.label, config)
            for model in campaign.models
            for config in model.configs
        }
        skipped_combos = sorted(
            f"{label}/{config}" for label, config in planned_combos - present_combos
        )
        expected = {
            key: point
            for key, point in expected.items()
            if (key[0], key[1]) in present_combos
        }

    missing = set(expected) - set(actual)
    extra = set(actual) - set(expected)
    if missing:
        raise CampaignValidationError(
            "index is missing expected rows: " + _format_points(missing)
        )
    if extra:
        raise CampaignValidationError(
            "index contains extra rows: " + _format_points(extra)
        )

    try:
        expected_config_sha = provenance.config_file_fingerprint(
            str(campaign.experiment_path)
        )
        base_config = ExperimentConfig.from_yaml(
            str(campaign.experiment_path)
        )
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        raise CampaignValidationError(
            f"experiment YAML is not accepted by the benchmark runner: {exc}"
        ) from exc
    current_source = provenance.source_fingerprint()
    current_environment = provenance.client_environment_fingerprint()
    current_commit = provenance.git_commit()
    if not COMMIT_RE.fullmatch(str(current_commit or "")):
        raise CampaignValidationError(
            "current benchmark source has no recoverable Git commit"
        )
    expected_trace = str(base_config.trace_sha256 or "")
    environment_path = _require_nonempty_file(
        output / "client_environment.json", "client environment manifest"
    )
    _validate_client_environment_manifest(environment_path)

    model_by_label = {model.label: model for model in campaign.models}
    consistent_global: dict[str, str] = {}
    consistent_pair: dict[tuple[str, str, str], str] = {}
    consistent_experiment: dict[tuple[str, str, int], str] = {}
    consistent_policy: dict[str, str] = {}
    commit_values: set[str] = set()
    runtime_fingerprints: dict[str, str] = {}
    runtime_policies: dict[str, Mapping[str, object]] = {}
    launch_repeats: dict[Path, tuple[str, str, int]] = {}
    raw_paths: list[Path] = []
    serving_policy_paths: list[Path] = []
    summary_paths: list[Path] = []
    log_paths: list[Path] = []
    power_sample_paths: list[Path] = []
    vllm_sample_paths: list[Path] = []

    for identity, point in expected.items():
        row_number, row = actual[identity]
        context = f"index row {row_number}"
        if row.get("model") != point.model:
            raise CampaignValidationError(
                f"{context} has model={row.get('model')!r}, expected {point.model!r}"
            )
        row_repeat_count = positive_integer(
            row.get("repeat_count"), f"{context} repeat_count"
        )
        if not point.repeat_id <= row_repeat_count <= repeat_count:
            raise CampaignValidationError(
                f"{context} repeat_count={row_repeat_count} is incompatible with "
                f"repeat_id={point.repeat_id} and final repeats={repeat_count}"
            )
        if _row_concurrencies(row, row_number) != loads:
            raise CampaignValidationError(
                f"{context} expected_concurrencies does not equal {loads}"
            )
        failed = nonnegative_integer(row.get("requests_failed"), f"{context} requests_failed")
        completed = nonnegative_integer(
            row.get("requests_completed"), f"{context} requests_completed"
        )
        tokens = nonnegative_integer(
            row.get("total_output_tokens"), f"{context} total_output_tokens"
        )
        if failed:
            raise CampaignValidationError(f"{context} records {failed} failed requests")
        if completed == 0:
            raise CampaignValidationError(f"{context} records zero completed requests")
        if tokens == 0:
            raise CampaignValidationError(f"{context} records zero output tokens")

        if row.get("methodology_version") != provenance.METHODOLOGY_VERSION:
            raise CampaignValidationError(
                f"{context} methodology_version is not {provenance.METHODOLOGY_VERSION!r}"
            )
        for field in PROVENANCE_DIGEST_FIELDS:
            value = _require_digest(row, field, row_number)
            if field in {"harness_source_sha256", "client_environment_sha256"}:
                previous = consistent_global.setdefault(field, value)
                if previous != value:
                    raise CampaignValidationError(
                        f"index mixes multiple {field} values"
                    )
            # deployment_fingerprint is checked for shape only. It hashes the
            # KV capacity vLLM lands on after memory profiling, which differs
            # by a few thousand blocks on every launch, so repeats of one arm
            # collected across launches legitimately carry different values.
            # The launch policy behind them is pinned by serving_fingerprint.
            if field == "serving_fingerprint":
                key = (point.model_label, point.config_label, field)
                previous = consistent_pair.setdefault(key, value)
                if previous != value:
                    raise CampaignValidationError(
                        f"index mixes multiple {field} values for "
                        f"{point.model_label}/{point.config_label}"
                    )
            if field == "experiment_fingerprint":
                key = (point.model_label, point.config_label, point.concurrency)
                previous = consistent_experiment.setdefault(key, value)
                if previous != value:
                    raise CampaignValidationError(
                        "repeats have different experiment_fingerprint values for "
                        f"{point.model_label}/{point.config_label}/c{point.concurrency}"
                    )
        if row.get("config_file_sha256") != expected_config_sha:
            raise CampaignValidationError(
                f"{context} config_file_sha256 does not match the supplied experiment YAML"
            )
        expected_current = {
            "harness_source_sha256": current_source,
            "client_environment_sha256": current_environment,
        }
        for field, expected_value in expected_current.items():
            if row.get(field) != expected_value:
                raise CampaignValidationError(
                    f"{context} {field} does not match the current release "
                    "source/environment"
                )
        if str(row.get("trace_sha256") or "") != expected_trace:
            raise CampaignValidationError(
                f"{context} trace_sha256 does not match the supplied trace"
            )
        git_commit = str(row.get("harness_git_commit") or "")
        if not COMMIT_RE.fullmatch(git_commit):
            raise CampaignValidationError(
                f"{context} is missing a valid harness_git_commit"
            )
        commit_values.add(git_commit)
        if not allow_source_match_commits:
            previous_commit = consistent_global.setdefault(
                "harness_git_commit", git_commit
            )
            if previous_commit != git_commit:
                raise CampaignValidationError(
                    "index mixes multiple harness_git_commit values"
                )
            if git_commit != current_commit:
                raise CampaignValidationError(
                    f"{context} harness_git_commit does not match current HEAD"
                )

        run_tag = str(row.get("run_tag") or "")
        if not SAFE_RUN_TAG_RE.fullmatch(run_tag):
            raise CampaignValidationError(f"{context} has unsafe run_tag {run_tag!r}")
        raw_path = _indexed_artifact(
            row, row_number, output, "raw", required=True
        )
        summary_path = _indexed_artifact(
            row, row_number, output, "summary", required=True
        )
        power_sample_path = _indexed_artifact(
            row,
            row_number,
            output,
            "power_samples",
            required=base_config.require_power_data,
        )
        vllm_sample_path = _indexed_artifact(
            row,
            row_number,
            output,
            "vllm_samples",
            required=base_config.require_vllm_metrics_data,
        )
        serving_policy_path = _indexed_artifact(
            row, row_number, output, "serving_policy", required=True
        )
        serving_policy = _validate_serving_policy(
            serving_policy_path,
            row,
            point,
            model_by_label[point.model_label],
            row_number,
            output,
        )
        expected_scope = (
            "single-host" if point.config_label == "single" else "dual-cluster"
        )
        if serving_policy.get("runtime_scope") != expected_scope:
            raise CampaignValidationError(
                f"{context} serving runtime_scope is "
                f"{serving_policy.get('runtime_scope')!r}, expected "
                f"{expected_scope!r}"
            )
        if str(serving_policy.get("power_backend")) != base_config.power_backend:
            raise CampaignValidationError(
                f"{context} serving policy power_backend does not match "
                "experiment.yaml"
            )
        global_policy_fields = (
            "image",
            "dtype",
            "prefix_caching",
            "generation_config",
            "torchinductor_autograd_cache",
            "hf_hub_offline",
            "ray_version",
            "connectx_iface",
            "nccl_ib_disable",
            "head_ip",
            "worker_ip",
            "power_backend",
        )
        for field in global_policy_fields:
            value = serving_policy.get(field)
            if value in (None, ""):
                raise CampaignValidationError(
                    f"{context} serving policy has no {field}"
                )
            canonical = json.dumps(
                value, sort_keys=True, separators=(",", ":")
            )
            previous = consistent_policy.setdefault(field, canonical)
            if previous != canonical:
                raise CampaignValidationError(
                    f"campaign mixes multiple serving-policy {field} values"
                )
        # Optional field: absent means vLLM's default (autotune on). Kernel
        # selection must be one policy across single, tp2, and pp2.
        autotune = str(serving_policy.get("flashinfer_autotune") or "1")
        if autotune not in ("0", "1"):
            raise CampaignValidationError(
                f"{context} serving policy has flashinfer_autotune={autotune!r}"
            )
        if consistent_policy.setdefault("flashinfer_autotune", autotune) != autotune:
            raise CampaignValidationError(
                "campaign mixes multiple serving-policy flashinfer_autotune values"
            )
        scope_key = "single" if point.config_label == "single" else "dual"
        runtime_fingerprint = str(
            serving_policy.get("runtime_facts_sha256") or ""
        )
        previous_runtime = runtime_fingerprints.setdefault(
            scope_key, runtime_fingerprint
        )
        if previous_runtime != runtime_fingerprint:
            raise CampaignValidationError(
                f"campaign mixes multiple {scope_key}-node runtime fingerprints"
            )
        runtime_policies.setdefault(scope_key, serving_policy)
        summary = _load_summary(summary_path, run_tag)
        server_log_path = _validate_summary(
            summary,
            row,
            point,
            model_by_label[point.model_label],
            row_number,
            base_config,
            logs_directory,
            serving_policy,
        )
        launch_identity = (point.model_label, point.config_label, point.repeat_id)
        previous_launch = launch_repeats.setdefault(server_log_path.resolve(), launch_identity)
        if previous_launch != launch_identity:
            raise CampaignValidationError(
                "independent replicates must use distinct server launch logs; "
                f"{server_log_path} is shared by {previous_launch} and {launch_identity}"
            )
        raw_paths.append(raw_path)
        summary_paths.append(summary_path)
        log_paths.append(server_log_path)

        serving_policy_paths.append(serving_policy_path)
        if power_sample_path is not None:
            power_sample_paths.append(power_sample_path)
        if vllm_sample_path is not None:
            vllm_sample_paths.append(vllm_sample_path)

    # Derived from the verified set, not the full plan, so a completed-only run
    # of single-node combos does not demand a dual-node all-reduce artifact.
    has_dual = any(key[1] in {"tp2", "pp2"} for key in expected)
    valid_allreduce: list[Path] = []
    if has_dual:
        dual_runtime = runtime_fingerprints.get("dual")
        dual_policy = runtime_policies.get("dual")
        if not dual_runtime or dual_policy is None:
            raise CampaignValidationError(
                "dual campaign has no canonical dual runtime policy"
            )
        allreduce_id = _expected_allreduce_fingerprint(
            dual_runtime, dual_policy
        )
        matching_allreduce = sorted(
        output.glob(f"allreduce_ray_{allreduce_id}_*.csv")
        )
        allreduce_errors: list[str] = []
        for candidate in matching_allreduce:
            try:
                validate_allreduce_csv(candidate)
            except CampaignValidationError as exc:
                allreduce_errors.append(str(exc))
            else:
                valid_allreduce.append(candidate)
        if not valid_allreduce:
            detail = (
                "; ".join(allreduce_errors)
                if allreduce_errors
                else f"no artifact with identity {allreduce_id}"
            )
            raise CampaignValidationError(
                "dual-config campaign needs a valid all-reduce artifact for "
                f"the measured runtime/source/network policy ({detail})"
            )

    config_paths = [campaign.models_path, campaign.experiment_path]
    trace_value = str(campaign.experiment.get("trace_path") or "")
    if trace_value:
        trace_path = Path(trace_value)
        if not trace_path.is_absolute():
            trace_path = campaign.experiment_path.resolve().parent / trace_path
        if trace_path.is_file():
            config_paths.append(trace_path)
    lock_paths = [
        path
        for path in (ROOT / "requirements.txt", ROOT / "requirements.lock")
        if path.is_file()
    ]
    source_paths = list((ROOT / "bench").glob("*.py"))
    source_paths += list((ROOT / "scripts").rglob("*.py"))
    source_paths += list((ROOT / "scripts").glob("*.sh"))
    source_paths += list((ROOT / "plotting").glob("*.py"))
    environment_paths = [environment_path]
    figure_root = output / "figs"
    figure_paths = (
        sorted(path for path in figure_root.rglob("*") if path.is_file())
        if figure_root.is_dir()
        else []
    )
    # The NCCL channel lines ("via NET/IB") beside each microbenchmark CSV are
    # the transport evidence behind the RDMA gate, so they are sealed too.
    allreduce_logs = sorted(
        log
        for csv_path in valid_allreduce
        for log in (
            logs_directory / f"{csv_path.stem}_raw.log",
            logs_directory / f"{csv_path.stem}_nccl.log",
        )
        if log.is_file()
    )

    artifacts = {
        "index": [index_path],
        "raw": raw_paths,
        "summary": summary_paths,
        "power_samples": power_sample_paths,
        "vllm_samples": vllm_sample_paths,
        "log": log_paths,
        "allreduce": valid_allreduce,
        "allreduce_log": allreduce_logs,
        "interconnect_extra": sorted(output.glob("interconnect_extra_*.csv")),
        "interconnect_log": sorted(
            list(logs_directory.glob("interconnect_extra_*_raw.log"))
            + list(logs_directory.glob("interconnect_extra_*_nccl.log"))
        ),
        "config": config_paths,
        "lock": lock_paths,
        "source": source_paths,
        "environment": environment_paths,
        "serving_policy": serving_policy_paths,
        "figure": figure_paths,
    }
    manifest_written: str | None = None
    if manifest_path is not None:
        destination = Path(manifest_path)
        artifact_resolutions = {
            path.resolve() for paths in artifacts.values() for path in paths
        }
        if destination.resolve() in artifact_resolutions:
            raise CampaignValidationError(
                f"manifest path {destination} would overwrite a campaign artifact"
            )
        manifest = build_manifest(
            campaign=campaign,
            out_dir=campaign_root,
            concurrencies=loads,
            repeats=repeat_count,
            expected_count=len(expected),
            artifacts=artifacts,
            completed_only=completed_only,
            skipped_combos=skipped_combos,
            allow_source_match_commits=allow_source_match_commits,
            harness_git_commits=sorted(commit_values),
        )
        write_json_atomic(destination, manifest)
        manifest_written = str(destination)

    return {
        "status": "ok",
        "verified_count": len(rows),
        "expected_count": len(expected),
        "model_config_count": sum(len(model.configs) for model in campaign.models),
        "concurrencies": list(loads),
        "repeats": repeat_count,
        "completed_only": completed_only,
        "skipped_combos": skipped_combos,
        "allow_source_match_commits": allow_source_match_commits,
        "harness_git_commits": sorted(commit_values),
        "manifest": manifest_written,
    }


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--models",
        "--models-yaml",
        dest="models_path",
        default=str(ROOT / "configs/models.yaml"),
        help="model matrix YAML (default: configs/models.yaml)",
    )
    parser.add_argument(
        "--experiment",
        "--experiment-yaml",
        dest="experiment_path",
        default=str(ROOT / "configs/experiment.yaml"),
        help="experiment YAML (default: configs/experiment.yaml)",
    )
    parser.add_argument(
        "--concurrencies",
        nargs="+",
        required=True,
        metavar="N",
        help="space-separated positive concurrency values (quoted or unquoted)",
    )
    parser.add_argument(
        "--repeats", required=True, help="positive repeat count"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or verify a publication benchmark campaign"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser(
        "plan", help="validate YAML and print the expected campaign matrix"
    )
    _add_common_arguments(plan_parser)

    verify_parser = subparsers.add_parser(
        "verify", help="verify a complete result directory"
    )
    _add_common_arguments(verify_parser)
    verify_parser.add_argument(
        "--out-dir",
        default=None,
        help="result directory (default: OUT_DIR or results/benchmark-<UTC-date>)",
    )
    verify_parser.add_argument(
        "--manifest",
        help="atomically write a JSON SHA-256 artifact manifest to this path",
    )
    verify_parser.add_argument(
        "--completed-only",
        action="store_true",
        help=(
            "verify only the model/config combos present in the index "
            "(each must still have its full concurrency/repeat rectangle); "
            "use when some planned combos were intentionally not run"
        ),
    )
    verify_parser.add_argument(
        "--allow-source-match-commits",
        action="store_true",
        help=(
            "do not require one harness_git_commit equal to HEAD; still require "
            "harness_source_sha256 to be identical across rows and match the "
            "current source. Use when results were committed incrementally "
            "without changing the fingerprinted harness."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "plan":
            result = plan_campaign(
                args.models_path,
                args.experiment_path,
                args.concurrencies,
                args.repeats,
            )
        else:
            if args.out_dir is None:
                out_dir = str(resolve_campaign_dir(os.environ.get("OUT_DIR")))
            else:
                out_dir = str(resolve_campaign_dir(args.out_dir))
            if args.manifest:
                manifest_path = Path(args.manifest).expanduser()
                if not manifest_path.is_absolute():
                    manifest_path = (
                        resolve_campaign_results_dir(out_dir) / manifest_path
                    )
                args.manifest = str(manifest_path.resolve())
            result = verify_campaign(
                args.models_path,
                args.experiment_path,
                out_dir,
                args.concurrencies,
                args.repeats,
                args.manifest,
                args.completed_only,
                args.allow_source_match_commits,
            )
    except (CampaignValidationError, OSError, RuntimeError) as exc:
        print(f"validate_campaign.py: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
