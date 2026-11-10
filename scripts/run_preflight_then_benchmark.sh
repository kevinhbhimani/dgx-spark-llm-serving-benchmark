#!/usr/bin/env bash
# Run the master preflight and full campaign under one output directory.
set -euo pipefail

if (( $# != 0 )); then
  echo "usage: scripts/run_preflight_then_benchmark.sh" >&2
  exit 2
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"
source "$HERE/scripts/load_env.sh" || exit 1
resolve_single_node_mode || exit 2
if [[ "$SINGLE_NODE_ONLY" == "1" ]]; then
  if [[ "${PURGE_WORKER:-0}" == "1" ]]; then
    echo "[serial] PURGE_WORKER=1 is invalid with SINGLE_NODE_ONLY=1" >&2
    exit 2
  fi
  for requested_config in ${CAMPAIGN_CONFIGS:-}; do
    if [[ "$requested_config" == "tp2" || "$requested_config" == "pp2" ]]; then
      echo "[serial] CAMPAIGN_CONFIGS may contain only single with SINGLE_NODE_ONLY=1" >&2
      exit 2
    fi
    if [[ "$requested_config" != "single" ]]; then
      echo "[serial] CAMPAIGN_CONFIGS contains unsupported config '$requested_config'" >&2
      exit 2
    fi
  done
fi

if [[ -z "${PYTHON:-}" ]]; then
  if [[ -x "$HERE/.venv/bin/python" ]]; then
    PYTHON="$HERE/.venv/bin/python"
  else
    PYTHON=python3
  fi
fi
export PYTHON

if [[ -n "${OUT_DIR:-}" ]]; then
  CAMPAIGN_OUT_DIR="$("$PYTHON" "$HERE/utils/campaign_output.py" resolve \
    --out-dir "$OUT_DIR")" || exit 1
else
  CAMPAIGN_OUT_DIR="$("$PYTHON" "$HERE/utils/campaign_output.py" resolve \
    --next-available)" || exit 1
fi
echo "[serial] campaign output: $CAMPAIGN_OUT_DIR"
if env OUT_DIR="$CAMPAIGN_OUT_DIR" SINGLE_NODE_ONLY="$SINGLE_NODE_ONLY" "$HERE/scripts/preflight.sh"; then
  echo "[serial] all preflights and the canary passed"
else
  status=$?
  echo "[serial] preflight failed with exit $status; full benchmark was not started" >&2
  exit "$status"
fi

echo "[serial] starting full benchmark: $CAMPAIGN_OUT_DIR"
exec env OUT_DIR="$CAMPAIGN_OUT_DIR" PLAN_ONLY=0 STOP_AFTER_CANARY=0 \
  ALLOW_CANARY_FAILURES=0 SINGLE_NODE_ONLY="$SINGLE_NODE_ONLY" \
  "$HERE/scripts/run_job.sh"
