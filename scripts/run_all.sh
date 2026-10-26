#!/usr/bin/env bash
# Sweep a concurrency list against an already running server for one config.
# Launch the server with scripts/launch_server.sh CONFIG first, then run this
# with the matching label so the results are tagged correctly.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/connectx_env.sh"
source "$HERE/campaign_output.sh"
LABEL="${1-single}"
case "$LABEL" in
  single|tp2|pp2) ;;
  *)
    echo "[sweep] LABEL must be one of single, tp2, or pp2; got '$LABEL'" >&2
    exit 1
    ;;
esac
BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
campaign_resolve_out_dir
campaign_reject_existing_implicit_out_dir || exit 1
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
declare -A EXPECTED_SET=()
for c in $EXPECTED_CONCURRENCIES; do
  if [[ ! "$c" =~ ^[1-9][0-9]*$ || -n "${EXPECTED_SET[$c]:-}" ]]; then
    echo "[sweep] invalid or duplicate expected concurrency '$c'" >&2
    exit 1
  fi
  EXPECTED_SET[$c]=1
done
if [[ "${#EXPECTED_SET[@]}" -eq 0 ]]; then
  echo "[sweep] EXPECTED_CONCURRENCIES must not be empty" >&2
  exit 1
fi

declare -A REQUESTED_SET=()
for c in $CONCURRENCIES; do
  if [[ ! "$c" =~ ^[1-9][0-9]*$ || -n "${REQUESTED_SET[$c]:-}" ]]; then
    echo "[sweep] invalid or duplicate requested concurrency '$c'" >&2
    exit 1
  fi
  if [[ -z "${EXPECTED_SET[$c]:-}" ]]; then
    echo "[sweep] requested concurrency $c is outside EXPECTED_CONCURRENCIES" >&2
    exit 1
  fi
  REQUESTED_SET[$c]=1
done
if [[ "${#REQUESTED_SET[@]}" -eq 0 ]]; then
  echo "[sweep] CONCURRENCIES must not be empty" >&2
  exit 1
fi

# Dual-node labels: sample the worker's GPU power too, through a Ray actor in
# the serving container, so per-token energy covers both nodes. Set
# WORKER_POWER_NODE_IP="" to disable (rows then record power_nodes=1).
if [[ "$LABEL" != "single" ]]; then
  WORKER_POWER_NODE_IP="${WORKER_POWER_NODE_IP-$HOEDUS_IP}"
  WORKER_POWER_CONTAINER="${WORKER_POWER_CONTAINER:-${VLLM_CONTAINER:-vllm-spark}}"
  if [[ -n "$WORKER_POWER_NODE_IP" && \
        ( "$WORKER_POWER_NODE_IP" != "$HOEDUS_IP" || \
          "$WORKER_POWER_NODE_IP" == "$HEAD_IP" ) ]]; then
    echo "[sweep] dual-node WORKER_POWER_NODE_IP must equal HOEDUS_IP ($HOEDUS_IP) and differ from HEAD_IP ($HEAD_IP); got '$WORKER_POWER_NODE_IP'" >&2
    exit 1
  fi
else
  if [[ -n "${WORKER_POWER_NODE_IP:-}" || -n "${WORKER_POWER_CONTAINER:-}" ]]; then
    echo "[sweep] single-node runs reject WORKER_POWER_NODE_IP and WORKER_POWER_CONTAINER" >&2
    exit 1
  fi
  WORKER_POWER_NODE_IP="${WORKER_POWER_NODE_IP:-}"
  WORKER_POWER_CONTAINER="${WORKER_POWER_CONTAINER:-}"
fi

require_local_connectx_ip "$HEAD_IP" "benchmark head role" || exit 1
acquire_campaign_lock || exit 1

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
SERVING_POLICY_FILE="${SERVING_POLICY_FILE:-}"
DEPLOYMENT_FINGERPRINT="${DEPLOYMENT_FINGERPRINT:-}"

