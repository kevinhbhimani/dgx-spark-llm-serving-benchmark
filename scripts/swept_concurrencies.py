#!/usr/bin/env python3
"""Build a fingerprint-safe resume schedule for a model/config pair.

Used by scripts/run_models.sh --skip-done to avoid re-running finished work.
The CLI prints missing ``repeat:concurrency`` pairs in repeat-major order. An
empty line means the requested rectangle is complete.

A row only counts as done when:
  - model_label and config_label match,
  - mode is "closed" (sweeps are always closed; open-mode rows never count),
  - request counts are finite, nonnegative, and internally consistent,
  - methodology, canonical config, current source tree, client runtime, and
    serving policy exactly match,
  - it carries a live deployment fingerprint (which need not match the active
    one: see completed_repeats), and
  - its one-based repeat_id is within the requested repeat count.

Usage: python scripts/swept_concurrencies.py INDEX_CSV LABEL CONFIG CONFIG_YAML \
       SERVING_FINGERPRINT REPEATS CONCURRENCY [CONCURRENCY ...]
"""
from __future__ import annotations
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import math
import os
from pathlib import Path
import sys
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.run_benchmark import (METHODOLOGY_VERSION, _source_fingerprint,
                                 client_environment_fingerprint,
                                 config_file_fingerprint)
from bench.config import ExperimentConfig

# Mirrors bench.config.ExperimentConfig.measure_s; keep in sync.
DEFAULT_MEASURE_S = 60.0

REQUEST_COUNT_FIELDS = (
    "requests_started",
    "requests_completed",
    "requests_failed",
    "requests_completed_in_window",
    "requests_started_and_completed_in_window",
)


def _positive_integral(value: object, field: str) -> int:
    """Parse a finite, positive integer without silently truncating floats."""
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite positive integer")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a finite positive integer") from exc
    if (not number.is_finite() or number < 1
            or number != number.to_integral_value()):
        raise ValueError(f"{field} must be a finite positive integer")
    return int(number)


def _positive_integral_list(values, field: str) -> tuple[int, ...]:
    parsed = tuple(_positive_integral(value, field) for value in values)
    if not parsed:
        raise ValueError(f"at least one {field} is required")
    if len(set(parsed)) != len(parsed):
        raise ValueError(f"{field} values must not contain duplicates")
    return parsed


def _row_expected_concurrencies(row: dict[str, str]) -> tuple[int, ...]:
    raw = str(row.get("expected_concurrencies") or "").split()
    return _positive_integral_list(raw, "expected concurrency")


def _validate_request_counts(row: dict[str, str]) -> None:
    """Require finite, nonnegative integral request counts."""
    for field in REQUEST_COUNT_FIELDS:
        if field != "requests_completed" and field not in row:
            continue
        try:
            value = Decimal(str(row.get(field, "")).strip())
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(
                f"{field} must be a finite nonnegative integer"
            ) from exc
        if (not value.is_finite() or value < 0
                or value != value.to_integral_value()):
            raise ValueError(
                f"{field} must be a finite nonnegative integer"
            )


def swept(index_csv: str, label: str, config: str, config_yaml: str) -> list[int]:
    """Legacy concurrency-only query retained for API compatibility/tests."""
    if not os.path.exists(index_csv):
        return []
    with open(config_yaml) as f:
        raw = yaml.safe_load(f) or {}
    want_measure = float(raw.get("measure_s", DEFAULT_MEASURE_S))
    tol = max(1.0, 0.01 * want_measure)

    done: set[int] = set()
    with open(index_csv, newline="") as f:
        for row in csv.DictReader(f):
            if (row.get("model_label") != label
                    or row.get("config_label") != config
                    or row.get("mode") != "closed"):
                continue
            try:
                _validate_request_counts(row)
                concurrency = _positive_integral(
                    row.get("concurrency"), "concurrency")
                measure_s = float(row.get("measure_s", ""))
            except (TypeError, ValueError):
                continue  # malformed row never counts as done
            if (not math.isfinite(measure_s)
                    or abs(measure_s - want_measure) > tol):
                continue
            done.add(concurrency)
    return sorted(done)


