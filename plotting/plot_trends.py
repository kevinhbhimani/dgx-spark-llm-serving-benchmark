#!/usr/bin/env python3
"""Cross-model trend figures for the PMBS26 paper, from a multi-model index.

Companion to plot_results.py (which draws the per-model figure sets and the
peak-bar model comparison). This module draws the figures whose subject is a
*trend across models* rather than one model's load sweep:

  parallelism_scaling  dual-node speedup over single per model (TP=2 and PP=2
                       peaks normalized to the single-Spark peak), plus PP=2
                       relative to TP=2 for every model that ran both.
  dense_vs_moe         matched-pair load sweeps for same-family, same-precision
                       dense vs MoE checkpoints (Qwen3-32B vs Qwen3-30B-A3B).
  precision_ladder     the same weights served at bf16 / fp8 / NVFP4
                       (Llama-3.3-70B ladder): throughput and per-token energy.
  efficiency_frontier  peak throughput vs tokens-per-watt at that peak, one
                       point per (model, config), classed open/NVIDIA x
                       dense/MoE.
  scale_trend          peak throughput vs resident weight footprint, the
                       memory-bound scaling view.

It is deliberately a separate file rather than an edit to plot_results.py:
scripts/run_job.sh seals each campaign against a clean committed tree that
includes plot_results.py, so mid-campaign figure development happens here and
can be merged into plot_results.py between campaigns. All loading, repeat
collapsing, and rectangle/identity guards are imported from plot_results so
both scripts read an index identically.

Usage:
    python plot_trends.py --index results/index.csv --out figs
    python plot_trends.py --allow-incomplete   # mid-campaign diagnostics

Model metadata (dense/MoE, precision, NVIDIA-optimized, parameter counts) is
keyed by model_label in MODEL_META, with a conservative pattern fallback for
labels the table does not know.
"""
from __future__ import annotations
import argparse
import re
import sys
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patheffects import withStroke

from plot_results import (
    COL_W, DUAL_NODE_CONFIGS, PANEL_H, TEXT_W, CONFIG_ORDER, CONFIG_STYLE,
    _model_values, _peak_row, _series_rows, _slug, _throughput_col,
    collapse_repeats, configs_present, draw_band, group, has,
    head_only_power, load_index, models_present, save, set_load_axis, set_rc,
)

# ---- model metadata ----------------------------------------------------------
# Keyed by model_label (configs/models.yaml). params_b/active_b are nominal
# published parameter counts in billions, used for ordering and axis text only.
# ladder_base groups checkpoints that serve the *same weights* at different
# precisions; the shared base label makes the precision ladder discoverable.
MODEL_META = {
    "qwen3-8b": dict(
        pretty="Qwen3-8B", arch="dense", quant="bf16", nvidia=False,
        params_b=8.2, active_b=8.2),
    "qwen3-32b": dict(
        pretty="Qwen3-32B", arch="dense", quant="bf16", nvidia=False,
        params_b=32.8, active_b=32.8),
    "qwen3-30b-a3b": dict(
        pretty="Qwen3-30B-A3B", arch="moe", quant="bf16", nvidia=False,
        params_b=30.5, active_b=3.3),
    "llama3.3-70b": dict(
        pretty="Llama-3.3-70B", arch="dense", quant="bf16", nvidia=False,
        params_b=70.6, active_b=70.6, ladder_base="llama3.3-70b"),
    "llama3.3-70b-fp8": dict(
        pretty="Llama-3.3-70B FP8", arch="dense", quant="fp8", nvidia=False,
        params_b=70.6, active_b=70.6, ladder_base="llama3.3-70b"),
    "llama3.3-70b-nvfp4": dict(
        pretty="Llama-3.3-70B NVFP4", arch="dense", quant="nvfp4", nvidia=True,
        params_b=70.6, active_b=70.6, ladder_base="llama3.3-70b"),
    "nemotron3-nano-30b-a3b-nvfp4": dict(
        pretty="Nemotron3-Nano-30B-A3B", arch="moe", quant="nvfp4",
        nvidia=True, params_b=30.0, active_b=3.0),
    "nemotron3-super-120b-a12b-nvfp4": dict(
        pretty="Nemotron3-Super-120B-A12B", arch="moe", quant="nvfp4",
        nvidia=True, params_b=120.0, active_b=12.0),
}

