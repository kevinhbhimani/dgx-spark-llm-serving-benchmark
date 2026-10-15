#!/usr/bin/env bash
# Shared ConnectX and container settings for the two node DGX Spark benchmark.
# Source from other scripts: source scripts/connectx_env.sh
# Diya = 10.10.10.1, Hoedus = 10.10.10.2. The names Diya and Hoedus resolve to
# these ConnectX addresses on both nodes via /etc/hosts, including self
# references, so prefer the names everywhere.
#
# Defaults describe the campaign cluster: two QSFP cables, four ConnectX-7
# interfaces per node (one subnet each, see docs/cluster-setup.md), RDMA (RoCEv2)
# for every NCCL collective and point-to-point transfer, and NCCL logging of
# the transport each channel actually used. NCCL_SOCKET_IFNAME and
# GLOO_SOCKET_IFNAME inherit the full CX_IFACE list; DIYA_IP / HOEDUS_IP /
# HEAD_IP stay on the first subnet for the Ray rendezvous. Override CX_IFACE
# for a single cable and NCCL_IB_DISABLE=1 for the TCP-socket ablation.

_CONNECTX_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$_CONNECTX_HERE/load_env.sh"
unset _CONNECTX_HERE

export CX_IFACE="${CX_IFACE:-enp1s0f0np0,enP2p1s0f0np0,enp1s0f1np1,enP2p1s0f1np1}"

export DIYA_HOST="${DIYA_HOST:-Diya}"
export HOEDUS_HOST="${HOEDUS_HOST:-Hoedus}"
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
export HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"
# Use Docker's host-derived resolver by default. An explicit public resolver can
# be unreachable on managed networks even while the host resolver works, which
# would make the pinned Ray install fail before the cluster starts. Override
# this only when the local Docker resolver is known to be broken.
export CONTAINER_DNS="${CONTAINER_DNS-}"

# nvcr.io/nvidia/vllm 26.04 and 26.09 do not bundle Ray, so start_ray.sh
# installs it into both containers. 2.56.1 is the release vLLM 0.29.0's CI pins
# (requirements/test/cuda.txt: ray>=2.56.0,<2.57). Pipeline-parallel stage
# transfers use vLLM's own NCCL communicator (VLLM_USE_RAY_WRAPPED_PP_COMM=1,
# the default), so the cgraph/cupy extra is not needed. Re-check when bumping
# VLLM_IMAGE.
export RAY_VERSION="${RAY_VERSION:-2.56.1}"

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

# One fixed serving container and one head GPU means campaigns targeting
# different output directories still conflict. Scope the nonblocking lock to
# the physical head/container pair, not OUT_DIR.
_campaign_lock_id="${VLLM_CONTAINER}_${HEAD_IP//[:.]/_}"
export CAMPAIGN_LOCK_FILE="${CAMPAIGN_LOCK_FILE:-/tmp/dgx-spark-benchmark_${_campaign_lock_id}.lock}"
unset _campaign_lock_id

acquire_campaign_lock() {
  if [[ "${CAMPAIGN_LOCK_HELD:-0}" == "1" ]]; then
    return 0
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
      echo "[cluster] campaign lock exists but is not readable:" >&2
      ls -l "$CAMPAIGN_LOCK_FILE" >&2 2>/dev/null || true
      echo "[cluster] make it readable (chmod a+r) so every user on this head" \
           "node can take the same lock." >&2
      return 1
    fi
  elif ! exec {CAMPAIGN_LOCK_FD}>"$CAMPAIGN_LOCK_FILE"; then
    echo "[cluster] cannot create the campaign lock $CAMPAIGN_LOCK_FILE" >&2
    echo "[cluster] its directory must be writable, or point" \
         "CAMPAIGN_LOCK_FILE at a path shared by everyone who runs campaigns" \
         "on this head node." >&2
    return 1
  fi
  if ! flock -n "$CAMPAIGN_LOCK_FD"; then
    echo "[cluster] another benchmark campaign holds $CAMPAIGN_LOCK_FILE" >&2
    return 1
  fi
  export CAMPAIGN_LOCK_HELD=1
}
