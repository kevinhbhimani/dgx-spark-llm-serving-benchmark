#!/usr/bin/env bash
# Shared ConnectX and container settings for the two node DGX Spark benchmark.
# Source from other scripts: source scripts/connectx_env.sh
# Diya = 10.10.10.1, Hoedus = 10.10.10.2 on ConnectX interface enp1s0f0np0.
# The names Diya and Hoedus resolve to these ConnectX addresses on both nodes
# via /etc/hosts, including self references, so prefer the names everywhere.

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
# The campus system resolver can be unavailable while direct DNS still works.
# Docker launchers use this resolver explicitly; set empty only when local DNS
# is known healthy and policy requires it.
export CONTAINER_DNS="${CONTAINER_DNS-8.8.8.8}"

# nvcr.io/nvidia/vllm tags do not consistently bundle Ray (26.04-py3 does not,
# though earlier tags did), so start_ray.sh installs it explicitly. Pinned to
# the version NVIDIA's own 26.01-py3 image shipped, a combination already
# validated against this vLLM build. Re-check when bumping VLLM_IMAGE.
export RAY_VERSION="${RAY_VERSION:-2.53.0}"