# Same-family, same-precision dense/MoE pairs at comparable total parameter
# count. Only pairs listed here are drawn: automatic pairing across families
# or precisions would compare confounded checkpoints.
DENSE_MOE_PAIRS = [("qwen3-32b", "qwen3-30b-a3b")]


def meta_for(label: str) -> dict:
    """Metadata for a model label, with a pattern fallback for unknown labels.

    The fallback classifies from the label alone (``-a<N>b`` marks MoE,
    ``nvfp4``/``fp8`` mark precision, NVIDIA checkpoints carry ``nemotron`` or
    ``nvfp4``) so a new model still lands in the right trend bucket, just
    without a curated pretty name or parameter count.
    """
    if label in MODEL_META:
        return MODEL_META[label]
    lab = str(label).lower()
    size = re.search(r"(\d+(?:\.\d+)?)b", lab)
    active = re.search(r"-a(\d+(?:\.\d+)?)b", lab)
    params = float(size.group(1)) if size else float("inf")
    return dict(
        pretty=str(label),
        arch="moe" if active else "dense",
        quant=("nvfp4" if "nvfp4" in lab else "fp8" if "fp8" in lab
               else "bf16"),
        nvidia=("nemotron" in lab or "nvfp4" in lab),
        params_b=params,
        active_b=float(active.group(1)) if active else params,
    )


def models_by_size(df: pd.DataFrame) -> list[str]:
    return sorted(models_present(df), key=lambda m: meta_for(m)["params_b"])


# Precision hues (Okabe-Ito, adjacent-pair CVD dE >= 12.4 under
# protan/deutan/tritan simulation, normal-vision dE >= 24). Identity is also
# carried by the legend order and per-config linestyle/marker, never hue alone.
PRECISION_STYLE = {
    "bf16":  dict(color="#0072B2", label="BF16"),
    "fp8":   dict(color="#E69F00", label="FP8"),
    "nvfp4": dict(color="#009E73", label="NVFP4"),
}
# Within precision/class figures configs are told apart by linestyle + marker
# (colors there belong to precision or class), reusing each config's
# established marker shape from CONFIG_STYLE.
CONFIG_LINESTYLE = {"single": "-", "tp2": "--", "pp2": ":"}

# Model classes for the frontier scatter. Hues avoid the config trio's
# blue/vermillion/green so no color means one thing in the load-sweep figures
# and another here (validated: adjacent-pair CVD dE >= 8.5, normal >= 20).
# Points are additionally annotated with the model name and shaped by config,
# so class hue is never the only encoding.
CLASS_STYLE = {
    "open-dense":   dict(color="#000000", label="Open dense"),
    "open-moe":     dict(color="#56B4E9", label="Open MoE"),
    "nvidia-dense": dict(color="#CC79A7", label="NVIDIA-opt. dense"),
    "nvidia-moe":   dict(color="#E69F00", label="NVIDIA-opt. MoE"),
}


def class_of(label: str) -> str:
    m = meta_for(label)
    return ("nvidia" if m["nvidia"] else "open") + "-" + m["arch"]


def _peak(df: pd.DataFrame, model: str, cfg: str, tcol: str):
    return _peak_row(_series_rows(df, model, cfg), tcol)


def _bar_labels(ax, xs, vals, fmt="{:.2f}x") -> None:
    for x, v in zip(xs, vals):
        if pd.notna(v):
            ax.annotate(fmt.format(v), (x, v), textcoords="offset points",
                        xytext=(0, 2), ha="center", fontsize=6, color="0.25")


def _name_point(ax, x, y, label) -> None:
    """Model-name annotation with a white halo so it survives grid/marker
    crossings. Scatter figures call this once per model (not per point) to
    keep labels from colliding."""
    ax.annotate(label, (x, y), textcoords="offset points", xytext=(3, 3),
                fontsize=5.5, color="0.25",
                path_effects=[withStroke(linewidth=1.5, foreground="white")])


