#!/usr/bin/env python3
"""Perftest-suite RDMA figures for the PMBS26 paper.

Reads every host-side results.csv under the perftest campaign tree
(perftest-results/<campaign>/run-*/host/ next to this script) and writes three
figures for the paper:

  perftest_bandwidth   send, write, and read bandwidth per device at the
                       suite's fixed message size, unidirectional and
                       bidirectional, against the captured link rate.
  perftest_latency     send and write latency per perftest percentile tier
                       (min, typical, mean, p99, p99.9) at the suite's 2 B
                       latency messages, plus each repeat's t_max as a strip.
  perftest_stability   each repeat's deviation from its (campaign, device,
                       test, bidi) run mean, which is the repeatability
                       evidence behind the min-max whiskers.

Two coverage facts the figures depend on. Perftest prints the read and atomic
latency tables on the client side, so those two tests carry error rows in the
host-side CSVs and appear in no figure. ib_atomic_bw runs at the perftest
default 8 B, which is not comparable with the suite's 64 KiB bandwidth tests;
report_coverage() prints its measured message rate instead of drawing it. The
loader refuses a campaign that sweeps message sizes under one test, because
the per-device figures assume one comparable size per test.

Usage:
    python rdma-testing/perftest-suite/plot_perftest.py
    python rdma-testing/perftest-suite/plot_perftest.py \
        --results <campaign tree> --out <figure directory>
    python rdma-testing/perftest-suite/plot_perftest.py --sans

The script imports only numpy, pandas, and matplotlib, so it runs from any
location. The style block below mirrors plotting/plot_results.py,
plot_paper.py, and plot_trends.py in this repository; keep the copies in step
when the paper style changes.

The defaults read and write next to this script (perftest-results/ and figs/),
so the runner's default output tree plots with no arguments from any working
directory. Bandwidth values come from the CSVs' decimal GB/s column
(perftest's --report_gbits output divided by 8).
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")                      # headless, safe over ssh
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter, NullFormatter


# ---- paper figure style ------------------------------------------------------
# Inlined from the repository's plotting scripts so the suite is standalone:
# rcParams from plot_results.py:set_rc(), the Type 42 font hardening from
# plot_paper.py:_publication_rc(), and the log-tick cleaner from
# plot_trends.py:clean_log_ticks().
COL_W = 3.5      # one IEEE column, inches
TEXT_W = 7.0     # full text width, inches
SERIF_FAMILIES = ["Times New Roman", "Liberation Serif", "DejaVu Serif"]


def apply_paper_style(dpi: int, serif: bool) -> None:
    """Set the rcParams the repository's paper figures use.

    IEEE PDF eXpress rejects matplotlib's default Type 3 font subsetting, so
    fonts are embedded as TrueType (Type 42) outlines in both font modes.
    Mathtext is mapped to a concrete serif family when one is installed:
    naming an absent font drops mathtext to DejaVu Sans rather than down the
    font.serif fallback list.
    """
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
        plt.rcParams["font.family"] = "serif"
        plt.rcParams["font.serif"] = list(SERIF_FAMILIES)
        plt.rcParams["mathtext.fontset"] = "dejavuserif"
    installed = {f.name for f in font_manager.fontManager.ttflist}
    family = next((f for f in SERIF_FAMILIES if f in installed), None)
    if family is not None:
        plt.rcParams.update({
            "mathtext.fontset": "custom",
            "mathtext.rm": family,
            "mathtext.it": f"{family}:italic",
            "mathtext.bf": f"{family}:bold",
            "mathtext.default": "regular",
        })


def clean_log_ticks(ax: Axes, axis: str = "y", max_ticks: int = 6) -> None:
    """Round-number major ticks for a log axis, replacing scientific notation.

    Mantissa sets are tried densest first (1-2-5, then 1-3, then decades) and
    the first that fits max_ticks wins, so a two-decade axis carries plain
    numbers instead of matplotlib's 2x10^2-style minor labels.
    """
    lo, hi = ax.get_ylim() if axis == "y" else ax.get_xlim()
    if not (lo > 0 and hi > lo):
        return
    ticks: list[float] = []
    for mants in ((1, 2, 5), (1, 3), (1,)):
        ticks = [m * 10.0 ** exponent
                 for exponent in range(math.floor(math.log10(lo)),
                                       math.ceil(math.log10(hi)) + 1)
                 for m in mants]
        ticks = [t for t in ticks if lo <= t <= hi]
        if len(ticks) <= max_ticks:
            break
    if len(ticks) < 2:
        return
    which = ax.yaxis if axis == "y" else ax.xaxis
    which.set_ticks(ticks)
    which.set_major_formatter(FuncFormatter(lambda value, _: f"{value:g}"))
    which.set_minor_formatter(NullFormatter())
    # Fixed major ticks can widen the view limits; restore the caller's.
    (ax.set_ylim if axis == "y" else ax.set_xlim)(lo, hi)


def save_figure(fig: Figure, name: str, out: str, formats: list[str],
                *, tight: bool = False) -> None:
    """Write one file per format under out/ and close the figure.

    tight=True expands the saved bounding box to include manual fig.text
    footnotes, which constrained_layout does not reserve room for.
    """
    os.makedirs(out, exist_ok=True)
    keyword = dict(bbox_inches="tight") if tight else {}
    for fmt in formats:
        path = os.path.join(out, f"{name}.{fmt}")
        fig.savefig(path, **keyword)
        print(f"  wrote {path}")
    plt.close(fig)


SCRIPT_DIR = Path(__file__).resolve().parent


# Bandwidth verbs plotted from the suite's fixed 64 KiB tests. atomic_bw is
# measured at 8 B and is reported as message rate by report_coverage().
VERBS = ("send", "write", "read")
LATENCY_VERBS = ("send", "write")
MEASUREMENT_COLUMNS = (
    "size_bytes", "bw_peak_GBps", "bw_avg_GBps", "msg_rate_Mpps",
    "lat_min_usec", "lat_max_usec", "lat_typical_usec", "lat_avg_usec",
    "lat_stdev_usec", "lat_p99_usec", "lat_p999_usec",
)
REQUIRED_COLUMNS = frozenset(
    {"test", "device", "role", "bidi", "error"} | set(MEASUREMENT_COLUMNS))

# Bar hues per verb, from the Okabe-Ito set shared by the other paper figures.
VERB_STYLE = {
    "send": "#0072B2",
    "write": "#D55E00",
    "read": "#009E73",
}
# Verb is encoded by marker and linestyle in the latency figure, where hue
# carries the campaign instead.
VERB_LINE = {
    "send": dict(marker="o", linestyle="-"),
    "write": dict(marker="s", linestyle="--"),
}

# Campaign directories as written by runner.sh --campaign. short feeds the
# bandwidth figure's two-line device ticks; label feeds the legends. Unknown
# campaign names fall back to the raw name with the next palette entry, so a
# new campaign plots without edits here.
CAMPAIGN_STYLE = {
    "test-oct-6": dict(short="1 cable", label="1 cable (2 devices)",
                       color="#0072B2", marker="o"),
    "dual-200Gb-Oct-8": dict(short="2 cables", label="2 cables (4 devices)",
                             color="#D55E00", marker="s"),
}
_FALLBACK_CAMPAIGN_STYLE = (
    dict(color="#009E73", marker="^"),
    dict(color="#CC79A7", marker="D"),
    dict(color="#E69F00", marker="v"),
    dict(color="#56B4E9", marker="P"),
)

# perftest's latency summary columns in increasing magnitude order.
LATENCY_TIERS = (
    ("min", "lat_min_usec"),
    ("typical", "lat_typical_usec"),
    ("mean", "lat_avg_usec"),
    ("p99", "lat_p99_usec"),
    ("p99.9", "lat_p999_usec"),
)
REFERENCE_COLOR = "0.35"
FOOTNOTE_FS = 6.0
FOOTNOTE_COLOR = "0.25"
# A deviation needs at least two runs to be a repeatability reading; a
# single-run group is zero by construction and would understate the spread.
MIN_REPEAT_RUNS = 2
# Deterministic horizontal offsets for strip points, patterned on the
# variance figure's cycling JIT so repeated renders are identical.
_STRIP_JITTER = np.linspace(-0.28, 0.28, 7)


def _campaign_run(results_root: str, path: str) -> tuple[str, int]:
    relative = os.path.relpath(path, results_root).split(os.sep)
    return relative[0], int(relative[1].split("-")[1])


def load_perftest_results(results_root: str) -> pd.DataFrame:
    """Load every host-side results.csv under <results_root>/<campaign>/run-*/.

    The glob fixes the role to host, so no role filtering is needed after the
    read. Numeric cells are coerced; the error column is kept verbatim so the
    coverage report can list failed tests. A CSV missing a required column
    raises with the file named.
    """
    paths = sorted(glob.glob(os.path.join(
        results_root, "*", "run-*", "host", "results.csv")))
    if not paths:
        raise SystemExit(f"no perftest results.csv under {results_root}")
    frames = []
    for path in paths:
        campaign, run = _campaign_run(results_root, path)
        frame = pd.read_csv(path)
        missing = REQUIRED_COLUMNS.difference(frame.columns)
        if missing:
            raise ValueError(f"{path}: missing column(s) {sorted(missing)}")
        frame["campaign"] = campaign
        frame["run"] = run
        frames.append(frame)
    df = pd.concat(frames, ignore_index=True)
    for column in MEASUREMENT_COLUMNS:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df["error"] = df["error"].fillna("").astype(str).str.strip()
    bidi_text = df["bidi"].fillna("").astype(str).str.strip().str.lower()
    df["bidi"] = bidi_text.map({"true": True, "false": False})
    if df["bidi"].isna().any():
        bad = sorted(bidi_text[df["bidi"].isna()].unique())
        raise ValueError(f"unrecognized bidi value(s): {bad}")
    parsed = df["test"].astype(str).str.extract(
        r"^ib_(?P<verb>[a-z]+)_(?P<kind>lat|bw)$")
    if parsed["kind"].isna().any():
        bad = sorted(df["test"][parsed["kind"].isna()].unique())
        raise ValueError(f"unrecognized test name(s): {bad}")
    df["verb"] = parsed["verb"]
    df["kind"] = parsed["kind"]

    measured = df[df["error"] == ""]
    sizes = measured.groupby(["test", "bidi"])["size_bytes"].nunique(dropna=True)
    swept = sizes[sizes > 1]
    if len(swept):
        detail = ", ".join(f"{test} (bidi={bidi})" for test, bidi in swept.index)
        raise ValueError(
            "message-size sweeps are not supported by these per-device "
            f"figures: {detail}")
    return df


def load_link_speeds(results_root: str) -> pd.DataFrame:
    """Per-run link rate from each run's captured environment/link.json."""
    rows = []
    for path in sorted(glob.glob(os.path.join(
            results_root, "*", "run-*", "host", "environment", "*",
            "link.json"))):
        campaign, run = _campaign_run(results_root, path)
        with open(path) as handle:
            link = json.load(handle)
        rows.append({
            "campaign": campaign,
            "run": run,
            "device": link.get("device") or os.path.basename(
                os.path.dirname(path)),
            "link_speed_mbps": link.get("link_speed_mbps"),
        })
    if not rows:
        return pd.DataFrame(
            columns=["campaign", "run", "device", "link_speed_mbps"])
    df = pd.DataFrame(rows)
    df["link_speed_mbps"] = pd.to_numeric(
        df["link_speed_mbps"], errors="coerce")
    return df


