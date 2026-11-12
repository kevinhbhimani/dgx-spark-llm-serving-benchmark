#!/usr/bin/env python3
"""Generate PMBS26 figures and tables from a campaign index.

Reads the benchmark sweep index, groups rows by configuration label (single,
tp2, pp2), and writes vector PDF figures plus PNG previews. Figures are sized
for the IEEE two column format and saved as PDF for direct \\includegraphics.
The script handles one configuration or several, sorts each series by load,
coerces empty cells to missing, and skips any figure whose columns are absent.

It also writes a paper-ready summary table (LaTeX booktabs + Markdown) with
``--table`` and prints a data-quality report that flags the two artifacts that
bite this workload hardest: measurement windows too short to collect a trustable
number of completions, and head-node-only power on dual-node runs.

Usage:
    python plot_results.py --index results/benchmark-2026-10-22
    python plot_results.py --index results/benchmark-2026-10-22/results/index.csv
    python plot_results.py                       # uses the UTC-dated default
    python plot_results.py --format png          # png only, for quick viewing
    python plot_results.py --serif               # Times-like body font for IEEE

Dependencies:  pip install pandas matplotlib numpy
"""
from __future__ import annotations
import argparse
import math
import os
import sys
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")                      # headless, safe over ssh
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter, NullFormatter

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.campaign_output import resolve_campaign_output_path, resolve_index_path


# Below this many successful requests started in the measurement window,
# percentiles are small-sample noise rather than a real reading.
MIN_SAMPLES = 20
# A p99 backed by fewer than 100 requests has not even observed one full
# percentile's worth of tail samples. Keep its warning separate from the p50
# threshold above rather than pretending one generic cutoff fits both.
MIN_P99_SAMPLES = 100

# ---- configuration appearance ------------------------------------------------
CONFIG_ORDER = ["single", "tp2", "pp2"]
# Okabe-Ito colorblind-safe qualitative hues, assigned in fixed order. The
# earlier blue/red/green set collapsed to near-identical colors under
# deuteranopia (Lab dE ~= 7); these stay separable under deuter/protan/tritan
# simulation (min dE ~= 17). Identity is also triple-encoded via markers and
# solid/dashed p50/p99 styling, so the figures never lean on hue alone.
CONFIG_STYLE = {
    "single": dict(label="Single Spark",      color="#0072B2", marker="o"),
    "tp2":    dict(label="Dual Spark, TP=2",  color="#D55E00", marker="s"),
    "pp2":    dict(label="Dual Spark, PP=2",  color="#009E73", marker="^"),
}
POSTER_MODEL_LABEL: str = "qwen3-next-80b-nvfp4"
POSTER_CONFIG_LABELS: dict[str, str] = {
    "single": "One Spark",
    "tp2": "Tensor Parallelism (TP=2)",
    "pp2": "Pipeline Parallelism (PP=2)",
}
POSTER_CONCURRENCIES: tuple[int, ...] = (1, 2, 4, 8, 16, 32, 64)
POSTER_FIGURE_WIDTH_INCHES: float = 4.8
POSTER_DEFAULT_ASPECT_RATIO: float = 3.0
POSTER_TTFT_THRESHOLD_SECONDS: float = 3.0
POSTER_ITL_THRESHOLD_MILLISECONDS: float = 100.0
POSTER_SECONDS_TO_MILLISECONDS: float = 1e3
POSTER_LINE_WIDTH: float = 1.8
POSTER_REFERENCE_LINE_WIDTH: float = 1.2
POSTER_MARKER_SIZE: float = 5.0
POSTER_X_EDGE_MARGIN: float = 0.25
POSTER_Y_HEADROOM: float = 1.1
POSTER_AXES_LEFT: float = 0.16
POSTER_AXES_RIGHT: float = 0.98
POSTER_AXES_TOP: float = 0.83
POSTER_AXES_BOTTOM: float = 0.2
POSTER_REFERENCE_LABEL_RIGHT_X: float = 0.99
POSTER_REFERENCE_LABEL_LEFT_X: float = 0.01
POSTER_REFERENCE_LABEL_HORIZONTAL_OFFSET: float = 3.0
POSTER_REFERENCE_LABEL_VERTICAL_OFFSET: float = 4.0
POSTER_REFERENCE_LABEL_FONT_SIZE: float = 7.0
POSTER_EXPECTED_POWER_NODES: dict[str, int] = {
    "single": 1,
    "tp2": 2,
    "pp2": 2,
}
POSTER_METRICS: tuple[
    tuple[str, str, str, float, float | None, str | None], ...
] = (
    ("ttft_by_concurrency", "ttft_p50", "TTFT (s)", 1.0,
     POSTER_TTFT_THRESHOLD_SECONDS, "3 s target"),
    ("itl_by_concurrency", "itl_p50", "ITL (ms)",
     POSTER_SECONDS_TO_MILLISECONDS, POSTER_ITL_THRESHOLD_MILLISECONDS,
     "100 ms target"),
    ("goodput_by_concurrency", "goodput_rps", "Goodput\nRequests/s", 1.0,
     None, None),
    ("energy_by_concurrency", "energy_per_token_j", "Energy (J/token)", 1.0,
     None, None),
)
_FALLBACK_COLORS = ["#CC79A7", "#E69F00", "#56B4E9", "#F0E442", "#000000"]
_FALLBACK_MARKERS = ["D", "v", "P", "X", "*"]

# Dual-node configs. When their rows record power_nodes < 2 (or predate the
# power_nodes column), the local nvidia_smi sampler saw one GB10 only, so
# per-token energy is a lower bound (real cluster draw is higher) and tokens/W
# an upper bound. The energy figure and table annotate this instead of silently
# comparing one-node dual numbers against a true single-node number. Rows with
# power_nodes == 2 carry both nodes' power and need no caveat.
DUAL_NODE_CONFIGS = {"tp2", "pp2"}


def head_only_power(cfg: str, g: pd.DataFrame) -> bool:
    """True when a dual-node series' energy columns cover the head node only."""
    if cfg not in DUAL_NODE_CONFIGS:
        return False
    if "power_nodes" not in g.columns:
        return True
    return not (g["power_nodes"] >= 2).all()

COL_W = 3.5      # one IEEE column, inches
TEXT_W = 7.0     # full text width, inches
PANEL_H = 2.7


def set_rc(dpi: int, serif: bool) -> None:
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
    })
    if serif:
        # Times-like body text to match an IEEE paper. Falls back gracefully if
        # the exact family is unavailable; mathtext is switched to match.
        plt.rcParams["font.family"] = "serif"
        plt.rcParams["font.serif"] = ["Times New Roman", "Liberation Serif",
                                      "DejaVu Serif"]
        plt.rcParams["mathtext.fontset"] = "dejavuserif"


# ---- data helpers ------------------------------------------------------------
NUMERIC_COLS = [
    "concurrency", "target_qps", "repeat_id", "repeat_count", "measure_s",
    "configured_measure_s", "measure_extension_s", "warmup_s",
    "configured_warmup_s", "warmup_extension_s", "cooldown_s",
    "load_extension_s", "drain_s",
    "requests_started", "requests_completed", "requests_failed",
    "requests_completed_in_window",
    "requests_started_and_completed_in_window",
    "avg_input_tokens", "avg_requested_output_tokens",
    "total_output_tokens", "req_thru_rps", "tok_thru_tps",
    "tok_thru_completed_tps",
    "goodput_rps", "goodput_itl2_rps",
    "ttft_p50", "ttft_p99", "itl_p50", "itl_p99", "mean_itl_p50",
    "e2e_p50", "e2e_p99",
    "kv_cache_usage_mean", "kv_cache_usage_max",
    "requests_running_mean", "requests_running_max",
    "requests_waiting_mean", "requests_waiting_max",
    "server_prompt_tokens", "server_generation_tokens",
    "server_prompt_tokens_cached", "server_gen_tps", "server_prompt_tps",
    "server_metric_samples", "server_metric_sample_span_s",
    "server_metric_max_gap_s", "kv_metric_samples", "kv_metric_sample_span_s",
    "kv_metric_max_gap_s",
    "num_preemptions",
    "max_model_len", "gpu_memory_utilization", "block_size", "gpu_blocks",
    "total_kv_tokens", "weight_gb", "kv_cache_gb", "worker_weight_gb",
    "worker_kv_cache_gb", "max_concurrency",
    "mean_power_w", "mean_power_w_head", "mean_power_w_worker", "power_nodes",
    "power_samples_head", "power_samples_worker",
    "power_max_gap_s_head", "power_max_gap_s_worker",
    "energy_per_token_j", "tokens_per_watt",
]

# Columns whose run-to-run spread across REPEATS is kept when repeats are
# collapsed: the mean lands in the original column and min/max in
# <col>__lo/<col>__hi, which draw_band() shades behind the mean line. With two
# or more repeats, <col>__sd and <col>__ci95 (Student-t half-width of the mean)
# are added for tables.
BAND_COLS = [
    "tok_thru_tps", "tok_thru_completed_tps", "server_gen_tps",
    "req_thru_rps", "goodput_rps", "goodput_itl2_rps",
    "ttft_p50", "ttft_p99", "itl_p50", "itl_p99", "mean_itl_p50", "e2e_p50",
    "energy_per_token_j", "tokens_per_watt", "mean_power_w",
]

