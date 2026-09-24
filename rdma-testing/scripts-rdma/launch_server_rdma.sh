#!/usr/bin/env bash
# =============================================================================
# scripts-rdma/launch_server_rdma.sh
#
# MINIMALLY MODIFIED COPY of scripts/launch_server.sh
# Changes from original:
#   - Sources connectx_env_rdma.sh instead of connectx_env.sh
#   - Added RDMA NCCL env vars to the single-mode docker run command
#   - Added RDMA NCCL env vars to the tp2/pp2 docker exec command
#   - Added a note that for tp2/pp2, NCCL vars are inherited from Ray start
#
# Usage (same as original):
#   bash scripts-rdma/launch_server_rdma.sh [single|tp2|pp2]
# =============================================================================
set -euo pipefail

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CHANGE: Source RDMA env instead of connectx_env.sh
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/connectx_env_rdma.sh"

CONFIG="${1:-single}"
MODEL="${MODEL:-Qwen/Qwen3-8B}"
REVISION="${REVISION:-}"
PORT="${PORT:-8000}"
DTYPE="${DTYPE:-auto}"
QUANT="${QUANT:-}"
TOKENIZER="${TOKENIZER:-}"
TOKENIZER_REVISION="${TOKENIZER_REVISION:-}"
if [[ -z "$TOKENIZER_REVISION" \
      && ( -z "$TOKENIZER" || "$TOKENIZER" == "$MODEL" ) ]]; then
  TOKENIZER_REVISION="$REVISION"
fi
TRUST_REMOTE_CODE="${TRUST_REMOTE_CODE:-}"
MAXLEN="${MAXLEN:-8704}"
GPU_UTIL="${GPU_UTIL:-0.80}"
PREFIX_CACHING="${PREFIX_CACHING:-0}"
GENERATION_CONFIG="${GENERATION_CONFIG:-vllm}"
TORCHINDUCTOR_AUTOGRAD_CACHE="${TORCHINDUCTOR_AUTOGRAD_CACHE:-0}"
HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"

hf_offline_env=()
if [[ "$HF_HUB_OFFLINE" == "1" ]]; then
  hf_offline_env=( -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 )
fi
container_dns_args=()
if [[ -n "$CONTAINER_DNS" ]]; then
  container_dns_args=( --dns "$CONTAINER_DNS" )
fi

common=( --host 0.0.0.0 --port "$PORT" --dtype "$DTYPE"
         --max-model-len "$MAXLEN" --gpu-memory-utilization "$GPU_UTIL"
         --generation-config "$GENERATION_CONFIG"
         --no-enable-log-requests )
if [[ "$PREFIX_CACHING" != "1" ]]; then
  common+=( --no-enable-prefix-caching )
fi
if [[ -n "$QUANT" ]]; then common+=( --quantization "$QUANT" ); fi
if [[ -n "$TOKENIZER" ]]; then common+=( --tokenizer "$TOKENIZER" ); fi
if [[ -n "$REVISION" ]]; then common+=( --revision "$REVISION" ); fi
if [[ -n "$TOKENIZER_REVISION" ]]; then
  common+=( --tokenizer-revision "$TOKENIZER_REVISION" )
fi
if [[ -n "$TRUST_REMOTE_CODE" ]]; then common+=( --trust-remote-code ); fi

require_local_connectx_ip "$HEAD_IP" "serving head role" || exit 1
echo "[launch] $CONFIG  model=$MODEL  image=$VLLM_IMAGE  iface=$CX_IFACE  head=$HEAD_IP"
echo "[launch] RDMA: NCCL_IB_DISABLE=$NCCL_IB_DISABLE  HCA=$NCCL_IB_HCA  GID=$NCCL_IB_GID_INDEX"

if [[ "${DROP_CACHES:-0}" == "1" ]]; then
  echo "[launch] flushing page cache (DROP_CACHES=1)"
  sync
  echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null
  echo "[launch] page cache flushed"
fi

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# NOTE: For tp2/pp2, NCCL environment is inherited from the Ray container
# (set by start_ray_rdma.sh). The docker exec here passes hf_offline_env
# and TORCHINDUCTOR only. RDMA NCCL vars are already in the container's env.
# For single mode, we set them explicitly below.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_require_ray() {
  if ! docker exec "$VLLM_CONTAINER" ray status 2>/dev/null | grep -q "Active"; then
    echo "[launch] ERROR: Ray cluster is not active in $VLLM_CONTAINER" >&2
    echo "[launch] Start Ray first: scripts-rdma/start_ray_rdma.sh head  (Diya)" >&2
    echo "[launch]                  scripts-rdma/start_ray_rdma.sh worker (Hoedus)" >&2
    exit 1
  fi
  echo "[launch] Ray cluster OK:"
  docker exec "$VLLM_CONTAINER" ray status
}