def completed_repeats(index_csv: str, label: str, config: str,
                      config_yaml: str, serving_fingerprint: str,
                      repeats: int,
                      config_overrides: dict | None = None,
                      expected_concurrencies=None) -> dict[int, set[int]]:
    """Return valid completed repeat ids keyed by concurrency.

    Rows must carry a live deployment fingerprint, but it is not required to
    equal any other row's or the active server's. Launch policy is pinned by
    serving_fingerprint; the deployment fingerprint additionally hashes the KV
    capacity vLLM settles on after memory profiling, and that lands a few
    thousand blocks apart on every launch. Requiring equality here made a
    repeat that runs on its own server launch permanently unschedulable, which
    is precisely how run-to-run variance has to be collected. The value stays
    recorded per row so a reader can still see which launch produced a point.
    """
    repeats = _positive_integral(repeats, "repeats")
    if not os.path.exists(index_csv):
        return {}
    campaign_cfg = ExperimentConfig.from_yaml(config_yaml)
    for key, value in (config_overrides or {}).items():
        setattr(campaign_cfg, key, value)
    campaign_cfg.validate()
    want_config = config_file_fingerprint(config_yaml, config_overrides)
    want_expected = None
    if expected_concurrencies is not None:
        want_expected = _positive_integral_list(expected_concurrencies, "expected concurrency")
    want_source = _source_fingerprint()
    want_client = client_environment_fingerprint()
    candidates: list[tuple[int, int]] = []
    identities: set[tuple[int, int]] = set()
    with open(index_csv, newline="") as f:
        for row in csv.DictReader(f):
            if (row.get("model_label") != label
                    or row.get("config_label") != config
                    or row.get("mode") != "closed"):
                continue

            current_identity = (
                row.get("methodology_version") == METHODOLOGY_VERSION
                and row.get("config_file_sha256") == want_config
                and row.get("harness_source_sha256") == want_source
                and row.get("client_environment_sha256") == want_client
                and row.get("serving_fingerprint") == serving_fingerprint
                and bool(row.get("experiment_fingerprint"))
            )
            if not current_identity:
                raise ValueError(
                    "index already contains an incompatible row for "
                    f"{label}/{config}; use a fresh OUT_DIR before launching "
                    "with changed config, source, environment, or serving policy"
                )

            if want_expected is not None:
                declared = _row_expected_concurrencies(row)
                if set(declared) != set(want_expected):
                    raise ValueError(
                        "otherwise-current row declares expected concurrencies "
                        f"{declared}, but this campaign requires {want_expected}"
                    )
            if not (row.get("deployment_fingerprint") or ""):
                raise ValueError(
                    "otherwise-current row has no live deployment fingerprint"
                )

            repeat_id = _positive_integral(row.get("repeat_id"), "repeat_id")
            if row.get("repeat_count") not in (None, ""):
                repeat_count = _positive_integral(
                    row.get("repeat_count"), "repeat_count"
                )
                if repeat_id > repeat_count:
                    raise ValueError(
                        "repeat_id cannot exceed the row's repeat_count"
                    )
            concurrency = _positive_integral(
                row.get("concurrency"), "concurrency")
            _validate_request_counts(row)
            started_count = int(Decimal(row.get("requests_started", "0")))
            completed_count = int(Decimal(row.get("requests_completed", "0")))
            failed_count = int(Decimal(row.get("requests_failed", "0")))
            if started_count != completed_count + failed_count:
                raise ValueError(
                    "otherwise-current row has inconsistent request accounting"
                )
            if campaign_cfg.require_zero_request_failures:
                if failed_count:
                    raise ValueError(
                        "otherwise-current row contains request failures and "
                        "cannot be resumed as a release point"
                    )
                if completed_count < campaign_cfg.min_successful_requests:
                    raise ValueError(
                        "otherwise-current row has fewer successful requests "
                        "than the release minimum"
                    )
                try:
                    total_tokens = _positive_integral(
                        row.get("total_output_tokens"), "total_output_tokens"
                    )
                except ValueError as exc:
                    raise ValueError(
                        "otherwise-current release row has no output tokens"
                    ) from exc

            # Current-schema production indexes carry durable artifact metadata.
            # Keep legacy unit/API fixtures (which omit the columns entirely)
            # readable, but fail closed when a declared artifact is absent,
            # resized, or its small summary file no longer matches its digest.
            if "raw_path" in row:
                root = Path(index_csv).resolve().parent
                for kind in ("raw", "summary"):
                    rel = row.get(f"{kind}_path") or ""
                    if not rel or Path(rel).name != rel:
                        raise ValueError(
                            f"otherwise-current row has invalid {kind} artifact"
                        )
                    artifact = root / rel
                    try:
                        expected_size = int(row.get(f"{kind}_bytes", ""))
                    except ValueError as exc:
                        raise ValueError(
                            f"otherwise-current row has invalid {kind}_bytes"
                        ) from exc
                    if not artifact.is_file() or artifact.stat().st_size != expected_size:
                        raise ValueError(
                            f"otherwise-current row's {kind} artifact is "
                            "missing or has changed size"
                        )
                    if kind == "summary":
                        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
                        if digest != row.get("summary_sha256"):
                            raise ValueError(
                                "otherwise-current row's summary artifact "
                                "digest does not match the index"
                            )

            identity = (concurrency, repeat_id)
            if identity in identities:
                raise ValueError(
                    "duplicate otherwise-current resume identity "
                    f"concurrency={concurrency}, repeat_id={repeat_id}"
                )
            identities.add(identity)
            if repeat_id <= repeats:
                candidates.append(identity)

    done: dict[int, set[int]] = {}
    for concurrency, repeat_id in candidates:
        done.setdefault(concurrency, set()).add(repeat_id)
    return done


