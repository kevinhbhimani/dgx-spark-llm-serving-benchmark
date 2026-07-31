#!/usr/bin/env python3
"""One-command generator for the complete PMBS26 paper figure and table set.

The per-model figure sets in plot_results.py spend one figure per model, which
a 10-page paper cannot afford. This script consolidates the same data into
paper-density figures and writes every candidate paper artifact into one
output directory:

  throughput_grid    small multiples: output tok/s vs concurrency, one panel
                     per model (ordered by size), three serving configs each.
  ttft_grid          median TTFT vs concurrency per model, with the 3 s SLO.
  itl_grid           median request-mean inter-token latency vs concurrency
                     per model, with strict (0.10 s) and loose (0.20 s) tiers.
  goodput_grid       SLO goodput vs concurrency per model, strict and loose.
  energy_grid        per-token energy vs concurrency per model (both nodes
                     sampled on dual-node rows; head-only points are marked).
  operating_regimes  representative fast-dense, large-dense, and sparse-MoE
                     load sweeps: throughput over request-mean ITL.
  kv_capacity        KV-cache capacity per (model, config): how many maximum
                     length requests fit, the mechanism behind TP=2's wins.
  allreduce          the dual-node all-reduce microbenchmark: latency and bus
                     bandwidth vs message size, with an estimated
                     activation-payload band for transport context.
  peak_table         compact per-(model, config) headline table (.tex + .md).

plus the cross-model trend figures re-rendered here for a uniform paper style
(parallelism_scaling, dense_vs_moe, precision_ladder, efficiency_frontier,
scale_trend from plot_trends.py; model_comparison, summary and capacity tables
from plot_results.py).

plot_results.py is part of the sealed v3 campaign source and is imported, not
edited. This file follows the plot_trends.py pattern: figure development lives
outside the sealed tree and reads the index through the exact same loading,
repeat-collapsing, and identity guards.

Usage:
    python plotting/plot_paper.py                       # results/index.csv -> figs/paper
    python plotting/plot_paper.py --index results/index.csv --out figs/paper
    python plotting/plot_paper.py --sans                # keep the sans font

Figures default to serif (Times-like) text to match the IEEE paper body, and
are written as vector PDF for \\includegraphics plus PNG previews.
"""
from __future__ import annotations
import argparse
import glob
import hashlib
import json
import math
import os
import re
import sys
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter, NullFormatter

from plot_results import (
    COL_W, CONFIG_ORDER, CONFIG_STYLE, DUAL_NODE_CONFIGS, TEXT_W,
    _fmt, _model_values, _throughput_col, collapse_repeats, configs_present,
    group, has, head_only_power, load_index, models_present, report_quality,
    save, set_rc, style_for, warn_low_n, write_capacity_table,
    write_summary_table, fig_model_comparison, MIN_SAMPLES,
)
from plot_trends import (
    clean_log_ticks, meta_for, models_by_size, fig_parallelism_scaling,
    fig_dense_vs_moe, fig_precision_ladder, fig_efficiency_frontier,
    fig_scale_trend, fig_prefill_scaling,
)

# SLO bounds drawn as reference lines. Values mirror configs/experiment.yaml
# (ttft_slo_s, itl_slo_s, itl_slo_s2); the goodput columns in the index were
# scored against these same bounds.
TTFT_SLO_S = 3.0
ITL_SLO_MS = 100.0
ITL_SLO2_MS = 200.0

GRID_COLS = 4
PANEL_TITLE_FS = 7.5
PANEL_TICK_FS = 7.0
PANEL_LABEL_FS = 8.0
SLO_COLOR = "0.35"


def _publication_rc() -> None:
    """Camera-ready hardening on top of set_rc().

    IEEE PDF eXpress rejects matplotlib's default Type 3 font subsetting, so
    embed TrueType (Type 42) outlines instead. That rules out the CFF-only
    Nimbus Roman fallback; Liberation Serif is metric-compatible with Times
    and ships as TrueType on this machine.

    set_rc() points mathtext at ``dejavuserif``, which is a different typeface
    from the text family: any panel carrying a ``$\\approx$`` or ``$R^2$``
    embedded DejaVu Serif next to Liberation Serif, so the math glyphs came out
    wider and heavier than the words around them. Mapping mathtext at the same
    family keeps one serif per figure.
    """
    serif = ["Times New Roman", "Liberation Serif", "DejaVu Serif"]
    plt.rcParams.update({
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "font.serif": serif,
    })
    # mathtext.rm takes a concrete family, not font.serif's fallback chain, so
    # it has to be resolved here: naming a font that is not installed drops
    # mathtext to DejaVu *Sans* rather than down the serif list, which is how a
    # sans "R" ends up inside a serif figure. Resolve the first family actually
    # present and only switch to a custom fontset once one is found.
    installed = {f.name for f in font_manager.fontManager.ttflist}
    family = next((f for f in serif if f in installed), None)
    if family is not None:
        plt.rcParams.update({
            "mathtext.fontset": "custom",
            "mathtext.rm": family,
            "mathtext.it": f"{family}:italic",
            "mathtext.bf": f"{family}:bold",
            "mathtext.default": "regular",
        })


def _panel_load_axis(ax, loads) -> None:
    """Compact log2 concurrency axis for small grid panels.

    set_load_axis() labels every power of two, which collides at panel widths
    of ~1.6 in; majors every other power keep the ticks readable.
    """
    loads = sorted({float(v) for v in loads if pd.notna(v)})
    ax.set_xscale("log", base=2)
    if loads:
        ax.set_xlim(min(loads) / 1.35, max(loads) * 1.35)
    # Every other power of two, but anchored at the TOP of the sweep so the
    # highest load always carries a label: the paper quotes c=128 repeatedly
    # (queueing collapse, TP/PP crossovers), and an unlabelled final point
    # makes the reader count doublings to find it.
    ticks = []
    if loads:
        v = max(loads)
        while v >= min(loads):
            ticks.append(v)
            v /= 4
        ticks = sorted(ticks)
        if min(loads) not in ticks:
            ticks.insert(0, min(loads))
    ax.set_xticks(ticks)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{int(v)}"))
    ax.xaxis.set_minor_formatter(NullFormatter())
    ax.tick_params(labelsize=PANEL_TICK_FS)
    ax.tick_params(which="minor", length=1.5)


def _mark_low_n_panel(ax, g, x, y, minimum=MIN_SAMPLES) -> bool:
    """mark_low_n with a smaller circle so it fits grid-panel markers."""
    ncol = ("requests_completed__min" if "requests_completed__min" in g.columns
            else "requests_completed")
    if ncol not in g.columns:
        return False
    mask = g[ncol] < minimum
    if not mask.any():
        return False
    x = x if isinstance(x, pd.Series) else pd.Series(x, index=g.index)
    y = y if isinstance(y, pd.Series) else pd.Series(y, index=g.index)
    ax.scatter(x[mask], y[mask], s=45, facecolors="none",
               edgecolors="0.15", linewidths=1.0, zorder=6, clip_on=False)
    return True