def reference_line_rate_gbps(link_speeds: pd.DataFrame) -> float | None:
    """The single captured link rate in GB/s, None when it is not unique.

    One rule cannot label mixed rates correctly, so a capture with more than
    one value omits the rule and prints the values instead.
    """
    unique = sorted({float(v) for v in link_speeds["link_speed_mbps"].dropna()})
    if not unique:
        print("  reference: no link_speed_mbps captured, line-rate rule omitted")
        return None
    if len(unique) != 1:
        print(f"  reference: captured link rates {unique} Mb/s, "
              "line-rate rule omitted")
        return None
    return unique[0] / 8000.0          # Mb/s to decimal GB/s


def campaigns_in_order(df: pd.DataFrame) -> list[str]:
    """Campaigns by device count then name, so 1-cable precedes 2-cable."""
    counts = df.groupby("campaign")["device"].nunique()
    return sorted(counts.index, key=lambda name: (counts[name], name))


def devices_in_order(df: pd.DataFrame) -> list[str]:
    """Device names in port order; casefold puts f0 before P2p1s0f0."""
    return sorted(df["device"].unique(), key=str.casefold)


def campaign_style(campaign: str, order: list[str]) -> dict[str, str]:
    if campaign in CAMPAIGN_STYLE:
        return CAMPAIGN_STYLE[campaign]
    fallback = _FALLBACK_CAMPAIGN_STYLE[order.index(campaign)
                                       % len(_FALLBACK_CAMPAIGN_STYLE)]
    return dict(short=campaign, label=campaign, **fallback)


