#!/usr/bin/env python3
"""Render the Qwen3-Next first-token wait graph from the campaign index."""
from __future__ import annotations

import csv
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BASELINE_DIRECTORY = REPOSITORY_ROOT / "baselines" / "rdma-pilot-2026-10"
INDEX_PATH = BASELINE_DIRECTORY / "index.csv"
EXCLUSIONS_PATH = BASELINE_DIRECTORY / "figs" / "plot_exclusions.csv"
OUTPUT_BASE = (
    BASELINE_DIRECTORY / "figs" / "qwen3-next-80b-nvfp4" / "latency_throughput"
)
MODEL_LABEL = "qwen3-next-80b-nvfp4"
EXCLUDED_RUN_TAG = (
    "qwen3-next-80b-nvfp4_single_closed_128_r1_20261015-095610-472921515"
)
CONFIG_STYLES: dict[str, tuple[str, str]] = {
    "single": ("#0072B2", "circle"),
    "tp2": ("#D55E00", "square"),
    "pp2": ("#009E73", "triangle"),
}
EXPECTED_CONCURRENCIES: dict[str, frozenset[int]] = {
    "single": frozenset({1, 2, 4, 8, 16, 32, 64}),
    "tp2": frozenset({1, 2, 4, 8, 16, 32, 64, 128}),
    "pp2": frozenset({1, 2, 4, 8, 16, 32, 64, 128}),
}
WIDTH = 1536
HEIGHT = 340
PLOT_LEFT = 170
PLOT_RIGHT = 68
PLOT_TOP = 28
PLOT_BOTTOM = 91
MAXIMUM_THROUGHPUT = 360.0
MAXIMUM_SECONDS = 3.0
THROUGHPUT_TICKS = (0, 100, 200, 300)
SECONDS_TICKS = (0, 1, 2, 3)
IMAGE_DENSITY = "96"
Y_LABEL_FONT = "DejaVu-Sans"
Y_LABEL_POINT_SIZE = "32"
Y_LABEL_CANVAS = "340x80"
Y_LABEL_OFFSET_X = "+45"


@dataclass(frozen=True, slots=True)
class DataPoint:
    concurrency: int
    throughput: float
    seconds: float


