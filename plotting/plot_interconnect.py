#!/usr/bin/env python3
"""Two-node interconnect figure: latency and bandwidth by transport and primitive.

Reads the interconnect_extra_*.csv files written by
scripts/interconnect_extra_bench.sh (columns: collective, transport, bytes,
avg_s, ...). Each row's transport is what NCCL actually used, read from its own
INFO log, not what the environment requested. Pass several result directories
to overlay transports, for example the RDMA campaign and the TCP ablation:

    python plotting/plot_interconnect.py results/rdma-v4-paper-2026-10-22 \
        results/tcp-ablation-2026-10-22

Hue is the transport and linestyle/marker the primitive (all-reduce is TP's
per-layer collective, send/recv is PP's stage hand-off), so neither dimension
relies on color alone. The shaded band is min-max across repeated runs.
"""
from __future__ import annotations
import argparse
import glob
import os
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter, NullFormatter

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from campaign_output import (
    campaign_root_from_input,
    resolve_campaign_output_path,
    resolve_campaign_results_dir,
)
from plot_results import COL_W, save, set_rc

# Okabe-Ito pair (CVD-safe); same hues as the PMBS26 interconnect figure.
TRANSPORT_STYLE = {
    "tcp":  dict(color="#0072B2", label="TCP sockets"),
    "roce": dict(color="#D55E00", label="RoCE / RDMA"),
}
COLLECTIVE_STYLE = {
    "allreduce": dict(linestyle="-", marker="o", label="all-reduce (TP)"),
    "sendrecv":  dict(linestyle="--", marker="s", label="send/recv (PP)"),
}
# ConnectX-7 on DGX Spark: 200 Gb/s per node.
LINE_RATE_GBPS = 25.0
REQUIRED = {"collective", "transport", "bytes", "avg_s"}


def _bytes_fmt(value, _):
    for unit, div in (("GiB", 2 ** 30), ("MiB", 2 ** 20), ("KiB", 2 ** 10)):
        if value >= div:
            return f"{value / div:g} {unit}"
    return f"{int(value)} B"


def _style(transport: str) -> dict:
    return TRANSPORT_STYLE.get(transport, dict(color="0.45", label=str(transport)))


def load(dirs: list[str]) -> pd.DataFrame:
    frames = []
    for directory in dirs:
        for path in sorted(glob.glob(os.path.join(directory, "interconnect_extra_*.csv"))):
            frame = pd.read_csv(path)
            if not REQUIRED.issubset(frame.columns):
                sys.exit(f"{path} lacks columns {sorted(REQUIRED - set(frame.columns))}")
            frames.append(frame)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["bytes"] = pd.to_numeric(df["bytes"], errors="coerce")
    df["avg_s"] = pd.to_numeric(df["avg_s"], errors="coerce")
    if df[["bytes", "avg_s"]].isna().any().any() or (df[["bytes", "avg_s"]] <= 0).any().any():
        sys.exit("interconnect CSVs contain nonpositive or nonnumeric bytes/avg_s")
    # Bandwidth from unrounded latency: the CSV's 3-decimal busbw column rounds
    # every small message to zero. Two ranks, so busbw equals algbw.
    df["bw"] = df["bytes"] / df["avg_s"] / 1e9
    return df


