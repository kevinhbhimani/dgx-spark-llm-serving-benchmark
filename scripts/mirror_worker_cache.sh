#!/usr/bin/env bash
# Mirror model weights from the head node's HF cache to the worker's, so
# tp2/pp2 launches find the same snapshot revision on every Ray node.
#
# Why: each node mounts its OWN local HF cache into the container
# (start_ray.sh); scripts/predownload.sh only ever warms the node it runs on,
# and the worker (Hoedus) can neither download from huggingface.co (no DNS)
# nor be reached over SSH. With HF_HUB_OFFLINE=1 the head resolves the model
# to a local snapshot path and ships it to the remote Ray worker, so a repo
# missing on the worker kills the launch with HFValidationError.
#
#   scripts/mirror_worker_cache.sh tp2    # every model whose configs include tp2 (default)
#   scripts/mirror_worker_cache.sh pp2
#   scripts/mirror_worker_cache.sh all    # union of tp2 and pp2
#
# Predownload the repos on the head first (scripts/predownload.sh); this script
# refuses metadata-only stubs. Transfers are resumable at file granularity and
# repos already complete on the worker are skipped, so rerunning is cheap.
# Requires the Ray cluster to be up on both nodes (scripts/start_ray.sh).
#
# Env: MODELS_YAML, VLLM_CONTAINER (via connectx_env.sh),
#      PYTHON (host interpreter for the YAML parse, default python3),
#      MIRROR_MARGIN_GB (free disk to keep on the worker, default 10),
#      DRY_RUN=1 (list the repos it would mirror, then exit).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/connectx_env.sh"

CONFIG="${1:-tp2}"
case "$CONFIG" in tp2|pp2|all) ;;
  single) echo "single-node serving does not use the worker cache" >&2; exit 1 ;;
  *) echo "usage: mirror_worker_cache.sh [tp2|pp2|all]" >&2; exit 1 ;;
esac

MODELS_YAML="${MODELS_YAML:-configs/models.yaml}"
PYTHON="${PYTHON:-python3}"

CONFIGS=("$CONFIG")
if [[ "$CONFIG" == "all" ]]; then CONFIGS=(tp2 pp2); fi

declare -A SEEN
REPOS=()
for cfg in "${CONFIGS[@]}"; do
  entries="$("$PYTHON" "$HERE/models_for_config.py" "$MODELS_YAML" "$cfg" 2>/dev/null || true)"
  [[ -z "$entries" ]] && continue
  while IFS='|' read -r label name maxlen gpu_util quant tok trust; do
    [[ -z "$name" ]] && continue
    if [[ -z "${SEEN[$name]:-}" ]]; then SEEN[$name]=1; REPOS+=("$name"); fi
    if [[ -n "$tok" && -z "${SEEN[$tok]:-}" ]]; then SEEN[$tok]=1; REPOS+=("$tok"); fi
  done <<<"$entries"
done

if [[ "${#REPOS[@]}" -eq 0 ]]; then
  echo "[mirror] no models request config '$CONFIG' in $MODELS_YAML" >&2
  exit 1
fi

echo "[mirror] ${#REPOS[@]} unique repo(s) for '$CONFIG' -> worker cache"
printf '  %s\n' "${REPOS[@]}"
if [[ "${DRY_RUN:-0}" == "1" ]]; then
  echo "[mirror] DRY_RUN=1, nothing transferred"
  exit 0
fi

if ! docker exec "$VLLM_CONTAINER" ray status 2>/dev/null | grep -q "Active"; then
  echo "[mirror] ERROR: Ray cluster is not active in $VLLM_CONTAINER" >&2
  echo "[mirror] Start Ray first: scripts/start_ray.sh head (Diya), worker (Hoedus)" >&2
  exit 1
fi

docker cp "$HERE/mirror_worker_cache.py" "$VLLM_CONTAINER":/tmp/mirror_worker_cache.py
docker exec -e MIRROR_MARGIN_GB="${MIRROR_MARGIN_GB:-10}" "$VLLM_CONTAINER" \
  python3 -u /tmp/mirror_worker_cache.py "${REPOS[@]}"
