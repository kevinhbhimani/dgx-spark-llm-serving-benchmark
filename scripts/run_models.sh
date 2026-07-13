#!/usr/bin/env bash
# Benchmark several models for ONE serving config, launching and tearing down a
# vLLM server per model. Reads configs/models.yaml (see MODELS_YAML).
#
#   scripts/run_models.sh single        # every model whose configs include single
#   scripts/run_models.sh tp2           # dual-node tensor parallel (Ray must be up)
#   scripts/run_models.sh pp2           # dual-node pipeline parallel (Ray must be up)
#   scripts/run_models.sh single --skip-done   # skip work already in index.csv
#
# --skip-done (or SKIP_DONE=1) consults OUT_DIR/index.csv before each model and
# only runs the concurrencies not yet recorded for that model/config at the
# current measurement window (see scripts/swept_concurrencies.py for what counts
# as done). A model with full coverage is skipped without launching its server;
# a partially swept model resumes with just the missing concurrencies. Use it to
# rerun a sweep that failed partway without repeating finished models.
#
# For tp2/pp2, start Ray on both nodes first (scripts/start_ray.sh head|worker);
# this script cycles only the `vllm serve` process between models and leaves the
# Ray cluster running. For single it launches and removes a standalone container.
#
# Each model's server stdout is teed to $OUT_DIR/server_<label>_<config>.log,
# which is passed to the benchmark so exact capacity figures (max concurrency,
# GPU blocks) are captured alongside the HTTP-derived ones. Results append to
# $OUT_DIR/index.csv tagged with the model label. Use a fresh OUT_DIR when the
# index schema has changed between versions (the benchmark refuses to append to a
# mismatched header).
#
# Env: MODELS_YAML BASE_URL OUT_DIR POWER_BACKEND CONCURRENCIES CONFIG_YAML
#      READY_TIMEOUT (seconds to wait for a model to come up, default 1800;
#      large models on tp2/pp2 can take >20 min to load weights + capture graphs)
#      SKIP_DONE (1 behaves like --skip-done, default 0)
#      PYTHON (interpreter for the benchmark and helpers, default python3).
# Per-model tokenizer defaults to the model name unless configs/models.yaml
# supplies an explicit tokenizer.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/connectx_env.sh"     # VLLM_CONTAINER, HEAD_IP, and .env

CONFIG="${1:-single}"
case "$CONFIG" in single|tp2|pp2) ;; *)
  echo "usage: run_models.sh [single|tp2|pp2] [--skip-done]" >&2; exit 1 ;;
esac

SKIP_DONE="${SKIP_DONE:-0}"
for arg in "${@:2}"; do
  case "$arg" in
    --skip-done) SKIP_DONE=1 ;;
    *) echo "usage: run_models.sh [single|tp2|pp2] [--skip-done]" >&2; exit 1 ;;
  esac
done

MODELS_YAML="${MODELS_YAML:-configs/models.yaml}"
BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
OUT_DIR="${OUT_DIR:-results}"
# Unset defers to the experiment YAML's power_backend; set it to override.
POWER_BACKEND="${POWER_BACKEND:-}"
CONFIG_YAML="${CONFIG_YAML:-configs/experiment.yaml}"
CONCURRENCIES="${CONCURRENCIES:-1 2 4 8 16 32 64 128}"
READY_TIMEOUT="${READY_TIMEOUT:-1800}"
PYTHON="${PYTHON:-python3}"

mkdir -p "$OUT_DIR"

# Select the models that request this serving config.
entries_text="$("$PYTHON" "$HERE/models_for_config.py" "$MODELS_YAML" "$CONFIG")" \
  || { status=$?; exit "$status"; }
ENTRIES=()
if [[ -n "$entries_text" ]]; then
  mapfile -t ENTRIES <<<"$entries_text"
fi
if [[ "${#ENTRIES[@]}" -eq 0 ]]; then
  echo "[models] no models request config '$CONFIG' in $MODELS_YAML" >&2
  exit 1
fi
echo "[models] $CONFIG: ${#ENTRIES[@]} model(s) from $MODELS_YAML"

LAUNCH_PID=""

server_ready() {
  # 200 from /v1/models AND the requested model id is being served
  local name="$1" body
  if ! body="$(curl -sf --max-time 5 "$BASE_URL/v1/models" 2>/dev/null)"; then
    return 1
  fi
  "$PYTHON" -c \
    'import json, sys
try:
    doc = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
want = sys.argv[1]
models = doc.get("data") or []
raise SystemExit(0 if any(m.get("id") == want for m in models) else 1)' \
    "$name" <<<"$body"
}

wait_ready() {
  local name="$1" waited=0
  echo "[models] waiting for $name (up to ${READY_TIMEOUT}s)"
  while (( waited < READY_TIMEOUT )); do
    if server_ready "$name"; then
      echo "[models] $name is serving after ${waited}s"
      return 0
    fi
    # bail early if the backgrounded launcher already died
    if [[ -n "$LAUNCH_PID" ]] && ! kill -0 "$LAUNCH_PID" 2>/dev/null; then
      echo "[models] launcher for $name exited before the server was ready" >&2
      return 1
    fi
    sleep 5; waited=$((waited + 5))
  done
  echo "[models] timed out after ${READY_TIMEOUT}s waiting for $name" >&2
  return 1
}

