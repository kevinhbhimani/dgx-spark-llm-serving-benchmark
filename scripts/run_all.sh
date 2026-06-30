#!/usr/bin/env bash
# Sweep a concurrency list against an already running server for one config.
# Launch the server with scripts/launch_server.sh CONFIG first, then run this
# with the matching label so the results are tagged correctly.
set -euo pipefail

LABEL="${1:-single}"
BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
OUT_DIR="${OUT_DIR:-results}"
POWER_BACKEND="${POWER_BACKEND:-none}"
CONFIG_YAML="${CONFIG_YAML:-configs/experiment.yaml}"
CONCURRENCIES="${CONCURRENCIES:-1 2 4 8 16 32 64 128}"

for c in $CONCURRENCIES; do
  echo "[sweep] $LABEL concurrency=$c"
  python -m bench.run_benchmark \
    --config "$CONFIG_YAML" \
    --config-label "$LABEL" \
    --mode closed \
    --concurrency "$c" \
    --base-url "$BASE_URL" \
    --out-dir "$OUT_DIR" \
    --power-backend "$POWER_BACKEND"
  sleep 3
done

echo "[sweep] done, see $OUT_DIR/index.csv"