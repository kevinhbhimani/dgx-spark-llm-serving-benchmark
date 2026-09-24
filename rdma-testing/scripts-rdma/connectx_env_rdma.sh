#!/usr/bin/env bash
# =============================================================================
# scripts-rdma/connectx_env_rdma.sh
#
# MINIMALLY MODIFIED COPY of scripts/connectx_env.sh
# Changes from original:
#   - NCCL_IB_DISABLE defaults to 0 (enable RDMA) instead of 1 (TCP)
#   - Added NCCL_IB_HCA, NCCL_IB_GID_INDEX, NCCL_IB_TC,
#     NCCL_IB_QPS_PER_CONNECTION, NCCL_IB_TIMEOUT, NCCL_NET_GDR_LEVEL
#
# Source from other scripts instead of scripts/connectx_env.sh:
#   source scripts-rdma/connectx_env_rdma.sh
# =============================================================================
# Shared ConnectX and container settings for the two node DGX Spark benchmark.
# Diya = 10.10.10.1, Hoedus = 10.10.10.2 on ConnectX interface enp1s0f0np0.

_CONNECTX_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_REPO_ROOT="$(cd "$_CONNECTX_HERE/../.." && pwd)"
source "$_REPO_ROOT/scripts/load_env.sh"
unset _CONNECTX_HERE _REPO_ROOT

export CX_IFACE="${CX_IFACE:-enp1s0f0np0}"

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

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CHANGE 1: Default to RDMA/RoCE enabled (was NCCL_IB_DISABLE=1 for TCP)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
export NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-0}"
export NCCL_DEBUG="${NCCL_DEBUG:-WARN}"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CHANGE 2: Added RDMA/RoCE-specific NCCL environment variables
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Restrict RDMA to the ConnectX HCA only (=mlx5_0 reserves all ports)
export NCCL_IB_HCA="${NCCL_IB_HCA:-=mlx5_0}"

# RoCEv2 GID index — typically 3 for RoCEv2, but verify with `show_gids`
# inside the container. Common values: 0=RoCEv1, 3=RoCEv2.
export NCCL_IB_GID_INDEX="${NCCL_IB_GID_INDEX:-3}"

# Traffic class for RoCE QoS (106 = standard RDMA DSCP mapping)
export NCCL_IB_TC="${NCCL_IB_TC:-106}"

# Number of queue pairs per connection — 4 is a good default for 200GbE
export NCCL_IB_QPS_PER_CONNECTION="${NCCL_IB_QPS_PER_CONNECTION:-4}"

# IB timeout (22 = ~67 seconds in IB timeout units)
# Increased from default to handle large all-reduces on busy links
export NCCL_IB_TIMEOUT="${NCCL_IB_TIMEOUT:-22}"

# GPU Direct RDMA level:
#   0 = disabled
#   1 = GDR using copy to intermediate buffer (safer)
#   2 = GDR using DMA from GPU memory (requires nvidia-peermem)
#   3 = GDR using DMA from GPU memory with P2P
#   5 = GDR using P2P with GPUDirect RDMA (requires NVLink or PCIe P2P)
# Default to 5 if the environment supports it; set to 0 to disable.
export NCCL_NET_GDR_LEVEL="${NCCL_NET_GDR_LEVEL:-5}"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Container settings (unchanged from original)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
export VLLM_IMAGE="${VLLM_IMAGE:-nvcr.io/nvidia/vllm@sha256:98494fdaea68ad892c107180b0be41c99caa3c77683175becc4e202b2cb75b27}"
export VLLM_CONTAINER="${VLLM_CONTAINER:-vllm-spark}"
export HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"
export CONTAINER_DNS="${CONTAINER_DNS-}"
export RAY_VERSION="${RAY_VERSION:-2.53.0}"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Safety helpers (unchanged from original)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
connectx_local_ipv4() {
  ip -4 -o addr show "$CX_IFACE" 2>/dev/null \
    | awk 'NR == 1 {split($4, a, "/"); print a[1]}'
}

require_local_connectx_ip() {
  local expected="$1" role="${2:-requested role}" actual
  actual="$(connectx_local_ipv4)"
  if [[ -z "$actual" || "$actual" != "$expected" ]]; then
    echo "[cluster] this host has ${actual:-no IPv4} on $CX_IFACE; $role requires $expected" >&2
    return 1
  fi
}

_campaign_lock_id="${VLLM_CONTAINER}_${HEAD_IP//[:.]/_}"
export CAMPAIGN_LOCK_FILE="${CAMPAIGN_LOCK_FILE:-/tmp/dgx-spark-benchmark_${_campaign_lock_id}.lock}"
unset _campaign_lock_id

acquire_campaign_lock() {
  if [[ "${CAMPAIGN_LOCK_HELD:-0}" == "1" ]]; then
    return 0
  fi
  if [[ -e "$CAMPAIGN_LOCK_FILE" ]]; then
    if ! exec {CAMPAIGN_LOCK_FD}<"$CAMPAIGN_LOCK_FILE"; then
      echo "[cluster] campaign lock exists but is not readable:" >&2
      ls -l "$CAMPAIGN_LOCK_FILE" >&2 2>/dev/null || true
      return 1
    fi
  elif ! exec {CAMPAIGN_LOCK_FD}>"$CAMPAIGN_LOCK_FILE"; then
    echo "[cluster] cannot create the campaign lock $CAMPAIGN_LOCK_FILE" >&2
    return 1
  fi
  if ! flock -n "$CAMPAIGN_LOCK_FD"; then
    echo "[cluster] another benchmark campaign holds $CAMPAIGN_LOCK_FILE" >&2
    return 1
  fi
  export CAMPAIGN_LOCK_HELD=1
}