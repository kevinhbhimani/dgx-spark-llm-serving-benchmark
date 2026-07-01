#!/usr/bin/env bash
# Launch the vLLM OpenAI compatible server in single, tp2, or pp2 mode on the two
# node DGX Spark cluster, using the NVIDIA vLLM container. Run on the head node.
# For tp2 and pp2, start Ray on both nodes first with scripts/start_ray.sh, which
# brings up the shared container; this script then execs the server into it.
#
# Optional env vars:
#   DROP_CACHES=1   flush the page cache before launch (requires sudo); recommended
#                   before large model loads on Grace unified memory to avoid false OOM.
#   GPU_UTIL        override --gpu-memory-utilization (default 0.90 single, 0.80 tp2/pp2).
#                   On Grace unified memory, GPU util shares the same pool as host
#                   memory, so tp2/pp2 default lower to leave headroom for Ray's
#                   node memory monitor (see README troubleshooting).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/connectx_env.sh"

CONFIG="${1:-single}"          # single, tp2, pp2
MODEL="${MODEL:-Qwen/Qwen3-8B}"
PORT="${PORT:-8000}"
DTYPE="${DTYPE:-auto}"
QUANT="${QUANT:-}"             # for example fp8; empty means none
# vLLM max model length is prompt + generated tokens. The default experiment's
# largest bucket is 8192 input + 512 output, so use at least 8704 by default.
MAXLEN="${MAXLEN:-8704}"
if [[ "$CONFIG" == "tp2" || "$CONFIG" == "pp2" ]]; then
  GPU_UTIL="${GPU_UTIL:-0.80}"
else
  GPU_UTIL="${GPU_UTIL:-0.90}"
fi

common=( --host 0.0.0.0 --port "$PORT" --dtype "$DTYPE"
         --max-model-len "$MAXLEN" --gpu-memory-utilization "$GPU_UTIL"
         --no-enable-log-requests )
if [[ -n "$QUANT" ]]; then common+=( --quantization "$QUANT" ); fi

echo "[launch] $CONFIG  model=$MODEL  image=$VLLM_IMAGE  iface=$CX_IFACE  head=$HEAD_IP"

# Optionally flush stale page cache before large model loads.
# Stale cache causes false OOM on Grace unified memory; requires sudo.
if [[ "${DROP_CACHES:-0}" == "1" ]]; then
  echo "[launch] flushing page cache (DROP_CACHES=1)"
  sync
  echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null
  echo "[launch] page cache flushed"
fi

# For distributed configs, confirm Ray is healthy before committing to the launch.
_require_ray() {
  if ! docker exec "$VLLM_CONTAINER" ray status 2>/dev/null | grep -q "Active"; then
    echo "[launch] ERROR: Ray cluster is not active in $VLLM_CONTAINER" >&2
    echo "[launch] Start Ray first: scripts/start_ray.sh head  (Diya)" >&2
    echo "[launch]                  scripts/start_ray.sh worker (Hoedus)" >&2
    echo "[launch] Then verify with: docker exec $VLLM_CONTAINER ray status" >&2
    exit 1
  fi
  echo "[launch] Ray cluster OK:"
  docker exec "$VLLM_CONTAINER" ray status
}

case "$CONFIG" in
  single)
    echo "[launch] single node TP=1 PP=1, standalone container"
    run_env=( -e NCCL_SOCKET_IFNAME="$CX_IFACE" -e GLOO_SOCKET_IFNAME="$CX_IFACE"
              -e NCCL_IB_DISABLE="$NCCL_IB_DISABLE" -e NCCL_DEBUG="$NCCL_DEBUG" )
    if [[ -n "${HF_TOKEN:-}" ]]; then run_env+=( -e HF_TOKEN="$HF_TOKEN" ); fi
    docker run --rm --gpus all --network host --ipc=host \
      "${run_env[@]}" -v "$HF_CACHE":/root/.cache/huggingface \
      "$VLLM_IMAGE" \
      vllm serve "$MODEL" "${common[@]}" \
      --tensor-parallel-size 1 --pipeline-parallel-size 1
    ;;
  tp2)
    echo "[launch] dual node tensor parallel TP=2 PP=1 over Ray in $VLLM_CONTAINER"
    _require_ray
    docker exec "$VLLM_CONTAINER" \
      vllm serve "$MODEL" "${common[@]}" \
      --tensor-parallel-size 2 --pipeline-parallel-size 1 \
      --distributed-executor-backend ray
    ;;
  pp2)
    echo "[launch] dual node pipeline parallel TP=1 PP=2 over Ray in $VLLM_CONTAINER"
    _require_ray
    docker exec "$VLLM_CONTAINER" \
      vllm serve "$MODEL" "${common[@]}" \
      --tensor-parallel-size 1 --pipeline-parallel-size 2 \
      --distributed-executor-backend ray
    ;;
  *)
    echo "unknown config $CONFIG, use single, tp2, or pp2"; exit 1 ;;
esac