LOW_N_PROXY = Line2D([], [], marker="o", linestyle="none", markersize=5,
                     markerfacecolor="none", markeredgecolor="0.15",
                     label=f"n < {MIN_SAMPLES}")


def _panel_title(model: str, *, compact: bool = False) -> str:
    """Model title with its precision, unless the name already carries it."""
    meta = meta_for(model)
    pretty = str(meta["pretty"])
    if compact:
        pretty = (pretty.replace("Llama-3.1", "L3.1")
                        .replace("Llama-3.3", "L3.3")
                        .replace("Nemotron3-", "N3-"))
    quant = meta["quant"].upper()
    if quant.lower() in pretty.lower():
        return pretty
    return f"{pretty} ({quant})"


def _model_grid(df, name, out, fmts, draw_panel, ylabel, *,
                finalize_panel=None, panel_note=None, extra_proxies=(),
                footnote=None, compact_titles=False):
    """Small-multiples frame: one panel per model plus a legend cell.

    draw_panel(ax, g, load_col, style, cfg) plots one config series into a
    panel and returns True when it circled any low-sample point; it must not
    set scales or limits -- set_ylim/axhline freeze autoscaling and would clip
    every config drawn after the first. Per-panel scale/limit/reference-line
    work goes in finalize_panel(ax), called once after all configs. Panels get
    per-model independent y ranges (throughput spans 26..520 tok/s across
    models; a shared range would flatten the slow models) but one shared,
    identically-ticked concurrency axis. Config identity keeps the paper-wide
    hue and marker from CONFIG_STYLE.
    """
    models = models_by_size(df)
    mv = _model_values(df)
    n = len(models)
    nrows = math.ceil((n + 1) / GRID_COLS)
    fig, axes = plt.subplots(nrows, GRID_COLS,
                             figsize=(TEXT_W, 1.72 * nrows + 0.30))
    flat = np.atleast_2d(axes).ravel()
    used_cfgs: list[str] = []
    any_low = False
    all_loads: list[float] = []
    for ax, model in zip(flat, models):
        msub = df[mv == model]
        cfgs = configs_present(msub)
        for cfg in cfgs:
            g, lc, _ = group(msub, cfg)
            s = style_for(cfg, CONFIG_ORDER.index(cfg)
                          if cfg in CONFIG_ORDER else 0)
            any_low |= bool(draw_panel(ax, g, lc, s, cfg))
            if cfg not in used_cfgs:
                used_cfgs.append(cfg)
            all_loads += g[lc].tolist()
        if finalize_panel is not None:
            finalize_panel(ax)
        if panel_note is not None:
            panel_note(ax, msub)
        # a single-series panel is a skipped-config finding (e.g. FP8-70B
        # runs TP=2 only); say so in the title so it cannot read as missing
        # data
        title = _panel_title(model, compact=compact_titles)
        if cfgs == ["tp2"]:
            title += " (TP=2 only)"
        ax.set_title(title, fontsize=PANEL_TITLE_FS, pad=3)
    for ax in flat[:n]:
        _panel_load_axis(ax, all_loads)
    # x labels on the lowest data panel of each column (the legend cell and
    # empty cells below a column must not swallow that column's axis labels)
    for col in range(GRID_COLS):
        col_axes = [flat[r * GRID_COLS + col] for r in range(nrows)
                    if r * GRID_COLS + col < n]
        if col_axes:
            col_axes[-1].set_xlabel("Concurrent requests",
                                    fontsize=PANEL_LABEL_FS)
    for r in range(nrows):
        if r * GRID_COLS < n:
            flat[r * GRID_COLS].set_ylabel(ylabel, fontsize=PANEL_LABEL_FS)
    # legend cell, then blank any remaining cells
    proxies = [Line2D([], [], color=CONFIG_STYLE[c]["color"],
                      marker=CONFIG_STYLE[c]["marker"], markersize=4,
                      label=CONFIG_STYLE[c]["label"])
               for c in CONFIG_ORDER if c in used_cfgs]
    proxies += list(extra_proxies)
    if any_low:
        proxies.append(LOW_N_PROXY)
    legend_ax = flat[n]
    legend_ax.axis("off")
    legend_ax.legend(handles=proxies, loc="center", fontsize=7.0,
                     frameon=False, handlelength=2.2)
    for ax in flat[n + 1:]:
        ax.axis("off")
    if footnote:
        fig.text(0.5, -0.02, footnote, ha="center", va="top",
                 fontsize=6.0, color="0.30")
    save(fig, name, out, fmts, tight=footnote is not None)
    return any_low


# ---- grid figures ------------------------------------------------------------
def fig_throughput_grid(df, out, fmts):
    tcol = _throughput_col(df)
    if not has(df, [tcol]):
        return

    def panel(ax, g, lc, s, cfg):
        ax.plot(g[lc], g[tcol], marker=s["marker"], color=s["color"],
                markersize=3.2, linewidth=1.2)
        return False

    _model_grid(df, "throughput_grid", out, fmts, panel,
                "Throughput (tok/s)",
                finalize_panel=lambda ax: ax.set_ylim(bottom=0))


def fig_ttft_grid(df, out, fmts):
    if not has(df, ["ttft_p50"]):
        return
    slo = Line2D([], [], color=SLO_COLOR, linestyle=":", linewidth=1.0,
                 label="TTFT SLO 3 s")

    def panel(ax, g, lc, s, cfg):
        y = g["ttft_p50"]
        ax.plot(g[lc], y, marker=s["marker"], color=s["color"],
                markersize=3.2, linewidth=1.2)
        return _mark_low_n_panel(ax, g, g[lc], y)

    def finalize(ax):
        ax.set_yscale("log")
        ax.axhline(TTFT_SLO_S, color=SLO_COLOR, linestyle=":",
                   linewidth=0.9)
        clean_log_ticks(ax)

    _model_grid(df, "ttft_grid", out, fmts, panel, "Median TTFT (s)",
                finalize_panel=finalize, extra_proxies=[slo])


def fig_itl_grid(df, out, fmts):
    if not has(df, ["mean_itl_p50"]):
        return
    slo1 = Line2D([], [], color=SLO_COLOR, linestyle=":", linewidth=1.0,
                  label="strict SLO 100 ms")
    slo2 = Line2D([], [], color=SLO_COLOR, linestyle="-.", linewidth=1.0,
                  label="loose SLO 200 ms")

    def panel(ax, g, lc, s, cfg):
        y = g["mean_itl_p50"] * 1e3
        ax.plot(g[lc], y, marker=s["marker"], color=s["color"],
                markersize=3.2, linewidth=1.2)
        return _mark_low_n_panel(ax, g, g[lc], y)

    def finalize(ax):
        ax.set_yscale("log")
        ax.axhline(ITL_SLO_MS, color=SLO_COLOR, linestyle=":", linewidth=0.9)
        ax.axhline(ITL_SLO2_MS, color=SLO_COLOR, linestyle="-.",
                   linewidth=0.9)
        clean_log_ticks(ax)

    _model_grid(df, "itl_grid", out, fmts, panel,
                "Median request-mean ITL (ms)",
                finalize_panel=finalize, extra_proxies=[slo1, slo2])


