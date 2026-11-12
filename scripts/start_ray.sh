#!/usr/bin/env bash
# Start Ray inside the vLLM container using the selected topology node.
#   scripts/start_ray.sh head     # defaults to configs/cluster.yaml
#   scripts/start_ray.sh worker
#   CLUSTER_CONFIG=/path/cluster.yaml scripts/start_ray.sh head  # override
#   Either:    scripts/start_ray.sh stop
# Ray runs inside the vLLM container at the version pinned by RAY_VERSION and
# scripts/ray-runtime-constraints.txt. The constraints file pins its dependencies
# on both nodes.
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
reject_solo_execution "Ray cluster management" || exit 2

ROLE="${1:-head}"
TORCHINDUCTOR_AUTOGRAD_CACHE="${TORCHINDUCTOR_AUTOGRAD_CACHE:-0}"
case "$ROLE" in
  head)
    if [[ "$LOCAL_NODE_ID" != "$HEAD_NODE_ID" ]]; then
      echo "[ray] local node $LOCAL_NODE_ID is the configured worker, not the head" >&2
      exit 2
    fi
    ;;
  worker)
    if [[ "$LOCAL_NODE_ID" != "$WORKER_NODE_ID" ]]; then
      echo "[ray] local node $LOCAL_NODE_ID is the configured head, not the worker" >&2
      exit 2
    fi
    ;;
  stop) ;;
  *) echo "usage: start_ray.sh [head|worker|stop]"; exit 1 ;;
esac
acquire_resource_lock || exit 1
if [[ "$ROLE" == "stop" ]]; then
  wait_for_resources_idle auto serving || exit 1
else
  wait_for_resources_idle auto || exit 1
fi

maybe_drop_caches() {
  if [[ "${DROP_CACHES:-0}" == "1" ]]; then
    echo "[ray] flushing page cache (DROP_CACHES=1)"
    sync
    echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null
    echo "[ray] page cache flushed"
  fi
}

# Some nvcr.io/nvidia/vllm tags do not bundle Ray. Install the same locked
# runtime on both nodes. A Ray version change requires a matching constraints
# file update.
RAY_CONSTRAINTS="$HERE/ray-runtime-constraints.txt"
LOCKED_RAY_VERSION="$(awk -F '==' '$1 == "ray" { print $2; exit }' "$RAY_CONSTRAINTS")"
if [[ -z "$LOCKED_RAY_VERSION" || "$RAY_VERSION" != "$LOCKED_RAY_VERSION" ]]; then
  echo "[ray] RAY_VERSION=$RAY_VERSION does not match $RAY_CONSTRAINTS (ray==$LOCKED_RAY_VERSION)" >&2
  echo "[ray] update RAY_VERSION and the constraints file together" >&2
  exit 2
fi
RAY_SETUP="pip install -q --constraint /tmp/ray-runtime-constraints.txt 'ray[default]==$RAY_VERSION'"

docker_common=( --name "$VLLM_CONTAINER" --gpus all --network host --ipc=host --cap-add=IPC_LOCK --ulimit memlock=-1:-1
  -e NCCL_SOCKET_IFNAME="$CX_IFACE" -e GLOO_SOCKET_IFNAME="$CX_IFACE"
  -e NCCL_IB_DISABLE="$NCCL_IB_DISABLE" -e NCCL_DEBUG="$NCCL_DEBUG"
  -e TOPOLOGY_SHA256="$TOPOLOGY_SHA256" -e LOCAL_NODE_ID="$LOCAL_NODE_ID"
  -e NCCL_DEBUG_SUBSYS="$NCCL_DEBUG_SUBSYS"
  -e NCCL_IB_HCA="$NCCL_IB_HCA" -e NCCL_NET_PLUGIN="$NCCL_NET_PLUGIN"
  -e NCCL_IB_GID_INDEX="$NCCL_IB_GID_INDEX"
  -e NCCL_NSOCKS_PERTHREAD="${NCCL_NSOCKS_PERTHREAD:-1}"
  -e NCCL_SOCKET_NTHREADS="${NCCL_SOCKET_NTHREADS:-1}"
  -e TORCHINDUCTOR_AUTOGRAD_CACHE="$TORCHINDUCTOR_AUTOGRAD_CACHE"
  -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1
  -e HF_HOME=/hf-cache -e HF_HUB_CACHE=/hf-cache/hub
  -e HF_MODULES_CACHE=/hf-cache/modules
  -e HF_CACHE_UID="$HF_CACHE_UID" -e HF_CACHE_GID="$HF_CACHE_GID"
  -e HF_HUB_DISABLE_SHARED_BLOBS="$HF_HUB_DISABLE_SHARED_BLOBS"
  -e BENCH_VLLM_IMAGE="$VLLM_IMAGE" -e BENCH_RAY_VERSION="$RAY_VERSION"
  --entrypoint /bin/bash
  -v "$HF_CACHE":/hf-cache
  -v "$RAY_CONSTRAINTS":/tmp/ray-runtime-constraints.txt:ro )
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
    validate_local_topology_links || exit 1
    if [[ "$NCCL_IB_DISABLE" == "0" && ! -d /dev/infiniband ]]; then
      echo "[ray] RDMA requires the host /dev/infiniband device directory" >&2
      exit 1
    fi
    check_hf_cache_access
    check_hf_cache_access_in_image
    echo "[ray] head node $LOCAL_NODE_ID on $LOCAL_IP:$RAY_PORT over $CX_IFACE"
    maybe_drop_caches
    docker rm -f "$VLLM_CONTAINER" >/dev/null 2>&1 || true
    docker run -d "${docker_common[@]}" -e VLLM_HOST_IP="$LOCAL_IP" "$VLLM_IMAGE" \
      -lc "$RAY_SETUP && umask 0002 && ray start --head --node-ip-address=$LOCAL_IP --port=$RAY_PORT --block"
    ;;
  worker)
    validate_local_topology_links || exit 1
    if [[ "$NCCL_IB_DISABLE" == "0" && ! -d /dev/infiniband ]]; then
      echo "[ray] RDMA requires the host /dev/infiniband device directory" >&2
      exit 1
    fi
    check_hf_cache_access
    check_hf_cache_access_in_image
    echo "[ray] worker node $LOCAL_NODE_ID on $LOCAL_IP joining $HEAD_IP:$RAY_PORT over $CX_IFACE"
    maybe_drop_caches
    docker rm -f "$VLLM_CONTAINER" >/dev/null 2>&1 || true
    docker run -d "${docker_common[@]}" -e VLLM_HOST_IP="$LOCAL_IP" "$VLLM_IMAGE" \
      -lc "$RAY_SETUP && umask 0002 && ray start --address=$HEAD_IP:$RAY_PORT --node-ip-address=$LOCAL_IP --block"
    ;;
  stop)
    docker rm -f "$VLLM_CONTAINER" >/dev/null 2>&1 || true
    echo "[ray] container removed"
    exit 0
    ;;
esac

require_running
