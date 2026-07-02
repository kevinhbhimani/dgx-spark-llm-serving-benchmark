#!/usr/bin/env bash
# Sweep a concurrency list against an already running server for one config.
# Launch the server with scripts/launch_server.sh CONFIG first, then run this
# with the matching label so the results are tagged correctly.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/load_env.sh"

LABEL="${1:-single}"
BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
OUT_DIR="${OUT_DIR:-results}"
POWER_BACKEND="${POWER_BACKEND:-none}"
CONFIG_YAML="${CONFIG_YAML:-configs/experiment.yaml}"
CONCURRENCIES="${CONCURRENCIES:-1 2 4 8 16 32 64 128}"

# Optional passthroughs, set by the multi-model orchestrator (run_models.sh) or
# by hand. Unset means the benchmark resolves the model from the server and
# derives a label from it.
MODEL="${MODEL:-}"
MODEL_LABEL="${MODEL_LABEL:-}"
SERVER_LOG="${SERVER_LOG:-}"
TOKENIZER="${TOKENIZER:-}"

for c in $CONCURRENCIES; do
  echo "[sweep] ${MODEL_LABEL:+$MODEL_LABEL }$LABEL concurrency=$c"
  args=( --config "$CONFIG_YAML" --config-label "$LABEL" --mode closed
         --concurrency "$c" --base-url "$BASE_URL" --out-dir "$OUT_DIR"
         --power-backend "$POWER_BACKEND" )
  [[ -n "$MODEL" ]]       && args+=( --model "$MODEL" )
  [[ -n "$MODEL_LABEL" ]] && args+=( --model-label "$MODEL_LABEL" )
  [[ -n "$SERVER_LOG" ]]  && args+=( --server-log "$SERVER_LOG" )
  [[ -n "$TOKENIZER" ]]   && args+=( --tokenizer "$TOKENIZER" )
  python -m bench.run_benchmark "${args[@]}"
  sleep 3
done

echo "[sweep] done, see $OUT_DIR/index.csv"