# Two-sided 95% Student-t critical values by degrees of freedom (n - 1).
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447,
       7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228}

# Live capacity that vLLM settles on after memory profiling. It differs by a
# few thousand KV blocks on every launch, so replicates that are separate
# launches legitimately disagree; within one replicate it must not change.
PER_LAUNCH_COLS = ["deployment_fingerprint", "gpu_blocks", "total_kv_tokens"]


def window_col(df: pd.DataFrame) -> str:
    """Column naming the configured measurement window.

    steady-exact-v4 rows may extend measure_s under the minimum-sample rule,
    so identity checks use the configured window. Older indexes have only
    measure_s, which was then always the configured value.
    """
    return ("configured_measure_s" if "configured_measure_s" in df.columns
            else "measure_s")


def load_index(path: str) -> pd.DataFrame:
    index_path = resolve_index_path(path)
    path = str(index_path)
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
    if "raw_path" in df.columns:
        def resolve_raw_path(value: object) -> object:
            if pd.isna(value) or not str(value).strip():
                return value
            raw_path = Path(str(value)).expanduser()
            if not raw_path.is_absolute():
                raw_path = index_path.parent / raw_path
            return str(raw_path.resolve())

        df["raw_path"] = df["raw_path"].map(resolve_raw_path)
    for c in NUMERIC_COLS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    if "config_label" not in df.columns:
        sys.exit("index is missing config_label; cannot group")
    config_text = df["config_label"].astype(str)
    if df["config_label"].isna().any() or config_text.str.strip().eq("").any():
        sys.exit("index contains a blank config_label")
    if "model_label" in df.columns or "model" in df.columns:
        resolved = pd.Series(pd.NA, index=df.index, dtype="object")
        for col in ("model_label", "model"):
            if col not in df.columns:
                continue
            rendered = df[col].astype(str)
            usable = df[col].notna() & rendered.str.strip().ne("")
            resolved = resolved.where(resolved.notna(), rendered.where(usable))
        if resolved.isna().any():
            sys.exit("index contains a row with no model_label or model")
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


def _model_values(df: pd.DataFrame) -> pd.Series | None:
    """Return each row's display model, preferring a nonblank model label.

    A partially populated ``model_label`` column must not hide rows that can be
    identified by ``model``. Returning one resolved series also keeps every
    filtering/grouping caller on the same row-wise fallback rule.
    """
    values = pd.Series(pd.NA, index=df.index, dtype="object")
    for col in ("model_label", "model"):
        if col not in df.columns:
            continue
        rendered = df[col].astype(str)
        usable = df[col].notna() & rendered.str.strip().ne("")
        values = values.where(values.notna(), rendered.where(usable))
    return values if values.notna().any() else None


def models_present(df: pd.DataFrame) -> list[str]:
    """Distinct model labels in the index, in first-seen order.

    Falls back to the full model id when only that column exists, and to an empty
    list when neither is present (an old single-model index), which keeps the
    plots in their original per-config comparison mode.
    """
    values = _model_values(df)
    return [] if values is None else list(dict.fromkeys(values.dropna().tolist()))


def _slug(name: str) -> str:
    """Filesystem-safe slug for a per-model output subdirectory."""
    s = "".join(c if (c.isalnum() or c in "-.") else "-" for c in str(name).lower())
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-") or "model"


def iter_model_frames(df: pd.DataFrame):
    """Yield (model_label_or_None, sub_df) for each model in the index.

    Config-level grouping/deduping must happen within a model, or two models
    measured at the same config and load look like duplicate points. A
    single-model or old index yields one (None, df) pair, preserving the
    original behavior.
    """
    values = _model_values(df)
    if values is None:
        yield None, df
        return
    for model in models_present(df):
        yield model, df[values == model]


def load_col_for(g: pd.DataFrame, mode: str) -> str:
    load_col = "concurrency" if mode == "closed" else "target_qps"
    if load_col not in g.columns:
        load_col = "concurrency" if "concurrency" in g.columns else g.columns[0]
    return load_col


def group(df: pd.DataFrame, cfg: str):
    """Return (sorted_subframe, load_column, mode) for one configuration.

    Assumes df has already been deduplicated by dedupe_index().
    """
    g = df[df["config_label"] == cfg].copy()
    mode = str(g["mode"].iloc[0]) if "mode" in g.columns and len(g) else "closed"
    load_col = load_col_for(g, mode)
    return g.sort_values(load_col), load_col, mode


