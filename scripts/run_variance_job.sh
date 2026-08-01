#!/usr/bin/env bash
# Unattended run-to-run variance campaign. Run this INSIDE tmux on the head node
# (Diya) after starting Ray on both nodes:
#
#   Diya:    scripts/start_ray.sh head
#   Hoedus:  scripts/start_ray.sh worker
#   Diya:    tmux new -s variance 'scripts/run_variance_job.sh'
#
# Answers the paper's #1 reviewer objection ("one run per point, so the narrow
# margins are indistinguishable from zero effect") by repeating ONLY the points
# the contested claims are computed at, in EVERY arm of each comparison, into a
# sibling OUT_DIR. The sealed main campaign in results/ is never touched.
#
# Point selection is derived from results/index.csv, not chosen by feel. The
# claims that a single repeat cannot defend are the ones with a small margin:
#
#   strict-goodput ranking (max over the load curve, argmax concurrency shown):
#     qwen3-30b-a3b        c=8  pp2 .3427 single .3347 tp2 .3333  -> 0.009 req/s
#     nemotron3-super-120b c=2  pp2 .0727 single .0633 tp2 .0620  -> 0.010 req/s
#   In BOTH cases all three arms sit inside 0.011 req/s, so which configuration
#   is even the runner-up is undecided; all three must be repeated or the Welch
#   interval is computed against an arbitrarily chosen loser.
#
#   peak-throughput ratios that round to unity:
#     nemotron3-nano c=128 pp2/single = 1.023
#     llama3.1-8b    c=32  tp2/single = 0.996
#     qwen3-8b       c=32  tp2/single = 1.000
#   Each ratio is taken at the concurrency where BOTH arms peak, so the repeats
#   land on the same points the ratio is computed from.
#
#   nemotron3-nano also repeats at c=8 (goodput .7280/.7113) so the spread is
#   measured at two loads on one model. Run-to-run noise is not assumed uniform
#   in concurrency: c=2 on the 120B sees ~130 completions per window while
#   c=128 on the nano sees ~3000, and the paper should not quote a noise floor
#   from one load and apply it to the whole curve.
#
# INTERLEAVE=1 (default) makes repeat the OUTER loop and calls run_models.sh
# once per repeat with an escalating REPEATS and --skip-done, so exactly one new
# window runs per call. That matters: with all repeats inside a single call they
# execute back-to-back against one already-loaded server, and the resulting CV
# excludes model load, allocator state and cluster formation -- variation that a
# pp2-vs-single margin necessarily spans, because those two arms are always
# different launches. INTERLEAVE=0 restores the cheaper stacked behaviour.
#
# Order is forced: every dual-node leg runs before any single-node leg, because
# launch_server.sh single replaces the Ray head container and Hoedus cannot be
# restarted from here (no SSH path). Within that, one leg per (model, config) so
# each model carries its own load list; run_models.sh crosses MODEL_LABELS with
# CONCURRENCIES, so one invocation cannot give two models different loads.
#
# The two-node point-to-point (sendrecv) microbench runs once up front, while
# Ray is up and the GPU is idle. results/ has all-reduce CSVs but no sendrecv
# artifact, which is why the paper currently has to say the all-reduce figures
# "would be comparable to a point-to-point socket measurement, which this study
# does not include". This closes that gap for minutes of wall time, and it must
# happen before phase 2 tears the Ray cluster down.
#
# Every leg uses --skip-done, so re-running this script after a crash resumes at
# (repeat, concurrency) granularity rather than repeating finished points. One
# caveat: a point's concurrency list is recorded on each row as
# expected_concurrencies, so EDITING a point's loads mid-campaign orphans the
# rows already collected for that model -- they no longer match the request and
# will be re-measured. Add a new entry instead of widening an existing one.
#
# This changes no measurement source: scripts/variance_report.py and this file
# are outside bench/run_benchmark.py's _source_fingerprint() list, so the rows
# produced here carry the SAME harness_source_sha256 as results/ -- which is
# what lets the measured spread be quoted as a bound on the main campaign.
#
# Knobs (env): OUT_DIR, VAR_REPEATS, POINTS_FILE, PLAN_ONLY, SKIP_MIRROR,
#              INTERLEAVE, SKIP_INTERCONNECT, INTERCONNECT_OUT_DIR.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

# Claim every knob from the caller BEFORE .env is sourced; scripts/load_env.sh
# gives already-exported values precedence, so this keeps a stale OUT_DIR in
# .env from silently retargeting the run at the sealed campaign directory.
export OUT_DIR="${OUT_DIR:-results-variance}"
export VAR_REPEATS="${VAR_REPEATS:-3}"
export PLAN_ONLY="${PLAN_ONLY-0}"
export SKIP_MIRROR="${SKIP_MIRROR-0}"
export INTERLEAVE="${INTERLEAVE-1}"
export SKIP_INTERCONNECT="${SKIP_INTERCONNECT-0}"

