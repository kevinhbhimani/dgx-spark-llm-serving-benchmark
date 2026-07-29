#!/usr/bin/env python3
"""Cross-model trend figures for the PMBS26 paper, from a multi-model index.

Companion to plot_results.py (which draws the per-model figure sets and the
peak-bar model comparison). This module draws the figures whose subject is a
*trend across models* rather than one model's load sweep:

  parallelism_scaling  dual-node speedup over single per model (TP=2 and PP=2
                       maxima normalized to the single-Spark maximum), PP=2
                       relative to TP=2, and energy at a baseline-selected
                       matched concurrency.
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
import math
import os
import re
import sys
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patheffects import withStroke
from matplotlib.ticker import FuncFormatter, NullFormatter
from matplotlib.transforms import Bbox, blended_transform_factory

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
    "llama3.1-8b": dict(
        pretty="Llama-3.1-8B", arch="dense", quant="bf16", nvidia=False,
        params_b=8.0, active_b=8.0, ladder_base="llama3.1-8b"),
    "llama3.1-8b-fp8": dict(
        pretty="Llama-3.1-8B FP8", arch="dense", quant="fp8", nvidia=True,
        params_b=8.0, active_b=8.0, ladder_base="llama3.1-8b"),
    "llama3.1-8b-nvfp4": dict(
        pretty="Llama-3.1-8B NVFP4", arch="dense", quant="nvfp4", nvidia=True,
        params_b=8.0, active_b=8.0, ladder_base="llama3.1-8b"),
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


def gpu_power_scope(df: pd.DataFrame) -> bool:
    """True when every power sample in the index is GPU-device scope, so axis
    labels can say "GPU energy"/"GPU watt" instead of the vaguer "sampled"."""
    return ("power_scope" in df.columns
            and df["power_scope"].dropna().eq("gpu_devices").all())


def clean_log_ticks(ax, axis: str = "y", max_ticks: int = 6) -> None:
    """Round-number major ticks for a log axis, replacing scientific notation.

    Log axes spanning less than a couple of decades pick up matplotlib's
    labeled minor ticks in 2x10^2-style notation, which is noisy at paper
    sizes and inconsistent from panel to panel. Mantissa sets are tried from
    densest to sparsest (1-2-5, then 1-3, then decades) and the first that
    fits max_ticks wins; labels are plain numbers. Axes whose span defeats
    even decade ticks keep the defaults.
    """
    lo, hi = ax.get_ylim() if axis == "y" else ax.get_xlim()
    if not (lo > 0 and hi > lo):
        return
    ticks: list[float] = []
    for mants in ((1, 2, 5), (1, 3), (1,)):
        ticks = [m * 10.0 ** e
                 for e in range(math.floor(math.log10(lo)),
                                math.ceil(math.log10(hi)) + 1)
                 for m in mants]
        ticks = [t for t in ticks if lo <= t <= hi]
        if len(ticks) <= max_ticks:
            break
    if len(ticks) < 2:
        return
    which = ax.yaxis if axis == "y" else ax.xaxis
    which.set_ticks(ticks)
    which.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    which.set_minor_formatter(NullFormatter())
    (ax.set_ylim if axis == "y" else ax.set_xlim)(lo, hi)


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


def _name_point(ax, x, y, label):
    """Model-name annotation with a white halo so it survives grid/marker
    crossings. Scatter figures call this once per model (not per point) and
    pass the returned texts to _spread_labels() to resolve collisions."""
    return ax.annotate(label, (x, y), textcoords="offset points", xytext=(3, 3),
                       fontsize=5.5, color="0.25", zorder=7,
                       path_effects=[withStroke(linewidth=1.5,
                                                foreground="white")])


def _spread_labels(fig, ax, texts) -> None:
    """Resolve point-label collisions by flipping and nudging (adjustText-lite).

    With 19 (model, config) peaks the dense mid-cluster overlaps several
    default up-right labels. Text extents exist only after a draw, so render
    once, then (1) right-anchor any label that would run past the axes' right
    edge and (2) walk labels left to right, pushing each one down by the
    pixel overlap with every already-placed label and with every data marker
    (a label sitting on someone else's point misattributes it). Offsets are
    in points, so pixel gaps are converted via the renderer dpi.
    """
    if not texts:
        return
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    px_to_pt = 72.0 / fig.dpi
    ax_right = ax.get_window_extent(renderer).x1
    for t in texts:
        if t.get_window_extent(renderer).x1 > ax_right:
            t.set_ha("right")
            dx, dy = t.get_position()
            t.set_position((-abs(dx), dy))
    pad = 3.5  # px half-extent of a scatter marker
    markers = [Bbox([[x - pad, y - pad], [x + pad, y + pad]])
               for coll in ax.collections
               for x, y in ax.transData.transform(coll.get_offsets())]
    placed = []
    for t in sorted(texts, key=lambda t: t.get_window_extent(renderer).x0):
        bb = t.get_window_extent(renderer)
        guard = 0
        while any(bb.overlaps(p) for p in placed + markers) and guard < 20:
            worst = max(bb.y1 - p.y0 for p in placed + markers
                        if bb.overlaps(p))
            dx, dy = t.get_position()
            t.set_position((dx, dy - (worst + 2) * px_to_pt))
            bb = t.get_window_extent(renderer)
            guard += 1
        placed.append(bb)


# ---- figures -----------------------------------------------------------------
def fig_parallelism_scaling(df, out, fmts):
    """Compact, paper-scale summary of the second-node trade-off.

    Each row is one deployment recipe. The three panels answer three separate
    questions without crowded, rotated bar labels: whether TP/PP beat one
    Spark, how much PP beats TP, and whether the second node changes energy per
    token at one matched concurrency. Throughput ratios compare each
    configuration's maximum exact token-in-window throughput for c <= 128.
    Energy selects the concurrency maximizing the single-Spark strict goodput
    and compares every placement at that same c.
    """
    tcol = _throughput_col(df)
    models = models_by_size(df)
    speed, ratio, energy = {}, {}, {}
    for model in models:
        peaks = {
            cfg: _peak(df, model, cfg, tcol)
            for cfg in ("single", "tp2", "pp2")
        }
        vals = {
            cfg: (float(row[tcol]) if row is not None else np.nan)
            for cfg, row in peaks.items()
        }
        if pd.notna(vals["single"]) and (
                pd.notna(vals["tp2"]) or pd.notna(vals["pp2"])):
            speed[model] = (
                vals["tp2"] / vals["single"],
                vals["pp2"] / vals["single"],
            )
        if pd.notna(vals["tp2"]) and pd.notna(vals["pp2"]):
            ratio[model] = vals["pp2"] / vals["tp2"]

        single_rows = _series_rows(df, model, "single")
        if (has(df, ["energy_per_token_j", "goodput_rps"])
                and len(single_rows)
                and "concurrency" in single_rows.columns
                and single_rows["goodput_rps"].notna().any()):
            best_idx = single_rows["goodput_rps"].idxmax()
            best_single = single_rows.loc[best_idx]
            if (float(best_single["goodput_rps"]) > 0
                    and pd.notna(best_single.get("energy_per_token_j",
                                                 np.nan))):
                matched_c = float(best_single["concurrency"])
                joules = {"single": float(
                    best_single["energy_per_token_j"])}
                for cfg in ("tp2", "pp2"):
                    rows = _series_rows(df, model, cfg)
                    loads = pd.to_numeric(
                        rows.get("concurrency", pd.Series(dtype=float)),
                        errors="coerce")
                    match = rows[np.isclose(loads, matched_c)]
                    joules[cfg] = (
                        float(match.iloc[0]["energy_per_token_j"])
                        if len(match)
                        and pd.notna(match.iloc[0].get(
                            "energy_per_token_j", np.nan))
                        else np.nan
                    )
                if pd.notna(joules["tp2"]) or pd.notna(joules["pp2"]):
                    energy[model] = (
                        joules["tp2"] / joules["single"],
                        joules["pp2"] / joules["single"],
                    )

    shown = [
        model for model in models
        if model in speed or model in ratio or model in energy
    ]
    if not shown:
        print("  parallelism_scaling: no comparable placement series, "
              "skipping")
        return

    def short_name(model: str) -> str:
        meta = meta_for(model)
        name = (meta["pretty"].replace("Llama-3.1", "L3.1")
                              .replace("Llama-3.3", "L3.3")
                              .replace("Nemotron3-", "N3-"))
        quant = meta["quant"].upper()
        return name if quant.lower() in name.lower() else f"{name} {quant}"

    y = np.arange(len(shown))
    fig, axes = plt.subplots(
        1, 3, figsize=(TEXT_W, 3.55), sharey=True, layout="none",
        gridspec_kw={"width_ratios": (1.35, 0.92, 1.18), "wspace": 0.10},
    )
    axt, axp, axe = axes
    tp_style, pp_style = CONFIG_STYLE["tp2"], CONFIG_STYLE["pp2"]

    def paired_points(ax, records):
        for yi, model in zip(y, shown):
            values = records.get(model)
            if values is None:
                continue
            tp, pp = values
            finite = [v for v in (tp, pp) if pd.notna(v)]
            if len(finite) == 2:
                ax.plot(finite, [yi, yi], color="0.78", linewidth=1.0,
                        zorder=1)
            if pd.notna(tp):
                ax.scatter(tp, yi, color=tp_style["color"],
                           marker=tp_style["marker"], s=26, zorder=3)
            if pd.notna(pp):
                ax.scatter(pp, yi, color=pp_style["color"],
                           marker=pp_style["marker"], s=30, zorder=3)

    paired_points(axt, speed)
    axt.axvline(1.0, color="0.35", linewidth=0.8)
    axt.axvline(2.0, color="0.45", linewidth=0.8, linestyle="--")
    axt.set_xlim(0.45, 2.08)
    # Name both reference rules in the panel. Unlabelled, the dashed line at
    # 2.0 reads as an axis artifact rather than the linear-scaling ideal that
    # no placement reaches -- which is the point of drawing it. Blended
    # transform: x in data units so a label tracks its rule, y in axes units
    # so it sits just inside the bottom spine whichever way the model axis
    # happens to be inverted.
    ref_tr = blended_transform_factory(axt.transData, axt.transAxes)
    for xv, text in ((1.0, "parity"), (2.0, "ideal 2×")):
        axt.text(xv, 0.015, text, transform=ref_tr, rotation=90,
                 ha="right", va="bottom", fontsize=5.8, color="0.35",
                 zorder=6,
                 path_effects=[withStroke(linewidth=1.6,
                                          foreground="white")])
    axt.set_xlabel("Dual / single maximum")
    tp_speed = [v[0] for v in speed.values() if pd.notna(v[0])]
    pp_speed = [v[1] for v in speed.values() if pd.notna(v[1])]
    axt.set_title(
        "(a) Maximum measured, $c\\leq128$\n"
        f"median TP={np.median(tp_speed):.2f}×, "
        f"PP={np.median(pp_speed):.2f}×",
        fontsize=8,
    )

    for yi, model in zip(y, shown):
        value = ratio.get(model)
        if value is None:
            continue
        # Deliberately a diamond, not PP's triangle: this panel plots a
        # PP-relative-to-TP ratio, which is a different quantity from the
        # PP series in panels (a) and (c). Keeping PP's hue ties it to the
        # placement; changing the shape stops it being read as the same
        # measurement. It is left out of the shared legend for the same
        # reason -- the caption names it.
        axp.scatter(value, yi, color=pp_style["color"], marker="D",
                    s=24, zorder=3)
        # Clear the marker before the text starts: s=24 pt^2 is ~4.9 pt wide,
        # so a 3 pt offset from the centre left the label touching the
        # diamond. Offset past the half-width plus a gap, and carry the same
        # white halo as _name_point so a label never reads as part of a mark.
        axp.annotate(f"{value:.2f}×", (value, yi),
                     textcoords="offset points", xytext=(7, 0),
                     ha="left", va="center", fontsize=6, color="0.25",
                     zorder=7,
                     path_effects=[withStroke(linewidth=1.5,
                                              foreground="white")])
    axp.axvline(1.0, color="0.35", linewidth=0.8)
    ratio_values = list(ratio.values())
    # Headroom for the widest label at the rightmost point (offset + ~5 chars).
    axp.set_xlim(0.95, max(1.75, max(ratio_values, default=1.0) * 1.26))
    axp.set_xlabel("PP maximum / TP maximum")
    axp.set_title(
        "(b) PP versus TP maximum\n"
        f"median={np.median(ratio_values):.2f}×",
        fontsize=8,
    )

    if energy:
        paired_points(axe, energy)
        tp_energy = [v[0] for v in energy.values() if pd.notna(v[0])]
        pp_energy = [v[1] for v in energy.values() if pd.notna(v[1])]
        all_energy = tp_energy + pp_energy
        axe.axvline(1.0, color="0.35", linewidth=0.8)
        axe.set_xlim(min(0.85, min(all_energy) * 0.96),
                     max(1.25, max(all_energy) * 1.08))
        axe.set_xlabel("J/token / single at matched $c$")
        n_energy = sum(
            pd.notna(tp) and pd.notna(pp)
            for tp, pp in energy.values()
        )
        axe.set_title(
            "(c) Energy at baseline-SLO load\n"
            f"median TP={np.median(tp_energy):.2f}×, "
            f"PP={np.median(pp_energy):.2f}× ($n$={n_energy})",
            fontsize=8,
        )
    else:
        axe.axis("off")
        axe.text(0.5, 0.5, "no energy data", transform=axe.transAxes,
                 ha="center", va="center", fontsize=8, color="0.4")

    axt.set_yticks(y)
    axt.set_yticklabels([short_name(model) for model in shown], fontsize=7)
    axt.invert_yaxis()
    for ax in axes:
        if not ax.axison:
            continue
        ax.grid(axis="y", visible=False)
        ax.tick_params(labelsize=6.5)
        ax.spines["left"].set_visible(False)
        ax.tick_params(axis="y", length=0)
    for ax in (axp, axe):
        ax.tick_params(labelleft=False)

    fig.legend(
        handles=[
            Line2D([], [], color=tp_style["color"],
                   marker=tp_style["marker"], linestyle="none",
                   label=tp_style["label"]),
            Line2D([], [], color=pp_style["color"],
                   marker=pp_style["marker"], linestyle="none",
                   label=pp_style["label"]),
        ],
        loc="upper center", ncol=2, frameon=False, fontsize=7,
        bbox_to_anchor=(0.5, -0.01),
    )
    fig.subplots_adjust(top=0.90, bottom=0.18)
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
            axr.set_ylabel("GPU energy per token (J)" if gpu_power_scope(df)
                           else "Sampled energy per token (J)")
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
        # Small multiples, one column per precision, rather than one panel with
        # precision on hue. Hue is the paper's placement channel everywhere
        # else (CONFIG_STYLE); spending it on precision here would make the
        # same three colors mean two different things across figures, and the
        # blue/vermillion/green trio leaves no CVD-safe room for a second
        # 3-hue set anyway (a violet ramp collides with the config blue at
        # protan dE 3.6). Precision becomes the column, so every curve in the
        # paper keeps its placement color.
        ncol = len(members)
        nrow = 2 if energy_ok else 1
        # 1.62 in per row is the floor at which an eight-tick concurrency axis
        # and a 0-based y axis both stay legible at 10 pt; the figure competes
        # for a strict 8-page budget that counts figures.
        fig, axes = plt.subplots(
            nrow, ncol, figsize=(TEXT_W, 1.62 * nrow + 0.5), squeeze=False,
            sharex=True, sharey="row", layout="none")
        all_loads, mode = [], "closed"
        used_cfgs = []
        for col, model in enumerate(members):
            quant = meta_for(model)["quant"]
            msub = df[mv == model]
            axt = axes[0][col]
            axe = axes[1][col] if energy_ok else None
            for cfg in configs_present(msub):
                style = CONFIG_STYLE.get(cfg, dict(color="0.3", marker="D"))
                g, lc, mode = group(msub, cfg)
                if cfg not in used_cfgs:
                    used_cfgs.append(cfg)
                axt.plot(g[lc], g[tcol], marker=style["marker"],
                         markersize=3.4, linewidth=1.2, color=style["color"])
                draw_band(axt, g, g[lc], tcol, style["color"])
                if axe is not None and g["energy_per_token_j"].notna().any():
                    axe.plot(g[lc], g["energy_per_token_j"],
                             marker=style["marker"], markersize=3.4,
                             linewidth=1.2, color=style["color"])
                    draw_band(axe, g, g[lc], "energy_per_token_j",
                              style["color"])
                all_loads += g[lc].tolist()
            axt.set_title(PRECISION_STYLE[quant]["label"], fontsize=8, pad=3)
        # Rows share a y axis on purpose -- reading the precision jump off the
        # figure is the point -- so the limits have to be set once, after every
        # column is drawn, from the maximum across the row. Setting them per
        # column froze each shared axis at the first column's autoscale and
        # clipped the faster rungs straight off the top.
        for row in axes:
            top = max((line.get_ydata().max() for ax in row
                       for line in ax.get_lines() if len(line.get_ydata())),
                      default=1.0)
            row[0].set_ylim(0, float(top) * 1.08)
        for ax in axes[-1]:
            set_load_axis(ax, all_loads, mode)
            ax.set_xlabel("Concurrent requests", fontsize=7.5)
        if energy_ok:
            for ax in axes[0]:
                ax.tick_params(labelbottom=False)
        axes[0][0].set_ylabel("Output token\nthroughput (tok/s)", fontsize=7.5)
        if energy_ok:
            axes[1][0].set_ylabel(
                "GPU energy\nper token (J)" if gpu_power_scope(df)
                else "Sampled energy\nper token (J)", fontsize=7.5)
        for row in axes:
            for ax in row:
                ax.tick_params(labelsize=7)
        proxies = [Line2D([], [], color=CONFIG_STYLE[c]["color"],
                          marker=CONFIG_STYLE[c]["marker"], markersize=4,
                          label=CONFIG_STYLE[c]["label"])
                   for c in CONFIG_ORDER if c in used_cfgs]
        fig.legend(handles=proxies, loc="upper center", ncol=len(proxies),
                   fontsize=6.8, frameon=False, bbox_to_anchor=(0.5, 0.03),
                   handlelength=1.8, columnspacing=1.2)
        fig.subplots_adjust(top=0.91, bottom=0.20, left=0.085, right=0.99,
                            hspace=0.16, wspace=0.10)
        # Always suffix by base slug so a figure's filename is stable whether
        # the index carries one ladder or several: adding a second base must
        # not rename the first out from under the paper's \includegraphics.
        save(fig, f"precision_ladder_{_slug(base)}", out, fmts, tight=True)


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
    labels = []
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
            labels.append(_name_point(ax, x, y, meta_for(model)["pretty"]))
    if not seen_classes:
        plt.close(fig)
        print("  efficiency_frontier: no peak rows with power data, skipping")
        return
    ax.set_xscale("log")
    # headroom so the per-point name labels stay inside the axes
    ax.margins(x=0.15, y=0.10)
    ax.set_xlabel("Peak output token throughput (tok/s)")
    ax.set_ylabel("Tokens per GPU watt (tok/s/W)" if gpu_power_scope(df)
                  else "Tokens per sampled watt (tok/s/W)")
    ax.set_ylim(bottom=0)
    clean_log_ticks(ax, axis="x")
    proxies = [Line2D([], [], linestyle="none", marker="o",
                      color=CLASS_STYLE[c]["color"],
                      label=CLASS_STYLE[c]["label"])
               for c in CLASS_STYLE if c in seen_classes]
    proxies += [Line2D([], [], linestyle="none", marker=CONFIG_STYLE[c]["marker"],
                       color="0.45", label=CONFIG_STYLE[c]["label"])
                for c in CONFIG_ORDER if c in seen_cfgs]
    ax.legend(handles=proxies, fontsize=6.5)
    _spread_labels(fig, ax, labels)
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
    labels = []
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
            labels.append(_name_point(ax, x, y, meta_for(model)["pretty"]))
    if not seen_cfgs:
        plt.close(fig)
        print("  scale_trend: no peak rows with weight_gb, skipping")
        return
    ax.set_xscale("log", base=2)
    ax.set_yscale("log")
    ax.margins(x=0.15, y=0.10)
    ax.set_xlabel("Model weight footprint (GiB)")
    ax.set_ylabel("Peak output token throughput (tok/s)")
    # plain numbers on both log axes: 16/32/64 GiB, not 2^k tok/s powers
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    ax.xaxis.set_minor_formatter(NullFormatter())
    clean_log_ticks(ax, axis="y")
    proxies = [Line2D([], [], linestyle="none",
                      marker=CONFIG_STYLE[c]["marker"],
                      color=CONFIG_STYLE[c]["color"],
                      label=CONFIG_STYLE[c]["label"])
               for c in CONFIG_ORDER if c in seen_cfgs]
    ax.legend(handles=proxies, fontsize=6.5)
    _spread_labels(fig, ax, labels)
    save(fig, "scale_trend", out, fmts)


# ---- prefill / input-length scaling ------------------------------------------
# Per-request fields pulled straight out of raw.jsonl. The records also carry
# the full token_times / inter_token_latencies arrays, which dominate the 1.9 GB
# of raw logs and are not needed here, so the scan matches scalar fields with
# regexes instead of parsing each line as JSON.
_RAW_FIELDS = {
    "input_len": re.compile(rb'"input_len":\s*(\d+)'),
    "output_len": re.compile(rb'"output_len":\s*(\d+)'),
    "ttft": re.compile(rb'"ttft":\s*([-\d.eE+]+)'),
    "mean_itl": re.compile(rb'"mean_itl":\s*([-\d.eE+]+)'),
}
_RAW_SUCCESS = re.compile(rb'"success":\s*(true|false)')

# The workload's four (input, output) buckets are a fixed property of the
# trace, so the unloaded sweep over them is a prefill-cost measurement with
# four x points per (model, config).
REPRESENTATIVE_PREFILL_MODEL = "llama3.1-8b-nvfp4"


def bucket_medians(df: pd.DataFrame, results_dir: str,
                   concurrency: int = 1) -> pd.DataFrame:
    """Per-workload-bucket TTFT/ITL medians for one load level.

    The index aggregates every bucket into one row per (model, config, load),
    which is the right unit for throughput but hides how latency scales with
    prompt length. This re-reads the retained per-request logs and splits them
    by the trace's (input_len, output_len) buckets.

    Defaults to ``concurrency=1`` because that is the only load level where
    TTFT is pure prefill: from Sec. "TTFT decomposition" onward the intercept
    is queueing delay, which is bucket-independent and would otherwise be
    charged to prefill. Returns median plus quartiles -- at c=1 a handful of
    requests per bucket carry rare multi-second stragglers that wreck a mean
    but leave the median's interquartile spread inside 10%.
    """
    sub = df[pd.to_numeric(df["concurrency"], errors="coerce") == concurrency]
    if sub.empty or "raw_path" not in sub.columns:
        return pd.DataFrame()
    rows = []
    missing = 0
    for _, r in sub.iterrows():
        path = str(r["raw_path"])
        if not os.path.isabs(path):
            path = os.path.join(results_dir, os.path.basename(path))
        if not os.path.exists(path):
            missing += 1
            continue
        recs: dict[tuple[int, int], list[tuple[float, float]]] = {}
        with open(path, "rb") as fh:
            for line in fh:
                if len(line) < 10:
                    continue
                ok = _RAW_SUCCESS.search(line)
                if not ok or ok.group(1) != b"true":
                    continue
                try:
                    key = (int(_RAW_FIELDS["input_len"].search(line).group(1)),
                           int(_RAW_FIELDS["output_len"].search(line).group(1)))
                    vals = (float(_RAW_FIELDS["ttft"].search(line).group(1)),
                            float(_RAW_FIELDS["mean_itl"].search(line).group(1)))
                except (AttributeError, ValueError):
                    continue
                recs.setdefault(key, []).append(vals)
        for (ilen, olen), vals in recs.items():
            a = np.array(vals, dtype=float)
            rows.append(dict(
                model_label=r["model_label"], config_label=r["config_label"],
                input_len=ilen, output_len=olen, n=len(a),
                ttft_p50=float(np.median(a[:, 0])),
                ttft_q1=float(np.percentile(a[:, 0], 25)),
                ttft_q3=float(np.percentile(a[:, 0], 75)),
                mean_itl_p50=float(np.median(a[:, 1])),
            ))
    if missing:
        print(f"  bucket_medians: {missing} run(s) had no readable raw log")
    return pd.DataFrame(rows)


def _prefill_fit(g: pd.DataFrame):
    """Least-squares TTFT = queue + slope*input_len over a curve's buckets.

    Returns (tokens/s, intercept seconds, r2) or None when the curve has too
    few distinct prompt lengths to fit a line.
    """
    g = g.sort_values("input_len")
    x = g["input_len"].to_numpy(dtype=float)
    y = g["ttft_p50"].to_numpy(dtype=float)
    if len(x) < 3 or not np.isfinite(y).all():
        return None
    slope, intercept = np.polyfit(x, y, 1)
    if slope <= 0:
        return None
    resid = y - (intercept + slope * x)
    denom = ((y - y.mean()) ** 2).sum()
    r2 = 1.0 - (resid ** 2).sum() / denom if denom > 0 else float("nan")
    return 1.0 / slope, intercept, r2


def fig_prefill_scaling(df, out, fmts, results_dir):
    """How serving cost scales with prompt length, and what that costs TP=2.

    The campaign's headline numbers are decode-side: peak token throughput and
    ITL both describe the generation phase. This figure covers the other phase.
    Panel (a) establishes that unloaded TTFT is linear in prompt length, which
    makes the fitted slope a prefill rate in tokens/s. Panel (b) reports that
    rate per placement, normalized to one Spark: PP=2 speeds prefill up, TP=2
    slows it below a single node for every model measured -- a second,
    compute-bound instance of the TP penalty that the all-reduce diagnostic
    already shows on the decode side. Panel (c) is the control: 64x more
    context barely moves per-token decode cost, so the prompt-length term lives
    almost entirely in prefill.
    """
    buckets = bucket_medians(df, results_dir)
    if buckets.empty:
        print("  prefill_scaling: no per-request logs available, skipping")
        return
    fits = {}
    for (model, cfg), g in buckets.groupby(["model_label", "config_label"]):
        fit = _prefill_fit(g)
        if fit is not None:
            fits[(model, cfg)] = fit

    # (c) decode's own prompt-length sensitivity: longest bucket vs shortest.
    context = {}
    for (model, cfg), g in buckets.groupby(["model_label", "config_label"]):
        s = g.set_index("input_len")["mean_itl_p50"]
        lo, hi = s.index.min(), s.index.max()
        if lo != hi and s.get(lo, 0) > 0:
            context[(model, cfg)] = s[hi] / s[lo]

    models = [m for m in models_by_size(df)
              if any(k[0] == m for k in fits)]
    if not models:
        print("  prefill_scaling: no fittable curves, skipping")
        return

    def short_name(model: str) -> str:
        name = meta_for(model)["pretty"]
        return (name.replace("Llama-3.1", "L3.1")
                    .replace("Llama-3.3", "L3.3")
                    .replace("Nemotron3-", "N3-"))

    # Four columns, the second one an empty spacer. Panel (b) labels its rows
    # with model names, right-aligned so they grow leftward: with a uniform
    # wspace wide enough for them the (b)/(c) seam wasted the same width for
    # nothing (panel (c) carries no row labels), and wspace narrow enough to
    # keep (c) close let the longest name ("N3-Super-120B-A12B") reach 0.38 in
    # back across panel (a)'s right spine and sit on its data. A dedicated
    # spacer sizes the two seams independently; the tighter (b)/(c) gap that
    # buys leaves panel (a) wider than the single-wspace layout did.
    # The spacer is sized against the measured extent of the longest row label
    # rather than by eye: 0.06 leaves it about 23 px (0.19 in) clear of panel
    # (a) at this figure width. Widening it pushes panel (b) right and buys
    # nothing; shrinking it below ~0.02 puts the label back on panel (a).
    fig = plt.figure(figsize=(TEXT_W, 3.2))
    gs = fig.add_gridspec(1, 4, width_ratios=(1.15, 0.06, 1.0, 0.78),
                          wspace=0.04)
    axa = fig.add_subplot(gs[0, 0])
    axb = fig.add_subplot(gs[0, 2])
    axc = fig.add_subplot(gs[0, 3])

    # ---- (a) TTFT is linear in prompt length -------------------------------
    rep = (REPRESENTATIVE_PREFILL_MODEL
           if any(k[0] == REPRESENTATIVE_PREFILL_MODEL for k in fits)
           else models[0])
    used_cfgs = []
    for cfg in CONFIG_ORDER:
        g = buckets[(buckets.model_label == rep)
                    & (buckets.config_label == cfg)].sort_values("input_len")
        if g.empty or (rep, cfg) not in fits:
            continue
        style = CONFIG_STYLE[cfg]
        x = g["input_len"].to_numpy(dtype=float)
        y = g["ttft_p50"].to_numpy(dtype=float) * 1e3
        err = np.vstack([y - g["ttft_q1"].to_numpy() * 1e3,
                         g["ttft_q3"].to_numpy() * 1e3 - y])
        axa.errorbar(x, y, yerr=err, color=style["color"],
                     marker=style["marker"], markersize=3.6, linewidth=0,
                     elinewidth=0.9, capsize=2, zorder=5)
        rate, intercept, r2 = fits[(rep, cfg)]
        xs = np.linspace(0, x.max(), 32)
        axa.plot(xs, (intercept + xs / rate) * 1e3, color=style["color"],
                 linewidth=1.1, linestyle="--", alpha=0.85, zorder=4)
        used_cfgs.append(cfg)
    axa.set_xlim(left=0)
    axa.set_ylim(bottom=0)
    axa.margins(y=0.08)
    axa.set_xlabel("Prompt length (tokens)", fontsize=7.5)
    axa.set_ylabel("Median TTFT (ms), unloaded", fontsize=7.5)
    axa.set_title(f"(a) {short_name(rep)}", fontsize=8, pad=3)
    # Quote the weakest fit over every curve in the campaign, not just the
    # three drawn here: panel (b) reads a prefill rate off all of them, so the
    # linearity claim has to hold campaign-wide to license that.
    r2_all = [f[2] for f in fits.values() if np.isfinite(f[2])]
    if r2_all:
        axa.annotate(f"linear fit, all {len(r2_all)} curves:\n"
                     f"$R^2 \\geq {math.floor(min(r2_all) * 1000) / 1000:.3f}$",
                     xy=(0.04, 0.95), xycoords="axes fraction", fontsize=6.2,
                     color="0.25", va="top", linespacing=1.35)
    axa.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))

    # ---- (b) prefill rate per placement, normalized to one Spark -----------
    y = np.arange(len(models))
    shown_b = []
    for yi, model in zip(y, models):
        base = fits.get((model, "single"))
        if base is None:
            continue
        vals = {cfg: fits.get((model, cfg)) for cfg in ("tp2", "pp2")}
        pts = {cfg: v[0] / base[0] for cfg, v in vals.items() if v}
        if len(pts) == 2:
            axb.plot([pts["tp2"], pts["pp2"]], [yi, yi], color="0.75",
                     linewidth=0.9, zorder=2)
        for cfg, v in pts.items():
            axb.scatter([v], [yi], marker=CONFIG_STYLE[cfg]["marker"],
                        color=CONFIG_STYLE[cfg]["color"], s=26, zorder=5)
        if pts:
            shown_b.append(model)
    axb.axvline(1.0, color="0.35", linewidth=0.9, linestyle=":", zorder=1)
    axb.set_yticks(y)
    axb.set_yticklabels([short_name(m) for m in models], fontsize=6.4)
    axb.set_ylim(-0.7, len(models) - 0.3)
    axb.invert_yaxis()
    axb.set_xlabel("Prefill rate vs single Spark", fontsize=7.5)
    axb.set_title("(b) Prompt processing", fontsize=8, pad=3)
    axb.grid(axis="y", alpha=0.15)

    # ---- (c) decode barely notices the prompt ------------------------------
    for yi, model in zip(y, models):
        for cfg in CONFIG_ORDER:
            v = context.get((model, cfg))
            if v is None:
                continue
            axc.scatter([v], [yi], marker=CONFIG_STYLE[cfg]["marker"],
                        color=CONFIG_STYLE[cfg]["color"], s=22, zorder=5)
    axc.axvline(1.0, color="0.35", linewidth=0.9, linestyle=":", zorder=1)
    axc.set_yticks(y)
    axc.set_yticklabels([])
    axc.set_ylim(-0.7, len(models) - 0.3)
    axc.invert_yaxis()
    axc.set_xlabel("ITL, 8K vs 128 prompt", fontsize=7.5)
    axc.set_title("(c) Decode", fontsize=8, pad=3)
    axc.grid(axis="y", alpha=0.15)

    proxies = [Line2D([], [], linestyle="none",
                      marker=CONFIG_STYLE[c]["marker"],
                      color=CONFIG_STYLE[c]["color"],
                      label=CONFIG_STYLE[c]["label"])
               for c in CONFIG_ORDER
               if any(k[1] == c for k in fits)]
    fig.legend(handles=proxies, loc="upper center", ncol=len(proxies),
               fontsize=6.6, frameon=False, bbox_to_anchor=(0.5, 0.02),
               handlelength=1.6, columnspacing=1.1)
    fig.subplots_adjust(top=0.90, bottom=0.20, left=0.075, right=0.985)
    save(fig, "prefill_scaling", out, fmts, tight=True)

    med_tp = np.median([fits[(m, "tp2")][0] / fits[(m, "single")][0]
                        for m in models
                        if (m, "tp2") in fits and (m, "single") in fits])
    med_pp = np.median([fits[(m, "pp2")][0] / fits[(m, "single")][0]
                        for m in models
                        if (m, "pp2") in fits and (m, "single") in fits])
    print(f"  prefill_scaling: {len(fits)} fitted curves, "
          f"median prefill rate vs single: TP=2 {med_tp:.2f}x, "
          f"PP=2 {med_pp:.2f}x")


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
    fig_prefill_scaling(df, args.out, fmts,
                        os.path.dirname(args.index) or ".")
    print("done")


if __name__ == "__main__":
    main()
