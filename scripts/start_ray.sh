#!/usr/bin/env bash
# Start Ray inside the vLLM container, pinned to ConnectX, for dual node serving.
#   On Diya:   scripts/start_ray.sh head
#   On Hoedus: scripts/start_ray.sh worker
#   Either:    scripts/start_ray.sh stop
# Ray runs inside the container so its version matches the vLLM build exactly.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/connectx_env.sh"

ROLE="${1:-head}"

docker_common=( --name "$VLLM_CONTAINER" --gpus all --network host --ipc=host
  -e NCCL_SOCKET_IFNAME="$CX_IFACE" -e GLOO_SOCKET_IFNAME="$CX_IFACE"
  -e NCCL_IB_DISABLE="$NCCL_IB_DISABLE" -e NCCL_DEBUG="$NCCL_DEBUG"
  --entrypoint /bin/bash
  -v "$HF_CACHE":/root/.cache/huggingface )
if [[ -n "${HF_TOKEN:-}" ]]; then docker_common+=( -e HF_TOKEN="$HF_TOKEN" ); fi

show_container_logs() {
  echo "[ray] $VLLM_CONTAINER is not running; recent container logs:"
  docker logs --tail 200 "$VLLM_CONTAINER" || true
  echo "[ray] container state:"
  docker ps -a --filter "name=$VLLM_CONTAINER" || true
}

require_running() {
  sleep 8
  if ! docker inspect -f '{{.State.Running}}' "$VLLM_CONTAINER" 2>/dev/null | grep -q true; then
    show_container_logs
    exit 1
  fi
  echo "[ray] status:"
  docker exec "$VLLM_CONTAINER" ray status
}

case "$ROLE" in
  head)
    echo "[ray] head on $HEAD_IP:$RAY_PORT over $CX_IFACE"
    docker rm -f "$VLLM_CONTAINER" >/dev/null 2>&1 || true
    docker run -d "${docker_common[@]}" -e VLLM_HOST_IP="$HEAD_IP" "$VLLM_IMAGE" \
      -lc "ray start --head --node-ip-address=$HEAD_IP --port=$RAY_PORT --block"
    ;;
  worker)
    LOCAL_IP="$(ip -4 -o addr show "$CX_IFACE" | awk '{print $4}' | cut -d/ -f1)"
    if [[ -z "$LOCAL_IP" ]]; then
      echo "[ray] could not find an IPv4 address on $CX_IFACE" >&2
      exit 1
    fi
    echo "[ray] worker $LOCAL_IP joining $HEAD_IP:$RAY_PORT over $CX_IFACE"
    docker rm -f "$VLLM_CONTAINER" >/dev/null 2>&1 || true
    docker run -d "${docker_common[@]}" -e VLLM_HOST_IP="$LOCAL_IP" "$VLLM_IMAGE" \
      -lc "ray start --address=$HEAD_IP:$RAY_PORT --node-ip-address=$LOCAL_IP --block"
    ;;
  stop)
    docker rm -f "$VLLM_CONTAINER" >/dev/null 2>&1 || true
    echo "[ray] container removed"
    exit 0
    ;;
  *)
    echo "usage: start_ray.sh [head|worker|stop]"; exit 1 ;;
esac

require_running