def fig_goodput_grid(df, out, fmts):
    if not has(df, ["goodput_rps"]):
        return
    has_itl2 = has(df, ["goodput_itl2_rps"])
    strict = Line2D([], [], color="0.15", linestyle="-",
                    label="strict ITL tier")
    loose = Line2D([], [], color="0.15", linestyle="--",
                   label="loose ITL tier")

    def panel(ax, g, lc, s, cfg):
        ax.plot(g[lc], g["goodput_rps"], marker=s["marker"], color=s["color"],
                markersize=3.2, linewidth=1.2)
        if has_itl2:
            ax.plot(g[lc], g["goodput_itl2_rps"], marker=s["marker"],
                    linestyle="--", color=s["color"],
                    markersize=3.0, linewidth=1.1)
        return _mark_low_n_panel(ax, g, g[lc], g["goodput_rps"])

    def finalize(ax):
        # A dense model whose ITL floors above both tiers has goodput ~= 0
        # everywhere; autoscaling those panels to their 0.0x req/s noise would
        # dress the absence of an SLO-compliant operating point up as signal.
        # A minimum y-span keeps such panels legibly flat at zero.
        ax.set_ylim(0, max(0.25, ax.get_ylim()[1]))

    def note(ax, msub):
        # A zero strict curve is a finding, not missing data. Keep the note
        # tier-specific: some dense checkpoints have zero strict goodput but a
        # small, measurable loose-tier operating range.
        strict_max = float(msub["goodput_rps"].max())
        if strict_max > 0:
            return
        loose_max = (float(msub["goodput_itl2_rps"].max())
                     if has_itl2 else 0.0)
        if loose_max > 0:
            text = f"strict = 0\nloose $\\leq$ {loose_max:.3f} req/s"
        else:
            text = "strict and loose\n= 0"
        ax.text(0.5, 0.62, text, transform=ax.transAxes,
                ha="center", va="center", fontsize=6.5,
                color="0.35", style="italic")

    _model_grid(df, "goodput_grid", out, fmts, panel,
                "Goodput under SLO (req/s)",
                finalize_panel=finalize, panel_note=note,
                extra_proxies=[strict, loose] if has_itl2 else [strict],
                compact_titles=True)


def fig_energy_grid(df, out, fmts):
    if not has(df, ["energy_per_token_j"]):
        print("  energy_grid: no power data in index, skipping")
        return
    head_only = []

    def panel(ax, g, lc, s, cfg):
        ax.plot(g[lc], g["energy_per_token_j"], marker=s["marker"],
                color=s["color"], markersize=3.2, linewidth=1.2)
        if head_only_power(cfg, g):
            head_only.append(cfg)
            mask = (~(g["power_nodes"] >= 2)) if "power_nodes" in g.columns \
                else pd.Series(True, index=g.index)
            ax.scatter(g.loc[mask, lc], g.loc[mask, "energy_per_token_j"],
                       marker="x", color="0.15", s=20, zorder=7)
        return False

    gpu_scope = ("power_scope" in df.columns
                 and df["power_scope"].dropna().eq("gpu_devices").all())
    note = (r"$\times$ power sampled on the head node only: per-token energy "
            "is a lower bound at those points.")
    _model_grid(df, "energy_grid", out, fmts, panel,
                "GPU energy per token (J)" if gpu_scope
                else "Sampled energy per token (J)",
                finalize_panel=lambda ax: ax.set_ylim(bottom=0),
                footnote=note if head_only else None)
    if head_only:
        print("  WARNING: energy_grid has head-node-only dual points; "
              "check power_nodes before quoting those numbers")


# ---- compact representative figure ------------------------------------------
def fig_operating_regimes(df, out, fmts):
    """Representative load curves for the three regimes behind the paper.

    The complete ten-model grids remain audit artifacts.  This two-by-three
    figure is the body-sized view: a fast quantized dense model, a large dense
    bandwidth-bound model, and a sparse MoE model.  Throughput and ITL share a
    concurrency axis, so the reader sees the saturation knee and its
    user-facing latency consequence without spending two full-page grid slots.
    """
    candidates = [
        ("llama3.1-8b-nvfp4", "Fast dense\nL3.1-8B NVFP4"),
        ("qwen3-32b", "Large dense\nQwen3-32B BF16"),
        ("nemotron3-nano-30b-a3b-nvfp4",
         "Sparse MoE\nN3-Nano-30B-A3B NVFP4"),
    ]
    mv = _model_values(df)
    chosen = [(model, title) for model, title in candidates
              if (mv == model).any()]
    if not chosen or not has(df, [_throughput_col(df), "mean_itl_p50"]):
        print("  operating_regimes: representative series unavailable, "
              "skipping")
        return

    tcol = _throughput_col(df)
    fig, axes = plt.subplots(2, len(chosen), figsize=(TEXT_W, 3.9),
                             squeeze=False, layout="none")
    used_cfgs = []
    for col, (model, title) in enumerate(chosen):
        msub = df[mv == model]
        axt, axl = axes[:, col]
        all_loads = []
        for cfg in configs_present(msub):
            g, lc, _ = group(msub, cfg)
            style = style_for(cfg, CONFIG_ORDER.index(cfg)
                              if cfg in CONFIG_ORDER else 0)
            axt.plot(g[lc], g[tcol], color=style["color"],
                     marker=style["marker"], markersize=3.4,
                     linewidth=1.2)
            axl.plot(g[lc], g["mean_itl_p50"] * 1e3,
                     color=style["color"],
                     marker=style["marker"], markersize=3.4,
                     linewidth=1.2)
            all_loads += g[lc].tolist()
            if cfg not in used_cfgs:
                used_cfgs.append(cfg)
        axt.set_title(title, fontsize=8, pad=3)
        axt.set_ylim(bottom=0)
        axl.set_yscale("log")
        axl.axhline(ITL_SLO_MS, color=SLO_COLOR, linestyle=":",
                    linewidth=0.9)
        axl.axhline(ITL_SLO2_MS, color=SLO_COLOR, linestyle="-.",
                    linewidth=0.9)
        clean_log_ticks(axl)
        _panel_load_axis(axt, all_loads)
        _panel_load_axis(axl, all_loads)
        axt.tick_params(labelbottom=False)
        axl.set_xlabel("Concurrent requests", fontsize=7.5)
        if col == 0:
            axt.set_ylabel("Throughput (tok/s)", fontsize=7.5)
            axl.set_ylabel("Median request-mean ITL (ms)", fontsize=7.5)

    proxies = [
        Line2D([], [], color=CONFIG_STYLE[c]["color"],
               marker=CONFIG_STYLE[c]["marker"], markersize=4,
               label=CONFIG_STYLE[c]["label"])
        for c in CONFIG_ORDER if c in used_cfgs
    ]
    proxies += [
        Line2D([], [], color=SLO_COLOR, linestyle=":", linewidth=1.0,
               label="strict ITL SLO (100 ms)"),
        Line2D([], [], color=SLO_COLOR, linestyle="-.", linewidth=1.0,
               label="loose ITL SLO (200 ms)"),
    ]
    fig.legend(handles=proxies, loc="upper center", ncol=len(proxies),
               fontsize=6.4, frameon=False, bbox_to_anchor=(0.5, -0.01),
               handlelength=1.8, columnspacing=0.9)
    fig.subplots_adjust(top=0.92, bottom=0.17, hspace=0.15)
    save(fig, "operating_regimes", out, fmts, tight=True)


