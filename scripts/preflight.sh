#!/usr/bin/env bash
# Run the full readiness sequence on the configured head. JIT mode removes model
# snapshots from both HF caches when the sequence finishes; disk mode retains them.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"
REQUESTED_OUT_DIR="${OUT_DIR:-}"
source "$HERE/scripts/connectx_env.sh" || exit 1
export MODELS_YAML="${MODELS_YAML:-configs/models.yaml}"
if [[ "${MODELS_ONDISK:-0}" == "1" ]] \
    && ! docker image inspect --format '{{.Id}}' "$VLLM_IMAGE" >/dev/null 2>&1; then
  echo "[preflight] MODELS_ONDISK=1 requires local image $VLLM_IMAGE; refusing to pull it" >&2
  exit 1
fi
if [[ "$SINGLE_NODE_ONLY" == "1" && "${PURGE_WORKER:-0}" == "1" ]]; then
  echo "[preflight] PURGE_WORKER=1 is invalid with SINGLE_NODE_ONLY=1" >&2
  exit 2
fi
validate_execution_environment "${HEAD_IP:-}" "preflight role" || exit 1
acquire_resource_lock || exit 1

if [[ -z "${PYTHON:-}" ]]; then
  if [[ -x "$HERE/.venv/bin/python" ]]; then
    PYTHON="$HERE/.venv/bin/python"
  else
    PYTHON=python3
  fi
fi
export PYTHON PYTHONNOUSERSITE=1
if [[ -n "$REQUESTED_OUT_DIR" ]]; then
  OUT_DIR="$("$PYTHON" "$HERE/utils/campaign_output.py" resolve \
    --out-dir "$REQUESTED_OUT_DIR")" || exit 1
else
  OUT_DIR="$("$PYTHON" "$HERE/utils/campaign_output.py" resolve \
    --next-available)" || exit 1
fi
export OUT_DIR
CAMPAIGN_RESULTS_DIR="$("$PYTHON" "$HERE/utils/campaign_output.py" \
  results-dir --out-dir "$OUT_DIR")" || exit 1
CAMPAIGN_LOGS_DIR="$("$PYTHON" "$HERE/utils/campaign_output.py" \
  logs-dir --out-dir "$OUT_DIR")" || exit 1
export CAMPAIGN_RESULTS_DIR CAMPAIGN_LOGS_DIR

if ! ALL_CONFIGURED_MODEL_LABELS="$("$PYTHON" - "$MODELS_YAML" <<'PY'
import re
import sys

import yaml

with open(sys.argv[1], encoding="utf-8") as handle:
    document = yaml.safe_load(handle) or {}
if not isinstance(document, dict):
    raise SystemExit(f"[preflight] {sys.argv[1]} must contain a mapping")
models = document.get("models")
if not isinstance(models, list) or not models \
        or any(not isinstance(model, dict) for model in models):
    raise SystemExit(f"[preflight] {sys.argv[1]} must list model mappings")
labels = [model.get("label") for model in models]
if not labels or any(not isinstance(label, str) for label in labels):
    raise SystemExit(f"[preflight] {sys.argv[1]} has missing model labels")
if len(labels) != len(set(labels)):
    raise SystemExit(f"[preflight] {sys.argv[1]} has duplicate model labels")
if any(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", label) is None
       for label in labels):
    raise SystemExit(f"[preflight] {sys.argv[1]} has an unsafe model label")
print(" ".join(labels))
PY
)"; then
  echo "[preflight] cannot identify every configured model for final cache cleanup" >&2
  exit 2
fi

RUN_ID="$(date -u +%Y%m%d-%H%M%S)-$$"
PREFLIGHT_RUN_DIR="$OUT_DIR/preflight/$RUN_ID"
PREFLIGHT_LOG_DIR="$PREFLIGHT_RUN_DIR/logs"
SMOKE_OUT_DIR="$PREFLIGHT_RUN_DIR/hardware-smoke"
CAMPAIGN_PREFLIGHT_OUT_DIR="$PREFLIGHT_RUN_DIR/campaign"
if ! mkdir -p "$PREFLIGHT_LOG_DIR"; then
  echo "[preflight] cannot create log directory $PREFLIGHT_LOG_DIR" >&2
  exit 1
