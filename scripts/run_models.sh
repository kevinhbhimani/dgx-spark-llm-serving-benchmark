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
# runs only missing (repeat, concurrency) pairs with matching methodology,
# config, harness, client-runtime, serving-policy, and live-deployment
# fingerprints. A fully covered model is skipped without launching its server.
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
#      REPEATS (fixed-workload repeated windows for spread, default 3)
#      READY_TIMEOUT (seconds to wait for a model to come up, default 1800;
#      large models on tp2/pp2 can take >20 min to load weights + capture graphs)
#      PROBE_TIMEOUT (seconds for the strict two-token streaming canary, default 600)
#      SKIP_DONE (1 behaves like --skip-done, default 0)
#      CANARY_ONLY=1 launches each selected model, validates the exact streaming
#      contract, and tears it down without starting the long measurement sweep.
#      CACHE_PREFLIGHT=1 (default) requires every selected pinned snapshot to be
#      locally complete before launching the first server.
#      MODEL_LABELS (optional space-separated label allowlist for targeted
#      canaries or reruns; default is every entry for the serving config).
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

require_local_connectx_ip "$HEAD_IP" "model benchmark head role" || exit 1
acquire_campaign_lock || exit 1

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
EXPECTED_CONCURRENCIES="${EXPECTED_CONCURRENCIES:-$CONCURRENCIES}"
REPEATS="${REPEATS:-3}"
READY_TIMEOUT="${READY_TIMEOUT:-1800}"
PROBE_TIMEOUT="${PROBE_TIMEOUT:-600}"
CACHE_PREFLIGHT="${CACHE_PREFLIGHT:-1}"
CANARY_ONLY="${CANARY_ONLY:-0}"
MODEL_LABELS="${MODEL_LABELS:-}"
PYTHON="${PYTHON:-python3}"

for flag_name in SKIP_DONE CACHE_PREFLIGHT CANARY_ONLY; do
  flag_value="${!flag_name}"
  if [[ "$flag_value" != "0" && "$flag_value" != "1" ]]; then
    echo "[models] $flag_name must be 0 or 1, got '$flag_value'" >&2
    exit 1
  fi
done
# A canary is a real launch/contract check, never a resume query. An inherited
# SKIP_DONE=1 must not turn it into a no-op.
if [[ "$CANARY_ONLY" == "1" ]]; then
  SKIP_DONE=0
fi
if [[ ! "$REPEATS" =~ ^[1-9][0-9]*$ ]]; then
  echo "[models] REPEATS must be a positive integer, got '$REPEATS'" >&2
  exit 1
fi
for timeout_name in READY_TIMEOUT PROBE_TIMEOUT; do
  timeout_value="${!timeout_name}"
  if [[ ! "$timeout_value" =~ ^[1-9][0-9]*$ ]]; then
    echo "[models] $timeout_name must be a positive integer, got '$timeout_value'" >&2
    exit 1
  fi
done
declare -A EXPECTED_SET=()
for c in $EXPECTED_CONCURRENCIES; do
  if [[ ! "$c" =~ ^[1-9][0-9]*$ || -n "${EXPECTED_SET[$c]:-}" ]]; then
    echo "[models] invalid or duplicate expected concurrency '$c'" >&2
    exit 1
  fi
  EXPECTED_SET[$c]=1
done
if [[ "${#EXPECTED_SET[@]}" -eq 0 ]]; then
  echo "[models] EXPECTED_CONCURRENCIES must not be empty" >&2
  exit 1
fi
declare -A REQUESTED_SET=()
for c in $CONCURRENCIES; do
  if [[ ! "$c" =~ ^[1-9][0-9]*$ || -n "${REQUESTED_SET[$c]:-}" ]]; then
    echo "[models] invalid or duplicate requested concurrency '$c'" >&2
    exit 1
  fi
  if [[ -z "${EXPECTED_SET[$c]:-}" ]]; then
    echo "[models] requested concurrency $c is outside EXPECTED_CONCURRENCIES" >&2
    exit 1
  fi
  REQUESTED_SET[$c]=1
done
if [[ "${#REQUESTED_SET[@]}" -eq 0 ]]; then
  echo "[models] CONCURRENCIES must not be empty" >&2
  exit 1
fi

if [[ ! "$VLLM_IMAGE" =~ @sha256:[0-9a-fA-F]{64}$ ]]; then
  echo "[models] VLLM_IMAGE must be pinned by @sha256 for a resumable sweep: $VLLM_IMAGE" >&2
  exit 1
