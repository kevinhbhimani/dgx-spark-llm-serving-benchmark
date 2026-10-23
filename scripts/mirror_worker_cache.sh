#!/usr/bin/env bash
# Mirror model weights from the head node's HF cache to the worker's, so
# tp2/pp2 launches find the same snapshot revision on every Ray node.
#
# Why: each node mounts its own local HF cache into the container
# (start_ray.sh); scripts/predownload.sh only acquires snapshots on the node
# where it runs, and the worker (Hoedus) cannot download from huggingface.co.
# With HF_HUB_OFFLINE=1 the head resolves the model to a local snapshot path
# and ships the exact pinned snapshot to the remote Ray worker over Ray, so the
# transfer does not depend on a separately configured SSH path.
#
#   scripts/mirror_worker_cache.sh tp2    # every model whose configs include tp2 (default)
#   scripts/mirror_worker_cache.sh pp2
#   scripts/mirror_worker_cache.sh all    # union of tp2 and pp2
#
# Acquire the repos on the head first (scripts/predownload.sh); this script
# refuses metadata-only stubs. Transfers are resumable at file granularity and
# repos already complete on the worker are skipped, so rerunning is cheap.
# Requires the Ray cluster to be up on both nodes (scripts/start_ray.sh).
#
# Env: MODELS_YAML, VLLM_CONTAINER (via connectx_env.sh),
#      PYTHON (host interpreter for the YAML parse, default python3),
#      MIRROR_MARGIN_GB (free disk to keep on the worker, default 10),
#      MODEL_LABELS (optional space-separated label allowlist; the campaign
#                    mirrors one model at a time and purges it afterwards),
#      DRY_RUN=1 (list the repos it would mirror, then exit).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/connectx_env.sh"
acquire_resource_lock || exit 1

CONFIG="${1:-tp2}"
case "$CONFIG" in tp2|pp2|all) ;;
  single) echo "single-node serving does not use the worker cache" >&2; exit 1 ;;
  *) echo "usage: mirror_worker_cache.sh [tp2|pp2|all]" >&2; exit 1 ;;
esac

MODELS_YAML="${MODELS_YAML:-configs/models.yaml}"
PYTHON="${PYTHON:-python3}"
MODEL_LABELS="${MODEL_LABELS:-}"

CONFIGS=("$CONFIG")
if [[ "$CONFIG" == "all" ]]; then CONFIGS=(tp2 pp2); fi

declare -A SEEN
SPECS=()
add_spec() {
  local repo="$1" revision="$2" kind="$3" key="$1|$2"
  if [[ -z "${SEEN[$key]:-}" ]]; then
    SEEN[$key]="$kind"
    SPECS+=("$repo|$revision|$kind")
  elif [[ "$kind" == "model" ]]; then
    # A repo used as both tokenizer and model needs the stronger weight check.
    SEEN[$key]="model"
  fi
}
for cfg in "${CONFIGS[@]}"; do
  rc=0
  entries="$("$PYTHON" "$HERE/models_for_config.py" "$MODELS_YAML" "$cfg")" || rc=$?
  if (( rc > 1 )); then
    echo "[mirror] cannot read $MODELS_YAML for $cfg" >&2
    exit 1
  fi
  [[ -z "$entries" ]] && continue
  while IFS='|' read -r label name revision maxlen gpu_util quant tok tokrev trust _max_num_seqs; do
    [[ -z "$name" ]] && continue
    if [[ -n "$MODEL_LABELS" && " $MODEL_LABELS " != *" $label "* ]]; then
      continue
    fi
    if [[ -z "$revision" ]]; then
      echo "[mirror] $label has no pinned revision in $MODELS_YAML" >&2
      exit 1
    fi
    add_spec "$name" "$revision" model
    if [[ -n "$tok" ]]; then
      if [[ -z "$tokrev" && "$tok" == "$name" ]]; then tokrev="$revision"; fi
      if [[ -z "$tokrev" ]]; then
        echo "[mirror] $label uses tokenizer $tok without tokenizer_revision" >&2
        exit 1
      fi
      add_spec "$tok" "$tokrev" tokenizer
    fi
  done <<<"$entries"
done

if [[ "${#SPECS[@]}" -eq 0 ]]; then
  echo "[mirror] no models request config '$CONFIG' in $MODELS_YAML" >&2
  exit 1
fi

MIRROR_SPECS=()
for spec in "${SPECS[@]}"; do
  IFS='|' read -r repo revision kind <<<"$spec"
  MIRROR_SPECS+=("$repo|$revision|${SEEN["$repo|$revision"]}")
done
echo "[mirror] ${#MIRROR_SPECS[@]} pinned snapshot(s) for '$CONFIG' -> worker cache"
printf '  %s\n' "${MIRROR_SPECS[@]}"
if [[ "${DRY_RUN:-0}" == "1" ]]; then
  echo "[mirror] DRY_RUN=1, nothing transferred"
  exit 0
fi

if ! docker exec "$VLLM_CONTAINER" ray status 2>/dev/null | grep -q "Active"; then
  echo "[mirror] ERROR: Ray cluster is not active in $VLLM_CONTAINER" >&2
  echo "[mirror] Start Ray first: scripts/start_ray.sh head (Diya), worker (Hoedus)" >&2
  exit 1
fi

wait_for_resources_idle required || exit 1

docker cp "$HERE/mirror_worker_cache.py" "$VLLM_CONTAINER":/tmp/mirror_worker_cache.py
docker exec -e MIRROR_MARGIN_GB="${MIRROR_MARGIN_GB:-10}" \
  -e EXPECTED_HEAD="$HEAD_IP" -e EXPECTED_WORKER="$HOEDUS_IP" \
  "$VLLM_CONTAINER" \
  python3 -u /tmp/mirror_worker_cache.py "${MIRROR_SPECS[@]}"