fi
echo "[preflight] campaign output: $OUT_DIR"
echo "[preflight] this run's artifacts: $PREFLIGHT_RUN_DIR"

PREFLIGHT_FAILED=0
LAST_STEP_STATUS=0
declare -a PREFLIGHT_RESULTS=()

run_step() {
  local step_name="$1" log_name="$2" log_path status
  shift 2
  log_path="$PREFLIGHT_LOG_DIR/$log_name.log"

  echo
  echo "[preflight] === $step_name ==="
  if "$@" 2>&1 | tee "$log_path"; then
    status=0
  else
    status=$?
  fi
  LAST_STEP_STATUS="$status"

  if (( status == 0 )); then
    PREFLIGHT_RESULTS+=("PASS: $step_name")
    echo "[preflight] PASS: $step_name"
  else
    PREFLIGHT_FAILED=1
    PREFLIGHT_RESULTS+=("FAIL: $step_name (exit $status; $log_path)")
    echo "[preflight] FAIL: $step_name (exit $status)"
    echo "[preflight] details: $log_path"
  fi
}

capture_preflight_system_snapshot() {
  local snapshot_path="$PREFLIGHT_LOG_DIR/system_snapshot.json"
  local -a snapshot_args=(--output "$snapshot_path" --container "$VLLM_CONTAINER" --image "$VLLM_IMAGE")
  if [[ "$SINGLE_NODE_ONLY" == "1" ]]; then
    snapshot_args+=(--local-only)
  else
    snapshot_args+=(--cluster-config "$CLUSTER_CONFIG")
  fi
  echo "[preflight] === Capture read-only system snapshot ==="
  if "$PYTHON" "$HERE/scripts/system_snapshot.py" "${snapshot_args[@]}" 2>&1 \
      | tee "$PREFLIGHT_LOG_DIR/system-snapshot.log"; then
    echo "[preflight] system snapshot collection completed"
  else
    echo "[preflight] WARNING: system snapshot could not be saved; continuing"
  fi
}

verify_campaign_output_writes() {
  local directory probe_file expected_content written_content
  expected_content="preflight-write-check"

  for directory in "$CAMPAIGN_RESULTS_DIR" "$CAMPAIGN_LOGS_DIR"; do
    if ! mkdir -p "$directory"; then
      echo "[preflight] cannot create campaign output directory $directory" >&2
      return 1
    fi
    if ! probe_file="$(mktemp "$directory/.preflight-write-check.XXXXXX")"; then
      echo "[preflight] cannot create a write-check file in $directory" >&2
      return 1
    fi
    if ! printf '%s\n' "$expected_content" >"$probe_file"; then
      rm -f "$probe_file"
      echo "[preflight] cannot write a check file in $directory" >&2
      return 1
    fi
    if ! IFS= read -r written_content <"$probe_file" \
        || [[ "$written_content" != "$expected_content" ]]; then
      rm -f "$probe_file"
      echo "[preflight] write-check contents did not match in $directory" >&2
      return 1
    fi
    if ! rm "$probe_file"; then
      echo "[preflight] cannot remove the write-check file $probe_file" >&2
      return 1
    fi
    echo "[preflight] campaign output directory is writable: $directory"
  done
}

purge_all_campaign_models() {
  if [[ "${MODELS_ONDISK:-0}" == "1" ]]; then
    echo "[preflight] MODELS_ONDISK=1; retaining configured model snapshots"
    return 0
  fi

  local both_nodes_status=0
  if [[ "$SINGLE_NODE_ONLY" == "1" ]]; then
    MODEL_LABELS="$ALL_CONFIGURED_MODEL_LABELS" PURGE_WORKER=0 \
      PURGE_CACHE_GARBAGE=1 scripts/purge_model_cache.sh
    return $?
  fi
  if MODEL_LABELS="$ALL_CONFIGURED_MODEL_LABELS" PURGE_WORKER=1 \
      PURGE_CACHE_GARBAGE=1 scripts/purge_model_cache.sh; then
    return 0
  else
    both_nodes_status=$?
  fi

  echo "[preflight] two-node purge failed (exit $both_nodes_status); retrying the head cache"
  if MODEL_LABELS="$ALL_CONFIGURED_MODEL_LABELS" PURGE_WORKER=0 \
      PURGE_CACHE_GARBAGE=1 scripts/purge_model_cache.sh; then
    echo "[preflight] configured head snapshots were purged"
  else
    echo "[preflight] head cache purge also failed" >&2
  fi
  echo "[preflight] worker cache cleanup could not be confirmed" >&2
  return 1
}