def _size_label(size_bytes: float) -> str:
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:g} KiB"
    return f"{size_bytes:g} B"


def _device_short(device: str) -> str:
    """Device name without its constant roce prefix, for tick labels.

    RoCE device names are 'roce' plus the netdev name (rocep1s0f0). Dropping
    the prefix keeps every device distinguishable at a grouped-bar tick width;
    the coverage report prints the full names.
    """
    return device.removeprefix("roce")


def repeat_deviations(
        frame: pd.DataFrame, value_column: str) -> tuple[pd.DataFrame, int]:
    """Per-run deviation (%) from the run mean of its repeat group.

    The group key is (campaign, device, test, bidi), so the result isolates
    run-to-run scatter from device and campaign level offsets. Groups with
    fewer than MIN_REPEAT_RUNS runs are returned as an exclusion count.
    """
    keys = ["campaign", "device", "test", "bidi"]
    sub = frame[frame[value_column].notna()].copy()
    sub["repeat_count"] = sub.groupby(keys)[value_column].transform("count")
    sub["repeat_mean"] = sub.groupby(keys)[value_column].transform("mean")
    sub["deviation_pct"] = ((sub[value_column] - sub["repeat_mean"])
                            / sub["repeat_mean"] * 100.0)
    excluded = int((sub["repeat_count"] < MIN_REPEAT_RUNS).sum())
    return sub[sub["repeat_count"] >= MIN_REPEAT_RUNS], excluded


