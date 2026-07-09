#!/usr/bin/env bash
# Pre-download model weights into the shared Hugging Face cache so that a later
# `vllm serve` does not spend its readiness budget pulling weights.
#
# Why: scripts/run_models.sh gives each server READY_TIMEOUT seconds (default
# 1200) to come up, then tears it down. On a cold cache the largest checkpoints
# spend most of that window just downloading (llama3.3-70b-fp8 took 803s and
# nemotron3-super-120b-a12b-nvfp4 took 1103s to download alone), so they were
# killed mid-load before ever serving. Warming the cache first removes that race.
#
#   scripts/predownload.sh single      # every model whose configs include single (default)
#   scripts/predownload.sh tp2
#   scripts/predownload.sh pp2
#   scripts/predownload.sh all         # every model in models.yaml, across all configs
#
# Downloads run inside the same VLLM_IMAGE and into the same HF_CACHE mount that
# launch_server.sh uses, so the serving path sees an identical, warm cache. Each
# unique repo is fetched once (a model shared across configs, or reused by an
# on-the-fly-quant entry like llama3.3-70b-fp8, is not downloaded twice). Gated
# repos (meta-llama) use HF_TOKEN from .env. Redundant PyTorch/GGUF copies
# (e.g. meta-llama's original/*.pth) are skipped; vLLM loads the safetensors.
#
# The download is idempotent and resumable: re-running skips files already
# present and continues any partial download.
#
# Env: MODELS_YAML, HF_CACHE, HF_TOKEN, VLLM_IMAGE (all via connectx_env.sh),
#      PYTHON (host interpreter for the YAML parse, default python3),
#      DRY_RUN=1 (list the repos it would fetch, then exit without downloading).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/connectx_env.sh"     # VLLM_IMAGE, HF_CACHE, HF_TOKEN, and .env

CONFIG="${1:-single}"
case "$CONFIG" in single|tp2|pp2|all) ;; *)
  echo "usage: predownload.sh [single|tp2|pp2|all]" >&2; exit 1 ;;
esac

MODELS_YAML="${MODELS_YAML:-configs/models.yaml}"
PYTHON="${PYTHON:-python3}"

# Which serving configs to cover. "all" unions every config so a shared repo is
# still fetched once.
CONFIGS=("$CONFIG")
if [[ "$CONFIG" == "all" ]]; then CONFIGS=(single tp2 pp2); fi

# Collect unique repos (model names, plus any explicit tokenizer repos) in
# first-seen order. models_for_config.py emits:
#   label|name|max_model_len|gpu_util|quant|tokenizer|trust_remote_code
declare -A SEEN
REPOS=()
for cfg in "${CONFIGS[@]}"; do
  # exits 1 when no model requests a config; tolerate that under "all"
  entries="$("$PYTHON" "$HERE/models_for_config.py" "$MODELS_YAML" "$cfg" 2>/dev/null || true)"
  [[ -z "$entries" ]] && continue
  while IFS='|' read -r label name maxlen gpu_util quant tok trust; do
    [[ -z "$name" ]] && continue
    if [[ -z "${SEEN[$name]:-}" ]]; then SEEN[$name]=1; REPOS+=("$name"); fi
    if [[ -n "$tok" && -z "${SEEN[$tok]:-}" ]]; then SEEN[$tok]=1; REPOS+=("$tok"); fi
  done <<<"$entries"
done

if [[ "${#REPOS[@]}" -eq 0 ]]; then
  echo "[predownload] no models request config '$CONFIG' in $MODELS_YAML" >&2
  exit 1
fi

echo "[predownload] ${#REPOS[@]} unique repo(s) for '$CONFIG' -> $HF_CACHE"
printf '  %s\n' "${REPOS[@]}"
if [[ "${DRY_RUN:-0}" == "1" ]]; then
  echo "[predownload] DRY_RUN=1, nothing downloaded"
  exit 0
fi

if [[ -z "${HF_TOKEN:-}" ]]; then
  echo "[predownload] note: HF_TOKEN is unset; gated repos (e.g. meta-llama) will fail." >&2
  echo "[predownload]       set HF_TOKEN in .env if any gated model does not download." >&2
fi

mkdir -p "$HF_CACHE"

run_env=()
if [[ -n "${HF_TOKEN:-}" ]]; then run_env+=( -e HF_TOKEN="$HF_TOKEN" ); fi

FAILURES=0
for repo in "${REPOS[@]}"; do
  echo "[predownload] === $repo ==="
  if docker run --rm --network host \
       "${run_env[@]}" -v "$HF_CACHE":/root/.cache/huggingface \
       "$VLLM_IMAGE" \
       python3 -c 'import sys
from huggingface_hub import snapshot_download
repo = sys.argv[1]
path = snapshot_download(repo, ignore_patterns=["original/*", "*.pth", "*.gguf"])
print("[predownload] cached", repo, "->", path)' \
       "$repo"; then
    echo "[predownload] done $repo"
  else
    echo "[predownload] FAILED $repo (gated repo without HF_TOKEN, or bad name?)" >&2
    FAILURES=$((FAILURES + 1))
  fi
done

if (( FAILURES > 0 )); then
  echo "[predownload] $FAILURES repo(s) failed; see output above" >&2
  exit 1
fi
echo "[predownload] all repos for '$CONFIG' cached under $HF_CACHE"
echo "[predownload] now run: scripts/run_models.sh $([[ "$CONFIG" == all ]] && echo single || echo "$CONFIG")"
