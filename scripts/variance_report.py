#!/usr/bin/env python3
"""Replicate statistics for a multi-repeat campaign index.

In steady-exact-v4 every point of the campaign is measured in independent
replicates (separate server launches; scripts/run_job.sh), so this report runs
over the whole index rather than over hand-picked points. It reports:

1. Per (model, config, concurrency): mean, sample standard deviation,
   coefficient of variation, and a Student-t 95% CI half-width across
   replicates, for throughput, goodput, ITL, TTFT, and energy per token. The
   worst-case CV over the whole campaign is the noise floor the paper quotes.

2. Per (model, concurrency, metric): a Welch 95% interval on EVERY pairwise
   config margin, not just winner vs runner-up. A ranking claim ("PP=2 has the
   highest goodput") needs the top config separated from *every* alternative;
   the per-point verdict reports exactly that. These are per-comparison
   intervals, not family-wise corrected.

3. Optionally, against --baseline (a single-repeat campaign such as
   baselines/pmbs26-tcp): whether each baseline value falls inside the range
   these replicates produced.

    python3 scripts/variance_report.py --index results/benchmark-2026-10-22
    python3 scripts/variance_report.py --index results/benchmark-2026-10-22 \
        --md figs/stats/replicate_stats.md --tex figs/stats/replicate_stats.tex
"""
from __future__ import annotations
import argparse
import itertools
import math
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.campaign_output import resolve_campaign_output_path, resolve_index_path

# (index column, human label, unit, scale-to-unit, TeX name fragment)
METRICS = [
    ("tok_thru_tps", "throughput", "tok/s", 1.0, "Throughput"),
    ("goodput_rps", "strict goodput", "req/s", 1.0, "Goodput"),
    ("goodput_itl2_rps", "loose goodput", "req/s", 1.0, "GoodputLoose"),
    # The request-mean median is the statistic the goodput SLO scores and the
    # paper's ITL figures plot; the pooled token-gap median is kept alongside
    # it because the two can disagree in sign (steady gaps vs stalls) and the
    # paper reports that disagreement where the repeats expose it.
    ("mean_itl_p50", "median ITL", "ms", 1e3, "ITL"),
    ("itl_p50", "pooled ITL", "ms", 1e3, "PoolITL"),
    ("ttft_p50", "median TTFT", "ms", 1e3, "TTFT"),
    ("energy_per_token_j", "energy/token", "J/tok", 1.0, "Energy"),
]
# Two-sided 95% Student-t critical values by degrees of freedom (n-1), so no
# scipy dependency for the small repeat counts this subset produces.
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447,
       7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179}
# Metrics where a larger value is the better outcome; the rest are latencies
# or costs.
HIGHER_IS_BETTER = {"tok_thru_tps", "goodput_rps", "goodput_itl2_rps"}
_DIGITS = {"0": "Zero", "1": "One", "2": "Two", "3": "Three", "4": "Four",
           "5": "Five", "6": "Six", "7": "Seven", "8": "Eight", "9": "Nine"}


def _texname(*parts: object) -> str:
    """Deterministic letters-only macro fragment (TeX names cannot hold digits).

    Digits become their English names and separators are dropped, so
    ("llama3.1-8b", "pp2", "C", 32) -> LlamaThreeOneEightbPpTwoCThreeTwo.
    """
    out = []
    for part in parts:
        for token in re.split(r"[^0-9A-Za-z]+", str(part)):
            if not token:
                continue
            piece = "".join(_DIGITS.get(ch, ch) for ch in token)
            out.append(piece[:1].upper() + piece[1:])
    return "".join(out)


def _t95(df: float) -> float:
    """Critical value at (conservatively floored) degrees of freedom."""
    if not math.isfinite(df) or df < 1:
        return math.nan
    return T95.get(int(math.floor(df)), 1.96)


def _ci_halfwidth(std: float, n: int) -> float:
    if n < 2 or not math.isfinite(std):
        return math.nan
    return T95.get(n - 1, 1.96) * std / math.sqrt(n)


def _welch(a, b) -> tuple[float, float]:
    """Mean difference (a-b) and 95% CI half-width via Welch's t-interval.

    Welch-Satterthwaite df is floored before the t lookup, which only widens
    the interval -- the conservative direction for a "can we separate these
    two configurations at all?" claim.
    """
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return float(a.mean() - b.mean()), math.nan
    va, vb = float(a.var(ddof=1)), float(b.var(ddof=1))
    se2 = va / na + vb / nb
    if se2 <= 0:
        return float(a.mean() - b.mean()), 0.0
    df_num = se2 ** 2
    df_den = (va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1)
    df = df_num / df_den if df_den > 0 else 1.0
    return float(a.mean() - b.mean()), _t95(df) * math.sqrt(se2)


