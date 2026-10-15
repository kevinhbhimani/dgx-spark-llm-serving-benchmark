#!/usr/bin/env python3
"""Emit the models.yaml entries that request a given serving config.

Used by scripts/run_models.sh to iterate models in the shell. Prints one
pipe-delimited line per selected model:

    label|name|revision|max_model_len|gpu_util|quant|tokenizer|
    tokenizer_revision|trust_remote_code|max_num_seqs

The trust_remote_code field is emitted as "1" when truthy, else empty.
gpu_util may be a scalar (applies to every config) or a mapping keyed by
config, e.g. {single: 0.90, tp2: 0.85}; a config absent from the mapping
emits empty and falls back to launch_server.sh's per-config default.

Empty fields are printed as empty strings, so the caller can decide defaults.

Usage:  python scripts/models_for_config.py configs/models.yaml single
"""
from __future__ import annotations
import sys
import yaml


def main() -> int:
    if len(sys.argv) != 3:
        sys.stderr.write("usage: models_for_config.py MODELS_YAML CONFIG\n")
        return 2
    path, want = sys.argv[1], sys.argv[2]
    with open(path) as f:
        doc = yaml.safe_load(f) or {}
    models = doc.get("models", [])
    if not isinstance(models, list):
        sys.stderr.write(f"{path}: 'models' must be a list\n")
        return 2

    seen_labels = set()
    n = 0
    for m in models:
        if not isinstance(m, dict):
            sys.stderr.write(f"{path}: each model entry must be a mapping "
                             f"(offending entry: {m!r})\n")
            return 2
        configs = m.get("configs", [])
        if not isinstance(configs, list):
            sys.stderr.write(f"{path}: 'configs' must be a list for model "
                             f"{m.get('label') or m.get('name') or m!r}\n")
            return 2
        if want not in configs:
            continue
        label = str(m.get("label") or "").strip()
        name = str(m.get("name") or "").strip()
        if not label or not name:
            sys.stderr.write(f"{path}: each model needs 'name' and 'label' "
                             f"(offending entry: {m!r})\n")
            return 2
        if label in seen_labels:
            sys.stderr.write(f"{path}: duplicate label '{label}'\n")
            return 2
        seen_labels.add(label)
        gpu_util = m.get("gpu_util", "")
        if isinstance(gpu_util, dict):
            gpu_util = gpu_util.get(want, "")
        # a pipe in any field would corrupt the shell split; reject early
        fields = [label, name,
                  str(m.get("revision", "") or ""),
                  str(m.get("max_model_len", "") or ""),
                  str(gpu_util or ""),
                  str(m.get("quant", "") or ""),
                  str(m.get("tokenizer", "") or ""),
                  str(m.get("tokenizer_revision", "") or ""),
                  "1" if m.get("trust_remote_code") else "",
                  str(m.get("max_num_seqs", ""))]
        if any("|" in f for f in fields):
            sys.stderr.write(f"{path}: '|' is not allowed in model fields\n")
            return 2
        print("|".join(fields))
        n += 1

    if n == 0:
        sys.stderr.write(f"{path}: no models request config '{want}'\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
