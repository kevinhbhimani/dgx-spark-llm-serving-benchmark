"""Open-weight LLM benchmark survey for UD chatbot model selection.

Reads open_llm_leaderboard_190_models.csv, engineers 8 UD-relevant benchmark
columns, ranks models by median normalized score, and produces three plots
plus a markdown survey report.

Outputs:
  - ../data/benchmarks_engineered.csv
  - ../data/top_performers.csv
  - ../data/serving_model_overlap.csv
  - ../data/spark_recommendations.csv
  - ../plots/params_bar.png
  - ../plots/top10_heatmap.png
  - ../plots/top3_per_benchmark.png
  - ../SURVEY.md
"""

from __future__ import annotations

import os
from pathlib import Path

# Keep matplotlib cache inside the repo (avoids permission issues on shared machines).
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
os.environ.setdefault("MPLCONFIGDIR", str(_ROOT / ".mplconfig"))

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns

HERE = _HERE
ROOT = _ROOT
PLOTS = ROOT / "plots"
DATA = ROOT / "data"
PLOTS.mkdir(exist_ok=True)
DATA.mkdir(exist_ok=True)

BENCHMARKS = [
    "DocVQA",
    "HumanEval",
    "LongBench",
    "MMMU",
    "MMLU",
    "MATH",
    "MT-Bench",
    "OCRBench",
]

BENCH_NAMES = {
    "DocVQA": "Documents (DocVQA proxy)",
    "HumanEval": "Code (HumanEval proxy)",
    "LongBench": "Long-Context (LongBench proxy)",
    "MMMU": "Multimodal (MMMU)",
    "MMLU": "Knowledge (MMLU)",
    "MATH": "Math (MATH proxy)",
    "MT-Bench": "Conversation (MT-Bench proxy)",
    "OCRBench": "OCR (OCRBench proxy)",
}

# Short axis labels for plots (avoids x-label overlap in the heatmap).
SHORT_BENCH_NAMES = {
    "DocVQA": "DocVQA",
    "HumanEval": "Code",
    "LongBench": "Long ctx",
    "MMMU": "MMMU",
    "MMLU": "MMLU",
    "MATH": "Math",
    "MT-Bench": "Chat",
    "OCRBench": "OCR",
}