# ---- single figures ----------------------------------------------------------
def fig_kv_capacity(df, out, fmts):
    """KV capacity per (model, config): maximum-length requests that fit.

    max_concurrency = total KV tokens / max_model_len, from the vLLM startup
    log. Doubling a model over two Sparks roughly doubles it (weights are
    split, freeing memory for KV) -- the mechanism behind TP=2 winning on the
    small models whose single-node ceiling is KV-bound. Log scale: the span is
    14.5 (Qwen3-32B single) to ~2000 (hybrid-cache Nemotron Nano dual).
    """
    if not has(df, ["max_concurrency"]):
        print("  kv_capacity: no max_concurrency in index, skipping")
        return
    models = models_by_size(df)
    mv = _model_values(df)
    configs = [c for c in CONFIG_ORDER if (df["config_label"] == c).any()]
    fig, ax = plt.subplots(figsize=(TEXT_W, 2.4))
    x = np.arange(len(models))
    width = min(0.8 / max(len(configs), 1), 0.26)
    maxlen = None
    for j, cfg in enumerate(configs):
        s = CONFIG_STYLE.get(cfg, dict(color="0.5", label=cfg))
        vals = []
        for m in models:
            sub = df[(mv == m) & (df["config_label"] == cfg)]
            v = sub["max_concurrency"].dropna()
            vals.append(float(v.iloc[0]) if len(v) else np.nan)
            ml = sub["max_model_len"].dropna() if "max_model_len" in sub else []
            maxlen = int(ml.iloc[0]) if len(ml) else maxlen
        off = (j - (len(configs) - 1) / 2) * width
        ax.bar(x + off, vals, width, color=s["color"], label=s["label"])
        for xi, v in zip(x + off, vals):
            if pd.notna(v):
                # stagger adjacent value labels: with 10 model groups the
                # bars are narrow enough that same-height neighbours
                # (e.g. Nano's 2,007 / 2,002) run together on one baseline
                ax.annotate(f"{v:,.0f}", (xi, v), textcoords="offset points",
                            xytext=(0, 1.5 + 5.0 * (j % 2)), ha="center",
                            fontsize=5.5, color="0.25")
    ax.set_yscale("log")
    clean_log_ticks(ax)
    ax.set_xticks(x)
    ax.set_xticklabels([meta_for(m)["pretty"] for m in models],
                       rotation=25, ha="right", fontsize=7)
    unit = f"{maxlen:,}-token" if maxlen else "max-length"
    ax.set_ylabel(f"KV capacity ({unit} requests)", fontsize=8)
    ax.grid(axis="x", visible=False)
    # legend above the axes: inside the frame it overlaps the tall Nano bars
    # and their value labels
    ax.legend(fontsize=7, ncol=len(configs), loc="lower left",
              bbox_to_anchor=(0, 1.01), borderaxespad=0)
    save(fig, "kv_capacity", out, fmts, tight=True)


def _bytes_fmt(v, _):
    for unit, div in (("GiB", 2 ** 30), ("MiB", 2 ** 20), ("KiB", 2 ** 10)):
        if v >= div:
            return f"{v / div:g} {unit}"
    return f"{int(v)} B"


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve_allreduce_paths(results_dir: str,
                             manifest_path: str | None = None) -> list[str]:
    """Select one reproducible set of all-reduce sweeps.

    The campaign manifest is authoritative when present: every selected file
    is checked for size and SHA-256 before plotting. A directory of CSVs
    without a manifest is rejected instead of silently selecting or mixing
    campaigns.
    """
    if manifest_path is None:
        candidate = os.path.join(results_dir, "campaign_manifest.json")
        manifest_path = candidate if os.path.exists(candidate) else None

    if manifest_path is not None:
        manifest_path = os.path.abspath(manifest_path)
        with open(manifest_path) as handle:
            manifest = json.load(handle)
        entries = manifest.get("artifacts", {}).get("allreduce", [])
        if not entries:
            raise ValueError(
                f"{manifest_path} contains no artifacts.allreduce entries")
        paths: list[str] = []
        for entry in entries:
            # Campaign manifests record artifact paths relative to the results
            # output directory, even when a copied manifest is supplied from
            # another location.
            path = os.path.abspath(os.path.join(results_dir, entry["path"]))
            if not os.path.isfile(path):
                raise FileNotFoundError(
                    f"manifest-sealed all-reduce file is missing: {path}")
            actual_bytes = os.path.getsize(path)
            expected_bytes = int(entry["bytes"])
            if actual_bytes != expected_bytes:
                raise ValueError(
                    f"size mismatch for {path}: manifest={expected_bytes}, "
                    f"actual={actual_bytes}")
            actual_sha = _sha256(path)
            expected_sha = str(entry["sha256"]).lower()
            if actual_sha != expected_sha:
                raise ValueError(
                    f"SHA-256 mismatch for {path}: "
                    f"manifest={expected_sha}, actual={actual_sha}")
            paths.append(path)
        print(f"  allreduce: verified {len(paths)} sweep(s) from "
              f"{manifest_path}")
        return paths

    paths = sorted(glob.glob(os.path.join(results_dir, "allreduce_*.csv")))
    if paths:
        raise ValueError(
            f"{results_dir} contains {len(paths)} all-reduce sweep(s) but no "
            "campaign manifest; pass --allreduce-manifest to select and "
            "verify a sealed set")
    return []