def collapse_repeats(df: pd.DataFrame, *,
                     allow_incomplete: bool = False) -> pd.DataFrame:
    """Average repeated (model, config, load) rows into one, keeping spread.

    A sweep run with REPEATS>1 records several fixed-workload windows of each
    point. Plotting them raw draws a line that zigzags through contradictory
    points at the same x value; collapsing to the mean gives one point per
    load, and the min/max across copies is kept for BAND_COLS as
    <col>__lo/<col>__hi so the figures can shade run-to-run spread behind the
    mean line. Failed copies are retained: dropping them would create
    survivorship bias. Non-numeric fields keep the first copy's value;
    n_repeats records how many windows back each point and the minimum per-window
    sample size is preserved for percentile warnings.
    """
    parts = []
    numeric = [
        c for c in NUMERIC_COLS
        if c in df.columns and c not in {"repeat_id", "repeat_count"}
    ]
    for model, msub in iter_model_frames(df):
        pfx = f"{model} " if model else ""
        for cfg in configs_present(msub):
            g = msub[msub["config_label"] == cfg].copy()   # chronological order
            mode = str(g["mode"].iloc[0]) if "mode" in g.columns and len(g) else "closed"
            if "mode" in g.columns and g["mode"].dropna().nunique() > 1:
                sys.exit(f"cannot combine closed/open rows under config_label '{cfg}'")
            load_col = load_col_for(g, mode)

            if mode == "closed" and "expected_concurrencies" in g.columns:
                schedule_sets = []
                for raw in g["expected_concurrencies"]:
                    tokens = str(raw or "").split()
                    if (not tokens
                            or any(not token.isdigit() or int(token) < 1
                                   for token in tokens)
                            or len(set(tokens)) != len(tokens)):
                        sys.exit(
                            f"invalid expected_concurrencies={raw!r} in the "
                            f"{pfx}{cfg} curve"
                        )
                    schedule_sets.append(frozenset(map(int, tokens)))
                if len(set(schedule_sets)) != 1:
                    sys.exit(
                        f"incompatible expected_concurrencies across the "
                        f"{pfx}{cfg} curve"
                    )
                expected_loads = set(schedule_sets[0])
                observed_values = pd.to_numeric(g[load_col], errors="coerce")
                if (observed_values.isna().any()
                        or not np.isfinite(observed_values).all()
                        or not np.isclose(
                            observed_values, np.round(observed_values)
                        ).all()):
                    sys.exit(f"invalid {load_col} in the {pfx}{cfg} curve")
                observed_loads = set(np.round(observed_values).astype(int))
                if observed_loads != expected_loads:
                    message = (
                        f"incomplete load rectangle for {pfx}{cfg}: observed "
                        f"{sorted(observed_loads)}, intended "
                        f"{sorted(expected_loads)}"
                    )
                    if not allow_incomplete:
                        sys.exit(message + "; rerun the missing loads or pass "
                                 "--allow-incomplete for diagnostic plots")
                    print("  WARNING: " + message, file=sys.stderr)

            # A release curve must describe one methodology and one launch
            # policy across *all* loads. Checking only duplicate rows at a
            # single load lets a resumed sweep silently splice two configurations
            # into a plausible-looking line. Live per-launch capacity is checked
            # per replicate below instead, because replicates are separate
            # server launches by design.
            win_col = window_col(g)
            family_identity_cols = [
                "methodology_version", "config_file_sha256",
                "expected_concurrencies", "harness_source_sha256",
                "client_environment_sha256",
                "serving_fingerprint", "model",
                win_col, "configured_warmup_s", "cooldown_s",
                "power_nodes", "power_scope", "vllm_version",
                "max_model_len", "kv_cache_dtype", "gpu_memory_utilization",
                "block_size", "total_kv_tokens_source", "hybrid_cache",
            ]
            mixed_family = [
                c for c in family_identity_cols
                if c in g.columns
                and g[c].astype(object).where(g[c].notna(), "<missing>")
                .nunique() > 1
            ]
            if mixed_family:
                sys.exit(
                    f"incompatible rows across the {pfx}{cfg} curve: "
                    f"{', '.join(mixed_family)} differ. Plot separate indexes "
                    "or filter to one deployment."
                )
            if "repeat_id" in g.columns and "deployment_fingerprint" in g.columns:
                for rep, rgrp in g.groupby("repeat_id", dropna=False):
                    launches = rgrp["deployment_fingerprint"].dropna().nunique()
                    if launches > 1:
                        print(f"  note: {pfx}{cfg} replicate {rep} spans "
                              f"{launches} server launches (resumed sweep)",
                              file=sys.stderr)

            repeat_key = "__checked_repeat_id"
            has_repeat_ids = "repeat_id" in g.columns and g["repeat_id"].notna().any()
            has_repeat_counts = "repeat_count" in g.columns
            expected_repeat_count = None
            if has_repeat_counts:
                raw_counts = pd.to_numeric(g["repeat_count"], errors="coerce")
                valid_counts = (
                    raw_counts.notna() & np.isfinite(raw_counts)
                    & (raw_counts >= 1)
                    & np.isclose(raw_counts, np.round(raw_counts))
                )
                if not valid_counts.all():
                    sys.exit(
                        f"invalid or missing repeat_count in the {pfx}{cfg} curve; "
                        "repeat counts must be positive integers"
                    )
                expected_repeat_count = int(raw_counts.max())
                if not has_repeat_ids:
                    sys.exit(
                        f"repeat_count is present without repeat_id in the "
                        f"{pfx}{cfg} curve"
                    )
            if has_repeat_ids:
                raw_ids = pd.to_numeric(g["repeat_id"], errors="coerce")
                valid = (raw_ids.notna() & np.isfinite(raw_ids) & (raw_ids >= 1)
                         & np.isclose(raw_ids, np.round(raw_ids)))
                if not valid.all():
                    sys.exit(
                        f"invalid or missing repeat_id in the {pfx}{cfg} curve; "
                        "repeat ids must be positive integers"
                    )
                g[repeat_key] = np.round(raw_ids).astype(int)
                repeat_sets = []
                if (has_repeat_counts
                        and (g[repeat_key]
                             > np.round(raw_counts).astype(int)).any()):
                    sys.exit(
                        f"repeat_id exceeds repeat_count in the {pfx}{cfg} curve"
                    )
                for load, grp in g.groupby(load_col, sort=False, dropna=False):
                    duplicated = grp[repeat_key][grp[repeat_key].duplicated()].unique()
                    if len(duplicated):
                        ids = ", ".join(str(int(v)) for v in sorted(duplicated))
                        sys.exit(
                            f"duplicate repeat_id(s) {ids} for {pfx}{cfg} "
                            f"{load_col}={load:g}; remove or isolate duplicate rows"
                        )
                    repeat_sets.append((load, frozenset(grp[repeat_key])))
                observed = set().union(*(ids for _, ids in repeat_sets))
                intended = (expected_repeat_count if expected_repeat_count is not None
                            else max(observed) if observed else 0)
                expected = set(range(1, intended + 1))
                if any(ids != expected for _, ids in repeat_sets):
                    detail = ", ".join(
                        f"{load:g}={{{','.join(map(str, sorted(ids)))}}}"
                        for load, ids in repeat_sets
                    )
                    message = (
                        f"incomplete repeat rectangle for {pfx}{cfg} "
                        f"({load_col}:repeat_ids {detail}; intended "
                        f"{{{','.join(map(str, sorted(expected)))}}})"
                    )
                    if not allow_incomplete:
                        sys.exit(
                            message + "; rerun the missing windows or pass "
                            "--allow-incomplete for diagnostic plots"
                        )
                    print(
                        "  WARNING: " + message
                        + "; aggregates use incomplete intended windows",
                        file=sys.stderr,
                    )
            else:
                # Legacy indexes predate repeat_id. They remain plottable, but
                # duplicate rows cannot be certified as distinct windows.
                if g.duplicated(subset=[load_col]).any():
                    sys.exit(
                        f"duplicate legacy rows for {pfx}{cfg} have no repeat_id; "
                        "plot a deduplicated index"
                    )
                g[repeat_key] = 1

            rows = []
            for load, grp in g.groupby(load_col, sort=False, dropna=False):
                # Repeated rows may be averaged only if their methodology,
                # resolved experiment, and configured window are identical.
                # Otherwise a plausible-looking mean would describe no run
                # that actually occurred. The deployment fingerprint may differ:
                # each replicate is its own launch.
                identity_cols = [
                    "methodology_version", "config_file_sha256",
                    "harness_source_sha256", "client_environment_sha256",
                    "serving_fingerprint",
                    "experiment_fingerprint",
                    win_col, "configured_warmup_s", "cooldown_s",
                    # Energy values with different node coverage or sensor
                    # scopes are not repeated measurements of the same
                    # quantity and must never be averaged together.
                    "power_nodes", "power_scope",
                ]
                mixed = [
                    c for c in identity_cols
                    if c in grp.columns
                    and grp[c].astype(object).where(grp[c].notna(), "<missing>")
                    .nunique() > 1
                ]
                if mixed:
                    sys.exit(
                        f"incompatible reruns for {pfx}{cfg} {load_col}={load:g}: "
                        f"{', '.join(mixed)} differ. Plot separate indexes or "
                        "filter to one experiment fingerprint."
                    )
                row = grp.iloc[0].copy()
                row = row.drop(labels=[repeat_key])
                for c in numeric:
                    row[c] = grp[c].mean(skipna=False)
                if expected_repeat_count is not None:
                    row["repeat_count"] = expected_repeat_count
                for c in BAND_COLS:
                    if c in grp.columns and grp[c].notna().any():
                        row[f"{c}__lo"] = grp[c].min()
                        row[f"{c}__hi"] = grp[c].max()
                        values = grp[c].dropna()
                        if len(values) >= 2:
                            sd = float(values.std(ddof=1))
                            row[f"{c}__sd"] = sd
                            row[f"{c}__ci95"] = (
                                T95.get(len(values) - 1, 1.96) * sd
                                / math.sqrt(len(values))
                            )
                row["n_repeats"] = grp[repeat_key].nunique()
                if row["n_repeats"] > 1 and "repeat_id" in row.index:
                    row["repeat_id"] = np.nan
                if "requests_completed" in grp.columns:
                    row["requests_completed__min"] = grp["requests_completed"].min()
                if "requests_failed" in grp.columns:
                    row["requests_failed__max"] = grp["requests_failed"].max()
                if row["n_repeats"] > 1:
                    print(f"  note: {pfx}{cfg} {load_col}={load:g}: averaged "
                          f"{int(row['n_repeats'])} repeats "
                          "(spread shaded in figures)")
                rows.append(row)
            parts.append(pd.DataFrame(rows))
    return pd.concat(parts) if parts else df


def draw_band(ax, g, x, ycol, color, scale: float = 1.0) -> None:
    """Shade the min-max spread across repeats behind a mean line, if present."""
    lo, hi = f"{ycol}__lo", f"{ycol}__hi"
    if lo not in g.columns or hi not in g.columns:
        return
    lo_v, hi_v = g[lo] * scale, g[hi] * scale
    if not (lo_v.notna() & hi_v.notna()).any():
        return
    x = x if isinstance(x, pd.Series) else pd.Series(x, index=g.index)
    ax.fill_between(x, lo_v, hi_v, color=color, alpha=0.15, linewidth=0)


def has(df: pd.DataFrame, cols: list[str]) -> bool:
    return all(c in df.columns and df[c].notna().any() for c in cols)


def mark_low_n(ax, g, x, y, minimum=None) -> bool:
    """Circle points backed by fewer than the requested successful samples.

    x and y must be the same (already unit-converted) values passed to the
    line's ax.plot() call -- e.g. ttft_p50 * 1e3, not the raw column -- or
    the circle lands at the raw value on an axis plotted in different units.

    Returns whether any point was marked, so callers can add a legend entry.
    """
    ncol = ("requests_completed__min"
            if "requests_completed__min" in g.columns
            else "requests_completed")
    if ncol not in g.columns:
        return False
    minimum = MIN_SAMPLES if minimum is None else minimum
    mask = g[ncol] < minimum
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
P99_LOW_N_PROXY = Line2D([], [], marker="o", linestyle="none", markersize=7,
                         markerfacecolor="none", markeredgecolor="0.15",
                         label=f"p99 n < {MIN_P99_SAMPLES}")


def add_low_n_legend(ax, any_marked: bool, proxies=None, **legend_kwargs) -> None:
    """Re-add the legend with a low-n proxy entry appended.

    Pass the same kwargs (loc, fontsize, ncol, ...) used for the figure's
    original ax.legend() call so this doesn't reset its placement.
    """
    if not any_marked:
        return
    proxies = proxies or [LOW_N_PROXY]
    handles, labels = ax.get_legend_handles_labels()
    ax.legend(handles + proxies, labels + [p.get_label() for p in proxies],
              **legend_kwargs)