# ---- figures -----------------------------------------------------------------
def fig_parallelism_scaling(df, out, fmts):
    """Dual-node payoff per model: TP=2/PP=2 peak over the single-Spark peak,
    and PP=2 relative to TP=2.

    Speedups compare each configuration's *peak* exact token-in-window
    throughput over its own concurrency sweep, so a config is credited with its
    best operating point rather than penalized at a load it would never be run
    at. The parity line at 1.0 marks where two Sparks merely match one; the
    dashed line at 2.0 is the ideal for doubled hardware. Models whose bf16
    weights do not fit one node (no single config) appear only in the PP/TP
    panel.
    """
    tcol = _throughput_col(df)
    models = models_by_size(df)
    speed, ratio = {}, {}
    for m in models:
        peaks = {c: _peak(df, m, c, tcol) for c in ("single", "tp2", "pp2")}
        vals = {c: float(r[tcol]) if r is not None else np.nan
                for c, r in peaks.items()}
        if pd.notna(vals["single"]) and (
                pd.notna(vals["tp2"]) or pd.notna(vals["pp2"])):
            speed[m] = (vals["tp2"] / vals["single"],
                        vals["pp2"] / vals["single"])
        if pd.notna(vals["tp2"]) and pd.notna(vals["pp2"]):
            ratio[m] = vals["pp2"] / vals["tp2"]
    if not speed and not ratio:
        print("  parallelism_scaling: no model has both a single and a "
              "dual-node series yet, skipping")
        return

    fig, (axl, axr) = plt.subplots(1, 2, figsize=(TEXT_W, PANEL_H + 0.6))
    if speed:
        ms = [m for m in models if m in speed]
        x = np.arange(len(ms))
        tp = [speed[m][0] for m in ms]
        pp = [speed[m][1] for m in ms]
        axl.bar(x - 0.19, tp, 0.36, color=CONFIG_STYLE["tp2"]["color"],
                label=CONFIG_STYLE["tp2"]["label"])
        axl.bar(x + 0.19, pp, 0.36, color=CONFIG_STYLE["pp2"]["color"],
                label=CONFIG_STYLE["pp2"]["label"])
        _bar_labels(axl, x - 0.19, tp)
        _bar_labels(axl, x + 0.19, pp)
        axl.axhline(1.0, color="0.4", linewidth=0.8)
        axl.axhline(2.0, color="0.4", linewidth=0.8, linestyle="--")
        axl.set_xticks(x)
        axl.set_xticklabels([meta_for(m)["pretty"] for m in ms],
                            rotation=30, ha="right", fontsize=7)
        axl.set_ylabel("Peak throughput vs single Spark")
        axl.set_ylim(0, max(2.2, np.nanmax(tp + pp) * 1.12))
        axl.grid(axis="x", visible=False)
        handles, labels = axl.get_legend_handles_labels()
        handles += [Line2D([], [], color="0.4", linewidth=0.8),
                    Line2D([], [], color="0.4", linewidth=0.8,
                           linestyle="--")]
        labels += ["single-node parity", "ideal 2x"]
        axl.legend(handles, labels, fontsize=6.5, ncol=2)
    else:
        axl.axis("off")
        axl.text(0.5, 0.5, "no single-node baselines in index",
                 transform=axl.transAxes, ha="center", va="center",
                 fontsize=8, color="0.4")
    if ratio:
        ms = [m for m in models if m in ratio]
        x = np.arange(len(ms))
        vals = [ratio[m] for m in ms]
        axr.bar(x, vals, 0.55, color="0.55")
        _bar_labels(axr, x, vals)
        axr.axhline(1.0, color="0.4", linewidth=0.8)
        axr.set_xticks(x)
        axr.set_xticklabels([meta_for(m)["pretty"] for m in ms],
                            rotation=30, ha="right", fontsize=7)
        axr.set_ylabel("PP=2 peak relative to TP=2")
        axr.set_ylim(bottom=0)
        axr.grid(axis="x", visible=False)
    else:
        axr.axis("off")
        axr.text(0.5, 0.5, "no model ran both TP=2 and PP=2",
                 transform=axr.transAxes, ha="center", va="center",
                 fontsize=8, color="0.4")
    save(fig, "parallelism_scaling", out, fmts, tight=True)


