#!/usr/bin/env bash
# Unattended RDMA campaign (methodology steady-exact-v4). Run inside tmux on the
# configured head after starting Ray on both nodes:
#
#   configured head:   scripts/start_ray.sh head
#   configured worker: scripts/start_ray.sh worker
#   configured head:   scripts/run_job.sh
#
# scripts/connectx_env.sh loads the topology and enables NCCL transport logging.
# An unset OUT_DIR uses results/benchmark-<UTC-date>-001 or the next sequence.
# A bare OUT_DIR names the prefix before benchmark, date, and sequence number.
# A path-style OUT_DIR is the complete campaign path for resuming a campaign.
#
# Model lifecycle. The matrix never has to fit on disk at once. Each model in
# configs/models.yaml (file order; entries sharing a repo form one group) goes
# through:
#   fetch    pinned snapshot into the head cache (skipped when already complete)
#   mirror   that snapshot to the worker cache (only if it runs tp2/pp2)
#   measure  REPEATS replicates of every config it lists
#   purge    the snapshot from both caches (PURGE_WEIGHTS=1, the default)
# so peak per-node cache use is the largest single snapshot (~163 GB).
# MODELS_ONDISK=1 validates pinned snapshots in HF_CACHE without Hub access
# and retains weights during canary and measurement cleanup.
#
# Replicates. A replicate is one fresh server launch plus a complete ascending
# concurrency sweep, so the spread across replicates includes launch-to-launch
# variance: KV capacity after memory profiling, allocator and compile state,
# and cluster setup. Every single-vs-tp2-vs-pp2 margin is a comparison across
# launches and has to be judged against exactly that variance. Within a model,
# replicate r runs the configs in a rotated order (r1: single tp2 pp2,
# r2: tp2 pp2 single, r3: pp2 single tp2), so every config takes every slot
# once and slow drift cannot favour one of them. Single-node launches exec into
# the running Ray head container (SINGLE_CONTAINER=ray), so the cluster survives
# them; the old "single last" ordering is gone.
#
# Phases:
#   1. prep (abort on failure): clean committed source, locked client env,
#      campaign plan, Ray health, idle GPUs, two-node runtime identity, Ray
#      all-reduce gate; the send/recv microbench is recorded too (non-fatal)
#   2. canary pass: every model is fetched, launched once under every config
#      with the strict stream contract, and purged. Failures are collected and
#      the job stops before any measurement if one failed. CANARY_FIRST=auto
#      (default) skips the pass once it has succeeded for this exact commit,
#      matrix, image, and network policy.
#   3. measurement pass: the lifecycle above, model by model
#   4. finalization: verify, plots and tables, replicate statistics, NCCL
#      transport audit, comparison with the TCP baseline, sealed manifest
#
# Resume. Every measurement call uses --skip-done. A model whose rectangle is
# complete is skipped without being downloaded; a partial one is fetched again
# and only its missing (replicate, concurrency) pairs run. Two campaign rules:
#   - finalize configs/experiment.yaml BEFORE the first point; it feeds
#     config_file_sha256, so a mid-campaign change requires a fresh OUT_DIR;
#   - do not edit bench/*.py or the fingerprinted scripts mid-campaign
#     (harness_source_sha256). The launch gate also requires these files to be
#     committed, so every row names a recoverable Git revision.
#
# Knobs (env): OUT_DIR (bare prefix or complete path), CAMPAIGN_NAME
#   (prefix when OUT_DIR is unset), GPU_UTIL (default 0.80, overrides the shared
#   gpu_util_default for planning, validation, and serving), CONCURRENCIES,
#   REPEATS (default 3), CANARY_FIRST
#   (auto|0|1), STOP_AFTER_CANARY, ALLOW_CANARY_FAILURES, PURGE_WEIGHTS,
#   CAMPAIGN_LABELS / CAMPAIGN_CONFIGS (space-separated subsets, e.g. for the
#   TCP transport ablation), PLAN_ONLY, MODELS_ONDISK (offline HF_CACHE snapshots).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

# Claim every campaign knob from the caller BEFORE .env is sourced.
# scripts/load_env.sh gives already-exported values precedence over .env, so
# exporting them here keeps a stale OUT_DIR/CONCURRENCIES in .env from
# silently retargeting the paper campaign at an old result directory.
if [[ -n "${OUT_DIR:-}" ]]; then
  export CAMPAIGN_OUT_DIR_IMPLICIT=0
else
  export CAMPAIGN_OUT_DIR_IMPLICIT=1
fi
export OUT_DIR="${OUT_DIR:-results}"
export CONCURRENCIES="${CONCURRENCIES-1 4 8 16 32 64}"
export EXPECTED_CONCURRENCIES="$CONCURRENCIES"
export REPEATS="${REPEATS-3}"
export CANARY_FIRST="${CANARY_FIRST-auto}"
export STOP_AFTER_CANARY="${STOP_AFTER_CANARY-0}"
export ALLOW_CANARY_FAILURES="${ALLOW_CANARY_FAILURES-0}"
export PURGE_WEIGHTS="${PURGE_WEIGHTS-1}"
export CAMPAIGN_LABELS="${CAMPAIGN_LABELS-}"
export CAMPAIGN_CONFIGS="${CAMPAIGN_CONFIGS-}"
export PLAN_ONLY="${PLAN_ONLY-0}"

source scripts/connectx_env.sh || exit 1 # mode, topology, shared environment
export MODELS_ONDISK="${MODELS_ONDISK-0}"
if [[ "$MODELS_ONDISK" != "0" && "$MODELS_ONDISK" != "1" ]]; then
  echo "[job] MODELS_ONDISK must be exactly 0 or 1, got '$MODELS_ONDISK'" >&2
  exit 2
fi
if [[ "$MODELS_ONDISK" == "1" ]]; then
  export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
fi
if [[ "$SINGLE_NODE_ONLY" == "1" && "${PURGE_WORKER:-0}" == "1" ]]; then
  echo "[job] PURGE_WORKER=1 is invalid with SINGLE_NODE_ONLY=1" >&2
  exit 2
fi