def read_number(row: dict[str, str | None], column: str, run_tag: str) -> float:
    raw_value = row.get(column)
    if raw_value is None or not raw_value.strip():
        raise ValueError(f"{run_tag} has no {column} value")
    value = float(raw_value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{run_tag} has an invalid {column} value: {raw_value}")
    return value


def load_excluded_run_tags(path: Path) -> set[str]:
    with path.open(newline="", encoding="utf-8") as exclusions_file:
        reader = csv.DictReader(exclusions_file)
        if "run_tag" not in (reader.fieldnames or []):
            raise ValueError(f"{path} has no run_tag column")
        excluded_run_tags = {
            row["run_tag"]
            for row in reader
            if row.get("run_tag")
        }
    if EXCLUDED_RUN_TAG not in excluded_run_tags:
        raise ValueError(f"{path} does not record {EXCLUDED_RUN_TAG}")
    return excluded_run_tags


def load_plot_points(
    index_path: Path, excluded_run_tags: set[str]
) -> dict[str, list[DataPoint]]:
    points_by_config: dict[str, list[DataPoint]] = {
        config: [] for config in CONFIG_STYLES
    }
    required_columns = {
        "run_tag", "model_label", "config_label", "concurrency",
        "tok_thru_tps", "ttft_p50",
    }
    with index_path.open(newline="", encoding="utf-8") as index_file:
        reader = csv.DictReader(index_file)
        missing_columns = required_columns - set(reader.fieldnames or [])
        if missing_columns:
            raise ValueError(
                f"{index_path} is missing columns: {', '.join(sorted(missing_columns))}"
            )
        for row in reader:
            run_tag = row.get("run_tag") or ""
            config = row.get("config_label") or ""
            if (
                run_tag in excluded_run_tags
                or row.get("model_label") != MODEL_LABEL
                or config not in points_by_config
            ):
                continue
            concurrency_value = read_number(row, "concurrency", run_tag)
            if not concurrency_value.is_integer():
                raise ValueError(f"{run_tag} has a non-integer concurrency")
            point = DataPoint(
                concurrency=int(concurrency_value),
                throughput=read_number(row, "tok_thru_tps", run_tag),
                seconds=read_number(row, "ttft_p50", run_tag),
            )
            if point.throughput > MAXIMUM_THROUGHPUT:
                raise ValueError(
                    f"{run_tag} exceeds the graph's throughput range: "
                    f"{point.throughput:g} tok/s"
                )
            if point.seconds > MAXIMUM_SECONDS:
                raise ValueError(
                    f"{run_tag} exceeds the graph's seconds range: "
                    f"{point.seconds:g} s"
                )
            points_by_config[config].append(point)

    for config, points in points_by_config.items():
        points.sort(key=lambda point: point.concurrency)
        actual_concurrencies = [point.concurrency for point in points]
        if len(actual_concurrencies) != len(set(actual_concurrencies)):
            raise ValueError(f"{config} has duplicate concurrency rows")
        if set(actual_concurrencies) != EXPECTED_CONCURRENCIES[config]:
            raise ValueError(
                f"{config} concurrency rows are {actual_concurrencies}, "
                f"expected {sorted(EXPECTED_CONCURRENCIES[config])}"
            )
    return points_by_config


def x_position(throughput: float) -> float:
    plot_width = WIDTH - PLOT_LEFT - PLOT_RIGHT
    return PLOT_LEFT + throughput / MAXIMUM_THROUGHPUT * plot_width


def y_position(seconds: float) -> float:
    plot_height = HEIGHT - PLOT_TOP - PLOT_BOTTOM
    return HEIGHT - PLOT_BOTTOM - seconds / MAXIMUM_SECONDS * plot_height


def build_svg(points_by_config: dict[str, list[DataPoint]]) -> str:
    svg_parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
        f'height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">',
        '<title>Qwen3-Next-80B-A3B first-token wait</title>',
        '<desc>Median TTFT in seconds versus output tokens per second. '
        'The recorded Single Spark concurrency 128 run is excluded.</desc>',
        '<rect width="100%" height="100%" fill="white"/>',
    ]
    axis_bottom = HEIGHT - PLOT_BOTTOM
    axis_right = WIDTH - PLOT_RIGHT
    for tick in SECONDS_TICKS:
        y = y_position(float(tick))
        if tick > 0:
            svg_parts.append(
                f'<line x1="{PLOT_LEFT}" y1="{y:.2f}" '
                f'x2="{axis_right}" y2="{y:.2f}" '
                'stroke="#d8e1e9" stroke-width="1.5"/>'
            )
        svg_parts.append(
            f'<text x="{PLOT_LEFT-12}" y="{y+10:.2f}" text-anchor="end" '
            'font-family="Arial, sans-serif" font-size="30" '
            f'fill="#152437">{tick}</text>'
        )
    for tick in THROUGHPUT_TICKS:
        x = x_position(float(tick))
        svg_parts.extend([
            f'<line x1="{x:.2f}" y1="{axis_bottom}" '
            f'x2="{x:.2f}" y2="{axis_bottom+7}" '
            'stroke="#8795a5" stroke-width="2"/>',
            f'<text x="{x:.2f}" y="{axis_bottom+35}" text-anchor="middle" '
            'font-family="Arial, sans-serif" font-size="30" '
            f'fill="#152437">{tick}</text>',
        ])
    svg_parts.extend([
        f'<line x1="{PLOT_LEFT}" y1="{PLOT_TOP}" '
        f'x2="{PLOT_LEFT}" y2="{axis_bottom}" '
        'stroke="#8795a5" stroke-width="2"/>',
        f'<line x1="{PLOT_LEFT}" y1="{axis_bottom}" '
        f'x2="{axis_right}" y2="{axis_bottom}" '
        'stroke="#8795a5" stroke-width="2"/>',
        f'<text x="{(PLOT_LEFT+axis_right)/2:.2f}" y="{HEIGHT-12}" '
        'text-anchor="middle" font-family="Arial, sans-serif" '
        'font-size="24" fill="#152437">'
        'Output tokens per second</text>',
    ])
    for config, (color, marker) in CONFIG_STYLES.items():
        points = points_by_config[config]
        coordinates = [
            (x_position(point.throughput), y_position(point.seconds))
            for point in points
        ]
        line_points = " ".join(
            f"{x:.2f},{y:.2f}" for x, y in coordinates
        )
        svg_parts.append(
            f'<polyline points="{line_points}" fill="none" stroke="{color}" '
            'stroke-width="4" stroke-linejoin="round" stroke-linecap="round"/>'
        )
        for x, y in coordinates:
            if marker == "circle":
                svg_parts.append(
                    f'<circle cx="{x:.2f}" cy="{y:.2f}" r="7.5" '
                    f'fill="{color}"/>'
                )
            elif marker == "square":
                svg_parts.append(
                    f'<rect x="{x-7.5:.2f}" y="{y-7.5:.2f}" '
                    f'width="15" height="15" fill="{color}"/>'
                )
            else:
                svg_parts.append(
                    f'<polygon points="{x:.2f},{y-9:.2f} '
                    f'{x-8.5:.2f},{y+6.5:.2f} {x+8.5:.2f},{y+6.5:.2f}" '
                    f'fill="{color}"/>'
                )
    svg_parts.append('</svg>')
    return "\n".join(svg_parts)


