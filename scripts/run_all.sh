#!/usr/bin/env bash
# Sweep a concurrency list against an already running server for one config.
# Launch the server with scripts/launch_server.sh CONFIG first, then run this
# with the matching label so the results are tagged correctly.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/connectx_env.sh"

LABEL="${1:-single}"
BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
OUT_DIR="${OUT_DIR:-results}"
# Unset means the YAML's power_backend applies; set it to override (e.g. "none"
# to disable sampling for a run without editing the config).
POWER_BACKEND="${POWER_BACKEND:-}"
CONFIG_YAML="${CONFIG_YAML:-configs/experiment.yaml}"
CONCURRENCIES="${CONCURRENCIES:-1 2 4 8 16 32 64 128}"
EXPECTED_CONCURRENCIES="${EXPECTED_CONCURRENCIES:-$CONCURRENCIES}"
# Repeated measurement windows of every point, for run-to-run spread. Each
# repeat deliberately replays the same seeded workload, making comparisons
# paired across serving configs rather than adding workload-composition noise.
# The repeat loop is
# outermost so back-to-back copies of the same point never share adjacent
# thermal/cache state. plot_results.py averages the copies and shades their
# spread.
REPEATS="${REPEATS:-3}"
# Optional space-separated repeat:concurrency pairs produced by the
# fingerprint-aware resume helper. Empty means the full rectangular sweep.
SWEEP_SCHEDULE="${SWEEP_SCHEDULE:-}"
# Interpreter for the benchmark; python3 works with or without the venv's
# `python` alias on PATH.
PYTHON="${PYTHON:-python3}"

if [[ ! "$REPEATS" =~ ^[1-9][0-9]*$ ]]; then
  echo "[sweep] REPEATS must be a positive integer, got '$REPEATS'" >&2
  exit 1
fi
for list_name in CONCURRENCIES EXPECTED_CONCURRENCIES; do
  value="${!list_name}"
  if [[ -z "${value//[[:space:]]/}" ]]; then
    echo "[sweep] $list_name must not be empty" >&2
    exit 1
  fi
  for c in $value; do
    if [[ ! "$c" =~ ^[1-9][0-9]*$ ]]; then
      echo "[sweep] $list_name contains invalid concurrency '$c'" >&2
      exit 1
    fi
  done
done

# Dual-node labels: sample the worker's GPU power too, through a Ray actor in
# the serving container, so per-token energy covers both nodes. Set
# WORKER_POWER_NODE_IP="" to disable (rows then record power_nodes=1).
if [[ "$LABEL" != "single" ]]; then
  WORKER_POWER_NODE_IP="${WORKER_POWER_NODE_IP-$HOEDUS_IP}"
  WORKER_POWER_CONTAINER="${WORKER_POWER_CONTAINER:-${VLLM_CONTAINER:-vllm-spark}}"
else
  WORKER_POWER_NODE_IP="${WORKER_POWER_NODE_IP:-}"
  WORKER_POWER_CONTAINER="${WORKER_POWER_CONTAINER:-}"
fi

# Optional passthroughs, set by the multi-model orchestrator (run_models.sh) or
# by hand. Unset means the benchmark resolves the model from the server and
# derives a label from it.
MODEL="${MODEL:-}"
MODEL_REVISION="${MODEL_REVISION:-}"
MODEL_LABEL="${MODEL_LABEL:-}"
SERVER_LOG="${SERVER_LOG:-}"
TOKENIZER="${TOKENIZER:-}"
TOKENIZER_REVISION="${TOKENIZER_REVISION:-}"
TRUST_REMOTE_CODE="${TRUST_REMOTE_CODE:-}"
SERVING_FINGERPRINT="${SERVING_FINGERPRINT:-}"
DEPLOYMENT_FINGERPRINT="${DEPLOYMENT_FINGERPRINT:-}"

run_point() {
    local rep="$1" c="$2"
    echo "[sweep] ${MODEL_LABEL:+$MODEL_LABEL }$LABEL concurrency=$c (repeat $rep/$REPEATS)"
    args=( --config "$CONFIG_YAML" --config-label "$LABEL" --mode closed
           --concurrency "$c" --expected-concurrencies "$EXPECTED_CONCURRENCIES"
           --base-url "$BASE_URL" --out-dir "$OUT_DIR" )
    [[ -n "$POWER_BACKEND" ]] && args+=( --power-backend "$POWER_BACKEND" )
    [[ -n "$MODEL" ]]       && args+=( --model "$MODEL" )
    [[ -n "$MODEL_REVISION" ]] && args+=( --model-revision "$MODEL_REVISION" )
    [[ -n "$MODEL_LABEL" ]] && args+=( --model-label "$MODEL_LABEL" )
    [[ -n "$SERVER_LOG" ]]  && args+=( --server-log "$SERVER_LOG" )
    [[ -n "$TOKENIZER" ]]   && args+=( --tokenizer "$TOKENIZER" )
    [[ -n "$TOKENIZER_REVISION" ]] && \
      args+=( --tokenizer-revision "$TOKENIZER_REVISION" )
    [[ -n "$SERVING_FINGERPRINT" ]] && \
      args+=( --serving-fingerprint "$SERVING_FINGERPRINT" )
    [[ -n "$DEPLOYMENT_FINGERPRINT" ]] && \
      args+=( --deployment-fingerprint "$DEPLOYMENT_FINGERPRINT" )
    args+=( --repeat-id "$rep" --repeat-count "$REPEATS" )
    [[ -n "$TRUST_REMOTE_CODE" ]] && args+=( --trust-remote-code )
    [[ -n "$WORKER_POWER_NODE_IP" && -n "$WORKER_POWER_CONTAINER" ]] && \
      args+=( --worker-power-node-ip "$WORKER_POWER_NODE_IP"
              --worker-power-container "$WORKER_POWER_CONTAINER" )
    "$PYTHON" -m bench.run_benchmark "${args[@]}"
    sleep 3
}

if [[ -n "$SWEEP_SCHEDULE" ]]; then
  for item in $SWEEP_SCHEDULE; do
    if [[ ! "$item" =~ ^[1-9][0-9]*:[1-9][0-9]*$ ]]; then
      echo "[sweep] invalid SWEEP_SCHEDULE item '$item' (expected repeat:concurrency)" >&2
      exit 1
    fi
    run_point "${item%%:*}" "${item#*:}"
  done
else
  for rep in $(seq 1 "$REPEATS"); do
    for c in $CONCURRENCIES; do
      run_point "$rep" "$c"
    done
  done
fi

echo "[sweep] done, see $OUT_DIR/index.csv"
