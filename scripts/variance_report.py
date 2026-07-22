#!/usr/bin/env python3
"""Summarize repeat-to-repeat spread for the variance subset (paper limitation #1).

Reads a multi-repeat index (scripts/variance_subset.sh writes one to
results-variance/index.csv) and reports, per (model, config, concurrency), the
mean, sample standard deviation, coefficient of variation, and a Student-t 95%
confidence-interval half-width across the repeats -- for output-token
throughput, median ITL, and median TTFT. This is the concrete evidence for the
paper's claim that the long steady-state window makes a single repeat
trustworthy: quote the worst-case CV so a reviewer sees the bound.

    python3 scripts/variance_report.py --index results-variance/index.csv
    python3 scripts/variance_report.py --index results-variance/index.csv --md variance.md
"""
from __future__ import annotations
import argparse
import math
import sys

import pandas as pd

# (index column, human label, unit, scale-to-unit)
METRICS = [
    ("tok_thru_tps", "throughput", "tok/s", 1.0),
    ("itl_p50", "median ITL", "ms", 1e3),
    ("ttft_p50", "median TTFT", "ms", 1e3),
]
# Two-sided 95% Student-t critical values by degrees of freedom (n-1), so no
# scipy dependency for the small repeat counts this subset produces.
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447,
       7: 2.365, 8: 2.306, 9: 2.262}


def _ci_halfwidth(std: float, n: int) -> float:
    if n < 2 or not math.isfinite(std):
        return math.nan
    return T95.get(n - 1, 1.96) * std / math.sqrt(n)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--index", default="results-variance/index.csv")
    ap.add_argument("--md", help="also write a markdown table to this path")
    args = ap.parse_args()

    try:
        df = pd.read_csv(args.index)
    except FileNotFoundError:
        sys.exit(f"no index at {args.index}; run scripts/variance_subset.sh first")

    keys = ["model_label", "config_label", "concurrency"]
    missing = [k for k in keys if k not in df.columns]
    if missing:
        sys.exit(f"index is missing key column(s): {', '.join(missing)}")

    rows = []
    worst_cv = {label: 0.0 for _, label, _, _ in METRICS}
    for (model, cfg, conc), g in df.groupby(keys):
        n = len(g)
        rec = {"model": model, "config": cfg, "c": int(conc), "repeats": n}
        for col, label, unit, scale in METRICS:
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
        rows.append(rec)

    rows.sort(key=lambda r: (str(r["model"]), str(r["config"]), r["c"]))
    hdr = ["model", "config", "c", "repeats"] + [m[1] for m in METRICS]
    widths = {h: max(len(h), *(len(str(r.get(h, ""))) for r in rows))
              for h in hdr}
    line = "  ".join(h.ljust(widths[h]) for h in hdr)
    print(line)
    print("  ".join("-" * widths[h] for h in hdr))
    for r in rows:
        print("  ".join(str(r.get(h, "")).ljust(widths[h]) for h in hdr))
    print()
    for _, label, _, _ in METRICS:
        w = worst_cv[label]
        print(f"worst-case {label} CV across all points: "
              f"{w:.1f}%" if w else f"worst-case {label} CV: n/a")

    if args.md:
        with open(args.md, "w") as f:
            f.write("| " + " | ".join(hdr) + " |\n")
            f.write("|" + "|".join("---" for _ in hdr) + "|\n")
            for r in rows:
                f.write("| " + " | ".join(str(r.get(h, "")) for h in hdr)
                        + " |\n")
        print(f"\nwrote {args.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