def fig_allreduce(df, out, fmts, results_dir, manifest_path=None):
    """Dual-node all-reduce microbenchmark: latency and bus bandwidth vs size.

    A transport diagnostic to compare with the TP=2 serving results. The
    campaign used NCCL over TCP sockets, and the shaded band marks plausible
    activation-sized payloads for the 8K-hidden Llama-70B sweep (c=1..128 at
    bf16). This supports a correlation, not a causal decomposition: there is no
    matched point-to-point pipeline-stage microbenchmark in the campaign.
    """
    paths = _resolve_allreduce_paths(results_dir, manifest_path)
    if not paths:
        print(f"  allreduce: no allreduce_*.csv under {results_dir}, skipping")
        return
    runs = []
    for path in paths:
        run = pd.read_csv(path).sort_values("bytes").copy()
        if not {"bytes", "avg_s"}.issubset(run.columns):
            raise ValueError(f"{path} is missing bytes or avg_s")
        run["bytes"] = pd.to_numeric(run["bytes"], errors="coerce")
        run["avg_s"] = pd.to_numeric(run["avg_s"], errors="coerce")
        if (run[["bytes", "avg_s"]].isna().any().any()
                or (run["bytes"] <= 0).any()
                or (run["avg_s"] <= 0).any()):
            raise ValueError(f"{path} contains nonpositive or nonnumeric data")
        if run["bytes"].duplicated().any():
            raise ValueError(f"{path} contains duplicate message sizes")
        # This is a two-rank all-reduce, for which NCCL's bus-bandwidth
        # correction 2*(n-1)/n is exactly one. Deriving from unrounded latency
        # preserves the 8--32 B points that the CSV's 0.001-GB/s formatting
        # rounds to zero.
        run["bw_exact"] = run["bytes"] / run["avg_s"] / 1e9
        runs.append(run)
    merged = pd.concat(runs).groupby("bytes", as_index=False).agg(
        avg_s=("avg_s", "mean"), avg_lo=("avg_s", "min"),
        avg_hi=("avg_s", "max"), bw=("bw_exact", "mean"),
        bw_lo=("bw_exact", "min"), bw_hi=("bw_exact", "max"))
    merged = merged.sort_values("bytes")
    color = "#0072B2"
    fig, (axt, axb) = plt.subplots(2, 1, figsize=(COL_W, 3.4), sharex=True)

    for run in runs:
        axt.plot(run["bytes"], run["avg_s"] * 1e6, color=color,
                 linewidth=0.7, alpha=0.25, zorder=1)
        axb.plot(run["bytes"], run["bw_exact"], color=color,
                 linewidth=0.7, alpha=0.25, zorder=1)
    axt.plot(merged["bytes"], merged["avg_s"] * 1e6, marker="o",
             markersize=3.0, linewidth=1.2, color=color, zorder=3)
    axt.fill_between(merged["bytes"], merged["avg_lo"] * 1e6,
                     merged["avg_hi"] * 1e6, color=color, alpha=0.15,
                     linewidth=0, zorder=2)
    axt.set_yscale("log")
    axt.set_ylabel("All-reduce latency (µs)", fontsize=8)

    axb.plot(merged["bytes"], merged["bw"], marker="o", markersize=3.0,
             linewidth=1.2, color=color, zorder=3)
    axb.fill_between(merged["bytes"], merged["bw_lo"], merged["bw_hi"],
                     color=color, alpha=0.15, linewidth=0, zorder=2)
    axb.set_yscale("log")
    axb.set_ylabel("NCCL bus bandwidth (GB/s)", fontsize=8)
    axb.set_xlabel("Message size", fontsize=8)

    # The largest payload is already on the plateau; using its mean avoids
    # cherry-picking a smaller-size noise maximum.
    peak = float(merged.iloc[-1]["bw"])
    axb.axhline(peak, color=SLO_COLOR, linestyle=":", linewidth=0.9)
    # Park the label in the empty lower-right corner, in axes coordinates so
    # it cannot drift back onto the data when the sweep range changes. Anchored
    # on the plateau line it ran along the dotted rule and through the rising
    # curve; one step down it still grazed the knee near 1 MiB.
    # axb.text(
    #     0.98, 0.06,
    #     f"NCCL/TCP mean plateau {peak:.1f} GB/s\n"
    #     f"({len(runs)} benchmark runs)",
    #     transform=axb.transAxes, ha="right", va="bottom",
    #     fontsize=6.8, color="0.25", linespacing=1.3)
    # axt.text(0.02, 0.78,
    #          f"TCP sockets\nmean of {len(runs)} benchmark runs; band=min–max",
    #          transform=axt.transAxes, ha="left", va="top",
    #          fontsize=6.6, color="0.25", linespacing=1.25)

    # Llama-3.3-70B decode all-reduce payloads: tokens_in_flight x hidden 8192
    # x 2 B (bf16 activations), c = 1 .. 128.
    lo, hi = 1 * 8192 * 2, 128 * 8192 * 2
    for ax in (axt, axb):
        ax.axvspan(lo, hi, color="0.75", alpha=0.25, linewidth=0)
    axt.annotate("estimated activation payloads\n(70B, c=1–128)",
                 xy=(math.sqrt(lo * hi), 1), xycoords=("data", "axes fraction"),
                 xytext=(0, -5), textcoords="offset points",
                 ha="center", va="top", fontsize=6.8, color="0.25")

    axb.set_xscale("log", base=2)
    ticks = [2 ** k for k in (3, 7, 11, 15, 19, 23, 27, 30)]
    axb.set_xticks([t for t in ticks
                    if merged["bytes"].min() <= t <= merged["bytes"].max()])
    axb.xaxis.set_major_formatter(FuncFormatter(_bytes_fmt))
    axb.xaxis.set_minor_formatter(NullFormatter())
    for ax in (axt, axb):
        ax.tick_params(labelsize=6.5)
    plt.setp(axb.get_xticklabels(), rotation=30, ha="right")
    save(fig, "allreduce", out, fmts)


# Transport hues (Okabe-Ito, CVD-safe) for the interconnect comparison; the
# collective (TP's all-reduce vs PP's p2p) is carried by linestyle+marker so
# neither dimension relies on colour alone.
TRANSPORT_STYLE = {
    "tcp":   dict(color="#0072B2", label="TCP sockets"),
    "roce":  dict(color="#D55E00", label="RoCE / RDMA"),
}
_TCP_TUNED_COLOR = "#009E73"
COLLECTIVE_STYLE = {
    "allreduce": dict(linestyle="-", marker="o", label="all-reduce (TP)"),
    "sendrecv":  dict(linestyle="--", marker="s", label="send/recv (PP)"),
}


def _transport_style(transport: str) -> dict:
    if transport in TRANSPORT_STYLE:
        return TRANSPORT_STYLE[transport]
    if str(transport).startswith("tcp-tuned"):
        return dict(color=_TCP_TUNED_COLOR, label=str(transport))
    return dict(color="0.5", label=str(transport))


