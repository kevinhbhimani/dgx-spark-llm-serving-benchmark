#!/usr/bin/env bash
# Unattended full re-run campaign. Run this INSIDE tmux on the head node (Diya)
# after starting Ray on both nodes:
#
#   Diya:    scripts/start_ray.sh head
#   Hoedus:  scripts/start_ray.sh worker
#   Diya:    scripts/run_job.sh
#
# Order is chosen so the cheap, fixable failures surface in the first hours
# instead of on day 3, and so nothing dual-node runs after the single-node leg
# (launch_server.sh single replaces the Ray head container, which kills the
# cluster; Hoedus cannot be restarted from here — no SSH path).
#
#   1. prep (aborts the job on failure): Ray health, pinned head cache
#      (validate, download only if incomplete), worker mirror, allreduce
#      microbench (once, while GPUs are idle)
#   2. strict canaries: every tp2/pp2 model launches once and must pass the
#      streaming contract; no measurement. Costs a few hours of model loads;
#      CANARY_FIRST=0 skips (the per-launch contract check still guards sweeps).
#   3. full pass at REPEATS=$FULL_REPEATS: tp2, then pp2
#   4. optional extra full rectangles up to $REPEAT_TOTAL copies
#      (REPEAT_CONCS="" skips; publication indexes cannot add only some loads)
#   5. single, LAST: full pass then optional extra full rectangle
#   6. fail-closed campaign validation, plots, and artifact manifest
#
# Every sweep leg uses --skip-done, so re-running this script after a crash
# resumes at (repeat, concurrency) granularity. Two campaign rules:
#   - finalize configs/experiment.yaml (warmup/measure/cooldown) BEFORE the
#     first point; it feeds config_file_sha256, so a mid-campaign change is
#     rejected and requires a fresh OUT_DIR;
#   - do not edit bench/*.py or the fingerprinted scripts mid-campaign, for the
#     same reason (harness_source_sha256). The launch gate also requires these
#     files to be committed, so every row names a recoverable Git revision.
#
# Knobs (env): OUT_DIR, CONCURRENCIES, FULL_REPEATS, REPEAT_CONCS,
#              REPEAT_TOTAL, CANARY_FIRST.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

# Claim every campaign knob from the caller BEFORE .env is sourced.
# scripts/load_env.sh gives already-exported values precedence over .env, so
# exporting them here keeps a stale OUT_DIR/CONCURRENCIES in .env from
# silently retargeting the paper campaign at an old result directory.
export OUT_DIR="${OUT_DIR:-results-steady-exact-v3}"
export CONCURRENCIES="${CONCURRENCIES-1 2 4 8 16 32 64 128}"
export EXPECTED_CONCURRENCIES="$CONCURRENCIES"
export FULL_REPEATS="${FULL_REPEATS-1}"
export REPEAT_CONCS="${REPEAT_CONCS:-}"
export REPEAT_TOTAL="${REPEAT_TOTAL-3}"
export CANARY_FIRST="${CANARY_FIRST-1}"
export PLAN_ONLY="${PLAN_ONLY-0}"

source scripts/connectx_env.sh    # VLLM_CONTAINER, HEAD_IP, HOEDUS_IP, .env

# The unattended paper campaign owns these inputs. Clear same-named values
# inherited from .env or the caller so planning, measurement, and final
# verification cannot silently target different YAML, a label subset, a
# canary-only sweep, or a power-policy override.
export MODELS_YAML="configs/models.yaml"
export CONFIG_YAML="configs/experiment.yaml"
export POWER_BACKEND=""
export CANARY_ONLY=0
export MODEL_LABELS=""

if [[ -z "${PYTHON:-}" ]]; then
  if [[ -x "$HERE/.venv/bin/python" ]]; then
    PYTHON="$HERE/.venv/bin/python"
  else
    PYTHON=python3
  fi
fi
export PYTHON
export PYTHONNOUSERSITE=1

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

