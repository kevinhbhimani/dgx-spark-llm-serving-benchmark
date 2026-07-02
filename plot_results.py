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
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter, NullFormatter


# Below this many completions in the measurement window, percentiles are small
# sample noise rather than a real reading (see fig_* functions and warn_low_n).
MIN_SAMPLES = 20

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
    """Return (sorted_subframe, load_column, mode) for one configuration.

    Assumes df has already been deduplicated by dedupe_index().
    """
    g = df[df["config_label"] == cfg].copy()
    mode = str(g["mode"].iloc[0]) if "mode" in g.columns and len(g) else "closed"
    load_col = "concurrency" if mode == "closed" else "target_qps"
    if load_col not in g.columns:
        load_col = "concurrency" if "concurrency" in g.columns else g.columns[0]
    return g.sort_values(load_col), load_col, mode


def dedupe_index(df: pd.DataFrame) -> pd.DataFrame:
    """Drop duplicate (config, load) rows, keeping the better-supported run.

    If the same load value was measured more than once (e.g. a sweep was
    rerun), plotting all of them draws a line that zigzags through two
    contradictory points at the same x value. Keep the run with more
    completed requests, since its percentiles rest on a larger sample; ties
    go to the chronologically later run. Dropped runs are reported so the
    discrepancy can be investigated in index.csv.
    """
    parts = []
    for cfg in configs_present(df):
        g = df[df["config_label"] == cfg].copy()   # preserves file/chronological order
        mode = str(g["mode"].iloc[0]) if "mode" in g.columns and len(g) else "closed"
        load_col = "concurrency" if mode == "closed" else "target_qps"
        if load_col not in g.columns:
            load_col = "concurrency" if "concurrency" in g.columns else g.columns[0]
        if "requests_completed" in g.columns:
            # stable sort: ties keep chronological order, so keep="last" below
            # then prefers the later run among equally-supported duplicates
            g = g.sort_values("requests_completed", kind="stable", na_position="first")
        dup_mask = g.duplicated(subset=[load_col], keep="last")
        if dup_mask.any():
            kept = g[~dup_mask]
            for _, row in g[dup_mask].iterrows():
                match = kept[kept[load_col] == row[load_col]]
                winner = match.iloc[0] if len(match) else None
                winner_tag = winner.get("run_tag", "?") if winner is not None else "?"
                n_dropped = row.get("requests_completed", "?")
                n_winner = winner.get("requests_completed", "?") if winner is not None else "?"
                print(f"  note: {cfg} {load_col}={row[load_col]:g}: run "
                      f"{row.get('run_tag', '?')} (n={n_dropped}) superseded by "
                      f"{winner_tag} (n={n_winner}); dropped from the figures "
                      f"(compare both in index.csv)")
            g = kept
        parts.append(g)
    return pd.concat(parts) if parts else df


def has(df: pd.DataFrame, cols: list[str]) -> bool:
    return all(c in df.columns and df[c].notna().any() for c in cols)


def mark_low_n(ax, g, x, y) -> bool:
    """Circle points backed by fewer than MIN_SAMPLES completions.

    x and y must be the same (already unit-converted) values passed to the
    line's ax.plot() call -- e.g. ttft_p50 * 1e3, not the raw column -- or
    the circle lands at the raw value on an axis plotted in different units.

    Returns whether any point was marked, so callers can add a legend entry.
    """
    if "requests_completed" not in g.columns:
        return False
    mask = g["requests_completed"] < MIN_SAMPLES
    if not mask.any():
        return False
    x = x if isinstance(x, pd.Series) else pd.Series(x, index=g.index)
    y = y if isinstance(y, pd.Series) else pd.Series(y, index=g.index)
    ax.scatter(x[mask], y[mask], s=90, facecolors="none",
               edgecolors="0.15", linewidths=1.3, zorder=6, clip_on=False)
    return True


LOW_N_PROXY = Line2D([], [], marker="o", linestyle="none", markersize=7,
                     markerfacecolor="none", markeredgecolor="0.15",
                     label=f"n < {MIN_SAMPLES} (unreliable)")