def fig_interconnect(out, fmts, results_dir):
    """Transport x collective interconnect comparison from the extra microbench.

    Reads scripts/interconnect_extra_bench.sh output (interconnect_extra_*.csv:
    collective, transport, bytes, avg_s, ...). Overlays each transport (TCP vs
    RoCE vs tuned sockets, by hue) and each collective (all-reduce = the TP=2
    primitive, send/recv = the PP=2 primitive, by linestyle) on shared latency
    and bandwidth panels, so the paper can show directly (a) whether RoCE moves
    the plateau/floor the TP conclusion rests on, and (b) that PP's one p2p
    hand-off is far cheaper than TP's per-layer all-reduce. Draws nothing when
    no such CSV exists yet, so it is safe to call before the run.
    """
    paths = sorted(glob.glob(os.path.join(results_dir,
                                          "interconnect_extra_*.csv")))
    if not paths:
        print("  interconnect: no interconnect_extra_*.csv, skipping "
              "(run scripts/interconnect_extra_bench.sh under each transport)")
        return
    frames = []
    for p in paths:
        try:
            frames.append(pd.read_csv(p))
        except Exception as exc:
            print(f"  interconnect: skipping unreadable {p}: {exc}")
    df = pd.concat(frames, ignore_index=True) if frames else None
    if df is None or df.empty or not {"collective", "transport", "bytes",
                                      "avg_s"}.issubset(df.columns):
        print("  interconnect: CSVs present but columns unexpected, skipping")
        return
    agg = (df.groupby(["collective", "transport", "bytes"], as_index=False)
             .agg(avg_s=("avg_s", "mean"),
                  bw=("busbw_GBps", "mean")))

    fig, (axt, axb) = plt.subplots(2, 1, figsize=(COL_W, 3.4), sharex=True)
    transports = [t for t in ["tcp"] + sorted(
        x for x in agg["transport"].unique() if x != "tcp")]
    collectives = [c for c in ("allreduce", "sendrecv")
                   if c in set(agg["collective"])]
    for transport in transports:
        ts = _transport_style(transport)
        for coll in collectives:
            cs = COLLECTIVE_STYLE[coll]
            sub = agg[(agg["transport"] == transport)
                      & (agg["collective"] == coll)].sort_values("bytes")
            if sub.empty:
                continue
            axt.plot(sub["bytes"], sub["avg_s"] * 1e6, color=ts["color"],
                     linestyle=cs["linestyle"], marker=cs["marker"],
                     markersize=2.6, linewidth=1.1)
            bw = sub[sub["bw"] > 0]
            axb.plot(bw["bytes"], bw["bw"], color=ts["color"],
                     linestyle=cs["linestyle"], marker=cs["marker"],
                     markersize=2.6, linewidth=1.1)
    axt.set_yscale("log")
    axt.set_ylabel("One-hop latency (µs)", fontsize=8)
    axb.set_yscale("log")
    axb.set_ylabel("Bus bandwidth (GB/s)", fontsize=8)
    axb.set_xlabel("Message size", fontsize=8)
    axb.axhline(LINE_RATE_GBPS, color=SLO_COLOR, linestyle=":", linewidth=0.9)
    axb.annotate(f"200 GbE line rate ({LINE_RATE_GBPS:.0f} GB/s)",
                 xy=(float(agg["bytes"].min()) * 2, LINE_RATE_GBPS),
                 xytext=(0, -8), textcoords="offset points",
                 fontsize=6.0, color="0.25", va="top")
    # decode all-reduce payloads for the 70B sweep, as in fig_allreduce
    lo, hi = 1 * 8192 * 2, 128 * 8192 * 2
    for ax in (axt, axb):
        ax.axvspan(lo, hi, color="0.75", alpha=0.25, linewidth=0)
    axb.set_xscale("log", base=2)
    ticks = [2 ** k for k in (3, 7, 11, 15, 19, 23, 27, 30)]
    axb.set_xticks([t for t in ticks
                    if agg["bytes"].min() <= t <= agg["bytes"].max()])
    axb.xaxis.set_major_formatter(FuncFormatter(_bytes_fmt))
    axb.xaxis.set_minor_formatter(NullFormatter())
    for ax in (axt, axb):
        ax.tick_params(labelsize=6.5)
    plt.setp(axb.get_xticklabels(), rotation=30, ha="right")
    proxies = [Line2D([], [], color=_transport_style(t)["color"],
                      label=_transport_style(t)["label"]) for t in transports]
    proxies += [Line2D([], [], color="0.35", linestyle=COLLECTIVE_STYLE[c][
        "linestyle"], marker=COLLECTIVE_STYLE[c]["marker"], markersize=3,
        label=COLLECTIVE_STYLE[c]["label"]) for c in collectives]
    axt.legend(handles=proxies, fontsize=5.8, ncol=2, frameon=False,
               loc="upper left")
    save(fig, "interconnect", out, fmts)


