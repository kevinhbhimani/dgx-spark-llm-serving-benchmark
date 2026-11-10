"""Validate campaign configuration and construct its expected execution grid."""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
import json
import math
import os
from pathlib import Path
import re
from typing import Iterable, Mapping, Sequence

import yaml

ROOT = Path(__file__).resolve().parents[2]
ALLOWED_CONFIGS = frozenset({"single", "tp2", "pp2"})
COMMIT_RE = re.compile(r"[0-9a-fA-F]{40}")
SAFE_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class CampaignValidationError(ValueError):
    """Raised when a campaign cannot be considered complete and valid."""


@dataclass(frozen=True)
class ModelSpec:
    name: str
    label: str
    revision: str
    configs: tuple[str, ...]
    max_model_len: int
    tokenizer: str
    tokenizer_revision: str
    quantization: str
    trust_remote_code: bool
    gpu_utilization: float
    max_num_seqs: int | None = None


@dataclass(frozen=True)
class Campaign:
    models_path: Path
    experiment_path: Path
    models: tuple[ModelSpec, ...]
    experiment: Mapping[str, object]
    largest_request_tokens: int


@dataclass(frozen=True)
class ModelSelection:
    models: tuple[ModelSpec, ...]
    excluded_labels: tuple[str, ...]


def select_models(
    models: Sequence[ModelSpec],
    labels: Sequence[str] | None = None,
    configs: Sequence[str] | None = None,
    single_node_only: bool = False,
) -> ModelSelection:
    """Select validated model records and filter their serving configurations."""
    requested_labels = set(labels or ())
    requested_configs = set(configs or ())
    unknown_labels = requested_labels - {model.label for model in models}
    if unknown_labels:
        raise CampaignValidationError(
            "unknown label(s): " + " ".join(sorted(unknown_labels))
        )
    unknown_configs = requested_configs - ALLOWED_CONFIGS
    if unknown_configs:
        raise CampaignValidationError(
            "configs must be among single tp2 pp2: "
            + " ".join(sorted(unknown_configs))
        )
    if single_node_only and requested_configs - {"single"}:
        raise CampaignValidationError(
            "SINGLE_NODE_ONLY=1 permits only CAMPAIGN_CONFIGS=single"
        )

    selected: list[ModelSpec] = []
    excluded: list[str] = []
    for model in models:
        if requested_labels and model.label not in requested_labels:
            continue
        if single_node_only:
            if "single" not in model.configs:
                excluded.append(model.label)
                continue
            selected.append(replace(model, configs=("single",)))
            continue
        filtered_configs = tuple(
            config
            for config in model.configs
            if not requested_configs or config in requested_configs
        )
        if filtered_configs:
            selected.append(replace(model, configs=filtered_configs))
    if not selected:
        raise CampaignValidationError("the model/config selection is empty")
    return ModelSelection(tuple(selected), tuple(excluded))


@dataclass(frozen=True, order=True)
class ExpectedPoint:
    model_label: str
    config_label: str
    concurrency: int
    repeat_id: int
    model: str

    @property
    def key(self) -> tuple[str, str, int, int]:
        return (
            self.model_label,
            self.config_label,
            self.concurrency,
            self.repeat_id,
        )


def _load_mapping(path: str | os.PathLike[str], description: str) -> dict[str, object]:
    """Read a YAML mapping with errors tied to its source path."""
    source = Path(path)
    try:
        with source.open(encoding="utf-8") as handle:
            value = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        raise CampaignValidationError(
            f"cannot load {description} {source}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise CampaignValidationError(
            f"{description} {source} must contain a YAML mapping"
        )
    return value


def _nonempty_string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CampaignValidationError(f"{context} must be a non-empty string")
    return value.strip()


def _decimal(value: object, context: str) -> Decimal:
    if isinstance(value, bool):
        raise CampaignValidationError(f"{context} must be numeric")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError) as exc:
        raise CampaignValidationError(f"{context} must be numeric") from exc
    if not number.is_finite():
        raise CampaignValidationError(f"{context} must be finite")
    return number


def positive_integer(value: object, context: str) -> int:
    """Require a finite, positive integer value."""
    number = _decimal(value, context)
    if number <= 0 or number != number.to_integral_value():
        raise CampaignValidationError(
            f"{context} must be a finite positive integer"
        )
    return int(number)