def add_low_n_legend(ax, any_marked: bool, **legend_kwargs) -> None:
    """Re-add the legend with a low-n proxy entry appended.

    Pass the same kwargs (loc, fontsize, ncol, ...) used for the figure's
    original ax.legend() call so this doesn't reset its placement.
    """
    if not any_marked:
        return
    handles, labels = ax.get_legend_handles_labels()
    ax.legend(handles + [LOW_N_PROXY], labels + [LOW_N_PROXY.get_label()],
              **legend_kwargs)


def warn_low_n(df: pd.DataFrame) -> None:
    if "requests_completed" not in df.columns:
        return
    any_bad = False
    for cfg in configs_present(df):
        g, lc, _ = group(df, cfg)
        bad = g[g["requests_completed"] < MIN_SAMPLES]
        if len(bad):
            any_bad = True
            pts = ", ".join(f"{lc}={v:g} (n={int(n)})"
                            for v, n in zip(bad[lc], bad["requests_completed"]))
            print(f"  warning: {cfg} has {len(bad)} point(s) with fewer than "
                  f"{MIN_SAMPLES} completed requests in the window: {pts}")
    if any_bad:
        print(f"           percentiles at those loads are small-sample noise, not "
              f"a capacity reading; circled (o) in the figures. A longer "
              f"--measure-s at high concurrency gives more completions to trust.")


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
    any_low_n = False
    for i, cfg in enumerate(configs_present(df)):
        g, lc, mode = group(df, cfg)
        s = style_for(cfg, i)
        ax.plot(g[lc], g["tok_thru_tps"], marker=s["marker"], color=s["color"],
                label=s["label"])
        any_low_n |= mark_low_n(ax, g, g[lc], g["tok_thru_tps"])
        all_loads += g[lc].tolist()
    set_load_axis(ax, all_loads, mode)
    ax.set_ylabel("Output token throughput (tok/s)")
    ax.set_ylim(bottom=0)
    ax.legend()
    add_low_n_legend(ax, any_low_n)
    save(fig, "throughput_scaling", out, fmts)


def fig_latency_throughput(df, out, fmts):
    if not has(df, ["tok_thru_tps", "ttft_p50", "itl_p50"]):
        return
    fig, (axl, axr) = plt.subplots(1, 2, figsize=(TEXT_W, PANEL_H))
    any_low_n = False
    for i, cfg in enumerate(configs_present(df)):
        g, lc, _ = group(df, cfg)
        s = style_for(cfg, i)
        ttft_ms = g["ttft_p50"] * 1e3
        itl_ms = g["itl_p50"] * 1e3
        axl.plot(g["tok_thru_tps"], ttft_ms, marker=s["marker"],
                 color=s["color"], label=s["label"])
        axr.plot(g["tok_thru_tps"], itl_ms, marker=s["marker"],
                 color=s["color"], label=s["label"])
        any_low_n |= mark_low_n(axl, g, g["tok_thru_tps"], ttft_ms)
        any_low_n |= mark_low_n(axr, g, g["tok_thru_tps"], itl_ms)
        # annotate each point with its load so the swept parameter is visible
        for x, y, c in zip(g["tok_thru_tps"], ttft_ms, g[lc]):
            if pd.notna(x) and pd.notna(y):
                axl.annotate(f"{int(c)}", (x, y), textcoords="offset points",
                             xytext=(3, 3), fontsize=6, color=s["color"])
    # log scale: TTFT/ITL span >100x across loads, and a single saturated
    # point (e.g. a queueing blow-up) would otherwise flatten the rest of
    # the curve onto the axis on a linear scale.
    axl.set_xlabel("Output token throughput (tok/s)")
    axl.set_ylabel("Median TTFT (ms)")
    axl.set_yscale("log")
    axr.set_xlabel("Output token throughput (tok/s)")
    axr.set_ylabel("Median inter token latency (ms)")
    axr.set_yscale("log")
    axl.legend()
    add_low_n_legend(axl, any_low_n)
    save(fig, "latency_throughput", out, fmts)


