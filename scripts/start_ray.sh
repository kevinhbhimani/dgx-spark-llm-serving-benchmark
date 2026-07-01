#!/usr/bin/env bash
# Start Ray inside the vLLM container, pinned to ConnectX, for dual node serving.
#   On Diya:   scripts/start_ray.sh head
#   On Hoedus: scripts/start_ray.sh worker
#   Either:    scripts/start_ray.sh stop
# Ray runs inside the container, installed at a pinned version (RAY_VERSION),
# so head and worker always agree even though it isn't bundled in every image tag.
#
# Optional env vars:
#   DROP_CACHES=1   flush this node's page cache before starting Ray (requires
#                   sudo); recommended on Grace unified memory to avoid Ray's
#                   node memory monitor false-OOM-killing a worker. Run on each
#                   node separately (head and worker), since this only flushes
#                   the local machine's cache.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/connectx_env.sh"

ROLE="${1:-head}"

maybe_drop_caches() {
  if [[ "${DROP_CACHES:-0}" == "1" ]]; then
    echo "[ray] flushing page cache (DROP_CACHES=1)"
    sync
    echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null
    echo "[ray] page cache flushed"
  fi
}

# Installed at container start rather than baked into the image: some
# nvcr.io/nvidia/vllm tags (e.g. 26.04-py3) do not bundle Ray. See RAY_VERSION
# in connectx_env.sh.
RAY_SETUP="pip install -q 'ray[default]==$RAY_VERSION'"

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
  # The container installs ray[default] before calling `ray start` (see
  # RAY_SETUP above), which can take well over a minute on a slow or flaky
  # DNS path to pypi.org, so poll for readiness instead of a fixed sleep.
  local waited=0 timeout=180
  while (( waited < timeout )); do
    if ! docker inspect -f '{{.State.Running}}' "$VLLM_CONTAINER" 2>/dev/null | grep -q true; then
      show_container_logs
      exit 1
    fi
    if docker exec "$VLLM_CONTAINER" ray status >/dev/null 2>&1; then
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
  docker exec "$VLLM_CONTAINER" ray status
}

case "$ROLE" in
  head)
    echo "[ray] head on $HEAD_IP:$RAY_PORT over $CX_IFACE"
    maybe_drop_caches
    docker rm -f "$VLLM_CONTAINER" >/dev/null 2>&1 || true
    docker run -d "${docker_common[@]}" -e VLLM_HOST_IP="$HEAD_IP" "$VLLM_IMAGE" \
      -lc "$RAY_SETUP && ray start --head --node-ip-address=$HEAD_IP --port=$RAY_PORT --block"
    ;;
  worker)
    LOCAL_IP="$(ip -4 -o addr show "$CX_IFACE" | awk '{print $4}' | cut -d/ -f1)"
    if [[ -z "$LOCAL_IP" ]]; then
      echo "[ray] could not find an IPv4 address on $CX_IFACE" >&2
      exit 1
    fi
    echo "[ray] worker $LOCAL_IP joining $HEAD_IP:$RAY_PORT over $CX_IFACE"
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
    echo "usage: start_ray.sh [head|worker|stop]"; exit 1 ;;
esac

require_running