def nonnegative_integer(value: object, context: str) -> int:
    """Require a finite, non-negative integer value."""
    number = _decimal(value, context)
    if number < 0 or number != number.to_integral_value():
        raise CampaignValidationError(
            f"{context} must be a finite non-negative integer"
        )
    return int(number)


def finite_float(value: object, context: str) -> float:
    """Require a finite numeric value representable as a float."""
    number = _decimal(value, context)
    result = float(number)
    if not math.isfinite(result):
        raise CampaignValidationError(f"{context} must be finite")
    return result


def positive_float(value: object, context: str) -> float:
    """Require a finite, positive float value."""
    result = finite_float(value, context)
    if result <= 0:
        raise CampaignValidationError(f"{context} must be positive")
    return result


def nonnegative_float(value: object, context: str) -> float:
    """Require a finite, non-negative float value."""
    result = finite_float(value, context)
    if result < 0:
        raise CampaignValidationError(f"{context} must be non-negative")
    return result


def _validate_gpu_util(value: object, context: str) -> float:
    number = finite_float(value, context)
    if not 0 < number <= 1:
        raise CampaignValidationError(f"{context} must satisfy 0 < value <= 1")
    return number


def validate_models_yaml(
    path: str | os.PathLike[str],
) -> tuple[ModelSpec, ...]:
    """Validate the full model matrix and return normalized model specs."""
    document = _load_mapping(path, "models YAML")
    raw_models = document.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        raise CampaignValidationError(
            f"models YAML {path} must define a non-empty 'models' list"
        )
    gpu_util_override = os.environ.get("GPU_UTIL")
    run_gpu_utilization = _validate_gpu_util(
        gpu_util_override
        if gpu_util_override is not None
        else document.get("gpu_util_default", 0.80),
        "GPU_UTIL" if gpu_util_override is not None
        else "models YAML gpu_util_default",
    )

    models: list[ModelSpec] = []
    seen_labels: set[str] = set()
    for index, entry in enumerate(raw_models, start=1):
        context = f"model entry {index}"
        if not isinstance(entry, dict):
            raise CampaignValidationError(f"{context} must be a mapping")
        if "gpu_util" in entry:
            raise CampaignValidationError(
                f"{context} sets per-model gpu_util; configure the shared "
                "run-wide models YAML gpu_util_default instead"
            )

        name = _nonempty_string(entry.get("name"), f"{context} name")
        label = _nonempty_string(entry.get("label"), f"{context} label")
        if not SAFE_LABEL_RE.fullmatch(label):
            raise CampaignValidationError(
                f"{context} label {label!r} is unsafe; use only letters, "
                "digits, '.', '_', and '-', starting with a letter or digit"
            )
        if label in seen_labels:
            raise CampaignValidationError(
                f"models YAML contains duplicate label {label!r}"
            )
        seen_labels.add(label)

        revision = _nonempty_string(
            entry.get("revision"), f"{context} revision"
        )
        if not COMMIT_RE.fullmatch(revision):
            raise CampaignValidationError(
                f"{context} revision must be an exact 40-hex commit"
            )

        raw_configs = entry.get("configs")
        if not isinstance(raw_configs, list) or not raw_configs:
            raise CampaignValidationError(
                f"{context} configs must be a non-empty list"
            )
        configs: list[str] = []
        for raw_config in raw_configs:
            if not isinstance(raw_config, str) or raw_config not in ALLOWED_CONFIGS:
                raise CampaignValidationError(
                    f"{context} has unsupported config {raw_config!r}; allowed "
                    f"configs are {sorted(ALLOWED_CONFIGS)}"
                )
            if raw_config in configs:
                raise CampaignValidationError(
                    f"{context} repeats config {raw_config!r}"
                )
            configs.append(raw_config)

        max_model_len = positive_integer(
            entry.get("max_model_len"), f"{context} max_model_len"
        )

        gpu_utilization = run_gpu_utilization

        raw_tokenizer = entry.get("tokenizer")
        tokenizer = (
            name
            if raw_tokenizer in (None, "")
            else _nonempty_string(raw_tokenizer, f"{context} tokenizer")
        )
        raw_tokenizer_revision = entry.get("tokenizer_revision")
        tokenizer_revision = ""
        if raw_tokenizer_revision not in (None, ""):
            tokenizer_revision = _nonempty_string(
                raw_tokenizer_revision, f"{context} tokenizer_revision"
            )
            if not COMMIT_RE.fullmatch(tokenizer_revision):
                raise CampaignValidationError(
                    f"{context} tokenizer_revision must be an exact "
                    "40-hex commit"
                )
        if tokenizer != name and not tokenizer_revision:
            raise CampaignValidationError(
                f"{context} uses tokenizer {tokenizer!r} without a pinned "
                "tokenizer_revision"
            )
        if not tokenizer_revision:
            tokenizer_revision = revision
        raw_quantization = entry.get("quant")
        quantization = (
            ""
            if raw_quantization in (None, "")
            else _nonempty_string(raw_quantization, f"{context} quant")
        )
        trust_remote_code = entry.get("trust_remote_code", False)
        if not isinstance(trust_remote_code, bool):
            raise CampaignValidationError(
                f"{context} trust_remote_code must be a boolean"
            )
        max_num_seqs = entry.get("max_num_seqs")
        if "max_num_seqs" in entry and (
            isinstance(max_num_seqs, bool)
            or not isinstance(max_num_seqs, int)
            or max_num_seqs < 1
        ):
            raise CampaignValidationError(
                f"{context} max_num_seqs must be a positive integer"
            )

        models.append(
            ModelSpec(
                name=name,
                label=label,
                revision=revision,
                configs=tuple(configs),
                max_model_len=max_model_len,
                tokenizer=tokenizer,
                tokenizer_revision=tokenizer_revision,
                quantization=quantization,
                trust_remote_code=trust_remote_code,
                gpu_utilization=gpu_utilization,
                max_num_seqs=max_num_seqs,
            )
        )
    return tuple(models)


