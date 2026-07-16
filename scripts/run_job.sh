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
#   4. targeted repeats at CONCURRENCIES="$REPEAT_CONCS" up to $REPEAT_TOTAL
#      copies (REPEAT_CONCS="" skips)
#   5. single, LAST: full pass then targeted repeats
#   6. best-effort plots
#
# Every sweep leg uses --skip-done, so re-running this script after a crash
# resumes at (repeat, concurrency) granularity. Two campaign rules:
#   - finalize configs/experiment.yaml (warmup/measure/cooldown) BEFORE the
#     first point; it feeds config_file_sha256, so a mid-campaign change makes
#     every prior row stop counting as done and mixes identities in plots;
#   - do not edit bench/*.py or the fingerprinted scripts mid-campaign, for the
#     same reason (harness_source_sha256).
#
# Knobs (env): OUT_DIR, CONCURRENCIES, FULL_REPEATS, REPEAT_CONCS,
#              REPEAT_TOTAL, CANARY_FIRST.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"
source scripts/connectx_env.sh    # VLLM_CONTAINER, HEAD_IP, HOEDUS_IP, .env
require_local_connectx_ip "$HEAD_IP" "paper campaign head role" || exit 1
acquire_campaign_lock || exit 1

export OUT_DIR="${OUT_DIR:-results}"
CONCURRENCIES="${CONCURRENCIES:-1 2 4 8 16 32 64 128}"
export EXPECTED_CONCURRENCIES="${EXPECTED_CONCURRENCIES:-$CONCURRENCIES}"
FULL_REPEATS="${FULL_REPEATS:-3}"
REPEAT_CONCS="${REPEAT_CONCS:-}"
REPEAT_TOTAL="${REPEAT_TOTAL:-3}"
CANARY_FIRST="${CANARY_FIRST:-1}"

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
    if python3 -c 'import csv, math, sys
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

leg "plots + summary table" \
  python3 plot_results.py --index "$OUT_DIR/index.csv" --out figs \
    --format both --table

if (( ${#FAILED[@]} )); then
  log "campaign finished WITH FAILURES:"
  printf '  - %s\n' "${FAILED[@]}"
  log "fix the cause and re-run scripts/run_job.sh; --skip-done resumes the gaps"
  exit 1
fi
log "campaign complete: $OUT_DIR/index.csv, figures in figs/"