def warn_low_n(df: pd.DataFrame) -> None:
    if "requests_completed" not in df.columns:
        return
    any_bad = False
    any_bad_p99 = False
    for model, msub in iter_model_frames(df):
        pfx = f"{model}/" if model else ""
        for cfg in configs_present(msub):
            g, lc, _ = group(msub, cfg)
            ncol = ("requests_completed__min"
                    if "requests_completed__min" in g.columns
                    else "requests_completed")
            bad = g[g[ncol] < MIN_SAMPLES]
            if len(bad):
                any_bad = True
                pts = ", ".join(f"{lc}={v:g} (n={int(n)})"
                                for v, n in zip(bad[lc], bad[ncol]))
                print(f"  warning: {pfx}{cfg} has {len(bad)} point(s) with fewer "
                      f"than {MIN_SAMPLES} successful requests started in "
                      f"the window: {pts}")
            bad_p99 = g[g[ncol] < MIN_P99_SAMPLES]
            if len(bad_p99):
                any_bad_p99 = True
                pts = ", ".join(
                    f"{lc}={v:g} (n={int(n)})"
                    for v, n in zip(bad_p99[lc], bad_p99[ncol])
                )
                print(f"  warning: {pfx}{cfg} has {len(bad_p99)} point(s) with "
                      f"fewer than {MIN_P99_SAMPLES} requests for p99: {pts}")
    if any_bad:
        print(f"           percentiles at those loads are small-sample noise, not "
              f"a capacity reading; circled (o) in the figures. A longer "
              f"--measure-s gives more requests to trust.")
    if any_bad_p99:
        print("           p99 points below the p99 threshold are circled in the "
              "tail-latency figure; quote them only as exploratory maxima.")


def report_quality(df: pd.DataFrame) -> None:
    """Print a compact data-quality report before drawing anything.

    Surfaces the two things that most often make this workload's numbers
    misleading: (1) too few successful measurement-started requests for stable
    latency percentiles, and (2) dual-node runs whose power is sampled on one
    node only.
    """
    print("data-quality report")
    have_ms = "measure_s" in df.columns
    for model, msub in iter_model_frames(df):
        pfx = f"{model}/" if model else ""
        for cfg in configs_present(msub):
            _report_series(msub, cfg, pfx, have_ms)


def _report_series(msub, cfg, pfx, have_ms):
    g, lc, _ = group(msub, cfg)
    n_pts = len(g)
    wcol = window_col(g)
    windows = sorted({float(v) for v in g[wcol].dropna()}) if have_ms else []
    win_str = ("/".join(f"{w:g}" for w in windows) + "s") if windows else "?"
    peak = "?"
    if "tok_thru_tps" in g.columns and g["tok_thru_tps"].notna().any():
        imax = g["tok_thru_tps"].idxmax()
        peak = (f"{g.loc[imax, 'tok_thru_tps']:.0f} tok/s @ "
                f"{lc}={g.loc[imax, lc]:g}")
    tag = " [head-node power only]" if head_only_power(cfg, g) else ""
    print(f"  {pfx + cfg:16s} {n_pts} pts, window(s)={win_str}, peak={peak}{tag}")
    if have_ms and len(windows) > 1:
        print(f"           mixed measurement windows in this series; windowed "
              f"rates stay comparable but per-point sample sizes differ a lot")
    if "measure_extension_s" in g.columns:
        extended = g[g["measure_extension_s"].fillna(0) > 0]
        if len(extended):
            pts = ", ".join(f"{lc}={v:g}" for v in extended[lc])
            print(f"           window extended by the minimum-sample rule at "
                  f"{pts}")
    # A completed-in-window==0 point does not invalidate exact token-in-window
    # throughput, but it does expose why conventional completed-only accounting
    # would have been misleading. Call that diagnostic out explicitly.
    if "requests_completed_in_window" in g.columns:
        empties = g[g["requests_completed_in_window"] == 0]
        if len(empties):
            pts = ", ".join(f"{lc}={v:g}" for v in empties[lc])
            print(f"           {len(empties)} point(s) with 0 completions "
                  f"in-window ({pts}): the completed-only diagnostic is 0, "
                  f"but exact token/server throughput remains valid")
    failed_col = ("requests_failed__max" if "requests_failed__max" in g.columns
                  else "requests_failed")
    if failed_col in g.columns:
        failed = g[g[failed_col] > 0]
        if len(failed):
            pts = ", ".join(
                f"{lc}={v:g} (max failed={int(n)})"
                for v, n in zip(failed[lc], failed[failed_col])
            )
            print(f"           request failures occurred at {pts}; repeats "
                  "are retained rather than survivorship-filtered")


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


def save(fig, name: str, out: str, fmts: list[str], tight: bool = False) -> None:
    os.makedirs(out, exist_ok=True)
    # tight=True expands the saved bbox to include manual fig.text (e.g. the
    # energy figure's footnote), which constrained_layout does not reserve room
    # for on its own.
    kw = dict(bbox_inches="tight") if tight else {}
    for fmt in fmts:
        path = os.path.join(out, f"{name}.{fmt}")
        fig.savefig(path, **kw)
        print(f"  wrote {path}")
    plt.close(fig)


def poster_figure_size(aspect_ratio: float) -> tuple[float, float]:
    """Return the fixed-width poster figure size for a width-to-height ratio."""
    if not math.isfinite(aspect_ratio) or aspect_ratio <= 0:
        raise ValueError("poster aspect ratio must be finite and greater than zero")
    figure_width: float = POSTER_FIGURE_WIDTH_INCHES
    figure_height: float = figure_width / aspect_ratio
    if not math.isfinite(figure_height) or figure_height <= 0:
        raise ValueError("poster aspect ratio produces an invalid figure size")
    return figure_width, figure_height


def parse_poster_aspect_ratio(value: str) -> float:
    """Parse a finite, positive poster aspect ratio for an argparse option."""
    try:
        aspect_ratio: float = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "poster aspect ratio must be a number"
        ) from error
    try:
        poster_figure_size(aspect_ratio)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    return aspect_ratio


def fig_poster_metric(
    df: pd.DataFrame,
    out: str,
    fmts: list[str],
    *,
    name: str,
    metric_column: str,
    y_label: str,
    scale: float,
    reference_value: float | None,
    reference_label: str | None,
    aspect_ratio: float,
) -> None:
    """Draw one categorical concurrency graph for the current poster model."""
    if metric_column not in df.columns:
        raise ValueError(f"poster index is missing {metric_column}")
    if (reference_value is None) != (reference_label is None):
        raise ValueError("poster reference lines require both value and label")

    fig, ax = plt.subplots(
        figsize=poster_figure_size(aspect_ratio), constrained_layout=False
    )
    ax.set_axisbelow(True)
    ax.grid(axis="y")
    ax.grid(axis="x", visible=False)
    maximum_value = 0.0

    for config in CONFIG_ORDER:
        series: pd.DataFrame
        load_column: str
        mode: str
        series, load_column, mode = group(df, config)
        if mode != "closed" or load_column != "concurrency":
            raise ValueError(f"poster config {config} is not a closed-load sweep")
        series = series[series[load_column].isin(POSTER_CONCURRENCIES)].copy()
        concurrency_values: pd.Series = pd.to_numeric(
            series[load_column], errors="coerce"
        )
        numeric_concurrency_values = concurrency_values.to_numpy(dtype=float)
        if (
            concurrency_values.isna().any()
            or not np.isfinite(numeric_concurrency_values).all()
            or not np.isclose(
                numeric_concurrency_values,
                np.round(numeric_concurrency_values),
            ).all()
        ):
            raise ValueError(f"poster config {config} has invalid concurrency values")
        actual_concurrencies: tuple[int, ...] = tuple(
            int(value) for value in numeric_concurrency_values
        )
        if actual_concurrencies != POSTER_CONCURRENCIES:
            raise ValueError(
                f"poster config {config} has concurrency values "
                f"{actual_concurrencies}, expected {POSTER_CONCURRENCIES}"
            )

        metric_values: pd.Series = pd.to_numeric(
            series[metric_column], errors="coerce"
        )
        numeric_values: np.ndarray = metric_values.to_numpy(dtype=float)
        if not np.isfinite(numeric_values).all() or (numeric_values < 0).any():
            raise ValueError(f"poster config {config} has invalid {metric_column} values")
        plotted_values: pd.Series = metric_values * scale
        maximum_value = max(maximum_value, float(plotted_values.max()))
        positions: pd.Series = pd.Series(
            np.arange(len(POSTER_CONCURRENCIES), dtype=float),
            index=series.index,
        )
        style: dict[str, str] = CONFIG_STYLE[config]
        ax.plot(
            positions,
            plotted_values,
            color=style["color"],
            marker=style["marker"],
            linewidth=POSTER_LINE_WIDTH,
            markersize=POSTER_MARKER_SIZE,
            zorder=4,
            clip_on=False,
        )
        draw_band(ax, series, positions, metric_column, style["color"], scale)

    if reference_value is not None and reference_label is not None:
        is_right_annotation = metric_column == "ttft_p50"
        annotation_x = (
            POSTER_REFERENCE_LABEL_RIGHT_X
            if is_right_annotation else POSTER_REFERENCE_LABEL_LEFT_X
        )
        annotation_x_offset = (
            -POSTER_REFERENCE_LABEL_HORIZONTAL_OFFSET
            if is_right_annotation else POSTER_REFERENCE_LABEL_HORIZONTAL_OFFSET
        )
        ax.axhline(
            reference_value,
            color="0.35",
            linestyle="--",
            linewidth=POSTER_REFERENCE_LINE_WIDTH,
        )
        ax.annotate(
            reference_label,
            xy=(annotation_x, reference_value),
            xycoords=("axes fraction", "data"),
            xytext=(annotation_x_offset, -POSTER_REFERENCE_LABEL_VERTICAL_OFFSET),
            textcoords="offset points",
            ha="right" if is_right_annotation else "left",
            va="top",
            fontsize=POSTER_REFERENCE_LABEL_FONT_SIZE,
            color="0.35",
        )

    top_value = max(maximum_value, reference_value or 0.0)
    ax.set_ylim(0, top_value * POSTER_Y_HEADROOM if top_value else 1.0)
    ax.set_yscale("linear")
    ax.set_xlim(
        -POSTER_X_EDGE_MARGIN,
        len(POSTER_CONCURRENCIES) - 1 + POSTER_X_EDGE_MARGIN,
    )
    ax.set_xscale("linear")
    ax.set_xticks(
        np.arange(len(POSTER_CONCURRENCIES)),
        [str(value) for value in POSTER_CONCURRENCIES],
    )
    ax.set_xlabel("Concurrent Requests")
    ax.set_ylabel(y_label)
    fig.subplots_adjust(
        left=POSTER_AXES_LEFT,
        right=POSTER_AXES_RIGHT,
        top=POSTER_AXES_TOP,
        bottom=POSTER_AXES_BOTTOM,
    )
    save(fig, name, out, fmts)


