#!/usr/bin/env python3
"""Generate the PMBS26 paper figures from results/index.csv.

Reads the benchmark sweep index, groups rows by configuration label (single,
tp2, pp2), and writes vector PDF figures plus PNG previews. Figures are sized
for the IEEE two column format and saved as PDF for direct \\includegraphics.
The script handles one configuration or several, sorts each series by load,
coerces empty cells to missing, and skips any figure whose columns are absent.

Usage:
    python plot_results.py --index results/index.csv --out figs
    python plot_results.py                       # uses the defaults above
    python plot_results.py --format png          # png only, for quick viewing

Dependencies:  pip install pandas matplotlib numpy
"""
from __future__ import annotations
import argparse
import os
import sys
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")                      # headless, safe over ssh
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, NullFormatter


# ---- configuration appearance ------------------------------------------------
CONFIG_ORDER = ["single", "tp2", "pp2"]
CONFIG_STYLE = {
    "single": dict(label="Single Spark",      color="#1f77b4", marker="o"),
    "tp2":    dict(label="Dual Spark, TP=2",  color="#d62728", marker="s"),
    "pp2":    dict(label="Dual Spark, PP=2",  color="#2ca02c", marker="^"),
}
_FALLBACK_COLORS = ["#9467bd", "#8c564b", "#e377c2", "#17becf", "#bcbd22"]
_FALLBACK_MARKERS = ["D", "v", "P", "X", "*"]

COL_W = 3.5      # one IEEE column, inches
TEXT_W = 7.0     # full text width, inches
PANEL_H = 2.7


def set_rc(dpi: int) -> None:
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
    })
    # To match an IEEE paper's Times-like body text, uncomment:
    # plt.rcParams["font.family"] = "serif"


# ---- data helpers ------------------------------------------------------------
NUMERIC_COLS = [
    "concurrency", "target_qps", "measure_s",
    "requests_started", "requests_completed", "requests_failed",
    "total_output_tokens", "req_thru_rps", "tok_thru_tps", "goodput_rps",
    "ttft_p50", "ttft_p99", "itl_p50", "itl_p99", "mean_itl_p50",
    "e2e_p50", "e2e_p99",
    "kv_cache_usage_mean", "kv_cache_usage_max",
    "requests_running_mean", "requests_running_max",
    "requests_waiting_mean", "requests_waiting_max",
    "mean_power_w", "energy_per_token_j", "tokens_per_watt",
]