# The unattended paper campaign owns these inputs. Clear same-named values
# inherited from .env or the caller so planning, measurement, and final
# verification cannot silently target different YAML, a label subset, a
# canary-only sweep, or a power-policy override. Subsets go through the
# explicit CAMPAIGN_* knobs claimed above.
export MODELS_YAML="configs/models.yaml"
export CONFIG_YAML="configs/experiment.yaml"
export POWER_BACKEND=""
export CANARY_ONLY=0
export MODEL_LABELS=""
export STATUS_ONLY=0
export ONLY_REPEAT=""
if [[ "$SINGLE_NODE_ONLY" == "1" ]]; then
  export SINGLE_CONTAINER=standalone
  export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-0}"
else
  export SINGLE_CONTAINER=ray
fi

if [[ -z "${PYTHON:-}" ]]; then
  if [[ -x "$HERE/.venv/bin/python" ]]; then
    PYTHON="$HERE/.venv/bin/python"
  else
    PYTHON=python3
  fi
fi
export PYTHON
export PYTHONNOUSERSITE=1
if [[ "$CAMPAIGN_OUT_DIR_IMPLICIT" == "1" ]]; then
  OUT_DIR="$("$PYTHON" "$HERE/utils/campaign_output.py" resolve \
    --next-available)" || exit 1
else
  OUT_DIR="$("$PYTHON" "$HERE/utils/campaign_output.py" resolve --out-dir "$OUT_DIR")" || exit 1
fi
export OUT_DIR
CAMPAIGN_RESULTS_DIR="$("$PYTHON" "$HERE/utils/campaign_output.py" \
  results-dir --out-dir "$OUT_DIR")" || exit 1
CAMPAIGN_LOGS_DIR="$("$PYTHON" "$HERE/utils/campaign_output.py" \
  logs-dir --out-dir "$OUT_DIR")" || exit 1
CAMPAIGN_FIGURES_DIR="$OUT_DIR/figs"
export CAMPAIGN_RESULTS_DIR CAMPAIGN_LOGS_DIR CAMPAIGN_FIGURES_DIR

option_error() {
  echo "[job] invalid campaign options: $*" >&2
  exit 1
}

validate_positive_integer() {
  local name="$1" value="$2"
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || \
    option_error "$name must be a positive integer, got '$value'"
}