integer_sets_match() {
  local left_name="$1" right_name="$2" value
  local -n left="$left_name" right="$right_name"
  (( ${#left[@]} == ${#right[@]} )) || return 1
  for value in "${!left[@]}"; do
    [[ -n "${right[$value]+x}" ]] || return 1
  done
}

# Compare positive decimal strings without overflowing bash arithmetic.
decimal_is_greater() {
  local left="$1" right="$2"
  (( ${#left} > ${#right} )) && return 0
  (( ${#left} < ${#right} )) && return 1
  [[ "$left" > "$right" ]]
}

[[ "$CANARY_FIRST" == "0" || "$CANARY_FIRST" == "1" ]] || \
  option_error "CANARY_FIRST must be exactly 0 or 1, got '$CANARY_FIRST'"
[[ "$PLAN_ONLY" == "0" || "$PLAN_ONLY" == "1" ]] || \
  option_error "PLAN_ONLY must be exactly 0 or 1, got '$PLAN_ONLY'"
validate_positive_integer FULL_REPEATS "$FULL_REPEATS"
validate_positive_integer REPEAT_TOTAL "$REPEAT_TOTAL"

declare -A CONCURRENCY_SET=() EXPECTED_CONCURRENCY_SET=() REPEAT_CONCURRENCY_SET=()
validate_integer_set CONCURRENCIES "$CONCURRENCIES" CONCURRENCY_SET
validate_integer_set EXPECTED_CONCURRENCIES "$EXPECTED_CONCURRENCIES" \
  EXPECTED_CONCURRENCY_SET
integer_sets_match CONCURRENCY_SET EXPECTED_CONCURRENCY_SET || \
  option_error "CONCURRENCIES and EXPECTED_CONCURRENCIES must contain the same full set"

if [[ -n "$REPEAT_CONCS" ]]; then
  validate_integer_set REPEAT_CONCS "$REPEAT_CONCS" REPEAT_CONCURRENCY_SET
  integer_sets_match CONCURRENCY_SET REPEAT_CONCURRENCY_SET || \
    option_error "REPEAT_CONCS must be empty or contain the same full concurrency set"
  decimal_is_greater "$REPEAT_TOTAL" "$FULL_REPEATS" || \
    option_error "REPEAT_TOTAL must exceed FULL_REPEATS when REPEAT_CONCS is nonempty"
fi
FINAL_REPEATS="$FULL_REPEATS"
[[ -n "$REPEAT_CONCS" ]] && FINAL_REPEATS="$REPEAT_TOTAL"

print_plan() {
  local canary_legs=0 repeat_legs=0
  [[ "$CANARY_FIRST" == "1" ]] && canary_legs=2
  [[ -n "$REPEAT_CONCS" ]] && repeat_legs=3
  echo "[plan] campaign options valid; no hardware operations will be performed"
  echo "[plan] PYTHON=$PYTHON PYTHONNOUSERSITE=$PYTHONNOUSERSITE OUT_DIR=$OUT_DIR"
  echo "[plan] canonical inputs: MODELS_YAML=$MODELS_YAML CONFIG_YAML=$CONFIG_YAML POWER_BACKEND=<experiment.yaml> MODEL_LABELS=<all> CANARY_ONLY=$CANARY_ONLY"
  echo "[plan] concurrencies (${#CONCURRENCY_SET[@]}): $CONCURRENCIES"
  echo "[plan] expected concurrency order: $EXPECTED_CONCURRENCIES"
  echo "[plan] prep legs (8): source, environment, matrix, Ray, runtime, head cache, worker cache, allreduce"
  echo "[plan] canary legs ($canary_legs): CANARY_FIRST=$CANARY_FIRST (tp2, pp2 when enabled)"
  echo "[plan] full measurement legs (3): tp2, pp2, single; $FULL_REPEATS repeat(s) per concurrency"
  if [[ -n "$REPEAT_CONCS" ]]; then
    echo "[plan] repeat measurement legs ($repeat_legs): tp2, pp2, single; target $REPEAT_TOTAL total repeat(s) per concurrency"
  else
    echo "[plan] repeat measurement legs (0): REPEAT_CONCS is empty"
  fi
  echo "[plan] finalization legs (5): source audit, verify, plots/table in $OUT_DIR/figs, pre-seal audit, sealed manifest"
}

if [[ "$PLAN_ONLY" == "1" ]]; then
  "$PYTHON" scripts/check_environment.py || exit 1
  "$PYTHON" scripts/validate_campaign.py plan --models "$MODELS_YAML" --experiment "$CONFIG_YAML" --concurrencies "$CONCURRENCIES" --repeats "$FINAL_REPEATS" || exit 1
  print_plan
  exit 0
fi

require_local_connectx_ip "$HEAD_IP" "paper campaign head role" || exit 1
acquire_campaign_lock || exit 1

mkdir -p "$OUT_DIR"
LOG="$OUT_DIR/run_job_$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

log() { echo "[job $(date '+%F %T')] $*"; }
FAILED=()
ACTIVE_CHILD=""

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

# A sweep-leg failure is logged and the campaign moves on; --skip-done makes a
# later rerun of this script pick up exactly the missing points.
leg() {
  local name="$1"; shift
  log "=== leg: $name ==="
  if run_child "$@"; then log "leg OK: $name"; else
    log "leg FAILED (rc=$?): $name -- continuing"; FAILED+=("$name")
  fi
}

ray_ok() {
  if ! docker exec "$VLLM_CONTAINER" ray status 2>/dev/null | grep -q "Active"; then
    log "Ray is not active in $VLLM_CONTAINER. Start it first:"
    log "  Diya:   scripts/start_ray.sh head"
    log "  Hoedus: scripts/start_ray.sh worker"
    log "(If this is a rerun after the single leg already finished, single"
    log " replaced the Ray head container; restart Ray on both nodes.)"
    return 1
  fi
}

source_tree_ok() {
  local untracked
  git rev-parse --verify HEAD >/dev/null 2>&1 || {
    log "repository has no recoverable Git commit"
    return 1
  }
  if ! git diff --quiet -- bench configs scripts plot_results.py requirements.txt requirements.lock; then
    log "tracked benchmark sources have uncommitted changes"
    return 1
  fi
  if ! git diff --cached --quiet -- bench configs scripts plot_results.py requirements.txt requirements.lock; then
    log "benchmark sources are staged but not committed"
    return 1
  fi
  untracked="$(git ls-files --others --exclude-standard -- bench configs scripts plot_results.py requirements.txt requirements.lock)"
  if [[ -n "$untracked" ]]; then
    log "untracked benchmark source/input files are not recoverable:"
    printf '%s\n' "$untracked"
    return 1
  fi
}

cluster_runtime_ok() {
  CLUSTER_RUNTIME_JSON="$(docker exec -i "$VLLM_CONTAINER" python3 - \
    "$HEAD_IP" "$HOEDUS_IP" "$VLLM_IMAGE" "$RAY_VERSION" \
    < scripts/cluster_runtime_fingerprint.py)" || return 1
  CLUSTER_RUNTIME_FINGERPRINT="$(printf '%s' "$CLUSTER_RUNTIME_JSON" \
    | sha256sum | awk '{print $1}')"
  export CLUSTER_RUNTIME_FINGERPRINT
  log "cluster runtime fingerprint: $CLUSTER_RUNTIME_FINGERPRINT"
}

# Validate the pinned snapshots offline first; only touch the network when
# something is actually missing, so a dead DNS can't block a warm cache.
warm_head_cache() {
  CHECK_ONLY=1 scripts/predownload.sh all && return 0
  log "head cache incomplete; downloading pinned snapshots"
  scripts/predownload.sh all
}

# Interconnect microbench: reuse only a complete artifact from this exact
# cluster runtime and benchmark source.
allreduce_once() {
  local artifact_id artifact
  artifact_id="$(scripts/ray_allreduce_bench.sh --fingerprint-only)" || return 1
  for artifact in "$OUT_DIR"/allreduce_ray_"$artifact_id"_*.csv; do
    [[ -e "$artifact" ]] || continue
    if "$PYTHON" -c 'import csv, math, sys
path = sys.argv[1]
with open(path, newline="") as handle:
    reader = csv.DictReader(handle)
    rows = list(reader)
expected_header = ["bytes", "avg_s", "min_s", "algbw_GBps", "busbw_GBps"]
expected_sizes = [1 << exponent for exponent in range(3, 31)]
valid = reader.fieldnames == expected_header
valid = valid and [int(row["bytes"]) for row in rows] == expected_sizes
valid = valid and all(
    math.isfinite(float(row[field])) and float(row[field]) > 0
    for row in rows
    for field in expected_header[1:]
)
raise SystemExit(0 if valid else 1)' "$artifact"; then
      log "valid allreduce artifact for runtime/source $artifact_id: $artifact"
      return 0
    fi
    log "ignoring malformed allreduce artifact: $artifact"
  done
  scripts/ray_allreduce_bench.sh
}

trap 'handle_signal 130' INT
trap 'handle_signal 143' TERM

log "campaign start; log: $LOG"
log "OUT_DIR=$OUT_DIR FULL_REPEATS=$FULL_REPEATS REPEAT_CONCS='$REPEAT_CONCS'" \
    "REPEAT_TOTAL=$REPEAT_TOTAL CONCURRENCIES='$CONCURRENCIES'"

prep "clean committed benchmark source tree" source_tree_ok
prep "isolated locked client environment" \
  "$PYTHON" scripts/check_environment.py \
    --manifest "$OUT_DIR/client_environment.json"
prep "valid complete campaign definition" \
  "$PYTHON" scripts/validate_campaign.py plan --models "$MODELS_YAML" \
    --experiment "$CONFIG_YAML" --concurrencies "$CONCURRENCIES" \
    --repeats "$FINAL_REPEATS"
prep "Ray cluster active on head+worker"      ray_ok
prep "two-node runtime identity matches"      cluster_runtime_ok
prep "head HF cache has pinned snapshots"     warm_head_cache
prep "mirror pinned snapshots to Hoedus"      scripts/mirror_worker_cache.sh all
prep "allreduce interconnect microbench"      allreduce_once

if [[ "$CANARY_FIRST" == "1" ]]; then
  prep "tp2 canaries (launch + stream contract)" \
    env CANARY_ONLY=1 SKIP_DONE=0 scripts/run_models.sh tp2
  prep "pp2 canaries (launch + stream contract)" \
    env CANARY_ONLY=1 SKIP_DONE=0 scripts/run_models.sh pp2
fi

leg "tp2 full pass (REPEATS=$FULL_REPEATS)" \
  env REPEATS="$FULL_REPEATS" CONCURRENCIES="$CONCURRENCIES" \
  scripts/run_models.sh tp2 --skip-done
leg "pp2 full pass (REPEATS=$FULL_REPEATS)" \
  env REPEATS="$FULL_REPEATS" CONCURRENCIES="$CONCURRENCIES" \
  scripts/run_models.sh pp2 --skip-done

if [[ -n "$REPEAT_CONCS" ]]; then
  leg "tp2 repeats x$REPEAT_TOTAL at c={$REPEAT_CONCS}" \
    env REPEATS="$REPEAT_TOTAL" CONCURRENCIES="$REPEAT_CONCS" \
    scripts/run_models.sh tp2 --skip-done
  leg "pp2 repeats x$REPEAT_TOTAL at c={$REPEAT_CONCS}" \
    env REPEATS="$REPEAT_TOTAL" CONCURRENCIES="$REPEAT_CONCS" \
    scripts/run_models.sh pp2 --skip-done
fi

# single LAST: its launcher does `docker rm -f $VLLM_CONTAINER`, replacing the
# Ray head container. Nothing dual-node may run after this point.
log "NOTE: starting single-node legs; the Ray cluster will be torn down."
leg "single full pass (REPEATS=$FULL_REPEATS)" \
  env REPEATS="$FULL_REPEATS" CONCURRENCIES="$CONCURRENCIES" \
  scripts/run_models.sh single --skip-done
if [[ -n "$REPEAT_CONCS" ]]; then
  leg "single repeats x$REPEAT_TOTAL at c={$REPEAT_CONCS}" \
    env REPEATS="$REPEAT_TOTAL" CONCURRENCIES="$REPEAT_CONCS" \
    scripts/run_models.sh single --skip-done
fi

# Recheck after the multi-day measurement campaign. This catches any source
# edit or checkout performed while the job was running before those current
# files can be sealed beside rows produced by a different revision.
prep "final clean committed benchmark source tree" source_tree_ok

prep "complete result grid + artifact verification" \
  "$PYTHON" scripts/validate_campaign.py verify --models "$MODELS_YAML" \
    --experiment "$CONFIG_YAML" --out-dir "$OUT_DIR" \
    --concurrencies "$CONCURRENCIES" --repeats "$FINAL_REPEATS"

prep "plots + summary table" \
  "$PYTHON" plot_results.py --index "$OUT_DIR/index.csv" --out "$OUT_DIR/figs" \
    --format both --table

prep "pre-seal clean committed benchmark source tree" source_tree_ok

prep "sealed final artifact manifest" \
  "$PYTHON" scripts/validate_campaign.py verify --models "$MODELS_YAML" \
    --experiment "$CONFIG_YAML" --out-dir "$OUT_DIR" \
    --concurrencies "$CONCURRENCIES" --repeats "$FINAL_REPEATS" \
    --manifest "$OUT_DIR/campaign_manifest.json"

if (( ${#FAILED[@]} )); then
  log "campaign finished WITH FAILURES:"
  printf '  - %s\n' "${FAILED[@]}"
  log "fix the cause and re-run scripts/run_job.sh; --skip-done resumes the gaps"
  exit 1
fi
log "campaign complete: $OUT_DIR/index.csv, figures in $OUT_DIR/figs/"
