#!/usr/bin/env bash
# Shared ConnectX and container settings for the two node DGX Spark benchmark.
# Source from other scripts: source scripts/connectx_env.sh
# cisc-diya = 10.10.10.1, cisc-hoedus = 10.10.10.2. These rendezvous names
# resolve to the primary ConnectX addresses on both nodes via /etc/hosts.
#
# Defaults describe the campaign cluster: two QSFP cables, four ConnectX-7
# interfaces per node (one subnet each, see docs/cluster-setup.md), RDMA (RoCEv2)
# for every NCCL collective and point-to-point transfer, and NCCL logging of
# the transport each channel actually used. NCCL_SOCKET_IFNAME and
# GLOO_SOCKET_IFNAME inherit the full CX_IFACE list; DIYA_IP / HOEDUS_IP /
# HEAD_IP stay on the first subnet for the Ray rendezvous. Override CX_IFACE
# for a single cable and NCCL_IB_DISABLE=1 for the TCP-socket ablation.

CONNECTX_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$CONNECTX_SCRIPT_DIR/load_env.sh"

export CX_IFACE="${CX_IFACE:-enp1s0f0np0,enP2p1s0f0np0,enp1s0f1np1,enP2p1s0f1np1}"

export DIYA_HOST="${DIYA_HOST:-cisc-diya}"
export HOEDUS_HOST="${HOEDUS_HOST:-cisc-hoedus}"
export DIYA_IP="${DIYA_IP:-10.10.10.1}"
export HOEDUS_IP="${HOEDUS_IP:-10.10.10.2}"

# head node for Ray and the torch rendezvous, Diya by default
export HEAD_HOST="${HEAD_HOST:-$DIYA_HOST}"
export HEAD_IP="${HEAD_IP:-$DIYA_IP}"
export RAY_PORT="${RAY_PORT:-6379}"

# Force every collective and control channel onto ConnectX, never the campus
# wireless path that measured 64 to 132 ms against about 1 ms here.
export NCCL_SOCKET_IFNAME="$CX_IFACE"
export GLOO_SOCKET_IFNAME="$CX_IFACE"
export VLLM_HOST_IP="${VLLM_HOST_IP:-$HEAD_IP}"

# NCCL data plane uses RDMA over ConnectX by default. Set NCCL_IB_DISABLE=1
# to fall back to TCP sockets, for example to diagnose a RoCE fault.
# GLOO and the Ray rendezvous use TCP sockets in both modes.
export NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-0}"
# NCCL_DEBUG=INFO with NCCL_DEBUG_SUBSYS=INIT,NET logs one "via NET/IB" or
# "via NET/Socket" line per channel, at communicator setup only, so it never
# touches a measurement window. run_models.sh fails a dual launch whose log
# shows the wrong transport; audit_nccl_transport.py reports every launch.
export NCCL_DEBUG="${NCCL_DEBUG:-INFO}"
export NCCL_DEBUG_SUBSYS="${NCCL_DEBUG_SUBSYS:-INIT,NET}"
# NCCL RDMA device allowlist. Empty discovers every device. Set for the
# dual-cable layout when NCCL selects fewer than all four devices:
# NCCL_IB_HCA="rocep1s0f0,roceP2p1s0f0,rocep1s0f1,roceP2p1s0f1"
export NCCL_IB_HCA="${NCCL_IB_HCA:-}"
# Empty loads the library NCCL discovers. Set to "none" to use the built-in
# IB transport instead of the HPC-X RDMA plugin bundled in the vLLM image.
export NCCL_NET_PLUGIN="${NCCL_NET_PLUGIN:-}"
# RoCEv2 GID index used by the built-in IB transport. Empty defers to NCCL.
export NCCL_IB_GID_INDEX="${NCCL_IB_GID_INDEX:-}"