def _results_summary(df: pd.DataFrame) -> str:
    """Campaign, run, and device totals for the coverage header."""
    campaigns = df["campaign"].nunique()
    devices = int(df.groupby("campaign")["device"].nunique().sum())
    runs = int(df.groupby(["campaign", "run"]).ngroups)
    return (f"{campaigns} campaign(s), {runs} run(s), "
            f"{devices} campaign-device pair(s)")


def report_coverage(df: pd.DataFrame, link_speeds: pd.DataFrame,
                    results_root: str) -> None:
    """Print runs, devices, measured-row counts, and captured link rates.

    Failed tests stay in results.csv as error rows. Listing every test whose
    measured row count falls short of its run count makes a missing test
    visible instead of silently absent from the figures.
    """
    print("coverage")
    print(f"  source: {len(df)} row(s) from {_results_summary(df)}")
    client_files = glob.glob(os.path.join(
        results_root, "*", "run-*", "client", "results.csv"))
    if client_files:
        print(f"  note: {len(client_files)} client-side results.csv present; "
              "this figure set reads host-side files only")
    else:
        print("  note: host-side results.csv only; the tree holds no "
              "client/results.csv")
    for campaign in sorted(df["campaign"].unique()):
        csub = df[df["campaign"] == campaign]
        runs = sorted(int(r) for r in csub["run"].unique())
        devices = devices_in_order(csub)
        print(f"  {campaign}: {len(runs)} runs ({runs[0]}..{runs[-1]}), "
              f"{len(devices)} device(s): {', '.join(devices)}")
        for test in sorted(csub["test"].unique()):
            for bidi_flag in (False, True):
                sub = csub[(csub["test"] == test)
                           & (csub["bidi"] == bidi_flag)]
                if sub.empty:
                    continue
                measured = int((sub["error"] == "").sum())
                if measured < len(sub):
                    tag = "bidi" if bidi_flag else "uni"
                    errors = sorted(set(
                        sub.loc[sub["error"] != "", "error"]))
                    print(f"    {test} ({tag}): {measured}/{len(sub)} rows "
                          f"measured, error rows: {', '.join(errors)}")
    if len(link_speeds):
        for campaign, group in link_speeds.groupby("campaign"):
            values = sorted({float(v) for v in group["link_speed_mbps"].dropna()})
            shown = ", ".join(f"{v:.0f}" for v in values) if values else "none"
            print(f"  link speed: {campaign}: {shown} Mb/s")
    atomic = df[(df["error"] == "") & (df["test"] == "ib_atomic_bw")]
    if len(atomic):
        sizes = ", ".join(
            _size_label(s) for s in sorted(atomic["size_bytes"].unique()))
        rates = atomic["msg_rate_Mpps"].dropna()
        print(f"  ib_atomic_bw: {len(atomic)} row(s) at {sizes}, "
              f"{rates.min():.2f}-{rates.max():.2f} Mpps; not plotted because "
              "its message size is not comparable with the 64 KiB bandwidth "
              "tests")