def _arms(g: pd.DataFrame, col: str, scale: float) -> dict:
    """Per-configuration repeat samples for one metric, needing n>=2 each."""
    arms = {}
    for cfg, gc in g.groupby("config_label"):
        vals = gc[col].dropna().astype(float) * scale
        if len(vals) >= 2:
            arms[cfg] = vals
    return arms


def _separation(df: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    """Every pairwise margin, plus a per-point verdict on the ranking claim.

    The paper's claim is an ordering ("PP=2 holds the highest strict-tier
    goodput"), so the point is defensible only if the top arm is separated from
    every other arm -- reporting winner vs runner-up alone would hide a second
    alternative sitting just as close.
    """
    pairs, verdicts = [], []
    for (model, conc), g in df.groupby(["model_label", "concurrency"]):
        for col, label, unit, scale, _ in METRICS:
            if col not in g.columns:
                continue
            arms = _arms(g, col, scale)
            if len(arms) < 2:
                continue
            # An all-zero metric (strict goodput above the ITL floor) carries no
            # information; reporting "not separated" there would be misleading.
            if all(float(v.abs().max()) == 0.0 for v in arms.values()):
                continue
            hib = col in HIGHER_IS_BETTER
            order = sorted(arms, key=lambda c: float(arms[c].mean()),
                           reverse=hib)
            win = order[0]
            all_sep = True
            for a, b in itertools.combinations(order, 2):
                diff, ci = _welch(arms[a], arms[b])
                sep = math.isfinite(ci) and abs(diff) > ci
                if a == win and not sep:
                    all_sep = False
                pairs.append({
                    "model": model, "c": int(conc), "metric": label,
                    "pair": f"{a} vs {b}",
                    "margin": f"{abs(diff):.3g} {unit}",
                    "95% CI": f"+/-{ci:.3g}" if math.isfinite(ci) else "n/a",
                    "separated": "yes" if sep else "NO",
                })
            verdicts.append({
                "model": model, "c": int(conc), "metric": label,
                "winner": win, "arms": len(arms),
                "beats all others": "yes" if all_sep else "NO",
            })
    return pairs, verdicts


def _baseline_check(df: pd.DataFrame, baseline_csv: str) -> list[dict]:
    """Does a single-repeat baseline value land inside the replicate spread?"""
    try:
        base = pd.read_csv(baseline_csv)
    except FileNotFoundError:
        print(f"[warn] no baseline index at {baseline_csv}; skipping", file=sys.stderr)
        return []
    keys = ["model_label", "config_label", "concurrency"]
    if any(k not in base.columns for k in keys):
        print(f"[warn] baseline {baseline_csv} lacks key columns; skipping",
              file=sys.stderr)
        return []
    rows = []
    for (model, cfg, conc), g in df.groupby(keys):
        b = base[(base.model_label == model) & (base.config_label == cfg)
                 & (base.concurrency == conc)]
        if b.empty:
            continue
        for col, label, unit, scale, _ in METRICS:
            if col not in g.columns or col not in b.columns:
                continue
            vals = g[col].dropna().astype(float) * scale
            bvals = b[col].dropna().astype(float) * scale
            if len(vals) < 2 or bvals.empty:
                continue
            bv = float(bvals.iloc[0])
            lo, hi = float(vals.min()), float(vals.max())
            mean, std = float(vals.mean()), float(vals.std(ddof=1))
            if mean == 0 and bv == 0:
                continue
            z = (bv - mean) / std if std > 0 else math.nan
            rows.append({
                "model": model, "config": cfg, "c": int(conc), "metric": label,
                "campaign": f"{bv:.4g} {unit}",
                "repeat range": f"{lo:.4g}..{hi:.4g}",
                "z": f"{z:+.2f}" if math.isfinite(z) else "n/a",
                "inside": "yes" if lo <= bv <= hi else "NO",
                # Raw values for the TeX macros; underscore keys stay out of
                # the printed/markdown tables.
                "_metric_col": col,
                "_dev_pct": (bv - mean) / mean * 100.0 if mean else math.nan,
            })
    return rows


def _print_table(rows: list[dict], hdr: list[str]) -> None:
    if not rows:
        print("(no rows)")
        return
    widths = {h: max([len(h)] + [len(str(r.get(h, ""))) for r in rows])
              for h in hdr}
    print("  ".join(h.ljust(widths[h]) for h in hdr))
    print("  ".join("-" * widths[h] for h in hdr))
    for r in rows:
        print("  ".join(str(r.get(h, "")).ljust(widths[h]) for h in hdr))


def _write_md(path: str, tables: list[tuple[str, list[dict], list[str]]]) -> None:
    with open(path, "w") as f:
        f.write("# Replicate statistics\n")
        for title, rows, hdr in tables:
            f.write(f"\n## {title}\n\n")
            if not rows:
                f.write("(no rows)\n")
                continue
            f.write("| " + " | ".join(hdr) + " |\n")
            f.write("|" + "|".join("---" for _ in hdr) + "|\n")
            for r in rows:
                f.write("| " + " | ".join(str(r.get(h, "")) for h in hdr) + " |\n")


def _cv_list(stats: list[dict], label: str) -> list[float]:
    vals = []
    for r in stats:
        v = r.get(f"_{label}")
        if v and math.isfinite(v[1]):
            vals.append(v[1])
    return sorted(vals, reverse=True)


def _write_tex(path: str, df: pd.DataFrame, stats: list[dict],
               pairs: list[dict], worst_cv: dict, n_repeats: int,
               n_arms: int, base_rows: list[dict]) -> None:
    """Macros for the paper. Names are letters-only and deterministic; the file
    lists each one in a comment so the prose can find what it needs."""
    lines = ["% Generated by scripts/variance_report.py -- do not edit.",
             "% Run-to-run spread across independent replicates.",
             ""]
    lines.append(f"\\newcommand{{\\dgxVarRepeats}}{{{n_repeats}}}")
    lines.append(f"\\newcommand{{\\dgxVarArms}}{{{n_arms}}}")
    lines.append(f"\\newcommand{{\\dgxVarRuns}}{{{len(df)}}}")
    hours = float(df["measure_s"].sum()) / 3600.0
    lines.append(f"\\newcommand{{\\dgxVarMeasureHours}}{{{hours:.1f}}}")
    for _, label, _, _, tex in METRICS:
        w = worst_cv.get(label, 0.0)
        if math.isfinite(w) and w > 0:
            lines.append(
                f"\\newcommand{{\\dgxVarWorstCV{tex}}}{{{w:.1f}}}")
        ranked = _cv_list(stats, label)
        if len(ranked) > 1:
            lines.append(
                f"\\newcommand{{\\dgxVarSecondCV{tex}}}{{{ranked[1]:.1f}}}")
    # A single defensible threshold the prose can cite: twice the worst CV over
    # the throughput-like metrics, rounded up to a whole percent.
    floor_cv = max((worst_cv.get(l, 0.0) for _, l, _, _, _ in METRICS
                    if l in ("throughput", "strict goodput")), default=0.0)
    if math.isfinite(floor_cv) and floor_cv > 0:
        lines.append("% Differences smaller than this are not resolvable here.")
        lines.append(
            f"\\newcommand{{\\dgxVarResolvablePct}}{{{math.ceil(2 * floor_cv)}}}")
    n_sep = sum(1 for p in pairs if p["separated"] == "yes")
    lines.append(f"\\newcommand{{\\dgxVarPairsTotal}}{{{len(pairs)}}}")
    lines.append(f"\\newcommand{{\\dgxVarPairsSeparated}}{{{n_sep}}}")
    lines.append("")
    lines.append("% Per-arm spread: mean, CV percent, 95% CI half-width, "
                 "min, max.")
    for r in stats:
        base = _texname(r["model"], r["config"], "C", r["c"])
        for _, label, _, _, tex in METRICS:
            v = r.get(f"_{label}")
            if not v:
                continue
            mean, cv, ci, lo, hi = v
            if not math.isfinite(mean):
                continue
            lines.append(f"\\newcommand{{\\dgxVar{base}{tex}Mean}}{{{mean:.4g}}}")
            if math.isfinite(cv):
                lines.append(f"\\newcommand{{\\dgxVar{base}{tex}CV}}{{{cv:.1f}}}")
            if math.isfinite(ci):
                lines.append(f"\\newcommand{{\\dgxVar{base}{tex}CI}}{{{ci:.3g}}}")
            lines.append(f"\\newcommand{{\\dgxVar{base}{tex}Min}}{{{lo:.4g}}}")
            lines.append(f"\\newcommand{{\\dgxVar{base}{tex}Max}}{{{hi:.4g}}}")
    lines.append("")
    lines.append("% Welch pairwise margins: |mean difference| and 95% CI "
                 "half-width per (model, c, metric, pair).")
    for p in pairs:
        a, b = p["pair"].split(" vs ")
        tex = next(t for _, l, _, _, t in METRICS if l == p["metric"])
        base = _texname(p["model"], "C", p["c"], tex, a, "Vs", b)
        margin = p["margin"].split()[0]
        lines.append(f"\\newcommand{{\\dgxVarM{base}Margin}}{{{margin}}}")
        if p["95% CI"] != "n/a":
            ci = p["95% CI"].lstrip("+/-")
            lines.append(f"\\newcommand{{\\dgxVarM{base}CI}}{{{ci}}}")
    lines.append("")
    lines.append("% Repeat-based throughput ratios (ratio of repeat means) "
                 "per (model, c, configuration pair).")
    dual_energy: list[float] = []
    for (model, conc), g in df.groupby(["model_label", "concurrency"]):
        for col, tag in (("tok_thru_tps", "Thru"), ("energy_per_token_j",
                                                    "Energy")):
            arms = _arms(g, col, 1.0)
            for a, b in itertools.permutations(sorted(arms), 2):
                base = _texname(model, "C", int(conc), a, "Vs", b)
                ratio = float(arms[a].mean()) / float(arms[b].mean())
                lines.append(
                    f"\\newcommand{{\\dgxVar{tag}Ratio{base}}}{{{ratio:.3f}}}")
                if (col == "energy_per_token_j" and b == "single"
                        and a in ("tp2", "pp2")):
                    dual_energy.append(ratio)
    if dual_energy:
        lines.append("\\newcommand{\\dgxVarEnergyDualRatioMin}"
                     f"{{{min(dual_energy):.2f}}}")
        lines.append("\\newcommand{\\dgxVarEnergyDualRatioMax}"
                     f"{{{max(dual_energy):.2f}}}")
        lines.append("\\newcommand{\\dgxVarEnergyDualRatioPairs}"
                     f"{{{len(dual_energy)}}}")
    if base_rows:
        lines.append("")
        lines.append("% Sealed-campaign deviation from the repeat mean, "
                     "percent, per arm (throughput and energy/token).")
        col_tag = {"tok_thru_tps": "Thru", "energy_per_token_j": "Energy"}
        devs: dict[str, list[float]] = {"Thru": [], "Energy": []}
        for r in base_rows:
            tag = col_tag.get(r.get("_metric_col", ""))
            d = r.get("_dev_pct", math.nan)
            if tag is None or not math.isfinite(d):
                continue
            devs[tag].append(d)
            base = _texname(r["model"], r["config"], "C", r["c"])
            lines.append(
                f"\\newcommand{{\\dgxVar{base}Base{tag}DevPct}}{{{d:+.1f}}}")
        ranked = sorted((abs(d) for d in devs["Thru"]), reverse=True)
        for name, val in (("RankOne", ranked[0] if ranked else None),
                          ("RankTwo", ranked[1] if len(ranked) > 1 else None),
                          ("RankThree", ranked[2] if len(ranked) > 2 else None),
                          ("RestMax", max(ranked[3:]) if len(ranked) > 3
                           else None)):
            if val is not None:
                lines.append("\\newcommand{\\dgxVarBaseThruDevAbs"
                             f"{name}}}{{{val:.1f}}}")
        if devs["Energy"]:
            lines.append("\\newcommand{\\dgxVarBaseEnergyDevMinPct}"
                         f"{{{min(devs['Energy']):.1f}}}")
            lines.append("\\newcommand{\\dgxVarBaseEnergyDevMaxPct}"
                         f"{{{max(devs['Energy']):.1f}}}")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


# Compact display names for the arms table; extend when models join the matrix
# (fails loudly rather than printing a raw label).
SHORT_NAME = {
    "llama3.1-8b": "Llama-3.1-8B",
    "llama3.1-8b-fp8": "Llama-3.1-8B (FP8)",
    "llama3.1-8b-nvfp4": "Llama-3.1-8B (NVFP4)",
    "qwen3-8b": "Qwen3-8B",
    "qwen3-30b-a3b": "Qwen3-30B-A3B",
    "qwen3.6-27b": "Qwen3.6-27B",
    "qwen3.6-27b-fp8": "Qwen3.6-27B (FP8)",
    "qwen3.6-27b-nvfp4": "Qwen3.6-27B (NVFP4)",
    "qwen3.6-35b-a3b": "Qwen3.6-35B-A3B",
    "qwen3.6-35b-a3b-fp8": "Qwen3.6-35B-A3B (FP8)",
    "qwen3.6-35b-a3b-nvfp4": "Qwen3.6-35B-A3B (NVFP4)",
    "gemma4-31b-it-nvfp4": "Gemma-4-31B (NVFP4)",
    "gemma4-26b-a4b-nvfp4": "Gemma-4-26B-A4B (NVFP4)",
    "nemotron3-nano-30b-a3b-nvfp4": "Nemotron-Nano-30B (NVFP4)",
    "nemotron3.5-lightning-30b-a3b-nvfp4": "Nemotron-3.5-Lightning (NVFP4)",
    "gpt-oss-120b": "gpt-oss-120b (MXFP4)",
    "nemotron3-super-120b-a12b-fp8": "Nemotron-Super-120B (FP8)",
    "nemotron3-super-120b-a12b-nvfp4": "Nemotron-Super-120B (NVFP4)",
    "qwen3-235b-a22b-nvfp4": "Qwen3-235B-A22B (NVFP4)",
    "mistral-medium-3.5-128b-nvfp4": "Mistral-Medium-3.5 (NVFP4)",
}
CONFIG_NAME = {"single": "single", "tp2": "TP=2", "pp2": "PP=2"}


def _write_paper_table(path: str, stats: list[dict]) -> None:
    """booktabs tabular for the paper: per arm, mean (CV%) for each metric.

    Emits only the tabular body; the paper wraps it in its own table float so
    caption and label stay under manuscript control.
    """
    cols = [(l, t) for _, l, _, _, t in METRICS
            if l in ("throughput", "strict goodput", "median ITL",
                     "median TTFT", "energy/token")]
    lines = [
        "% Generated by scripts/variance_report.py -- do not edit.",
        "\\begin{tabular}{@{}llr" + "r" * len(cols) + "@{}}",
        "\\toprule",
        " & & & tok/s & req/s & ITL ms & TTFT ms & J/tok \\\\",
        "Model & Config & $c$ & " + " & ".join(
            "\\multicolumn{1}{c}{(CV\\%)}" for _ in cols) + " \\\\",
        "\\midrule",
    ]
    for r in stats:
        model = SHORT_NAME.get(r["model"])
        if model is None:
            raise KeyError(f"no short name for model label {r['model']!r}")
        cells = []
        for label, _ in cols:
            v = r.get(f"_{label}")
            if not v or not math.isfinite(v[0]) or (v[0] == 0):
                cells.append("--")
                continue
            mean, cv = v[0], v[1]
            mtxt = (f"{mean:,.0f}".replace(",", "{,}") if mean >= 1000
                    else f"{mean:.3g}")
            cvtxt = f" ({cv:.1f})" if math.isfinite(cv) else ""
            cells.append(f"{mtxt}{cvtxt}")
        lines.append(
            f"{model} & {CONFIG_NAME[r['config']]} & {r['c']} & "
            + " & ".join(cells) + " \\\\")
    lines += ["\\bottomrule", "\\end{tabular}"]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--index", help="campaign directory or index.csv path")
    ap.add_argument("--md", help="also write a markdown report to this path")
    ap.add_argument("--tex", help="also write LaTeX macros to this path")
    ap.add_argument("--paper-table",
                    help="also write a booktabs tabular (per-arm mean and CV) "
                         "to this path")
    ap.add_argument("--baseline", help="single-repeat baseline index to cross-check")
    args = ap.parse_args()
    index_path = resolve_index_path(args.index)
    args.index = str(index_path)
    for field in ("md", "tex", "paper_table"):
        value = getattr(args, field)
        if value:
            setattr(args, field, str(resolve_campaign_output_path(value, index_path)))
    if args.baseline:
        args.baseline = str(resolve_index_path(args.baseline))

    try:
        df = pd.read_csv(args.index)
    except FileNotFoundError:
        sys.exit(f"no index at {args.index}; run scripts/run_job.sh first")

    keys = ["model_label", "config_label", "concurrency"]
    missing = [k for k in keys if k not in df.columns]
    if missing:
        sys.exit(f"index is missing key column(s): {', '.join(missing)}")

    rows = []
    worst_cv = {label: 0.0 for _, label, _, _, _ in METRICS}
    for (model, cfg, conc), g in df.groupby(keys):
        n = len(g)
        rec = {"model": model, "config": cfg, "c": int(conc), "repeats": n}
        for col, label, unit, scale, _ in METRICS:
            if col not in g.columns or g[col].isna().all():
                rec[label] = "-"
                continue
            vals = g[col].dropna().astype(float) * scale
            mean = float(vals.mean())
            std = float(vals.std(ddof=1)) if len(vals) > 1 else float("nan")
            cv = (std / mean * 100.0) if mean and math.isfinite(std) else float("nan")
            ci = _ci_halfwidth(std, len(vals))
            if math.isfinite(cv):
                worst_cv[label] = max(worst_cv[label], cv)
            ci_txt = f"+/-{ci:.3g}" if math.isfinite(ci) else "n/a"
            cv_txt = f"{cv:.1f}%" if math.isfinite(cv) else "n/a"
            rec[label] = f"{mean:.3g} {unit} (CV {cv_txt}, 95%CI {ci_txt})"
            rec[f"_{label}"] = (mean, cv, ci, float(vals.min()),
                                float(vals.max()))
        rows.append(rec)

    rows.sort(key=lambda r: (str(r["model"]), str(r["config"]), r["c"]))
    hdr = ["model", "config", "c", "repeats"] + [m[1] for m in METRICS]
    print("=== per-arm spread across repeats ===")
    _print_table(rows, hdr)
    print()
    under = [r for r in rows if r["repeats"] < 2]
    if under:
        print(f"[warn] {len(under)} arm(s) have <2 repeats and carry no spread; "
              "rerun the job to fill them", file=sys.stderr)
    for _, label, _, _, _ in METRICS:
        w = worst_cv[label]
        print(f"worst-case {label} CV across all points: "
              f"{w:.1f}%" if w else f"worst-case {label} CV: n/a")

    pairs, verdicts = _separation(df)
    if pairs:
        pairs.sort(key=lambda r: (r["metric"], str(r["model"]), r["c"], r["pair"]))
        print("\n=== every pairwise margin (Welch 95% CI) ===")
        _print_table(pairs, ["model", "c", "metric", "pair", "margin",
                             "95% CI", "separated"])
        n_sep = sum(1 for p in pairs if p["separated"] == "yes")
        print(f"\n{n_sep}/{len(pairs)} pairwise margins exclude zero at 95%")
    if verdicts:
        verdicts.sort(key=lambda r: (r["metric"], str(r["model"]), r["c"]))
        print("\n=== ranking verdict: is the top arm separated from ALL others? ===")
        _print_table(verdicts, ["model", "c", "metric", "winner", "arms",
                                "beats all others"])

    base_rows = []
    if args.baseline:
        base_rows = _baseline_check(df, args.baseline)
        if base_rows:
            base_rows.sort(key=lambda r: (str(r["model"]), str(r["config"]),
                                          r["c"], r["metric"]))
            print("\n=== baseline value vs this campaign's replicate range ===")
            _print_table(base_rows, ["model", "config", "c", "metric",
                                     "campaign", "repeat range", "z", "inside"])
            out = sum(1 for r in base_rows if r["inside"] == "NO")
            print(f"\n{len(base_rows) - out}/{len(base_rows)} campaign values "
                  "fall inside the repeat range")

    if args.md:
        _write_md(args.md, [
            ("Per-arm spread across repeats", rows, hdr),
            ("Every pairwise margin (Welch 95% CI)", pairs,
             ["model", "c", "metric", "pair", "margin", "95% CI", "separated"]),
            ("Ranking verdict: top arm separated from all others", verdicts,
             ["model", "c", "metric", "winner", "arms", "beats all others"]),
            ("Sealed campaign value vs repeat range", base_rows,
             ["model", "config", "c", "metric", "campaign", "repeat range",
              "z", "inside"]),
        ])
        print(f"\nwrote {args.md}")
    if args.tex:
        n_repeats = int(max((r["repeats"] for r in rows), default=0))
        _write_tex(args.tex, df, rows, pairs, worst_cv, n_repeats, len(rows),
                   base_rows)
        print(f"wrote {args.tex}")
    if args.paper_table:
        _write_paper_table(args.paper_table, rows)
        print(f"wrote {args.paper_table}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