source scripts/connectx_env.sh    # VLLM_CONTAINER, HEAD_IP, HOEDUS_IP, .env

# This job owns these inputs; clear same-named values inherited from .env so a
# leftover label subset or power override cannot silently change what runs.
export MODELS_YAML="configs/models.yaml"
export CONFIG_YAML="configs/experiment.yaml"
export POWER_BACKEND=""
export CANARY_ONLY=0
export CANARY_FIRST=0
export MODEL_LABELS=""
export INTERCONNECT_OUT_DIR="${INTERCONNECT_OUT_DIR:-$OUT_DIR}"

if [[ -z "${PYTHON:-}" ]]; then
  if [[ -x "$HERE/.venv/bin/python" ]]; then
    PYTHON="$HERE/.venv/bin/python"
  else
    PYTHON=python3
  fi
fi
export PYTHON
export PYTHONNOUSERSITE=1

for flag_name in PLAN_ONLY SKIP_MIRROR INTERLEAVE SKIP_INTERCONNECT; do
  flag_value="${!flag_name}"
  [[ "$flag_value" == "0" || "$flag_value" == "1" ]] || {
    echo "[variance-job] $flag_name must be 0 or 1, got '$flag_value'" >&2
    exit 1; }
done
[[ "$VAR_REPEATS" =~ ^[2-9][0-9]*$ ]] || {
  echo "[variance-job] VAR_REPEATS must be an integer >= 2 (a variance run" \
       "needs at least two samples), got '$VAR_REPEATS'" >&2; exit 1; }

