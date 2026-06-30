#!/usr/bin/env bash
# Shared ConnectX and container settings for the two node DGX Spark benchmark.
# Source from other scripts: source scripts/connectx_env.sh
# Diya = 10.10.10.1, Hoedus = 10.10.10.2 on ConnectX interface enp1s0f0np0.
# The names Diya and Hoedus resolve to these ConnectX addresses on both nodes
# via /etc/hosts, including self references, so prefer the names everywhere.

export CX_IFACE="enp1s0f0np0"

export DIYA_HOST="Diya"
export HOEDUS_HOST="Hoedus"
export DIYA_IP="10.10.10.1"
export HOEDUS_IP="10.10.10.2"

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

# Container settings. Pin this tag in the platform table for reproducibility.
# Check NGC for a newer monthly tag and pin whatever you validate on sm_121.
export VLLM_IMAGE="${VLLM_IMAGE:-nvcr.io/nvidia/vllm:26.04-py3}"
export VLLM_CONTAINER="${VLLM_CONTAINER:-vllm-spark}"
export HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"

# nvcr.io/nvidia/vllm tags do not consistently bundle Ray (26.04-py3 does not,
# though earlier tags did), so start_ray.sh installs it explicitly. Pinned to
# the version NVIDIA's own 26.01-py3 image shipped, a combination already
# validated against this vLLM build. Re-check when bumping VLLM_IMAGE.
export RAY_VERSION="${RAY_VERSION:-2.53.0}"