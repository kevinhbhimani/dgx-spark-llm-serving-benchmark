#!/usr/bin/env python3
"""Standalone interconnect microbenchmark plotter.

Reads interconnect_extra_*.csv files (produced by
scripts/interconnect_extra_bench.sh) and writes two figures:

  interconnect_bidi    – all-reduce (TP primitive) latency + bandwidth
  interconnect_single  – send/recv (PP primitive) latency + bandwidth

Each figure overlays every transport present (TCP, RoCE, tuned sockets) so the
bidirectional and unidirectional primitives can be read against the 200 GbE line
rate without visual interference.

Usage:
    python rdma-testing/plot_interconnect.py                           \
        --results rdma-testing/results --out figs/paper
"""

from __future__ import annotations

import argparse
import glob
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter, NullFormatter

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

COL_W = 3.5          # one IEEE column, inches
LINE_RATE_GBPS = 25.0  # 200 GbE aggregate, bits → bytes
SLO_COLOR = "0.35"

TRANSPORT_STYLE = {
    "tcp":   dict(color="#0072B2", label="TCP sockets"),
    "roce":  dict(color="#D55E00", label="RoCE / RDMA"),
}
_TCP_TUNED_COLOR = "#009E73"

COLLECTIVE_STYLE = {
    "allreduce": dict(marker="o", label="all-reduce (TP)"),
    "sendrecv":  dict(marker="s", label="send/recv (PP)"),
}

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _transport_style(transport: str) -> dict:
    if transport in TRANSPORT_STYLE:
        return TRANSPORT_STYLE[transport]
    if str(transport).startswith("tcp-tuned"):
        return dict(color=_TCP_TUNED_COLOR, label=str(transport))
    return dict(color="0.5", label=str(transport))


def _bytes_fmt(v, _):
    for unit, div in (("GiB", 2 ** 30), ("MiB", 2 ** 20), ("KiB", 2 ** 10)):
        if v >= div:
            return f"{v / div:g} {unit}"
    return f"{int(v)} B"


def _set_rc(dpi: int = 200) -> None:
    plt.rcParams.update({
        "savefig.dpi": dpi,
        "figure.dpi": 120,
        "font.size": 9,
        "axes.titlesize": 9,
        "axes.labelsize": 9,
        "legend.fontsize": 8,
        "legend.frameon": False,
        "xtick.labelsize": 8,
        "ytick.labelsize": 8,
        "axes.grid": True,
        "grid.alpha": 0.30,
        "grid.linewidth": 0.5,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "lines.linewidth": 1.6,
        "lines.markersize": 5,
        "figure.constrained_layout.use": True,
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "font.family": "serif",
        "font.serif": ["Times New Roman", "Liberation Serif", "DejaVu Serif"],
        "mathtext.fontset": "dejavuserif",
    })


def _save(fig, name: str, out: str, fmts: list[str]) -> None:
    os.makedirs(out, exist_ok=True)
    for fmt in fmts:
        path = os.path.join(out, f"{name}.{fmt}")
        fig.savefig(path)
        print(f"  wrote {path}")
    plt.close(fig)


# ---------------------------------------------------------------------------
# data loading
# ---------------------------------------------------------------------------


def _load_data(results_dir: str) -> tuple[pd.DataFrame | None, list[str] | None]:
    """Return (aggregated DataFrame, ordered transport list) or (None, None)."""
    paths = sorted(glob.glob(os.path.join(results_dir,
                                          "interconnect_extra_*.csv")))
    if not paths:
        print("No interconnect_extra_*.csv found — nothing to plot.")
        return None, None

    frames = []
    for p in paths:
        try:
            frames.append(pd.read_csv(p))
        except Exception as exc:
            print(f"  skipping unreadable {p}: {exc}")

    df = pd.concat(frames, ignore_index=True) if frames else None
    if df is None or df.empty:
        print("No readable data.")
        return None, None

    required = {"collective", "transport", "bytes", "avg_s"}
    if not required.issubset(df.columns):
        print(f"CSV columns {list(df.columns)} missing one of {required}")
        return None, None

    df["bw_exact"] = df["bytes"] / df["avg_s"] / 1e9
    agg = (df.groupby(["collective", "transport", "bytes"], as_index=False)
             .agg(avg_s=("avg_s", "mean"),
                  avg_lo=("avg_s", "min"),
                  avg_hi=("avg_s", "max"),
                  bw=("bw_exact", "mean"),
                  bw_lo=("bw_exact", "min"),
                  bw_hi=("bw_exact", "max")))

    transports = [t for t in ["tcp"] + sorted(
        x for x in agg["transport"].unique() if x != "tcp")]
    return agg, transports