_check_local_memory() {
  echo "[launch] preflight: local free memory vs GPU_UTIL=$GPU_UTIL"
  # (same as original — omitted for brevity but functionally identical)
  GPU_UTIL="$GPU_UTIL" HF_CACHE="$HF_CACHE" python3 - <<'PY'
import os
def meminfo():
    fields = {}
    with open("/proc/meminfo") as handle:
        for line in handle:
            key, value = line.split(":", 1)
            fields[key] = int(value.split()[0]) * 1024
    return fields["MemFree"], fields["MemAvailable"], fields["MemTotal"]
util = float(os.environ["GPU_UTIL"])
free, available, total = meminfo()
need = util * total
ok = free >= need
print(f"[launch]   free {free / 2**30:.1f} (available "
      f"{available / 2**30:.1f}) of {total / 2**30:.1f} GiB, "
      f"need {need / 2**30:.1f} GiB: {'ok' if ok else 'LOW'}")
if not ok:
    raise SystemExit("[launch] PREFLIGHT FAILED: local memory remains low")
PY
}

case "$CONFIG" in
  single)
    echo "[launch] single node TP=1 PP=1, standalone container"
    _check_local_memory

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # CHANGE: Added RDMA NCCL env vars to single-mode docker run
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    run_env=( -e NCCL_SOCKET_IFNAME="$CX_IFACE" -e GLOO_SOCKET_IFNAME="$CX_IFACE"
              -e NCCL_IB_DISABLE="$NCCL_IB_DISABLE" -e NCCL_DEBUG="$NCCL_DEBUG"
              -e NCCL_IB_HCA="$NCCL_IB_HCA" -e NCCL_IB_GID_INDEX="$NCCL_IB_GID_INDEX"
              -e NCCL_IB_TC="$NCCL_IB_TC" -e NCCL_IB_QPS_PER_CONNECTION="$NCCL_IB_QPS_PER_CONNECTION"
              -e NCCL_IB_TIMEOUT="$NCCL_IB_TIMEOUT" -e NCCL_NET_GDR_LEVEL="$NCCL_NET_GDR_LEVEL"
              -e TORCHINDUCTOR_AUTOGRAD_CACHE="$TORCHINDUCTOR_AUTOGRAD_CACHE" )
    if [[ "$HF_HUB_OFFLINE" != "1" && -n "${HF_TOKEN:-}" ]]; then
      run_env+=( -e HF_TOKEN )
    fi
    docker rm -f "$VLLM_CONTAINER" >/dev/null 2>&1 || true
    docker run --rm --name "$VLLM_CONTAINER" --gpus all --network host --ipc=host \
      "${container_dns_args[@]}" \
      "${run_env[@]}" "${hf_offline_env[@]}" -v "$HF_CACHE":/root/.cache/huggingface \
      "$VLLM_IMAGE" \
      vllm serve "$MODEL" "${common[@]}" \
      --tensor-parallel-size 1 --pipeline-parallel-size 1
    ;;
  tp2)
    echo "[launch] dual node tensor parallel TP=2 PP=1 over Ray in $VLLM_CONTAINER"
    _require_ray
    # NOTE: NCCL env vars (including RDMA) are inherited from start_ray_rdma.sh
    docker exec "${hf_offline_env[@]}" \
      -e TORCHINDUCTOR_AUTOGRAD_CACHE="$TORCHINDUCTOR_AUTOGRAD_CACHE" \
      "$VLLM_CONTAINER" \
      vllm serve "$MODEL" "${common[@]}" \
      --tensor-parallel-size 2 --pipeline-parallel-size 1 \
      --distributed-executor-backend ray
    ;;
  pp2)
    echo "[launch] dual node pipeline parallel TP=1 PP=2 over Ray in $VLLM_CONTAINER"
    _require_ray
    # NOTE: NCCL env vars (including RDMA) are inherited from start_ray_rdma.sh
    docker exec "${hf_offline_env[@]}" \
      -e TORCHINDUCTOR_AUTOGRAD_CACHE="$TORCHINDUCTOR_AUTOGRAD_CACHE" \
      "$VLLM_CONTAINER" \
      vllm serve "$MODEL" "${common[@]}" \
      --tensor-parallel-size 1 --pipeline-parallel-size 2 \
      --distributed-executor-backend ray
    ;;
  *)
    echo "unknown config $CONFIG, use single, tp2, or pp2"; exit 1 ;;
esac