def make_poster_figures(
    df: pd.DataFrame,
    out: str,
    fmts: list[str],
    *,
    aspect_ratio: float,
) -> None:
    """Write the four poster figures from one model's measurement rows."""
    if "power_scope" not in df.columns or not df["power_scope"].eq("gpu_devices").all():
        raise ValueError("poster energy requires GPU-device power measurements")
    if "power_nodes" not in df.columns:
        raise ValueError("poster energy requires power_nodes in the campaign index")
    for config, expected_nodes in POSTER_EXPECTED_POWER_NODES.items():
        config_rows = df[df["config_label"] == config]
        observed_nodes = pd.to_numeric(config_rows["power_nodes"], errors="coerce")
        if config_rows.empty or not observed_nodes.eq(expected_nodes).all():
            raise ValueError(
                f"poster config {config} requires GPU energy from "
                f"{expected_nodes} participating node(s)"
            )
    for name, column, y_label, scale, reference, reference_label in POSTER_METRICS:
        fig_poster_metric(
            df,
            out,
            fmts,
            name=name,
            metric_column=column,
            y_label=y_label,
            scale=scale,
            reference_value=reference,
            reference_label=reference_label,
            aspect_ratio=aspect_ratio,
        )


# ---- figures -----------------------------------------------------------------
def fig_throughput_scaling(df, out, fmts):
    tcol = _throughput_col(df)
    if not has(df, [tcol]):
        return
    fig, ax = plt.subplots(figsize=(COL_W, PANEL_H))
    all_loads = []
    mode = "closed"
    for i, cfg in enumerate(configs_present(df)):
        g, lc, mode = group(df, cfg)
        s = style_for(cfg, i)
        ax.plot(g[lc], g[tcol], marker=s["marker"], color=s["color"],
                label=s["label"])
        draw_band(ax, g, g[lc], tcol, s["color"])
        all_loads += g[lc].tolist()
    set_load_axis(ax, all_loads, mode)
    ax.set_ylabel("Output token throughput (tok/s)")
    ax.set_ylim(bottom=0)
    ax.legend()
    save(fig, "throughput_scaling", out, fmts)


