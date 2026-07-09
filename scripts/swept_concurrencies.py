#!/usr/bin/env python3
"""Print the concurrency points already swept for a model/config pair.

Used by scripts/run_models.sh --skip-done to avoid re-running finished work.
Reads OUT_DIR/index.csv and prints, space-separated on one line, the closed-mode
concurrencies already recorded for (model_label, config_label). Prints nothing
(and exits 0) when the index does not exist or has no matching rows.

A row only counts as done when:
  - model_label and config_label match,
  - mode is "closed" (sweeps are always closed; open-mode rows never count),
  - requests_completed > 0 (a row from hammering a dead server is not done),
  - measure_s matches the current experiment YAML within 1s or 1%, so rows
    from an older, shorter measurement window (see the 60s windowing artifact)
    do not mask a re-sweep at the current window.

Usage:  python scripts/swept_concurrencies.py INDEX_CSV LABEL CONFIG CONFIG_YAML
"""
from __future__ import annotations
import csv
import os
import sys
import yaml

# Mirrors bench.config.ExperimentConfig.measure_s; keep in sync.
DEFAULT_MEASURE_S = 60.0


def swept(index_csv: str, label: str, config: str, config_yaml: str) -> list[int]:
    if not os.path.exists(index_csv):
        return []
    with open(config_yaml) as f:
        raw = yaml.safe_load(f) or {}
    want_measure = float(raw.get("measure_s", DEFAULT_MEASURE_S))
    tol = max(1.0, 0.01 * want_measure)

    done: set[int] = set()
    with open(index_csv, newline="") as f:
        for row in csv.DictReader(f):
            try:
                if (row.get("model_label") != label
                        or row.get("config_label") != config
                        or row.get("mode") != "closed"
                        or float(row.get("requests_completed") or 0) <= 0
                        or abs(float(row.get("measure_s") or 0) - want_measure) > tol):
                    continue
                done.add(int(float(row["concurrency"])))
            except (TypeError, ValueError):
                continue  # malformed row never counts as done
    return sorted(done)


def main() -> int:
    if len(sys.argv) != 5:
        sys.stderr.write(
            "usage: swept_concurrencies.py INDEX_CSV LABEL CONFIG CONFIG_YAML\n")
        return 2
    concs = swept(*sys.argv[1:5])
    if concs:
        print(" ".join(str(c) for c in concs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