fi

EFFECTIVE_POWER_BACKEND="$("$PYTHON" - "$CONFIG_YAML" "$POWER_BACKEND" <<'PY'
import sys
from bench.config import ExperimentConfig

cfg = ExperimentConfig.from_yaml(sys.argv[1])
if sys.argv[2]:
    cfg.power_backend = sys.argv[2]
if cfg.require_power_data and cfg.power_backend == "none":
    raise SystemExit("[models] full config requires power data; backend cannot be none")
if cfg.require_vllm_metrics_data and not cfg.scrape_vllm_metrics:
    raise SystemExit("[models] full config requires vLLM metrics scraping")
print(cfg.power_backend)
PY
)"

mkdir -p "$OUT_DIR"

# Refuse an incompatible index before loading a model for many minutes. The
# benchmark repeats this check per point, but orchestration must fail before GPU
# work begins.
"$PYTHON" -c \
  'import sys; from bench.run_benchmark import validate_index_schema; validate_index_schema(sys.argv[1])' \
  "$OUT_DIR"

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
if [[ -n "$MODEL_LABELS" ]]; then
  FILTERED=()
  for wanted in $MODEL_LABELS; do
    found=0
    for entry in "${ENTRIES[@]}"; do
      if [[ "${entry%%|*}" == "$wanted" ]]; then
        FILTERED+=("$entry")
        found=1
        break
      fi
    done
    if [[ "$found" != "1" ]]; then
      echo "[models] MODEL_LABELS requested unknown/inactive label '$wanted' for $CONFIG" >&2
      exit 1
    fi
  done
  ENTRIES=("${FILTERED[@]}")
fi
echo "[models] $CONFIG: ${#ENTRIES[@]} model(s) from $MODELS_YAML"

# Cache misses are a deterministic launch failure because serving is offline.
# Validate the complete selected matrix once, before spending time loading a GPU.
if [[ "$CACHE_PREFLIGHT" == "1" ]]; then
  CHECK_ONLY=1 MODEL_LABELS="$MODEL_LABELS" MODELS_YAML="$MODELS_YAML" \
    PYTHON="$PYTHON" "$HERE/predownload.sh" "$CONFIG"
fi

# Dual runs execute model code on both containers. Verify and hash the actual
# per-node Python/Ray/vLLM/torch/CUDA/driver/GPU/power-limit environment rather
# than assuming Hoedus sourced the same .env as Diya. The hash becomes part of
# every dual serving fingerprint, so a dependency change cannot be resumed into
# an older curve.
CLUSTER_RUNTIME_FINGERPRINT="standalone"
if [[ "$CONFIG" != "single" ]]; then
  if ! cluster_runtime_json="$(docker exec -i "$VLLM_CONTAINER" \
      python3 - "$HEAD_IP" "$HOEDUS_IP" "$VLLM_IMAGE" "$RAY_VERSION" \
      < "$HERE/cluster_runtime_fingerprint.py")"; then
    echo "[models] dual-node runtime identity preflight failed" >&2
    exit 1
  fi
  CLUSTER_RUNTIME_FINGERPRINT="$(printf '%s' "$cluster_runtime_json" \
    | sha256sum | awk '{print $1}')"
  echo "[models] cluster runtime fingerprint: $CLUSTER_RUNTIME_FINGERPRINT"
  echo "[models] cluster runtime facts: $cluster_runtime_json"
fi

LAUNCH_PID=""
ACTIVE_CHILD=""

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