def render_graph(svg: str, output_base: Path) -> tuple[Path, Path]:
    convert_executable = shutil.which("convert")
    if convert_executable is None:
        raise FileNotFoundError("ImageMagick 'convert' is required to render PNG and PDF")
    output_base.parent.mkdir(parents=True, exist_ok=True)
    output_paths = (output_base.with_suffix(".png"), output_base.with_suffix(".pdf"))
    with tempfile.TemporaryDirectory(
        prefix="plot-first-token-wait-", dir=output_base.parent
    ) as temporary_directory:
        temporary_base = Path(temporary_directory) / output_base.name
        svg_path = temporary_base.with_suffix(".svg")
        svg_path.write_text(svg, encoding="utf-8")
        temporary_paths = (
            temporary_base.with_suffix(".png"),
            temporary_base.with_suffix(".pdf"),
        )
        rendered_png_path = temporary_base.with_name(
            f"{temporary_base.name}-without-y-label.png"
        )
        y_label_path = temporary_base.with_name(f"{temporary_base.name}-y-label.png")
        subprocess.run(
            [
                convert_executable,
                "-density", IMAGE_DENSITY,
                str(svg_path),
                "-background", "white",
                str(rendered_png_path),
            ],
            check=True,
        )
        subprocess.run(
            [
                convert_executable,
                "-size", Y_LABEL_CANVAS,
                "xc:none",
                "-font", Y_LABEL_FONT,
                "-pointsize", Y_LABEL_POINT_SIZE,
                "-fill", "#152437",
                "-gravity", "center",
                "-annotate", "+0+0", "Seconds",
                "-rotate", "270",
                str(y_label_path),
            ],
            check=True,
        )
        subprocess.run(
            [
                convert_executable,
                str(rendered_png_path),
                str(y_label_path),
                "-geometry", f"{Y_LABEL_OFFSET_X}+0",
                "-compose", "over",
                "-composite",
                str(temporary_paths[0]),
            ],
            check=True,
        )
        subprocess.run(
            [
                convert_executable,
                "-density", IMAGE_DENSITY,
                str(temporary_paths[0]),
                "-background", "white",
                str(temporary_paths[1]),
            ],
            check=True,
        )
        for temporary_path, output_path in zip(temporary_paths, output_paths):
            temporary_path.replace(output_path)
    return output_paths


def main() -> int:
    excluded_run_tags = load_excluded_run_tags(EXCLUSIONS_PATH)
    points_by_config = load_plot_points(INDEX_PATH, excluded_run_tags)
    output_paths = render_graph(build_svg(points_by_config), OUTPUT_BASE)
    for output_path in output_paths:
        print(f"wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