def fig_latency_throughput(df, out, fmts):
    tcol = _throughput_col(df)
    if not has(df, [tcol, "ttft_p50", "itl_p50"]):
        return
    fig, (axl, axr) = plt.subplots(1, 2, figsize=(TEXT_W, PANEL_H))
    any_low_n = False
    for i, cfg in enumerate(configs_present(df)):
        g, lc, _ = group(df, cfg)
        s = style_for(cfg, i)
        ttft_ms = g["ttft_p50"] * 1e3
        itl_ms = g["itl_p50"] * 1e3
        axl.plot(g[tcol], ttft_ms, marker=s["marker"],
                 color=s["color"], label=s["label"])
        axr.plot(g[tcol], itl_ms, marker=s["marker"],
                 color=s["color"], label=s["label"])
        # the throughput axis is completion-independent, but the latency
        # percentiles on the y-axis are not, so low-n marking still applies here
        any_low_n |= mark_low_n(axl, g, g[tcol], ttft_ms)
        any_low_n |= mark_low_n(axr, g, g[tcol], itl_ms)
        # annotate each point with its load so the swept parameter is visible
        for x, y, c in zip(g[tcol], ttft_ms, g[lc]):
            if pd.notna(x) and pd.notna(y):
                axl.annotate(f"{c:g}", (x, y), textcoords="offset points",
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
    any_low_p99 = False
    mode = "closed"
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
        draw_band(axl, g, g[lc], "ttft_p50", s["color"], scale=1e3)
        draw_band(axr, g, g[lc], "itl_p50", s["color"], scale=1e3)
        any_low_n |= mark_low_n(axl, g, g[lc], ttft_p50_ms)
        any_low_p99 |= mark_low_n(axl, g, g[lc], ttft_p99_ms,
                                  MIN_P99_SAMPLES)
        any_low_n |= mark_low_n(axr, g, g[lc], itl_p50_ms)
        any_low_p99 |= mark_low_n(axr, g, g[lc], itl_p99_ms,
                                  MIN_P99_SAMPLES)
        all_loads += g[lc].tolist()
    for ax, title in ((axl, "TTFT (ms)"), (axr, "Inter token latency (ms)")):
        set_load_axis(ax, all_loads, mode)
        ax.set_yscale("log")
        ax.set_ylabel(title)
    tail_legend_kwargs = dict(ncol=2, loc="upper center",
                              bbox_to_anchor=(0.5, -0.18), fontsize=7)
    axl.legend(**tail_legend_kwargs)
    proxies = ([LOW_N_PROXY] if any_low_n else []) + (
        [P99_LOW_N_PROXY] if any_low_p99 else [])
    add_low_n_legend(axl, bool(proxies), proxies=proxies,
                     **tail_legend_kwargs)
    save(fig, "tail_latency", out, fmts)


def fig_goodput(df, out, fmts):
    if not has(df, ["goodput_rps"]):
        return
    fig, ax = plt.subplots(figsize=(COL_W, PANEL_H))
    all_loads = []
    any_low_n = False
    mode = "closed"
    # solid = strict ITL SLO (goodput_rps); dashed = looser ITL tier
    # (goodput_itl2_rps), which recovers the bandwidth-bound models whose strict
    # goodput floors near zero; dotted = achieved req/s ceiling.
    has_itl2 = "goodput_itl2_rps" in df.columns and df["goodput_itl2_rps"].notna().any()
    for i, cfg in enumerate(configs_present(df)):
        g, lc, mode = group(df, cfg)
        s = style_for(cfg, i)
        ax.plot(g[lc], g["goodput_rps"], marker=s["marker"], color=s["color"],
                label=s["label"])
        draw_band(ax, g, g[lc], "goodput_rps", s["color"])
        if has_itl2:
            ax.plot(g[lc], g["goodput_itl2_rps"], marker=s["marker"],
                    linestyle="--", color=s["color"], alpha=0.7,
                    label=f"{s['label']} loose ITL")
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
    mode = "closed"
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
    """Honest two-panel reliability figure.

    The left panel is the real reliability signal: the share of *finished*
    requests that errored (HTTP or stream failures). The right panel is the
    share of requests started in the window that also *completed* inside it --
    which is a backlog/window indicator, not an error rate. At high concurrency
    with a short window this drops toward zero simply because long requests do
    not finish before the window closes; separating the two panels keeps that
    windowing effect from being read as the server dropping requests.
    """
    need_fail = has(df, ["requests_failed", "requests_completed"])
    need_win = has(df, ["requests_started_and_completed_in_window",
                        "requests_completed"])
    if not (need_fail or need_win):
        return
    win_col = "requests_started_and_completed_in_window"
    fig, (axl, axr) = plt.subplots(1, 2, figsize=(TEXT_W, PANEL_H))
    all_loads = []
    any_low_n = False
    mode = "closed"
    max_err = 0.0
    for i, cfg in enumerate(configs_present(df)):
        g, lc, mode = group(df, cfg)
        s = style_for(cfg, i)
        if need_fail:
            finished = g["requests_completed"] + g["requests_failed"]
            err = (g["requests_failed"] / finished.replace(0, np.nan)) * 100
            if np.isfinite(err.values).any():
                max_err = max(max_err, float(np.nanmax(err.values)))
            axl.plot(g[lc], err, marker=s["marker"], color=s["color"],
                     label=s["label"])
        if need_win:
            # Use successful measurement-started requests in both numerator
            # and denominator. Failures belong exclusively in the left panel;
            # letting them lower this ratio would make an error look like
            # backlog despite the panel's stated purpose.
            frac = (g[win_col] / g["requests_completed"].replace(0, np.nan))
            frac = frac.clip(0, 1) * 100
            axr.plot(g[lc], frac, marker=s["marker"], color=s["color"],
                     label=s["label"])
            any_low_n |= mark_low_n(axr, g, g[lc], frac)
        all_loads += g[lc].tolist()
    set_load_axis(axl, all_loads, mode)
    set_load_axis(axr, all_loads, mode)
    axl.set_ylabel("Failed requests (% of finished)")
    # keep a readable band even when every value is zero, so a flat "no errors"
    # result is legible rather than a line pinned to an invisible axis edge
    axl.set_ylim(-0.5, max(5.0, max_err * 1.15))
    if need_fail and max_err == 0:
        axl.text(0.5, 0.5, "no request failures\nacross the sweep",
                 transform=axl.transAxes, ha="center", va="center",
                 fontsize=8, color="0.4")
    elif not need_fail:
        axl.text(0.5, 0.5, "unavailable in index schema",
                 transform=axl.transAxes, ha="center", va="center",
                 fontsize=8, color="0.4")
    axr.set_ylabel("Completed within window (%)")
    axr.set_ylim(0, 105)
    axr.set_title("windowing / backlog, not errors", fontsize=7.5, color="0.35")
    if not need_win:
        axr.text(0.5, 0.5, "unavailable in old index schema",
                 transform=axr.transAxes, ha="center", va="center",
                 fontsize=8, color="0.4")
    if need_fail:
        axl.legend()
    add_low_n_legend(axr, any_low_n)
    save(fig, "reliability", out, fmts)


def fig_energy(df, out, fmts):
    if not has(df, ["energy_per_token_j"]) and not has(df, ["tokens_per_watt"]):
        print("  energy: no power data in index, skipping "
              "(rerun with POWER_BACKEND set)")
        return
    fig, (axl, axr) = plt.subplots(1, 2, figsize=(TEXT_W, PANEL_H))
    gpu_scope = ("power_scope" in df.columns
                 and df["power_scope"].dropna().eq("gpu_devices").all())
    all_loads = []
    mode = "closed"
    caveat_present = False
    for i, cfg in enumerate(configs_present(df)):
        g, lc, mode = group(df, cfg)
        s = style_for(cfg, i)
        if cfg in DUAL_NODE_CONFIGS:
            head_mask = ((~(g["power_nodes"] >= 2)) if "power_nodes" in g.columns
                         else pd.Series(True, index=g.index))
        else:
            head_mask = pd.Series(False, index=g.index)
        caveat_present |= bool(head_mask.any())
        lab = s["label"]
        ls = "--" if cfg in DUAL_NODE_CONFIGS else "-"
        if "energy_per_token_j" in g.columns:
            axl.plot(g[lc], g["energy_per_token_j"], marker=s["marker"],
                     color=s["color"], linestyle=ls, label=lab)
            if head_mask.any():
                axl.scatter(g.loc[head_mask, lc],
                            g.loc[head_mask, "energy_per_token_j"], marker="x",
                            color="0.15", s=35, zorder=7)
            draw_band(axl, g, g[lc], "energy_per_token_j", s["color"])
        if "tokens_per_watt" in g.columns:
            axr.plot(g[lc], g["tokens_per_watt"], marker=s["marker"],
                     color=s["color"], linestyle=ls, label=lab)
            if head_mask.any():
                axr.scatter(g.loc[head_mask, lc],
                            g.loc[head_mask, "tokens_per_watt"], marker="x",
                            color="0.15", s=35, zorder=7)
            draw_band(axr, g, g[lc], "tokens_per_watt", s["color"])
        all_loads += g[lc].tolist()
    set_load_axis(axl, all_loads, mode)
    set_load_axis(axr, all_loads, mode)
    axl.set_ylabel("GPU energy per token (J)" if gpu_scope
                   else "Sampled energy per token (J)")
    axl.set_ylim(bottom=0)
    axr.set_ylabel("Tokens per GPU watt (tok/s/W)" if gpu_scope
                   else "Tokens per sampled watt (tok/s/W)")
    axr.set_ylim(bottom=0)
    axl.legend()
    if caveat_present:
        # one caption for the whole figure rather than crowding both panels
        fig.text(0.5, -0.04,
                 r"$\times$ power sampled on the head node only at marked "
                 r"dual-node points: per-token energy is a lower bound, "
                 r"tokens/W an upper bound.",
                 ha="center", va="top", fontsize=6.5, color="0.30")
    save(fig, "energy", out, fmts, tight=caveat_present)


def _throughput_col(df: pd.DataFrame) -> str:
    """Use exact client token-in-window throughput as the headline rate.

    New runs count returned delta token IDs exactly, including partial streams,
    so this is completion-independent and present for every valid row. The
    server counter remains a separate diagnostic and a fallback for old indexes.
    """
    if "tok_thru_tps" in df.columns and df["tok_thru_tps"].notna().any():
        return "tok_thru_tps"
    return "server_gen_tps"


def fig_server_vs_client(df, out, fmts):
    """Client throughput vs server-side counter throughput per config.

    A methodology diagnostic. With token-in-window accounting the client rate
    should track the server counter closely at every load; a residual gap means
    streaming losses or a window opened off steady state. When the index also
    carries the old completed-requests-only rate (tok_thru_completed_tps), it is
    drawn dotted to show how badly that accounting understates throughput once
    request duration approaches the window (the 60s/600s windowing artifacts).
    """
    if not (has(df, ["tok_thru_tps"]) and "server_gen_tps" in df.columns
            and df["server_gen_tps"].notna().any()):
        return
    have_completed = ("tok_thru_completed_tps" in df.columns
                      and df["tok_thru_completed_tps"].notna().any())
    fig, ax = plt.subplots(figsize=(COL_W, PANEL_H))
    all_loads = []
    mode = "closed"
    for i, cfg in enumerate(configs_present(df)):
        g, lc, mode = group(df, cfg)
        s = style_for(cfg, i)
        ax.plot(g[lc], g["tok_thru_tps"], marker=s["marker"], color=s["color"],
                label=f"{s['label']} client")
        ax.plot(g[lc], g["server_gen_tps"], marker=s["marker"], linestyle="--",
                color=s["color"], alpha=0.7, label=f"{s['label']} server")
        if have_completed:
            ax.plot(g[lc], g["tok_thru_completed_tps"], marker=s["marker"],
                    linestyle=":", color=s["color"], alpha=0.5,
                    label=f"{s['label']} completed-only")
        all_loads += g[lc].tolist()
    set_load_axis(ax, all_loads, mode)
    ax.set_ylabel("Output token throughput (tok/s)")
    ax.set_ylim(bottom=0)
    ax.legend(fontsize=7)
    save(fig, "server_vs_client_throughput", out, fmts)


# ---- cross-model comparison --------------------------------------------------
def _series_rows(df, model, cfg):
    values = _model_values(df)
    if values is None:
        return df[df["config_label"] == cfg]
    return df[(values == str(model)) & (df["config_label"] == cfg)]


def _peak_row(sub, tcol):
    if not len(sub) or tcol not in sub.columns or not sub[tcol].notna().any():
        return None
    return sub.loc[sub[tcol].idxmax()]


def fig_model_comparison(df, out, fmts):
    """Grouped bars comparing models: peak throughput and TTFT at that peak.

    One group per model, one bar per serving config (colored by config, <=3
    hues). Throughput is the peak exact client token-in-window rate over the
    concurrency sweep; TTFT is the median at that same peak point.
    """
    models = models_present(df)
    if len(models) < 2:
        return
    configs = configs_present(df)
    tcol = _throughput_col(df)
    fig, (axl, axr) = plt.subplots(1, 2, figsize=(TEXT_W, PANEL_H + 0.5))
    x = np.arange(len(models))
    width = min(0.8 / max(len(configs), 1), 0.28)
    for j, cfg in enumerate(configs):
        s = style_for(cfg, j)
        peaks, ttfts = [], []
        for model in models:
            row = _peak_row(_series_rows(df, model, cfg), tcol)
            peaks.append(float(row[tcol]) if row is not None else np.nan)
            ttfts.append(float(row["ttft_p50"]) * 1e3
                         if (row is not None and pd.notna(row.get("ttft_p50")))
                         else np.nan)
        off = (j - (len(configs) - 1) / 2) * width
        axl.bar(x + off, peaks, width, color=s["color"], label=s["label"])
        axr.bar(x + off, ttfts, width, color=s["color"], label=s["label"])
    srv = " (server fallback)" if tcol == "server_gen_tps" else ""
    axl.set_ylabel(f"Peak output throughput{srv} (tok/s)")
    axl.set_ylim(bottom=0)
    axr.set_ylabel("Median TTFT at peak (ms)")
    axr.set_yscale("log")
    for ax in (axl, axr):
        ax.set_xticks(x)
        ax.set_xticklabels(models, rotation=30, ha="right", fontsize=7)
        ax.grid(axis="x", visible=False)
    axl.legend(fontsize=7, ncol=len(configs), loc="upper center",
               bbox_to_anchor=(0.5, 1.18))
    save(fig, "model_comparison", out, fmts, tight=True)


# ---- summary table -----------------------------------------------------------
def _fmt(v, spec, low_n=False):
    try:
        if pd.isna(v):
            return "--"
    except (TypeError, ValueError):
        pass
    if v is None:
        return "--"
    try:
        return format(v, spec)
    except (TypeError, ValueError):
        return str(v)


def _yes_no(v):
    try:
        if pd.isna(v):
            return "--"
    except (TypeError, ValueError):
        pass
    if isinstance(v, str):
        return "yes" if v.strip().lower() in {"true", "1", "yes"} else "no"
    return "yes" if bool(v) else "no"


def write_summary_table(df, out) -> None:
    """Write a paper-ready per-point table as LaTeX (booktabs) and Markdown.

    One row per (config, load). Columns are the headline serving numbers a
    reader wants beside the figures: sample size, output-token and request
    throughput, TTFT/ITL/E2E percentiles, goodput, and (when present) per-token
    energy. Rows with fewer than MIN_P99_SAMPLES successful requests are
    daggered because the table includes p99; dual-node energy carries the
    head-node-only caveat.
    """
    os.makedirs(out, exist_ok=True)
    rows = []
    tcol = _throughput_col(df)
    energy_present = has(df, ["energy_per_token_j"])
    gpu_scope = (energy_present and "power_scope" in df.columns
                 and df["power_scope"].dropna().eq("gpu_devices").all())
    models = models_present(df)
    multi_model = len(models) > 1
    # iterate per (model, config) so multiple models are never merged into one
    # config series; single-model indexes collapse to the original behavior
    model_values = _model_values(df)
    for model in (models or [None]):
        msub = (df if model is None or model_values is None
                else df[model_values == model])
        for cfg in configs_present(msub):
            g, lc, _ = group(msub, cfg)
            s = style_for(cfg, 0)
            for _, r in g.iterrows():
                n = r.get("requests_completed", float("nan"))
                n_min = r.get("requests_completed__min", n)
                # This table includes TTFT p99, so apply the tail-specific
                # threshold rather than the p50-only cutoff.
                low = pd.notna(n_min) and n_min < MIN_P99_SAMPLES
                rows.append({
                    "model": model or "",
                    "cfg": s["label"],
                    "load": r.get(lc, float("nan")),
                    "win": r.get("measure_s", float("nan")),
                    "n": n,
                    "low": low,
                    "dual": head_only_power(cfg, pd.DataFrame([r])),
                    "tps": r.get(tcol, float("nan")),
                    "tps_ci": r.get(f"{tcol}__ci95", float("nan")),
                    "rps": r.get("req_thru_rps", float("nan")),
                    "ttft50": (r.get("ttft_p50", float("nan")) * 1e3),
                    "ttft99": (r.get("ttft_p99", float("nan")) * 1e3),
                    "itl50": (r.get("itl_p50", float("nan")) * 1e3),
                    "itl50_ci": (r.get("itl_p50__ci95", float("nan")) * 1e3),
                    "reps": r.get("n_repeats", 1),
                    "e2e50": r.get("e2e_p50", float("nan")),
                    "good": r.get("goodput_rps", float("nan")),
                    "ept": r.get("energy_per_token_j", float("nan")),
                })

    # ---- Markdown ----
    headers = []
    aligns = []
    if multi_model:
        headers.append("Model")
        aligns.append("---")
    headers += ["Config", "Load", "Win (s)", "n", "Tok/s", "Req/s",
                "TTFT p50/p99 (ms)", "ITL p50 (ms)", "E2E p50 (s)",
                "Goodput (r/s)"]
    aligns += ["---", "--:", "--:", "--:", "--:", "--:", "--:", "--:", "--:",
               "--:"]
    if energy_present:
        headers.append("GPU energy (J/tok)" if gpu_scope else "Energy (J/tok)")
        aligns.append("--:")
    def with_ci(value, ci, spec, sep=" ± "):
        text = _fmt(value, spec)
        if pd.notna(ci):
            text += sep + _fmt(ci, spec)
        return text

    any_ci = any(pd.notna(r["tps_ci"]) or pd.notna(r["itl50_ci"]) for r in rows)
    max_reps = max((int(r["reps"]) for r in rows if pd.notna(r["reps"])),
                   default=1)
    md = ["| " + " | ".join(headers) + " |",
          "|" + "|".join(aligns) + "|"]
    for r in rows:
        dag = " †" if r["low"] else ""
        cells = []
        if multi_model:
            cells.append(r["model"])
        cells += [
            r["cfg"], _fmt(r["load"], ".0f"), _fmt(r["win"], ".0f"),
            f"{_fmt(r['n'], '.0f')}{dag}",
            with_ci(r["tps"], r["tps_ci"], ".0f"),
            _fmt(r["rps"], ".2f"),
            f"{_fmt(r['ttft50'], '.0f')}/{_fmt(r['ttft99'], '.0f')}",
            with_ci(r["itl50"], r["itl50_ci"], ".0f"), _fmt(r["e2e50"], ".1f"),
            _fmt(r["good"], ".2f"),
        ]
        if energy_present:
            cells.append(f"{_fmt(r['ept'], '.3f')}{'*' if r['dual'] else ''}")
        md.append("| " + " | ".join(cells) + " |")
    md.append("")
    if any_ci:
        md.append(f"Values are means over up to {max_reps} independent server "
                  "launches; ± is the 95% Student-t half-width of the mean.")
    md.append(f"† fewer than {MIN_P99_SAMPLES} requests; p99 is small-sample "
              "noise.")
    if energy_present and any(r["dual"] for r in rows):
        md.append("* dual-node power sampled on the head node only; per-token "
                  "energy is a lower bound.")
    md_path = os.path.join(out, "summary_table.md")
    with open(md_path, "w") as f:
        f.write("\n".join(md) + "\n")
    print(f"  wrote {md_path}")

    # ---- LaTeX (booktabs) ----
    # Plain right-aligned numeric columns (no siunitx): robust to the "--"
    # missing-value cells and the trailing \rlap daggers, which strict siunitx S
    # columns reject. \rlap keeps the number right-aligned with the marker hung
    # into the gutter. Only \usepackage{booktabs} is required.
    ecol = "r" if energy_present else ""
    ehead = (" & GPU J/tok" if gpu_scope else " & J/tok") if energy_present else ""
    mcolspec = "l " if multi_model else ""
    mhead_tex = "Model & " if multi_model else ""
    lines = [
        r"% Requires \usepackage{booktabs}.",
        r"\begin{tabular}{" + mcolspec + r"l " + "r " * 10 + ecol + "}",
        r"\toprule",
        mhead_tex + r"Config & Load & Win (s) & $n$ & Tok/s & Req/s & "
        r"TTFT$_{50}$ (ms) & TTFT$_{99}$ (ms) & ITL$_{50}$ (ms) & E2E$_{50}$ (s) & "
        r"Goodput" + ehead + r" \\",
        r"\midrule",
    ]
    tex_pm = r" $\pm$ "
    last_key = None
    for r in rows:
        key = (r["model"], r["cfg"])
        if last_key is not None and key != last_key:
            lines.append(r"\addlinespace")
        last_key = key
        dag = r"\rlap{$^\dagger$}" if r["low"] else ""
        mcell = f"{r['model']} & " if multi_model else ""
        eic = ""
        if energy_present:
            star = r"\rlap{$^*$}" if r["dual"] else ""
            eic = f" & {_fmt(r['ept'], '.3f')}{star}"
        tps_cell = with_ci(r["tps"], r["tps_ci"], ".0f", tex_pm)
        itl_cell = with_ci(r["itl50"], r["itl50_ci"], ".0f", tex_pm)
        lines.append(
            f"{mcell}{r['cfg']} & {_fmt(r['load'], '.0f')} & {_fmt(r['win'], '.0f')} & "
            f"{_fmt(r['n'], '.0f')}{dag} & {tps_cell} & "
            f"{_fmt(r['rps'], '.2f')} & {_fmt(r['ttft50'], '.0f')} & "
            f"{_fmt(r['ttft99'], '.0f')} & {itl_cell} & "
            f"{_fmt(r['e2e50'], '.1f')} & "
            f"{_fmt(r['good'], '.2f')}{eic} \\\\")
    lines += [
        r"\bottomrule",
        r"\end{tabular}",
        rf"% $^\dagger$ fewer than {MIN_P99_SAMPLES} requests for p99.",
    ]
    if any_ci:
        lines.append(rf"% Means over up to {max_reps} independent launches; "
                     r"$\pm$ is the 95% Student-t half-width.")
    if energy_present and any(r["dual"] for r in rows):
        lines.append(r"% $^*$ dual-node power sampled on head node only "
                     r"(per-token energy is a lower bound).")
    tex_path = os.path.join(out, "summary_table.tex")
    with open(tex_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"  wrote {tex_path}")


_CAP_COLS = ["weight_gb", "kv_cache_gb", "worker_weight_gb",
             "worker_kv_cache_gb", "total_kv_tokens", "gpu_blocks",
             "block_size", "kv_cache_dtype", "gpu_memory_utilization",
             "max_model_len", "max_concurrency", "total_kv_tokens_source",
             "hybrid_cache", "vllm_version"]


def write_capacity_table(df, out) -> None:
    """Write the per-(model, config) capacity/launch table as Markdown + LaTeX.

    These fields are constant for a given server, so one row per (model, config)
    is taken from the first matching run. Weight and KV cache footprints are
    full-model aggregates (per GPU is roughly the value divided by the parallel
    size for tp2/pp2). Skips silently if no capacity columns are present.
    """
    if not any(c in df.columns and df[c].notna().any() for c in _CAP_COLS):
        return
    os.makedirs(out, exist_ok=True)
    models = models_present(df) or [None]
    model_values = _model_values(df)
    multi_model = len([m for m in models if m is not None]) > 1
    rows = []
    for model in models:
        msub = (df if model is None or model_values is None
                else df[model_values == model])
        for cfg in configs_present(msub):
            g, _, _ = group(msub, cfg)
            r = g.iloc[0]
            rows.append((model or "", style_for(cfg, 0)["label"],
                         {c: r.get(c) for c in _CAP_COLS}))

    hdr = (["Model"] if multi_model else []) + [
        "Config", "HF weight (GiB)", "HF KV (GiB)", "Worker weight (GiB)",
        "Worker KV (GiB)", "KV tokens", "GPU blocks", "Block", "KV dtype",
        "GPU mem util", "max_model_len", "max conc.", "Hybrid cache",
        "KV token source", "vLLM"]

    def cells(model, cfg, cap):
        vals = [
            _fmt(cap["weight_gb"], ".1f"), _fmt(cap["kv_cache_gb"], ".1f"),
            _fmt(cap["worker_weight_gb"], ".1f"),
            _fmt(cap["worker_kv_cache_gb"], ".1f"),
            _fmt(cap["total_kv_tokens"], ",.0f"), _fmt(cap["gpu_blocks"], ",.0f"),
            _fmt(cap["block_size"], ".0f"),
            str(cap["kv_cache_dtype"]) if pd.notna(cap["kv_cache_dtype"]) else "--",
            _fmt(cap["gpu_memory_utilization"], ".2f"),
            _fmt(cap["max_model_len"], ",.0f"),
            _fmt(cap["max_concurrency"], ".1f"),
            _yes_no(cap["hybrid_cache"]),
            str(cap["total_kv_tokens_source"])
            if pd.notna(cap["total_kv_tokens_source"]) else "--",
            str(cap["vllm_version"])
            if pd.notna(cap["vllm_version"]) else "--"]
        return ([model] if multi_model else []) + [cfg] + vals

    md = ["| " + " | ".join(hdr) + " |",
          "|" + "|".join(["---"] + ["--:"] * (len(hdr) - 1)) + "|"]
    for model, cfg, cap in rows:
        md.append("| " + " | ".join(cells(model, cfg, cap)) + " |")
    md += ["", "HF weight/KV cache columns are optional full-model estimates. "
           "Worker weight/KV columns come from the vLLM startup log and describe "
           "the actual per-worker serving process when a server log was captured."]
    md_path = os.path.join(out, "capacity_table.md")
    with open(md_path, "w") as f:
        f.write("\n".join(md) + "\n")
    print(f"  wrote {md_path}")

    ncol = len(hdr)
    tex = [r"% Requires \usepackage{booktabs}.",
           r"\begin{tabular}{l " + "r " * (ncol - 1) + "}",
           r"\toprule",
           " & ".join(hdr).replace("_", r"\_") + r" \\",
           r"\midrule"]
    for model, cfg, cap in rows:
        tex.append(" & ".join(c.replace("_", r"\_") for c in cells(model, cfg, cap))
                   + r" \\")
    tex += [r"\bottomrule", r"\end{tabular}",
            r"% HF weight/KV columns are optional full-model estimates. Worker "
            r"columns come from the vLLM startup log and are per serving worker."]
    tex_path = os.path.join(out, "capacity_table.tex")
    with open(tex_path, "w") as f:
        f.write("\n".join(tex) + "\n")
    print(f"  wrote {tex_path}")


def make_config_figures(df, out, fmts) -> None:
    """The per-configuration figure set for a single model's slice of the index."""
    fig_throughput_scaling(df, out, fmts)
    fig_latency_throughput(df, out, fmts)
    fig_tail_latency(df, out, fmts)
    fig_goodput(df, out, fmts)
    fig_capacity(df, out, fmts)
    fig_reliability(df, out, fmts)
    fig_energy(df, out, fmts)
    fig_server_vs_client(df, out, fmts)


# ---- main --------------------------------------------------------------------
def main() -> None:
    global MIN_SAMPLES, MIN_P99_SAMPLES
    ap = argparse.ArgumentParser(description="Plot PMBS26 figures from index.csv")
    ap.add_argument("--index", help="campaign directory or index.csv path")
    ap.add_argument("--out", help="output directory (default: campaign/figs)")
    ap.add_argument("--format", default="pdf", choices=["pdf", "png", "both"])
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--serif", action="store_true",
                    help="use a Times-like serif font to match an IEEE paper body")
    ap.add_argument("--table", action="store_true",
                    help="also write summary_table and capacity_table (.tex/.md)")
    ap.add_argument(
        "--allow-incomplete", action="store_true",
        help="diagnostic only: plot missing intended loads/repeats with warnings",
    )
    ap.add_argument("--model", default=None,
                    help="restrict to a model label; with --poster, select "
                         "the poster model (default: Qwen3-Next NVFP4). With "
                         "several models, the default is cross-model and "
                         "per-model figures")
    ap.add_argument("--poster", action="store_true",
                    help="write the four poster figures only; defaults to "
                         "Qwen3-Next NVFP4 or uses --model")
    ap.add_argument(
        "--aspect-ratio",
        type=parse_poster_aspect_ratio,
        help=(
            "poster figure width-to-height ratio "
            f"(default: {POSTER_DEFAULT_ASPECT_RATIO:g}); requires --poster"
        ),
    )
    ap.add_argument("--min-samples", type=int, default=MIN_SAMPLES,
                    help="successful requests below this make median percentiles "
                         "unreliable; such points are circled (default: %(default)s)")
    ap.add_argument("--min-p99-samples", type=int, default=MIN_P99_SAMPLES,
                    help="requests below this make p99 unreliable "
                         "(default: %(default)s)")
    args = ap.parse_args()
    if args.aspect_ratio is not None and not args.poster:
        ap.error("--aspect-ratio requires --poster")

    index_path = resolve_index_path(args.index)
    args.index = str(index_path)
    args.out = str(resolve_campaign_output_path(args.out, index_path))

    MIN_SAMPLES = args.min_samples
    MIN_P99_SAMPLES = args.min_p99_samples
    LOW_N_PROXY.set_label(f"n < {MIN_SAMPLES} (unreliable)")
    P99_LOW_N_PROXY.set_label(f"p99 n < {MIN_P99_SAMPLES}")
    fmts = ["pdf", "png"] if args.format == "both" else [args.format]
    set_rc(args.dpi, args.serif)
    df = load_index(args.index)

    if args.poster:
        poster_model = args.model or POSTER_MODEL_LABEL
        model_values = _model_values(df)
        if model_values is None:
            sys.exit("--poster requires model_label or model in the index")
        poster_df = df[model_values == poster_model].copy()
        if poster_df.empty:
            sys.exit(f"no rows for poster model '{poster_model}' in {args.index}")
        if "concurrency" not in poster_df.columns:
            sys.exit("poster index has no concurrency column")
        poster_df = poster_df[
            poster_df["concurrency"].isin(POSTER_CONCURRENCIES)
        ].copy()
        if "expected_concurrencies" in poster_df.columns:
            # Repeat validation uses the displayed schedule after the plotting filter.
            poster_df["expected_concurrencies"] = " ".join(
                str(value) for value in POSTER_CONCURRENCIES
            )
        poster_df = collapse_repeats(poster_df)
        poster_out = os.path.join(args.out, "poster", _slug(poster_model))
        make_poster_figures(
            poster_df,
            poster_out,
            fmts,
            aspect_ratio=(
                args.aspect_ratio
                if args.aspect_ratio is not None
                else POSTER_DEFAULT_ASPECT_RATIO
            ),
        )
        print("poster figures use concurrency values 1, 2, 4, 8, 16, 32, and 64")
        print("done")
        return

    if args.model is not None:
        model_values = _model_values(df)
        if model_values is None:
            sys.exit("--model given but the index has no model column")
        df = df[model_values == args.model]
        if not len(df):
            sys.exit(f"no rows for model '{args.model}' in {args.index}")

    models = models_present(df)
    print(f"loaded {len(df)} rows, "
          + (f"models: {', '.join(models)}, " if models else "")
          + f"configs: {', '.join(configs_present(df))}")
    df = collapse_repeats(df, allow_incomplete=args.allow_incomplete)
    report_quality(df)
    warn_low_n(df)

    if len(models) > 1:
        # cross-model summary at the top level, then a full figure set per model
        fig_model_comparison(df, args.out, fmts)
        model_values = _model_values(df)
        for model in models:
            sub = df[model_values == model]
            make_config_figures(sub, os.path.join(args.out, _slug(model)), fmts)
    else:
        make_config_figures(df, args.out, fmts)

    if args.table:
        write_summary_table(df, args.out)
        write_capacity_table(df, args.out)
    print("done")


if __name__ == "__main__":
    main()