def fig_bandwidth(df: pd.DataFrame, out: str, fmts: list[str],
                  line_rate_gbps: float | None) -> None:
    """Per-device bandwidth at the suite's tested message size.

    Bars are means over each campaign's repeats with min-max whiskers.
    perftest -b reports the sum of both directions, so the bidirectional
    panel's reference rule is twice the line rate. The host-side ib_send_bw
    peak cell is zero by perftest design, so only the average column is read.
    """
    measured = df[df["error"] == ""]
    bw = measured[(measured["kind"] == "bw") & measured["verb"].isin(VERBS)
                  & measured["bw_avg_GBps"].notna()]
    if bw.empty:
        print("  perftest_bandwidth: no measured bandwidth rows, skipping")
        return
    campaigns = campaigns_in_order(bw)
    devices = devices_in_order(bw)
    groups = [(campaign, device) for campaign in campaigns for device in devices
              if ((bw["campaign"] == campaign)
                  & (bw["device"] == device)).any()]
    counts = bw.groupby(["campaign", "device", "test", "bidi"])["run"].count()
    runs_text = (f"{int(counts.min())} repeats per device"
                 if counts.min() == counts.max()
                 else f"{int(counts.min())}-{int(counts.max())} repeats per device")
    sizes = ", ".join(_size_label(s) for s in sorted(bw["size_bytes"].unique()))

    fig, axes = plt.subplots(1, 2, figsize=(TEXT_W, 2.8))
    x = np.arange(len(groups))
    width = 0.8 / len(VERBS)
    for ax, bidi_flag, title, reference in (
            (axes[0], False, "(a) unidirectional", line_rate_gbps),
            (axes[1], True, "(b) bidirectional (both directions summed)",
             2 * line_rate_gbps if line_rate_gbps else None)):
        highs_all: list[float] = []
        for column, verb in enumerate(VERBS):
            means, lows, highs = [], [], []
            for campaign, device in groups:
                values = bw[(bw["campaign"] == campaign)
                            & (bw["device"] == device)
                            & (bw["verb"] == verb)
                            & (bw["bidi"] == bidi_flag)]["bw_avg_GBps"]
                means.append(float(values.mean()) if len(values) else np.nan)
                lows.append(float(values.min()) if len(values) else np.nan)
                highs.append(float(values.max()) if len(values) else np.nan)
            offset = (column - (len(VERBS) - 1) / 2) * width
            ax.bar(x + offset, means, width * 0.9, color=VERB_STYLE[verb],
                   label=verb, linewidth=0)
            mean_v, low_v, high_v = map(np.asarray, (means, lows, highs))
            has_bar = np.isfinite(mean_v)
            if has_bar.any():
                ax.errorbar(
                    x[has_bar] + offset, mean_v[has_bar],
                    yerr=np.vstack([mean_v[has_bar] - low_v[has_bar],
                                    high_v[has_bar] - mean_v[has_bar]]),
                    fmt="none", ecolor="0.15", elinewidth=0.7,
                    capsize=1.5, capthick=0.7)
            highs_all.extend(float(v) for v in high_v if np.isfinite(v))
        top = max(highs_all, default=0.0)
        if reference is not None:
            ax.axhline(reference, color=REFERENCE_COLOR, linestyle=":",
                       linewidth=0.9)
            gbps = reference * 8
            if bidi_flag:
                text = (f"full duplex {gbps:.0f} Gb/s "
                        f"({reference:.0f} GB/s aggregate)")
            else:
                text = f"line rate {gbps:.0f} Gb/s ({reference:.0f} GB/s)"
            ax.annotate(text, xy=(len(groups) - 0.4, reference),
                        xytext=(0, -2), textcoords="offset points",
                        ha="right", va="top", fontsize=6.0,
                        color=FOOTNOTE_COLOR)
            top = max(top, reference)
        ax.set_ylim(0, top * 1.15)
        ax.set_xticks(x)
        ax.set_xticklabels(
            [f"{campaign_style(campaign, campaigns)['short']}\n"
             f"{_device_short(device)}" for campaign, device in groups],
            fontsize=6.2)
        ax.set_title(title, fontsize=8)
        ax.grid(axis="x", visible=False)
        ax.legend(fontsize=6.5, ncol=len(VERBS), loc="upper left",
                  handlelength=1.1, columnspacing=1.0, handletextpad=0.4)
    axes[0].set_ylabel("Bandwidth (GB/s)", fontsize=8)
    fig.text(0.5, -0.01,
             f"host-side perftest tables; {sizes} messages; {runs_text}; "
             "whiskers = min-max over repeats",
             ha="center", fontsize=FOOTNOTE_FS, color=FOOTNOTE_COLOR)
    save_figure(fig, "perftest_bandwidth", out, fmts, tight=True)


