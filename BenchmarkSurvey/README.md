# BenchmarkSurvey

Phase 1 model selection for the UD on prem chatbot project.

We score 190 open models on 8 UD relevant benchmarks, rank them, and check which top models can be served on one DGX Spark node.

## Run

Place `open_llm_leaderboard_190_models.csv` in `data/` (not committed). Then:

```bash
cd BenchmarkSurvey
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/survey_analysis.py
```

## Outputs

| File | What it is |
|------|------------|
| `SURVEY.md` | Main report with findings and plots |
| `plots/params_bar.png` | MoE parameter chart (committed) |
| `plots/top10_heatmap.png` | Top 10 benchmark heatmap (committed) |
| `plots/top3_per_benchmark.png` | Top 3 per benchmark chart (committed) |
| `data/top_performers.csv` | Top 10 models by UD benchmark score (local, gitignored) |
| `data/spark_recommendations.csv` | Spark practical models not in `configs/models.yaml` (local) |
| `data/serving_model_overlap.csv` | How the team's 7 models rank in this survey (local) |

## How this connects to the rest of the repo

`configs/models.yaml` lists models for Phase 2 hardware serving tests. This survey suggests additional models from the top 10 that score higher and look practical on Spark. See `SURVEY.md` for the list.