validate_integer_set() {
  local name="$1" raw="$2" target_name="$3" value
  local -a values=()
  local -n target="$target_name"
  local IFS=$' \t\r\n'

  # -d NUL lets read consume embedded newlines as well as ordinary spaces.
  read -r -d '' -a values < <(printf '%s\0' "$raw") || true
  (( ${#values[@]} > 0 )) || option_error "$name must not be empty"
  target=()
  for value in "${values[@]}"; do
    [[ "$value" =~ ^[1-9][0-9]*$ ]] || \
      option_error "$name contains non-positive integer '$value'"
    [[ -z "${target[$value]+x}" ]] || \
      option_error "$name contains duplicate concurrency '$value'"
    target[$value]=1
  done
}

for flag_name in STOP_AFTER_CANARY ALLOW_CANARY_FAILURES PURGE_WEIGHTS PLAN_ONLY; do
  [[ "${!flag_name}" == "0" || "${!flag_name}" == "1" ]] || \
    option_error "$flag_name must be exactly 0 or 1, got '${!flag_name}'"
done
[[ "$CANARY_FIRST" == "auto" || "$CANARY_FIRST" == "0" || "$CANARY_FIRST" == "1" ]] || \
  option_error "CANARY_FIRST must be auto, 0, or 1, got '$CANARY_FIRST'"
validate_positive_integer REPEATS "$REPEATS"
if [[ -n "${FULL_REPEATS:-}${REPEAT_CONCS:-}${REPEAT_TOTAL:-}" ]]; then
  option_error "FULL_REPEATS/REPEAT_CONCS/REPEAT_TOTAL were replaced by REPEATS" \
    "(every point gets REPEATS independent launches); remove them from the" \
    "environment or .env"
fi
for config in $CAMPAIGN_CONFIGS; do
  [[ "$config" == "single" || "$config" == "tp2" || "$config" == "pp2" ]] || \
    option_error "CAMPAIGN_CONFIGS may only contain single, tp2, pp2; got '$config'"
  if [[ "$SINGLE_NODE_ONLY" == "1" && "$config" != "single" ]]; then
    option_error "CAMPAIGN_CONFIGS may contain only single with SINGLE_NODE_ONLY=1"
  fi
done

declare -A CONCURRENCY_SET=()
validate_integer_set CONCURRENCIES "$CONCURRENCIES" CONCURRENCY_SET

# One line per model group: "LABELS|NEEDS_DUAL|rep:config:label;...".
# The helper rejects unknown labels and an empty selection.
SCHEDULER_ARGS=("$MODELS_YAML" "$REPEATS" --labels "$CAMPAIGN_LABELS" \
  --configs "$CAMPAIGN_CONFIGS")
[[ "$SINGLE_NODE_ONLY" == "1" ]] && SCHEDULER_ARGS+=(--single-node-only)
if ! SCHEDULE_TEXT="$("$PYTHON" scripts/campaign_schedule.py \
    "${SCHEDULER_ARGS[@]}")"; then
  option_error "cannot build the campaign schedule (see the message above)"
fi
MODEL_GROUPS=()
if [[ -n "$SCHEDULE_TEXT" ]]; then
  mapfile -t MODEL_GROUPS <<<"$SCHEDULE_TEXT"
fi

SELECTION_ARGS=(--models "$MODELS_YAML" --experiment "$CONFIG_YAML" \
  --concurrencies "$CONCURRENCIES" --repeats "$REPEATS")
if [[ -n "$CAMPAIGN_LABELS" ]]; then
  read -r -a REQUESTED_LABELS <<<"$CAMPAIGN_LABELS"
  SELECTION_ARGS+=(--labels "${REQUESTED_LABELS[@]}")
fi
if [[ -n "$CAMPAIGN_CONFIGS" ]]; then
  read -r -a REQUESTED_CONFIGS <<<"$CAMPAIGN_CONFIGS"
  SELECTION_ARGS+=(--configs "${REQUESTED_CONFIGS[@]}")
fi
[[ "$SINGLE_NODE_ONLY" == "1" ]] && SELECTION_ARGS+=(--single-node-only)
SELECTION_PLAN_JSON="$("$PYTHON" scripts/validate_campaign.py plan \
  "${SELECTION_ARGS[@]}")" || option_error "cannot validate the selected campaign grid"
SELECTED_LABELS="$("$PYTHON" -c 'import json,sys; print(" ".join(json.loads(sys.stdin.read())["selection"]["labels"]))' \
  <<<"$SELECTION_PLAN_JSON")"
SELECTED_CONFIGS="$("$PYTHON" -c 'import json,sys; selected=set(json.loads(sys.stdin.read())["selection"]["configs"]); print(" ".join(config for config in ("single", "tp2", "pp2") if config in selected))' \
  <<<"$SELECTION_PLAN_JSON")"
EXCLUDED_LABELS="$("$PYTHON" -c 'import json,sys; print(" ".join(json.loads(sys.stdin.read())["selection"]["excluded_labels"]))' \
  <<<"$SELECTION_PLAN_JSON")"

if [[ "$PLAN_ONLY" != "1" ]]; then
  validate_execution_environment "${HEAD_IP:-}" "paper campaign role" || exit 1
fi

if [[ "$SINGLE_NODE_ONLY" == "1" && "$PLAN_ONLY" != "1" ]]; then
  refuse_solo_ray_container_collision || exit 1
fi

IDENTITY_ARGS=(--file "$MODELS_YAML" --file "$CONFIG_YAML"
  --value "git_commit=$(git rev-parse HEAD)"
  --value "concurrencies=$CONCURRENCIES" --value "repeats=$REPEATS"
  --value "gpu_util_override=${GPU_UTIL:-}"
  --value "image=$VLLM_IMAGE"
  --value "dtype=${DTYPE:-auto}"
  --value "prefix_caching=${PREFIX_CACHING:-0}"
  --value "flashinfer_autotune=${FLASHINFER_AUTOTUNE:-0}"
  --value "generation_config=${GENERATION_CONFIG:-vllm}"
  --value "inductor_cache=${TORCHINDUCTOR_AUTOGRAD_CACHE:-0}"
  --value "single_container=$SINGLE_CONTAINER")
if [[ "$SINGLE_NODE_ONLY" == "1" ]]; then
  SOLO_EXECUTION_SHA256="$(PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON" -m bench.local_execution identity "$LOCAL_HOSTNAME" standalone)" || exit 1
  IDENTITY_ARGS+=(
    --value "single_node_only=1"
    --value "execution_scope=single-node"
    --value "execution_sha256=$SOLO_EXECUTION_SHA256"
    --value "hostname=$LOCAL_HOSTNAME"
    --value "serving_container=standalone"
    --value "hf_hub_offline=$HF_HUB_OFFLINE"
    --value "selected_labels=$SELECTED_LABELS"
    --value "selected_configs=$SELECTED_CONFIGS"
    --value "excluded_labels=$EXCLUDED_LABELS"
    --value "max_num_seqs=${MAX_NUM_SEQS:-}"
    --value "quant=${QUANT:-}"
  )
else
  IDENTITY_ARGS+=(
    --file "$CLUSTER_CONFIG"
    --value "campaign_labels=$CAMPAIGN_LABELS"
    --value "campaign_configs=$CAMPAIGN_CONFIGS"
    --value "network_policy=$NCCL_IB_DISABLE"
    --value "ib_hca=${NCCL_IB_HCA:-}"
    --value "gid_index=${NCCL_IB_GID_INDEX:-}"
    --value "net_plugin=${NCCL_NET_PLUGIN:-}"
    --value "ray_version=$RAY_VERSION"
    --value "head_ip=$HEAD_IP"
    --value "worker_ip=$WORKER_IP"
    --value "connectx_iface=$CX_IFACE"
    --value "topology_sha256=$TOPOLOGY_SHA256"
    --value "head_node_id=$HEAD_NODE_ID"
    --value "worker_node_id=$WORKER_NODE_ID"
    --value "head_cx_iface=$HEAD_CX_IFACE"
    --value "worker_cx_iface=$WORKER_CX_IFACE"
  )
fi
if [[ "$MODELS_ONDISK" == "1" ]]; then
  IDENTITY_ARGS+=(--value "models_ondisk=1" --value "hf_cache=$HF_CACHE")
fi
CAMPAIGN_IDENTITY="$("$PYTHON" "$HERE/utils/campaign_output.py" identity \
  "${IDENTITY_ARGS[@]}")" || exit 1
prepare_args=(--out-dir "$OUT_DIR" --identity-json "$CAMPAIGN_IDENTITY")
[[ "$PLAN_ONLY" == "1" ]] && prepare_args+=(--plan-only)
[[ "$CAMPAIGN_OUT_DIR_IMPLICIT" == "0" ]] && prepare_args+=(--allow-empty-existing)
"$PYTHON" "$HERE/utils/campaign_output.py" prepare "${prepare_args[@]}" || exit 1
if [[ "$PLAN_ONLY" != "1" && "$SINGLE_NODE_ONLY" == "1" ]]; then
  SAVED_EXECUTION_SHA256="$(PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON" -m bench.local_execution record "$CAMPAIGN_RESULTS_DIR" \
    "$LOCAL_HOSTNAME" standalone)" || exit 1
  [[ "$SAVED_EXECUTION_SHA256" == "$SOLO_EXECUTION_SHA256" ]] || {
    echo "[job] local execution record digest changed during campaign setup" >&2
    exit 1
  }
elif [[ "$PLAN_ONLY" != "1" ]]; then
  "$PYTHON" - "$CLUSTER_CONFIG" "$CAMPAIGN_RESULTS_DIR/inputs" \
      "$TOPOLOGY_RAW_SHA256" "$TOPOLOGY_SHA256" <<'PY' || exit 1
import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

source = Path(sys.argv[1])
input_directory = Path(sys.argv[2])
expected_raw_sha256 = sys.argv[3]
topology_sha256 = sys.argv[4]
topology_bytes = source.read_bytes()
actual_raw_sha256 = hashlib.sha256(topology_bytes).hexdigest()
if actual_raw_sha256 != expected_raw_sha256:
    raise SystemExit("[job] cluster YAML changed after topology validation")
input_directory.mkdir(parents=True, exist_ok=True)

def install_immutable_file(destination: Path, content: bytes) -> None:
    if destination.exists() or destination.is_symlink():
        status = destination.lstat()
        if not stat.S_ISREG(status.st_mode) or destination.is_symlink():
            raise SystemExit(f"[job] refusing non-regular topology input: {destination}")
        if destination.read_bytes() != content:
            raise SystemExit(f"[job] topology input conflicts with campaign: {destination}")
        return
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if destination.is_symlink() or destination.read_bytes() != content:
                raise SystemExit(
                    f"[job] topology input conflicts with campaign: {destination}"
                )
    finally:
        temporary.unlink(missing_ok=True)

metadata = json.dumps(
    {
        "file": "cluster.yaml",
        "raw_sha256": actual_raw_sha256,
        "schema_version": 1,
        "topology_sha256": topology_sha256,
    },
    indent=2,
    sort_keys=True,
).encode() + b"\n"
install_immutable_file(input_directory / "cluster.yaml", topology_bytes)
install_immutable_file(input_directory / "cluster_topology.json", metadata)
PY
fi

count_steps() {
  local entry steps total=0
  for entry in "${MODEL_GROUPS[@]}"; do
    [[ -z "$entry" ]] && continue
    IFS='|' read -r _ _ steps <<<"$entry"
    total=$(( total + $(tr ';' '\n' <<<"$steps" | grep -c .) ))
  done
  echo "$total"
}

print_plan() {
  local launches points point_s="" minimum_h="?" gpu_util_target
  launches="$(count_steps)"
  points=$(( launches * ${#CONCURRENCY_SET[@]} ))
  point_s="$("$PYTHON" -c 'import sys, yaml
c = yaml.safe_load(open(sys.argv[1]))
print(int(float(c["warmup_s"]) + float(c["measure_s"]) + float(c["cooldown_s"])))' \
    "$CONFIG_YAML" 2>/dev/null || true)"
  if [[ "$point_s" =~ ^[0-9]+$ ]]; then
    minimum_h=$(( points * point_s / 3600 ))
  fi
  gpu_util_target="$("$PYTHON" -c 'import os, sys, yaml
doc = yaml.safe_load(open(sys.argv[1])) or {}
value = os.environ.get("GPU_UTIL")
if value is None:
    value = doc.get("gpu_util_default", 0.80)
print(f"{float(value):g}")' "$MODELS_YAML")"
  echo "[plan] campaign options valid; no hardware operations will be performed"
  echo "[plan] PYTHON=$PYTHON PYTHONNOUSERSITE=$PYTHONNOUSERSITE OUT_DIR=$OUT_DIR"
  echo "[plan] canonical inputs: MODELS_YAML=$MODELS_YAML CONFIG_YAML=$CONFIG_YAML POWER_BACKEND=<experiment.yaml> MODEL_LABELS=<all> CANARY_ONLY=$CANARY_ONLY"
  echo "[plan] selection: labels=$SELECTED_LABELS configs=$SELECTED_CONFIGS excluded=$EXCLUDED_LABELS"
  echo "[plan] GPU_UTIL target: $gpu_util_target (default 0.80; override with GPU_UTIL)"
  echo "[plan] concurrencies (${#CONCURRENCY_SET[@]}): $CONCURRENCIES"
  echo "[plan] expected concurrency order: $EXPECTED_CONCURRENCIES"
  echo "[plan] replicates: REPEATS=$REPEATS independent launches per model/config, rotated config order"
  echo "[plan] model groups: ${#MODEL_GROUPS[@]}; server launches: $launches; measurement windows: $points"
  local entry labels dual steps first
  for entry in "${MODEL_GROUPS[@]}"; do
    IFS='|' read -r labels dual steps <<<"$entry"
    # Replicate 1's order names the group's configs.
    first="$(tr ';' '\n' <<<"$steps" | awk -F: '$1 == 1 {print $2}' | paste -sd' ')"
    printf '[plan]   %-40s configs: %-16s launches: %s\n' "$labels" "$first" \
      "$(tr ';' '\n' <<<"$steps" | grep -c .)"
  done
  echo "[plan] minimum scheduled phase time: ~${minimum_h} h (${point_s:-?} s per window before automatic extensions)"
  echo "[plan] canary pass: CANARY_FIRST=$CANARY_FIRST STOP_AFTER_CANARY=$STOP_AFTER_CANARY ALLOW_CANARY_FAILURES=$ALLOW_CANARY_FAILURES"
  if [[ "$MODELS_ONDISK" == "1" ]]; then
    echo "[plan] weights: MODELS_ONDISK=1 HF_CACHE=$HF_CACHE (local validation, mirror if dual-node, retain snapshots)"
    echo "[plan] compile-cache cleanup: canaries always; measurements when PURGE_WEIGHTS=$PURGE_WEIGHTS is 1"
  else
    echo "[plan] weights: MODELS_ONDISK=0 PURGE_WEIGHTS=$PURGE_WEIGHTS (acquire, mirror if dual-node, measure, purge per model)"
  fi
  if [[ "$SINGLE_NODE_ONLY" == "1" ]]; then
    echo "[plan] SINGLE_NODE_ONLY=1; local-only readiness, standalone serving, and local cache cleanup"
    echo "[plan] finalization skips interconnect figures and NCCL transport audit"
  else
    echo "[plan] finalization: verify, plots/tables, replicate statistics, transport audit, baseline comparison, sealed manifest"
  fi
  if (( REPEATS < 3 )); then
    echo "[plan] NOTE: REPEATS=$REPEATS gives no usable 95% interval; the paper design uses 3"
  fi
}

verify_saved_topology_input() {
  if [[ "$SINGLE_NODE_ONLY" == "1" ]]; then
    local saved_execution_sha256
    saved_execution_sha256="$(PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}" \
      "$PYTHON" -m bench.local_execution verify "$CAMPAIGN_RESULTS_DIR" \
      "$LOCAL_HOSTNAME" standalone)" || return 1
    if [[ "$saved_execution_sha256" != "$SOLO_EXECUTION_SHA256" ]]; then
      echo "[job] local execution record digest changed during the campaign" >&2
      return 1
    fi
    echo "[job] local execution record matches $saved_execution_sha256"
    return 0
  fi
  "$PYTHON" - "$CLUSTER_CONFIG" \
      "$CAMPAIGN_RESULTS_DIR/inputs/cluster.yaml" \
      "$TOPOLOGY_RAW_SHA256" "$TOPOLOGY_SHA256" <<'PY'
import sys
from pathlib import Path

from scripts.cluster_topology import TopologyError, read_topology

current_path = Path(sys.argv[1])
saved_path = Path(sys.argv[2])
expected_raw_sha256 = sys.argv[3]
expected_topology_sha256 = sys.argv[4]
try:
    current = read_topology(current_path)
    saved = read_topology(saved_path)
except (OSError, TopologyError) as error:
    raise SystemExit(f"[job] cannot verify saved topology input: {error}") from error
if current.raw_sha256 != expected_raw_sha256:
    raise SystemExit("[job] current cluster YAML changed during the campaign")
if saved.raw_sha256 != expected_raw_sha256:
    raise SystemExit("[job] saved cluster YAML differs from the campaign input")
if current.canonical_sha256 != expected_topology_sha256:
    raise SystemExit("[job] current topology digest changed during the campaign")
if saved.canonical_sha256 != expected_topology_sha256:
    raise SystemExit("[job] saved topology digest differs from the campaign input")
if current.raw_sha256 != saved.raw_sha256:
    raise SystemExit("[job] current and saved cluster YAML bytes differ")
print(f"[job] current topology matches saved input {expected_topology_sha256}")
PY
}

if [[ "$PLAN_ONLY" == "1" ]]; then
  "$PYTHON" scripts/check_environment.py || exit 1
  "$PYTHON" scripts/validate_campaign.py plan "${SELECTION_ARGS[@]}" >/dev/null || exit 1
  print_plan
  exit 0
fi

acquire_campaign_lock || exit 1

mkdir -p "$OUT_DIR" "$CAMPAIGN_RESULTS_DIR" "$CAMPAIGN_LOGS_DIR"
export CAMPAIGN_PATH_PREPARED=1
LOG="$CAMPAIGN_LOGS_DIR/run_job_$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

log() { echo "[job $(date '+%F %T')] $*"; }
FAILED=()
ACTIVE_CHILD=""
WORKER_SCOPE="$(solo_resource_scope)"
SOLO_RUNTIME_JSON=""
SOLO_RUNTIME_SHA256=""

capture_system_snapshot() {
  local timestamp snapshot_path status
  local -a snapshot_args=()
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  snapshot_path="$CAMPAIGN_LOGS_DIR/system_snapshot_${timestamp}_pid$$.json"
  snapshot_args=(--output "$snapshot_path" --container "$VLLM_CONTAINER" --image "$VLLM_IMAGE")
  if [[ "$SINGLE_NODE_ONLY" == "1" ]]; then
    snapshot_args+=(--local-only)
  else
    snapshot_args+=(--cluster-config "$CLUSTER_CONFIG")
  fi
  if "$PYTHON" scripts/system_snapshot.py "${snapshot_args[@]}"; then
    log "system snapshot saved: $snapshot_path"
  else
    status=$?
    log "system snapshot collection failed (rc=$status); continuing"
  fi
}

run_child() {
  local status=0
  "$@" &
  ACTIVE_CHILD=$!
  wait "$ACTIVE_CHILD" || status=$?
  ACTIVE_CHILD=""
  return "$status"
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
  # Fallback if a forcibly killed model wrapper could not run its own trap.
  docker exec "$VLLM_CONTAINER" pkill -f "vllm serve" >/dev/null 2>&1 || true
  if ! wait_for_resources_idle "$WORKER_SCOPE"; then
    log "shared resources may still be active after interruption"
  fi
  exit "$status"
}

# A prep failure means the campaign would waste GPU-days: stop immediately.
prep() {
  local name="$1"; shift
  log "=== prep: $name ==="
  if run_child "$@"; then log "prep OK: $name"; else
    log "PREP FAILED: $name -- aborting before any measurement"; exit 1
  fi
}

# A leg failure is logged and the campaign moves on; --skip-done makes a later
# rerun of this script pick up exactly the missing points.
leg() {
  local name="$1"; shift
  log "=== leg: $name ==="
  if run_child "$@"; then log "leg OK: $name"; else
    local status=$?
    log "leg FAILED (rc=$status): $name"
    if ! wait_for_resources_idle "$WORKER_SCOPE"; then
      log "shared resources are not idle after $name; stopping the campaign"
      exit 1
    fi
    log "shared resources are idle; continuing to the next independent leg"
    FAILED+=("$name")
  fi
}

ray_ok() {
  if ! docker exec "$VLLM_CONTAINER" ray status 2>/dev/null | grep -q "Active"; then
    log "Ray is not active in $VLLM_CONTAINER. Start it on both nodes:"
    log "  configured head:   scripts/start_ray.sh head"
    log "  configured worker: scripts/start_ray.sh worker"
    return 1
  fi
}

# A stale server collides with the first launch and breaks the idle-GPU
# precondition of the interconnect microbenchmarks.
no_stale_server() {
  wait_for_resources_idle "$WORKER_SCOPE"
}

source_tree_ok() {
  local untracked
  git rev-parse --verify HEAD >/dev/null 2>&1 || {
    log "repository has no recoverable Git commit"
    return 1
  }
  if ! git diff --quiet -- bench configs scripts plotting requirements.txt requirements.lock; then
    log "tracked benchmark sources have uncommitted changes"
    return 1
  fi
  if ! git diff --cached --quiet -- bench configs scripts plotting requirements.txt requirements.lock; then
    log "benchmark sources are staged but not committed"
    return 1
  fi
  untracked="$(git ls-files --others --exclude-standard -- bench configs scripts plotting requirements.txt requirements.lock)"
  if [[ -n "$untracked" ]]; then
    log "untracked benchmark source/input files are not recoverable:"
    printf '%s\n' "$untracked"
    return 1
  fi
}

cluster_runtime_ok() {
  CLUSTER_RUNTIME_JSON="$(docker exec -i "$VLLM_CONTAINER" python3 - \
    "$HEAD_IP" "$WORKER_IP" "$VLLM_IMAGE" "$RAY_VERSION" \
    "$TOPOLOGY_SHA256" "$HEAD_NODE_ID" "$WORKER_NODE_ID" \
    "$HEAD_CX_IFACE" "$WORKER_CX_IFACE" \
    < scripts/cluster_runtime_fingerprint.py)" || return 1
  CLUSTER_RUNTIME_FINGERPRINT="$(printf '%s' "$CLUSTER_RUNTIME_JSON" \
    | sha256sum | awk '{print $1}')"
  export CLUSTER_RUNTIME_FINGERPRINT
  log "cluster runtime fingerprint: $CLUSTER_RUNTIME_FINGERPRINT"
}

collect_solo_runtime() {
  SOLO_RUNTIME_JSON="$(PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}" \
    HF_HUB_OFFLINE="$HF_HUB_OFFLINE" \
    TORCHINDUCTOR_AUTOGRAD_CACHE="${TORCHINDUCTOR_AUTOGRAD_CACHE:-0}" \
    "$PYTHON" -m bench.local_execution runtime "$SOLO_EXECUTION_SHA256" \
    "$LOCAL_HOSTNAME" standalone "$VLLM_IMAGE")" || return 1
  SOLO_RUNTIME_SHA256="$(printf '%s' "$SOLO_RUNTIME_JSON" \
    | sha256sum | awk '{print $1}')"
  export SOLO_RUNTIME_SHA256
  log "single-host runtime fingerprint: $SOLO_RUNTIME_SHA256"
}

# RDMA availability can change across boots while the software fingerprint
# stays constant. Measure the live cluster on every campaign invocation.
allreduce_preflight() {
  scripts/ray_allreduce_bench.sh
}

# Point-to-point send/recv is PP's primitive; the all-reduce gate covers TP.
# Recorded once per campaign while the GPUs are idle.
sendrecv_once() {
  local artifact_id artifact want="roce"
  [[ "$NCCL_IB_DISABLE" == "1" ]] && want="tcp"
  artifact_id="$(scripts/interconnect_extra_bench.sh --fingerprint-only)" || return 1
  for artifact in "$CAMPAIGN_RESULTS_DIR"/interconnect_extra_"$artifact_id"_*.csv; do
    [[ -e "$artifact" ]] || continue
    if "$PYTHON" scripts/validate_microbench.py interconnect "$artifact" \
        --expected-transport "$want"; then
      log "valid send/recv artifact for runtime/source $artifact_id: $artifact"
      return 0
    fi
    log "ignoring malformed send/recv artifact: $artifact"
  done
  scripts/interconnect_extra_bench.sh
}

canary_identity() {
  if [[ "$SINGLE_NODE_ONLY" == "1" ]]; then
    {
      sha256sum "$MODELS_YAML" "$CONFIG_YAML" \
        "$CAMPAIGN_RESULTS_DIR/client_environment.json" \
        "$CAMPAIGN_RESULTS_DIR/inputs/local_execution.json"
      printf '%s\n' "$(git rev-parse HEAD)" "$VLLM_IMAGE" \
        "$SOLO_EXECUTION_SHA256" "$SOLO_RUNTIME_SHA256" "$LOCAL_HOSTNAME" \
        "$SINGLE_CONTAINER" "$SELECTED_LABELS" "$SELECTED_CONFIGS" \
        "$EXCLUDED_LABELS" "$CONCURRENCIES" "$REPEATS" \
        "${DTYPE:-auto}" "${PREFIX_CACHING:-0}" \
        "${FLASHINFER_AUTOTUNE:-0}" "${GENERATION_CONFIG:-vllm}" \
        "${TORCHINDUCTOR_AUTOGRAD_CACHE:-0}" "$HF_HUB_OFFLINE" \
        "${GPU_UTIL:-}"
    } | sha256sum | awk '{print $1}'
    return 0
  fi
  { sha256sum "$MODELS_YAML" "$CONFIG_YAML"
    # A successful canary is reusable only for the exact client, cluster,
    # serving policy, and network policy that passed it. The Git commit alone
    # does not identify environment-supplied launch options or live hardware.
    sha256sum "$CAMPAIGN_RESULTS_DIR/client_environment.json"
    printf '%s\n' "$(git rev-parse HEAD)" "$VLLM_IMAGE" "$RAY_VERSION" \
      "$CLUSTER_RUNTIME_FINGERPRINT" "$HEAD_IP" "$WORKER_IP" "$CX_IFACE" \
      "$TOPOLOGY_SHA256" "$HEAD_NODE_ID" "$WORKER_NODE_ID" \
      "$HEAD_CX_IFACE" "$WORKER_CX_IFACE" \
      "$NCCL_IB_DISABLE" "${NCCL_IB_HCA:-}" "${NCCL_IB_GID_INDEX:-}" \
      "${NCCL_NET_PLUGIN:-}" "${DTYPE:-auto}" "${PREFIX_CACHING:-0}" \
      "${FLASHINFER_AUTOTUNE:-0}" \
      "${GENERATION_CONFIG:-vllm}" "${TORCHINDUCTOR_AUTOGRAD_CACHE:-0}" \
      "${GPU_UTIL:-}" \
      "${HF_HUB_OFFLINE:-1}" "$SINGLE_CONTAINER" \
      "$CAMPAIGN_LABELS" "$CAMPAIGN_CONFIGS"
  } | sha256sum | awk '{print $1}'
}

acquire_group() {
  local labels="$1" needs_dual="$2"
  if [[ "$MODELS_ONDISK" == "1" ]]; then
    env MODEL_LABELS="$labels" CHECK_ONLY=1 scripts/predownload.sh all || return 1
  else
    env MODEL_LABELS="$labels" scripts/predownload.sh all || return 1
  fi
  if [[ "$needs_dual" == "1" ]]; then
    env MODEL_LABELS="$labels" scripts/mirror_worker_cache.sh all || return 1
  fi
}

purge_group() {
  if [[ "$MODELS_ONDISK" == "1" ]]; then
    env MODEL_LABELS="$1" PURGE_WEIGHTS=0 PURGE_CACHE_GARBAGE=0 \
      PURGE_COMPILE_CACHE=1 scripts/purge_model_cache.sh
  else
    env MODEL_LABELS="$1" PURGE_WEIGHTS=1 PURGE_COMPILE_CACHE=1 scripts/purge_model_cache.sh
  fi
}

# 0: every (label, config) of the group is complete; 3: work remains; else an
# error (an incompatible index row, Ray down, ...).
group_status() {
  local steps="$1" step rep config label rc result=0
  local -A seen=()
  local -a step_list=()
  IFS=';' read -ra step_list <<<"$steps"
  for step in "${step_list[@]}"; do
    IFS=':' read -r rep config label <<<"$step"
    [[ -n "${seen[$config:$label]:-}" ]] && continue
    seen[$config:$label]=1
    rc=0
    env STATUS_ONLY=1 MODEL_LABELS="$label" REPEATS="$REPEATS" \
        CONCURRENCIES="$CONCURRENCIES" EXPECTED_CONCURRENCIES="$CONCURRENCIES" \
        scripts/run_models.sh "$config" --skip-done || rc=$?
    case "$rc" in
      0) ;;
      3) result=3 ;;
      *) return 1 ;;
    esac
  done
  return "$result"
}

