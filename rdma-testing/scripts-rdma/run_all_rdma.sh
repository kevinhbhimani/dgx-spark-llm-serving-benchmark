#!/usr/bin/env bash
# =============================================================================
# scripts-rdma/run_all_rdma.sh
#
# MINIMALLY MODIFIED COPY of scripts/run_all.sh
# Changes from original:
#   - Sources connectx_env_rdma.sh instead of connectx_env.sh
#
# Sweep a concurrency list against an already running server for one config.
# Launch the server with scripts-rdma/launch_server_rdma.sh CONFIG first.
# =============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/connectx_env_rdma.sh"
LABEL="${1-single}"
case "$LABEL" in
  single|tp2|pp2) ;;
  *)
    echo "[sweep] LABEL must be one of single, tp2, or pp2; got '$LABEL'" >&2
    exit 1
    ;;
esac
BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
OUT_DIR="${OUT_DIR:-results}"
POWER_BACKEND="${POWER_BACKEND:-}"
CONFIG_YAML="${CONFIG_YAML:-configs/experiment.yaml}"
CONCURRENCIES="${CONCURRENCIES:-1 2 4 8 16 32 64 128}"
EXPECTED_CONCURRENCIES="${EXPECTED_CONCURRENCIES:-$CONCURRENCIES}"
REPEATS="${REPEATS:-3}"
SWEEP_SCHEDULE="${SWEEP_SCHEDULE:-}"
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
    "$PYTHON" -m bench.run_benchmark "${args[@]}"
    sleep 3
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
    run_point "$rep" "$c"
  done
else
  for rep in $(seq 1 "$REPEATS"); do
    for c in $CONCURRENCIES; do
      run_point "$rep" "$c"
    done
  done
fi

echo "[sweep] done, see $OUT_DIR/index.csv"
echo "[sweep] RDMA mode: NCCL_IB_DISABLE=$NCCL_IB_DISABLE"