# ---------------------------------------------------------------------------
# drawing
# ---------------------------------------------------------------------------


def _draw_one(agg: pd.DataFrame, transports: list[str], collective: str,
              out: str, fmts: list[str], tag: str) -> None:
    """Latency (top) + bandwidth (bottom) panels for a single collective."""
    sub_all = agg[agg["collective"] == collective]
    if sub_all.empty:
        print(f"  no {collective} data, skipping {tag}")
        return

    cs = COLLECTIVE_STYLE[collective]
    coll_label = cs["label"]

    fig, (axt, axb) = plt.subplots(2, 1, figsize=(COL_W, 3.4), sharex=True)

    for transport in transports:
        ts = _transport_style(transport)
        sub = sub_all[sub_all["transport"] == transport].sort_values("bytes")
        if sub.empty:
            continue
        axt.plot(sub["bytes"], sub["avg_s"] * 1e6, color=ts["color"],
                 marker=cs["marker"], markersize=2.6, linewidth=1.1)
        axt.fill_between(sub["bytes"], sub["avg_lo"] * 1e6,
                         sub["avg_hi"] * 1e6, color=ts["color"],
                         alpha=0.15, linewidth=0)
        axb.plot(sub["bytes"], sub["bw"], color=ts["color"],
                 marker=cs["marker"], markersize=2.6, linewidth=1.1)
        axb.fill_between(sub["bytes"], sub["bw_lo"], sub["bw_hi"],
                         color=ts["color"], alpha=0.15, linewidth=0)

    # -- latency panel --------------------------------------------------------
    axt.set_yscale("log")
    axt.set_ylabel(f"{coll_label} latency (µs)", fontsize=8)

    # -- bandwidth panel ------------------------------------------------------
    axb.set_yscale("log")
    axb.set_ylim(bottom=1e-3)
    axb.set_ylabel(f"{coll_label} bandwidth (GB/s)", fontsize=8)
    axb.set_xlabel("Message size", fontsize=8)

    # line-rate reference
    axb.axhline(LINE_RATE_GBPS, color=SLO_COLOR, linestyle=":", linewidth=0.9)
    peak_bw = float(sub_all["bw"].max())
    pct = peak_bw / LINE_RATE_GBPS * 100
    axb.annotate(
        f"{LINE_RATE_GBPS:.0f} GB/s line rate\n"
        f"(peak {peak_bw:.1f} GB/s = {pct:.0f}%)",
        xy=(float(sub_all["bytes"].max()), LINE_RATE_GBPS),
        xytext=(0, -8), textcoords="offset points",
        fontsize=5.8, color="0.25", va="top", ha="right",
    )

    # -- shared x-axis --------------------------------------------------------
    axb.set_xscale("log", base=2)
    ticks = [2 ** k for k in (3, 7, 11, 15, 19, 23, 27, 30)]
    axb.set_xticks([t for t in ticks
                    if sub_all["bytes"].min() <= t <= sub_all["bytes"].max()])
    axb.xaxis.set_major_formatter(FuncFormatter(_bytes_fmt))
    axb.xaxis.set_minor_formatter(NullFormatter())
    for ax in (axt, axb):
        ax.tick_params(labelsize=6.5)
    plt.setp(axb.get_xticklabels(), rotation=30, ha="right")

    # legend — transports only; collective is in the axis labels
    proxies = [Line2D([], [], color=_transport_style(t)["color"],
                      label=_transport_style(t)["label"]) for t in transports]
    axt.legend(handles=proxies, fontsize=6.5, ncol=len(transports),
               frameon=False, loc="upper left")

    _save(fig, tag, out, fmts)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Plot interconnect microbenchmark results")
    ap.add_argument("--results", default="rdma-testing/interconnect/results",
                    help="directory containing interconnect_extra_*.csv")
    ap.add_argument("--out", default="rdma-testing/interconnect/figs",
                    help="output directory for figures")
    ap.add_argument("--format", default="both",
                    choices=["pdf", "png", "both"])
    ap.add_argument("--dpi", type=int, default=200)
    args = ap.parse_args()

    fmts = ["pdf", "png"] if args.format == "both" else [args.format]
    _set_rc(args.dpi)

    agg, transports = _load_data(args.results)
    if agg is None:
        sys.exit(1)

    for collective, tag in [("allreduce", "interconnect_bidi"),
                            ("sendrecv", "interconnect_single")]:
        _draw_one(agg, transports, collective, args.out, fmts, tag)


if __name__ == "__main__":
    main()