canary_pass() {
  local entry labels needs_dual steps step rep config label purge_status
  local -a failures=() step_list=()
  log "=== canary pass: ${#MODEL_GROUPS[@]} model group(s) ==="
  for entry in "${MODEL_GROUPS[@]}"; do
    IFS='|' read -r labels needs_dual steps <<<"$entry"
    if [[ "$SINGLE_NODE_ONLY" != "1" ]]; then
      ray_ok || { log "Ray cluster lost during the canary pass"; exit 1; }
    fi
    log "--- canary: $labels ---"
    if ! run_child acquire_group "$labels" "$needs_dual"; then
      failures+=("$labels: acquire/mirror")
      log "acquire/mirror FAILED for $labels"
    else
      IFS=';' read -ra step_list <<<"$steps"
      for step in "${step_list[@]}"; do
        IFS=':' read -r rep config label <<<"$step"
        [[ "$rep" == "1" ]] || continue
        if [[ "$SINGLE_NODE_ONLY" != "1" ]]; then
          ray_ok || { log "Ray cluster lost during the canary pass"; exit 1; }
        fi
        if ! run_child env RUN_JOB_CACHE_READY=1 CANARY_ONLY=1 \
            SKIP_DONE=0 MODEL_LABELS="$label" \
            scripts/run_models.sh "$config"; then
          failures+=("$label/$config")
          if ! wait_for_resources_idle "$WORKER_SCOPE"; then
            log "shared resources are not idle after canary $label/$config; stopping the canary pass"
            exit 1
          fi
        fi
      done
    fi
    if run_child purge_group "$labels"; then
      :
    else
      purge_status=$?
      log "cache purge failed (rc=$purge_status) for $labels; aborting because cache state is shared"
      exit 1
    fi
  done
  if (( ${#failures[@]} )); then
    log "canary pass FAILED for ${#failures[@]} item(s):"
    printf '  - %s\n' "${failures[@]}"
    if [[ "$ALLOW_CANARY_FAILURES" != "1" ]]; then
      log "fix models.yaml/image (or set ALLOW_CANARY_FAILURES=1), commit, and rerun"
      exit 1
    fi
    log "ALLOW_CANARY_FAILURES=1: measuring the models that passed"
    return 0
  fi
  canary_identity > "$CAMPAIGN_RESULTS_DIR/.canary_passed"
  log "canary pass OK; marker $CAMPAIGN_RESULTS_DIR/.canary_passed"
}

measure_group() {
  local labels="$1" needs_dual="$2" steps="$3" step rep config label status
  local -a step_list=()
  if [[ "$SINGLE_NODE_ONLY" != "1" ]]; then
    ray_ok || { log "Ray cluster lost; restart it on both nodes and rerun"; exit 1; }
  fi
  log "=== model: $labels ==="
  status=0
  group_status "$steps" || status=$?
  if [[ "$status" == "0" ]]; then
    log "$labels: every replicate is complete; not downloading"
    return 0
  elif [[ "$status" != "3" ]]; then
    log "$labels: resume status validation failed; aborting because campaign state is not trustworthy"
    exit 1
  fi
  if ! run_child acquire_group "$labels" "$needs_dual"; then
    FAILED+=("$labels: acquire/mirror")
    log "acquire/mirror FAILED for $labels"
    if [[ "$PURGE_WEIGHTS" == "1" ]]; then
      if run_child purge_group "$labels"; then
        :
      else
        status=$?
        log "cache purge failed (rc=$status) for $labels; aborting because cache state is shared"
        exit 1
      fi
    fi
    return 0
  fi
  IFS=';' read -ra step_list <<<"$steps"
  for step in "${step_list[@]}"; do
    IFS=':' read -r rep config label <<<"$step"
    if [[ "$SINGLE_NODE_ONLY" != "1" ]]; then
      ray_ok || { log "Ray cluster lost during measurements"; exit 1; }
    fi
    leg "$label $config replicate $rep/$REPEATS" \
      env RUN_JOB_CACHE_READY=1 REPEATS="$REPEATS" ONLY_REPEAT="$rep" \
          MODEL_LABELS="$label" \
          CONCURRENCIES="$CONCURRENCIES" EXPECTED_CONCURRENCIES="$CONCURRENCIES" \
          scripts/run_models.sh "$config" --skip-done
  done
  if [[ "$PURGE_WEIGHTS" == "1" ]]; then
    log "=== cleanup: purge $labels ==="
    if run_child purge_group "$labels"; then
      log "cleanup OK: purge $labels"
    else
      status=$?
      log "cleanup FAILED (rc=$status): purge $labels; aborting because cache state is shared"
      exit 1
    fi
  else
    log "PURGE_WEIGHTS=0: keeping $labels in the caches"
  fi
}

trap 'handle_signal 130' INT
trap 'handle_signal 143' TERM

log "campaign start; log: $LOG"
capture_system_snapshot
print_plan

prep "clean committed benchmark source tree" source_tree_ok
prep "isolated locked client environment" \
  "$PYTHON" scripts/check_environment.py \
    --manifest "$CAMPAIGN_RESULTS_DIR/client_environment.json"
prep "valid complete campaign definition" \
  "$PYTHON" scripts/validate_campaign.py plan "${SELECTION_ARGS[@]}"
if [[ "$MODELS_ONDISK" == "1" ]]; then
  prep "local serving image available" \
    docker image inspect --format '{{.Id}}' "$VLLM_IMAGE"
  prep "selected pinned snapshots complete in local cache" \
    env MODEL_LABELS="$SELECTED_LABELS" CHECK_ONLY=1 scripts/predownload.sh all
fi
if [[ "$SINGLE_NODE_ONLY" == "1" ]]; then
  HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-0}"
  export HF_HUB_OFFLINE
  prep "no local vLLM server" no_stale_server
  log "=== prep: local execution runtime identity ==="
  if collect_solo_runtime; then
    log "prep OK: local execution runtime identity"
  else
    log "PREP FAILED: local execution runtime identity -- aborting before any measurement"
    exit 1
  fi
  log "SKIP: Ray health and per-node runtime checks disabled (SINGLE_NODE_ONLY=1)"
  log "SKIP: worker cache mirroring disabled (SINGLE_NODE_ONLY=1)"
  log "SKIP: all-reduce gate disabled (SINGLE_NODE_ONLY=1)"
  log "SKIP: send/recv benchmark disabled (SINGLE_NODE_ONLY=1)"
else
  prep "Ray cluster active on head+worker"       ray_ok
  prep "no stale vLLM server"                    no_stale_server
  prep "two-node runtime identity matches"       cluster_runtime_ok
  prep "current-cluster allreduce RDMA preflight" allreduce_preflight
  leg  "send/recv interconnect microbench"       sendrecv_once
fi

case "$CANARY_FIRST" in
  1) canary_pass ;;
  auto)
    if [[ -f "$CAMPAIGN_RESULTS_DIR/.canary_passed" ]] \
        && [[ "$(cat "$CAMPAIGN_RESULTS_DIR/.canary_passed")" == "$(canary_identity)" ]]; then
      log "canary pass already succeeded for this commit/matrix/image; skipping"
    else
      canary_pass
    fi ;;
  0) log "CANARY_FIRST=0: no canary pass (every launch still checks the stream contract)" ;;
