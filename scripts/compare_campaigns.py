#!/usr/bin/env python3
"""Compare a campaign against a baseline campaign on their shared points.

Joins two index.csv files on (model_label, config_label, concurrency) and
reports, per shared model/config, the headline numbers side by side: low-load
decode latency (median ITL at the lowest shared concurrency), peak throughput,
and peak strict goodput, with the new campaign's replicate 95% CI where it has
replicates. A provenance table lists what differs between the campaigns
(image, vLLM version, methodology, NCCL transport and interfaces read from the
serving policies), because a ratio between campaigns measures *all* of those
differences at once, not only the transport.

    python3 scripts/compare_campaigns.py \
        --new results/benchmark-2026-10-22 \
        --baseline baselines/pmbs26-tcp/index.csv
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.campaign_output import resolve_campaign_output_path, resolve_index_path

KEYS = ["model_label", "config_label", "concurrency"]
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447}
METRICS = ("tok_thru_tps", "goodput_rps", "itl_p50", "mean_itl_p50",
           "ttft_p50", "e2e_p50", "energy_per_token_j")


def load(path: str) -> pd.DataFrame:
    frame = pd.read_csv(path)
    missing = [key for key in KEYS if key not in frame.columns]
    if missing:
        sys.exit(f"{path} lacks key column(s): {', '.join(missing)}")
    frame = frame[frame.get("mode", "closed") == "closed"].copy()
    for column in METRICS:
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def transport(index_path: str, frame: pd.DataFrame) -> str:
    """Summarize NCCL transport and interfaces from the serving policies."""
    root = Path(index_path).resolve().parent
    seen = set()
    for name in frame.get("serving_policy_path", pd.Series(dtype=str)).dropna().unique():
        try:
            policy = json.loads((root / str(name)).read_text())
        except (OSError, ValueError):
            continue
        if policy.get("config") == "single":
            continue
        ifaces = len(str(policy.get("connectx_iface", "")).split(","))
        kind = "TCP sockets" if str(policy.get("nccl_ib_disable")) == "1" else "RDMA"
        seen.add(f"{kind}, {ifaces} interface(s)")
    return "; ".join(sorted(seen)) or "unknown"


def provenance(index_path: str, frame: pd.DataFrame) -> dict:
    def values(column):
        if column not in frame.columns:
            return "-"
        unique = sorted(str(v) for v in frame[column].dropna().unique())
        if len(unique) > 2:
            return f"{len(unique)} values"
        return ", ".join(unique) or "-"

    reps = frame.groupby(KEYS)["concurrency"].size()
    return {
        "index": index_path,
        "rows": str(len(frame)),
        "replicates per point": (f"{reps.min()}-{reps.max()}" if len(reps)
                                 and reps.min() != reps.max()
                                 else str(reps.max() if len(reps) else 0)),
        "methodology": values("methodology_version"),
        "vLLM": values("vllm_version"),
        "image": values("vllm_image"),
        "window (s)": values("configured_measure_s")
        if "configured_measure_s" in frame.columns else values("measure_s"),
        "NCCL transport": transport(index_path, frame),
    }


def point_stats(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for key, group in frame.groupby(KEYS):
        row = dict(zip(KEYS, key))
        row["n"] = len(group)
        for column in METRICS:
            if column not in group.columns:
                continue
            values = group[column].dropna()
            row[column] = float(values.mean()) if len(values) else math.nan
            if len(values) >= 2:
                row[f"{column}__ci"] = (T95.get(len(values) - 1, 1.96)
                                        * float(values.std(ddof=1))
                                        / math.sqrt(len(values)))
        rows.append(row)
    return pd.DataFrame(rows)


def fmt(value: float, spec: str, ci: float = math.nan) -> str:
    if value is None or not math.isfinite(value):
        return "-"
    text = format(value, spec)
    if ci is not None and math.isfinite(ci):
        text += " ± " + format(ci, spec)
    return text


def ratio(new: float, base: float) -> str:
    if not (math.isfinite(new) and math.isfinite(base)) or base == 0:
        return "-"
    return f"{new / base:.2f}x"


def summarize(new: pd.DataFrame, base: pd.DataFrame) -> list[dict]:
    rows = []
    shared = new.merge(base, on=KEYS, suffixes=("", "_base"))
    for (model, config), group in shared.groupby(["model_label", "config_label"]):
        group = group.sort_values("concurrency")
        low = group.iloc[0]
        peak_new = group.loc[group["tok_thru_tps"].idxmax()]
        peak_base = group.loc[group["tok_thru_tps_base"].idxmax()]
        good = "goodput_rps" in group.columns and group["goodput_rps"].notna().any()
        rows.append({
            "model": model, "config": config,
            "loads": len(group),
            "c_low": int(low["concurrency"]),
            "itl_new": low.get("itl_p50", math.nan) * 1e3,
            "itl_new_ci": low.get("itl_p50__ci", math.nan) * 1e3,
            "itl_base": low.get("itl_p50_base", math.nan) * 1e3,
            "peak_new": peak_new["tok_thru_tps"],
            "peak_new_ci": peak_new.get("tok_thru_tps__ci", math.nan),
            "peak_new_c": int(peak_new["concurrency"]),
            "peak_base": peak_base["tok_thru_tps_base"],
            "peak_base_c": int(peak_base["concurrency"]),
            "good_new": group["goodput_rps"].max() if good else math.nan,
            "good_base": group["goodput_rps_base"].max() if good else math.nan,
        })
    return rows


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--new", required=True, help="campaign index.csv")
    parser.add_argument("--baseline", required=True, help="baseline index.csv")
    parser.add_argument("--md", help="also write the report as markdown")
    args = parser.parse_args(argv)

    new_index = resolve_index_path(args.new)
    baseline_index = resolve_index_path(args.baseline)
    args.new = str(new_index)
    args.baseline = str(baseline_index)
    if args.md:
        args.md = str(resolve_campaign_output_path(args.md, new_index))

    new_raw, base_raw = load(args.new), load(args.baseline)
    rows = summarize(point_stats(new_raw), point_stats(base_raw))

    prov_new = provenance(args.new, new_raw)
    prov_base = provenance(args.baseline, base_raw)
    lines = ["# Campaign comparison", "",
             "| | new | baseline |", "|---|---|---|"]
    for field in prov_new:
        lines.append(f"| {field} | {prov_new[field]} | {prov_base[field]} |")
    lines += ["", "Ratios are new / baseline. ± is the new campaign's 95% "
              "Student-t half-width across replicates. A ratio reflects every "
              "provenance difference above, not only the transport.", ""]
    if not rows:
        lines.append("The campaigns share no (model, config, concurrency) point.")
    else:
        lines += [
            "| model | config | loads | ITL p50 @c_low (ms) new | base | ratio "
            "| peak tok/s new (c) | base (c) | ratio | max goodput new | base |",
            "|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
        ]
        for r in rows:
            lines.append(
                f"| {r['model']} | {r['config']} | {r['loads']} | "
                f"{fmt(r['itl_new'], '.1f', r['itl_new_ci'])} (c={r['c_low']}) | "
                f"{fmt(r['itl_base'], '.1f')} | {ratio(r['itl_new'], r['itl_base'])} | "
                f"{fmt(r['peak_new'], '.0f', r['peak_new_ci'])} ({r['peak_new_c']}) | "
                f"{fmt(r['peak_base'], '.0f')} ({r['peak_base_c']}) | "
                f"{ratio(r['peak_new'], r['peak_base'])} | "
                f"{fmt(r['good_new'], '.3f')} | {fmt(r['good_base'], '.3f')} |")
    text = "\n".join(lines) + "\n"
    print(text)
    if args.md:
        Path(args.md).parent.mkdir(parents=True, exist_ok=True)
        Path(args.md).write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
