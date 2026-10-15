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
TORCHINDUCTOR_AUTOGRAD_CACHE="${TORCHINDUCTOR_AUTOGRAD_CACHE:-0}"

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

docker_common=( --name "$VLLM_CONTAINER" --gpus all --network host --ipc=host --cap-add=IPC_LOCK --ulimit memlock=-1:-1
  -e NCCL_SOCKET_IFNAME="$CX_IFACE" -e GLOO_SOCKET_IFNAME="$CX_IFACE"
  -e NCCL_IB_DISABLE="$NCCL_IB_DISABLE" -e NCCL_DEBUG="$NCCL_DEBUG"
  -e NCCL_DEBUG_SUBSYS="$NCCL_DEBUG_SUBSYS"
  -e NCCL_IB_HCA="$NCCL_IB_HCA" -e NCCL_NET_PLUGIN="$NCCL_NET_PLUGIN"
  -e NCCL_IB_GID_INDEX="$NCCL_IB_GID_INDEX"
  -e NCCL_NSOCKS_PERTHREAD="${NCCL_NSOCKS_PERTHREAD:-1}"
  -e NCCL_SOCKET_NTHREADS="${NCCL_SOCKET_NTHREADS:-1}"
  -e TORCHINDUCTOR_AUTOGRAD_CACHE="$TORCHINDUCTOR_AUTOGRAD_CACHE"
  -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1
  -e BENCH_VLLM_IMAGE="$VLLM_IMAGE" -e BENCH_RAY_VERSION="$RAY_VERSION"
  --entrypoint /bin/bash
  -v "$HF_CACHE":/root/.cache/huggingface )
if [[ -d /dev/infiniband ]]; then
  docker_common+=( --device=/dev/infiniband )
fi
if [[ -n "$CONTAINER_DNS" ]]; then docker_common+=( --dns "$CONTAINER_DNS" ); fi
# Ray workers serve exclusively from the preflighted, mounted cache.  Do not
# pass registry credentials into these long-lived offline containers.

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
    require_connectx_rdma || exit 1
    if [[ "$NCCL_IB_DISABLE" == "0" && ! -d /dev/infiniband ]]; then
      echo "[ray] RDMA requires the host /dev/infiniband device directory" >&2
      exit 1
    fi
    echo "[ray] head on $HEAD_IP:$RAY_PORT over $CX_IFACE"
    maybe_drop_caches
    docker rm -f "$VLLM_CONTAINER" >/dev/null 2>&1 || true
    docker run -d "${docker_common[@]}" -e VLLM_HOST_IP="$HEAD_IP" "$VLLM_IMAGE" \
      -lc "$RAY_SETUP && ray start --head --node-ip-address=$HEAD_IP --port=$RAY_PORT --block"
    ;;
  worker)
    require_local_connectx_ip "$HOEDUS_IP" "Ray worker role" || exit 1
    require_connectx_rdma || exit 1
    if [[ "$NCCL_IB_DISABLE" == "0" && ! -d /dev/infiniband ]]; then
      echo "[ray] RDMA requires the host /dev/infiniband device directory" >&2
      exit 1
    fi
    echo "[ray] worker $HOEDUS_IP joining $HEAD_IP:$RAY_PORT over $CX_IFACE"
    maybe_drop_caches
    docker rm -f "$VLLM_CONTAINER" >/dev/null 2>&1 || true
    docker run -d "${docker_common[@]}" -e VLLM_HOST_IP="$HOEDUS_IP" "$VLLM_IMAGE" \
      -lc "$RAY_SETUP && ray start --address=$HEAD_IP:$RAY_PORT --node-ip-address=$HOEDUS_IP --block"
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