# ---- peak table --------------------------------------------------------------
def write_peak_table(df, out) -> None:
    """Compact headline table: one row per (model, config) at its best load.

    The 152-point summary table is an artifact appendix; this is the table
    that fits in the paper body. Peak throughput is each config's best exact
    token-in-window rate over the sweep; latency columns are the unloaded
    (c=1) medians; the ITL column is the pooled token-gap summary and energy
    columns describe the peak-throughput point.
    """
    tcol = _throughput_col(df)
    energy = has(df, ["energy_per_token_j"])
    mv = _model_values(df)
    rows = []
    for model in models_by_size(df):
        msub = df[mv == model]
        for cfg in configs_present(msub):
            g, lc, _ = group(msub, cfg)
            if not g[tcol].notna().any():
                continue
            pk = g.loc[g[tcol].idxmax()]
            c1 = g[g[lc] == 1]
            c1 = c1.iloc[0] if len(c1) else None
            nodes = 2 if cfg in DUAL_NODE_CONFIGS else 1
            rows.append({
                "model": meta_for(model)["pretty"],
                "quant": meta_for(model)["quant"].upper(),
                "cfg": CONFIG_STYLE.get(cfg, {}).get("label", cfg),
                "peak": float(pk[tcol]),
                "at": pk[lc],
                "pernode": float(pk[tcol]) / nodes,
                "ttft1": (float(c1["ttft_p50"]) * 1e3
                          if c1 is not None and pd.notna(c1.get("ttft_p50"))
                          else np.nan),
                "itl1": (float(c1["itl_p50"]) * 1e3
                         if c1 is not None and pd.notna(c1.get("itl_p50"))
                         else np.nan),
                "jtok": (float(pk.get("energy_per_token_j", np.nan))
                         if energy else np.nan),
                "star": head_only_power(cfg, pd.DataFrame([pk])),
            })
    os.makedirs(out, exist_ok=True)

    headers = ["Model", "Prec.", "Config", "Peak tok/s", "@c",
               "tok/s/node", "TTFT@1 (ms)", "Pooled ITL@1 (ms)"]
    if energy:
        headers.append("J/tok@peak")
    md = ["| " + " | ".join(headers) + " |",
          "|---|---|---|" + "--:|" * (len(headers) - 3)]
    for r in rows:
        cells = [r["model"], r["quant"], r["cfg"], _fmt(r["peak"], ".0f"),
                 _fmt(r["at"], ".0f"), _fmt(r["pernode"], ".0f"),
                 _fmt(r["ttft1"], ".0f"), _fmt(r["itl1"], ".0f")]
        if energy:
            cells.append(f"{_fmt(r['jtok'], '.2f')}{'*' if r['star'] else ''}")
        md.append("| " + " | ".join(cells) + " |")
    if energy and any(r["star"] for r in rows):
        md += ["", "* head-node power only; J/tok is a lower bound."]
    path = os.path.join(out, "peak_table.md")
    with open(path, "w") as f:
        f.write("\n".join(md) + "\n")
    print(f"  wrote {path}")

    ecol = "r" if energy else ""
    ehead = r" & J/tok\textsuperscript{peak}" if energy else ""
    lines = [
        r"% Requires \usepackage{booktabs}.",
        r"\begin{tabular}{l l l r r r r r " + ecol + "}",
        r"\toprule",
        r"Model & Prec. & Config & Peak tok/s & @$c$ & tok/s/node & "
        r"TTFT$^{c=1}_{50}$ (ms) & Pooled ITL$^{c=1}_{50}$ (ms)"
        + ehead + r" \\",
        r"\midrule",
    ]
    last_model = None
    for r in rows:
        if last_model is not None and r["model"] != last_model:
            lines.append(r"\addlinespace")
        last_model = r["model"]
        eic = ""
        if energy:
            star = r"\rlap{$^*$}" if r["star"] else ""
            eic = f" & {_fmt(r['jtok'], '.2f')}{star}"
        lines.append(
            f"{r['model']} & {r['quant']} & {r['cfg']} & "
            f"{_fmt(r['peak'], '.0f')} & {_fmt(r['at'], '.0f')} & "
            f"{_fmt(r['pernode'], '.0f')} & {_fmt(r['ttft1'], '.0f')} & "
            f"{_fmt(r['itl1'], '.0f')}{eic} \\\\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    if energy and any(r["star"] for r in rows):
        lines.append(r"% $^*$ head-node power only; J/tok is a lower bound.")
    path = os.path.join(out, "peak_table.tex")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"  wrote {path}")


def _short_name(model: str) -> str:
    """Column-width model names, matching the parallelism-scaling y labels."""
    name = meta_for(model)["pretty"]
    return (name.replace("Llama-3.1", "L3.1")
                .replace("Llama-3.3", "L3.3")
                .replace("Nemotron3-", "N3-"))


def _table_name(model: str) -> str:
    """_short_name, minus the active-parameter suffix carried by the Type
    column. Table I is single-column, where ``N3-Super-120B-A12B'' alone is
    wide enough to push the tabular past the column and into the gutter."""
    return re.sub(r"-A\d+B$", "", _short_name(model))


def write_models_table(df, out) -> None:
    """Table I: one row per checkpoint -- type, precision, weights, KV capacity.

    The single-column condensation of capacity_table.tex the paper asks for:
    full-model weight footprint (single-node worker weight; 2x the per-worker
    weight when the checkpoint never ran single-node) and KV capacity in
    maximum-length requests per placement, straight from the vLLM startup log
    fields captured in the index.
    """
    mv = _model_values(df)
    # Single-column table: the spanning header must not be wider than the three
    # narrow columns beneath it, or the tabular is stretched to fit the header
    # rather than the data. Request length lives in the caption.
    lines = [
        r"% Requires \usepackage{booktabs}. Generated by plot_paper.py.",
        r"\begin{tabular}{@{}l l l r r r r@{}}",
        r"\toprule",
        r"Model & Type & Prec. & Wts & \multicolumn{3}{c}{KV cap.} \\",
        r"\cmidrule(lr){5-7}",
        r" & & & (GiB) & $1\times$ & TP & PP \\",
        r"\midrule",
    ]
    md = ["| Model | Type | Prec. | Wts (GiB) | KV 1x | KV TP2 | KV PP2 |",
          "|---|---|---|--:|--:|--:|--:|"]
    for model in models_by_size(df):
        msub = df[mv == model]
        meta = meta_for(model)
        arch = ("MoE %.0fB" % meta["active_b"] if meta["arch"] == "moe"
                else "dense")
        weight = np.nan
        cap = {}
        for cfg in configs_present(msub):
            g, _, _ = group(msub, cfg)
            row = g.iloc[0]
            w = row.get("worker_weight_gb", np.nan)
            if cfg == "single" and pd.notna(w):
                weight = float(w)
            elif pd.isna(weight) and pd.notna(w):
                weight = 2.0 * float(w)   # dual-only checkpoint: whole model
            mc = row.get("max_concurrency", np.nan)
            cap[cfg] = float(mc) if pd.notna(mc) else np.nan
        # max_concurrency is a capacity bound, so round down rather than to the
        # nearest integer: rounding up would claim that a fractional request
        # fits in the cache.
        capacity_cells = []
        for cfg in ("single", "tp2", "pp2"):
            value = cap.get(cfg, np.nan)
            capacity_cells.append(
                "--" if pd.isna(value) else str(math.floor(value))
            )
        cells = [_table_name(model), arch, meta["quant"].upper(),
                 _fmt(weight, ".1f")] + capacity_cells
        lines.append(" & ".join(cells) + r" \\")
        md.append("| " + " | ".join(cells) + " |")
    lines += [r"\bottomrule", r"\end{tabular}"]
    for name, text in (("models_table.tex", "\n".join(lines)),
                       ("models_table.md", "\n".join(md))):
        path = os.path.join(out, name)
        with open(path, "w") as f:
            f.write(text + "\n")
        print(f"  wrote {path}")