# Container settings. A mutable monthly tag alone is not sufficient provenance
# for a paper rerun, so the image is pinned by digest. This is NGC 26.09-py3
# (vLLM 0.29.0, CUDA 13.4.1, NCCL 2.31.2, HPC-X 2.51), required by the newer
# checkpoints in configs/models.yaml. The baselines/ campaigns used 26.04-py3
# (vLLM 0.19.0): nvcr.io/nvidia/vllm@sha256:98494fdaea68ad892c107180b0be41c99caa3c77683175becc4e202b2cb75b27
export VLLM_IMAGE="${VLLM_IMAGE:-nvcr.io/nvidia/vllm@sha256:557747337846eecf34486866f3d992383c6f8af869fdb0e365b5093fcaee02c2}"
export VLLM_CONTAINER="${VLLM_CONTAINER:-vllm-spark}"
export HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface-$UID}"
# HF_CACHE is the host Hugging Face home. Every host and container process uses
# its repository cache below that root.
export HF_HOME="$HF_CACHE"
export HF_HUB_CACHE="${HF_CACHE%/}/hub"
export HF_MODULES_CACHE="${HF_CACHE%/}/modules"
export HF_CACHE_UID="$(id -u)"
export HF_CACHE_GID="$(id -g)"
# Keep Hugging Face downloads in repo-local blobs. The cache-wide Xet blob
# store creates an extra symlink level that complicates mirroring and cleanup.
export HF_HUB_DISABLE_SHARED_BLOBS=1
# Use Docker's host-derived resolver by default. An explicit public resolver can
# be unreachable on managed networks even while the host resolver works, which
# would make the pinned Ray install fail before the cluster starts. Override
# this only when the local Docker resolver is known to be broken.
export CONTAINER_DNS="${CONTAINER_DNS-}"

# The image digest pins the platform stack. start_ray.sh installs the pinned
# Ray runtime using scripts/ray-runtime-constraints.txt, which also fixes its
# Python dependency versions to the latest full runtime recorded in this repo.
# Update the image, Ray version, and constraints together.
export RAY_VERSION="${RAY_VERSION:-2.56.1}"

check_hf_cache_access() {
  local script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  local python="${PYTHON:-python3}"
  mkdir -p "$HF_HUB_CACHE" || {
    echo "[cache] cannot create HF_HUB_CACHE=$HF_HUB_CACHE as UID:GID $HF_CACHE_UID:$HF_CACHE_GID" >&2
    return 1
  }
  "$python" "$script_dir/cache_access_probe.py" "$HF_HUB_CACHE"
}

check_hf_cache_access_in_image() {
  local script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  docker run --rm --user "$HF_CACHE_UID:$HF_CACHE_GID" \
    --entrypoint python3 \
    -v "$HF_CACHE:/hf-cache" \
    -v "$script_dir/cache_access_probe.py:/tmp/cache_access_probe.py:ro" \
    -e HOME=/tmp -e HF_HOME=/hf-cache -e HF_HUB_CACHE=/hf-cache/hub \
    "$VLLM_IMAGE" /tmp/cache_access_probe.py /hf-cache/hub
}

# Safety helpers shared by every launcher/orchestrator. Paper measurements must
# originate on the configured head because local GPU power is part of each row.
#
# CX_IFACE may be a comma-separated list. These functions iterate over every
# interface in the list.
connectx_local_ipv4() {
  local iface
  local -a ifaces
  IFS=',' read -ra ifaces <<< "$CX_IFACE"
  for iface in "${ifaces[@]}"; do
    local addr
    addr=$(ip -4 -o addr show "$iface" 2>/dev/null \
      | awk 'NR == 1 {split($4, a, "/"); print a[1]}')
    if [[ -n "$addr" ]]; then
      echo "$addr"
      return 0
    fi
  done
  return 1
}

require_local_connectx_ip() {
  local expected="$1" role="${2:-requested role}"
  local iface found=0
  local -a ifaces
  IFS=',' read -ra ifaces <<< "$CX_IFACE"
  for iface in "${ifaces[@]}"; do
    local actual
    actual=$(ip -4 -o addr show "$iface" 2>/dev/null \
      | awk '{split($4, a, "/"); print a[1]}')
    if [[ -n "$actual" ]] && grep -Fxq -- "$expected" <<< "$actual"; then
      found=1
      break
    fi
  done
  if [[ "$found" != "1" ]]; then
    echo "[cluster] expected IP $expected for $role not found on any interface in CX_IFACE ($CX_IFACE)" >&2
    return 1
  fi
}

# Link reachability through TCP does not prove that verbs are available. Check
# each requested rail before a launcher removes/recreates the serving container.
require_connectx_rdma() {
  case "$NCCL_IB_DISABLE" in
    1) return 0 ;;
    0) ;;
    *) echo "[cluster] NCCL_IB_DISABLE must be 0 (RDMA) or 1 (TCP)" >&2; return 1 ;;
  esac
  local iface links
  local -a ifaces
  command -v rdma >/dev/null || {
    echo "[cluster] rdma command missing; install iproute2 RDMA tooling" >&2
    return 1
  }
  links="$(rdma link show)" || return 1
  IFS=',' read -ra ifaces <<< "$CX_IFACE"
  for iface in "${ifaces[@]}"; do
    if ! awk -v iface="$iface" '
      {active=0; linked=0; physical=0;
       for(i=1;i<NF;i++) {
         if($i=="state" && $(i+1)=="ACTIVE") active=1;
         if($i=="physical_state" && $(i+1)=="LINK_UP") physical=1;
         if($i=="netdev" && $(i+1)==iface) linked=1;
       }
       if(active && physical && linked) found=1}
      END {exit !found}' <<< "$links"; then
      echo "[cluster] no ACTIVE/LINK_UP RDMA port mapped to $iface" >&2
      return 1
    fi
  done
}