def fig_dense_vs_moe(df, out, fmts):
    """Load sweeps for a matched dense/MoE pair: throughput plus per-token
    energy (or median ITL when the index has no power data).

    Config identity keeps its paper-wide color and marker; architecture is the
    solid (dense) vs dashed (MoE) linestyle, so the same-colored curve pair at
    each config reads as "same deployment shape, different architecture".
    """
    mv = _model_values(df)
    tcol = _throughput_col(df)
    pairs = [
        (d, m) for d, m in DENSE_MOE_PAIRS
        if mv is not None and (mv == d).any() and (mv == m).any()
    ]
    if not pairs:
        print("  dense_vs_moe: no matched dense/MoE pair in index, skipping")
        return
    energy_ok = has(df, ["energy_per_token_j"])
    rcol = "energy_per_token_j" if energy_ok else "itl_p50"
    for dense, moe in pairs:
        fig, (axl, axr) = plt.subplots(1, 2, figsize=(TEXT_W, PANEL_H))
        all_loads, mode = [], "closed"
        used_cfgs = []
        for i, cfg in enumerate(CONFIG_ORDER):
            s = CONFIG_STYLE.get(cfg)
            if s is None:
                continue
            for model, ls in ((dense, "-"), (moe, "--")):
                msub = df[mv == model]
                if not (msub["config_label"] == cfg).any():
                    continue
                g, lc, mode = group(msub, cfg)
                if cfg not in used_cfgs:
                    used_cfgs.append(cfg)
                axl.plot(g[lc], g[tcol], marker=s["marker"], linestyle=ls,
                         color=s["color"])
                draw_band(axl, g, g[lc], tcol, s["color"])
                if rcol in g.columns and g[rcol].notna().any():
                    y = g[rcol] * (1.0 if energy_ok else 1e3)
                    axr.plot(g[lc], y, marker=s["marker"], linestyle=ls,
                             color=s["color"])
                    if energy_ok:
                        draw_band(axr, g, g[lc], rcol, s["color"])
                all_loads += g[lc].tolist()
        set_load_axis(axl, all_loads, mode)
        set_load_axis(axr, all_loads, mode)
        axl.set_ylabel("Output token throughput (tok/s)")
        axl.set_ylim(bottom=0)
        if energy_ok:
            axr.set_ylabel("Sampled energy per token (J)")
            axr.set_ylim(bottom=0)
        else:
            axr.set_ylabel("Median inter token latency (ms)")
            axr.set_yscale("log")
        proxies = [Line2D([], [], color=CONFIG_STYLE[c]["color"],
                          marker=CONFIG_STYLE[c]["marker"],
                          label=CONFIG_STYLE[c]["label"])
                   for c in used_cfgs]
        proxies += [
            Line2D([], [], color="0.15", linestyle="-",
                   label=f"{meta_for(dense)['pretty']} (dense)"),
            Line2D([], [], color="0.15", linestyle="--",
                   label=f"{meta_for(moe)['pretty']} (MoE)"),
        ]
        axl.legend(handles=proxies, fontsize=7)
        name = "dense_vs_moe"
        if len(pairs) > 1:
            name += f"_{_slug(dense)}"
        save(fig, name, out, fmts)


def fig_precision_ladder(df, out, fmts):
    """The same weights served at descending precision: throughput and energy.

    Groups checkpoints by MODEL_META ladder_base (Llama-3.3-70B at bf16, fp8,
    and NVIDIA's NVFP4 ModelOpt checkpoint). Color carries precision; configs
    are told apart by linestyle and their usual marker. The bf16 base runs
    dual-node only (it does not fit one GB10), which is itself part of the
    story: quantization is what makes single-Spark serving of a 70B possible.
    """
    models = models_present(df)
    ladders: dict[str, list[str]] = {}
    for m in models:
        base = meta_for(m).get("ladder_base")
        if base:
            ladders.setdefault(base, []).append(m)
    ladders = {b: ms for b, ms in ladders.items() if len(ms) >= 2}
    if not ladders:
        print("  precision_ladder: fewer than two precisions of one base "
              "model in index, skipping")
        return
    mv = _model_values(df)
    tcol = _throughput_col(df)
    energy_ok = has(df, ["energy_per_token_j"])
    for base, members in ladders.items():
        members = sorted(
            members, key=lambda m: ["bf16", "fp8", "nvfp4"].index(
                meta_for(m)["quant"]))
        fig, (axl, axr) = plt.subplots(1, 2, figsize=(TEXT_W, PANEL_H))
        all_loads, mode = [], "closed"
        used_cfgs, used_quants = [], []
        for model in members:
            quant = meta_for(model)["quant"]
            ps = PRECISION_STYLE[quant]
            msub = df[mv == model]
            for cfg in configs_present(msub):
                ls = CONFIG_LINESTYLE.get(cfg, "-")
                mk = CONFIG_STYLE.get(cfg, {}).get("marker", "D")
                g, lc, mode = group(msub, cfg)
                if cfg not in used_cfgs:
                    used_cfgs.append(cfg)
                if quant not in used_quants:
                    used_quants.append(quant)
                axl.plot(g[lc], g[tcol], marker=mk, linestyle=ls,
                         color=ps["color"])
                draw_band(axl, g, g[lc], tcol, ps["color"])
                if energy_ok and g["energy_per_token_j"].notna().any():
                    axr.plot(g[lc], g["energy_per_token_j"], marker=mk,
                             linestyle=ls, color=ps["color"])
                    draw_band(axr, g, g[lc], "energy_per_token_j",
                              ps["color"])
                all_loads += g[lc].tolist()
        set_load_axis(axl, all_loads, mode)
        axl.set_ylabel("Output token throughput (tok/s)")
        axl.set_ylim(bottom=0)
        if energy_ok:
            set_load_axis(axr, all_loads, mode)
            axr.set_ylabel("Sampled energy per token (J)")
            axr.set_ylim(bottom=0)
        else:
            axr.axis("off")
            axr.text(0.5, 0.5, "no power data in index",
                     transform=axr.transAxes, ha="center", va="center",
                     fontsize=8, color="0.4")
        proxies = [Line2D([], [], color=PRECISION_STYLE[q]["color"],
                          label=PRECISION_STYLE[q]["label"])
                   for q in ("bf16", "fp8", "nvfp4") if q in used_quants]
        proxies += [Line2D([], [], color="0.15",
                           linestyle=CONFIG_LINESTYLE[c],
                           marker=CONFIG_STYLE[c]["marker"],
                           label=CONFIG_STYLE[c]["label"])
                    for c in CONFIG_ORDER if c in used_cfgs]
        axl.legend(handles=proxies, fontsize=7)
        name = "precision_ladder"
        if len(ladders) > 1:
            name += f"_{_slug(base)}"
        save(fig, name, out, fmts)


