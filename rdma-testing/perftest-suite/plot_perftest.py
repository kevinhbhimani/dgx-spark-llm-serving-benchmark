"""
   If you are looking at this file this is definitely total garbage right now.
   I had an llm whip it up just to visualize results quickly and, shocker,
   it did an awful job.
"""


"""plot_perftest.py -- figures for the RDMA perftest matrix.

The perftest suite (`perftest-suite.sh`) writes one directory per run, each
containing a `<role>/summary.csv` with the columns:

    test,device,role,rc,bw_peak_MBps,bw_avg_MBps,lat_typical_usec,size_bytes

Two things about that table drive every choice in this script:

  * The **client** summary is the source of truth.  perftest prints the results
    table on the client; the host's `ib_read_*`/`ib_atomic_*` latency and its
    `ib_send_bw` peak are zero by construction (see the suite README).  We
    therefore read client rows and only fall back to host rows for a run that
    has no client side.
  * Bandwidth values are what `--report_gbits` printed, i.e. **Gbit/s**, even
    though the CSV header says `*_MBps`.  Latency is in microseconds.

The script finds the newest `--num-runs` runs that actually contain
measurements, parses their test tags of the form

    ib_<verb>_<kind>[_bidi]_<device>          e.g. ib_write_bw_bidi_rocep1s0f1

and emits a small, purpose-built set of figures:

  1. bw_by_operation_and_device  bandwidth per verb, grouped by device (log y)
  2. bidirectional_vs_unidirectional  uni vs `-b` per verb
  3. cable_penalty_ratio         f1/f0 bandwidth ratio (the cable question)
  4. latency_by_operation_and_device
  5. run_to_run_repeatability    the per-run spread behind every mean
  6. peak_vs_average_bw          measurement stability inside a single run

Usage:
    python plot_perftest.py
    python plot_perftest.py --results-dir <dir> --out <dir> --num-runs 3
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")  # headless, safe over ssh
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter


# ---------------------------------------------------------------------------
# Appearance -- Okabe-Ito qualitative hues, per the repo's other plot scripts.
# ---------------------------------------------------------------------------
VERB_ORDER = ["send", "write", "read", "atomic"]
VERB_LABEL = {
    "send": "SEND",
    "write": "WRITE",
    "read": "READ",
    "atomic": "ATOMIC",
}

# Cable identity is the story here, so hue encodes cable and hatch encodes the
# physical NIC port (each QSFP port carries two logical RDMA devices).
CABLE_STYLE = {
    "f0": dict(label="f0  (short black cable)", color="#0072B2", hatch=""),
    "f1": dict(label="f1  (long blue-tab cable)", color="#D55E00", hatch="//"),
}
PORT_STYLE = {1: "", 2: "xx"}

COL_W = 3.5   # one IEEE column, inches
PANEL_H = 2.7


def device_style(dev: str) -> tuple[str, int]:
    """Return (cable_key, port) for an RDMA device name.

    'rocep1s0f0'  -> ('f0', 1)     'rocep1s0f1'   -> ('f1', 1)
    'roceP2p1s0f0'-> ('f0', 2)     'roceP2p1s0f1' -> ('f1', 2)
    """
    cable = "f1" if dev.endswith("f1") else "f0"
    if dev.startswith("roceP2"):
        port = 2
    elif dev.startswith("rocep1"):
        port = 1
    else:  # unknown scheme: keep it deterministic rather than crashing
        port = 0
    return cable, port


def cable_label(dev: str) -> str:
    cable, port = device_style(dev)
    short = "black" if cable == "f0" else "blue"
    return f"{dev}\n(port {port}, {short})"


# ---------------------------------------------------------------------------
# Discovery + parsing
# ---------------------------------------------------------------------------
TAG_RE = re.compile(r"^ib_(send|write|read|atomic)_(bw|lat)(_bidi)?_(.+)$")
SUMMARY_COLS = [
    "test",
    "device",
    "role",
    "rc",
    "bw_peak_MBps",
    "bw_avg_MBps",
    "lat_typical_usec",
    "size_bytes",
]


def load_summary(path: Path, run: str, role: str) -> pd.DataFrame | None:
    """Parse one summary.csv into a tidy frame, or None if it has no data."""
    try:
        df = pd.read_csv(path, dtype=str)
    except Exception as exc:  # unreadable / half-written file
        print(f"  [skip] {path}: {exc}", file=sys.stderr)
        return None
    if df.empty or not set(SUMMARY_COLS).issubset(df.columns):
        return None

    df = df.reindex(columns=SUMMARY_COLS)
    # Drop rows whose tag doesn't match the expected shape.
    parsed = df["test"].str.extract(TAG_RE)
    parsed.columns = ["verb", "kind", "bidi", "device_tagged"]
    df = df.join(parsed)
    # Trust the tag's device when present; fall back to the CSV column.
    df["device"] = df["device_tagged"].fillna(df["device"])
    df = df.drop(columns=["device_tagged"]).dropna(subset=["verb", "kind", "device"])
    if df.empty:
        return None

    df["bidi"] = df["bidi"].notna()
    df["role"] = role
    df["run"] = run
    for col in ("bw_peak_MBps", "bw_avg_MBps", "lat_typical_usec", "size_bytes"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


# The host and client of one run get separate timestamped directories, seconds
# apart; distinct runs are minutes apart.  Cluster directories within this many
# seconds into a single logical run so host/client aren't counted twice.
RUN_GAP_SECONDS = 90


def _ts(name: str) -> float:
    from datetime import datetime
    return datetime.strptime(name[:15], "%Y%m%d-%H%M%S").timestamp()


def _roles(d: Path) -> set:
    return {p.name for p in d.iterdir() if p.is_dir() and p.name in ("host", "client")}


def discover_runs(results_dir: Path, num_runs: int, gap: int = RUN_GAP_SECONDS):
    """Return the newest `num_runs` logical runs that hold measurements.

    A logical run is a cluster of `<timestamp>/<role>/summary.csv` directories
    whose timestamps are within `gap` seconds (a host dir and its client dir).
    A cluster holds at most one host and one client, so two back-to-back runs
    cannot chain into one.  Client rows are authoritative, so when a cluster
    has both sides the client summary is used; the host is only a fallback for
    clientless clusters.
    """
    run_dirs = sorted(
        (d for d in results_dir.iterdir() if d.is_dir() and d.name[:8].isdigit()),
        key=lambda d: d.name,
    )
    if not run_dirs:
        return []

    clusters = [[run_dirs[0]]]
    seen_roles = _roles(run_dirs[0])
    for d in run_dirs[1:]:
        roles = _roles(d)
        if (_ts(d.name) - _ts(clusters[-1][-1].name) <= gap
                and not (roles & seen_roles)):
            clusters[-1].append(d)
            seen_roles |= roles
        else:
            clusters.append([d])
            seen_roles = roles

    chosen = []
    for cluster in reversed(clusters):  # newest first
        label = "+".join(d.name for d in cluster)
        for role in ("client", "host"):
            for d in cluster:
                summary = d / role / "summary.csv"
                if not summary.is_file():
                    continue
                df = load_summary(summary, label, role)
                if df is not None and not df.empty:
                    chosen.append((label, role, df))
                    break
            else:
                continue
            break
        if len(chosen) >= num_runs:
            break
    return chosen


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------
def agg(df: pd.DataFrame, keys) -> pd.DataFrame:
    """Mean / min / max of the metric across runs, grouped by `keys`."""
    metric = "bw_avg_MBps" if "bw" in _kind_of(df) else "lat_typical_usec"
    g = df.groupby(list(keys), as_index=False)[metric].agg(
        mean="mean", lo="min", hi="max", n="count"
    )
    g["err_lo"] = (g["mean"] - g["lo"]).clip(lower=0)
    g["err_hi"] = (g["hi"] - g["mean"]).clip(lower=0)
    return g


def _kind_of(df: pd.DataFrame) -> str:
    kinds = set(df["kind"].dropna())
    return "bw" if kinds == {"bw"} else ("lat" if kinds == {"lat"} else "mixed")


def bw_only(df):
    return df[(df["kind"] == "bw") & (df["bw_avg_MBps"].fillna(0) > 0)].copy()


def lat_only(df):
    return df[(df["kind"] == "lat") & (df["lat_typical_usec"].fillna(0) > 0)].copy()


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------
def fig_bw_by_op_device(bw, devices, out, fmt):
    """Bandwidth per verb, grouped by device, log scale (atomic is ~1000x down)."""
    g = agg(bw[~bw["bidi"]], ["verb", "device"])
    verbs = [v for v in VERB_ORDER if v in set(g["verb"])]

    fig, ax = plt.subplots(figsize=(COL_W * 2, PANEL_H * 1.15))
    x = np.arange(len(verbs))
    w = 0.8 / len(devices)
    offs = (np.arange(len(devices)) - (len(devices) - 1) / 2) * w
    for i, dev in enumerate(devices):
        sub = g[g["device"] == dev].set_index("verb").reindex(verbs)
        cable, port = device_style(dev)
        ax.bar(
            x + offs[i], sub["mean"], w, label=cable_label(dev),
            color=CABLE_STYLE[cable]["color"], hatch=PORT_STYLE.get(port, ""),
            edgecolor="black", linewidth=0.5,
            yerr=[sub["err_lo"].fillna(0), sub["err_hi"].fillna(0)],
            error_kw=dict(ecolor="0.25", elinewidth=0.8, capsize=2),
        )
    ax.set_yscale("log")
    ax.set_ylim(0.05, 400)
    ax.set_xticks(x)
    ax.set_xticklabels([VERB_LABEL[v] + "\nBW" for v in verbs])
    ax.set_ylabel("Bandwidth (Gbit/s, log)")
    ax.set_title("Unidirectional bandwidth by operation", fontsize=9)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    ax.grid(axis="y", which="both", color="0.85", linewidth=0.5)
    ax.set_axisbelow(True)
    ax.legend(fontsize=6, ncol=2, loc="upper right", frameon=False)
    fig.tight_layout()
    _save(fig, out, "bw_by_operation_and_device", fmt)


def fig_uni_vs_bidi(bw, devices, out, fmt):
    """Half-duplex vs `-b` bidirectional, one panel per verb.

    Atomic is excluded: its ~0.08 Gbit/s would be invisible beside 100+ on the
    shared linear axis (it keeps its own log-scaled figure).
    """
    g = agg(bw, ["verb", "device", "bidi"])
    verbs = [v for v in VERB_ORDER if v in set(g["verb"]) and v != "atomic"]
    fig, axes = plt.subplots(1, len(verbs), figsize=(COL_W * 2.3, PANEL_H), sharey=True)

    for ax, verb in zip(np.atleast_1d(axes), verbs):
        sub = g[g["verb"] == verb]
        x = np.arange(len(devices))
        w = 0.38
        for j, (bidi, style) in enumerate([(False, dict(facecolor="white", hatch="")),
                                            (True, dict(facecolor="0.55", hatch=""))]):
            vals, los, his = [], [], []
            for dev in devices:
                row = sub[(sub["device"] == dev) & (sub["bidi"] == bidi)]
                vals.append(row["mean"].iloc[0] if len(row) else np.nan)
                los.append(row["err_lo"].iloc[0] if len(row) else 0)
                his.append(row["err_hi"].iloc[0] if len(row) else 0)
            ax.bar(x + (j - 0.5) * w, vals, w, edgecolor="black", linewidth=0.6,
                   yerr=[los, his], error_kw=dict(ecolor="0.25", elinewidth=0.7, capsize=2),
                   **style)
        ax.set_xticks(x)
        ax.set_xticklabels([f"p{device_style(d)[1]}\n{device_style(d)[0]}" for d in devices],
                           fontsize=6.5)
        ax.set_title(f"{VERB_LABEL[verb]} BW", fontsize=8)
        ax.grid(axis="y", color="0.85", linewidth=0.5)
        ax.set_axisbelow(True)
    np.atleast_1d(axes)[0].set_ylabel("Bandwidth (Gbit/s)")
    handles = [
        plt.Rectangle((0, 0), 1, 1, facecolor="white", edgecolor="black",
                      label="unidirectional"),
        plt.Rectangle((0, 0), 1, 1, facecolor="0.55", edgecolor="black",
                      label="bidirectional (-b)"),
    ]
    fig.legend(handles=handles, fontsize=6, ncol=2, loc="lower center",
               frameon=False, bbox_to_anchor=(0.5, 0.0))
    fig.text(0.5, -0.04,
             "x tick = NIC port . cable index   (f0 = short black, f1 = long blue-tab)",
             ha="center", fontsize=6, color="0.35")
    fig.suptitle("Bidirectional vs unidirectional bandwidth", fontsize=9)
    fig.tight_layout(rect=(0, 0.06, 1, 0.96))
    _save(fig, out, "bidirectional_vs_unidirectional", fmt)


def fig_cable_penalty(bw, devices, out, fmt):
    """f1/f0 bandwidth ratio per port and verb -- tests the 'about 1/3' claim."""
    g = agg(bw[~bw["bidi"]], ["verb", "device"])
    verbs = [v for v in VERB_ORDER if v in set(g["verb"])]
    ports = sorted({device_style(d)[1] for d in devices})

    fig, ax = plt.subplots(figsize=(COL_W * 1.6, PANEL_H))
    x = np.arange(len(verbs))
    w = 0.8 / len(ports)
    offs = (np.arange(len(ports)) - (len(ports) - 1) / 2) * w
    port_colors = {1: "#0072B2", 2: "#56B4E9"}
    for i, port in enumerate(ports):
        f0 = next((d for d in devices if device_style(d) == ("f0", port)), None)
        f1 = next((d for d in devices if device_style(d) == ("f1", port)), None)
        if not (f0 and f1):
            continue
        ratios = []
        for verb in verbs:
            a = g[(g["device"] == f0) & (g["verb"] == verb)]["mean"]
            b = g[(g["device"] == f1) & (g["verb"] == verb)]["mean"]
            ratios.append((b.iloc[0] / a.iloc[0]) if len(a) and len(b) and a.iloc[0] else np.nan)
        ax.bar(x + offs[i], ratios, w, color=port_colors.get(port, "0.5"),
               edgecolor="black", linewidth=0.5, label=f"NIC port {port}")
    ax.axhline(1.0, color="0.3", linewidth=0.9, linestyle="--")
    ax.axhline(1 / 3, color="#D55E00", linewidth=0.9, linestyle=":")
    ax.text(len(verbs) - 0.5, 1 / 3 + 0.02, "1/3", color="#D55E00",
            fontsize=6, ha="right")
    ax.set_xticks(x)
    ax.set_xticklabels([VERB_LABEL[v] + "\nBW" for v in verbs])
    ax.set_ylabel("blue-tab (f1) / short-black (f0)")
    ax.set_title("Bandwidth penalty of the long blue-tab cable", fontsize=9)
    ax.set_ylim(0, 1.15)
    ax.grid(axis="y", color="0.85", linewidth=0.5)
    ax.set_axisbelow(True)
    ax.legend(fontsize=6, frameon=False)
    fig.tight_layout()
    _save(fig, out, "cable_penalty_ratio", fmt)


def fig_latency(lat, devices, out, fmt):
    """Typical latency per verb, grouped by device."""
    g = agg(lat, ["verb", "device"])
    verbs = [v for v in VERB_ORDER if v in set(g["verb"])]

    fig, ax = plt.subplots(figsize=(COL_W * 2, PANEL_H * 1.05))
    x = np.arange(len(verbs))
    w = 0.8 / len(devices)
    offs = (np.arange(len(devices)) - (len(devices) - 1) / 2) * w
    for i, dev in enumerate(devices):
        sub = g[g["device"] == dev].set_index("verb").reindex(verbs)
        cable, port = device_style(dev)
        ax.bar(x + offs[i], sub["mean"], w, label=cable_label(dev),
               color=CABLE_STYLE[cable]["color"], hatch=PORT_STYLE.get(port, ""),
               edgecolor="black", linewidth=0.5,
               yerr=[sub["err_lo"].fillna(0), sub["err_hi"].fillna(0)],
               error_kw=dict(ecolor="0.25", elinewidth=0.8, capsize=2))
    ax.set_xticks(x)
    ax.set_xticklabels([VERB_LABEL[v] + "\nLAT" for v in verbs])
    ax.set_ylabel("Typical latency (usec)")
    ax.set_title("Latency by operation", fontsize=9)
    ax.grid(axis="y", color="0.85", linewidth=0.5)
    ax.set_axisbelow(True)
    ax.legend(fontsize=6, ncol=2, loc="upper left", frameon=False)
    fig.tight_layout()
    _save(fig, out, "latency_by_operation_and_device", fmt)


def fig_repeatability(bw, devices, out, fmt):
    """Per-run spread behind every mean, one panel per verb."""
    verbs = [v for v in VERB_ORDER if v in set(bw["verb"])]
    fig, axes = plt.subplots(2, 2, figsize=(COL_W * 2, PANEL_H * 1.5), sharex=True)
    for ax, verb in zip(axes.ravel(), verbs):
        sub = bw[(bw["verb"] == verb) & (~bw["bidi"])]
        x = np.arange(len(devices))
        for i, dev in enumerate(devices):
            vals = sub[sub["device"] == dev]["bw_avg_MBps"].to_numpy()
            jit = np.linspace(-0.12, 0.12, len(vals)) if len(vals) > 1 else [0]
            ax.scatter(np.full(len(vals), i) + jit, vals, s=14,
                       color=CABLE_STYLE[device_style(dev)[0]]["color"],
                       edgecolor="black", linewidth=0.4, zorder=3)
            if len(vals):
                ax.plot([i - 0.22, i + 0.22], [vals.mean()] * 2, color="0.2",
                        linewidth=1.1, zorder=2)
        ax.set_title(f"{VERB_LABEL[verb]} BW", fontsize=8)
        ax.grid(axis="y", color="0.9", linewidth=0.5)
        ax.set_axisbelow(True)
    for ax in axes[1]:
        ax.set_xticks(np.arange(len(devices)))
        ax.set_xticklabels([f"{d}\np{device_style(d)[1]}" for d in devices], fontsize=5.5)
    for ax in axes[:, 0]:
        ax.set_ylabel("BW avg (Gbit/s)", fontsize=7)
    fig.suptitle("Run-to-run repeatability (one dot per run)", fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    _save(fig, out, "run_to_run_repeatability", fmt)


def fig_peak_vs_avg(bw, out, fmt):
    """Peak vs average within each run -- how stable the measurement was."""
    fig, ax = plt.subplots(figsize=(COL_W * 1.4, PANEL_H * 1.1))
    colors = dict(zip(VERB_ORDER, ["#0072B2", "#D55E00", "#009E73", "#CC79A7"]))
    lim = [0.05, 400]
    for verb in VERB_ORDER:
        sub = bw[bw["verb"] == verb]
        ax.scatter(sub["bw_avg_MBps"], sub["bw_peak_MBps"], s=16,
                   color=colors[verb], edgecolor="black", linewidth=0.4,
                   label=VERB_LABEL[verb], alpha=0.85)
    ax.plot(lim, lim, color="0.3", linewidth=0.9, linestyle="--")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(*lim)
    ax.set_ylim(*lim)
    ax.set_xlabel("BW average (Gbit/s, log)")
    ax.set_ylabel("BW peak (Gbit/s, log)")
    ax.set_title("Peak vs average bandwidth", fontsize=9)
    ax.grid(which="both", color="0.88", linewidth=0.5)
    ax.set_axisbelow(True)
    ax.legend(fontsize=6, frameon=False, loc="upper left")
    fig.tight_layout()
    _save(fig, out, "peak_vs_average_bw", fmt)


def _save(fig, out: Path, stem: str, fmt):
    out.mkdir(parents=True, exist_ok=True)
    for ext in fmt:
        path = out / f"{stem}.{ext}"
        fig.savefig(path, dpi=200, bbox_inches="tight")
        print(f"  wrote {path}")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def print_report(df, runs):
    print("\n== runs used ==")
    for ts, role, sub in runs:
        print(f"  {ts}  ({role})  {len(sub)} tests")

    bw = bw_only(df)
    g = agg(bw[~bw["bidi"]], ["verb", "device"]).sort_values(["verb", "device"])
    print("\n== unidirectional bandwidth, mean over runs (Gbit/s) ==")
    pivot = g.pivot(index="device", columns="verb", values="mean")
    print(pivot.reindex(columns=VERB_ORDER).round(2).to_string())

    print("\n== cable penalty: f1 (blue) / f0 (black) ==")
    for verb in VERB_ORDER:
        row = g[g["verb"] == verb].set_index("device")
        for f0 in [d for d in row.index if device_style(d)[0] == "f0"]:
            f1 = f0[:-1] + "1"
            if f1 in row.index and row.loc[f0, "mean"]:
                r = row.loc[f1, "mean"] / row.loc[f0, "mean"]
                print(f"  {verb:>6}  {f0} -> {f1}:  {r:0.3f}")

    lat = lat_only(df)
    if len(lat):
        gl = agg(lat, ["verb", "device"])
        print("\n== typical latency, mean over runs (usec) ==")
        print(gl.pivot(index="device", columns="verb", values="mean")
                .reindex(columns=VERB_ORDER).round(3).to_string())


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    here = Path(__file__).resolve().parent
    ap.add_argument("--results-dir", type=Path,
                    default=here / "perftest-results",
                    help="directory of <timestamp>/<role>/summary.csv runs")
    ap.add_argument("--out", type=Path, default=here / "perftest-results" / "figs",
                    help="output directory for figures")
    ap.add_argument("--num-runs", type=int, default=3,
                    help="number of most-recent result-bearing runs to use")
    ap.add_argument("--format", default="png,pdf",
                    help="comma-separated output formats")
    args = ap.parse_args(argv)

    if not args.results_dir.is_dir():
        ap.error(f"results dir not found: {args.results_dir}")

    runs = discover_runs(args.results_dir, args.num_runs)
    if not runs:
        ap.error(f"no runs with results found under {args.results_dir}")
    if len(runs) < args.num_runs:
        print(f"warning: only {len(runs)} result-bearing run(s) found "
              f"(asked for {args.num_runs})", file=sys.stderr)

    df = pd.concat([sub for _, _, sub in runs], ignore_index=True)
    devices = sorted(df["device"].unique(),
                     key=lambda d: (device_style(d)[1], device_style(d)[0]))

    fmt = [f.strip() for f in args.format.split(",") if f.strip()]
    print(f"using {len(runs)} run(s), {df['device'].nunique()} device(s), "
          f"{len(df)} test rows")

    bw, lat = bw_only(df), lat_only(df)
    fig_bw_by_op_device(bw, devices, args.out, fmt)
    fig_uni_vs_bidi(bw, devices, args.out, fmt)
    fig_cable_penalty(bw, devices, args.out, fmt)
    if len(lat):
        fig_latency(lat, devices, args.out, fmt)
    fig_repeatability(bw, devices, args.out, fmt)
    fig_peak_vs_avg(bw, args.out, fmt)

    print_report(df, runs)


if __name__ == "__main__":
    main()