def fig_latency(df: pd.DataFrame, out: str, fmts: list[str]) -> None:
    """Send and write latency by perftest percentile tier.

    One line per campaign and verb with the repeat-and-device min-max band
    behind it. t_max is the single worst sample of a run rather than a
    percentile, so each run's t_max is drawn as one strip point at the right
    edge instead of a summary line.
    """
    measured = df[df["error"] == ""]
    lat = measured[(measured["kind"] == "lat")
                   & measured["verb"].isin(LATENCY_VERBS)
                   & measured["lat_typical_usec"].notna()]
    if lat.empty:
        print("  perftest_latency: no measured latency rows, skipping")
        return
    campaigns = campaigns_in_order(lat)
    sizes = ", ".join(_size_label(s) for s in sorted(lat["size_bytes"].unique()))

    fig, ax = plt.subplots(figsize=(COL_W, 2.8))
    xs = np.arange(len(LATENCY_TIERS))
    x_strip = float(len(LATENCY_TIERS))
    maxes_all: list[float] = []
    for campaign in campaigns:
        style = campaign_style(campaign, campaigns)
        for verb in LATENCY_VERBS:
            sub = lat[(lat["campaign"] == campaign) & (lat["verb"] == verb)]
            if sub.empty:
                continue
            means, lows, highs = [], [], []
            for _, column in LATENCY_TIERS:
                values = sub[column].dropna()
                means.append(float(values.mean()) if len(values) else np.nan)
                lows.append(float(values.min()) if len(values) else np.nan)
                highs.append(float(values.max()) if len(values) else np.nan)
            line = VERB_LINE[verb]
            ax.plot(xs, means, color=style["color"], markersize=3.0,
                    linewidth=1.1, zorder=3, **line)
            ax.fill_between(xs, lows, highs, color=style["color"],
                            alpha=0.12, linewidth=0, zorder=2)
            maxes = sub["lat_max_usec"].dropna().to_numpy()
            if len(maxes):
                offsets = np.array([_STRIP_JITTER[k % len(_STRIP_JITTER)]
                                    for k in range(len(maxes))])
                ax.scatter(x_strip + offsets, maxes, s=6, facecolors="none",
                           edgecolors=style["color"], linewidths=0.5,
                           alpha=0.6, zorder=3)
                maxes_all.extend(float(v) for v in maxes)
    ax.set_xticks(list(xs) + [x_strip])
    ax.set_xticklabels([tier for tier, _ in LATENCY_TIERS] + ["max\n(per run)"],
                       fontsize=6.5)
    ax.set_yscale("log")
    ax.set_ylim(1.2, max(3.0, max(maxes_all, default=0.0) * 1.3))
    clean_log_ticks(ax)
    ax.set_ylabel("One-way latency (µs)", fontsize=8)
    handles = [Line2D([], [], color=campaign_style(c, campaigns)["color"],
                      label=campaign_style(c, campaigns)["label"])
               for c in campaigns]
    handles += [Line2D([], [], color="0.35", markersize=3, label=verb,
                       **VERB_LINE[verb]) for verb in LATENCY_VERBS]
    ax.legend(handles=handles, fontsize=6.2, ncol=2, loc="upper left",
              handlelength=1.4, columnspacing=1.0, handletextpad=0.4)
    fig.text(0.5, -0.02,
             f"host-side perftest tables; {sizes} messages; "
             "band = min-max across each campaign's repeats and devices",
             ha="center", fontsize=FOOTNOTE_FS, color=FOOTNOTE_COLOR)
    save_figure(fig, "perftest_latency", out, fmts, tight=True)


