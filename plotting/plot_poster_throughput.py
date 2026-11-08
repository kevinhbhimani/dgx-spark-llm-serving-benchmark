#!/usr/bin/env python3
"""Generate the Qwen3-Next poster output-throughput figure from index.csv."""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from plot_results import (
    POSTER_CONCURRENCIES,
    POSTER_CONFIG_LABELS,
    POSTER_DEFAULT_ASPECT_RATIO,
    POSTER_MODEL_LABEL,
    _model_values,
    _slug,
    _throughput_col,
    collapse_repeats,
    fig_poster_metric,
    group,
    load_index,
    parse_poster_aspect_ratio,
    set_rc,
)
from utils.campaign_output import (
    resolve_campaign_output_path,
    resolve_index_path,
)


ROOT: Path = Path(__file__).resolve().parents[1]
DEFAULT_INDEX: Path = ROOT / "baselines/rdma-pilot-2026-10/index.csv"
FIGURE_NAME: str = "output_throughput_by_concurrency"
THROUGHPUT_LABEL: str = "Throughput\nTokens/s"


def load_poster_throughput_rows(
    index_path: Path, model_label: str
) -> pd.DataFrame:
    """Load and collapse one model's plotted output-throughput measurements."""
    index: pd.DataFrame = load_index(str(index_path))
    model_values: pd.Series | None = _model_values(index)
    if model_values is None:
        raise ValueError("index.csv has no model or model_label column")

    rows: pd.DataFrame = index[model_values == model_label].copy()
    if rows.empty:
        raise ValueError(f"index has no rows for {model_label}")
    if "concurrency" not in rows.columns:
        raise ValueError("index has no concurrency column")

    rows = rows[rows["concurrency"].isin(POSTER_CONCURRENCIES)].copy()
    if rows.empty:
        raise ValueError("index has no measurements at plotted concurrency values")
    if "expected_concurrencies" in rows.columns:
        rows["expected_concurrencies"] = " ".join(
            str(value) for value in POSTER_CONCURRENCIES
        )

    throughput_column: str = _throughput_col(rows)
    if throughput_column != "tok_thru_tps":
        raise ValueError(
            "index has no measured client output-throughput field tok_thru_tps"
        )
    return collapse_repeats(rows)


def report_peak_throughput(rows: pd.DataFrame) -> None:
    """Print each plotted series peak and the PP2 improvement over One Spark."""
    peak_values: dict[str, float] = {}
    for config_label, display_label in POSTER_CONFIG_LABELS.items():
        series: pd.DataFrame
        load_column: str
        mode: str
        series, load_column, mode = group(rows, config_label)
        if mode != "closed" or load_column != "concurrency":
            raise ValueError(f"{config_label} is not a closed concurrency sweep")
        if series.empty:
            raise ValueError(f"index has no rows for configuration {config_label}")
        peak_row: pd.Series = series.loc[series["tok_thru_tps"].idxmax()]
        peak_throughput: float = float(peak_row["tok_thru_tps"])
        peak_concurrency: int = int(peak_row[load_column])
        peak_values[config_label] = peak_throughput
        print(
            f"{display_label}: {peak_throughput:.3f} tokens/s at "
            f"{peak_concurrency} concurrent requests"
        )

    improvement: float = (
        peak_values["pp2"] / peak_values["single"] - 1
    ) * 100
    print(
        "PP=2 peak improvement over One Spark, plotted concurrency only: "
        f"{improvement:.2f}%"
    )


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Plot aggregate output throughput against concurrency."
    )
    parser.add_argument(
        "--index",
        default=str(DEFAULT_INDEX),
        help="campaign directory or index.csv path",
    )
    parser.add_argument(
        "--model",
        default=POSTER_MODEL_LABEL,
        help=f"model label to plot (default: {POSTER_MODEL_LABEL})",
    )
    parser.add_argument(
        "--out",
        help="figure output directory (default: campaign/figs/poster/model)",
    )
    parser.add_argument("--format", choices=("pdf", "png", "both"), default="both")
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument(
        "--aspect-ratio",
        type=parse_poster_aspect_ratio,
        default=POSTER_DEFAULT_ASPECT_RATIO,
        help=(
            "figure width-to-height ratio "
            f"(default: {POSTER_DEFAULT_ASPECT_RATIO:g})"
        ),
    )
    args: argparse.Namespace = parser.parse_args()

    index_path: Path = resolve_index_path(args.index)
    if args.out is None:
        campaign_figures: Path = resolve_campaign_output_path(
            None, index_path, default_subdir="figs"
        )
        output_directory: Path = (
            campaign_figures / "poster" / _slug(args.model)
        )
    else:
        output_directory = resolve_campaign_output_path(args.out, index_path)

    try:
        rows: pd.DataFrame = load_poster_throughput_rows(index_path, args.model)
    except ValueError as error:
        parser.error(str(error))
    report_peak_throughput(rows)
    set_rc(args.dpi, False)
    formats: list[str] = (
        ["pdf", "png"] if args.format == "both" else [args.format]
    )
    fig_poster_metric(
        rows,
        str(output_directory),
        formats,
        name=FIGURE_NAME,
        metric_column="tok_thru_tps",
        y_label=THROUGHPUT_LABEL,
        scale=1.0,
        reference_value=None,
        reference_label=None,
        aspect_ratio=args.aspect_ratio,
    )


if __name__ == "__main__":
    main()