esac
if [[ "$STOP_AFTER_CANARY" == "1" ]]; then
  if (( ${#FAILED[@]} )); then
    log "preflight finished WITH FAILURES:"
    printf '  - %s\n' "${FAILED[@]}"
    exit 1
  fi
  log "STOP_AFTER_CANARY=1: stopping before measurement"
  exit 0
fi

for entry in "${MODEL_GROUPS[@]}"; do
  IFS='|' read -r group_labels group_dual group_steps <<<"$entry"
  measure_group "$group_labels" "$group_dual" "$group_steps"
done

if (( ${#FAILED[@]} )); then
  log "measurement pass finished WITH FAILURES:"
  printf '  - %s\n' "${FAILED[@]}"
  log "fix the cause and re-run scripts/run_job.sh; --skip-done resumes the gaps"
  log "(complete models are not downloaded again)"
  exit 1
fi

# Recheck after the multi-day measurement campaign. This catches any source
# edit or checkout performed while the job was running before those current
# files can be sealed beside rows produced by a different revision.
prep "final clean committed benchmark source tree" source_tree_ok
if [[ "$SINGLE_NODE_ONLY" == "1" ]]; then
  prep "local execution record matches saved campaign input" verify_saved_topology_input
else
  prep "current topology matches saved campaign input" verify_saved_topology_input
fi

VERIFY_ARGS=("${SELECTION_ARGS[@]}" --out-dir "$OUT_DIR")
if [[ "$SINGLE_NODE_ONLY" != "1" && -n "$CAMPAIGN_LABELS$CAMPAIGN_CONFIGS" ]]; then
  VERIFY_ARGS+=(--completed-only)
fi
prep "complete result grid + artifact verification" \
  "$PYTHON" scripts/validate_campaign.py verify "${VERIFY_ARGS[@]}"

leg "primary result figures + summary table" \
  "$PYTHON" plotting/plot_results.py --index "$CAMPAIGN_RESULTS_DIR/index.csv" --out "$CAMPAIGN_FIGURES_DIR" \
    --format both --table

# Written under figs/ so the sealed manifest covers them.
mkdir -p "$CAMPAIGN_FIGURES_DIR/stats"
if [[ "$SINGLE_NODE_ONLY" == "1" ]]; then
  log "SKIP: interconnect figure disabled (SINGLE_NODE_ONLY=1)"
else
  leg "interconnect figure (all-reduce and send/recv by transport)" \
    "$PYTHON" plotting/plot_interconnect.py "$OUT_DIR" --out "$CAMPAIGN_FIGURES_DIR/stats"
fi
leg "cross-model trend figures" \
  "$PYTHON" plotting/plot_trends.py --index "$CAMPAIGN_RESULTS_DIR/index.csv" \
    --out "$CAMPAIGN_FIGURES_DIR/trends" --format both --models "$MODELS_YAML"
leg "replicate statistics" \
  "$PYTHON" scripts/variance_report.py --index "$CAMPAIGN_RESULTS_DIR/index.csv" \
    --md "$CAMPAIGN_FIGURES_DIR/stats/replicate_stats.md" \
    --tex "$CAMPAIGN_FIGURES_DIR/stats/replicate_stats.tex"
if [[ "$SINGLE_NODE_ONLY" == "1" ]]; then
  log "SKIP: NCCL transport audit disabled (SINGLE_NODE_ONLY=1)"
else
  leg "NCCL transport audit" \
    "$PYTHON" scripts/audit_nccl_transport.py "$OUT_DIR" \
      --md "$CAMPAIGN_FIGURES_DIR/stats/nccl_transport_audit.md"
fi
# Same-model comparisons use the active Llama 3.1 8B ladder against the PMBS26
# TCP baseline and the Qwen3-Next 80B pair against the October RDMA pilot.
if [[ -f baselines/pmbs26-tcp/index.csv ]]; then
  leg "comparison with the PMBS26 TCP baseline" \
    "$PYTHON" scripts/compare_campaigns.py --new "$CAMPAIGN_RESULTS_DIR/index.csv" \
      --baseline baselines/pmbs26-tcp/index.csv \
      --md "$CAMPAIGN_FIGURES_DIR/stats/compare_vs_pmbs26_tcp.md"
fi
if [[ -f baselines/rdma-pilot-2026-10/index.csv ]]; then
  leg "comparison with the October RDMA pilot" \
    "$PYTHON" scripts/compare_campaigns.py --new "$CAMPAIGN_RESULTS_DIR/index.csv" \
      --baseline baselines/rdma-pilot-2026-10/index.csv \
      --md "$CAMPAIGN_FIGURES_DIR/stats/compare_vs_rdma_pilot.md"
fi

prep "pre-seal clean committed benchmark source tree" source_tree_ok

prep "sealed final artifact manifest" \
  "$PYTHON" scripts/validate_campaign.py verify "${VERIFY_ARGS[@]}" \
    --manifest "$CAMPAIGN_RESULTS_DIR/campaign_manifest.json"

if (( ${#FAILED[@]} )); then
  log "campaign sealed; optional reports failed:"
  printf '  - %s\n' "${FAILED[@]}"
  exit 1
fi
log "campaign complete: $CAMPAIGN_RESULTS_DIR/index.csv, figures and statistics in $CAMPAIGN_FIGURES_DIR/"