server_contract() {
  # Exercise the exact response fields used by bench/client.py. Merely seeing
  # /v1/models is insufficient: a stale server or incompatible streaming build
  # can otherwise invalidate an entire multi-day sweep.
  "$PYTHON" - "$BASE_URL" "$1" "$PROBE_TIMEOUT" <<'PY'
import json
import sys
import urllib.request

base_url, model, timeout = sys.argv[1], sys.argv[2], float(sys.argv[3])
payload = {
    "model": model,
    "prompt": [1],
    "max_tokens": 2,
    "min_tokens": 2,
    "ignore_eos": True,
    "temperature": 0.0,
    "stream": True,
    "return_token_ids": True,
    "stream_options": {"include_usage": True},
}
request = urllib.request.Request(
    base_url.rstrip("/") + "/v1/completions",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
count = 0
saw_ids = False
usage = None
finish = ""
done = False
try:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                done = True
                break
            chunk = json.loads(data)
            if chunk.get("error"):
                raise RuntimeError(f"stream error: {chunk['error']}")
            choices = chunk.get("choices") or []
            choice = choices[0] if choices else {}
            token_ids = choice.get("token_ids")
            if token_ids is None:
                token_ids = (choice.get("delta") or {}).get("token_ids")
            if token_ids is None:
                token_ids = chunk.get("token_ids")
            if isinstance(token_ids, list):
                saw_ids = True
                count += len(token_ids)
            if choice.get("finish_reason") is not None:
                finish = str(choice["finish_reason"])
            if (chunk.get("usage") or {}).get("completion_tokens") is not None:
                usage = int(chunk["usage"]["completion_tokens"])
except Exception as exc:
    print(f"[models] strict streaming probe failed: {type(exc).__name__}: {exc}",
          file=sys.stderr)
    raise SystemExit(1)

problems = []
if not done: problems.append("missing [DONE]")
if not saw_ids: problems.append("missing delta token_ids")
if count != 2: problems.append(f"received {count} token ids, expected 2")
if usage != 2: problems.append(f"final usage={usage!r}, expected 2")
if finish != "length": problems.append(f"finish_reason={finish!r}, expected 'length'")
if problems:
    print("[models] strict streaming probe failed: " + "; ".join(problems),
          file=sys.stderr)
    raise SystemExit(1)
print("[models] strict stream contract OK: exact token_ids, usage, length, [DONE]")
PY
}

wait_ready() {
  local name="$1" waited=0
  echo "[models] waiting for $name (up to ${READY_TIMEOUT}s)"
  while (( waited < READY_TIMEOUT )); do
    if server_ready "$name"; then
      echo "[models] $name endpoint is ready after ${waited}s; probing stream contract"
      server_contract "$name"
      return $?
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
    # A docker exec client can linger after its remote process exits. Bound the
    # cleanup so one broken launcher cannot deadlock the whole model matrix.
    local i
    for i in $(seq 1 30); do
      kill -0 "$LAUNCH_PID" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$LAUNCH_PID" 2>/dev/null; then
      kill -TERM "$LAUNCH_PID" 2>/dev/null || true
      for i in $(seq 1 5); do
        kill -0 "$LAUNCH_PID" 2>/dev/null || break
        sleep 1
      done
    fi
    kill -KILL "$LAUNCH_PID" 2>/dev/null || true
    wait "$LAUNCH_PID" 2>/dev/null || true
    LAUNCH_PID=""
  fi
}
handle_signal() {
  local status="$1" i
  trap - INT TERM
  if [[ -n "$ACTIVE_CHILD" ]] && kill -0 "$ACTIVE_CHILD" 2>/dev/null; then
    kill -TERM "$ACTIVE_CHILD" 2>/dev/null || true
    for i in $(seq 1 30); do
      kill -0 "$ACTIVE_CHILD" 2>/dev/null || break
      sleep 1
    done
    kill -KILL "$ACTIVE_CHILD" 2>/dev/null || true
    wait "$ACTIVE_CHILD" 2>/dev/null || true
    ACTIVE_CHILD=""
  fi
  teardown
  exit "$status"
}

wait_endpoint_down() {
  local i
  for i in $(seq 1 30); do
    if ! curl -sf --max-time 1 "$BASE_URL/v1/models" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "[models] endpoint $BASE_URL is still live after teardown; refusing a stale-server launch" >&2
  return 1
}

serving_fingerprint() {
  local name="$1" revision="$2" tokenizer="$3" tokrev="$4"
  local maxlen="$5" gpu_util="$6" quant="$7" trust="$8"
  {
    printf 'schema=serving-v1\n'
    printf 'config=%s\n' "$CONFIG"
    printf 'image=%s\n' "$VLLM_IMAGE"
    printf 'model=%s\nrevision=%s\n' "$name" "$revision"
    printf 'tokenizer=%s\ntokenizer_revision=%s\n' "$tokenizer" "$tokrev"
    printf 'dtype=%s\nquant=%s\n' "${DTYPE:-auto}" "$quant"
    printf 'max_model_len=%s\ngpu_util=%s\n' "$maxlen" "$gpu_util"
    printf 'prefix_caching=%s\ngeneration_config=%s\n' \
      "${PREFIX_CACHING:-0}" "${GENERATION_CONFIG:-vllm}"
    printf 'torchinductor_autograd_cache=%s\nhf_hub_offline=%s\n' \
      "${TORCHINDUCTOR_AUTOGRAD_CACHE:-0}" "${HF_HUB_OFFLINE:-1}"
    printf 'trust_remote_code=%s\nray_version=%s\n' "$trust" "$RAY_VERSION"
    printf 'connectx_iface=%s\nnccl_ib_disable=%s\n' "$CX_IFACE" "$NCCL_IB_DISABLE"
    printf 'head_ip=%s\nworker_ip=%s\npower_backend=%s\n' \
      "$HEAD_IP" "$HOEDUS_IP" "$EFFECTIVE_POWER_BACKEND"
    printf 'cluster_runtime=%s\n' "$CLUSTER_RUNTIME_FINGERPRINT"
  } | sha256sum | awk '{print $1}'
}

run_one() {
  local label="$1" name="$2" revision="$3" maxlen="$4" gpu_util="$5"
  local quant="$6" tok="$7" tokrev="$8" trust="$9"
  local log="$OUT_DIR/server_${label}_${CONFIG}.log"
  local tokenizer="${tok:-$name}"
  local tokenizer_revision="$tokrev"
  local actual_maxlen="${maxlen:-8704}"
  local actual_gpu_util="${gpu_util:-0.80}"
  local fingerprint deployment="" schedule=""
  local status=0
  echo "[models] === $label ($name) config=$CONFIG ==="

  if [[ -z "$revision" ]]; then
    echo "[models] $label has no pinned model revision" >&2
    return 1
  fi
  if [[ -z "$tokenizer_revision" && "$tokenizer" == "$name" ]]; then
    tokenizer_revision="$revision"
  elif [[ -z "$tokenizer_revision" && -n "$tok" ]]; then
    echo "[models] $label uses tokenizer $tok without a pinned tokenizer_revision" >&2
    return 1
  fi
  fingerprint="$(serving_fingerprint "$name" "$revision" "$tokenizer" \
                    "$tokenizer_revision" "$actual_maxlen" "$actual_gpu_util" \
                    "$quant" "$trust")"
  echo "[models] serving fingerprint: $fingerprint"

  # Resume at exact (repeat, concurrency) granularity. Only rows produced by
  # the current methodology/config/source/server identity count as complete.
  if [[ "$SKIP_DONE" == "1" ]]; then
    if ! schedule="$(POWER_BACKEND="$POWER_BACKEND" \
                       EXPECTED_CONCURRENCIES="$EXPECTED_CONCURRENCIES" \
                       DEPLOYMENT_FINGERPRINT= \
                       "$PYTHON" "$HERE/swept_concurrencies.py" \
                          "$OUT_DIR/index.csv" "$label" "$CONFIG" "$CONFIG_YAML" \
                          "$fingerprint" "$REPEATS" $CONCURRENCIES)"; then
      echo "[models] resume validation failed; refusing to guess what is complete" >&2
      return 1
    fi
    if [[ -z "$schedule" ]]; then
      echo "[models] $label: all $REPEATS repeat(s) of $CONCURRENCIES are complete; skipping"
      return 0
    fi
    echo "[models] $label missing repeat:concurrency pairs: $schedule"
  fi

  # Explicit empty values prevent unrelated .env values from leaking from one
  # model entry into another.
  local launch_env=(MODEL="$name" REVISION="$revision"
                    MAXLEN="$actual_maxlen" GPU_UTIL="$actual_gpu_util"
                    QUANT="$quant" TOKENIZER="$tok"
                    TOKENIZER_REVISION="$tokenizer_revision"
                    TRUST_REMOTE_CODE="$trust" DTYPE="${DTYPE:-auto}"
                    PREFIX_CACHING="${PREFIX_CACHING:-0}"
                    GENERATION_CONFIG="${GENERATION_CONFIG:-vllm}"
                    TORCHINDUCTOR_AUTOGRAD_CACHE="${TORCHINDUCTOR_AUTOGRAD_CACHE:-0}"
                    HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}")
  local sweep_env=(MODEL="$name" MODEL_LABEL="$label" SERVER_LOG="$log"
                   MODEL_REVISION="$revision"
                   TOKENIZER="$tokenizer" TRUST_REMOTE_CODE="$trust"
                   TOKENIZER_REVISION="$tokenizer_revision"
                   SERVING_FINGERPRINT="$fingerprint"
                   DEPLOYMENT_FINGERPRINT=""
                   BASE_URL="$BASE_URL" OUT_DIR="$OUT_DIR"
                   POWER_BACKEND="$POWER_BACKEND" CONFIG_YAML="$CONFIG_YAML"
                   CONCURRENCIES="$CONCURRENCIES"
                   EXPECTED_CONCURRENCIES="$EXPECTED_CONCURRENCIES"
                   REPEATS="$REPEATS" SWEEP_SCHEDULE="$schedule"
                   HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
                   HF_HUB_CACHE="$HF_CACHE/hub"
                   HF_MODULES_CACHE="${HF_MODULES_CACHE:-$HOME/.cache/huggingface-client/modules}"
                   PYTHON="$PYTHON")

  teardown
  wait_endpoint_down || return 1

  # Launch the server in the background, teeing its log. launch_server.sh reads
  # MODEL/MAXLEN/GPU_UTIL/QUANT/TOKENIZER from the environment.
  env "${launch_env[@]}" "$HERE/launch_server.sh" "$CONFIG" >"$log" 2>&1 &
  LAUNCH_PID=$!

  if ! wait_ready "$name"; then
    echo "[models] skipping $label; see $log" >&2
    teardown
    return 1
  fi

  # Hash the runtime/capacity actually observed after startup. This prevents a
  # crash-resume from filling one curve with points from a subtly different
  # server deployment (different vLLM build, KV capacity, block size, etc.).
  if ! deployment="$("$PYTHON" - "$BASE_URL" "$name" "$log" "$fingerprint" <<'PY'
import sys
from bench.run_benchmark import collect_deployment_info, deployment_fingerprint

base_url, model, log, serving = sys.argv[1:5]
info = collect_deployment_info(
    base_url, model, log, compute_from_hf=False,
)
print(deployment_fingerprint(serving, info))
PY
)"; then
    echo "[models] failed to fingerprint the live deployment for $label" >&2
    teardown
    return 1
  fi
  echo "[models] deployment fingerprint: $deployment"

  if [[ "$SKIP_DONE" == "1" ]]; then
    if ! schedule="$(POWER_BACKEND="$POWER_BACKEND" \
                       EXPECTED_CONCURRENCIES="$EXPECTED_CONCURRENCIES" \
                       DEPLOYMENT_FINGERPRINT="$deployment" \
                       "$PYTHON" "$HERE/swept_concurrencies.py" \
                          "$OUT_DIR/index.csv" "$label" "$CONFIG" "$CONFIG_YAML" \
                          "$fingerprint" "$REPEATS" $CONCURRENCIES)"; then
      echo "[models] live deployment resume validation failed" >&2
      teardown
      return 1
    fi
    if [[ -z "$schedule" ]]; then
      echo "[models] $label became complete before measurement; skipping"
      teardown
      wait_endpoint_down || return 1
      return 0
    fi
    echo "[models] live deployment missing repeat:concurrency pairs: $schedule"
  fi
  sweep_env+=(DEPLOYMENT_FINGERPRINT="$deployment" SWEEP_SCHEDULE="$schedule")

  if [[ "$CANARY_ONLY" == "1" ]]; then
    echo "[models] $label canary passed; measurement intentionally skipped"
    teardown
    wait_endpoint_down || return 1
    return 0
  fi

  # Sweep this model. run_all.sh passes the model, label, and server log through
  # to the benchmark so rows are tagged and capacity figures captured.
  env "${sweep_env[@]}" "$HERE/run_all.sh" "$CONFIG" &
  ACTIVE_CHILD=$!
  if ! wait "$ACTIVE_CHILD"; then
    echo "[models] sweep for $label returned an error" >&2
    status=1
  fi
  ACTIVE_CHILD=""

  teardown
  echo "[models] done $label"
  sleep 5
  return "$status"
}

trap 'teardown' EXIT
trap 'handle_signal 130' INT
trap 'handle_signal 143' TERM

FAILURES=0
for entry in "${ENTRIES[@]}"; do
  IFS='|' read -r label name revision maxlen gpu_util quant tok tokrev trust <<<"$entry"
  if ! run_one "$label" "$name" "$revision" "$maxlen" "$gpu_util" \
               "$quant" "$tok" "$tokrev" "$trust"; then
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

if [[ "$CANARY_ONLY" == "1" ]]; then
  echo "[models] all $CONFIG strict server canaries passed"
else
  echo "[models] all $CONFIG models done, see $OUT_DIR/index.csv"
fi