def load_index(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        sys.exit(f"index not found: {path}")
    try:
        df = pd.read_csv(path)
    except pd.errors.ParserError as e:
        sys.exit(
            f"could not parse {path}: {e}\n"
            "Check for a concatenated row from an older run whose final line "
            "was missing a newline, or write new results to a fresh OUT_DIR."
        )
    df.columns = [c.strip() for c in df.columns]
    df = df.loc[:, ~df.columns.str.startswith("Unnamed")]   # drop trailing blanks
    for c in NUMERIC_COLS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    if "config_label" not in df.columns:
        sys.exit("index is missing config_label; cannot group")
    return df


def configs_present(df: pd.DataFrame) -> list[str]:
    have = list(dict.fromkeys(df["config_label"].dropna().tolist()))
    ordered = [c for c in CONFIG_ORDER if c in have]
    extras = [c for c in have if c not in CONFIG_ORDER]
    return ordered + extras


def style_for(cfg: str, i: int) -> dict:
    if cfg in CONFIG_STYLE:
        return CONFIG_STYLE[cfg]
    return dict(label=cfg,
                color=_FALLBACK_COLORS[i % len(_FALLBACK_COLORS)],
                marker=_FALLBACK_MARKERS[i % len(_FALLBACK_MARKERS)])


def group(df: pd.DataFrame, cfg: str):
    """Return (sorted_subframe, load_column, mode) for one configuration."""
    g = df[df["config_label"] == cfg].copy()
    mode = str(g["mode"].iloc[0]) if "mode" in g.columns and len(g) else "closed"
    load_col = "concurrency" if mode == "closed" else "target_qps"
    if load_col not in g.columns:
        load_col = "concurrency" if "concurrency" in g.columns else g.columns[0]
    return g.sort_values(load_col), load_col, mode


def has(df: pd.DataFrame, cols: list[str]) -> bool:
    return all(c in df.columns and df[c].notna().any() for c in cols)


def set_load_axis(ax, loads, mode: str) -> None:
    loads = sorted({float(v) for v in loads if pd.notna(v)})
    is_pow2 = bool(loads) and all(
        v >= 1 and float(v).is_integer() and (int(v) & (int(v) - 1)) == 0
        for v in loads
    )
    if mode == "closed" and is_pow2:
        ax.set_xscale("log", base=2)
        ax.set_xticks(loads)
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{int(v)}"))
        ax.xaxis.set_minor_formatter(NullFormatter())
        ax.set_xlabel("Concurrent requests")
    else:
        ax.set_xlabel("Concurrent requests" if mode == "closed"
                      else "Offered load (req/s)")


def save(fig, name: str, out: str, fmts: list[str]) -> None:
    os.makedirs(out, exist_ok=True)
    for fmt in fmts:
        path = os.path.join(out, f"{name}.{fmt}")
        fig.savefig(path)
        print(f"  wrote {path}")
    plt.close(fig)


# ---- figures -----------------------------------------------------------------
def fig_throughput_scaling(df, out, fmts):
    if not has(df, ["tok_thru_tps"]):
        return
    fig, ax = plt.subplots(figsize=(COL_W, PANEL_H))
    all_loads = []
    for i, cfg in enumerate(configs_present(df)):
        g, lc, mode = group(df, cfg)
        s = style_for(cfg, i)
        ax.plot(g[lc], g["tok_thru_tps"], marker=s["marker"], color=s["color"],
                label=s["label"])
        all_loads += g[lc].tolist()
    set_load_axis(ax, all_loads, mode)
    ax.set_ylabel("Output token throughput (tok/s)")
    ax.set_ylim(bottom=0)
    ax.legend()
    save(fig, "throughput_scaling", out, fmts)


def fig_latency_throughput(df, out, fmts):
    if not has(df, ["tok_thru_tps", "ttft_p50", "itl_p50"]):
        return
    fig, (axl, axr) = plt.subplots(1, 2, figsize=(TEXT_W, PANEL_H))
    for i, cfg in enumerate(configs_present(df)):
        g, lc, _ = group(df, cfg)
        s = style_for(cfg, i)
        axl.plot(g["tok_thru_tps"], g["ttft_p50"] * 1e3, marker=s["marker"],
                 color=s["color"], label=s["label"])
        axr.plot(g["tok_thru_tps"], g["itl_p50"] * 1e3, marker=s["marker"],
                 color=s["color"], label=s["label"])
        # annotate each point with its load so the swept parameter is visible
        for x, y, c in zip(g["tok_thru_tps"], g["ttft_p50"] * 1e3, g[lc]):
            if pd.notna(x) and pd.notna(y):
                axl.annotate(f"{int(c)}", (x, y), textcoords="offset points",
                             xytext=(3, 3), fontsize=6, color=s["color"])
    axl.set_xlabel("Output token throughput (tok/s)")
    axl.set_ylabel("Median TTFT (ms)")
    axl.set_ylim(bottom=0)
    axr.set_xlabel("Output token throughput (tok/s)")
    axr.set_ylabel("Median inter token latency (ms)")
    axr.set_ylim(bottom=0)
    axl.legend()
    save(fig, "latency_throughput", out, fmts)


def fig_tail_latency(df, out, fmts):
    if not has(df, ["ttft_p50", "ttft_p99", "itl_p50", "itl_p99"]):
        return
    fig, (axl, axr) = plt.subplots(1, 2, figsize=(TEXT_W, PANEL_H))
    all_loads = []
    for i, cfg in enumerate(configs_present(df)):
        g, lc, mode = group(df, cfg)
        s = style_for(cfg, i)
        axl.plot(g[lc], g["ttft_p50"] * 1e3, marker=s["marker"],
                 color=s["color"], label=f"{s['label']} p50")
        axl.plot(g[lc], g["ttft_p99"] * 1e3, marker=s["marker"], linestyle="--",
                 color=s["color"], alpha=0.7, label=f"{s['label']} p99")
        axr.plot(g[lc], g["itl_p50"] * 1e3, marker=s["marker"],
                 color=s["color"], label=f"{s['label']} p50")
        axr.plot(g[lc], g["itl_p99"] * 1e3, marker=s["marker"], linestyle="--",
                 color=s["color"], alpha=0.7, label=f"{s['label']} p99")
        all_loads += g[lc].tolist()
    for ax, title in ((axl, "TTFT (ms)"), (axr, "Inter token latency (ms)")):
        set_load_axis(ax, all_loads, mode)
        ax.set_yscale("log")
        ax.set_ylabel(title)
    axl.legend(ncol=1)
    save(fig, "tail_latency", out, fmts)


def fig_goodput(df, out, fmts):
    if not has(df, ["goodput_rps"]):
        return
    fig, ax = plt.subplots(figsize=(COL_W, PANEL_H))
    all_loads = []
    for i, cfg in enumerate(configs_present(df)):
        g, lc, mode = group(df, cfg)
        s = style_for(cfg, i)
        ax.plot(g[lc], g["goodput_rps"], marker=s["marker"], color=s["color"],
                label=s["label"])
        if "req_thru_rps" in g.columns:
            ax.plot(g[lc], g["req_thru_rps"], marker=s["marker"], linestyle=":",
                    color=s["color"], alpha=0.5,
                    label=f"{s['label']} achieved")
        all_loads += g[lc].tolist()
    set_load_axis(ax, all_loads, mode)
    ax.set_ylabel("Goodput under SLO (req/s)")
    ax.set_ylim(bottom=0)
    ax.legend()
    save(fig, "goodput", out, fmts)


def fig_capacity(df, out, fmts):
    need_kv = has(df, ["kv_cache_usage_max"])
    need_q = has(df, ["requests_waiting_max"])
    if not (need_kv or need_q):
        return
    fig, (axl, axr) = plt.subplots(1, 2, figsize=(TEXT_W, PANEL_H))
    all_loads = []
    for i, cfg in enumerate(configs_present(df)):
        g, lc, mode = group(df, cfg)
        s = style_for(cfg, i)
        if need_kv:
            axl.plot(g[lc], g["kv_cache_usage_max"] * 100, marker=s["marker"],
                     color=s["color"], label=f"{s['label']} max")
            if "kv_cache_usage_mean" in g.columns:
                axl.plot(g[lc], g["kv_cache_usage_mean"] * 100, marker=s["marker"],
                         linestyle="--", color=s["color"], alpha=0.6,
                         label=f"{s['label']} mean")
        if need_q:
            axr.plot(g[lc], g["requests_waiting_max"], marker=s["marker"],
                     color=s["color"], label=f"{s['label']} max")
            if "requests_waiting_mean" in g.columns:
                axr.plot(g[lc], g["requests_waiting_mean"], marker=s["marker"],
                         linestyle="--", color=s["color"], alpha=0.6,
                         label=f"{s['label']} mean")
        all_loads += g[lc].tolist()
    set_load_axis(axl, all_loads, mode)
    set_load_axis(axr, all_loads, mode)
    axl.set_ylabel("KV cache occupancy (%)")
    axl.set_ylim(bottom=0)
    axr.set_ylabel("Requests waiting in queue")
    axr.set_ylim(bottom=0)
    axl.legend()
    save(fig, "capacity", out, fmts)


def fig_reliability(df, out, fmts):
    if not has(df, ["requests_completed", "requests_started"]):
        return
    fig, ax = plt.subplots(figsize=(COL_W, PANEL_H))
    all_loads = []
    for i, cfg in enumerate(configs_present(df)):
        g, lc, mode = group(df, cfg)
        s = style_for(cfg, i)
        frac = (g["requests_completed"] / g["requests_started"]).clip(0, 1) * 100
        ax.plot(g[lc], frac, marker=s["marker"], color=s["color"],
                label=s["label"])
        all_loads += g[lc].tolist()
    set_load_axis(ax, all_loads, mode)
    ax.set_ylabel("Requests completed in window (%)")
    ax.set_ylim(0, 105)
    ax.legend()
    save(fig, "reliability", out, fmts)


def fig_energy(df, out, fmts):
    if not has(df, ["energy_per_token_j"]) and not has(df, ["tokens_per_watt"]):
        print("  energy: no power data in index, skipping "
              "(rerun with POWER_BACKEND set)")
        return
    fig, (axl, axr) = plt.subplots(1, 2, figsize=(TEXT_W, PANEL_H))
    all_loads = []
    for i, cfg in enumerate(configs_present(df)):
        g, lc, mode = group(df, cfg)
        s = style_for(cfg, i)
        if "energy_per_token_j" in g.columns:
            axl.plot(g[lc], g["energy_per_token_j"], marker=s["marker"],
                     color=s["color"], label=s["label"])
        if "tokens_per_watt" in g.columns:
            axr.plot(g[lc], g["tokens_per_watt"], marker=s["marker"],
                     color=s["color"], label=s["label"])
        all_loads += g[lc].tolist()
    set_load_axis(axl, all_loads, mode)
    set_load_axis(axr, all_loads, mode)
    axl.set_ylabel("Energy per token (J)")
    axl.set_ylim(bottom=0)
    axr.set_ylabel("Tokens per watt (tok/s/W)")
    axr.set_ylim(bottom=0)
    axl.legend()
    save(fig, "energy", out, fmts)


# ---- main --------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Plot PMBS26 figures from index.csv")
    ap.add_argument("--index", default="results/index.csv")
    ap.add_argument("--out", default="figs")
    ap.add_argument("--format", default="both", choices=["pdf", "png", "both"])
    ap.add_argument("--dpi", type=int, default=200)
    args = ap.parse_args()

    fmts = ["pdf", "png"] if args.format == "both" else [args.format]
    set_rc(args.dpi)
    df = load_index(args.index)

    print(f"loaded {len(df)} rows, configs: {', '.join(configs_present(df))}")
    fig_throughput_scaling(df, args.out, fmts)
    fig_latency_throughput(df, args.out, fmts)
    fig_tail_latency(df, args.out, fmts)
    fig_goodput(df, args.out, fmts)
    fig_capacity(df, args.out, fmts)
    fig_reliability(df, args.out, fmts)
    fig_energy(df, args.out, fmts)
    print("done")


if __name__ == "__main__":
    main()