def fig_tail_latency(df, out, fmts):
    if not has(df, ["ttft_p50", "ttft_p99", "itl_p50", "itl_p99"]):
        return
    fig, (axl, axr) = plt.subplots(1, 2, figsize=(TEXT_W, PANEL_H))
    all_loads = []
    any_low_n = False
    for i, cfg in enumerate(configs_present(df)):
        g, lc, mode = group(df, cfg)
        s = style_for(cfg, i)
        ttft_p50_ms = g["ttft_p50"] * 1e3
        ttft_p99_ms = g["ttft_p99"] * 1e3
        itl_p50_ms = g["itl_p50"] * 1e3
        itl_p99_ms = g["itl_p99"] * 1e3
        axl.plot(g[lc], ttft_p50_ms, marker=s["marker"],
                 color=s["color"], label=f"{s['label']} p50")
        axl.plot(g[lc], ttft_p99_ms, marker=s["marker"], linestyle="--",
                 color=s["color"], alpha=0.7, label=f"{s['label']} p99")
        axr.plot(g[lc], itl_p50_ms, marker=s["marker"],
                 color=s["color"], label=f"{s['label']} p50")
        axr.plot(g[lc], itl_p99_ms, marker=s["marker"], linestyle="--",
                 color=s["color"], alpha=0.7, label=f"{s['label']} p99")
        any_low_n |= mark_low_n(axl, g, g[lc], ttft_p50_ms)
        any_low_n |= mark_low_n(axl, g, g[lc], ttft_p99_ms)
        any_low_n |= mark_low_n(axr, g, g[lc], itl_p50_ms)
        any_low_n |= mark_low_n(axr, g, g[lc], itl_p99_ms)
        all_loads += g[lc].tolist()
    for ax, title in ((axl, "TTFT (ms)"), (axr, "Inter token latency (ms)")):
        set_load_axis(ax, all_loads, mode)
        ax.set_yscale("log")
        ax.set_ylabel(title)
    tail_legend_kwargs = dict(ncol=2, loc="upper center",
                              bbox_to_anchor=(0.5, -0.18), fontsize=7)
    axl.legend(**tail_legend_kwargs)
    add_low_n_legend(axl, any_low_n, **tail_legend_kwargs)
    save(fig, "tail_latency", out, fmts)


def fig_goodput(df, out, fmts):
    if not has(df, ["goodput_rps"]):
        return
    fig, ax = plt.subplots(figsize=(COL_W, PANEL_H))
    all_loads = []
    any_low_n = False
    for i, cfg in enumerate(configs_present(df)):
        g, lc, mode = group(df, cfg)
        s = style_for(cfg, i)
        ax.plot(g[lc], g["goodput_rps"], marker=s["marker"], color=s["color"],
                label=s["label"])
        if "req_thru_rps" in g.columns:
            ax.plot(g[lc], g["req_thru_rps"], marker=s["marker"], linestyle=":",
                    color=s["color"], alpha=0.5,
                    label=f"{s['label']} achieved")
        any_low_n |= mark_low_n(ax, g, g[lc], g["goodput_rps"])
        all_loads += g[lc].tolist()
    set_load_axis(ax, all_loads, mode)
    ax.set_ylabel("Goodput under SLO (req/s)")
    ax.set_ylim(bottom=0)
    ax.legend()
    add_low_n_legend(ax, any_low_n)
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
    global MIN_SAMPLES
    ap = argparse.ArgumentParser(description="Plot PMBS26 figures from index.csv")
    ap.add_argument("--index", default="results/index.csv")
    ap.add_argument("--out", default="figs")
    ap.add_argument("--format", default="pdf", choices=["pdf", "png", "both"])
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--min-samples", type=int, default=MIN_SAMPLES,
                    help="completions below this make a point's percentiles "
                         "unreliable; such points are circled (default: %(default)s)")
    args = ap.parse_args()

    MIN_SAMPLES = args.min_samples
    LOW_N_PROXY.set_label(f"n < {MIN_SAMPLES} (unreliable)")
    fmts = ["pdf", "png"] if args.format == "both" else [args.format]
    set_rc(args.dpi)
    df = load_index(args.index)

    print(f"loaded {len(df)} rows, configs: {', '.join(configs_present(df))}")
    df = dedupe_index(df)
    warn_low_n(df)
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
