#!/usr/bin/env bash
# Shared ConnectX and container settings for the two node DGX Spark benchmark.
# Source from other scripts: source scripts/connectx_env.sh
# Diya = 10.10.10.1, Hoedus = 10.10.10.2 on ConnectX interface enp1s0f0np0.
# The names Diya and Hoedus resolve to these ConnectX addresses on both nodes
# via /etc/hosts, including self references, so prefer the names everywhere.
#
# CX_IFACE accepts a comma-separated list for multi-cable setups, e.g.:
#   export CX_IFACE="enp1s0f0np0,enP2p1s0f0np0,enp1s0f1np1,enP2p1s0f1np1"
# NCCL_SOCKET_IFNAME and GLOO_SOCKET_IFNAME inherit the full list. NCCL stripes
# across all listed interfaces. DIYA_IP / HOEDUS_IP / HEAD_IP stay on one subnet
# for the Ray rendezvous.

_CONNECTX_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$_CONNECTX_HERE/load_env.sh"
unset _CONNECTX_HERE

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

# ConnectX is an Ethernet NIC. Keep NCCL on sockets unless you have verified
# RoCE is configured, in which case set NCCL_IB_DISABLE=0 and test it as a
# separate data point for the paper.
export NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-1}"
export NCCL_DEBUG="${NCCL_DEBUG:-WARN}"

# Container settings. The digest is the validated 26.04-py3 image; a mutable
# monthly tag alone is not sufficient provenance for a paper rerun.
export VLLM_IMAGE="${VLLM_IMAGE:-nvcr.io/nvidia/vllm@sha256:98494fdaea68ad892c107180b0be41c99caa3c77683175becc4e202b2cb75b27}"
export VLLM_CONTAINER="${VLLM_CONTAINER:-vllm-spark}"
export HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"
# Use Docker's host-derived resolver by default. An explicit public resolver can
# be unreachable on managed networks even while the host resolver works, which
# would make the pinned Ray install fail before the cluster starts. Override
# this only when the local Docker resolver is known to be broken.
export CONTAINER_DNS="${CONTAINER_DNS-}"

# nvcr.io/nvidia/vllm tags do not consistently bundle Ray (26.04-py3 does not,
# though earlier tags did), so start_ray.sh installs it explicitly. Pinned to
# the version NVIDIA's own 26.01-py3 image shipped, a combination already
# validated against this vLLM build. Re-check when bumping VLLM_IMAGE.
export RAY_VERSION="${RAY_VERSION:-2.53.0}"

# Safety helpers shared by every launcher/orchestrator. Paper measurements must
# originate on the configured head because local GPU power is part of each row.
#
# CX_IFACE may be a comma-separated list. These functions iterate over every
# interface in the list.
connectx_local_ipv4() {
  local iface
  IFS=',' read -ra _cx_ifaces <<< "$CX_IFACE"
  for iface in "${_cx_ifaces[@]}"; do
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
  IFS=',' read -ra _cx_ifaces <<< "$CX_IFACE"
  for iface in "${_cx_ifaces[@]}"; do
    local actual
    actual=$(ip -4 -o addr show "$iface" 2>/dev/null \
      | awk 'NR == 1 {split($4, a, "/"); print a[1]}')
    if [[ -n "$actual" && "$actual" == "$expected" ]]; then
      found=1
      break
    fi
  done
  if [[ "$found" != "1" ]]; then
    echo "[cluster] expected IP $expected for $role not found on any interface in CX_IFACE ($CX_IFACE)" >&2
    return 1
  fi
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