def fig_stability(df: pd.DataFrame, out: str, fmts: list[str]) -> None:
    """Repeat-to-repeat scatter around each repeat group's mean.

    Normalizing per (campaign, device, test, bidi) removes device and campaign
    level offsets, so the strips show run-to-run noise. Each panel reports its
    own standard deviation and worst absolute deviation.
    """
    measured = df[df["error"] == ""]
    bw = measured[(measured["kind"] == "bw") & measured["verb"].isin(VERBS)
                  & measured["bw_avg_GBps"].notna()]
    lat = measured[(measured["kind"] == "lat")
                   & measured["verb"].isin(LATENCY_VERBS)]
    if bw.empty and lat.empty:
        print("  perftest_stability: no measured rows, skipping")
        return
    campaigns = campaigns_in_order(measured)
    fig, (ax_bw, ax_lat) = plt.subplots(
        1, 2, figsize=(TEXT_W, 2.5),
        gridspec_kw=dict(width_ratios=(2.3, 1.0)))

    bw_specs = [(f"{verb}\n{'bidi' if bidi else 'uni'}", "bw_avg_GBps",
                 (bw["verb"] == verb) & (bw["bidi"] == bidi))
                for bidi in (False, True) for verb in VERBS]
    lat_specs = [(f"{verb}\n{name}", column, lat["verb"] == verb)
                 for verb in LATENCY_VERBS
                 for name, column in (("typical", "lat_typical_usec"),
                                      ("p99", "lat_p99_usec"))]
    for ax, frame, specs, title in (
            (ax_bw, bw, bw_specs, "(a) bandwidth"),
            (ax_lat, lat, lat_specs, "(b) latency")):
        deviations = _draw_repeat_strip(ax, frame, specs, campaigns)
        ax.set_title(title, fontsize=8)
        ax.set_xticks(range(len(specs)))
        ax.set_xticklabels([label for label, _, _ in specs], fontsize=6.2)
        ax.grid(axis="x", visible=False)
        ax.set_ylabel("Deviation from repeat mean (%)", fontsize=7.5)
        if deviations:
            span = max(1.0, float(np.abs(deviations).max()) * 1.3)
            ax.set_ylim(-span, span)
            ax.annotate(
                f"σ = {np.std(deviations, ddof=1):.2f}%   "
                f"max |dev| = {np.abs(deviations).max():.2f}%",
                xy=(0.99, 0.03), xycoords="axes fraction", ha="right",
                fontsize=6.0, color=FOOTNOTE_COLOR)
    ax_bw.axvline(len(VERBS) - 0.5, color="0.85", linewidth=0.7, zorder=0)
    ax_bw.annotate("unidirectional", xy=(1.0, 1.0), xycoords=("data", "axes fraction"),
                   xytext=(0, 2), textcoords="offset points", ha="center",
                   fontsize=6.5, color=FOOTNOTE_COLOR)
    ax_bw.annotate("bidirectional", xy=(1.0 + len(VERBS), 1.0),
                   xycoords=("data", "axes fraction"), xytext=(0, 2),
                   textcoords="offset points", ha="center", fontsize=6.5,
                   color=FOOTNOTE_COLOR)
    save_figure(fig, "perftest_stability", out, fmts)


