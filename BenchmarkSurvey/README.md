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
| `data/top_performers.csv` | Top 10 models by UD benchmark score |
| `data/spark_recommendations.csv` | Top models that fit Spark and are not already in `configs/models.yaml` |
| `data/serving_model_overlap.csv` | How the team's 7 models rank in this survey |
| `plots/*.png` | Figures referenced by `SURVEY.md` |

## How this connects to the rest of the repo

`configs/models.yaml` lists models for Phase 2 hardware serving tests. This survey suggests additional models from the top 10 that score higher and look practical on Spark. See `SURVEY.md` for the list.
