#!/usr/bin/env python3
"""Summarize repeat-to-repeat spread for the variance subset (paper limitation #1).

Reads a multi-repeat index (scripts/run_variance_job.sh writes one to
results-variance/index.csv) and reports three things the paper needs:

1. Per (model, config, concurrency): mean, sample standard deviation,
   coefficient of variation, and a Student-t 95% CI half-width across repeats,
   for throughput, strict goodput, median ITL and median TTFT. The worst-case CV
   is the noise floor the paper quotes.

2. Per (model, concurrency, metric): a Welch 95% interval on EVERY pairwise
   margin, not just winner vs runner-up. At the contested points all three
   deployment configurations sit inside 0.011 req/s, so "is PP=2 separable from
   the runner-up" is the wrong question -- the ranking claim needs PP=2 to be
   separable from *both* alternatives, and which one is second is itself
   undecided. The per-point verdict reports exactly that.

3. Against --baseline (the sealed single-repeat campaign): whether the value the
   paper already cites falls inside the range these repeats produced. That is
   what licenses quoting this spread as a bound on results/ rather than as a
   measurement of a different thing.

    python3 scripts/variance_report.py --index results-variance/index.csv
    python3 scripts/variance_report.py --index results-variance/index.csv \
        --md variance.md --tex variance_numbers.tex --baseline results/index.csv
"""
from __future__ import annotations
import argparse
import itertools
import math
import re
import sys

import pandas as pd

# (index column, human label, unit, scale-to-unit, TeX name fragment)
METRICS = [
    ("tok_thru_tps", "throughput", "tok/s", 1.0, "Throughput"),
    ("goodput_rps", "strict goodput", "req/s", 1.0, "Goodput"),
    ("itl_p50", "median ITL", "ms", 1e3, "ITL"),
    ("ttft_p50", "median TTFT", "ms", 1e3, "TTFT"),
]
# Two-sided 95% Student-t critical values by degrees of freedom (n-1), so no
# scipy dependency for the small repeat counts this subset produces.
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447,
       7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179}
# Metrics where a larger value is the better outcome; the rest are latencies.
HIGHER_IS_BETTER = {"tok_thru_tps", "goodput_rps"}
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
    """Does the sealed campaign's single run land inside the repeat spread?"""
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
        f.write("# Run-to-run variance subset\n")
        for title, rows, hdr in tables:
            f.write(f"\n## {title}\n\n")
            if not rows:
                f.write("(no rows)\n")
                continue
            f.write("| " + " | ".join(hdr) + " |\n")
            f.write("|" + "|".join("---" for _ in hdr) + "|\n")
            for r in rows:
                f.write("| " + " | ".join(str(r.get(h, "")) for h in hdr) + " |\n")


def _write_tex(path: str, stats: list[dict], pairs: list[dict],
               worst_cv: dict, n_repeats: int, n_arms: int) -> None:
    """Macros for the paper. Names are letters-only and deterministic; the file
    lists each one in a comment so the prose can find what it needs."""
    lines = ["% Generated by scripts/variance_report.py -- do not edit.",
             "% Run-to-run spread over the repeated contested points.",
             ""]
    lines.append(f"\\newcommand{{\\dgxVarRepeats}}{{{n_repeats}}}")
    lines.append(f"\\newcommand{{\\dgxVarArms}}{{{n_arms}}}")
    for _, label, _, _, tex in METRICS:
        w = worst_cv.get(label, 0.0)
        if math.isfinite(w) and w > 0:
            lines.append(
                f"\\newcommand{{\\dgxVarWorstCV{tex}}}{{{w:.1f}}}")
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
    lines.append("% Per-arm spread: mean, CV percent, 95% CI half-width.")
    for r in stats:
        base = _texname(r["model"], r["config"], "C", r["c"])
        for _, label, _, _, tex in METRICS:
            v = r.get(f"_{label}")
            if not v:
                continue
            mean, cv, ci = v
            if not math.isfinite(mean):
                continue
            lines.append(f"\\newcommand{{\\dgxVar{base}{tex}Mean}}{{{mean:.4g}}}")
            if math.isfinite(cv):
                lines.append(f"\\newcommand{{\\dgxVar{base}{tex}CV}}{{{cv:.1f}}}")
            if math.isfinite(ci):
                lines.append(f"\\newcommand{{\\dgxVar{base}{tex}CI}}{{{ci:.3g}}}")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--index", default="results-variance/index.csv")
    ap.add_argument("--md", help="also write a markdown report to this path")
    ap.add_argument("--tex", help="also write LaTeX macros to this path")
    ap.add_argument("--baseline", help="sealed campaign index to cross-check")
    args = ap.parse_args()

    try:
        df = pd.read_csv(args.index)
    except FileNotFoundError:
        sys.exit(f"no index at {args.index}; run scripts/run_variance_job.sh first")

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
            rec[f"_{label}"] = (mean, cv, ci)
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
            print("\n=== sealed campaign value vs this subset's repeat range ===")
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
        _write_tex(args.tex, rows, pairs, worst_cv, n_repeats, len(rows))
        print(f"wrote {args.tex}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