def fig_efficiency_frontier(df, out, fmts):
    """Peak throughput vs energy efficiency at that peak, all models/configs.

    One point per (model, config) at the config's throughput-peak row, so the
    x and y values describe the same operating point. Hue is the model class
    (open/NVIDIA-optimized x dense/MoE), shape is the config, and every point
    is name-labeled. Head-node-only power on a dual-node point (pre-fix rows)
    overplots an x: its tokens/W is an upper bound.
    """
    tcol = _throughput_col(df)
    if not has(df, ["tokens_per_watt"]):
        print("  efficiency_frontier: no power data in index, skipping")
        return
    fig, ax = plt.subplots(figsize=(COL_W * 1.35, PANEL_H + 0.3))
    seen_classes, seen_cfgs = [], []
    for model in models_by_size(df):
        cls = class_of(model)
        cs = CLASS_STYLE[cls]
        pts = []
        for cfg in ("single", "tp2", "pp2"):
            row = _peak(df, model, cfg, tcol)
            if row is None or pd.isna(row.get("tokens_per_watt", np.nan)):
                continue
            mk = CONFIG_STYLE.get(cfg, {}).get("marker", "D")
            x, y = float(row[tcol]), float(row["tokens_per_watt"])
            ax.scatter([x], [y], marker=mk, color=cs["color"], s=30,
                       zorder=5)
            if head_only_power(cfg, pd.DataFrame([row])):
                ax.scatter([x], [y], marker="x", color="0.15", s=40, zorder=6)
            pts.append((x, y))
            if cls not in seen_classes:
                seen_classes.append(cls)
            if cfg not in seen_cfgs:
                seen_cfgs.append(cfg)
        if pts:
            # one label per model, at its fastest config's point
            x, y = max(pts)
            _name_point(ax, x, y, meta_for(model)["pretty"])
    if not seen_classes:
        plt.close(fig)
        print("  efficiency_frontier: no peak rows with power data, skipping")
        return
    ax.set_xscale("log")
    # headroom so the per-point name labels stay inside the axes
    ax.margins(x=0.15, y=0.10)
    ax.set_xlabel("Peak output token throughput (tok/s)")
    ax.set_ylabel("Tokens per sampled watt (tok/s/W)")
    ax.set_ylim(bottom=0)
    proxies = [Line2D([], [], linestyle="none", marker="o",
                      color=CLASS_STYLE[c]["color"],
                      label=CLASS_STYLE[c]["label"])
               for c in CLASS_STYLE if c in seen_classes]
    proxies += [Line2D([], [], linestyle="none", marker=CONFIG_STYLE[c]["marker"],
                       color="0.45", label=CONFIG_STYLE[c]["label"])
                for c in CONFIG_ORDER if c in seen_cfgs]
    ax.legend(handles=proxies, fontsize=6.5)
    save(fig, "efficiency_frontier", out, fmts)