# One physical head GPU and its local cache are shared by every campaign,
# regardless of output directory or serving-container name. Every repository
# command that changes or measures these resources takes this same lock.
_resource_lock_id="$(hostname -s 2>/dev/null || printf 'unknown-host')"
export CAMPAIGN_LOCK_FILE="${CAMPAIGN_LOCK_FILE:-${RESOURCE_LOCK_FILE:-/tmp/dgx-spark-resources_${_resource_lock_id}.lock}}"
export RESOURCE_LOCK_FILE="$CAMPAIGN_LOCK_FILE"
export RESOURCE_LOCK_WAIT_SECONDS="${RESOURCE_LOCK_WAIT_SECONDS:-3600}"
export RESOURCE_WAIT_TIMEOUT_SECONDS="${RESOURCE_WAIT_TIMEOUT_SECONDS:-1800}"
export RESOURCE_WAIT_INTERVAL_SECONDS="${RESOURCE_WAIT_INTERVAL_SECONDS:-5}"
unset _resource_lock_id

acquire_resource_lock() {
  if [[ "${CAMPAIGN_LOCK_HELD:-0}" == "1" && -n "${CAMPAIGN_LOCK_FD:-}" \
        && -e "/proc/$$/fd/$CAMPAIGN_LOCK_FD" ]]; then
    local inherited_path expected_path
    inherited_path="$(readlink "/proc/$$/fd/$CAMPAIGN_LOCK_FD" 2>/dev/null || true)"
    expected_path="$(readlink -f "$CAMPAIGN_LOCK_FILE" 2>/dev/null || true)"
    if [[ "$inherited_path" == "$expected_path" ]] \
        && flock -n "$CAMPAIGN_LOCK_FD"; then
      export RESOURCE_LOCK_INHERITED=1
      return 0
    fi
  fi
  unset CAMPAIGN_LOCK_HELD CAMPAIGN_LOCK_FD

  if [[ ! "$RESOURCE_LOCK_WAIT_SECONDS" =~ ^[0-9]+$ ]]; then
    echo "[resources] RESOURCE_LOCK_WAIT_SECONDS must be a non-negative integer" >&2
    return 2
  fi
  # flock(2) locks an inode through any descriptor and never needs write
  # permission, so open an EXISTING lock file read-only. That matters on a
  # shared head node: /tmp is sticky and world-writable, and under
  # fs.protected_regular=2 the kernel refuses the O_CREAT open that `>` always
  # performs whenever the file belongs to another user -- whatever its mode is,
  # so chmod cannot fix it. Only creating the file for the first time needs
  # write access, and that is a plain create in a directory we can write.
  # The open is also checked rather than redirected: `exec` without a command
  # applies its redirections to the shell permanently, so a `2>/dev/null` here
  # would survive the success path and silently discard every later error.
  if [[ -e "$CAMPAIGN_LOCK_FILE" ]]; then
    if ! exec {CAMPAIGN_LOCK_FD}<"$CAMPAIGN_LOCK_FILE"; then
      echo "[resources] resource lock exists but is not readable:" >&2
      ls -l "$CAMPAIGN_LOCK_FILE" >&2 2>/dev/null || true
      echo "[resources] make it readable (chmod a+r) so every user on this host" \
           "can take the same lock." >&2
      return 1
    fi
  else
    # Create a read-only lock inode so another account can open it to take the
    # same flock. Noclobber makes simultaneous first use safe.
    if ! (umask 0222; set -o noclobber; : >"$CAMPAIGN_LOCK_FILE") 2>/dev/null \
        && [[ ! -e "$CAMPAIGN_LOCK_FILE" ]]; then
      echo "[resources] cannot create the resource lock $CAMPAIGN_LOCK_FILE" >&2
      echo "[resources] its directory must be writable by this user" >&2
      return 1
    fi
    if ! exec {CAMPAIGN_LOCK_FD}<"$CAMPAIGN_LOCK_FILE"; then
      echo "[resources] cannot open the resource lock $CAMPAIGN_LOCK_FILE" >&2
      return 1
    fi
  fi
  if ! flock -n "$CAMPAIGN_LOCK_FD"; then
    echo "[resources] another repository operation holds $CAMPAIGN_LOCK_FILE; waiting up to ${RESOURCE_LOCK_WAIT_SECONDS}s" >&2
    if ! flock -w "$RESOURCE_LOCK_WAIT_SECONDS" "$CAMPAIGN_LOCK_FD"; then
      echo "[resources] timed out waiting for $CAMPAIGN_LOCK_FILE" >&2
      return 1
    fi
  fi
  export CAMPAIGN_LOCK_FD
  export CAMPAIGN_LOCK_HELD=1
  export RESOURCE_LOCK_INHERITED=0
}