def missing_schedule(index_csv: str, label: str, config: str,
                     config_yaml: str, serving_fingerprint: str,
                     repeats: int, concurrencies: list[int],
                     config_overrides: dict | None = None,
                     expected_concurrencies=None) -> list[tuple[int, int]]:
    repeats = _positive_integral(repeats, "repeats")
    concurrencies = list(_positive_integral_list(concurrencies, "concurrency"))
    if expected_concurrencies is None:
        expected_concurrencies = concurrencies
    else:
        expected_concurrencies = _positive_integral_list(expected_concurrencies, "expected concurrency")
    completed = completed_repeats(index_csv, label, config, config_yaml,
                                  serving_fingerprint, repeats,
                                  config_overrides=config_overrides,
                                  expected_concurrencies=expected_concurrencies)
    return [(rep, concurrency)
            for rep in range(1, repeats + 1)
            for concurrency in concurrencies
            if rep not in completed.get(concurrency, set())]


def main() -> int:
    if len(sys.argv) < 8:
        sys.stderr.write(
            "usage: swept_concurrencies.py INDEX_CSV LABEL CONFIG CONFIG_YAML "
            "SERVING_FINGERPRINT REPEATS CONCURRENCY [CONCURRENCY ...]\n")
        return 2
    index, label, config, config_yaml, fingerprint = sys.argv[1:6]
    try:
        repeats = int(sys.argv[6])
        concurrencies = [int(value) for value in sys.argv[7:]]
        config_overrides = {}
        if os.environ.get("POWER_BACKEND"):
            config_overrides["power_backend"] = os.environ["POWER_BACKEND"]
        expected_raw = os.environ.get("EXPECTED_CONCURRENCIES", "")
        expected_concurrencies = expected_raw.split() if expected_raw else concurrencies
        schedule = missing_schedule(
            index, label, config, config_yaml, fingerprint, repeats, concurrencies,
            config_overrides=config_overrides,
            expected_concurrencies=expected_concurrencies,
        )
    except (RuntimeError, TypeError, ValueError) as exc:
        sys.stderr.write(f"swept_concurrencies.py: {exc}\n")
        return 2
    if schedule:
        print(" ".join(f"{rep}:{concurrency}"
                       for rep, concurrency in schedule))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