run_point() {
    local rep="$1" c="$2"
    local status=0
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
    [[ -n "$SERVING_POLICY_FILE" ]] && \
      args+=( --serving-policy-file "$SERVING_POLICY_FILE" )
    [[ -n "$DEPLOYMENT_FINGERPRINT" ]] && \
      args+=( --deployment-fingerprint "$DEPLOYMENT_FINGERPRINT" )
    args+=( --repeat-id "$rep" --repeat-count "$REPEATS" )
    [[ -n "$TRUST_REMOTE_CODE" ]] && args+=( --trust-remote-code )
    [[ "$LABEL" != "single" ]] && args+=( --head-node-ip "$HEAD_IP" )
    [[ -n "$WORKER_POWER_NODE_IP" && -n "$WORKER_POWER_CONTAINER" ]] && \
      args+=( --worker-power-node-ip "$WORKER_POWER_NODE_IP"
              --worker-power-container "$WORKER_POWER_CONTAINER" )
    if "$PYTHON" -m bench.run_benchmark "${args[@]}"; then
      :
    else
      status=$?
    fi
    sleep 3
    return "$status"
}

POINT_FAILURES=()
CAMPAIGN_ABORTED=0

server_is_healthy() {
  local body
  if ! body="$(curl -fsS --max-time 5 "$BASE_URL/v1/models")"; then
    echo "[sweep] serving endpoint is unavailable after a failed point" >&2
    return 1
  fi
  if [[ -n "$MODEL" ]]; then
    if ! "$PYTHON" -c 'import json, sys
try:
    models = json.load(sys.stdin).get("data") or []
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if any(model.get("id") == sys.argv[1] for model in models)
                 else 1)' "$MODEL" <<<"$body"; then
      echo "[sweep] expected model $MODEL is not served after a failed point" >&2
      return 1
    fi
  fi
  return 0
}

run_scheduled_point() {
  local rep="$1" c="$2" status=0
  if run_point "$rep" "$c"; then
    return 0
  else
    status=$?
  fi

  POINT_FAILURES+=("repeat=$rep concurrency=$c rc=$status")
  echo "[sweep] point FAILED (rc=$status): ${MODEL_LABEL:+label=$MODEL_LABEL }" \
       "model=${MODEL:-unknown} revision=${MODEL_REVISION:-unknown} config=$LABEL" \
       "repeat=$rep concurrency=$c head=${HEAD_IP:-unknown}" \
       "worker=${WORKER_POWER_NODE_IP:-none} results=$OUT_DIR" \
       "server_log=${SERVER_LOG:-none}; checking shared serving state" >&2
  if ! server_is_healthy; then
    echo "[sweep] stopping because shared serving state is unhealthy" >&2
    CAMPAIGN_ABORTED=1
    return 1
  fi
  echo "[sweep] shared serving state is healthy; continuing to the next point" >&2
  return 0
}

if [[ -n "$SWEEP_SCHEDULE" ]]; then
  for item in $SWEEP_SCHEDULE; do
    if [[ ! "$item" =~ ^[1-9][0-9]*:[1-9][0-9]*$ ]]; then
      echo "[sweep] invalid SWEEP_SCHEDULE item '$item' (expected repeat:concurrency)" >&2
      exit 1
    fi
    rep="${item%%:*}"
    c="${item#*:}"
    if (( rep > REPEATS )); then
      echo "[sweep] schedule repeat $rep exceeds REPEATS=$REPEATS" >&2
      exit 1
    fi
    if [[ -z "${EXPECTED_SET[$c]:-}" ]]; then
      echo "[sweep] schedule concurrency $c is outside EXPECTED_CONCURRENCIES" >&2
      exit 1
    fi
    if ! run_scheduled_point "$rep" "$c" && (( CAMPAIGN_ABORTED )); then
      break
    fi
  done
else
  for rep in $(seq 1 "$REPEATS"); do
    for c in $CONCURRENCIES; do
      if ! run_scheduled_point "$rep" "$c" && (( CAMPAIGN_ABORTED )); then
        break 2
      fi
    done
  done
fi

if (( ${#POINT_FAILURES[@]} )); then
  if (( CAMPAIGN_ABORTED )); then
    echo "[sweep] ${#POINT_FAILURES[@]} independent point(s) failed before shared serving state became unhealthy:" >&2
  else
    echo "[sweep] ${#POINT_FAILURES[@]} independent point(s) failed; completed remaining safe points:" >&2
  fi
  printf '  - %s\n' "${POINT_FAILURES[@]}" >&2
  echo "[sweep] results are partial; rerun with --skip-done to fill the missing points" >&2
  exit 1
fi

echo "[sweep] done, see $CAMPAIGN_RESULTS_DIR/index.csv"