def write_headline_table(df, out) -> None:
    """Table II: the nine complete placement families in one table.

    Peak tok/s per placement, dual-node throughput ratios, and J/token at
    each placement's own peak normalized to single -- the numeric twin of the
    parallelism-scaling figure, with the medians the abstract quotes as the
    final row.
    """
    tcol = _throughput_col(df)
    mv = _model_values(df)
    rows = []
    for model in models_by_size(df):
        msub = df[mv == model]
        if not all(c in configs_present(msub) for c in CONFIG_ORDER):
            continue
        pk = {}
        for cfg in CONFIG_ORDER:
            g, _, _ = group(msub, cfg)
            pk[cfg] = g.loc[g[tcol].idxmax()]
        s, t, p = (float(pk[c][tcol]) for c in CONFIG_ORDER)
        es, et, ep = (float(pk[c].get("energy_per_token_j", np.nan))
                      for c in CONFIG_ORDER)
        rows.append({
            "model": _short_name(model), "quant": meta_for(model)["quant"].upper(),
            "s": s, "t": t, "p": p, "t1": t / s, "p1": p / s, "pt": p / t,
            "et1": et / es, "ep1": ep / es,
        })
    if not rows:
        print("  headline_table: no complete three-placement family, skipping")
        return
    med = {k: float(np.median([r[k] for r in rows]))
           for k in ("t1", "p1", "pt", "et1", "ep1")}
    lines = [
        r"% Requires \usepackage{booktabs}. Generated by plot_paper.py.",
        r"\begin{tabular}{l l r r r r r r r r}",
        r"\toprule",
        r" & & \multicolumn{3}{c}{Peak tok/s} & "
        r"\multicolumn{3}{c}{Throughput ratio} & "
        r"\multicolumn{2}{c}{J/tok at peak / single} \\",
        r"\cmidrule(lr){3-5}\cmidrule(lr){6-8}\cmidrule(lr){9-10}",
        r"Model & Prec. & $1\times$ & TP=2 & PP=2 & TP/$1\times$ & "
        r"PP/$1\times$ & PP/TP & TP=2 & PP=2 \\",
        r"\midrule",
    ]
    md = ["| Model | Prec. | 1x | TP2 | PP2 | TP/1x | PP/1x | PP/TP "
          "| E TP2 | E PP2 |", "|---|---|" + "--:|" * 8]
    for r in rows:
        cells = [r["model"], r["quant"], _fmt(r["s"], ".0f"),
                 _fmt(r["t"], ".0f"), _fmt(r["p"], ".0f"),
                 _fmt(r["t1"], ".2f"), _fmt(r["p1"], ".2f"),
                 _fmt(r["pt"], ".2f"), _fmt(r["et1"], ".2f"),
                 _fmt(r["ep1"], ".2f")]
        lines.append(" & ".join(cells) + r" \\")
        md.append("| " + " | ".join(cells) + " |")
    med_cells = ["Median", "", "", "", "", _fmt(med["t1"], ".2f"),
                 _fmt(med["p1"], ".2f"), _fmt(med["pt"], ".2f"),
                 _fmt(med["et1"], ".2f"), _fmt(med["ep1"], ".2f")]
    lines += [r"\midrule", " & ".join(med_cells) + r" \\",
              r"\bottomrule", r"\end{tabular}"]
    md.append("| **Median** |  |  |  |  | %s | %s | %s | %s | %s |" % (
        _fmt(med["t1"], ".2f"), _fmt(med["p1"], ".2f"), _fmt(med["pt"], ".2f"),
        _fmt(med["et1"], ".2f"), _fmt(med["ep1"], ".2f")))
    for name, text in (("headline_table.tex", "\n".join(lines)),
                       ("headline_table.md", "\n".join(md))):
        path = os.path.join(out, name)
        with open(path, "w") as f:
            f.write(text + "\n")
        print(f"  wrote {path}")


# ---- main --------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Generate the complete PMBS26 paper figure/table set")
    ap.add_argument("--index", default="results/index.csv")
    ap.add_argument("--out", default="figs/paper")
    ap.add_argument("--format", default="both", choices=["pdf", "png", "both"])
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument(
        "--allreduce-manifest",
        help="campaign manifest whose artifacts.allreduce entries select and "
             "verify the sweeps (default: campaign_manifest.json beside "
             "--index when present)")
    ap.add_argument("--sans", action="store_true",
                    help="keep the sans-serif font (default is the IEEE "
                         "Times-like serif)")
    ap.add_argument(
        "--allow-incomplete", action="store_true",
        help="diagnostic only: plot an index with missing loads/repeats")
    ap.add_argument(
        "--drop-incomplete", action="store_true",
        help="EXCLUDE any (model, config) series whose load rectangle is "
             "still incomplete instead of failing: for mid-campaign paper "
             "snapshots, a partial series must not fake a peak at the "
             "highest load it happens to have reached")
    args = ap.parse_args()

    fmts = ["pdf", "png"] if args.format == "both" else [args.format]
    set_rc(args.dpi, serif=not args.sans)
    _publication_rc()
    df = load_index(args.index)
    if args.drop_incomplete:
        keep = []
        for (model, cfg), g in df.groupby(["model_label", "config_label"]):
            expected = set()
            for raw in g.get("expected_concurrencies", pd.Series(dtype=str)):
                expected |= {int(t) for t in str(raw or "").split()
                             if t.isdigit()}
            observed = set(pd.to_numeric(g["concurrency"],
                                         errors="coerce").dropna().astype(int))
            if expected and observed != expected:
                print(f"  drop-incomplete: excluding {model}/{cfg} "
                      f"({len(observed)}/{len(expected)} loads measured)")
                continue
            keep.append(g)
        df = pd.concat(keep, ignore_index=True)
    models = models_present(df)
    if len(models) < 2:
        sys.exit("the paper set needs a multi-model index; use "
                 "plot_results.py for a single model")
    print(f"loaded {len(df)} rows, {len(models)} models, "
          f"configs: {', '.join(configs_present(df))}")
    df = collapse_repeats(df, allow_incomplete=args.allow_incomplete)
    report_quality(df)
    warn_low_n(df)

    print("paper grids")
    fig_operating_regimes(df, args.out, fmts)
    fig_throughput_grid(df, args.out, fmts)
    fig_ttft_grid(df, args.out, fmts)
    fig_itl_grid(df, args.out, fmts)
    fig_goodput_grid(df, args.out, fmts)
    fig_energy_grid(df, args.out, fmts)
    print("mechanism figures")
    fig_kv_capacity(df, args.out, fmts)
    fig_allreduce(df, args.out, fmts, os.path.dirname(args.index) or ".",
                  args.allreduce_manifest)
    fig_interconnect(args.out, fmts, os.path.dirname(args.index) or ".")
    fig_prefill_scaling(df, args.out, fmts,
                        os.path.dirname(args.index) or ".")
    print("cross-model trends")
    fig_parallelism_scaling(df, args.out, fmts)
    fig_dense_vs_moe(df, args.out, fmts)
    fig_precision_ladder(df, args.out, fmts)
    fig_efficiency_frontier(df, args.out, fmts)
    fig_scale_trend(df, args.out, fmts)
    # fig_model_comparison labels its bars with the index's model values;
    # hand it a size-ordered copy carrying the curated pretty names so its
    # axis matches the rest of the paper set.
    mv = _model_values(df)
    pretty = pd.concat([df[mv == m] for m in models_by_size(df)]).copy()
    pretty["model_label"] = _model_values(pretty).map(
        lambda m: _panel_title(m))
    fig_model_comparison(pretty, args.out, fmts)
    print("tables")
    write_peak_table(df, args.out)
    write_models_table(df, args.out)
    write_headline_table(df, args.out)
    write_summary_table(df, args.out)
    write_capacity_table(df, args.out)
    print("done")


if __name__ == "__main__":
    main()
