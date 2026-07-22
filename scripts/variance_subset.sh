#!/usr/bin/env bash
# Variance / confidence-interval subset for the paper's #1 limitation
# ("single repeat per point"). Runs a FEW representative (model, config) points
# at a FEW concurrencies with REPEATS>1, into a SEPARATE OUT_DIR so the sealed
# main campaign stays a clean single-repeat rectangle (the harness forbids a
# ragged repeat count inside a publication index; a sibling directory sidesteps
# that cleanly).
#
# It just sets env and calls the unmodified, fingerprinted scripts/run_models.sh
# once per config, so it changes no measurement source. Run it on the head node
# with Ray up on both nodes and the checkpoints already cached/mirrored (they
# are the same models as the main campaign, so predownload/mirror already cover
# them). Dual-node configs run first; single LAST, because the single launcher
# tears down the Ray head container.
#
#   scripts/variance_subset.sh
#
# Then summarize the spread:
#   python3 scripts/variance_report.py --index results-variance/index.csv
#
# Knobs (env): OUT_DIR, VAR_MODELS, VAR_CONCURRENCIES, VAR_REPEATS, VAR_CONFIGS.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE/.."

export OUT_DIR="${OUT_DIR:-results-variance}"
# Representative of the three regimes the paper leans on: small dense that
# saturates, MoE that sustains goodput, large dense NVFP4 where PP wins.
VAR_MODELS="${VAR_MODELS:-qwen3-8b nemotron3-nano-30b-a3b-nvfp4 llama3.3-70b-nvfp4}"
# Unloaded, near-knee, saturated -- enough to show the window is stable across
# the load regimes, not just at one point.
VAR_CONCURRENCIES="${VAR_CONCURRENCIES:-1 32 128}"
VAR_REPEATS="${VAR_REPEATS:-3}"
# Dual first, single LAST (single tears down the Ray head container). tp2 is
# omitted by default to halve cost; add it here if you want TP variance too.
VAR_CONFIGS="${VAR_CONFIGS:-pp2 single}"

echo "[variance] OUT_DIR=$OUT_DIR"
echo "[variance] models: $VAR_MODELS"
echo "[variance] concurrencies: $VAR_CONCURRENCIES  repeats: $VAR_REPEATS"
echo "[variance] configs (in order): $VAR_CONFIGS"
n_models=$(wc -w <<<"$VAR_MODELS"); n_conc=$(wc -w <<<"$VAR_CONCURRENCIES")
n_cfg=$(wc -w <<<"$VAR_CONFIGS")
echo "[variance] ~$(( n_models * n_conc * VAR_REPEATS * n_cfg )) measurement points" \
     "(~0.5 h each incl. warmup/cooldown, before model loads)"

rc=0
for cfg in $VAR_CONFIGS; do
  echo "[variance] === config=$cfg ==="
  if ! env \
      OUT_DIR="$OUT_DIR" \
      MODEL_LABELS="$VAR_MODELS" \
      CONCURRENCIES="$VAR_CONCURRENCIES" \
      EXPECTED_CONCURRENCIES="$VAR_CONCURRENCIES" \
      REPEATS="$VAR_REPEATS" \
      SKIP_DONE=1 \
      scripts/run_models.sh "$cfg" --skip-done; then
    echo "[variance] config=$cfg returned an error (continuing)" >&2
    rc=1
  fi
done

echo "[variance] done (rc=$rc). Summarize with:"
echo "           python3 scripts/variance_report.py --index $OUT_DIR/index.csv"
exit "$rc"