def fig_interconnect(df: pd.DataFrame, out: str, fmts: list[str]) -> None:
    agg = (df.groupby(["collective", "transport", "bytes"], as_index=False)
             .agg(avg_s=("avg_s", "mean"), avg_lo=("avg_s", "min"),
                  avg_hi=("avg_s", "max"), bw=("bw", "mean"),
                  bw_lo=("bw", "min"), bw_hi=("bw", "max"), runs=("bw", "size")))
    transports = sorted(agg["transport"].unique(), key=lambda t: (t != "roce", t))
    collectives = [c for c in COLLECTIVE_STYLE if c in set(agg["collective"])]

    fig, (axt, axb) = plt.subplots(2, 1, figsize=(COL_W, 3.4), sharex=True)
    for transport in transports:
        color = _style(transport)["color"]
        for coll in collectives:
            sub = agg[(agg["transport"] == transport)
                      & (agg["collective"] == coll)].sort_values("bytes")
            if sub.empty:
                continue
            line = dict(color=color, linestyle=COLLECTIVE_STYLE[coll]["linestyle"],
                        marker=COLLECTIVE_STYLE[coll]["marker"], markersize=2.6,
                        linewidth=1.1)
            axt.plot(sub["bytes"], sub["avg_s"] * 1e6, **line)
            axt.fill_between(sub["bytes"], sub["avg_lo"] * 1e6, sub["avg_hi"] * 1e6,
                             color=color, alpha=0.15, linewidth=0)
            axb.plot(sub["bytes"], sub["bw"], **line)
            axb.fill_between(sub["bytes"], sub["bw_lo"], sub["bw_hi"],
                             color=color, alpha=0.15, linewidth=0)
    axt.set_yscale("log")
    axt.set_ylabel("Latency (µs)", fontsize=8)
    axb.set_yscale("log")
    # Keep the line-rate reference in view even when every transport is far
    # below it (TCP plateaus near 2-3 GB/s).
    axb.set_ylim(1e-3, max(float(agg["bw_hi"].max()), LINE_RATE_GBPS) * 2)
    axb.set_ylabel("Bus bandwidth (GB/s)", fontsize=8)
    axb.set_xlabel("Message size", fontsize=8)
    axb.axhline(LINE_RATE_GBPS, color="0.35", linestyle=":", linewidth=0.9)
    axb.annotate(f"200 Gb/s line rate ({LINE_RATE_GBPS:.0f} GB/s)",
                 xy=(float(agg["bytes"].min()) * 2, LINE_RATE_GBPS),
                 xytext=(0, -8), textcoords="offset points",
                 fontsize=6.0, color="0.25", va="top")
    axb.set_xscale("log", base=2)
    ticks = [2 ** k for k in (3, 7, 11, 15, 19, 23, 27, 30)]
    axb.set_xticks([t for t in ticks if agg["bytes"].min() <= t <= agg["bytes"].max()])
    axb.xaxis.set_major_formatter(FuncFormatter(_bytes_fmt))
    axb.xaxis.set_minor_formatter(NullFormatter())
    for ax in (axt, axb):
        ax.tick_params(labelsize=6.5)
    plt.setp(axb.get_xticklabels(), rotation=30, ha="right")
    handles = [Line2D([], [], color=_style(t)["color"], label=_style(t)["label"])
               for t in transports]
    handles += [Line2D([], [], color="0.35", linestyle=COLLECTIVE_STYLE[c]["linestyle"],
                       marker=COLLECTIVE_STYLE[c]["marker"], markersize=3,
                       label=COLLECTIVE_STYLE[c]["label"]) for c in collectives]
    axt.legend(handles=handles, fontsize=5.8, ncol=2, frameon=False, loc="upper left")
    save(fig, "interconnect", out, fmts)

    # Plateau and floor per transport/primitive, for the text.
    rows = []
    for (coll, transport), g in agg.groupby(["collective", "transport"]):
        g = g.sort_values("bytes")
        rows.append(f"| {coll} | {_style(transport)['label']} | {int(g['runs'].max())} | "
                    f"{g.iloc[0]['avg_s'] * 1e6:.1f} | {g.iloc[-1]['bw']:.2f} |")
    table = ["| primitive | transport | runs | latency @ smallest (µs) | bandwidth @ largest (GB/s) |",
             "|---|---|--:|--:|--:|"] + rows
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "interconnect_table.md"), "w") as handle:
        handle.write("\n".join(table) + "\n")
    print(f"  wrote {os.path.join(out, 'interconnect_table.md')}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dirs", nargs="+", help="result directories holding interconnect_extra_*.csv")
    ap.add_argument(
        "--out", help="output directory (default: first campaign/figs/stats)"
    )
    ap.add_argument("--format", default="both", choices=["pdf", "png", "both"])
    ap.add_argument("--serif", action="store_true")
    args = ap.parse_args()
    campaign_roots = [campaign_root_from_input(directory) for directory in args.dirs]
    args.dirs = [str(resolve_campaign_results_dir(directory))
                 for directory in campaign_roots]
    args.out = str(resolve_campaign_output_path(
        args.out, args.dirs[0], default_subdir="figs/stats"
    ))
    set_rc(200, args.serif)
    df = load(args.dirs)
    if df.empty:
        print("no interconnect_extra_*.csv found; run scripts/interconnect_extra_bench.sh")
        return 0
    fig_interconnect(df, args.out, ["pdf", "png"] if args.format == "both" else [args.format])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