def _draw_repeat_strip(ax: Axes, frame: pd.DataFrame,
                       specs: list[tuple[str, str, pd.Series]],
                       campaigns: list[str]) -> list[float]:
    """Strip one deviation cloud per spec; return every plotted deviation."""
    ax.axhline(0, color="0.6", linewidth=0.7, zorder=0)
    deviations: list[float] = []
    for index, (label, value_column, mask) in enumerate(specs):
        points, excluded = repeat_deviations(frame[mask], value_column)
        if excluded:
            print(f"  stability: {label!r}: excluded {excluded} row(s) in "
                  f"groups with fewer than {MIN_REPEAT_RUNS} runs")
        for campaign in campaigns:
            csub = points[points["campaign"] == campaign]
            if csub.empty:
                continue
            style = campaign_style(campaign, campaigns)
            values = csub["deviation_pct"].to_numpy()
            offsets = np.array([_STRIP_JITTER[k % len(_STRIP_JITTER)]
                                for k in range(len(values))])
            ax.scatter(index + offsets, values, s=6, color=style["color"],
                       marker=style["marker"], linewidths=0, alpha=0.55,
                       zorder=3)
            deviations.extend(float(v) for v in values)
    return deviations


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Perftest-suite RDMA figures for the PMBS26 paper")
    parser.add_argument(
        "--results", default=str(SCRIPT_DIR / "perftest-results"),
        help="campaign tree holding <campaign>/run-*/host/results.csv")
    parser.add_argument(
        "--out", default=str(SCRIPT_DIR / "figs"),
        help="figure output directory")
    parser.add_argument("--format", default="both",
                        choices=["pdf", "png", "both"])
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--sans", action="store_true",
                        help="keep the sans-serif font (default is the IEEE "
                             "Times-like serif)")
    args = parser.parse_args()

    formats = ["pdf", "png"] if args.format == "both" else [args.format]
    apply_paper_style(args.dpi, serif=not args.sans)
    df = load_perftest_results(args.results)
    link_speeds = load_link_speeds(args.results)
    report_coverage(df, link_speeds, args.results)
    line_rate = reference_line_rate_gbps(link_speeds)
    print("perftest figures")
    fig_bandwidth(df, args.out, formats, line_rate)
    fig_latency(df, args.out, formats)
    fig_stability(df, args.out, formats)
    print("done")


if __name__ == "__main__":
    main()
