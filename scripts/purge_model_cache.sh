#!/usr/bin/env bash
# Operator entry point for model cache cleanup.
# Selects repos from MODELS_YAML and runs the container-side Python implementation.
#
#   MODEL_LABELS="qwen3.6-27b" scripts/purge_model_cache.sh
#   DRY_RUN=1 MODEL_LABELS="qwen3.6-27b qwen3.6-27b-fp8" scripts/purge_model_cache.sh
#
# For each selected label this removes its model repo (and an explicit
# tokenizer repo), except repos that some label NOT selected still references,
# so a later model never loses a shared snapshot. scripts/run_job.sh calls it
# after a model's last replicate; it is also safe to run by hand between
# models. A node that is still serving refuses the purge.
#
# The deletion runs as root inside the Ray head container (the cache is written
# by containers); the worker side is a Ray task pinned to the worker node.
# Without a running Ray cluster only the head cache is purged, through a
# throwaway container.
#
# Env: MODEL_LABELS (optional; selects pinned model snapshots), MODELS_YAML, PYTHON,
#      PURGE_WORKER (1 also purges the worker; default 1 when Ray is up),
#      PURGE_COMPILE_CACHE (1 also clears vLLM's torch.compile cache, default 0),
#      PURGE_CACHE_GARBAGE (prunes detached revisions, incomplete files, and
#                           unreferenced shared blobs; default 1, set to 0 to
#                           pass --no-prune-cache-garbage),
#      DRY_RUN=1 (report what would be deleted).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/connectx_env.sh"
require_local_connectx_ip "$HEAD_IP" "model cache cleanup head role" || exit 1
acquire_resource_lock || exit 1

MODELS_YAML="${MODELS_YAML:-configs/models.yaml}"
PYTHON="${PYTHON:-python3}"
MODEL_LABELS="${MODEL_LABELS:-}"
DRY_RUN="${DRY_RUN:-0}"
PURGE_COMPILE_CACHE="${PURGE_COMPILE_CACHE:-0}"
PURGE_CACHE_GARBAGE="${PURGE_CACHE_GARBAGE:-1}"

for flag_name in DRY_RUN PURGE_COMPILE_CACHE PURGE_CACHE_GARBAGE; do
  [[ "${!flag_name}" == "0" || "${!flag_name}" == "1" ]] || {
    echo "[purge] $flag_name must be 0 or 1" >&2; exit 2; }
done
if [[ -z "${MODEL_LABELS// /}" && "$PURGE_CACHE_GARBAGE" != "1" ]]; then
  echo "[purge] set MODEL_LABELS when PURGE_CACHE_GARBAGE=0" >&2
  exit 2
fi

# Command substitution (not process substitution) so an unknown label fails
# the script instead of reading as "nothing to purge".
if ! repo_text="$("$PYTHON" - "$MODELS_YAML" $MODEL_LABELS <<'PY'
import sys
import yaml

path, wanted = sys.argv[1], set(sys.argv[2:])
models = (yaml.safe_load(open(path)) or {}).get("models") or []
unknown = wanted - {m.get("label") for m in models}
if unknown:
    raise SystemExit(f"[purge] unknown label(s) in {path}: {' '.join(sorted(unknown))}")

def repos(entry):
    names = [entry["name"]]
    if entry.get("tokenizer"):
        names.append(entry["tokenizer"])
    return names

keep = {r for m in models if m.get("label") not in wanted for r in repos(m)}
selected = []
for m in models:
    if m.get("label") in wanted:
        selected += [r for r in repos(m) if r not in keep and r not in selected]
print("\n".join(selected))
PY
)"; then
  exit 2
fi
if [[ -z "$repo_text" && "$PURGE_CACHE_GARBAGE" != "1" ]]; then
  echo "[purge] nothing to purge: every repo of '$MODEL_LABELS' is still used elsewhere"
  exit 0
fi
REPOS=()
if [[ -n "$repo_text" ]]; then mapfile -t REPOS <<<"$repo_text"; fi

ray_up=0
if docker exec "$VLLM_CONTAINER" ray status 2>/dev/null | grep -q "Active"; then
  ray_up=1
fi
PURGE_WORKER="${PURGE_WORKER:-$ray_up}"
args=()
[[ "$DRY_RUN" == "1" ]] && args+=(--dry-run)
[[ "$PURGE_COMPILE_CACHE" == "1" ]] && args+=(--compile-cache)
[[ "$PURGE_CACHE_GARBAGE" == "0" ]] && args+=(--no-prune-cache-garbage)
if [[ "$PURGE_WORKER" == "1" ]]; then
  if [[ "$ray_up" != "1" ]]; then
    echo "[purge] PURGE_WORKER=1 needs the Ray cluster; start it or set PURGE_WORKER=0" >&2
    exit 1
  fi
  args+=(--worker "$WORKER_IP")
fi

if [[ "$PURGE_WORKER" == "1" ]]; then
  wait_for_resources_idle required || exit 1
else
  wait_for_resources_idle auto || exit 1
fi

echo "[purge] ${#REPOS[@]} repo(s) for '${MODEL_LABELS:-none}'" \
     "(worker: $([[ "$PURGE_WORKER" == "1" ]] && echo "$WORKER_IP" || echo no))"
if [[ "$ray_up" == "1" ]]; then
  validate_local_topology_links || exit 1
  if [[ "$PURGE_WORKER" == "1" ]]; then
    validate_cluster_runtime || exit 1
  fi
  docker cp "$HERE/purge_model_cache_container.py" \
    "$VLLM_CONTAINER":/tmp/purge_model_cache_container.py
  docker exec -e HF_HOME=/hf-cache -e HF_HUB_CACHE=/hf-cache/hub \
    -e HF_MODULES_CACHE=/hf-cache/modules \
    -e HF_HUB_DISABLE_SHARED_BLOBS="$HF_HUB_DISABLE_SHARED_BLOBS" \
    "$VLLM_CONTAINER" \
    python3 -u /tmp/purge_model_cache_container.py "${args[@]}" "${REPOS[@]}"
else
  validate_local_topology_links || exit 1
  docker run --rm -i -e HF_HOME=/hf-cache -e HF_HUB_CACHE=/hf-cache/hub \
    -e HF_MODULES_CACHE=/hf-cache/modules \
    -e HF_HUB_DISABLE_SHARED_BLOBS="$HF_HUB_DISABLE_SHARED_BLOBS" \
    -v "$HF_CACHE":/hf-cache "$VLLM_IMAGE" \
    python3 -u - "${args[@]}" "${REPOS[@]}" \
      < "$HERE/purge_model_cache_container.py"
fi