teardown() {
  case "$CONFIG" in
    single)
      docker rm -f "$VLLM_CONTAINER" >/dev/null 2>&1 || true ;;
    tp2|pp2)
      # stop only the serve process; keep the Ray cluster up for the next model
      docker exec "$VLLM_CONTAINER" pkill -f "vllm serve" >/dev/null 2>&1 || true ;;
  esac
  if [[ -n "$LAUNCH_PID" ]]; then
    wait "$LAUNCH_PID" 2>/dev/null || true
    LAUNCH_PID=""
  fi
}

run_one() {
  local label="$1" name="$2" maxlen="$3" gpu_util="$4" quant="$5" tok="$6" trust="$7"
  local log="$OUT_DIR/server_${label}_${CONFIG}.log"
  local tokenizer="${tok:-$name}"
  local status=0
  echo "[models] === $label ($name) config=$CONFIG ==="

  # With --skip-done, only sweep the concurrencies not already recorded in
  # index.csv for this model/config at the current measurement window. Full
  # coverage means the server is never launched.
  local run_concs="$CONCURRENCIES"
  if [[ "$SKIP_DONE" == "1" ]]; then
    local done_concs missing=() c
    done_concs="$("$PYTHON" "$HERE/swept_concurrencies.py" \
                    "$OUT_DIR/index.csv" "$label" "$CONFIG" "$CONFIG_YAML")" \
      || { echo "[models] swept_concurrencies.py failed; not skipping" >&2; done_concs=""; }
    for c in $CONCURRENCIES; do
      [[ " $done_concs " == *" $c "* ]] || missing+=("$c")
    done
    if [[ "${#missing[@]}" -eq 0 ]]; then
      echo "[models] $label: concurrencies $CONCURRENCIES already in index.csv; skipping"
      return 0
    fi
    if [[ -n "$done_concs" ]]; then
      echo "[models] $label: already done: $done_concs; running: ${missing[*]}"
    fi
    run_concs="${missing[*]}"
  fi

  local launch_env=(MODEL="$name")
  local sweep_env=(MODEL="$name" MODEL_LABEL="$label" SERVER_LOG="$log"
                   TOKENIZER="$tokenizer" TRUST_REMOTE_CODE="$trust"
                   BASE_URL="$BASE_URL" OUT_DIR="$OUT_DIR"
                   POWER_BACKEND="$POWER_BACKEND" CONFIG_YAML="$CONFIG_YAML"
                   CONCURRENCIES="$run_concs" PYTHON="$PYTHON")

  [[ -n "$maxlen" ]] && launch_env+=(MAXLEN="$maxlen")
  [[ -n "$gpu_util" ]] && launch_env+=(GPU_UTIL="$gpu_util")
  [[ -n "$quant" ]] && launch_env+=(QUANT="$quant")
  [[ -n "$tok" ]] && launch_env+=(TOKENIZER="$tok")
  [[ -n "$trust" ]] && launch_env+=(TRUST_REMOTE_CODE="$trust")

  # Launch the server in the background, teeing its log. launch_server.sh reads
  # MODEL/MAXLEN/GPU_UTIL/QUANT/TOKENIZER from the environment.
  env "${launch_env[@]}" "$HERE/launch_server.sh" "$CONFIG" >"$log" 2>&1 &
  LAUNCH_PID=$!

  if ! wait_ready "$name"; then
    echo "[models] skipping $label; see $log" >&2
    teardown
    return 1
  fi

  # Sweep this model. run_all.sh passes the model, label, and server log through
  # to the benchmark so rows are tagged and capacity figures captured.
  env "${sweep_env[@]}" "$HERE/run_all.sh" "$CONFIG" \
    || { echo "[models] sweep for $label returned an error" >&2; status=1; }

  teardown
  echo "[models] done $label"
  sleep 5
  return "$status"
}

trap 'teardown' EXIT

FAILURES=0
for entry in "${ENTRIES[@]}"; do
  IFS='|' read -r label name maxlen gpu_util quant tok trust <<<"$entry"
  if ! run_one "$label" "$name" "$maxlen" "$gpu_util" "$quant" "$tok" "$trust"; then
    FAILURES=$((FAILURES + 1))
    # A failed launcher preflight means a node is short on memory; every later
    # model would fail the same way, so stop instead of cycling through them.
    if grep -q "PREFLIGHT FAILED" "$OUT_DIR/server_${label}_${CONFIG}.log" 2>/dev/null; then
      echo "[models] cluster memory preflight failed for $label; aborting the" \
           "sweep. Free memory on the low node (see the log), then rerun" \
           "with --skip-done." >&2
      exit 1
    fi
    echo "[models] $label did not complete; continuing" >&2
  fi
done

if (( FAILURES > 0 )); then
  echo "[models] $FAILURES model(s) failed for $CONFIG; see logs in $OUT_DIR" >&2
  exit 1
fi

echo "[models] all $CONFIG models done, see $OUT_DIR/index.csv"