def fig_scale_trend(df, out, fmts):
    """Peak throughput against resident weight footprint, per config.

    On a bandwidth-bound device the tokens/s ceiling tracks the bytes touched
    per token, so weight GiB (which quantization shrinks, unlike nominal
    parameter count) is the mechanistic x axis. The footprint is the optional
    HF estimate (weight_gb) when the index carries it, else the vLLM startup
    log's actual per-worker weights (worker_weight_gb) doubled on dual-node
    configs where each worker holds half the model. Config keeps its
    paper-wide color and marker; points are name-labeled.
    """
    tcol = _throughput_col(df)
    if not (has(df, ["weight_gb"]) or has(df, ["worker_weight_gb"])):
        print("  scale_trend: no weight_gb/worker_weight_gb in index, "
              "skipping")
        return

    def footprint(row, cfg):
        v = row.get("weight_gb", np.nan)
        if pd.notna(v):
            return float(v)
        v = row.get("worker_weight_gb", np.nan)
        if pd.isna(v):
            return np.nan
        return float(v) * (2.0 if cfg in DUAL_NODE_CONFIGS else 1.0)
    fig, ax = plt.subplots(figsize=(COL_W * 1.35, PANEL_H + 0.3))
    seen_cfgs = []
    for model in models_by_size(df):
        pts = []
        for cfg in ("single", "tp2", "pp2"):
            row = _peak(df, model, cfg, tcol)
            if row is None:
                continue
            gib = footprint(row, cfg)
            if pd.isna(gib):
                continue
            s = CONFIG_STYLE.get(cfg, dict(color="0.3", marker="D",
                                           label=cfg))
            x, y = gib, float(row[tcol])
            ax.scatter([x], [y], marker=s["marker"], color=s["color"], s=30,
                       zorder=5)
            pts.append((y, x))
            if cfg not in seen_cfgs:
                seen_cfgs.append(cfg)
        if pts:
            # one label per model, at its highest-throughput point
            y, x = max(pts)
            _name_point(ax, x, y, meta_for(model)["pretty"])
    if not seen_cfgs:
        plt.close(fig)
        print("  scale_trend: no peak rows with weight_gb, skipping")
        return
    ax.set_xscale("log", base=2)
    ax.set_yscale("log")
    ax.margins(x=0.15, y=0.10)
    ax.set_xlabel("Model weight footprint (GiB)")
    ax.set_ylabel("Peak output token throughput (tok/s)")
    proxies = [Line2D([], [], linestyle="none",
                      marker=CONFIG_STYLE[c]["marker"],
                      color=CONFIG_STYLE[c]["color"],
                      label=CONFIG_STYLE[c]["label"])
               for c in CONFIG_ORDER if c in seen_cfgs]
    ax.legend(handles=proxies, fontsize=6.5)
    save(fig, "scale_trend", out, fmts)


# ---- main --------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Plot cross-model trend figures from a multi-model index")
    ap.add_argument("--index", default="results/index.csv")
    ap.add_argument("--out", default="figs")
    ap.add_argument("--format", default="pdf", choices=["pdf", "png", "both"])
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--serif", action="store_true",
                    help="use a Times-like serif font to match an IEEE paper")
    ap.add_argument(
        "--allow-incomplete", action="store_true",
        help="diagnostic only: plot mid-campaign indexes with missing "
             "loads/repeats (figures are not paper-ready)")
    args = ap.parse_args()

    fmts = ["pdf", "png"] if args.format == "both" else [args.format]
    set_rc(args.dpi, args.serif)
    df = load_index(args.index)
    models = models_present(df)
    if len(models) < 2:
        sys.exit("trend figures need at least two models in the index; "
                 "plot_results.py covers the single-model case")
    print(f"loaded {len(df)} rows, models: {', '.join(models)}, "
          f"configs: {', '.join(configs_present(df))}")
    unknown = [m for m in models if m not in MODEL_META]
    if unknown:
        print("  note: metadata fallback (label patterns) used for: "
              + ", ".join(unknown))
    df = collapse_repeats(df, allow_incomplete=args.allow_incomplete)

    fig_parallelism_scaling(df, args.out, fmts)
    fig_dense_vs_moe(df, args.out, fmts)
    fig_precision_ladder(df, args.out, fmts)
    fig_efficiency_frontier(df, args.out, fmts)
    fig_scale_trend(df, args.out, fmts)
    print("done")


if __name__ == "__main__":
    main()
