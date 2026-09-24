#!/usr/bin/env bash
# =============================================================================
# scripts-rdma/start_ray_rdma.sh
#
# MINIMALLY MODIFIED COPY of scripts/start_ray.sh
# Changes from original:
#   - Sources connectx_env_rdma.sh instead of connectx_env.sh
#   - Added RDMA NCCL env vars (-e NCCL_IB_HCA, -e NCCL_IB_GID_INDEX, etc.)
#     to both docker_common (container launch) and the runtime exec path
#
# Usage (same as original):
#   On Diya:   bash scripts-rdma/start_ray_rdma.sh head
#   On Hoedus: bash scripts-rdma/start_ray_rdma.sh worker
#   Either:    bash scripts-rdma/start_ray_rdma.sh stop
# =============================================================================
set -euo pipefail

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CHANGE: Source RDMA env instead of connectx_env.sh
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/connectx_env_rdma.sh"

ROLE="${1:-head}"
TORCHINDUCTOR_AUTOGRAD_CACHE="${TORCHINDUCTOR_AUTOGRAD_CACHE:-0}"

maybe_drop_caches() {
  if [[ "${DROP_CACHES:-0}" == "1" ]]; then
    echo "[ray] flushing page cache (DROP_CACHES=1)"
    sync
    echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null
    echo "[ray] page cache flushed"
  fi
}

RAY_SETUP="pip install -q 'ray[default]==$RAY_VERSION'"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CHANGE: Added RDMA NCCL variables to docker_common
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
docker_common=( --name "$VLLM_CONTAINER" --gpus all --network host --ipc=host
  -e NCCL_SOCKET_IFNAME="$CX_IFACE" -e GLOO_SOCKET_IFNAME="$CX_IFACE"
  -e NCCL_IB_DISABLE="$NCCL_IB_DISABLE" -e NCCL_DEBUG="$NCCL_DEBUG"
  -e NCCL_IB_HCA="$NCCL_IB_HCA" -e NCCL_IB_GID_INDEX="$NCCL_IB_GID_INDEX"
  -e NCCL_IB_TC="$NCCL_IB_TC" -e NCCL_IB_QPS_PER_CONNECTION="$NCCL_IB_QPS_PER_CONNECTION"
  -e NCCL_IB_TIMEOUT="$NCCL_IB_TIMEOUT" -e NCCL_NET_GDR_LEVEL="$NCCL_NET_GDR_LEVEL"
  -e TORCHINDUCTOR_AUTOGRAD_CACHE="$TORCHINDUCTOR_AUTOGRAD_CACHE"
  -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1
  -e BENCH_VLLM_IMAGE="$VLLM_IMAGE" -e BENCH_RAY_VERSION="$RAY_VERSION"
  --entrypoint /bin/bash
  -v "$HF_CACHE":/root/.cache/huggingface )
if [[ -n "$CONTAINER_DNS" ]]; then docker_common+=( --dns "$CONTAINER_DNS" ); fi

show_container_logs() {
  echo "[ray] $VLLM_CONTAINER is not running; recent container logs:"
  docker logs --tail 200 "$VLLM_CONTAINER" || true
  echo "[ray] container state:"
  docker ps -a --filter "name=$VLLM_CONTAINER" || true
}

require_running() {
  local waited=0 timeout=180 status=""
  while (( waited < timeout )); do
    if ! docker inspect -f '{{.State.Running}}' "$VLLM_CONTAINER" 2>/dev/null | grep -q true; then
      show_container_logs
      exit 1
    fi
    status="$(docker exec "$VLLM_CONTAINER" ray status 2>/dev/null || true)"
    if grep -q '^Active:' <<<"$status"; then
      break
    fi
    sleep 3
    waited=$((waited + 3))
  done
  if (( waited >= timeout )); then
    echo "[ray] timed out after ${timeout}s waiting for ray status" >&2
    show_container_logs
    exit 1
  fi
  echo "[ray] status:"
  printf '%s\n' "$status"
}

case "$ROLE" in
  head)
    require_local_connectx_ip "$HEAD_IP" "Ray head role" || exit 1
    echo "[ray] head on $HEAD_IP:$RAY_PORT over $CX_IFACE"
    echo "[ray] RDMA: NCCL_IB_DISABLE=$NCCL_IB_DISABLE (0=RoCE, 1=TCP)"
    maybe_drop_caches
    docker rm -f "$VLLM_CONTAINER" >/dev/null 2>&1 || true
    docker run -d "${docker_common[@]}" -e VLLM_HOST_IP="$HEAD_IP" "$VLLM_IMAGE" \
      -lc "$RAY_SETUP && ray start --head --node-ip-address=$HEAD_IP --port=$RAY_PORT --block"
    ;;
  worker)
    require_local_connectx_ip "$HOEDUS_IP" "Ray worker role" || exit 1
    LOCAL_IP="$(connectx_local_ipv4)"
    if [[ -z "$LOCAL_IP" ]]; then
      echo "[ray] could not find an IPv4 address on $CX_IFACE" >&2
      exit 1
    fi
    echo "[ray] worker $LOCAL_IP joining $HEAD_IP:$RAY_PORT over $CX_IFACE"
    echo "[ray] RDMA: NCCL_IB_DISABLE=$NCCL_IB_DISABLE (0=RoCE, 1=TCP)"
    maybe_drop_caches
    docker rm -f "$VLLM_CONTAINER" >/dev/null 2>&1 || true
    docker run -d "${docker_common[@]}" -e VLLM_HOST_IP="$LOCAL_IP" "$VLLM_IMAGE" \
      -lc "$RAY_SETUP && ray start --address=$HEAD_IP:$RAY_PORT --node-ip-address=$LOCAL_IP --block"
    ;;
  stop)
    docker rm -f "$VLLM_CONTAINER" >/dev/null 2>&1 || true
    echo "[ray] container removed"
    exit 0
    ;;
  *)
    echo "usage: start_ray_rdma.sh [head|worker|stop]"; exit 1 ;;
esac

require_running