def validate_experiment_yaml(
    path: str | os.PathLike[str], models: Sequence[ModelSpec]
) -> tuple[dict[str, object], int]:
    """Validate workload buckets and ensure every model can hold the largest."""
    document = _load_mapping(path, "experiment YAML")
    # Planning must exercise the same configuration parser as measurement.
    # Otherwise a typo or invalid duration can pass prep and fail only after a
    # large checkpoint has been downloaded and loaded on both GPUs.
    from bench.config import ExperimentConfig

    try:
        config = ExperimentConfig.from_yaml(str(path))
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        raise CampaignValidationError(
            f"experiment YAML is not accepted by the benchmark runner: {exc}"
        ) from exc
    if config.require_power_data and config.power_backend == "none":
        raise CampaignValidationError(
            "experiment requires power data; power_backend cannot be none"
        )
    if config.require_vllm_metrics_data and not config.scrape_vllm_metrics:
        raise CampaignValidationError(
            "experiment requires vLLM metrics; scraping must be enabled"
        )
    raw_buckets = document.get("buckets")
    if not isinstance(raw_buckets, list) or not raw_buckets:
        raise CampaignValidationError(
            f"experiment YAML {path} must define a non-empty 'buckets' list"
        )

    largest = 0
    for index, bucket in enumerate(raw_buckets, start=1):
        context = f"experiment bucket {index}"
        if not isinstance(bucket, dict):
            raise CampaignValidationError(f"{context} must be a mapping")
        input_len = positive_integer(bucket.get("input_len"), f"{context} input_len")
        output_len = positive_integer(
            bucket.get("output_len"), f"{context} output_len"
        )
        if "weight" in bucket:
            positive_float(bucket["weight"], f"{context} weight")
        largest = max(largest, input_len + output_len)

    # A trace replaces the bucket workload in Workload, so its actual maximum
    # request length must determine the launch context requirement too.
    if config.trace_path:
        largest = 0
        try:
            with Path(config.trace_path).open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    context = f"trace {config.trace_path} line {line_number}"
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise CampaignValidationError(f"{context} must be a mapping")
                    input_value = record.get("input_len", 0)
                    if "prompt" in record and not config.use_token_ids and not input_value:
                        prompt = record["prompt"]
                        if not isinstance(prompt, str):
                            raise CampaignValidationError(
                                f"{context} prompt must be a string"
                            )
                        input_value = len(prompt.split())
                    input_len = positive_integer(input_value, f"{context} input_len")
                    output_len = positive_integer(
                        record.get("output_len", record.get("max_tokens", 128)),
                        f"{context} output_len",
                    )
                    largest = max(largest, input_len + output_len)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CampaignValidationError(f"cannot load workload trace: {exc}") from exc
        if not largest:
            raise CampaignValidationError(f"trace is empty: {config.trace_path}")

    too_small = [
        f"{model.label} ({model.max_model_len})"
        for model in models
        if model.max_model_len < largest
    ]
    if too_small:
        raise CampaignValidationError(
            f"largest workload request needs {largest} tokens, exceeding "
            "max_model_len for: " + ", ".join(too_small)
        )
    return document, largest


