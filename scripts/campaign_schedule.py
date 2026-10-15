#!/usr/bin/env python3
"""Emit the per-model measurement schedule for scripts/run_job.sh.

Entries of configs/models.yaml that share a model repository form one group:
the group is downloaded once, measured, and purged once. Groups keep the file
order. Within a group, replicate r runs the configs in a rotated order, so
over R replicates position counts differ by at most one. They are exactly
balanced when R is a multiple of the number of configs. This limits order
bias from slow drift (thermals, page cache, cluster state).

Output, one line per group:

    LABELS|NEEDS_DUAL|STEP;STEP;...

LABELS is space-separated, NEEDS_DUAL is 1 when any step is tp2/pp2 (the
group must be mirrored to the worker), and each STEP is
``replicate:config:label``.

Usage: campaign_schedule.py MODELS_YAML REPEATS [--labels "A B"] [--configs "single tp2"]
"""
from __future__ import annotations

import argparse
import sys

import yaml

CONFIG_ORDER = ("single", "tp2", "pp2")


def rotated(configs: list[str], replicate: int) -> list[str]:
    """Configs in canonical order, rotated left by replicate - 1."""
    ordered = [config for config in CONFIG_ORDER if config in configs]
    if not ordered:
        return []
    shift = (replicate - 1) % len(ordered)
    return ordered[shift:] + ordered[:shift]


def build_groups(models: list[dict], repeats: int, labels: set[str] | None = None,
                 configs: set[str] | None = None) -> list[dict]:
    groups: dict[str, dict] = {}
    for entry in models:
        label = entry["label"]
        if labels is not None and label not in labels:
            continue
        wanted = [c for c in entry.get("configs") or []
                  if configs is None or c in configs]
        if not wanted:
            continue
        group = groups.setdefault(entry["name"], {"labels": [], "configs": {}})
        group["labels"].append(label)
        group["configs"][label] = wanted

    result = []
    for group in groups.values():
        steps = []
        for replicate in range(1, repeats + 1):
            union = [c for c in CONFIG_ORDER
                     if any(c in cfgs for cfgs in group["configs"].values())]
            for config in rotated(union, replicate):
                for label in group["labels"]:
                    if config in group["configs"][label]:
                        steps.append((replicate, config, label))
        result.append({
            "labels": group["labels"],
            "needs_dual": any(config != "single" for _, config, _ in steps),
            "steps": steps,
        })
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("models_yaml")
    parser.add_argument("repeats", type=int)
    parser.add_argument("--labels", default="")
    parser.add_argument("--configs", default="")
    args = parser.parse_args(argv)
    if args.repeats < 1:
        print("campaign_schedule.py: repeats must be positive", file=sys.stderr)
        return 2
    with open(args.models_yaml) as handle:
        models = (yaml.safe_load(handle) or {}).get("models") or []
    known = {entry.get("label") for entry in models}
    labels = set(args.labels.split()) or None
    if labels and labels - known:
        print("campaign_schedule.py: unknown label(s): "
              + " ".join(sorted(labels - known)), file=sys.stderr)
        return 2
    configs = set(args.configs.split()) or None
    if configs and configs - set(CONFIG_ORDER):
        print("campaign_schedule.py: configs must be among single tp2 pp2",
              file=sys.stderr)
        return 2
    groups = build_groups(models, args.repeats, labels, configs)
    if not groups:
        print("campaign_schedule.py: the selection is empty", file=sys.stderr)
        return 2
    for group in groups:
        steps = ";".join(f"{r}:{c}:{label}" for r, c, label in group["steps"])
        print(f"{' '.join(group['labels'])}|{int(group['needs_dual'])}|{steps}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