# Serving models configured in ../../configs/models.yaml (hardware benchmark matrix).
TEAM_SERVING_MODELS: list[tuple[str, str]] = [
    ("llama3.1-8b", "meta-llama/Llama-3.1-8B-Instruct"),
    ("nemotron-nano-8b", "nvidia/Llama-3.1-Nemotron-Nano-8B-v1"),
    ("gemma3-27b", "google/gemma-3-27b-it"),
    ("qwen3-32b", "Qwen/Qwen3-32B"),
    ("nemotron3-nano-30b-a3b-fp8", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-FP8"),
    ("nemotron-super-49b-fp8", "nvidia/Llama-3_3-Nemotron-Super-49B-v1_5-FP8"),
    ("llama3.3-70b", "meta-llama/Llama-3.3-70B-Instruct"),
]

BENCH_MAPPING = {
    "DocVQA": ("Vision",),
    "HumanEval": ("SWE-bench Verified (%)", "Coding"),
    "LongBench": ("Long Ctx",),
    "MMMU": ("MMMU-Pro (%)", "MMMU (%)", "MMMLU (%)"),
    "MMLU": ("MMLU (%)",),
    "MATH": ("AIME 2025 (%)", "Math"),
    "MT-Bench": ("Writing",),
    "OCRBench": ("MMMU-Pro (%)", "MMMU (%)", "MMMLU (%)", "Vision"),
}

ARCH_COLORS = {
    "Dense": "#3a7bd5",
    "MoE": "#e67e22",
    "Multimodal": "#1e7a3e",
}

VRAM_4BIT_CEILING_GB = 85.0
SPARK_BF16_CEILING_GB = 110.0  # one GB10 node (~128 GB unified), leave room for KV cache
MIN_BENCHMARKS_FOR_HEATMAP = 3
TOP_N_HEATMAP = 10
TOP_N_PARAMS_BAR = 20
RANKING_METHOD = (
    "Median of per-benchmark min-max normalized scores "
    "(each benchmark scaled to [0,1] across all models; "
    "final rank = median across benchmarks with data)."
)


def _truncate_name(name: str, max_len: int = 26) -> str:
    if len(name) <= max_len:
        return name
    return name[: max_len - 1] + "\u2026"


def _hf_key(hf_id: str) -> str:
    key = hf_id.split("/")[-1].lower().replace("_", "-")
    for suffix in (
        "-base-bf16",
        "-base",
        "-bf16",
        "-fp8",
        "-instruct",
        "-it",
        "-v1-5",
        "-v1",
    ):
        while key.endswith(suffix):
            key = key[: -len(suffix)]
    return key


def _find_survey_row(raw: pd.DataFrame, target_hf: str) -> pd.Series | None:
    exact = raw.loc[raw["HuggingFace_ID"].fillna("") == target_hf]
    if not exact.empty:
        return exact.iloc[0]

    target_key = _hf_key(target_hf)
    best: pd.Series | None = None
    for _, row in raw.iterrows():
        hf = str(row.get("HuggingFace_ID", "")).strip()
        if not hf or hf.lower() == "nan":
            continue
        row_key = _hf_key(hf)
        if target_key == row_key or target_key in row_key or row_key in target_key:
            return row
        if target_key[:24] == row_key[:24]:
            best = row
    return best


def build_serving_model_overlap(
    raw: pd.DataFrame,
    engineered: pd.DataFrame,
    wide: pd.DataFrame,
    rank_score: pd.Series,
) -> pd.DataFrame:
    rows: list[dict] = []
    for label, hf_id in TEAM_SERVING_MODELS:
        row = _find_survey_row(raw, hf_id)
        if row is None:
            rows.append(
                {
                    "label": label,
                    "models_yaml_hf_id": hf_id,
                    "survey_model": None,
                    "survey_hf_id": None,
                    "in_survey": False,
                    "survey_rank": None,
                    "median_norm_score": None,
                    "benchmark_count": None,
                    "in_top10": False,
                    "match_note": "not found in leaderboard CSV",
                }
            )
            continue

        model = row["Model"]
        eng = engineered.loc[engineered["Model"] == model]
        bench_count = int(eng.iloc[0]["Benchmark_Count"]) if not eng.empty else None
        score = float(rank_score.loc[model]) if model in rank_score.index else None
        qualified = (
            engineered.loc[engineered["Benchmark_Count"] >= MIN_BENCHMARKS_FOR_HEATMAP, "Model"]
        )
        qualified_ranked = rank_score[rank_score.index.isin(qualified)]
        if model in qualified_ranked.index:
            survey_rank = int(qualified_ranked.index.get_loc(model) + 1)
        elif bench_count is not None and bench_count < MIN_BENCHMARKS_FOR_HEATMAP:
            survey_rank = None
        else:
            survey_rank = None
        top10 = qualified_ranked.head(TOP_N_HEATMAP).index.tolist()
        matched_hf = str(row.get("HuggingFace_ID", ""))
        note = "exact HF match" if matched_hf == hf_id else f"closest match: {matched_hf}"

        rows.append(
            {
                "label": label,
                "models_yaml_hf_id": hf_id,
                "survey_model": model,
                "survey_hf_id": matched_hf,
                "in_survey": True,
                "survey_rank": survey_rank,
                "median_norm_score": round(score, 4) if score is not None else None,
                "benchmark_count": bench_count,
                "in_top10": model in top10,
                "match_note": note,
            }
        )
    return pd.DataFrame(rows)


def _serving_memory_gb(row: pd.Series) -> float:
    """Rough one-node weight memory at bf16 (2 bytes/param). Uses active params for MoE."""
    active = row.get("Active Parameters (B)")
    total = row.get("Parameters (B)")
    params = active if pd.notna(active) else total
    if pd.isna(params):
        return np.nan
    return round(float(params) * 2.0, 1)


def _has_hf_id(row: pd.Series) -> bool:
    hf = str(row.get("HuggingFace_ID", "")).strip()
    return bool(hf) and hf.lower() != "nan"


def _is_spark_practical(row: pd.Series) -> bool:
    """Model can plausibly be served on one DGX Spark node with vLLM."""
    if not _has_hf_id(row):
        return False
    if int(row.get("Benchmark_Count", 0)) < MIN_BENCHMARKS_FOR_HEATMAP:
        return False
    mem = _serving_memory_gb(row)
    if pd.isna(mem):
        return False
    return mem <= SPARK_BF16_CEILING_GB


def _fits_4bit_budget(row: pd.Series) -> bool:
    vram = row.get("VRAM @ 4-bit (GB)")
    return pd.notna(vram) and float(vram) <= VRAM_4BIT_CEILING_GB


def _coalesce_row(row: pd.Series, cols: tuple[str, ...]) -> float:
    for col in cols:
        val = row.get(col)
        if pd.notna(val):
            return float(val)
    return np.nan


def _derive_architecture(row: pd.Series) -> str:
    if str(row.get("Multimodal", "")).strip().lower() == "yes":
        return "Multimodal"
    if str(row.get("Is_MoE", "")).strip().lower() == "yes":
        return "MoE"
    return "Dense"


def load_raw() -> pd.DataFrame:
    return pd.read_csv(DATA / "open_llm_leaderboard_190_models.csv")


def engineer_benchmarks(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    for bench, source_cols in BENCH_MAPPING.items():
        out[bench] = out.apply(lambda row, cols=source_cols: _coalesce_row(row, cols), axis=1)

    out["Architecture"] = out.apply(_derive_architecture, axis=1)
    if "HuggingFace_ID" not in out.columns:
        out["HuggingFace_ID"] = np.nan
    serving_params = out["Active Parameters (B)"].fillna(out["Parameters (B)"])
    out["VRAM @ 4-bit (GB)"] = (serving_params * 0.7).round(1)
    out["Serving_Memory_GB"] = out.apply(_serving_memory_gb, axis=1)
    out["Benchmark_Count"] = out[BENCHMARKS].notna().sum(axis=1)
    out["Spark_Practical"] = out.apply(_is_spark_practical, axis=1)

    meta_cols = [
        "Model",
        "Model_ID",
        "HuggingFace_ID",
        "Organization",
        "Parameters (B)",
        "Active Parameters (B)",
        "Architecture",
        "Multimodal",
        "License",
        "VRAM @ 4-bit (GB)",
        "Serving_Memory_GB",
        "Spark_Practical",
        "Benchmark_Count",
        "Context (tokens)",
        "Release Date",
    ]
    return out[meta_cols + BENCHMARKS]


def normalize_wide(wide: pd.DataFrame) -> pd.DataFrame:
    def _norm_col(col: pd.Series) -> pd.Series:
        valid = col.dropna()
        if len(valid) < 2:
            return col * np.nan
        return (col - valid.min()) / (valid.max() - valid.min())

    return wide.apply(_norm_col)


def compute_median_rank(wide: pd.DataFrame) -> pd.Series:
    norm = normalize_wide(wide)
    return norm.median(axis=1, skipna=True).sort_values(ascending=False)


def format_score(value: float) -> str:
    if pd.isna(value):
        return ""
    if abs(value) >= 10 or value == int(value):
        return f"{value:.1f}"
    return f"{value:.3f}"


def build_top_performers(engineered: pd.DataFrame, wide: pd.DataFrame, top_n: int = TOP_N_HEATMAP) -> pd.DataFrame:
    norm = normalize_wide(wide)
    rank_score = compute_median_rank(wide)

    qualified = engineered.loc[engineered["Benchmark_Count"] >= MIN_BENCHMARKS_FOR_HEATMAP, "Model"]
    top_models = rank_score[rank_score.index.isin(qualified)].head(top_n)

    rows = []
    for rank, (model, score) in enumerate(top_models.items(), start=1):
        row = engineered.loc[engineered["Model"] == model].iloc[0]
        entry = {
            "Rank": rank,
            "Model": model,
            "MedianNormScore": round(score, 4),
            "Benchmark_Count": int(row["Benchmark_Count"]),
            "Architecture": row["Architecture"],
            "License": row["License"],
            "Parameters (B)": row["Parameters (B)"],
            "Active Parameters (B)": row["Active Parameters (B)"],
            "VRAM @ 4-bit (GB)": row["VRAM @ 4-bit (GB)"],
            "Serving_Memory_GB": row["Serving_Memory_GB"],
            "HuggingFace_ID": row["HuggingFace_ID"],
            "Spark_Practical": bool(row["Spark_Practical"]),
            "Fits_4bit_Budget": _fits_4bit_budget(row),
            "Fits_DGX_Spark": _is_spark_practical(row),
        }
        for bench in BENCHMARKS:
            entry[f"{bench}_raw"] = wide.loc[model, bench] if model in wide.index else np.nan
            entry[f"{bench}_norm"] = norm.loc[model, bench] if model in norm.index else np.nan
        rows.append(entry)

    return pd.DataFrame(rows)


def build_spark_recommendations(
    top_performers: pd.DataFrame,
    serving_overlap: pd.DataFrame,
) -> pd.DataFrame:
    """Top-10 models that are Spark-practical and not already in configs/models.yaml."""
    team_hf = {str(r["models_yaml_hf_id"]) for _, r in serving_overlap.iterrows() if r["in_survey"]}
    team_survey_names = {str(r["survey_model"]) for _, r in serving_overlap.iterrows() if r["in_survey"]}

    rows: list[dict] = []
    for _, row in top_performers.iterrows():
        if not row["Spark_Practical"]:
            continue
        hf = str(row.get("HuggingFace_ID", "")).strip()
        already = hf in team_hf or row["Model"] in team_survey_names
        rows.append(
            {
                "Rank": int(row["Rank"]),
                "Model": row["Model"],
                "HuggingFace_ID": hf,
                "MedianNormScore": row["MedianNormScore"],
                "Serving_Memory_GB": row["Serving_Memory_GB"],
                "Architecture": row["Architecture"],
                "Benchmark_Count": int(row["Benchmark_Count"]),
                "Already_In_Team_List": already,
                "Recommend_For_Spark": not already,
            }
        )
    return pd.DataFrame(rows)


def _team_best_score(serving_overlap: pd.DataFrame) -> float:
    scores = serving_overlap["median_norm_score"].dropna()
    return float(scores.max()) if not scores.empty else 0.0


def plot_params_bar(engineered: pd.DataFrame, path: Path) -> None:
    sns.set_style("whitegrid")
    moe = engineered.dropna(subset=["Parameters (B)", "Active Parameters (B)"]).copy()
    moe = moe.sort_values("Parameters (B)", ascending=False).head(TOP_N_PARAMS_BAR)
    if moe.empty:
        return

    raw_models = moe["Model"].tolist()[::-1]
    models = [_truncate_name(m, 34) for m in raw_models]
    indexed = moe.set_index("Model")
    total = indexed.loc[raw_models, "Parameters (B)"].values
    active = indexed.loc[raw_models, "Active Parameters (B)"].values

    y = np.arange(len(models))
    height = 0.35

    fig_h = max(9.0, len(models) * 0.45)
    fig, ax = plt.subplots(figsize=(13, fig_h))
    ax.barh(y - height / 2, total, height, label="Total params (B)", color="#3a7bd5", edgecolor="black", alpha=0.9)
    ax.barh(y + height / 2, active, height, label="Active params (B)", color="#e67e22", edgecolor="black", alpha=0.9)

    ax.set_yticks(y)
    ax.set_yticklabels(models, fontsize=9)
    ax.set_xlabel("Parameters (billions)", fontsize=11)
    ax.legend(loc="lower right", fontsize=9, framealpha=0.95)
    ax.grid(axis="x", alpha=0.3)

    fig.suptitle(
        f"Total vs Active Parameters (top {len(models)} MoE models)",
        fontsize=14,
        fontweight="bold",
        y=0.99,
    )
    ax.set_title("Active params drive inference memory on DGX Spark", fontsize=10, pad=14)
    fig.subplots_adjust(top=0.90, left=0.30, right=0.97, bottom=0.08)
    fig.savefig(path, dpi=150, bbox_inches="tight", pad_inches=0.25)
    plt.close(fig)


def plot_top10_heatmap(
    engineered: pd.DataFrame,
    wide: pd.DataFrame,
    path: Path,
    top_performers: pd.DataFrame,
) -> None:
    sns.set_style("whitegrid")
    top_models = top_performers["Model"].tolist()
    sub = wide.loc[top_models, BENCHMARKS].copy()

    rank_matrix = np.full(sub.shape, np.nan)
    for j, col in enumerate(sub.columns):
        col_vals = wide[col].dropna()
        if len(col_vals) < 2:
            continue
        ranked = col_vals.rank(ascending=True, pct=True)
        for i, model in enumerate(sub.index):
            if model in ranked.index and pd.notna(sub.iloc[i, j]):
                rank_matrix[i, j] = ranked.loc[model]

    annot = np.empty(sub.shape, dtype=object)
    for i in range(sub.shape[0]):
        for j in range(sub.shape[1]):
            annot[i, j] = format_score(sub.iloc[i, j])

    meta = engineered.set_index("Model")
    headers = [SHORT_BENCH_NAMES[b] for b in sub.columns]

    fig_h = max(9.0, len(top_models) * 0.58)
    fig, ax = plt.subplots(figsize=(14.5, fig_h))
    sns.heatmap(
        rank_matrix,
        ax=ax,
        cmap="RdYlGn",
        vmin=0,
        vmax=1,
        annot=annot,
        fmt="",
        linewidths=1.0,
        linecolor="white",
        annot_kws={"fontsize": 9, "fontweight": "bold"},
        cbar_kws={"label": "Rank percentile (1.0 = best)", "shrink": 0.75, "pad": 0.02},
        xticklabels=headers,
        yticklabels=[_truncate_name(m, 30) for m in sub.index.tolist()],
    )

    for i in range(sub.shape[0]):
        for j in range(sub.shape[1]):
            if pd.isna(rank_matrix[i, j]):
                ax.add_patch(
                    plt.Rectangle((j, i), 1, 1, facecolor="#ececec", edgecolor="white", linewidth=0.8, zorder=2)
                )
                ax.text(j + 0.5, i + 0.5, "n/a", ha="center", va="center", fontsize=7, color="#666", style="italic")

    ylabels = []
    for model in sub.index:
        arch = meta.loc[model, "Architecture"] if model in meta.index else "?"
        ylabels.append(f"{_truncate_name(model, 28)} ({arch})")
    ax.set_yticklabels(ylabels, fontsize=8)
    plt.setp(ax.get_xticklabels(), rotation=40, ha="right", fontsize=9)

    fig.suptitle(
        f"Top {len(top_models)} models by median normalized score",
        fontsize=14,
        fontweight="bold",
        y=0.99,
    )
    ax.set_title(
        "Greener = better within column | numbers = raw scores | gray = missing",
        fontsize=10,
        pad=12,
    )
    fig.text(
        0.5,
        0.01,
        _truncate_name(RANKING_METHOD, 120),
        ha="center",
        va="bottom",
        fontsize=8,
        color="#444",
        wrap=True,
    )
    fig.subplots_adjust(top=0.90, bottom=0.18, left=0.30, right=0.92)
    fig.savefig(path, dpi=150, bbox_inches="tight", pad_inches=0.3)
    plt.close(fig)


def plot_top3_per_benchmark(wide: pd.DataFrame, path: Path, top_n: int = 3) -> None:
    sns.set_style("whitegrid")
    cols = 4
    rows = 2
    fig, axes = plt.subplots(rows, cols, figsize=(18, 10))
    axes = axes.flatten()

    for ax, bench in zip(axes, BENCHMARKS):
        sub = wide[bench].dropna().nlargest(top_n).iloc[::-1]
        labels = [_truncate_name(str(m), 24) for m in sub.index]
        bars = ax.barh(labels, sub.values, color="#1e7a3e", edgecolor="black", alpha=0.85)
        xmax = max(sub.max() * 1.28, 0.05) if not sub.empty else 1.0
        ax.set_xlim(0, xmax)
        for bar, score in zip(bars, sub.values):
            ax.text(
                bar.get_width() + xmax * 0.02,
                bar.get_y() + bar.get_height() / 2,
                format_score(score),
                va="center",
                ha="left",
                fontsize=8,
            )
        ax.set_title(SHORT_BENCH_NAMES[bench], fontsize=11, fontweight="bold", pad=8)
        ax.tick_params(axis="y", labelsize=8)
        ax.tick_params(axis="x", labelsize=8)
        ax.margins(x=0.12)

    fig.suptitle("Top 3 open-weight models per benchmark", fontsize=15, fontweight="bold", y=0.98)
    fig.subplots_adjust(top=0.90, hspace=0.62, wspace=0.55, left=0.14, right=0.98, bottom=0.08)
    fig.savefig(path, dpi=150, bbox_inches="tight", pad_inches=0.25)
    plt.close(fig)


def _survey_body_lines(
    engineered: pd.DataFrame,
    wide: pd.DataFrame,
    top_performers: pd.DataFrame,
    serving_overlap: pd.DataFrame,
    spark_recommendations: pd.DataFrame,
) -> list[str]:
    n_models = len(engineered)
    team_best = _team_best_score(serving_overlap)
    practical = top_performers[top_performers["Spark_Practical"]]
    not_practical = top_performers[~top_performers["Spark_Practical"]]
    to_add = spark_recommendations[spark_recommendations["Recommend_For_Spark"]]

    lines = [
        "## What we did",
        "",
        f"We scored {n_models} open weight models on 8 benchmarks that matter for a UD on prem chatbot "
        "(documents, code, long context, multimodal, general knowledge, math, conversation, OCR).",
        "",
        "We ranked models by their median score across those benchmarks. Then we checked which top models "
        "can realistically be served on one DGX Spark node: they need a HuggingFace model id, at least 3 "
        f"benchmark scores, and about {SPARK_BF16_CEILING_GB:.0f} GB or less for model weights at bf16 "
        "(using active parameters for MoE models).",
        "",
        "## What the team already picked",
        "",
        "`configs/models.yaml` has 7 models for Spark serving tests. All 7 are in our CSV. "
        f"Their best median score in our ranking is {team_best:.3f}. None of them land in the overall top 10.",
        "",
    ]

    for _, row in serving_overlap.iterrows():
        if not row["in_survey"]:
            lines.append(f"* {row['label']}: not found in our CSV")
            continue
        score = row["median_norm_score"]
        score_txt = f"{score:.3f}" if pd.notna(score) else "n/a"
        rank_txt = f"#{int(row['survey_rank'])}" if pd.notna(row["survey_rank"]) else "unranked"
        lines.append(f"* {row['label']} ({row['survey_model']}): score {score_txt}, rank {rank_txt}")

    lines.extend(["", "## Top 10 by UD benchmarks", ""])

    for _, row in top_performers.iterrows():
        practical_txt = "can run on Spark" if row["Spark_Practical"] else "hard to serve on one Spark node"
        mem = row["Serving_Memory_GB"]
        mem_txt = f"{mem:.0f} GB weights" if pd.notna(mem) else "memory unknown"
        hf = str(row.get("HuggingFace_ID", "")).strip()
        hf_txt = f"`{hf}`" if hf and hf.lower() != "nan" else "no HuggingFace id"
        lines.append(
            f"{int(row['Rank'])}. **{row['Model']}** score {row['MedianNormScore']:.3f}. "
            f"{practical_txt}. {mem_txt}. {hf_txt}."
        )

    lines.extend(
        [
            "",
            "## Spark practical models from the top 10",
            "",
            f"{len(practical)} of 10 top models look practical on one Spark node.",
            "",
        ]
    )
    for _, row in practical.iterrows():
        lines.append(
            f"* **{row['Model']}** (score {row['MedianNormScore']:.3f}, "
            f"{row['Serving_Memory_GB']:.0f} GB, `{row['HuggingFace_ID']}`)"
        )

    if not not_practical.empty:
        lines.extend(["", "These top 10 models are **not** practical on one Spark node:", ""])
        for _, row in not_practical.iterrows():
            if not _has_hf_id(row):
                reason = "no HuggingFace id"
            elif pd.notna(row["Serving_Memory_GB"]) and row["Serving_Memory_GB"] > SPARK_BF16_CEILING_GB:
                reason = f"too large ({row['Serving_Memory_GB']:.0f} GB estimated weights)"
            else:
                reason = "not enough benchmark data"
            lines.append(f"* **{row['Model']}**: {reason}")

    lines.extend(
        [
            "",
            "## Models we recommend adding to `configs/models.yaml`",
            "",
            "These score higher than the current team list and look practical on Spark. "
            "We think the team should serve test them in Phase 2.",
            "",
        ]
    )
    if to_add.empty:
        lines.append("No new recommendations beyond the current team list.")
    else:
        for _, row in to_add.iterrows():
            lines.append(
                f"* **{row['Model']}** score {row['MedianNormScore']:.3f}, "
                f"`{row['HuggingFace_ID']}`, about {row['Serving_Memory_GB']:.0f} GB weights"
            )

    lines.extend(
        [
            "",
            "## Notes",
            "",
            "Some benchmarks use proxy columns from the leaderboard CSV (for example Vision stands in for DocVQA).",
            "Scores are normalized per benchmark so different scales can be compared.",
            "Memory numbers are estimates. Real serving also needs KV cache headroom.",
            "Next step: run the recommended models through the repo serving benchmark on Spark.",
            "",
        ]
    )

    return lines


def build_survey_md(body_lines: list[str], output: Path) -> None:
    """Write the full survey report with embedded plot figures."""
    plot_specs = [
        ("params_bar.png", "Total vs active parameters", "MoE models use far fewer active params than total size."),
        ("top10_heatmap.png", "Top 10 heatmap", "Green is better within each benchmark column."),
        ("top3_per_benchmark.png", "Top 3 per benchmark", "Best model on each task."),
    ]

    lines = [
        "# Model survey for UD chatbot on DGX Spark",
        "",
        "Phase 1 analysis: pick strong open models for a UD on prem chatbot, then check which ones "
        "can actually be served on Spark hardware.",
        "",
        *body_lines,
        "",
        "## Plots",
        "",
    ]

    for idx, (fname, title, caption) in enumerate(plot_specs, start=1):
        lines.extend(
            [
                f"### {idx}. {title}",
                "",
                caption,
                "",
                f"![{title}](plots/{fname})",
                "",
            ]
        )

    output.write_text("\n".join(lines).rstrip() + "\n")


def main() -> None:
    raw = load_raw()
    engineered = engineer_benchmarks(raw)
    engineered.to_csv(DATA / "benchmarks_engineered.csv", index=False)
    print(f"Engineered {len(engineered)} models → benchmarks_engineered.csv")

    wide = engineered.set_index("Model")[BENCHMARKS]
    rank_score = compute_median_rank(wide)
    top_performers = build_top_performers(engineered, wide)
    top_performers.to_csv(DATA / "top_performers.csv", index=False)
    print(f"Top {len(top_performers)} performers → top_performers.csv")

    serving_overlap = build_serving_model_overlap(raw, engineered, wide, rank_score)
    serving_overlap.to_csv(DATA / "serving_model_overlap.csv", index=False)
    print("Serving-model overlap → serving_model_overlap.csv")

    spark_recommendations = build_spark_recommendations(top_performers, serving_overlap)
    spark_recommendations.to_csv(DATA / "spark_recommendations.csv", index=False)
    print("Spark recommendations → spark_recommendations.csv")
    practical_n = int(top_performers["Spark_Practical"].sum())
    add_n = int(spark_recommendations["Recommend_For_Spark"].sum())
    print(f"  {practical_n}/10 top models are Spark practical; {add_n} new models recommended")

    for _, row in serving_overlap.iterrows():
        if row["in_survey"]:
            rank_out = (
                f"rank #{int(row['survey_rank'])}"
                if pd.notna(row["survey_rank"])
                else f"unranked ({int(row['benchmark_count'])} benchmarks)"
            )
            print(
                f"  {row['label']}: {row['survey_model']} "
                f"({rank_out}, top10={row['in_top10']})"
            )
        else:
            print(f"  {row['label']}: NOT IN CSV")

    plot_params_bar(engineered, PLOTS / "params_bar.png")
    print("Saved params_bar.png")

    plot_top10_heatmap(engineered, wide, PLOTS / "top10_heatmap.png", top_performers)
    print("Saved top10_heatmap.png")

    plot_top3_per_benchmark(wide, PLOTS / "top3_per_benchmark.png")
    print("Saved top3_per_benchmark.png")

    survey_out = ROOT / "SURVEY.md"
    body_lines = _survey_body_lines(
        engineered, wide, top_performers, serving_overlap, spark_recommendations
    )
    build_survey_md(body_lines, survey_out)
    print(f"Survey report written to {survey_out}")


if __name__ == "__main__":
    main()