def load_campaign(
    models_path: str | os.PathLike[str] = ROOT / "configs/models.yaml",
    experiment_path: str | os.PathLike[str] = ROOT / "configs/experiment.yaml",
) -> Campaign:
    """Load and globally validate both campaign configuration files."""
    models_source = Path(models_path)
    experiment_source = Path(experiment_path)
    models = validate_models_yaml(models_source)
    experiment, largest = validate_experiment_yaml(experiment_source, models)
    return Campaign(
        models_path=models_source,
        experiment_path=experiment_source,
        models=models,
        experiment=experiment,
        largest_request_tokens=largest,
    )


def parse_concurrencies(values: Sequence[object] | str) -> tuple[int, ...]:
    """Parse quoted or unquoted space-separated positive concurrencies."""
    raw_values: Iterable[object]
    if isinstance(values, str):
        raw_values = values.split()
    else:
        flattened: list[object] = []
        for value in values:
            if isinstance(value, str):
                flattened.extend(value.split())
            else:
                flattened.append(value)
        raw_values = flattened

    parsed: list[int] = []
    for value in raw_values:
        concurrency = positive_integer(value, "concurrency")
        if concurrency in parsed:
            raise CampaignValidationError(
                f"concurrencies contain duplicate value {concurrency}"
            )
        parsed.append(concurrency)
    if not parsed:
        raise CampaignValidationError("at least one concurrency is required")
    return tuple(parsed)


def build_expected_grid(
    models: Sequence[ModelSpec],
    concurrencies: Sequence[object] | str,
    repeats: object,
) -> tuple[ExpectedPoint, ...]:
    """Return the exact campaign grid in model/config/repeat/load order."""
    loads = parse_concurrencies(concurrencies)
    repeat_count = positive_integer(repeats, "repeats")
    return tuple(
        ExpectedPoint(
            model_label=model.label,
            config_label=config,
            concurrency=concurrency,
            repeat_id=repeat_id,
            model=model.name,
        )
        for model in models
        for config in model.configs
        for repeat_id in range(1, repeat_count + 1)
        for concurrency in loads
    )


def plan_campaign(
    models_path: str | os.PathLike[str],
    experiment_path: str | os.PathLike[str],
    concurrencies: Sequence[object] | str,
    repeats: object,
    labels: Sequence[str] | None = None,
    configs: Sequence[str] | None = None,
    single_node_only: bool = False,
) -> dict[str, object]:
    """Validate a campaign and return its JSON-serializable execution plan."""
    campaign = load_campaign(models_path, experiment_path)
    selection = select_models(
        campaign.models, labels, configs, single_node_only
    )
    loads = parse_concurrencies(concurrencies)
    repeat_count = positive_integer(repeats, "repeats")
    grid = build_expected_grid(selection.models, loads, repeat_count)
    matrix = [
        {
            "model_label": model.label,
            "model": model.name,
            "gpu_utilization": model.gpu_utilization,
            "config_label": config,
            "concurrencies": list(loads),
            "repeat_ids": list(range(1, repeat_count + 1)),
            "expected_count": len(loads) * repeat_count,
        }
        for model in selection.models
        for config in model.configs
    ]
    return {
        "schema_version": 2 if single_node_only else 1,
        "models_yaml": str(campaign.models_path),
        "experiment_yaml": str(campaign.experiment_path),
        "gpu_utilization": campaign.models[0].gpu_utilization,
        "largest_request_tokens": campaign.largest_request_tokens,
        "model_count": len(selection.models),
        "model_config_count": len(matrix),
        "selection": {
            "labels": [model.label for model in selection.models],
            "configs": sorted({config for model in selection.models for config in model.configs}),
            "excluded_labels": list(selection.excluded_labels),
        },
        "concurrencies": list(loads),
        "repeats": repeat_count,
        "expected_count": len(grid),
        "matrix": matrix,
    }