finish_preflight() {
  local original_status="$?"
  trap - EXIT INT TERM

  local cache_cleanup_label="Purge all configured model snapshots on head and worker"
  if [[ "${MODELS_ONDISK:-0}" == "1" ]]; then
    cache_cleanup_label="Retain configured model snapshots"
  elif [[ "$SINGLE_NODE_ONLY" == "1" ]]; then
    cache_cleanup_label="Purge all configured model snapshots on local host"
  fi
  run_step "$cache_cleanup_label" \
    "purge-models" purge_all_campaign_models
  if (( LAST_STEP_STATUS != 0 )); then
    PREFLIGHT_FAILED=1
  fi

  echo
  echo "[preflight] === Summary ==="
  printf '  %s\n' "${PREFLIGHT_RESULTS[@]}"
  if (( PREFLIGHT_FAILED == 0 && original_status == 0 )); then
    if [[ "${MODELS_ONDISK:-0}" == "1" ]]; then
      echo "[preflight] ALL CHECKS PASSED; configured model weights were retained in the cache"
    elif [[ "$SINGLE_NODE_ONLY" == "1" ]]; then
      echo "[preflight] ALL LOCAL CHECKS PASSED; configured model weights were purged from the local cache"
    else
      echo "[preflight] ALL CHECKS PASSED; configured model weights were purged from both nodes"
    fi
    exit 0
  fi
  echo "[preflight] FAILED; review the failed stage logs above"
  exit 1
}

trap 'exit 130' INT
trap 'exit 143' TERM
trap finish_preflight EXIT

capture_preflight_system_snapshot

run_step "Verify campaign results and logs are writable" "output-writes" \
  verify_campaign_output_writes

run_step "Validate campaign plan and client environment" "plan" \
  env OUT_DIR="$CAMPAIGN_PREFLIGHT_OUT_DIR" PLAN_ONLY=1 \
  CAMPAIGN_LABELS= CAMPAIGN_CONFIGS= \
  "$HERE/scripts/run_job.sh"

if [[ "$SINGLE_NODE_ONLY" == "1" ]]; then
  echo "[preflight] SKIP: ConnectX routes, links, and RDMA checks disabled (SINGLE_NODE_ONLY=1)"
  PREFLIGHT_RESULTS+=("SKIP: ConnectX routes, links, and RDMA checks disabled (SINGLE_NODE_ONLY=1)")
  PREFLIGHT_RESULTS+=("SKIP: Ray health and per-node runtime checks disabled (SINGLE_NODE_ONLY=1)")
  PREFLIGHT_RESULTS+=("SKIP: all-reduce gate disabled (SINGLE_NODE_ONLY=1)")
  PREFLIGHT_RESULTS+=("SKIP: send/recv benchmark disabled (SINGLE_NODE_ONLY=1)")
  PREFLIGHT_RESULTS+=("SKIP: worker cache mirroring disabled (SINGLE_NODE_ONLY=1)")
else
  run_step "Verify ConnectX routes, links, and RDMA ports" "connectx" \
    "$HERE/scripts/verify_connectx.sh"
fi

run_step "Run single-node serving and measurement smoke" "hardware-smoke" \
  env OUT_DIR="$SMOKE_OUT_DIR" SMOKE_GPU_UTIL="${GPU_UTIL:-0.80}" \
  KEEP_WEIGHTS="${MODELS_ONDISK:-0}" \
  "$HERE/scripts/hardware_smoke.sh"

run_step "Run campaign gates, including all-reduce, and forced canary" \
  "campaign-canary" \
  env OUT_DIR="$CAMPAIGN_PREFLIGHT_OUT_DIR" PLAN_ONLY=0 CANARY_FIRST=1 \
  STOP_AFTER_CANARY=1 \
  ALLOW_CANARY_FAILURES=0 PURGE_WEIGHTS=1 \
  CAMPAIGN_LABELS= CAMPAIGN_CONFIGS= "$HERE/scripts/run_job.sh"

exit 0