# Existing callers use this name; it now takes the shared resource lock.
acquire_campaign_lock() { acquire_resource_lock; }

resource_check_once() {
  local worker_scope="${1:-auto}" mode="${2:-all}"
  local local_output="" worker_output="" local_status=0 worker_status=0
  local container_running=0 check_worker=0 ray_status=""
  local manager="$CONNECTX_SCRIPT_DIR/resource_manager.py"
  local python="${RESOURCE_MANAGER_PYTHON:-python3}"

  case "$worker_scope" in auto|required|none) ;; *)
    echo "[resources] invalid worker scope '$worker_scope'" >&2; return 2 ;; esac
  case "$mode" in all|serving) ;; *)
    echo "[resources] invalid resource-check mode '$mode'" >&2; return 2 ;; esac

  if local_output="$("$python" "$manager" --mode "$mode" 2>&1)"; then
    local_status=0
  else
    local_status=$?
  fi
  if (( local_status > 1 )); then
    printf '%s\n' "$local_output" >&2
    return 2
  fi

  if docker inspect -f '{{.State.Running}}' "$VLLM_CONTAINER" 2>/dev/null \
      | grep -q '^true$'; then
    container_running=1
  fi
  if [[ "$worker_scope" == "required" ]]; then
    check_worker=1
  elif [[ "$worker_scope" == "auto" && "$container_running" == "1" ]]; then
    ray_status="$(docker exec "$VLLM_CONTAINER" ray status 2>/dev/null || true)"
    [[ "$ray_status" == *"Active:"* ]] && check_worker=1
  fi
  if [[ "$check_worker" == "1" ]]; then
    if [[ "$container_running" != "1" ]]; then
      echo "[resources] UNKNOWN: required Ray container $VLLM_CONTAINER is not running" >&2
      return 2
    fi
    local -a worker_check_args=(--mode "$mode" --worker --head-ip "$HEAD_IP" --worker-ip "$HOEDUS_IP")
    [[ "$worker_scope" == "auto" ]] && worker_check_args+=(--allow-missing-worker)
    if worker_output="$(docker exec -i "$VLLM_CONTAINER" python3 - "${worker_check_args[@]}" \
        < "$manager" 2>&1)"; then
      worker_status=0
    else
      worker_status=$?
    fi
    if (( worker_status > 1 )) || \
        { (( worker_status == 1 )) && [[ "$worker_output" != *"[resources] BUSY"* ]]; }; then
      printf '%s\n' "$worker_output" >&2
      return 2
    fi
  fi

  [[ -n "$local_output" ]] && printf '%s\n' "$local_output"
  [[ -n "$worker_output" ]] && printf '%s\n' "$worker_output"
  if (( local_status == 1 || worker_status == 1 )); then
    return 1
  fi
}

wait_for_resources_idle() {
  local worker_scope="${1:-auto}" mode="${2:-all}"
  local elapsed=0 status=0 output=""
  if [[ ! "$RESOURCE_WAIT_TIMEOUT_SECONDS" =~ ^[0-9]+$ \
        || ! "$RESOURCE_WAIT_INTERVAL_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
    echo "[resources] wait timeout must be non-negative and interval must be positive" >&2
    return 2
  fi
  while :; do
    if output="$(resource_check_once "$worker_scope" "$mode" 2>&1)"; then
      echo "$output"
      return 0
    else
      status=$?
    fi
    if (( status != 1 )); then
      [[ -n "$output" ]] && printf '%s\n' "$output" >&2
      echo "[resources] availability could not be confirmed; refusing to proceed" >&2
      return 2
    fi
    if (( elapsed >= RESOURCE_WAIT_TIMEOUT_SECONDS )); then
      printf '%s\n' "$output" >&2
      echo "[resources] timed out after ${elapsed}s waiting for idle resources" >&2
      return 1
    fi
    echo "[resources] resources are occupied; waiting (${elapsed}/${RESOURCE_WAIT_TIMEOUT_SECONDS}s)" >&2
    [[ -n "$output" ]] && printf '%s\n' "$output" >&2
    sleep "$RESOURCE_WAIT_INTERVAL_SECONDS"
    elapsed=$((elapsed + RESOURCE_WAIT_INTERVAL_SECONDS))
  done
}