# "<model> <c[,c...]> <cfg[,cfg...]> [note]". Configs are explicit -- single is
# no longer implied -- so a point can repeat exactly the arms its claim ranks.
# POINTS_FILE overrides for a different question ('#' comments allowed).
POINTS=(
  "qwen3-30b-a3b                   8     pp2,tp2,single strict-goodput 3-way tie, margin 0.009 req/s"
  "nemotron3-super-120b-a12b-nvfp4 2     pp2,tp2,single strict-goodput 3-way tie, margin 0.010 req/s"
  "nemotron3-nano-30b-a3b-nvfp4    8,128 pp2,single     peak-throughput ratio 1.023 at c=128; c=8 gives a second load"
  "llama3.1-8b                     32    tp2,single     peak-throughput ratio 0.996"
  "qwen3-8b                        32    tp2,single     peak-throughput ratio 1.000"
)
if [[ -n "${POINTS_FILE:-}" ]]; then
  [[ -r "$POINTS_FILE" ]] || {
    echo "[variance-job] cannot read POINTS_FILE=$POINTS_FILE" >&2; exit 1; }
  mapfile -t POINTS < <(sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "$POINTS_FILE")
  (( ${#POINTS[@]} > 0 )) || {
    echo "[variance-job] POINTS_FILE has no entries" >&2; exit 1; }
fi

# Validate every entry before any GPU time is committed: an unknown label or a
# typo'd config would otherwise surface hours in, on the leg that uses it.
KNOWN_LABELS="$("$PYTHON" -c '
import yaml
d = yaml.safe_load(open("configs/models.yaml"))
print(" ".join(m["label"] for m in d["models"] if "label" in m))
')" || { echo "[variance-job] could not read model labels" >&2; exit 1; }

N_ARMS=0
for entry in "${POINTS[@]}"; do
  read -r p_model p_conc p_cfg p_note <<<"$entry"
  [[ -n "$p_model" && -n "$p_conc" && -n "$p_cfg" ]] || {
    echo "[variance-job] malformed POINTS entry: '$entry'" >&2; exit 1; }
  [[ " $KNOWN_LABELS " == *" $p_model "* ]] || {
    echo "[variance-job] unknown model label '$p_model'" >&2; exit 1; }
  for c in ${p_conc//,/ }; do
    [[ "$c" =~ ^[1-9][0-9]*$ ]] || {
      echo "[variance-job] bad concurrency '$c' for $p_model" >&2; exit 1; }
  done
  seen_cfg=""
  for cfg in ${p_cfg//,/ }; do
    [[ "$cfg" == "tp2" || "$cfg" == "pp2" || "$cfg" == "single" ]] || {
      echo "[variance-job] config must be tp2, pp2 or single, got '$cfg'" \
           "for $p_model" >&2; exit 1; }
    [[ " $seen_cfg " == *" $cfg "* ]] && {
      echo "[variance-job] duplicate config '$cfg' for $p_model" >&2; exit 1; }
    seen_cfg+="$cfg "
    N_ARMS=$(( N_ARMS + $(wc -w <<<"${p_conc//,/ }") ))
  done
done

# Point cost straight from the experiment YAML, so the estimate tracks the
# window if configs/experiment.yaml ever changes.
POINT_S="$("$PYTHON" -c '
import yaml
c = yaml.safe_load(open("configs/experiment.yaml"))
print(int(c["warmup_s"] + c["measure_s"] + c["cooldown_s"]))
' 2>/dev/null || echo 1800)"
N_POINTS=$(( N_ARMS * VAR_REPEATS ))
EST_H=$(( N_POINTS * POINT_S / 3600 ))
# One run_models.sh call == one model launch. INTERLEAVE pays for the extra
# launches with launch-to-launch variance the stacked form cannot see.
N_LEGS=0
for entry in "${POINTS[@]}"; do
  read -r _ _ p_cfg _ <<<"$entry"
  N_LEGS=$(( N_LEGS + $(wc -w <<<"${p_cfg//,/ }") ))
done
(( INTERLEAVE == 1 )) && N_LAUNCHES=$(( N_LEGS * VAR_REPEATS )) \
                      || N_LAUNCHES=$N_LEGS

print_plan() {
  echo "[plan] OUT_DIR=$OUT_DIR  VAR_REPEATS=$VAR_REPEATS  INTERLEAVE=$INTERLEAVE"
  echo "[plan] PYTHON=$PYTHON"
  echo "[plan] contested points (arms are repeated exactly as listed):"
  for entry in "${POINTS[@]}"; do
    read -r p_model p_conc p_cfg p_note <<<"$entry"
    printf '[plan]   %-32s c=%-6s %-15s %s\n' \
      "$p_model" "$p_conc" "$p_cfg" "$p_note"
  done
  echo "[plan] prep: source check, Ray health, head cache, worker mirror"
  if [[ "$SKIP_INTERCONNECT" == "0" ]]; then
    echo "[plan] microbench: all-reduce + point-to-point sendrecv -> $INTERCONNECT_OUT_DIR"
  fi
  echo "[plan] $N_ARMS arms x $VAR_REPEATS repeats = $N_POINTS points" \
       "x ${POINT_S}s = ~${EST_H} h, before model loads"
  echo "[plan] $N_LAUNCHES model launches (dual legs all run before any single leg)"
  echo "[plan] finalization: variance_report.py -> $OUT_DIR/variance.md"
}

if [[ "$PLAN_ONLY" == "1" ]]; then
  print_plan
  exit 0
fi

require_local_connectx_ip "$HEAD_IP" "variance campaign head role" || exit 1
acquire_campaign_lock || exit 1

mkdir -p "$OUT_DIR"
LOG="$OUT_DIR/run_variance_job_$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

log() { echo "[variance-job $(date '+%F %T')] $*"; }
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

# A prep failure means the job would waste GPU-hours: stop immediately.
prep() {
  local name="$1"; shift
  log "=== prep: $name ==="
  if run_child "$@"; then log "prep OK: $name"; else
    log "PREP FAILED: $name -- aborting before any measurement"; exit 1
  fi
}

# A measurement-leg failure is logged and the job moves on; --skip-done makes a
# later rerun pick up exactly the missing points.
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
    log "(If this is a rerun after the single legs already finished, single"
    log " replaced the Ray head container; restart Ray on both nodes.)"
    return 1
  fi
}

# A stale server from an earlier session collides with the first launch and also
# invalidates the idle-GPU precondition of the interconnect microbench.
no_stale_server() {
  if docker exec "$VLLM_CONTAINER" pgrep -f "vllm serve" >/dev/null 2>&1; then
    log "a vLLM server is already running in $VLLM_CONTAINER; stop it first:"
    log "  docker exec $VLLM_CONTAINER pkill -f 'vllm serve'"
    return 1
  fi
}

# Only the files that actually feed harness_source_sha256 need to be committed;
# this script and variance_report.py are deliberately outside that set, so a
# clean-tree check over all of scripts/ would block the run for no reason.
fingerprinted_sources_committed() {
  local tracked="bench configs scripts/launch_server.sh scripts/start_ray.sh
    scripts/run_all.sh scripts/run_models.sh scripts/models_for_config.py
    scripts/swept_concurrencies.py scripts/cluster_runtime_fingerprint.py"
  git rev-parse --verify HEAD >/dev/null 2>&1 || {
    log "repository has no recoverable Git commit"; return 1; }
  if ! git diff --quiet -- $tracked || ! git diff --cached --quiet -- $tracked; then
    log "measurement-affecting sources have uncommitted changes:"
    git diff --name-only -- $tracked
    git diff --cached --name-only -- $tracked
    log "commit them so every row names a recoverable revision, then rerun"
    return 1
  fi
}

warm_head_cache() {
  local labels="$1"
  MODEL_LABELS="$labels" CHECK_ONLY=1 scripts/predownload.sh all && return 0
  log "head cache incomplete; downloading pinned snapshots"
  MODEL_LABELS="$labels" scripts/predownload.sh all
}

trap 'handle_signal 130' INT
trap 'handle_signal 143' TERM

log "variance campaign start; log: $LOG"
print_plan

VAR_LABELS=""
for entry in "${POINTS[@]}"; do
  read -r p_model _ _ _ <<<"$entry"
  [[ " $VAR_LABELS " == *" $p_model "* ]] || VAR_LABELS+="$p_model "
done

prep "measurement-affecting sources committed" fingerprinted_sources_committed
prep "Ray cluster active on head+worker"       ray_ok
prep "no stale vLLM server in the container"   no_stale_server
prep "head HF cache has pinned snapshots"      warm_head_cache "$VAR_LABELS"
if [[ "$SKIP_MIRROR" == "1" ]]; then
  log "SKIP_MIRROR=1; assuming the worker cache still holds these snapshots"
else
  # Cheap when already mirrored: complete repos on the worker are skipped.
  prep "mirror pinned snapshots to Hoedus"     scripts/mirror_worker_cache.sh all
fi

# Point-to-point BEFORE any model is served: the microbench needs an idle GPU,
# and phase 2 destroys the Ray cluster it needs. Non-fatal -- a missing sendrecv
# CSV costs the paper one sentence, not 20 hours of serving measurements.
if [[ "$SKIP_INTERCONNECT" == "1" ]]; then
  log "SKIP_INTERCONNECT=1; no point-to-point artifact will be produced"
else
  leg "interconnect all-reduce + sendrecv" \
    env OUT_DIR="$INTERCONNECT_OUT_DIR" scripts/interconnect_extra_bench.sh
fi

# One run_models.sh call per (entry, config, repeat). Passing REPEATS=$rep with
# --skip-done leaves exactly one repeat missing, so each call measures one new
# window per concurrency and relaunches the server on the way in.
run_arm() {
  local rep="$1" model="$2" concs="$3" cfg="$4" tag
  if (( INTERLEAVE == 1 )); then
    tag="rep$rep/$VAR_REPEATS $cfg $model c=$concs"
  else
    tag="$cfg $model c=$concs x$VAR_REPEATS"
  fi
  leg "$tag" \
    env OUT_DIR="$OUT_DIR" VAR_MODELS="$model" VAR_CONCURRENCIES="$concs" \
        VAR_CONFIGS="$cfg" VAR_REPEATS="$rep" \
    scripts/variance_subset.sh
}

# phase 1 = dual-node, phase 2 = single. Never interleave the two phases:
# launch_server.sh single does `docker rm -f $VLLM_CONTAINER`, replacing the Ray
# head container, and Hoedus has no SSH path back.
run_phase() {
  local want_single="$1" rep entry p_model p_conc p_cfg cfg is_single
  local -a reps
  if (( INTERLEAVE == 1 )); then reps=($(seq 1 "$VAR_REPEATS"))
  else reps=("$VAR_REPEATS"); fi
  for rep in "${reps[@]}"; do
    for entry in "${POINTS[@]}"; do
      read -r p_model p_conc p_cfg _ <<<"$entry"
      for cfg in ${p_cfg//,/ }; do
        [[ "$cfg" == "single" ]] && is_single=1 || is_single=0
        (( is_single == want_single )) || continue
        run_arm "$rep" "$p_model" "${p_conc//,/ }" "$cfg"
      done
    done
  done
}

log "=== phase 1: dual-node arms (Ray cluster stays up) ==="
run_phase 0

log "NOTE: starting single-node legs; the Ray cluster will be torn down."
log "=== phase 2: single-node arms ==="
run_phase 1

# Report even after partial failure: a half-finished subset still bounds the
# spread at the points that did land, and shows what a rerun still owes.
log "=== summarizing spread and margin separation ==="
"$PYTHON" scripts/variance_report.py --index "$OUT_DIR/index.csv" \
  --md "$OUT_DIR/variance.md" --tex "$OUT_DIR/variance_numbers.tex" \
  --baseline results/index.csv || log "variance_report.py failed"

if (( ${#FAILED[@]} )); then
  log "COMPLETED WITH ${#FAILED[@]} FAILED LEG(S):"
  for name in "${FAILED[@]}"; do log "  - $name"; done
  log "rerun this script to retry only the missing points (--skip-done)"
  log "if the single legs already ran, restart Ray on both nodes first"
  exit 1
fi

log "all legs OK; table in $OUT_DIR/variance.md, macros in"
log "$OUT_DIR/variance_numbers.tex, log in $LOG"
